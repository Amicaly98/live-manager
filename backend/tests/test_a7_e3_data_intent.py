"""A7/E3/A8：停止意图持久化、数据目录初始化与旧数据迁移。"""

import json
import threading
import time
from pathlib import Path

import pytest

from app.core.config import stop_intent_file_path, init_data_dir, DATA_DIR
from app.core.data_migration import migrate_legacy_data


def test_user_stop_persists_intent(controller, fake_api, fresh_state):
    """用户停止：持久化停止意图；任务模式保留进度（桌面手动恢复语义）。"""
    from datetime import datetime, timedelta
    controller.current_room_id = 77
    controller.is_streaming = True
    controller._stream_mode = 'task'
    controller.current_instruction = _make_instruction("王者荣耀")
    controller.stream_start_time = datetime.now() - timedelta(seconds=120)
    controller.state.is_streaming = True
    controller.state.current_zone = "王者荣耀"
    controller.state.elapsed_seconds = 100

    controller.stop_streaming()

    f = stop_intent_file_path()
    assert f.exists(), "停止意图必须持久化"
    data = json.loads(f.read_text(encoding='utf-8'))
    assert data['zone'] == '王者荣耀'
    # 任务模式保留状态供恢复
    assert controller.state.is_streaming is True
    assert controller.state.elapsed_seconds >= 100


def test_successful_start_clears_stop_intent(controller, fake_api, fresh_state):
    """成功开播后清除持久化停止意图。"""
    stop_intent_file_path().write_text('{"zone": "x", "epoch": 0}', encoding='utf-8')
    controller.current_room_id = 78
    controller.switch_partition = lambda zone: True
    instruction = _make_instruction("英雄联盟")
    controller.start_streaming(instruction, "", is_task_mode=True)
    deadline = time.time() + 5
    while time.time() < deadline and not controller.is_streaming:
        time.sleep(0.05)
    assert controller.is_streaming
    # 等待开播线程完成收尾（清除动作发生在 is_streaming=True 之后一瞬）
    if controller._start_thread:
        controller._start_thread.join(timeout=5)
    assert not stop_intent_file_path().exists(), "新开播必须清除停止意图"
    controller.stop_streaming()


def test_late_face_verify_confirm_rejected_after_stop(controller):
    """停止后代际推进：迟到的邮件验证确认不得重试开播（A7）。"""
    confirm_epoch = controller._control_epoch
    controller._advance_control_epoch()  # 用户停止
    assert controller.retry_after_face_verify_guarded(confirm_epoch) is False
    assert controller.retry_after_face_verify_guarded(controller._control_epoch) is True


def test_stale_epoch_start_rejected_synchronously(controller):
    """停止前挂起的开播请求带着旧代际到达 → 同步拒绝，不接受。"""
    old_epoch = controller._control_epoch
    controller._advance_control_epoch()
    instruction = _make_instruction("王者荣耀")
    accepted = controller.start_streaming(instruction, "", is_task_mode=True,
                                          epoch=old_epoch)
    assert accepted is False
    assert controller._is_starting is False


def test_data_dir_init_and_write_probe(tmp_path):
    """数据目录初始化：创建 + 可写探测 + 幂等（测试后还原全局状态）。"""
    from app.core import config
    saved_dir, saved_flag = config.DATA_DIR, config._data_dir_initialized
    try:
        config._data_dir_initialized = False
        d = tmp_path / "iso-data"
        p1 = config.init_data_dir(str(d))
        assert p1 == d.resolve() and p1.exists()
        # 探测文件写入并可读回（保留不删除：兼容环境的删除保护）
        probe = p1 / ".write_probe"
        assert probe.exists() and probe.read_text(encoding="utf-8") != ""
        p2 = config.init_data_dir()  # 幂等（已初始化时直接返回）
        assert p2 == p1
    finally:
        config.DATA_DIR = saved_dir
        config._data_dir_initialized = saved_flag


def test_migration_copies_with_backup_and_never_overwrites(tmp_path):
    """E3：旧数据迁移——带备份；目标已存在的新数据绝不被旧数据覆盖。"""
    legacy = tmp_path / "legacy_root"
    data = tmp_path / "data"
    legacy.mkdir()
    data.mkdir()
    (legacy / "live_state.json").write_text('{"elapsed_seconds": 555}', encoding='utf-8')
    (legacy / "settings.json").write_text('{"video_path": "old"}', encoding='utf-8')
    # 新数据已存在 → 必须保留
    (data / "settings.json").write_text('{"video_path": "new"}', encoding='utf-8')

    actions = dict(migrate_legacy_data(data, legacy))
    assert actions["live_state.json"] == "migrated"
    assert actions["settings.json"] == "kept_new"
    assert json.loads((data / "live_state.json").read_text(encoding='utf-8'))["elapsed_seconds"] == 555
    assert json.loads((data / "settings.json").read_text(encoding='utf-8'))["video_path"] == "new"
    # 备份存在
    backups = list((data / "migration_backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "live_state.json").exists()


def test_migration_noop_when_same_dir(tmp_path):
    data = tmp_path / "same"
    data.mkdir()
    assert migrate_legacy_data(data, data) == []


def _make_instruction(zone):
    from app.models.schemas import LiveInstruction
    return LiveInstruction(zone_name=zone, duration_seconds=3600)
