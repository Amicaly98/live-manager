"""发布前桌面后端同步矩阵。

这些测试只替换平台、SMTP 与单调时钟边界；任务库、监控收尾、通知模板和
配置读写均走当前桌面实现。每个测试使用 pytest ``tmp_path`` 下的新数据目录，
因此不会读取仓库 data/、用户 cookies 或既有任务库。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


TODAY = date.today()


def _wait_threads(threads, timeout: float = 3.0) -> None:
    """等待本测试创建的后台线程退出，避免测试把 worker 留给下一项。"""
    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"测试创建的后台线程未回收：{alive}"


@pytest.fixture()
def isolated_runtime(tmp_path, monkeypatch):
    """把 config 的真实字段切到本测试的独立 data 目录。"""
    from app.core import config

    old_dir = config.DATA_DIR
    old_initialized = config._data_dir_initialized
    data_dir = (tmp_path / "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BILIBILI_DATA_DIR", str(data_dir))

    # 桌面代码实际使用 DATA_DIR/_data_dir_initialized；不能只设置服务器版
    # 的同名环境变量后假定路径已经切换。
    config.DATA_DIR = data_dir
    config._data_dir_initialized = True

    # 让 LiveController 的启动引导只读合成分区，不进入在线拉取。
    (data_dir / "bili_areas_full.json").write_text(
        json.dumps([
            {
                "id": 1,
                "name": "合成大区",
                "parent_id": 0,
                "parent_name": "合成大区",
                "children": [
                    {"id": 101, "name": "急速区", "parent_id": 1,
                     "parent_name": "合成大区", "children": []},
                    {"id": 102, "name": "稳定区", "parent_id": 1,
                     "parent_name": "合成大区", "children": []},
                ],
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    yield data_dir

    config.DATA_DIR = old_dir
    config._data_dir_initialized = old_initialized


@pytest.fixture()
def manager(isolated_runtime):
    """带两个合成任务的真实 TaskManager/SQLite 实例。"""
    from app.core.db import TaskDB
    from app.core.task_manager import TaskManager

    db_path = isolated_runtime / "tasks.sqlite"
    seed_db = TaskDB(str(db_path))
    seed_db.insert_task({
        "zone_name": "急速区",
        "category": 2,
        "total_days": 3,
        "days_done": 1,
        "deadline_raw": (TODAY + timedelta(days=2)).isoformat(),
        "today_done": None,
        # 故意给旧列一个陈旧值；TaskManager 会按权威派生口径重算。
        "remaining_days": 1,
    })
    seed_db.insert_task({
        "zone_name": "稳定区",
        "category": 2,
        "total_days": 5,
        "days_done": 0,
        "deadline_raw": (TODAY + timedelta(days=60)).isoformat(),
        "today_done": None,
        "remaining_days": 1,
    })

    before = set(threading.enumerate())
    task_manager = TaskManager(
        db_path=str(db_path),
        excel_path=str(isolated_runtime / "tasks.xlsx"),
    )
    own_threads = [
        thread for thread in threading.enumerate()
        if thread not in before and thread.name == "DailyResetScheduler"
    ]
    try:
        yield task_manager
    finally:
        task_manager.shutdown()
        _wait_threads(own_threads)


def _task_id(task_manager, zone: str) -> int:
    row = task_manager.db.get_task_by_zone(zone)
    assert row and row["id"] is not None
    return int(row["id"])


def _write_workbook(path: Path, rows: list[dict]) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "优先度", "分区名", "类别", "需要完成天数", "已完成天数",
        "截止时间", "今日是否完成", "距离完成",
    ])
    for row in rows:
        sheet.append([
            row.get("priority", 1), row["zone_name"], row.get("category", 2),
            row.get("total_days", 2), row.get("days_done", 0),
            row.get("deadline_raw", (TODAY + timedelta(days=30)).isoformat()),
            row.get("today_done"), row.get("remaining_days", 1),
        ])
    workbook.save(path)
    workbook.close()


def test_sqlite_settlement_sort_and_revision_are_one_transaction(manager, monkeypatch):
    """结算真实 SQLite 行、排序和 revision 同步提交；重算失败必须整体回滚。"""
    assert [task.zone_name for task in manager.tasks] == ["急速区", "稳定区"]
    assert manager.tasks[0].remaining_exec_days() == 2

    urgent_id = _task_id(manager, "急速区")
    before_revision = manager.tasks_revision
    outcome = manager.settle_task_done(
        "急速区", execution_date=TODAY, task_id=urgent_id, run_id="run-a",
    )
    assert outcome["status"] == "settled"
    assert outcome["days_done"] == 2
    assert manager.tasks_revision is not None
    assert manager.tasks_revision > before_revision

    rows = manager.db.get_all_tasks()
    stored_revision, stored_fingerprint = manager.db.read_revision_meta()
    assert stored_revision == manager.tasks_revision
    assert stored_fingerprint == manager.db.fingerprint_rows(rows)
    committed = manager.db.get_task_by_id(urgent_id)
    assert committed["days_done"] == 2
    assert committed["today_done"] == 1
    assert committed["last_done_date"] == TODAY.isoformat()

    # 让真实提交路径在原字段更新后、版本写入前失败；SQLite 事务必须把两者
    # 一起回滚，不能留下“任务已结算、旧 revision 仍可覆盖”的窗口。
    saved_revision = manager.tasks_revision
    saved_row = dict(committed)

    def fail_revision(*_args, **_kwargs):
        raise RuntimeError("synthetic revision write failure")

    monkeypatch.setattr(manager, "_sync_revision_meta", fail_revision)
    failed = manager.settle_task_done(
        "稳定区", execution_date=TODAY,
        task_id=_task_id(manager, "稳定区"), run_id="run-b",
    )
    assert failed["status"] == "failed"
    assert manager.tasks_revision == saved_revision
    assert manager.db.get_task_by_zone("急速区")["days_done"] == saved_row["days_done"]
    stable_after = manager.db.get_task_by_zone("稳定区")
    assert stable_after["days_done"] == 0
    assert stable_after["today_done"] is None
    revision_after, fingerprint_after = manager.db.read_revision_meta()
    assert revision_after == saved_revision
    assert fingerprint_after == manager.db.fingerprint_rows(manager.db.get_all_tasks())


def test_old_overwrite_after_settlement_is_rejected_without_erasing_progress(manager):
    """结算后重放旧覆盖确认必须冲突，不能把已提交进度改回旧值。"""
    urgent_id = _task_id(manager, "急速区")
    old_revision = manager.tasks_revision
    settled = manager.settle_task_done(
        "急速区", execution_date=TODAY, task_id=urgent_id, run_id="run-old",
    )
    assert settled["status"] == "settled"
    assert manager.db.get_task_by_id(urgent_id)["days_done"] == 2

    from app.core.task_manager import TaskMutationRejected

    with pytest.raises(TaskMutationRejected) as rejected:
        manager.create_task({
            "id": urgent_id,
            "zone_name": "急速区",
            "category": 2,
            "total_days": 3,
            "days_done": 1,
            "deadline_raw": (TODAY + timedelta(days=2)).isoformat(),
            "today_done": None,
            "remaining_days": 2,
            "expected_revision": old_revision,
            "business_date": TODAY.isoformat(),
        }, overwrite=True)
    assert rejected.value.code == "overwrite_stale_revision"
    row = manager.db.get_task_by_id(urgent_id)
    assert row["days_done"] == 2
    assert row["today_done"] == 1
    assert row["last_done_date"] == TODAY.isoformat()


def test_import_receipt_replay_is_idempotent_and_token_conflict_safe(manager, isolated_runtime):
    """真实 Excel 计划导入写入 SQLite 收据；重放不重复写，改内容的同票据拒绝。"""
    workbook_path = isolated_runtime / "import.xlsx"
    _write_workbook(workbook_path, [{
        "zone_name": "导入区",
        "category": 2,
        "total_days": 4,
        "days_done": 0,
        "deadline_raw": (TODAY + timedelta(days=14)).isoformat(),
    }, {
        "zone_name": "急速区",
        "category": 2,
        "total_days": 3,
        "days_done": 1,
        "deadline_raw": (TODAY + timedelta(days=3)).isoformat(),
    }])

    first = manager.import_from_excel(
        str(workbook_path), mode="merge", operation_token="op-import-1",
    )
    assert first["rejected"] is False
    assert first["imported"] == 1
    assert first["updated"] == 1
    assert first["revision"] == manager.tasks_revision

    # 票据首次成功后，先提交一个无关任务，证明重放返回历史结果而不按当前
    # existing_zones 重新解释 imported/updated，也不回退后来提交。
    assert manager.create_task({
        "zone_name": "后来任务", "category": 2, "total_days": 1,
        "days_done": 0, "deadline_raw": (TODAY + timedelta(days=20)).isoformat(),
    })
    revision_after_other_write = manager.tasks_revision
    replay = manager.import_from_excel(
        str(workbook_path), mode="merge", operation_token="op-import-1",
    )
    assert replay["replayed"] is True
    assert replay["imported"] == first["imported"]
    assert replay["updated"] == first["updated"]
    assert manager.tasks_revision == revision_after_other_write
    assert manager.db.get_task_by_zone("后来任务") is not None

    _write_workbook(workbook_path, [{
        "zone_name": "导入区",
        "category": 2,
        "total_days": 99,
        "days_done": 0,
        "deadline_raw": (TODAY + timedelta(days=14)).isoformat(),
    }])
    from app.core.task_manager import TaskMutationRejected

    with pytest.raises(TaskMutationRejected) as conflict:
        manager.import_from_excel(
            str(workbook_path), mode="merge", operation_token="op-import-1",
        )
    assert conflict.value.code == "import_replay_conflict"
    assert manager.db.get_task_by_zone("导入区")["total_days"] == 4
    assert manager.db.get_task_by_zone("后来任务") is not None


class _MonitorApi:
    def __init__(self, live_status):
        self.live_status = live_status
        self.status_calls = 0
        self.stop_calls = []

    def get_live_status(self, _room_id):
        self.status_calls += 1
        return True, {"code": 0, "data": self.live_status}

    def get_csrf(self):
        return "synthetic-csrf"

    def stop_live(self, room_id, _csrf):
        self.stop_calls.append(room_id)
        return True, {"code": 0}


@pytest.fixture()
def live_controller(isolated_runtime, manager, monkeypatch):
    """构造真实 LiveController，但封锁其启动引导的网络边界。"""
    import app.core.live_controller as controller_module
    from app.core.live_controller import LiveController

    @contextmanager
    def local_retry(_cancel):
        yield

    monkeypatch.setattr(
        controller_module.BilibiliApi,
        "retry_network_until_cancelled",
        local_retry,
    )
    monkeypatch.setattr(controller_module.BilibiliApi, "is_logged_in", lambda _self: False)
    before = set(threading.enumerate())
    controller = LiveController(
        task_manager=manager,
        state_file=str(isolated_runtime / "live_state.json"),
        area_file=str(isolated_runtime / "bili_areas_full.json"),
    )
    bootstrap_threads = [
        thread for thread in threading.enumerate()
        if thread not in before and thread.name == "LiveBootstrap"
    ]
    _wait_threads(bootstrap_threads)
    try:
        yield controller
    finally:
        controller.stop_monitor.set()
        controller._start_cancel.set()
        if controller.monitor_thread:
            controller.monitor_thread.join(timeout=2.0)
        if controller._start_thread:
            controller._start_thread.join(timeout=2.0)
        controller._startup_cancel.set()
        _wait_threads(bootstrap_threads)


def _prepare_monitor(controller, manager, status, duration, execution_day, monkeypatch,
                     stop_after_first_wait=False):
    import app.api.settings as settings_module
    import app.core.live_controller as controller_module
    from app.models.schemas import LiveInstruction

    monkeypatch.setattr(
        settings_module,
        "load_settings",
        lambda: SimpleNamespace(
            max_reconnect=1,
            live_retry_cooldown_minutes=1,
            scan_interval_seconds=5,
        ),
    )
    api = _MonitorApi(status)
    controller.api = api
    controller._get_stream_settings = lambda: ("manual", False)
    task_id = _task_id(manager, "急速区")
    run_id = f"matrix-{execution_day.isoformat()}"
    controller.current_room_id = 9001
    controller.current_instruction = LiveInstruction(
        zone_name="急速区",
        duration_seconds=duration,
        task_id=task_id,
        run_id=run_id,
        execution_date=execution_day.isoformat(),
    )
    controller._stream_mode = "task"
    controller._current_run_id = run_id
    controller.is_streaming = True
    controller.stop_monitor.clear()
    controller._platform_stop_done = False
    controller._ffmpeg_unrecycled = False
    controller.video_process = None
    controller.state.begin_session(
        "急速区", 9001, duration, duration_known=True,
        source="new", task_id=task_id,
        execution_date=execution_day.isoformat(), run_id=run_id,
    )
    controller._effective_clock.reset(0, origin="live")
    controller._segment_monotonic = None

    class SteppedMonotonic:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 6.0
            return self.value

    monkeypatch.setattr(controller_module, "_monotonic", SteppedMonotonic())
    wait_calls = {"count": 0}

    def wait(_timeout=None):
        wait_calls["count"] += 1
        if stop_after_first_wait and wait_calls["count"] >= 1:
            controller.stop_monitor.set()
        return False

    monkeypatch.setattr(controller.stop_monitor, "wait", wait)
    return api


def test_monitor_unknown_status_never_settles(live_controller, manager, monkeypatch):
    """真实 monitor 收到成功但缺 live_status 时，暂停计时且不结算。"""
    called = []
    monkeypatch.setattr(
        live_controller,
        "_settle_natural_completion",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    api = _prepare_monitor(
        live_controller, manager, status={}, duration=1,
        execution_day=TODAY, monkeypatch=monkeypatch,
        stop_after_first_wait=True,
    )
    before = dict(manager.db.get_task_by_zone("急速区"))
    live_controller._monitor_streaming()
    after = manager.db.get_task_by_zone("急速区")
    assert api.status_calls == 1
    assert called == []
    assert live_controller._status_query_failures == 1
    assert after["days_done"] == before["days_done"]
    assert after["today_done"] is None
    assert live_controller.is_streaming is True


def test_monitor_normal_expiry_settles_through_real_cleanup(live_controller, manager, monkeypatch):
    """真实 monitor 的有效观察累计到目标后，先下播再按稳定身份结算。"""
    live_controller.run_next_task = lambda epoch=None: False
    api = _prepare_monitor(
        live_controller, manager, status={"live_status": 1}, duration=20,
        execution_day=TODAY, monkeypatch=monkeypatch,
    )
    live_controller._monitor_streaming()
    row = manager.db.get_task_by_zone("急速区")
    assert api.status_calls >= 3
    assert api.stop_calls == [9001]
    assert row["days_done"] == 2
    assert row["today_done"] == 1
    assert row["last_done_date"] == TODAY.isoformat()
    assert live_controller.is_streaming is False


def test_monitor_late_run_stale_day_does_not_settle(live_controller, manager, monkeypatch):
    """有效时长到期但执行日已过期时，真实收尾不得记到今天。"""
    live_controller.run_next_task = lambda epoch=None: False
    yesterday = TODAY - timedelta(days=1)
    api = _prepare_monitor(
        live_controller, manager, status={"live_status": 1}, duration=20,
        execution_day=yesterday, monkeypatch=monkeypatch,
    )
    live_controller._monitor_streaming()
    row = manager.db.get_task_by_zone("急速区")
    assert api.stop_calls == [9001]
    assert row["days_done"] == 1
    assert row["today_done"] is None
    assert row["last_done_date"] is None
    assert live_controller.is_streaming is False


def test_resume_retry_rechecks_real_task_identity(live_controller, manager):
    """人脸确认等待期间任务完成/替换时，resume 不得复活旧身份。"""
    from app.models.schemas import LiveInstruction

    task_id = _task_id(manager, "急速区")
    execution_day = TODAY.isoformat()
    live_controller.state.current_zone = "急速区"
    live_controller.state.task_id = task_id
    live_controller.state.execution_date = execution_day
    live_controller.state.source_mode = "resume"
    live_controller.state.run_id = "resume-owner"
    live_controller.state.is_streaming = False
    live_controller.state.effective_seconds = 4849
    live_controller.state.save()
    live_controller.current_instruction = LiveInstruction(
        zone_name="急速区", duration_seconds=3600, task_id=task_id,
        run_id="resume-owner", execution_date=execution_day,
    )
    live_controller._pending_source = "resume"
    live_controller._is_starting = False
    live_controller.stop_monitor.clear()
    live_controller._start_cancel.clear()
    intent = {
        "source": "resume",
        "zone": "急速区",
        "task_id": task_id,
        "execution_date": execution_day,
        "run_id": "resume-owner",
    }

    assert live_controller.validate_face_verify_retry_intent(intent) is True
    assert manager.settle_task_done(
        "急速区", execution_date=TODAY, task_id=task_id,
        run_id="settle-before-face-confirm",
    )["status"] == "settled"
    assert live_controller.validate_face_verify_retry_intent(intent) is False

    # 同名重建后 id 已变化；旧 resume 指令仍然不能落到新记录。
    assert manager.delete_task_by_zone("急速区", task_id=task_id)
    assert manager.create_task({
        "zone_name": "急速区", "category": 2, "total_days": 3,
        "days_done": 0, "deadline_raw": (TODAY + timedelta(days=2)).isoformat(),
    })
    assert live_controller.validate_face_verify_retry_intent(intent) is False


def test_manual_duration_zero_is_unlimited_and_negative_is_rejected(monkeypatch):
    """手动开播 API 保留显式 0，不把负数送进 controller。"""
    import asyncio
    from app.api import live as live_api
    from app.models.schemas import StartLiveRequest

    calls = []

    class ManualController:
        current_room_id = 9022
        _pending_face_verify = False
        _face_verify_url = ""
        video_finder = SimpleNamespace(find_video=lambda _zone: None)

        def _get_stream_settings(self):
            return "manual", False

        def start_streaming(self, instruction, video_path, **kwargs):
            calls.append((instruction, video_path, kwargs))
            return True

    controller = ManualController()
    monkeypatch.setattr(live_api, "get_live_controller", lambda: controller)
    monkeypatch.setattr(live_api, "_begin_operation", lambda *_args, **_kwargs: 0)

    async def run_cases():
        unlimited = await live_api.start_live(
            StartLiveRequest(zone_name="合成区", duration_seconds=0),
        )
        negative = await live_api.start_live(
            StartLiveRequest(zone_name="合成区", duration_seconds=-1),
        )
        return unlimited, negative

    unlimited, negative = asyncio.run(run_cases())
    assert unlimited.success is True
    assert len(calls) == 1
    assert calls[0][0].duration_seconds == 0
    assert negative.success is False
    assert "不能为负数" in negative.message
    assert len(calls) == 1


def test_late_confirmation_owner_is_rejected_but_current_owner_is_accepted(live_controller):
    """迟到的人脸确认不能清掉新会话状态，当前 run/room/epoch 才能确认。"""
    live_controller.current_room_id = 9023
    live_controller._current_run_id = "run-new"
    live_controller.state.run_id = "run-new"
    live_controller._control_epoch = 4
    live_controller._pending_face_verify = True
    live_controller._face_verify_url = "https://synthetic.invalid/face"
    live_controller.stop_monitor.clear()

    assert live_controller.confirm_face_verify(
        run_id="run-old", room_id=9023, epoch=3,
    ) is False
    assert live_controller._pending_face_verify is True
    assert live_controller.confirm_face_verify(
        run_id="run-new", room_id=9023, epoch=4,
    ) is True
    assert live_controller._pending_face_verify is False


@pytest.fixture()
def mailer(isolated_runtime, monkeypatch):
    """真实 EmailSender worker，只把最终 SMTP 连接替换为内存收集器。"""
    from app.core.email_sender import EmailSender

    sender = EmailSender()
    delivered = []
    monkeypatch.setattr(sender, "_load_config", lambda: {
        "master_enabled": True,
        "channel": "email",
        "email_enabled": True,
        "smtp_host": "127.0.0.1",
        "smtp_port": 1,
        "smtp_user": "synthetic@example.invalid",
        "smtp_pass": "synthetic",
        "recipients": "receiver@example.invalid",
        "serverchan_sendkey": "",
        "notify_start": True,
        "notify_stop": True,
        "notify_error": True,
        "notify_complete": True,
        "daily_summary": True,
        "face_verify_port": 19080,
    })

    def capture(_config, subject, body, html=False):
        delivered.append({"subject": subject, "body": body, "html": html})
        return True

    # SMTP 是唯一被替换的投递边界；模板、事件分类、去重和 worker 仍是真实实现。
    monkeypatch.setattr(sender, "_do_send_email", capture)
    try:
        yield sender, delivered
    finally:
        sender.shutdown()
        assert all(not thread.is_alive() for thread in sender._workers)


def _drain_notifications(sender):
    sender._queue.join()


def test_notification_templates_keep_stop_and_face_facts(mailer):
    """停播未回收和人脸状态各保留一个准确事实，重复人脸事件只发一封。"""
    sender, delivered = mailer
    queued = sender.notify_stopped(
        "急速区", stage="pending_recycle", elapsed_label="12 分钟",
        reason="仍有资源未确认回收，可再次点击停止重试",
    )
    assert queued == "queued"
    _drain_notifications(sender)
    stop_body = delivered[-1]["body"]
    assert "停止处理中（结果待确认）" in stop_body
    assert "状态：已停止" not in stop_body
    assert "仍有资源未确认回收" in stop_body

    first = sender.send_face_verify(
        "https://synthetic.invalid/verify", run_id="face-run",
        room_id=9001, epoch=3, stage="status_query",
        local_stop_requested=False,
    )
    _drain_notifications(sender)
    second = sender.send_face_verify(
        "https://synthetic.invalid/verify", run_id="face-run",
        room_id=9001, epoch=3, stage="status_query",
        local_stop_requested=False,
    )
    _drain_notifications(sender)
    assert first == "queued"
    assert second == "coalesced"
    face_bodies = [item["body"] for item in delivered
                   if "状态查询提示需要人脸验证" in item["body"]]
    assert len(face_bodies) == 1
    assert "本路径仅提示验证，未停止推流" in face_bodies[0]
    assert "本路径已请求停止本地推流" not in face_bodies[0]


def test_daily_summary_uses_stat_date_and_derived_remaining_values(mailer):
    """日报把统计日与发送时刻分开，并展示权威剩余 8/4/4 及 8/4 统计。"""
    sender, delivered = mailer
    summary_date = TODAY - timedelta(days=1)
    result = sender.send_daily_summary(
        {
            "today_done": 4,
            "today_pending": 4,
            "pending_total": 3,
            "remaining_time": 8,
            "avg_remaining": 4,
            "urgency": 0.4,
        },
        [
            {"zone_name": "甲", "priority": 1, "days_done": 0,
             "actual_days": 8, "remaining_exec_days": 8},
            {"zone_name": "乙", "priority": 2, "days_done": 1,
             "actual_days": 5, "remaining_exec_days": 4},
            {"zone_name": "丙", "priority": 3, "days_done": 2,
             "actual_days": 6, "remaining_exec_days": 4},
        ],
        summary_date=summary_date,
        snapshot_meta={
            "as_of": datetime.combine(summary_date, datetime.min.time()),
            "revision": 17,
            "rows": 3,
            "done_flags": {
                "window": summary_date,
                "confident": True,
                "done_rows": 4,
                "dateless_done_rows": 0,
            },
        },
    )
    assert result == "queued"
    _drain_notifications(sender)
    body = delivered[-1]["body"]
    assert f"统计日期：</b>{summary_date.isoformat()}" in body
    assert "<b>当日已完成</b></td><td>4" in body
    assert "<b>待执行任务（当前口径）</b></td><td>4" in body
    assert "<b>剩余时间</b></td><td>8 小时" in body
    assert body.count("<td>8</td>") == 1
    assert body.count("<td>4</td>") >= 2
    assert "版本 17" in body


def test_settings_conflict_and_last_good_copy_are_explicit(isolated_runtime):
    """配置版本冲突拒绝旧写入；主文件损坏时读取最后可用副本并标明 fallback。"""
    from app.api import settings as settings_module

    first_revision = settings_module.save_settings(
        settings_module.AppSettings(scan_interval_seconds=11),
    )
    assert first_revision == 1
    with pytest.raises(settings_module.SettingsConflict):
        settings_module.save_settings(
            settings_module.AppSettings(scan_interval_seconds=99),
            expected_revision=0,
        )
    assert settings_module.load_settings().scan_interval_seconds == 11

    primary = isolated_runtime / "settings.json"
    fallback = isolated_runtime / "settings.last-good.json"
    assert primary.exists() and fallback.exists()
    primary.write_text("{broken json", encoding="utf-8")
    restored, revision, trust = settings_module.settings_document()
    assert trust == "fallback"
    assert revision == first_revision
    assert restored.scan_interval_seconds == 11
