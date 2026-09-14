"""连续停止的受控时序判定（CTRL-02c/02d）。

桌面 stop_streaming 是"受理 + 清理"一次完成的同步入口（见 contracts.json 的
DIFF-STOP-ENTRY），因此这里把两个预期写成明确断言：

1. 清理已完成、且此后没有新的在播会话 → 重复停止只确认既有结果，不再多打一次
   平台下播（否则重复点击会在新会话刚建立时把它误清理）；
2. 新直播之后的新停止仍然有效（幂等判据不得把新会话的停止吃掉）。

"清理尚未完成时的并发重复"属于服务器"异步受理 + 后台清理"形态才有的时序，
由服务器仓 ``tests/test_control_safety.py`` 的 Event 受控用例覆盖；两侧靠
共同契约 CTRL-02c/02d 保持同一断言语义。
"""

import time

from app.models.schemas import LiveInstruction


def _start(controller):
    """走真实开播入口并等待后台开播线程结束（OBS 模式：不创建本地推流）。"""
    controller.switch_partition = lambda zone: True
    accepted = controller.start_streaming(
        LiveInstruction(zone_name="王者荣耀", duration_seconds=0), "",
        is_task_mode=False)
    assert accepted is True, "开播请求必须被受理"
    thread = getattr(controller, "_start_thread", None)
    if thread is not None:
        thread.join(timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline and not controller.is_streaming:
        time.sleep(0.05)
    assert controller.is_streaming is True, "开播必须生效"


def _stop(controller):
    assert controller.stop_streaming() is True, "停止必须成功返回"
    thread = getattr(controller, "_start_thread", None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


def test_repeated_stop_after_completion_only_confirms(controller, fake_api):
    """清理已完成且无新会话：重复停止只确认，不再重复下播。"""
    controller.current_room_id = 42
    controller._get_stream_settings = lambda: ("manual", False)
    fake_api.live_status_result = (True, {"code": 0, "data": {"live_status": 0}})

    _start(controller)
    _stop(controller)
    assert len(fake_api.stop_calls) == 1, "第一次停止必须真正执行一次平台下播"

    _stop(controller)
    assert len(fake_api.stop_calls) == 1, (
        "清理已完成且无新意图：重复停止不得再提交一次平台下播")


def test_stop_after_new_session_still_stops(controller, fake_api):
    """新直播之后的新停止仍然有效。"""
    controller.current_room_id = 42
    controller._get_stream_settings = lambda: ("manual", False)
    fake_api.live_status_result = (True, {"code": 0, "data": {"live_status": 0}})

    _start(controller)
    _stop(controller)
    assert len(fake_api.stop_calls) == 1

    _stop(controller)  # 重复停止：只确认
    assert len(fake_api.stop_calls) == 1

    _start(controller)  # 新意图接管
    assert controller.is_streaming is True

    _stop(controller)
    assert controller.is_streaming is False, "新会话的停止必须生效"
    assert len(fake_api.stop_calls) == 2, (
        "新会话的停止必须真正执行一次平台下播（不得被幂等判据吃掉）")
