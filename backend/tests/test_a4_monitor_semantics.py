"""A4：监控语义——查询失败只记未知不掐流；恢复按实际推流方式判断。"""

import time
from datetime import datetime

import pytest

from app.models.schemas import LiveInstruction


class StubSettings:
    max_reconnect = 3
    live_retry_cooldown_minutes = 60
    scan_interval_seconds = 5  # 桌面设置里的最小值


@pytest.fixture()
def fast_monitor(monkeypatch):
    """把监控间隔压到最小（5s），并让 stop_monitor.wait 立即返回不阻塞。"""
    import app.api.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_settings", lambda: StubSettings())


def _make_instruction(zone):
    return LiveInstruction(zone_name=zone, duration_seconds=3600)


def _run_monitor_for_one_round(controller, monkeypatch, round_seconds=6.0):
    """让监控线程跑完一轮查询后停止。stop_monitor.wait 打桩为立即 False。"""
    import threading
    monkeypatch.setattr(controller.stop_monitor, "wait",
                        lambda t=None: False, raising=True)

    def stop_after_round():
        time.sleep(round_seconds)
        controller.stop_monitor.set()
    threading.Thread(target=stop_after_round, daemon=True).start()
    controller._monitor_streaming()


def test_query_failure_records_unknown_not_restart(controller, fake_api, monkeypatch, fast_monitor):
    """状态查询失败（网络异常）→ 计数+节流提示；绝不触发 _retry_start_live。"""
    from datetime import datetime as dt
    controller.current_room_id = 42
    controller.is_streaming = True
    controller.current_instruction = _make_instruction("王者荣耀")
    controller.stream_start_time = dt.now()
    controller._stream_mode = 'task'

    fake_api.live_status_result = (False, {"code": -1, "msg": "网络错误"})
    retry_calls = []
    monkeypatch.setattr(controller, '_retry_start_live', lambda: retry_calls.append(1))

    _run_monitor_for_one_round(controller, monkeypatch)

    assert controller._status_query_failures >= 1
    assert retry_calls == [], "查询失败绝不重开直播间（A4 红线）"


def test_query_failure_throttled_notice(controller, fake_api, monkeypatch, fast_monitor):
    """连续查询失败 → 60 秒节流，只推送一次提示事件。"""
    controller.current_room_id = 42
    controller.is_streaming = True
    controller.current_instruction = _make_instruction("王者荣耀")
    controller.stream_start_time = datetime.now()
    controller._stream_mode = 'task'

    fake_api.live_status_result = (False, {"code": -1, "msg": "网络错误"})
    notices = []
    monkeypatch.setattr(controller, '_push_backend_event',
                        lambda tag, t, msg: notices.append(tag) if tag == '监控' else None)

    _run_monitor_for_one_round(controller, monkeypatch, round_seconds=11.5)
    # 11.5 秒内约两轮查询，但节流 60s → 最多 1 条
    assert len(notices) <= 1


def test_confirmed_platform_close_with_network_ok_triggers_limited_retry(controller, fake_api, monkeypatch, fast_monitor):
    """查询成功且 live_status=0（确认被平台关闭）+ 网络正常 → 走限次重连。"""
    controller.current_room_id = 42
    controller.is_streaming = True
    controller.current_instruction = _make_instruction("王者荣耀")
    controller.stream_start_time = datetime.now()
    controller._stream_mode = 'task'

    fake_api.live_status_result = (True, {"code": 0, "data": {"live_status": 0}})
    retry_calls = []
    monkeypatch.setattr(controller, '_handle_live_anomaly_retry',
                        lambda mx, cd: retry_calls.append((mx, cd)))
    monkeypatch.setattr(controller, '_check_network_ok', lambda: True)

    _run_monitor_for_one_round(controller, monkeypatch)
    assert retry_calls, "确认平台掐断+网络正常 → 应限次重连"


def test_retry_restores_ffmpeg_for_manual_partition(controller, fake_api, monkeypatch):
    """手动分区 + FFmpeg 设置 → 重连后同样恢复推流（A4：不看 task/manual）。"""
    controller.current_room_id = 42
    controller.current_instruction = _make_instruction("英雄联盟")
    controller._stream_mode = 'manual'  # 手动分区

    monkeypatch.setattr(controller, '_get_stream_settings', lambda: ('ffmpeg', True))
    started = []
    monkeypatch.setattr(controller, '_start_ffmpeg_stream', lambda: started.append(1) or True)
    monkeypatch.setattr(controller.area_loader, 'get_area_id', lambda z, auto_update=False: 101)
    fake_api.start_live_result = (True, {"code": 0, "data": {"rtmp": {"addr": "rtmp://a", "code": "?k=1"}}})

    controller._retry_start_live()

    assert started == [1], "手动分区的 FFmpeg 推流必须恢复（旧版被 _stream_mode != 'manual' 挡住）"


def test_retry_does_not_touch_obs_stream(controller, fake_api, monkeypatch):
    """OBS 外部推流（非 ffmpeg 设置）→ 重连只重开房间，不碰用户推流进程。"""
    controller.current_room_id = 42
    controller.current_instruction = _make_instruction("英雄联盟")
    controller._stream_mode = 'manual'

    monkeypatch.setattr(controller, '_get_stream_settings', lambda: ('manual', True))
    started = []
    monkeypatch.setattr(controller, '_start_ffmpeg_stream', lambda: started.append(1) or True)
    monkeypatch.setattr(controller.area_loader, 'get_area_id', lambda z, auto_update=False: 102)
    fake_api.start_live_result = (True, {"code": 0, "data": {}})

    controller._retry_start_live()
    assert started == [], "OBS 模式不得启动本地 FFmpeg"
