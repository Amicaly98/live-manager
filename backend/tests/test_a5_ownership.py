"""A5：进程所有权——精确回收自建进程树，外部进程绝不受伤。"""

import subprocess
import sys
import time
import threading

import pytest


def _spawn_sleeper(seconds=60):
    """真实子进程（python -c sleep），Windows 下 taskkill /T 可实测进程树。"""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def test_external_ffmpeg_not_killed(controller, fake_api):
    """全局 taskkill /IM ffmpeg.exe 已删除；回收只针对自建进程对象。

    用两个真实 python 子进程模拟"自建推流进程"与"外部 FFmpeg"：
    回收自建后，外部进程必须仍然存活。
    """
    owned = _spawn_sleeper()
    external = _spawn_sleeper()
    try:
        gen = controller._new_pusher_generation()
        controller._claim_pusher(gen, owned)
        assert controller.video_process is owned

        controller._kill_ffmpeg(timeout=8)

        deadline = time.time() + 8
        while time.time() < deadline and owned.poll() is None:
            time.sleep(0.1)
        assert owned.poll() is not None, "自建进程应被回收"
        assert external.poll() is None, "外部进程绝不能被误杀（A5 红线）"
        assert controller.video_process is None, "确认退出后引用清除"
        assert controller._ffmpeg_unrecycled is False
    finally:
        for p in (owned, external):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_unreclaimed_process_blocks_new_start(controller):
    """杀不掉的旧进程：保留引用并标记 _ffmpeg_unrecycled，禁止重复创建。

    用一个"杀不掉"的替身进程对象验证所有权状态机（不依赖平台行为）。
    """
    class UnkillableProcess:
        def __init__(self):
            self.pid = 999999
            self.returncode = None

        def poll(self):
            return None  # 永远"活着"

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    bad = UnkillableProcess()
    gen = controller._new_pusher_generation()
    controller._claim_pusher(gen, bad)
    controller._kill_ffmpeg(timeout=1)

    assert controller.video_process is bad, "未确认回收必须保留引用"
    assert controller._ffmpeg_unrecycled is True
    # _start_ffmpeg_stream 必须拒绝（不能另起一路同房间推流）
    controller.current_room_id = 1
    controller._get_cached_push_url = lambda: "rtmp://fake"
    ok = controller._start_ffmpeg_stream()
    assert ok is False, "未回收时禁止重复创建推流"


def test_release_pusher_keeps_reference_while_alive(controller):
    """_release_pusher：进程仍活着 → 保留引用 + 标记；确认退出 → 清引用。"""
    proc = _spawn_sleeper()
    try:
        gen = controller._new_pusher_generation()
        controller._claim_pusher(gen, proc)
        controller._release_pusher(gen)
        assert controller.video_process is proc, "存活时不得清引用"
        assert controller._ffmpeg_unrecycled is True

        proc.kill()
        proc.wait(timeout=5)
        controller._release_pusher(gen)
        assert controller.video_process is None
        assert controller._ffmpeg_unrecycled is False
    finally:
        if proc.poll() is None:
            proc.kill()


def test_old_generation_cleanup_does_not_clear_new_reference(controller):
    """旧代循环的 finally/_release 不得清掉新代登记的进程引用（A5）。"""
    old_proc = _spawn_sleeper()
    new_proc = _spawn_sleeper()
    try:
        gen1 = controller._new_pusher_generation()
        controller._claim_pusher(gen1, old_proc)
        gen2 = controller._new_pusher_generation()
        controller._claim_pusher(gen2, new_proc)

        # 旧代收尾（进程已退出也不行——代际已推进）
        old_proc.kill()
        old_proc.wait(timeout=5)
        controller._release_pusher(gen1)
        assert controller.video_process is new_proc, "旧代 finally 不得清新代引用"
        assert controller._pusher_owner_generation() == gen2
    finally:
        for p in (old_proc, new_proc):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_real_process_tree_reclaim_windows(controller):
    """Windows 真实子进程树回收：taskkill /F /T 能带走孙进程。

    父进程派生一个孙进程并打印其 PID 后自行保持存活；
    用 taskkill /F /T /PID 父PID 回收整棵树，孙进程必须一并退出。
    """
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
         "print(child.pid, flush=True);"
         "time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace',
    )
    try:
        child_pid = None
        deadline = time.time() + 10
        while time.time() < deadline and child_pid is None:
            line = parent.stdout.readline() if parent.stdout else ""
            line = line.strip()
            if line.isdigit():
                child_pid = int(line)
                break
        assert child_pid, "未取得孙进程 PID"
        # 回收父进程树
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(parent.pid)],
                       capture_output=True, timeout=10)
        deadline = time.time() + 8
        while time.time() < deadline and parent.poll() is None:
            time.sleep(0.1)
        assert parent.poll() is not None, "父进程应被回收"
        # 孙进程不在我们的树里被直接持有：按 PID 验证已随树退出
        # （中文 Windows 的 tasklist 输出为 GBK，显式指定编码避免解码崩溃）
        rc = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}"],
                            capture_output=True, timeout=10)
        tasklist_out = rc.stdout.decode('gbk', errors='replace')
        assert str(child_pid) not in tasklist_out, "孙进程应随进程树一起回收"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
