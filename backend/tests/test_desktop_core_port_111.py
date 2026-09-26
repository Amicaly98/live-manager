"""Desktop backend regression coverage for the 1.1.1 core port."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from types import SimpleNamespace


def test_shutdown_policy_does_not_escalate_on_lifespan_repeat(
        controller, fake_api, tmp_path):
    """The lifespan's second no-argument shutdown must preserve stop=false."""
    from app.core.live_controller import LiveInstruction, LiveState

    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "合成区", 123, 3600, duration_known=True,
        source="new", initial_elapsed=75,
    )
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="合成区", duration_seconds=3600, task_id=7,
        run_id="run-no-stop", execution_date=date.today().isoformat(),
    )
    controller._current_run_id = "run-no-stop"
    controller._pending_run_id = "run-no-stop"
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()
    controller._is_starting = True
    controller._kill_ffmpeg = lambda *args, **kwargs: None

    controller.shutdown(stop_platform=False)
    controller.shutdown()

    assert fake_api.stop_calls == []
    assert controller.state.current_zone == "合成区"
    assert controller.state.duration_seconds == 3600
    assert controller.state.elapsed_seconds >= 75


def test_shutdown_retries_failed_local_recycle_without_platform_stop(
        controller, fake_api):
    controller.is_streaming = True
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()
    attempts = []

    def fail_then_recycle():
        attempts.append(len(attempts) + 1)
        controller._ffmpeg_unrecycled = len(attempts) < 3

    controller._kill_ffmpeg = fail_then_recycle

    controller.shutdown(stop_platform=False)
    assert controller._shutdown_complete is False
    controller.shutdown()

    assert controller._shutdown_stop_platform is False
    assert controller._shutdown_complete is True
    assert len(attempts) == 3
    assert fake_api.stop_calls == []


def test_shutdown_true_is_not_repeated_by_lifespan(
        controller, fake_api, monkeypatch, tmp_path):
    """显式"停止并退出"作为一次用户停止执行；lifespan 二次调用不重复停播。

    语义更新（1.1.1）：单次关闭事务改为"显式用户停止"——退出对话框里
    选择"停止并退出"是用户的主动决定，控制器以 user_initiated=True 执行
    平台下播并把停止意图落盘（重启后仅提示继续，不自动开播）。旧断言
    ``[False]`` 来自"关闭路径的系统停止不算用户停止"的旧语义，已被取代：
    这里用真实停止路径（fake_api + LiveState）取证后再断言新语义。
    """
    import json
    from app.core.live_controller import LiveInstruction, LiveState

    intent_file = tmp_path / "stop_intent.json"
    monkeypatch.setattr(
        "app.core.live_controller.stop_intent_file_path",
        lambda: intent_file)

    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "合成区", 123, 3600, duration_known=True,
        source="new", initial_elapsed=75,
    )
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="合成区", duration_seconds=3600, task_id=7,
        run_id="run-stop-exit", execution_date=date.today().isoformat(),
    )
    controller.is_streaming = True
    controller._current_run_id = "run-stop-exit"
    controller._pending_run_id = "run-stop-exit"
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()

    controller.shutdown(stop_platform=True)  # 显式"停止并退出"
    controller.shutdown()  # lifespan 二次调用

    # 平台下播恰好一次（重复 lifespan 不再次停播）
    assert fake_api.stop_calls == [(123, "fake-csrf")]
    # 显式用户停止的证据：user stop 代际标记 + 停止意图落盘
    # （_last_user_stop_source_epoch 记录停止发出时的源代际；
    #   意图文件写的是停止推进后的当前代际，重启后用于识别"此代已被停止"）
    assert controller._last_user_stop_source_epoch is not None
    assert intent_file.exists()
    intent = json.loads(intent_file.read_text(encoding="utf-8"))
    assert intent["zone"] == "合成区"
    assert intent["epoch"] == controller._control_epoch
    # 任务进度保留（恢复语义），没有因为退出被清空或重置
    assert controller.state.current_zone == "合成区"
    assert controller.state.duration_seconds == 3600
    assert controller.state.elapsed_seconds >= 75
    assert controller._shutdown_complete is True


def test_stop_and_quit_during_blocked_manual_stop_fails_then_recovers(
        controller, fake_api, monkeypatch, tmp_path):
    """手动停止阻塞超过响应窗口时，"停止并退出"必须如实报败并可恢复。

    Event 精确交错：面板手动 stop 的清理线程被闸门阻塞（超过旧 5 秒响应
    边界），此时发送"停止并退出"。要求：关闭请求不得返回"已安全关闭"、
    不得强杀在途清理、不得丢停止意图；放行原 stop 后重试必须正常完成，
    平台只下播一次、无进程残留、最后进度保留。
    """
    import json
    from app.core.live_controller import LiveInstruction, LiveState

    intent_file = tmp_path / "stop_intent.json"
    monkeypatch.setattr(
        "app.core.live_controller.stop_intent_file_path",
        lambda: intent_file)

    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "合成区", 123, 3600, duration_known=True,
        source="new", initial_elapsed=75,
    )
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="合成区", duration_seconds=3600, task_id=7,
        run_id="run-blocked", execution_date=date.today().isoformat(),
    )
    controller.is_streaming = True
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()
    controller._kill_ffmpeg = lambda *args, **kwargs: None

    gate = threading.Event()
    entered = threading.Event()
    original_stop_live_process = controller._stop_live_process
    kill_calls = []

    def killing_ffmpeg_counter(*args, **kwargs):
        kill_calls.append(1)

    def gated_stop_live_process(*args, **kwargs):
        entered.set()
        gate.wait(10.0)
        return original_stop_live_process(*args, **kwargs)

    controller._stop_live_process = gated_stop_live_process
    controller._kill_ffmpeg = killing_ffmpeg_counter

    stop_thread = threading.Thread(
        target=controller.stop_streaming, name="BlockedManualStop",
        daemon=True)
    stop_thread.start()
    assert entered.wait(5.0)
    assert controller._stop_in_progress is True

    controller._shutdown_wait_timeout_seconds = 0.3
    ok = controller.shutdown(stop_platform=True)

    # 失败路径：不得谎报"已安全关闭"，不得强杀在途清理
    assert ok is False
    assert controller._shutdown_complete is False
    assert kill_calls == []
    assert controller._stop_in_progress is True
    assert fake_api.stop_calls == []  # 原停止仍持有清理权，未被绕过
    # 停止意图与进度未被丢弃（意图在清理阻塞前已落盘）
    assert intent_file.exists()
    intent = json.loads(intent_file.read_text(encoding="utf-8"))
    assert intent["zone"] == "合成区"
    assert controller.state.current_zone == "合成区"

    gate.set()
    stop_thread.join(timeout=5.0)
    assert not stop_thread.is_alive()

    ok_retry = controller.shutdown()
    assert ok_retry is True
    assert controller._shutdown_complete is True
    # 平台只下播一次：由放行后的原停止完成，重试不得再次停播
    assert fake_api.stop_calls == [(123, "fake-csrf")]
    # 无进程残留
    assert controller._process_alive(controller.video_process) is False
    assert controller._ffmpeg_unrecycled is False
    assert controller._stop_in_progress is False
    # 最后进度与恢复状态保留，停止意图未清除
    assert controller.state.current_zone == "合成区"
    assert controller.state.elapsed_seconds >= 75
    assert intent_file.exists()


def test_quit_without_stop_never_calls_platform_stop_and_keeps_resume_state(
        controller, fake_api, monkeypatch, tmp_path):
    """退出对话框选择"不停止并退出"：绝不调用平台下播，进度保留可恢复。"""
    from app.core.live_controller import LiveInstruction, LiveState

    intent_file = tmp_path / "stop_intent.json"
    monkeypatch.setattr(
        "app.core.live_controller.stop_intent_file_path",
        lambda: intent_file)

    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "合成区", 123, 3600, duration_known=True,
        source="new", initial_elapsed=120,
    )
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="合成区", duration_seconds=3600, task_id=7,
        run_id="run-quit-keep", execution_date=date.today().isoformat(),
    )
    controller.is_streaming = True
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()

    assert controller.shutdown(stop_platform=False) is True

    assert fake_api.stop_calls == []  # 绝不下播，平台侧房间保持原状
    assert intent_file.exists() is False  # 没有停止意图（用户选择继续播）
    assert controller.state.current_zone == "合成区"
    assert controller.state.duration_seconds == 3600
    assert controller.state.elapsed_seconds >= 120
    assert controller.state.is_streaming is True  # 重开后提示继续
    assert controller._shutdown_complete is True


def test_shutdown_true_with_failed_cleanup_fails_then_retry_reclaims(
        controller, fake_api, monkeypatch, tmp_path):
    """shutdown 自身的平台停止已受理但本地回收失败 → 关闭必须报败，
    保留进程供重试；回收恢复后重试成功，且不重复平台下播。"""
    from app.core.live_controller import LiveInstruction, LiveState

    intent_file = tmp_path / "stop_intent.json"
    monkeypatch.setattr(
        "app.core.live_controller.stop_intent_file_path",
        lambda: intent_file)
    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "合成区", 123, 3600, duration_known=True,
        source="new", initial_elapsed=60,
    )
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="合成区", duration_seconds=3600, task_id=7,
        run_id="run-failed-cleanup", execution_date=date.today().isoformat(),
    )
    controller.is_streaming = True
    controller._stream_mode = "task"
    controller.stream_start_time = __import__("datetime").datetime.now()
    attempts = []

    def fail_then_recycle():
        # 第一次 shutdown 会尝试两次回收（停止清理 + 关闭收尾），
        # 两次都失败；之后的重试才允许回收成功。
        attempts.append(len(attempts) + 1)
        controller._ffmpeg_unrecycled = len(attempts) < 3

    controller._kill_ffmpeg = fail_then_recycle

    assert controller.shutdown(stop_platform=True) is False
    assert controller._shutdown_complete is False
    assert fake_api.stop_calls == [(123, "fake-csrf")]  # 平台已下播一次
    assert controller._ffmpeg_unrecycled is True  # 保留 owner 待重试
    attempts_after_first = len(attempts)
    assert attempts_after_first >= 1  # 首次关闭真实尝试过回收

    assert controller.shutdown() is True
    assert len(attempts) > attempts_after_first  # 重试真的再次回收
    assert fake_api.stop_calls == [(123, "fake-csrf")]  # 不重复平台下播
    assert controller._ffmpeg_unrecycled is False
    assert controller._shutdown_complete is True
    assert controller.state.current_zone == "合成区"
    assert controller.state.elapsed_seconds >= 60


def test_daily_summary_snapshot_delivers_ten_priority_rows(monkeypatch):
    """The legacy top5 method name must still carry the requested top ten."""
    from app.core.task_manager import TaskManager
    from app.dependencies import get_email_sender

    today = date.today()
    rows = [
        {
            "id": index,
            "a_val": index,
            "zone_name": f"priority-{index:02d}",
            "category": 1,
            "total_days": 5,
            "days_done": 1,
            "deadline_raw": (today + timedelta(days=30 + index)).isoformat(),
            "today_done": None,
            "remaining_days": 4,
            "last_done_date": None,
        }
        for index in range(1, 13)
    ]
    manager = TaskManager.__new__(TaskManager)
    manager._mutation_lock = threading.RLock()
    manager.reset_completed_date = None
    manager.get_snapshot = lambda: (19, rows)

    sent = []
    sender = SimpleNamespace(
        send_daily_summary=lambda stats, top, **kwargs: sent.append((stats, top, kwargs)) or "queued",
        _instance_label=lambda: "synthetic",
    )
    monkeypatch.setattr("app.dependencies.get_email_sender", lambda: sender)

    snapshot = manager._capture_daily_snapshot(as_of=today)
    manager._deliver_daily_summary(snapshot, today)

    assert [item["zone_name"] for item in sent[0][1]] == [
        f"priority-{index:02d}" for index in range(1, 11)
    ]
    assert sent[0][2]["brief"] is True


def test_settled_completion_and_empty_rotation_use_one_combined_notice(
        controller, monkeypatch):
    """A's committed result is carried into one A-to-none rotation notice."""
    from app.core.live_controller import LiveInstruction

    notices = []
    legacy_completions = []

    class Mailer:
        def notify_task_complete(self, *args, **kwargs):
            legacy_completions.append((args, kwargs))

        def notify_task_rotation(self, **kwargs):
            notices.append(kwargs)

    class Manager:
        def settle_task_done(self, zone_name, **kwargs):
            return {
                "status": "settled",
                "zone_name": zone_name,
                "task_id": kwargs.get("task_id"),
                "execution_date": date.today().isoformat(),
                "days_done": 4,
                "all_done": False,
            }

        def select_next_instruction(self):
            return None

    controller.task_manager = Manager()
    controller._control_epoch = 5
    monkeypatch.setattr("app.dependencies.get_email_sender", lambda: Mailer())
    context = {}

    status = controller._settle_natural_completion(
        "A", 17, date.today().isoformat(), run_id="run-A",
        completion_context=context,
    )
    accepted = controller.run_next_task(epoch=5, completion_context=context)

    assert status == "settled"
    assert accepted is False
    assert legacy_completions == []
    assert len(notices) == 1
    assert notices[0]["completion"]["zone"] == "A"
    assert notices[0]["completion"]["days_done"] == 4
    assert notices[0]["next_task"] is None
    assert notices[0]["result"] == "none"


def test_rotation_late_result_does_not_repeat_completion(monkeypatch, controller):
    from app.core.live_controller import LiveInstruction

    notices = []

    class Mailer:
        def notify_task_rotation(self, **kwargs):
            notices.append(kwargs)

    monkeypatch.setattr("app.dependencies.get_email_sender", lambda: Mailer())
    instruction = LiveInstruction(
        zone_name="B", duration_seconds=1200, task_id=18,
        run_id="run-B", execution_date=date.today().isoformat())
    context = {
        "completion": {
            "zone": "A", "execution_date": date.today().isoformat(),
            "effective_seconds": 600, "days_done": 3,
        },
        "next_task": {"zone": "B", "duration_seconds": 1200},
        "instruction": instruction,
        "run_id": "run-B",
        "identity": "18:today:run-B",
        "preparing_sent": False,
        "finalized": False,
        "suppressed": False,
    }
    controller._task_rotation_context = context

    controller._task_rotation_preparing_timeout(context)
    controller._finish_task_rotation(context, "started")

    assert len(notices) == 2
    assert notices[0]["completion"]["zone"] == "A"
    assert notices[0]["result"] == "preparing"
    assert notices[1]["completion"] is None
    assert notices[1]["result"] == "started"
    assert notices[1]["late_update"] is True


def test_user_stop_suppresses_late_rotation_success(monkeypatch, controller):
    notices = []

    class Mailer:
        def notify_task_rotation(self, **kwargs):
            notices.append(kwargs)

    monkeypatch.setattr("app.dependencies.get_email_sender", lambda: Mailer())
    context = {
        "completion": {"zone": "A", "execution_date": "today",
                       "effective_seconds": 60, "days_done": 1},
        "next_task": {"zone": "B", "duration_seconds": 1200},
        "preparing_sent": False,
        "finalized": False,
        "suppressed": False,
    }
    controller._task_rotation_context = context

    controller._suppress_task_rotation()
    controller._finish_task_rotation(context, "started")

    assert notices == []


def test_platform_outage_episode_emits_one_recovery(monkeypatch, controller):
    results = []

    class Mailer:
        def notify_platform_outage_result(self, *args):
            results.append(args)

    monkeypatch.setattr("app.dependencies.get_email_sender", lambda: Mailer())
    controller.current_room_id = 123
    monkeypatch.setattr("app.core.live_controller._monotonic", lambda: 100.0)

    episode = controller._begin_platform_outage("run-outage", "A")
    episode["started_at"] = 0.0
    monkeypatch.setattr("app.core.live_controller._monotonic", lambda: 301.0)
    controller._platform_outage_maybe_alert()
    controller._platform_outage_maybe_alert()
    assert controller._platform_outage_recovered("run-outage") is True
    assert controller._platform_outage_recovered("run-outage") is False

    assert [result[1] for result in results] == ["still_retrying", "recovered"]
    assert results[0][0] == "A"
    assert results[0][2] == "run-outage"
    assert results[0][3] == episode["episode_id"]


def test_failed_progress_checkpoint_blocks_natural_settlement(controller, tmp_path):
    """An unpersisted confirmed-time checkpoint cannot settle or rotate a task."""
    from app.core.live_controller import LiveState

    class Manager:
        def __init__(self):
            self.settle_calls = []

        def settle_task_done(self, *args, **kwargs):
            self.settle_calls.append((args, kwargs))
            return {"status": "settled"}

    manager = Manager()
    controller.task_manager = manager
    controller.state = LiveState(str(tmp_path / "live_state.json"))
    controller.state.begin_session(
        "A", 123, 3600, duration_known=True,
        source="new", initial_elapsed=3600,
    )
    controller.state._last_save_error = "synthetic disk failure"

    status = controller._settle_natural_completion(
        "A", 17, date.today().isoformat(), run_id="run-A")

    assert status == "blocked"
    assert manager.settle_calls == []
    assert controller.state.current_zone == "A"
    assert controller.state.duration_seconds == 3600


def test_live_platform_status_cannot_credit_a_stalled_local_pusher(
        controller, monkeypatch):
    """Live room + Popen are insufficient without advancing media time."""
    from app.core.live_controller import EffectiveClock, LiveInstruction

    class MonitorStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return False

    class RunningProcess:
        def poll(self):
            return None

    class Reader:
        def __init__(self, media_seconds):
            self.media_seconds = media_seconds

        def read(self):
            return SimpleNamespace(
                outcome="available", media_seconds=self.media_seconds)

    settled = []
    controller.is_streaming = True
    controller._stream_mode = "task"
    controller._control_epoch = 9
    controller._current_run_id = "run-stalled"
    controller.current_room_id = 123
    controller.current_instruction = LiveInstruction(
        zone_name="A", duration_seconds=10, task_id=17,
        run_id="run-stalled", execution_date=date.today().isoformat(),
    )
    controller.state.begin_session(
        "A", 123, 10, duration_known=True,
        source="new", initial_elapsed=9,
    )
    controller.state.run_id = "run-stalled"
    controller._effective_clock = EffectiveClock()
    controller._effective_clock.reset(confirmed=9, origin="live")
    controller._segment_monotonic = 0.0
    controller._monitor_interval_seconds = 5
    controller.stop_monitor = MonitorStop()
    controller.video_process = RunningProcess()
    controller._video_process_generation = 31
    controller._pusher_media_generation = 31
    controller._pusher_progress_required = True
    controller._local_pusher_required = True
    reader = Reader(100.0)
    controller._pusher_media_reader = reader
    controller._pusher_media_seconds = 100.0
    controller._ffmpeg_loop_thread = SimpleNamespace(is_alive=lambda: True)
    controller.api.get_live_status = lambda _room: (
        True, {"data": {"live_status": 1}})
    controller.task_manager = SimpleNamespace(
        settle_task_done=lambda *args, **kwargs: settled.append((args, kwargs)))
    stopped = []
    controller._stop_live_process = lambda *args, **kwargs: stopped.append(kwargs)

    ticks = iter([0.0, 0.0, 5.1, 5.1, 5.2])
    monkeypatch.setattr("app.core.live_controller._monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        "app.api.settings.load_settings",
        lambda: SimpleNamespace(
            max_reconnect=3, live_retry_cooldown_minutes=60,
            scan_interval_seconds=5),
    )
    monkeypatch.setattr(controller, "_get_stream_settings", lambda: ("ffmpeg", False))

    controller._monitor_streaming()

    assert controller._confirmed_elapsed() == 9
    assert settled == []
    assert stopped == []

    # The first advancing sample only reopens the timer anchor. The next one
    # earns at most its media-time delta, never the stalled wall-clock gap.
    reader.media_seconds = 101.0
    assert controller._credit_effective_observation(10.0, 10.2) == "anchor"
    reader.media_seconds = 102.0
    assert controller._credit_effective_observation(15.0, 15.2) == "credited"
    assert controller._confirmed_elapsed() == 10


def test_short_ffmpeg_attempt_without_a_second_sample_stays_unconfirmed(
        controller):
    """An attempt ending between platform samples is conservatively undercounted."""
    from app.core.live_controller import EffectiveClock

    class ExitedProcess:
        def poll(self):
            return 0

    class Reader:
        def read(self):
            return SimpleNamespace(outcome="available", media_seconds=4.0)

    controller.is_streaming = True
    controller._local_pusher_required = True
    controller._pusher_progress_required = True
    controller._pusher_media_reader = Reader()
    controller.video_process = ExitedProcess()
    controller._effective_clock = EffectiveClock()
    controller._effective_clock.reset(confirmed=8, origin="live")
    controller._segment_monotonic = 0.0

    outcome = controller._credit_effective_observation(5.0, 5.1)

    assert outcome == "paused_pusher"
    assert controller._confirmed_elapsed() == 8
    assert controller._segment_monotonic is None


def test_external_obs_session_does_not_require_local_ffmpeg_progress(controller):
    """Platform-confirmed OBS sessions keep the existing effective clock."""
    from app.core.live_controller import EffectiveClock

    controller.is_streaming = True
    controller._stream_mode = "task"
    controller._local_pusher_required = False
    controller._effective_clock = EffectiveClock()
    controller._effective_clock.reset(confirmed=4, origin="live")
    controller._segment_monotonic = 0.0

    outcome = controller._credit_effective_observation(0.0, 5.0)

    assert outcome == "credited"
    assert controller._confirmed_elapsed() == 9
