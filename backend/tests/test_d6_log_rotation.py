"""D6：FFmpeg 日志存活期容量轮转——Windows 真实写句柄实测。

用真实子进程（继承句柄、持续输出）代替 FFmpeg 验证：
1. 输出跨过阈值后原地截断，子进程继续输出（写句柄不失效）；
2. 截断后文件从头部继续增长、总量受控（跨过阈值可再次截断）；
3. 整个过程日志写失败不影响"推流"（子进程存活）。

关键机制：子进程继承的 stdout 句柄与父进程 log_fp 共享同一内核
文件指针——seek(0) 同时复位两者的写位置。
"""
import os
import subprocess
import sys
import time

from app.core.live_controller import _truncate_ffmpeg_log_inplace

# 测试用小阈值（64KB），产品默认值不改变
TEST_MAX_BYTES = 64 * 1024


def test_log_rotation_while_process_alive_windows(tmp_path):
    log = tmp_path / 'ffmpeg.log'
    child_code = (
        "import sys, time\n"
        "i = 0\n"
        "while True:\n"
        "    sys.stdout.write('line %d ' % i + 'x' * 200 + chr(10))\n"
        "    sys.stdout.flush()\n"
        "    i += 1\n"
        "    time.sleep(0.02)\n"
    )
    fp = open(str(log), 'a', encoding='utf-8', errors='replace')
    proc = subprocess.Popen(
        [sys.executable, '-u', '-c', child_code],
        stdout=fp, stderr=subprocess.STDOUT)
    try:
        # 前置：真实子进程输出跨过阈值
        deadline = time.time() + 30
        while time.time() < deadline:
            if log.exists() and log.stat().st_size >= TEST_MAX_BYTES:
                break
            assert proc.poll() is None, '子进程不应提前退出'
            time.sleep(0.1)
        assert log.exists() and log.stat().st_size >= TEST_MAX_BYTES, \
            '前置失败：日志未跨过阈值'

        # 与产品 watchdog 相同逻辑：原地截断（不关闭句柄、不中断子进程）
        assert _truncate_ffmpeg_log_inplace(fp, TEST_MAX_BYTES) is True
        time.sleep(1.0)
        size_after = log.stat().st_size
        assert size_after < TEST_MAX_BYTES, \
            f'截断后应从头部继续写，总量受控（实际 {size_after} 字节）'
        assert proc.poll() is None, '截断后子进程必须仍存活（输出不中断）'

        # 持续写入再次跨过阈值 → 再次截断（长会话受控）
        truncated_again = False
        t_end = time.time() + 15
        while time.time() < t_end:
            assert proc.poll() is None, '持续阶段子进程必须存活'
            if log.exists() and log.stat().st_size > TEST_MAX_BYTES:
                assert _truncate_ffmpeg_log_inplace(fp, TEST_MAX_BYTES)
                truncated_again = True
                break
            time.sleep(0.1)
        assert truncated_again, '持续输出应再次跨过阈值并支持再次截断'
        time.sleep(0.5)
        assert proc.poll() is None, '二次截断后输出仍持续'
        assert log.stat().st_size < TEST_MAX_BYTES * 2, '文件总量始终受控'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        fp.close()


def test_log_rotation_failure_does_not_kill_writer(tmp_path):
    """日志截断失败（句柄异常）绝不影响推流进程。"""
    child_code = (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('x' * 100 + chr(10))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n"
    )
    fp = open(str(tmp_path / 'a.log'), 'a', encoding='utf-8', errors='replace')
    proc = subprocess.Popen(
        [sys.executable, '-u', '-c', child_code],
        stdout=fp, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        # 制造截断失败：close 底层句柄后再调用截断
        fp.close()
        assert _truncate_ffmpeg_log_inplace(fp, 1024) is False, '截断失败应返回 False 而非抛出'
        time.sleep(0.5)
        assert proc.poll() is None, '日志失败绝不能停掉推流进程'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
