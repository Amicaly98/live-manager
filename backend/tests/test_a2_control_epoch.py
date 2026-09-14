"""A2/A3：控制代际、票据签发与重放保护（桌面版核心安全语义）。"""

import threading
import time

import pytest

from app.core.live_controller import _OperationTokens


def test_stop_advances_epoch_and_invalidates_pending_start(controller, fake_api):
    """慢开播期间执行停止：开播线程在平台返回后的代际复核点作废，
    平台侧刚打开的房间被撤销，绝不变成新的直播。"""
    controller.current_room_id = 123
    gate = threading.Event()

    original_start = fake_api.start_live
    def slow_start_live(room_id, area_id, csrf):
        gate.wait(timeout=10)   # 模拟平台慢返回
        return original_start(room_id, area_id, csrf)
    fake_api.start_live = slow_start_live

    # 让 switch_partition 直接成功（避免依赖分区 API 细节）
    controller.switch_partition = lambda zone: True

    instruction = controller.current_instruction = _make_instruction("王者荣耀")
    ok = controller.start_streaming(instruction, "", is_task_mode=True)
    assert ok is True  # 已接受（后台执行中）
    deadline = time.time() + 5
    while time.time() < deadline and not controller._is_starting:
        time.sleep(0.05)
    assert controller._is_starting

    epoch_at_start = controller._control_epoch
    # 用户停止：推进代际 + 放行被卡住的开播线程
    stopped = controller.stop_streaming()
    gate.set()

    # 等待开播线程退出
    t = controller._start_thread
    if t:
        t.join(timeout=10)
    assert stopped is True
    assert controller._control_epoch > epoch_at_start, "停止应推进控制代际"
    assert controller.is_streaming is False
    # 平台侧撤销：stop_live 被调用（撤销刚打开的房间）
    assert len(fake_api.stop_calls) >= 1, "慢开播被停止后必须撤销平台侧房间"


def _make_instruction(zone):
    from app.models.schemas import LiveInstruction
    return LiveInstruction(zone_name=zone, duration_seconds=3600)


def test_issued_ticket_rejected_after_stop(controller):
    """停止后重放旧代际签发的开播票据 → begin_control_operation 返回 None。"""
    ticket = controller.issue_operation()
    epoch = controller.begin_control_operation(ticket)
    assert epoch == controller._control_epoch

    controller._advance_control_epoch()  # 模拟停止
    assert controller.begin_control_operation(ticket) is None


def test_old_stop_replay_confirms_without_executing(controller):
    """旧停止重放只确认既有结果（claim 返回 None），绝不停掉新直播。

    D1 夹具迁移：claim_stop_operation 由 bool 改为 Optional[int]
    （None=重放不执行；int=登记时刻的目标开播意图快照）。
    """
    stop_ticket = "uuid-stop-1"
    assert controller.claim_stop_operation(stop_ticket) is not None
    controller._advance_control_epoch()  # 停止执行 → 代际推进
    # 之后用户开启了新直播，旧停止的重放到达：
    assert controller.claim_stop_operation(stop_ticket) is None, \
        "旧代际停止重放必须只确认结果，不得再次执行下播"


def test_new_start_after_stop_with_fresh_ticket_succeeds(controller):
    """停止后用新票据发起的开播必须有效（不会被旧停止误杀）。"""
    old_stop = "uuid-stop-old"
    controller.claim_stop_operation(old_stop)
    controller._advance_control_epoch()

    new_ticket = controller.issue_operation()
    assert controller.begin_control_operation(new_ticket) is not None
    # 旧停止此刻重放：不得影响新代际
    assert controller.claim_stop_operation(old_stop) is None


def test_boot_change_invalidates_all_tickets():
    """后端重启（boot 变化）：旧进程的票据全部识别为外来票据而拒绝。

    boot_id 生产形态是 secrets.token_hex(4)（hex），签发票据形如
    `<hex>:<epoch>:<seq>`；非 hex 前缀的票据一律按自定义票据登记处理。
    """
    tokens = _OperationTokens(boot_id="aaaa1111")
    ok, reason = tokens.accept("aaaa1111:0:1", 0)
    assert ok and reason == "issued_ticket"
    ok2, reason2 = tokens.accept("bbbb2222:0:2", 0)   # 别的进程的票据
    assert not ok2 and reason2 == "foreign_boot_ticket"
    exec_ok, reason3 = tokens.register_stop("bbbb2222:0:2", 0)
    assert not exec_ok   # 外来停止也不执行


def test_custom_ticket_replay_after_stop_rejected():
    """自定义票据（旧客户端 UUID）：停止后重放 → 拒绝。"""
    tokens = _OperationTokens(boot_id="boot-a")
    ok, _ = tokens.accept("uuid-1", 0)
    assert ok
    tokens.register_stop("uuid-1", 0)          # 停止登记在当前代
    ok2, reason = tokens.accept("uuid-1", 1)   # 代际已推进
    assert not ok2


def test_stop_during_slow_start_via_executor(controller, fake_api):
    """停止通道（API 层独立执行器）在慢开播期间可用且有效。"""
    from app.api.live import _STOP_EXECUTOR, _stop_live_sync
    from app import dependencies
    dependencies.init_globals(None, controller, None)  # _stop_live_sync 从全局容器取控制器
    controller.current_room_id = 456
    controller.switch_partition = lambda zone: True
    gate = threading.Event()
    original = fake_api.start_live

    def slow_start_live(room_id, area_id, csrf):
        gate.wait(timeout=10)
        return original(room_id, area_id, csrf)
    fake_api.start_live = slow_start_live

    instruction = _make_instruction("英雄联盟")
    controller.start_streaming(instruction, "", is_task_mode=True)
    deadline = time.time() + 5
    while time.time() < deadline and not controller._is_starting:
        time.sleep(0.05)

    try:
        future = _STOP_EXECUTOR.submit(_stop_live_sync, None)
        gate.set()
        result = future.result(timeout=15)
        assert result["success"] is True, f"stop_live_sync 异常：{result.get('message')}"
        assert controller.is_streaming is False
    finally:
        dependencies.init_globals(None, None, None)


def test_duplicate_queued_stops_do_not_kill_new_intent(controller, monkeypatch):
    """D1：两个停止登记入队 → 期间新意图开播 → 队列中的停止执行时
    仍关联登记时刻的目标意图，不得停掉之后新接受的开播。"""
    from app.api.live import _stop_live_sync
    from app import dependencies
    dependencies.init_globals(None, controller, None)
    try:
        t1 = controller.claim_stop_operation('uuid-q1')
        t2 = controller.claim_stop_operation('uuid-q2')  # 同代际不同票据：均需执行
        assert t1 is not None and t2 is not None and t1 == t2
        # 期间新意图被接受（模拟新开播）
        controller._start_intent_id += 1
        controller.is_streaming = True
        stopped = []
        monkeypatch.setattr(controller, 'stop_streaming',
                            lambda: stopped.append(1) or True)
        r1 = _stop_live_sync(None, t1)
        r2 = _stop_live_sync(None, t2)
        assert stopped == [], "旧队列中的停止不得停掉新意图"
        assert r1["success"] and r2["success"]
        assert controller.is_streaming
    finally:
        dependencies.init_globals(None, None, None)
