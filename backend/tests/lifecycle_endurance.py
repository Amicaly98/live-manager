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
    if not out or out == 'gone':
        return {"rss_mb": None, "handles": None, "alive": False}
    try:
        rss, handles = out.splitlines()[-1].split(',')
        return {"rss_mb": round(int(rss) / (1024 * 1024), 1),
                "handles": int(handles), "alive": True}
    except Exception:
        return {"rss_mb": None, "handles": None, "alive": True}


def list_descendants(pid: int) -> list:
    """枚举直接子进程 PID（收尾后残留检查用）。"""
    out = _ps_output(
        f"(Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\")"
        f"| ForEach-Object {{ Write-Output $_.ProcessId }}")
    try:
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _pid_alive(pid: int) -> bool:
    return proc_metrics(pid)["alive"]


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
    deadline = time.time() + timeout
    while time.time() < deadline:
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
    record = {"cycle": cycle, "started_at": datetime.now().isoformat()}
    env = {**os.environ, "BILIBILI_SKIP_MIGRATION": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    data_dir.mkdir(parents=True, exist_ok=True)
    out_log = open(data_dir / "backend_stdout.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        backend_cmd, cwd=str(data_dir), env=env,
        stdout=out_log, stderr=subprocess.STDOUT)
    record["pid"] = proc.pid
    record["command"] = ' '.join(str(c) for c in backend_cmd)
    ready = wait_health(port)
    record["health_ok"] = ready
    if ready:
        time.sleep(0.5)
        record["metrics_ready"] = proc_metrics(proc.pid)
    # 优雅关闭（E2：uvicorn should_exit）
    record["shutdown_http"] = http_post(f"http://127.0.0.1:{port}/api/shutdown?stop_live=true")
    try:
        proc.wait(timeout=20)
        record["exit_code"] = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        record["exit_code"] = "killed_after_timeout"
    out_log.close()
    time.sleep(0.5)
    # 收尾资源：根进程 + 后代残留
    record["residual_alive"] = _pid_alive(proc.pid)
    record["descendants_after"] = [p for p in list_descendants(proc.pid) if _pid_alive(p)]
    record["finished_at"] = datetime.now().isoformat()
    return record


def run_cycles(cycles: int, base_port: int, backend_cmd: list) -> dict:
    results = {"backend_cmd": ' '.join(str(c) for c in backend_cmd),
               "cycles": [], "started_at": datetime.now().isoformat()}
    for i in range(cycles):
        port = base_port + i
        data_dir = RESULTS_DIR / f"cycle-{results['started_at'].replace(':', '')[:17]}" / f"data-{i + 1}"
        rec = run_one_cycle(i + 1, port, data_dir, backend_cmd)
        results["cycles"].append(rec)
        print(f"cycle {rec['cycle']}: health={rec['health_ok']} "
              f"shutdown={rec.get('shutdown_http')} exit={rec.get('exit_code')} "
              f"residual={rec['residual_alive']} "
              f"rss={rec.get('metrics_ready', {}).get('rss_mb')}MB "
              f"handles={rec.get('metrics_ready', {}).get('handles')}", flush=True)
    ok = sum(1 for c in results["cycles"]
             if c["health_ok"] and c.get("shutdown_http")
             and c.get("exit_code") == 0 and not c["residual_alive"]
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
                        stats["last_byte_at"] = time.time()
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
        ntdll = ctypes.WinDLL('ntdll')
        kernel32 = ctypes.WinDLL('kernel32')
        h = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not h:
            return False
        try:
            (ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess)(h)
            return True
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


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
    deadline = time.time() + 3
    while time.time() < deadline and stats.get("bytes", 0) <= before:
        time.sleep(0.1)
    stats["stop"] = True
    return stats.get("bytes", 0) > before


def _gap_windows(samples: list) -> list:
    """逐窗口缺口判定器（供注入停顿与正常耐久共用）。"""
    gaps = []
    for i in range(1, len(samples)):
        dt = samples[i]["t"] - samples[i - 1]["t"]
        growth = samples[i]["bytes"] - samples[i - 1]["bytes"]
        if dt >= WINDOW_SECONDS * 0.8 and growth < GAP_MIN_BYTES:
            gaps.append({
                "from": samples[i - 1]["phase"],
                "to": samples[i]["phase"],
                "window_s": round(dt, 1),
                "growth_bytes": growth,
            })
    return gaps


def run_product_endurance(minutes: float, sink_port: int, data_dir: Path,
                          ffmpeg_bin: str = 'ffmpeg') -> dict:
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
              "started_at": datetime.now().isoformat()}
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
    sink_thread = threading.Thread(target=tcp_sink, args=(sink_port, stats), daemon=True)
    sink_thread.start()

    from app.models.schemas import LiveInstruction
    instruction = LiveInstruction(zone_name=zone, duration_seconds=0)  # 0=不限时
    accepted = controller.start_streaming(instruction, '', is_task_mode=True)
    record["start_accepted"] = bool(accepted)

    # 等待真实推流建立（产品 _ffmpeg_loop → concat → ffmpeg → sink）
    deadline = time.time() + 60
    while time.time() < deadline:
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

    def pusher_pid():
        proc = controller.video_process
        return proc.pid if proc is not None else None

    def sample(phase):
        t = time.time()
        m = proc_metrics(os.getpid())
        samples.append({"t": t, "phase": phase, "bytes": stats.get("bytes", 0),
                        "rss_mb": m["rss_mb"], "handles": m["handles"],
                        "ffmpeg_pid": pusher_pid()})
        pid = pusher_pid()
        if pid:
            ffmpeg_pids_seen.add(pid)

    def wait_bytes(min_growth: float, timeout: float) -> bool:
        base = stats.get("bytes", 0)
        deadline = time.time() + timeout
        while time.time() < deadline:
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
    stall_ok = _suspend_windows(pid, suspend=True)
    record["stall_injected"] = bool(stall_ok)
    time.sleep(WINDOW_SECONDS * 1.6)  # 制造一个完整缺口窗口
    _suspend_windows(pid, suspend=False)
    stall_end = time.time()
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
    pid = pusher_pid()
    record["killed_pid"] = pid
    if pid:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    record["killed_ffmpeg"] = True
    # 产品 _ffmpeg_loop 检测退出 → 退避 → 自动重启 → 字节恢复
    recovered = False
    deadline = time.time() + 60
    base_bytes = stats.get("bytes", 0)
    new_pid_seen = False
    while time.time() < deadline:
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
    phase_b_deadline = time.time() + minutes * 60
    while time.time() < phase_b_deadline:
        time.sleep(WINDOW_SECONDS)
        sample("steady")
    gaps_all = _gap_windows(samples)
    # 正常耐久段（steady 之后）不得出现缺口
    steady_idx = max(i for i, s in enumerate(samples) if s["phase"] == "steady") if any(
        s["phase"] == "steady" for s in samples) else len(samples)
    steady_samples = samples[steady_idx - 1:] if steady_idx > 0 else samples
    record["steady_gaps"] = _gap_windows(steady_samples)
    record["all_gaps"] = gaps_all
    record["samples"] = samples
    record["ffmpeg_pids_seen"] = sorted(ffmpeg_pids_seen)

    # 末尾新鲜度：最后窗口仍有字节增长
    if len(samples) >= 2:
        last = samples[-1]
        prev = samples[-2]
        record["tail_fresh"] = (last["bytes"] - prev["bytes"]) > GAP_MIN_BYTES
        record["tail_bytes"] = last["bytes"] - prev["bytes"]

    # ---------- 真实产品 stop 收尾 ----------
    stop_ok = controller.stop_streaming()
    record["product_stop_ok"] = bool(stop_ok)
    time.sleep(1.5)
    stats["stop"] = True
    record["total_bytes"] = stats.get("bytes", 0)
    record["sink_connections"] = stats.get("connections", 0)
    record["sink_disconnections"] = stats.get("disconnections", 0)
    # 收尾资源：后代（推流进程）不得残留
    residual = [p for p in ffmpeg_pids_seen if _pid_alive(p)]
    record["pusher_residual"] = residual
    record["self_metrics_after_stop"] = proc_metrics(os.getpid())
    record["finished_at"] = datetime.now().isoformat()
    record["pass"] = bool(
        record["stream_established"] and record["injected_gap_detected"]
        and record["product_self_recovered"] and not record["steady_gaps"]
        and record.get("tail_fresh") and record["product_stop_ok"]
        and not residual)
    return record


def main():
    parser = argparse.ArgumentParser(description="桌面稳定性：生命周期 + 产品耐久")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--port", type=int, default=18060)
    parser.add_argument("--endurance-minutes", type=float, default=0,
                        help="产品耐久分钟数（Phase B；0=跳过耐久）")
    parser.add_argument("--endurance-quick", action="store_true",
                        help="短确定性场景（Phase B 仅 30 秒）")
    parser.add_argument("--backend-exe", type=str, default=None,
                        help="生命周期轮直接运行的打包 run.exe（默认 python run.py）")
    parser.add_argument("--skip-cycles", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not args.skip_cycles:
        if args.backend_exe:
            cmd = [args.backend_exe, "--port", str(args.port)]
        else:
            cmd = [PYTHON, str(BACKEND_DIR / "run.py"), "--port", str(args.port)]
        cyc = run_cycles(args.cycles, args.port, cmd)
        (RESULTS_DIR / f"lifecycle-{stamp}.json").write_text(
            json.dumps(cyc, indent=2, ensure_ascii=False), encoding='utf-8')

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
            "product_stop_ok", "pass")}, ensure_ascii=False, indent=2), flush=True)
        if not end.get("pass"):
            sys.exit(1)


if __name__ == "__main__":
    main()
