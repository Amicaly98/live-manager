"""桌面稳定性耐久与生命周期测试（D5/D7 修订版）。

与旧版的关键差异（监督第一轮 D5）：
1. 耐久走**真实产品控制器**：LiveController + 替身平台 API（推流地址指向
   本地 TCP sink）+ 真实 FFmpeg 推本地合成媒体，经过产品 _ffmpeg_loop、
   控制代际与恢复路径——不再直接 Popen 独立 FFmpeg。
2. 判定器闭环：先用**同一个逐窗口缺口判定器**检出注入停顿
   （NtSuspendProcess 暂停推流进程），再进行正常耐久；并验证 kill 后
   产品自行恢复（_ffmpeg_loop 自动重启）。
3. 指标口径修正：RSS/句柄用 PowerShell（WorkingSet64/HandleCount）——
   旧版把 tasklist CSV 的"会话编号"列误当句柄计数。
4. 收尾资源：真实产品 stop 后核对 owner、后代进程、RSS/句柄与末尾新鲜度。
5. 生命周期轮支持 --backend-exe 直接跑打包 run.exe（证据与命令一致）。

用法（在仓库根）：
  .venv-test/Scripts/python.exe backend/tests/lifecycle_endurance.py \
      --cycles 10 --endurance-minutes 20 --port 18060 [--backend-exe backend/dist/run.exe]
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
RESULTS_DIR = BACKEND_DIR / "tests" / "_endurance_results"
PYTHON = sys.executable

WINDOW_SECONDS = 5.0          # 缺口判定窗口
GAP_MIN_BYTES = 100_000       # 窗口内增长低于该值 → 判定缺口（合成源约 195KB/s）
MIN_SAMPLE_COUNT = 6          # A1/A2 + Phase B baseline/recovery evidence
MIN_STEADY_SAMPLE_COUNT = 2
TOOL_VERSION = "20260922.lifecycle-endurance.v4"


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _executable_provenance(command: list | None) -> dict:
    """Hash a packaged backend executable when the cycle launches one."""
    executable = Path(str(command[0])) if command else None
    python_names = {"python.exe", "python3.exe", "py.exe"}
    if (executable is None or executable.suffix.lower() != ".exe"
            or executable.name.lower() in python_names):
        return {"backend_executable": None, "backend_exe_sha256": None}
    digest = _sha256_file(executable) if executable.is_file() else None
    return {"backend_executable": str(executable),
            "backend_exe_sha256": digest}


def _backend_source_hashes() -> dict:
    """Hash backend source files only; never read data/config artifacts."""
    candidates = []
    for source_root in (BACKEND_DIR / "app" / "core",
                        BACKEND_DIR / "app" / "api"):
        candidates.extend(sorted(source_root.glob("*.py")))
    candidates.extend([
        BACKEND_DIR / "app" / "models" / "schemas.py",
        BACKEND_DIR / "run.py",
    ])
    hashes = {}
    for path in sorted(set(candidates)):
        digest = _sha256_file(path)
        if digest:
            hashes[path.relative_to(REPO_ROOT).as_posix()] = digest
    return hashes


def _tool_provenance(command: list | None = None) -> dict:
    """Return exact harness, backend-source, and executable identities."""
    source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {"tool_version": TOOL_VERSION, "source_sha256": source_sha256,
            "backend_source_sha256": _backend_source_hashes(),
            **_executable_provenance(command)}


def _monotonic() -> float:
    """One seam for interval/deadline decisions (wall time is for labels only)."""
    return time.monotonic()


def _ps_output(script: str, timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def proc_metrics(pid: int) -> dict:
    """RSS 与真实句柄计数（PowerShell HandleCount）。

    D5 口径修正：tasklist CSV 没有句柄列（第 4 列是会话编号），旧版
    把它当句柄是字段选错；这里用 Get-Process 的 WorkingSet64/HandleCount。
    """
    out = _ps_output(
        f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
        f"if($p){{ Write-Output ($p.WorkingSet64.ToString() + ',' + $p.HandleCount.ToString()) }} "
        f"else {{ Write-Output 'gone' }}")
    if out == 'gone':
        return {"rss_mb": None, "handles": None, "alive": False,
                "status": "absent"}
    if not out:
        # Empty output means PowerShell/metrics collection failed.  Treating it
        # as a dead process would turn an evidence gap into a green cleanup.
        return {"rss_mb": None, "handles": None, "alive": None,
                "status": "unknown"}
    try:
        rss, handles = out.splitlines()[-1].split(',')
        return {"rss_mb": round(int(rss) / (1024 * 1024), 1),
                "handles": int(handles), "alive": True, "status": "present"}
    except Exception:
        return {"rss_mb": None, "handles": None, "alive": None,
                "status": "unknown"}


def list_descendants(pid: int) -> list:
    """Compatibility helper returning all identity-matched descendants."""
    lookup = _lookup_process_identity(pid)
    identity = lookup.get("identity")
    if not identity:
        return []
    return [int(row["pid"]) for row in _bfs_process_tree(identity, _process_table() or [])
            if int(row["pid"]) != int(pid)]


def _pid_alive(pid: int) -> bool:
    return proc_metrics(pid)["alive"] is True


def _pid_state(pid: int) -> dict:
    """Return process state without collapsing collection failure into gone."""
    metrics = proc_metrics(pid)
    return {"pid": pid, "alive": metrics.get("alive"),
            "status": metrics.get("status", "unknown")}


def _process_table() -> list | None:
    """Read PID/parent/creation tuples, or ``None`` when enumeration failed."""
    out = _ps_output(
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CreationDate | "
        "ConvertTo-Json -Compress")
    if not out:
        return None
    try:
        decoded = json.loads(out)
        rows = decoded if isinstance(decoded, list) else [decoded]
        table = []
        for row in rows:
            pid = row.get("ProcessId")
            parent = row.get("ParentProcessId")
            creation = row.get("CreationDate")
            if pid is None or creation in (None, ""):
                continue
            table.append({"pid": int(pid), "parent_pid": int(parent or 0),
                          "creation_time": str(creation)})
        return table
    except Exception:
        return None


def _same_process_identity(expected: dict, current: dict) -> bool:
    """PID reuse is not ownership: both PID and creation time must match."""
    return bool(
        expected and current
        and int(expected.get("pid", -1)) == int(current.get("pid", -2))
        and expected.get("creation_time") not in (None, "")
        and current.get("creation_time") not in (None, "")
        and str(expected.get("creation_time")) == str(current.get("creation_time"))
    )


def _bfs_process_tree(root_identity: dict, table: list) -> list:
    """Return the root and every current descendant in breadth-first order."""
    if not root_identity:
        return []
    by_pid = {int(row["pid"]): row for row in table or []
              if row.get("pid") is not None}
    root_pid = int(root_identity["pid"])
    current_root = by_pid.get(root_pid)
    if current_root is None or not _same_process_identity(root_identity, current_root):
        return []
    children = {}
    for row in by_pid.values():
        children.setdefault(int(row.get("parent_pid", 0)), []).append(row)
    queue = [current_root]
    seen = set()
    result = []
    while queue:
        current = queue.pop(0)
        key = (int(current["pid"]), str(current.get("creation_time", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(current)
        queue.extend(children.get(int(current["pid"]), []))
    return result


def _lookup_process_identity(pid: int) -> dict:
    table = _process_table()
    if table is None:
        return {"status": "unknown", "identity": None}
    row = next((item for item in table if int(item["pid"]) == int(pid)), None)
    return {"status": "present" if row else "absent", "identity": row}


def _capture_owned_tree(root_identity: dict) -> dict:
    table = _process_table()
    if table is None:
        return {"status": "unknown", "records": []}
    records = _bfs_process_tree(root_identity, table)
    return {"status": "present" if records else "absent", "records": records}


def _owned_residuals(created_records: list) -> dict:
    """Match only the recorded PID+creation identities in the current table."""
    table = _process_table()
    if table is None:
        return {"status": "unknown", "residual": []}
    by_pid = {int(row["pid"]): row for row in table or []}
    residual = []
    for expected in created_records or []:
        current = by_pid.get(int(expected["pid"]))
        if current is not None and _same_process_identity(expected, current):
            residual.append(expected)
    return {"status": "known", "residual": residual}


def _reclaim_owned_processes(records: list) -> list:
    """Best-effort rescue for this run's exact process identities only."""
    outcomes = []
    for expected in records or []:
        lookup = _lookup_process_identity(expected["pid"])
        current = lookup.get("identity")
        outcome = {"pid": expected["pid"],
                   "creation_time": expected.get("creation_time"),
                   "status": lookup.get("status")}
        if lookup.get("status") == "unknown":
            outcome["reclaimed"] = False
            outcome["reason"] = "identity_lookup_unknown"
        elif current is None:
            outcome["reclaimed"] = True
            outcome["reason"] = "already_exited"
        elif not _same_process_identity(expected, current):
            outcome["reclaimed"] = False
            outcome["reason"] = "pid_reused_or_identity_mismatch"
        else:
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(expected["pid"])],
                    capture_output=True, text=True, timeout=10)
                outcome["returncode"] = result.returncode
                outcome["reclaimed"] = result.returncode == 0
            except Exception as exc:
                outcome["reclaimed"] = False
                outcome["error"] = repr(exc)
        outcomes.append(outcome)
    return outcomes


# ==================== 网络工具（本地请求绕过环境代理） ====================

def _no_proxy_opener():
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_OPENER = _no_proxy_opener()


def http_ok(url: str, timeout=2.0) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_health(port: int, timeout: float = 45.0) -> bool:
    deadline = _monotonic() + timeout
    while _monotonic() < deadline:
        if http_ok(f"http://127.0.0.1:{port}/api/health", timeout=1.5):
            return True
        time.sleep(0.5)
    return False


def http_post(url: str, timeout=5.0) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, method="POST")
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ==================== 生命周期轮（真实后端进程启停） ====================

def run_one_cycle(cycle: int, port: int, data_dir: Path, backend_cmd: list) -> dict:
    record = {"cycle": cycle, "started_at": datetime.now().isoformat(),
              **_tool_provenance(backend_cmd)}
    env = {**os.environ, "BILIBILI_SKIP_MIGRATION": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    out_log = None
    proc = None
    root_identity = None
    created_processes = []
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        out_log = open(data_dir / "backend_stdout.log", "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            backend_cmd, cwd=str(data_dir), env=env,
            stdout=out_log, stderr=subprocess.STDOUT)
        record["pid"] = proc.pid
        record["command"] = ' '.join(str(c) for c in backend_cmd)
        root_lookup = _lookup_process_identity(proc.pid)
        record["root_identity_status"] = root_lookup["status"]
        root_identity = root_lookup.get("identity")
        if root_identity:
            record["root_identity"] = root_identity
        ready = wait_health(port)
        record["health_ok"] = ready
        if ready:
            time.sleep(0.5)
            record["metrics_ready"] = proc_metrics(proc.pid)
        # Capture the complete tree even when health failed; a failed startup
        # can still leave a worker descendant that belongs to this exact root.
        if root_identity:
            tree = _capture_owned_tree(root_identity)
            record["created_tree_status"] = tree["status"]
            created_processes = tree["records"]
            record["created_processes"] = created_processes
        else:
            record["created_tree_status"] = "unknown"
        # 优雅关闭（E2：uvicorn should_exit）
        record["shutdown_http"] = http_post(
            f"http://127.0.0.1:{port}/api/shutdown?stop_live=true")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record.setdefault("health_ok", False)
        record.setdefault("shutdown_http", False)
    finally:
        if proc is not None:
            try:
                proc.wait(timeout=20)
                record["exit_code"] = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as exc:
                    record["kill_error"] = repr(exc)
                record["exit_code"] = "killed_after_timeout"
            except Exception as exc:
                record["exit_code"] = "wait_failed"
                record["wait_error"] = repr(exc)
            time.sleep(0.5)
            # 收尾资源：只按本轮记录的 PID+creation 身份核对根与全部后代。
            # 枚举失败是 unknown，不能被当成“进程已退出”。
            if created_processes:
                residual_state = _owned_residuals(created_processes)
                record["residual_check_status"] = residual_state["status"]
                residual_records = residual_state["residual"]
                record["residual_processes"] = residual_records
                record["residual_alive"] = bool(residual_records)
                record["descendants_after"] = [
                    int(item["pid"]) for item in residual_records
                    if int(item["pid"]) != int(proc.pid)]
                if residual_records:
                    record["rescue"] = _reclaim_owned_processes(residual_records)
            elif record.get("root_identity_status") == "unknown":
                record["residual_check_status"] = "unknown"
                record["residual_alive"] = None
                record["descendants_after"] = []
            else:
                # The root exited before identity capture; the exact Popen
                # handle was still waited above, but no broad PID kill is safe.
                record["residual_check_status"] = "unknown"
                record["residual_alive"] = None
                record["descendants_after"] = []
        else:
            record.setdefault("exit_code", "spawn_failed")
            record["residual_check_status"] = "none"
            record["residual_alive"] = False
            record["descendants_after"] = []
        if out_log is not None:
            out_log.close()
        record["finished_at"] = datetime.now().isoformat()
    return record


def run_cycles(cycles: int, base_port: int, backend_cmd: list,
               data_root: Path = None) -> dict:
    results = {"backend_cmd": ' '.join(str(c) for c in backend_cmd),
               "cycles": [], "started_at": datetime.now().isoformat(),
               **_tool_provenance(backend_cmd)}
    for i in range(cycles):
        port = base_port + i
        data_dir = (data_root or RESULTS_DIR / f"cycle-{results['started_at'].replace(':', '')[:17]}") / f"data-{i + 1}"
        # --port/--data-dir 按轮次追加（run.py 与打包 exe 都必须显式传入：
        # 冻结版 BASE_DIR 指向临时解包目录，绝不回退默认）
        cmd = list(backend_cmd) + ["--port", str(port), "--data-dir", str(data_dir)]
        rec = run_one_cycle(i + 1, port, data_dir, cmd)
        results["cycles"].append(rec)
        print(f"cycle {rec['cycle']}: health={rec['health_ok']} "
              f"shutdown={rec.get('shutdown_http')} exit={rec.get('exit_code')} "
              f"residual={rec['residual_alive']} "
              f"rss={rec.get('metrics_ready', {}).get('rss_mb')}MB "
              f"handles={rec.get('metrics_ready', {}).get('handles')}", flush=True)
    ok = sum(1 for c in results["cycles"]
             if c["health_ok"] and c.get("shutdown_http")
             and c.get("exit_code") == 0 and not c["residual_alive"]
             and c.get("residual_check_status") == "known"
             and not c["descendants_after"])
    results["cycles_ok"] = ok
    results["finished_at"] = datetime.now().isoformat()
    print(f"cycles_ok={ok}/{cycles}", flush=True)
    return results


# ==================== 产品耐久（真实 LiveController 路径） ====================

def tcp_sink(port: int, stats: dict):
    """本地 FLV-over-TCP 接收端：支持断流后重连，累计字节数。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(2)
    stats["listening"] = True
    try:
        while not stats.get("stop"):
            srv.settimeout(1.0)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            stats["connections"] = stats.get("connections", 0) + 1
            stats["connected"] = True
            try:
                conn.settimeout(1.0)
                while not stats.get("stop"):
                    try:
                        data = conn.recv(65536)
                        if not data:
                            stats["disconnections"] = stats.get("disconnections", 0) + 1
                            break
                        stats["bytes"] = stats.get("bytes", 0) + len(data)
                        stats["last_byte_monotonic"] = _monotonic()
                        stats["last_byte_at"] = datetime.now().isoformat()
                    except socket.timeout:
                        continue
                    except (ConnectionResetError, OSError):
                        stats["disconnections"] = stats.get("disconnections", 0) + 1
                        break
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as e:
        stats["error"] = str(e)
    finally:
        srv.close()


def _suspend_windows(pid: int, suspend: bool) -> bool:
    """Windows 进程暂停/恢复（NtSuspendProcess/NtResumeProcess）。"""
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        # Keep the native ABI explicit: HANDLE is a pointer-sized value and
        # Nt*Process returns a signed 32-bit NTSTATUS (STATUS_SUCCESS == 0).
        HANDLE = ctypes.c_void_p
        NTSTATUS = ctypes.c_int32
        ntdll = ctypes.WinDLL('ntdll')
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = HANDLE
        kernel32.CloseHandle.argtypes = [HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        native_call = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        native_call.argtypes = [HANDLE]
        native_call.restype = NTSTATUS

        h = kernel32.OpenProcess(0x0800, False, wintypes.DWORD(pid))  # PROCESS_SUSPEND_RESUME
        if not h:
            return False
        operation_ok = False
        try:
            operation_ok = int(native_call(h)) == 0
        finally:
            close_ok = bool(kernel32.CloseHandle(h))
        return operation_ok and close_ok
    except Exception:
        return False


def _suspend_and_resume(pid: int, pause_seconds: float, sleep=time.sleep) -> dict:
    """Pause one owned pusher and always attempt its matching resume."""
    result = {"pid": pid, "suspend_ok": False, "resume_attempted": False,
              "resume_ok": False}
    if not pid:
        return result
    attempted = False
    try:
        attempted = True
        result["suspend_ok"] = bool(_suspend_windows(pid, True))
        if result["suspend_ok"] and pause_seconds > 0:
            sleep(pause_seconds)
    except Exception as exc:
        result["suspend_error"] = repr(exc)
    finally:
        if attempted:
            result["resume_attempted"] = True
            try:
                result["resume_ok"] = bool(_suspend_windows(pid, False))
            except Exception as exc:
                result["resume_error"] = repr(exc)
    return result


def _kill_owned_process(process, timeout: float = 10.0) -> dict:
    """Kill one exact Popen owner and require both command and exit evidence."""
    result = {"pid": getattr(process, "pid", None), "taskkill_ok": False,
              "old_owner_exited": False, "killed_ffmpeg": False}
    pid = result["pid"]
    if process is None or not pid:
        result["error"] = "missing_owned_process"
        return result
    try:
        command = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=timeout)
        result["taskkill_returncode"] = command.returncode
        result["taskkill_stdout"] = (command.stdout or "")[-1000:]
        result["taskkill_stderr"] = (command.stderr or "")[-1000:]
        result["taskkill_ok"] = command.returncode == 0
    except Exception as exc:
        result["taskkill_error"] = repr(exc)
        return result
    if result["taskkill_ok"]:
        try:
            if process.poll() is None:
                process.wait(timeout=timeout)
            result["old_owner_exited"] = process.poll() is not None
        except Exception as exc:
            result["wait_error"] = repr(exc)
    result["killed_ffmpeg"] = bool(
        result["taskkill_ok"] and result["old_owner_exited"])
    return result


class _StubPlatformApi:
    """替身平台：start_live 返回指向本地 sink 的推流地址；零网络。"""

    def __init__(self, sink_port: int):
        self._sink_port = sink_port
        self.stop_calls = []

    def retry_network_until_cancelled(self, cancel):
        import contextlib
        return contextlib.nullcontext()

    def get_csrf(self):
        return 'synthetic'

    def start_live(self, room_id, area_id, csrf):
        return True, {'code': 0, 'data': {'rtmp': {
            'addr': f'tcp://127.0.0.1:{self._sink_port}',
            'code': '?timeout=10000000'}}}

    def stop_live(self, room_id, csrf):
        self.stop_calls.append(room_id)
        return True, {'code': 0}

    def get_live_status(self, room_id):
        return True, {'code': 0, 'data': {'live_status': 1}}

    def get_push_url(self, room_id):
        return True, {'push_url': f'tcp://127.0.0.1:{self._sink_port}?timeout=10000000'}

    def update_area(self, room_id, area_id, csrf):
        return True, {'code': 0}

    def is_logged_in(self):
        return False


def _generate_media(ffmpeg_bin: str, media_root: Path, zone: str) -> list:
    """生成合成媒体（真实本地文件，同参数编码保证 -c copy 兼容）。

    单文件 40 秒：concat 一轮约 80 秒，跨过产品"快速失败/正常完成"
    的 30 秒判据——短媒体会被误判快速失败累积退避（测试场景缺陷，
    真实推流媒体远长于此）。
    """
    zone_dir = media_root / zone
    zone_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i in (1, 2):
        out = zone_dir / f"clip{i}.mp4"
        if not out.exists():
            subprocess.run(
                [ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
                 '-f', 'lavfi', '-i', f'testsrc=size=640x360:rate=25:duration=40',
                 '-f', 'lavfi', '-i', f'sine=frequency=1000:duration=40',
                 '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '1500k',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '64k',
                 '-shortest', str(out)],
                check=True, timeout=180)
        files.append(str(out))
    return files


def _trial_push(ffmpeg_bin: str, push_url: str, port: int) -> bool:
    """推流 URL 可用性自测（2 秒试推）——同时验证 sink 与 query 选项。"""
    stats = {"stop": False}
    sink_thread = threading.Thread(target=tcp_sink, args=(port, stats), daemon=True)
    sink_thread.start()
    proc = None
    try:
        time.sleep(0.5)
        before = stats.get("bytes", 0)
        proc = subprocess.Popen(
            [ffmpeg_bin, '-hide_banner', '-loglevel', 'error',
             '-re', '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=25',
             '-f', 'lavfi', '-i', 'sine=frequency=1000',
             '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '800k',
             '-c:a', 'aac', '-b:a', '64k', '-t', '2',
             '-f', 'flv', push_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        deadline = _monotonic() + 3
        while _monotonic() < deadline and stats.get("bytes", 0) <= before:
            time.sleep(0.1)
        return stats.get("bytes", 0) > before
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                try:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)
                except Exception:
                    pass
        stats["stop"] = True
        sink_thread.join(timeout=5)


def _gap_windows(samples: list, phase: str = None) -> list:
    """逐窗口缺口判定器（供注入停顿与正常耐久共用）。

    When ``phase`` is supplied, only complete windows whose two endpoints are
    in that phase are evaluated.  This prevents a recovery-to-steady boundary
    from hiding the first steady window or being counted as a steady failure.
    """
    gaps = []
    for i in range(1, len(samples)):
        previous, current = samples[i - 1], samples[i]
        if phase is not None and (previous.get("phase") != phase or current.get("phase") != phase):
            continue
        dt = current["t"] - previous["t"]
        growth = current["bytes"] - previous["bytes"]
        if dt >= WINDOW_SECONDS * 0.8 and growth < GAP_MIN_BYTES:
            gaps.append({
                "from": previous["phase"],
                "to": current["phase"],
                "window_s": round(dt, 1),
                "growth_bytes": growth,
            })
    return gaps


def _tail_min_growth(dt: float) -> float:
    """Scale only a short final interval; retain the complete-window gate."""
    if dt >= WINDOW_SECONDS * 0.8:
        return float(GAP_MIN_BYTES)
    return float(GAP_MIN_BYTES) * max(0.0, dt) / WINDOW_SECONDS


def _tail_fresh(samples: list) -> bool:
    """Check the final interval without discarding a short deadline window."""
    if len(samples) < 2:
        return False
    previous, current = samples[-2], samples[-1]
    dt = float(current.get("t", 0.0)) - float(previous.get("t", 0.0))
    growth = int(current.get("bytes", 0)) - int(previous.get("bytes", 0))
    if dt <= 0:
        return False
    # Preserve the original threshold for a nearly complete five-second
    # window.  A deadline can leave a shorter final interval; scale only that
    # interval's expected growth so a healthy stop is not a false negative.
    minimum_growth = _tail_min_growth(dt)
    return growth > minimum_growth


def _collect_phase_b(minutes: float, sample, sleep=time.sleep,
                     monotonic=_monotonic, window_seconds: float = WINDOW_SECONDS) -> list:
    """Collect Phase B with an immediate baseline before the first wait."""
    collected = []

    def record(phase):
        collected.append(sample(phase))

    # The baseline belongs to Phase B itself.  Sleeping before this point
    # silently discards an opening stall.
    record("steady")
    deadline = monotonic() + max(0.0, minutes) * 60.0
    while monotonic() < deadline:
        remaining = deadline - monotonic()
        sleep(min(window_seconds, max(0.0, remaining)))
        # Record the endpoint even when this wait lands exactly on the
        # deadline; otherwise the final steady window is silently unobserved.
        record("steady")
    return collected


def _assert_deterministic_window_coverage() -> dict:
    """Exercise opening, middle, tail, and recovery windows deterministically."""
    steady = [
        {"t": 0.0, "phase": "steady", "bytes": 0},
        {"t": 5.0, "phase": "steady", "bytes": 0},  # opening: injected stall
        {"t": 10.0, "phase": "steady", "bytes": GAP_MIN_BYTES * 2},  # opening recovery
        {"t": 15.0, "phase": "steady", "bytes": GAP_MIN_BYTES * 2},  # middle: stall
        {"t": 20.0, "phase": "steady", "bytes": GAP_MIN_BYTES * 4},  # middle recovery
        {"t": 25.0, "phase": "steady", "bytes": GAP_MIN_BYTES * 4},  # tail: stall
        {"t": 30.0, "phase": "steady", "bytes": GAP_MIN_BYTES * 6},  # tail recovery
    ]
    recovery = [
        {"t": 30.0, "phase": "recovery", "bytes": GAP_MIN_BYTES * 6},
        {"t": 35.0, "phase": "recovery", "bytes": GAP_MIN_BYTES * 8},
    ]
    steady_gaps = _gap_windows(steady, phase="steady")
    recovery_gaps = _gap_windows(recovery, phase="recovery")
    if len(steady_gaps) != 3 or [g["growth_bytes"] for g in steady_gaps] != [0, 0, 0]:
        raise AssertionError(f"deterministic steady coverage failed: {steady_gaps!r}")
    if recovery_gaps:
        raise AssertionError(f"deterministic recovery window falsely failed: {recovery_gaps!r}")
    short_tail_healthy = [
        {"t": 100.0, "phase": "steady", "bytes": 0},
        {"t": 100.75, "phase": "steady", "bytes": GAP_MIN_BYTES},
    ]
    short_tail_stall = [
        {"t": 100.0, "phase": "steady", "bytes": 0},
        {"t": 100.75, "phase": "steady", "bytes": 0},
    ]
    if not _tail_fresh(short_tail_healthy) or _tail_fresh(short_tail_stall):
        raise AssertionError("deterministic short-tail freshness failed")
    return {"steady_gap_count": len(steady_gaps),
            "steady_gaps": steady_gaps,
            "recovery_gaps": recovery_gaps,
            "short_tail_healthy": True,
            "short_tail_stall_rejected": True}


def _record_pid_residual(pids) -> tuple:
    """Return live and unknown PID states without treating unknown as gone."""
    residual = []
    unknown = []
    for pid in sorted(set(pids or [])):
        state = _pid_state(pid)
        if state["alive"] is True:
            residual.append(pid)
        elif state["alive"] is None:
            unknown.append(pid)
    return residual, unknown


def _product_pass_criteria(record: dict) -> bool:
    """Single acceptance gate; rescue cleanup cannot turn product failure green."""
    return bool(
        not record.get("error")
        and not record.get("sink_error")
        and record.get("stream_established") is True
        and record.get("injected_gap_detected") is True
        and record.get("product_self_recovered") is True
        and not record.get("steady_gaps")
        and record.get("tail_fresh") is True
        and record.get("suspend_ok") is True
        and record.get("resume_ok") is True
        and record.get("killed_ffmpeg") is True
        and record.get("old_owner_exited") is True
        and record.get("sample_count", 0) >= MIN_SAMPLE_COUNT
        and record.get("steady_sample_count", 0) >= MIN_STEADY_SAMPLE_COUNT
        and record.get("product_cleanup_ok") is True
        and not record.get("product_pusher_residual")
        and not record.get("product_residual_unknown")
        and record.get("fixture_rescue_needed") is False
        and record.get("fixture_rescue_ok") is True
        and record.get("cleanup_sink_joined") is True
        and not record.get("pusher_residual_after_cleanup")
        and not record.get("pusher_unknown_after_cleanup"))


def _cleanup_owned_worker(worker) -> tuple:
    """Reclaim only the Popen handle handed to us by the product controller."""
    if worker is None:
        return False, "missing_owned_worker"
    try:
        if worker.poll() is not None:
            return True, None
        worker.terminate()
        worker.wait(timeout=5)
    except Exception:
        try:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
        except Exception as exc:
            return False, repr(exc)
    return worker.poll() is not None, None


def _cleanup_product_resources(resources: dict) -> None:
    """Close product resources while preserving product-vs-fixture evidence."""
    controller = resources.get("controller")
    worker = resources.get("worker")
    if worker is None and controller is not None:
        worker = getattr(controller, "video_process", None)
    record = resources.get("record")

    # Product stop is an acceptance fact.  A later fixture rescue may reclaim
    # the Popen handle, but must never replace this result.
    if record is not None and "product_pusher_residual" not in record:
        if "pusher_residual" in record:
            record["product_pusher_residual"] = list(record["pusher_residual"] or [])
            record["product_residual_unknown"] = bool(record.get("pusher_unknown"))
        else:
            residual, unknown = _record_pid_residual(
                resources.get("ffmpeg_pids_seen", set()))
            record["product_pusher_residual"] = residual
            record["product_residual_unknown"] = unknown

    stop_attempted = bool(resources.get("controller_stop_attempted"))
    stop_ok = record.get("product_stop_ok") if record is not None else None
    if controller is not None and not stop_attempted:
        resources["controller_stop_attempted"] = True
        try:
            stop_ok = bool(controller.stop_streaming())
            resources["cleanup_stop_ok"] = stop_ok
        except Exception as exc:
            resources["cleanup_stop_error"] = repr(exc)
            stop_ok = False
        resources["controller_stopped"] = True
        if record is not None:
            record["product_stop_ok"] = bool(stop_ok)
    elif record is not None:
        stop_ok = bool(record.get("product_stop_ok"))

    if record is not None:
        record["product_cleanup_ok"] = bool(
            stop_ok is True
            and not record.get("product_pusher_residual")
            and not record.get("product_residual_unknown"))

    # A rescue is a separate fixture event and is itself an acceptance failure
    # whenever product cleanup was incomplete.
    fixture_needed = bool(
        record is None
        or not record.get("product_cleanup_ok")
        or record.get("product_pusher_residual")
        or record.get("product_residual_unknown"))
    if record is not None:
        record["fixture_rescue_needed"] = fixture_needed
    rescue_ok = True
    if fixture_needed:
        if record is not None:
            record["fixture_rescue_attempted"] = True
        rescue_ok, rescue_error = _cleanup_owned_worker(worker)
        if record is not None and rescue_error:
            record["fixture_rescue_error"] = rescue_error
    if record is not None:
        record["fixture_rescue_ok"] = bool(rescue_ok)

    stats = resources.get("stats")
    if stats is not None:
        stats["stop"] = True
    sink_thread = resources.get("sink_thread")
    if sink_thread is not None:
        sink_thread.join(timeout=5)

    if record is not None:
        record["cleanup_sink_stopped"] = bool(stats is None or stats.get("stop"))
        record["cleanup_sink_joined"] = bool(
            sink_thread is None or not sink_thread.is_alive())
        if stats is not None and stats.get("error"):
            record["sink_error"] = stats["error"]
        after, unknown_after = _record_pid_residual(
            resources.get("ffmpeg_pids_seen", set()))
        record["pusher_residual_after_cleanup"] = after
        record["pusher_unknown_after_cleanup"] = unknown_after
        # Keep the product snapshot frozen; this post-rescue observation is
        # explicitly separate evidence.
        record.setdefault("finished_at", datetime.now().isoformat())
        record["pass"] = _product_pass_criteria(record)


def run_product_endurance(minutes: float, sink_port: int, data_dir: Path,
                          ffmpeg_bin: str = 'ffmpeg') -> dict:
    """Run product endurance and always reclaim its local resources."""
    resources = {"record": {"mode": "product_controller", "minutes": minutes,
                             "started_at": datetime.now().isoformat(), "pass": False,
                             **_tool_provenance()}}
    try:
        return _run_product_endurance_impl(minutes, sink_port, data_dir, ffmpeg_bin, resources)
    except Exception as exc:
        resources["record"]["error"] = f"{type(exc).__name__}: {exc}"
        return resources["record"]
    finally:
        _cleanup_product_resources(resources)


def _run_product_endurance_impl(minutes: float, sink_port: int, data_dir: Path,
                                ffmpeg_bin: str, resources: dict) -> dict:
    """真实产品控制器耐久：
    Phase A1 注入停顿（判定器必须检出）→ A2 kill（产品须自行恢复）→
    Phase B 正常耐久（剩余分钟数）→ 真实产品 stop 收尾并核对资源。
    """
    os.environ.setdefault("BILIBILI_SKIP_MIGRATION", "1")
    sys.path.insert(0, str(BACKEND_DIR))
    from app.core import config
    from app.core import live_controller as lcmod

    data_dir.mkdir(parents=True, exist_ok=True)
    config.init_data_dir(str(data_dir))
    config.ffmpeg_log_path().parent.mkdir(parents=True, exist_ok=True)

    record = {"mode": "product_controller", "minutes": minutes,
              "started_at": datetime.now().isoformat(), **_tool_provenance()}
    resources["record"] = record
    record["pass"] = False
    zone = "耐久合成区"
    media_root = RESULTS_DIR / "_media"
    media_files = _generate_media(ffmpeg_bin, media_root, zone)
    record["media_files"] = [Path(f).name for f in media_files]

    # 分区表（合成区）+ 产品设置（真实 settings 流程：ffmpeg 推流）
    config.state_file_path().parent.mkdir(parents=True, exist_ok=True)
    config.area_file_path().write_text(json.dumps(
        [{"id": 1, "name": "合成大区", "parent_id": 0, "parent_name": "合成大区",
          "children": [{"id": 101, "name": zone, "parent_id": 1,
                        "parent_name": "合成大区", "children": []}]}],
        ensure_ascii=False), encoding='utf-8')
    config.settings_file_path().write_text(json.dumps({
        "stream_mode": "ffmpeg", "ffmpeg_path": ffmpeg_bin,
        "auto_open_video": False, "ffmpeg_reencode": False,
        "scan_interval_seconds": 30}, ensure_ascii=False), encoding='utf-8')

    # 真实控制器 + 替身平台
    controller = lcmod.LiveController(task_manager=None)
    resources["controller"] = controller
    controller.api = _StubPlatformApi(sink_port)
    controller.current_room_id = 123
    # 视频根目录指向合成媒体（D5：一路真实本地媒体）
    lcmod.VIDEO_BASE_PATH = media_root

    # 推流 URL 自测（sink + query 选项 + FLV over TCP 全链路）
    push_url = f"tcp://127.0.0.1:{sink_port}?timeout=10000000"
    record["trial_push_ok"] = _trial_push(ffmpeg_bin, push_url, sink_port)
    if not record["trial_push_ok"]:
        record["error"] = "trial push failed: URL/sink 链路不通"
        return record
    time.sleep(0.5)

    stats = {"stop": False, "bytes": 0}
    resources["stats"] = stats
    sink_thread = threading.Thread(target=tcp_sink, args=(sink_port, stats), daemon=True)
    resources["sink_thread"] = sink_thread
    sink_thread.start()

    from app.models.schemas import LiveInstruction
    # Current controller protocol accepts task mode with no TaskManager: the
    # synthetic instruction's zero duration is the explicit open-ended value.
    # Keep this fixture aligned with that protocol instead of adding a fake
    # scheduler solely for the endurance harness.
    instruction = LiveInstruction(zone_name=zone, duration_seconds=0)  # 0=不限时
    record["fixture_protocol"] = {
        "is_task_mode": True, "duration_seconds": 0,
        "task_manager": None,
    }
    accepted = controller.start_streaming(instruction, '', is_task_mode=True)
    record["start_accepted"] = bool(accepted)

    # 等待真实推流建立（产品 _ffmpeg_loop → concat → ffmpeg → sink）
    deadline = _monotonic() + 60
    while _monotonic() < deadline:
        if stats.get("bytes", 0) > 0 and controller.video_process is not None:
            break
        time.sleep(0.5)
    record["stream_established"] = stats.get("bytes", 0) > 0
    if not record["stream_established"]:
        record["error"] = "产品推流未建立（60s 内无字节）"
        stats["stop"] = True
        return record

    samples = []
    ffmpeg_pids_seen = set()
    resources["ffmpeg_pids_seen"] = ffmpeg_pids_seen

    def pusher_pid():
        proc = controller.video_process
        return proc.pid if proc is not None else None

    def sample(phase):
        t = _monotonic()
        m = proc_metrics(os.getpid())
        samples.append({"t": t, "wall_time": datetime.now().isoformat(),
                        "phase": phase, "bytes": stats.get("bytes", 0),
                        "rss_mb": m["rss_mb"], "handles": m["handles"],
                        "metrics_status": m.get("status"),
                        "ffmpeg_pid": pusher_pid()})
        pid = pusher_pid()
        if pid:
            ffmpeg_pids_seen.add(pid)

    def wait_bytes(min_growth: float, timeout: float) -> bool:
        base = stats.get("bytes", 0)
        deadline = _monotonic() + timeout
        while _monotonic() < deadline:
            if stats.get("bytes", 0) - base >= min_growth:
                return True
            time.sleep(0.2)
        return False

    # ---------- Phase A1：注入停顿（同一判定器必须检出） ----------
    sample("pre_stall")
    wait_bytes(GAP_MIN_BYTES * 3, 30)
    sample("pre_stall_settled")
    pid = pusher_pid()
    record["stall_pid"] = pid
    stall_result = _suspend_and_resume(pid, WINDOW_SECONDS * 1.6)
    record["suspend_ok"] = bool(stall_result.get("suspend_ok"))
    record["resume_ok"] = bool(stall_result.get("resume_ok"))
    record["stall_injected"] = bool(record["suspend_ok"])
    record["stall_resume_attempted"] = bool(stall_result.get("resume_attempted"))
    if stall_result.get("suspend_error"):
        record["stall_error"] = stall_result["suspend_error"]
    sample("post_stall")
    # 判定器面对注入停顿必须报缺口
    gaps = _gap_windows(samples)
    record["injected_gap_detected"] = any(
        g["from"] == "pre_stall_settled" and g["growth_bytes"] < GAP_MIN_BYTES
        for g in gaps)
    record["stall_gap"] = gaps[-1] if gaps else None
    # 恢复后字节继续增长（推流进程未死，产品无需干预即继续）
    record["recovered_after_stall"] = wait_bytes(GAP_MIN_BYTES, 30)
    sample("post_stall_recovered")

    # ---------- Phase A2：kill 推流进程 → 产品自行恢复 ----------
    owner_before_kill = controller.video_process
    pid = pusher_pid()
    record["killed_pid"] = pid
    if owner_before_kill is not None and getattr(owner_before_kill, "pid", None) == pid:
        kill_result = _kill_owned_process(owner_before_kill)
    else:
        kill_result = {"killed_ffmpeg": False, "old_owner_exited": False,
                       "taskkill_ok": False, "error": "owner_changed_before_kill"}
    record.update({"killed_ffmpeg": bool(kill_result.get("killed_ffmpeg")),
                   "old_owner_exited": bool(kill_result.get("old_owner_exited")),
                   "taskkill_ok": bool(kill_result.get("taskkill_ok"))})
    record["kill_evidence"] = kill_result
    if kill_result.get("error"):
        record["kill_error"] = kill_result["error"]
    # 产品 _ffmpeg_loop 检测退出 → 退避 → 自动重启 → 字节恢复
    recovered = False
    deadline = _monotonic() + 60
    base_bytes = stats.get("bytes", 0)
    new_pid_seen = False
    while _monotonic() < deadline:
        npid = pusher_pid()
        if npid and npid != pid:
            new_pid_seen = True
        if stats.get("bytes", 0) - base_bytes > GAP_MIN_BYTES and new_pid_seen:
            recovered = True
            break
        time.sleep(0.5)
    record["product_self_recovered"] = recovered
    record["recovery_source"] = "product _ffmpeg_loop auto-restart (new PID)" if recovered else "none"
    sample("post_kill_recovered")

    # ---------- Phase B：正常耐久（剩余分钟数，同一判定器） ----------
    _collect_phase_b(minutes, sample)
    gaps_all = _gap_windows(samples)
    # Phase B baseline is recorded before any wait, so its opening window is
    # included rather than clipped away by a post-sleep first sample.
    steady_indices = [i for i, s in enumerate(samples) if s["phase"] == "steady"]
    steady_idx = min(steady_indices) if steady_indices else len(samples)
    steady_samples = samples[steady_idx:] if steady_indices else []
    record["steady_start_index"] = steady_idx
    record["steady_sample_count"] = len(steady_samples)
    record["steady_gaps"] = _gap_windows(steady_samples, phase="steady")
    record["all_gaps"] = gaps_all
    record["samples"] = samples
    record["sample_count"] = len(samples)
    record["ffmpeg_pids_seen"] = sorted(ffmpeg_pids_seen)

    # 末尾新鲜度：最后窗口仍有字节增长
    if len(samples) >= 2:
        last = samples[-1]
        prev = samples[-2]
        record["tail_fresh"] = _tail_fresh(samples)
        record["tail_window_s"] = last["t"] - prev["t"]
        record["tail_min_growth"] = _tail_min_growth(record["tail_window_s"])
        record["tail_bytes"] = last["bytes"] - prev["bytes"]

    # ---------- 真实产品 stop 收尾 ----------
    resources["worker"] = controller.video_process
    resources["controller_stop_attempted"] = True
    try:
        stop_ok = controller.stop_streaming()
    except Exception as exc:
        stop_ok = False
        record["stop_error"] = repr(exc)
    resources["controller_stopped"] = True
    record["product_stop_ok"] = bool(stop_ok)
    time.sleep(1.5)
    record["total_bytes"] = stats.get("bytes", 0)
    record["sink_connections"] = stats.get("connections", 0)
    record["sink_disconnections"] = stats.get("disconnections", 0)
    # 收尾资源：后代（推流进程）不得残留
    residual, residual_unknown = _record_pid_residual(ffmpeg_pids_seen)
    record["product_pusher_residual"] = residual
    record["product_residual_unknown"] = residual_unknown
    record["pusher_residual"] = list(residual)
    record["pusher_unknown"] = list(residual_unknown)
    record["self_metrics_after_stop"] = proc_metrics(os.getpid())
    record["finished_at"] = datetime.now().isoformat()
    record["pass"] = _product_pass_criteria(record)
    return record


def main():
    global RESULTS_DIR
    _assert_deterministic_window_coverage()
    parser = argparse.ArgumentParser(description="桌面稳定性：生命周期 + 产品耐久")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--port", type=int, default=18060)
    parser.add_argument("--endurance-minutes", type=float, default=0,
                        help="产品耐久分钟数（Phase B；0=跳过耐久）")
    parser.add_argument("--endurance-quick", action="store_true",
                        help="短确定性场景（Phase B 仅 30 秒）")
    parser.add_argument("--backend-exe", type=str, default=None,
                        help="生命周期轮直接运行的打包 run.exe（默认 python run.py）")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                        help="隔离结果目录；默认 backend/tests/_endurance_results")
    parser.add_argument("--skip-cycles", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR = args.results_dir.resolve()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not args.skip_cycles:
        data_root = RESULTS_DIR / f"cycle-{stamp}"
        if args.backend_exe:
            cmd = [args.backend_exe]
        else:
            cmd = [PYTHON, str(BACKEND_DIR / "run.py")]
        cyc = run_cycles(args.cycles, args.port, cmd, data_root)
        (RESULTS_DIR / f"lifecycle-{stamp}.json").write_text(
            json.dumps(cyc, indent=2, ensure_ascii=False), encoding='utf-8')
        if cyc.get("cycles_ok") != args.cycles:
            print(
                f"cycles_failed={cyc.get('cycles_ok', 0)}/{args.cycles}; "
                "refusing a successful process exit",
                file=sys.stderr, flush=True)
            sys.exit(1)

    minutes = 0.5 if args.endurance_quick else args.endurance_minutes
    if minutes > 0:
        end = run_product_endurance(minutes, args.port + 100,
                                    RESULTS_DIR / f"endurance-data-{stamp}")
        (RESULTS_DIR / f"endurance-{stamp}.json").write_text(
            json.dumps(end, indent=2, ensure_ascii=False, default=str),
            encoding='utf-8')
        print(json.dumps({k: end.get(k) for k in (
            "stream_established", "injected_gap_detected", "product_self_recovered",
            "steady_gaps", "tail_fresh", "total_bytes", "pusher_residual",
            "product_pusher_residual", "pusher_residual_after_cleanup",
            "product_stop_ok", "cleanup_sink_joined", "sample_count",
            "suspend_ok", "resume_ok", "killed_ffmpeg", "old_owner_exited",
            "pass")}, ensure_ascii=False, indent=2), flush=True)
        if not end.get("pass"):
            sys.exit(1)


if __name__ == "__main__":
    main()
