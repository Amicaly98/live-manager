"""2026-09-28 收口批次·缺口3:FFmpeg 媒体三态与交接原子边界。

对应验收矩阵 A04/A05 与服务器 `9f6a112` PUSHER_FAILURE、`69b32f7` 交接 FSM:

- FFmpeg 任务模式下"平台开房"不再是接管事实:交接结论(abandoned)、
  begin_session、开播成功通知全部推迟到**本地媒体首次确认推进**;
- 推迟窗口内旧进度保留可恢复、成功邮件为零、无双推;
- 交接绑定写失败 → 受理失败;abandoned 写失败 → 接管中止;
  begin_session 落盘失败 → 绝不宣告成功;
- 手动/OBS 模式按平台开房接管(成功事实与 FFmpeg 分开)。

修前应整文件红(桌面当前在 _start_ffmpeg_stream 之前 begin_session+发成功事件)。
"""

import json
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _closeout_20260928 import CloseoutEnv, wait_until

fastapi_testclient = pytest.importorskip("fastapi.testclient")

AREA_FILE = [
    {"id": 1, "name": "学习大区", "parent_id": 0, "parent_name": "学习大区",
     "children": [{"id": 101, "name": "学习区", "parent_id": 1,
                   "parent_name": "学习大区", "children": []}]},
    {"id": 2, "name": "游戏大区", "parent_id": 0, "parent_name": "游戏大区",
     "children": [{"id": 102, "name": "游戏区", "parent_id": 2,
                   "parent_name": "游戏大区", "children": []}]},
]


class MediaReader:
    """FFmpeg stderr 进度读数替身:media_seconds 可编程推进。"""

    def __init__(self, seconds=100.0):
        self.media_seconds = seconds

    def read(self):
        return SimpleNamespace(outcome='available',
                               media_seconds=self.media_seconds)


def _arm_media(controller, generation=31, seconds=100.0):
    """把控制器摆成"本地推流已登记"的可观察状态(与生产 _claim_pusher 对齐)。"""
    controller.video_process = SimpleNamespace(poll=lambda: None)
    controller._video_process_generation = generation
    controller._pusher_media_generation = generation
    controller._pusher_progress_required = True
    controller._local_pusher_required = True
    controller._pusher_media_reader = MediaReader(seconds)
    controller._pusher_media_seconds = None


def _env(tmp_path, zones=('学习区', '游戏区')):
    env = CloseoutEnv(tmp_path, zones=zones,
                      settings={'stream_mode': 'ffmpeg', 'auto_open_video': False})
    env.area_file.write_text(json.dumps(AREA_FILE, ensure_ascii=False),
                             encoding='utf-8')
    env.controller.area_loader._load_areas()
    env.controller.video_finder.find_video = lambda zone: str(env.root / f'{zone}.mp4')
    return env


def _client(env):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import dependencies
    dependencies.init_globals(env.task_manager, env.controller, env.email)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def env(tmp_path):
    e = _env(tmp_path)
    yield e
    e.close()


@pytest.fixture()
def client(env):
    c = _client(env)
    yield c
    from app import dependencies
    dependencies.init_globals(None, None, None)


def _ticket(client):
    return client.get('/api/live/operation-ticket').json()['ticket']


def _start_specified(client, env, zone, claim=None):
    payload = {'task_id': env.task_id(zone),
               'expected_revision': env.revision(),
               'business_date': date.today().isoformat()}
    if claim is not None:
        payload['abandon_saved'] = claim
    return client.post('/api/live/start', json=payload,
                       headers={'X-Operation-Token': _ticket(client)})


def test_a04_ffmpeg_failure_keeps_old_progress_zero_success_mail(env, client):
    """A04:确认放弃旧进度后 FFmpeg 起不来 —— 旧进度可恢复、无 abandoned、
    成功通知为 0、无双推。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()

    # FFmpeg 无法启动(推流地址/进程失败);平台房间照常打开
    ffmpeg_attempts = []
    env.controller._start_ffmpeg_stream = lambda epoch=None: (
        ffmpeg_attempts.append(epoch) or False)

    r = _start_specified(client, env, '游戏区', claim=claim)
    assert r.status_code == 200 and r.json()['success'] is True, r.text
    assert wait_until(lambda: env.controller.api.start_live_calls, timeout=5)
    env.join_worker()

    # 平台开房但媒体未就绪:推迟窗口内旧存档原样
    assert env.read_state() == before, '接管前不得改写旧存档'
    assert env.email.count('stream_running') == 0, '成功邮件只能属于接管完成'
    # 交接记录存在且保持 pending(未记 abandoned)
    handover = env.read_handover()
    assert handover is not None, '确认放弃必须留下交接记录'
    assert handover['outcome'] == 'pending'
    assert handover['old']['elapsed_seconds'] == 600

    # 多个监控周期后仍无双推:FFmpeg 启动尝试只发生受理时那一次
    time.sleep(0.2)
    assert len(ffmpeg_attempts) == 1, f'双推风险:FFmpeg 启动被触发 {len(ffmpeg_attempts)} 次'

    # 旧进度仍可按 resume 恢复(resolver 认这份存档)
    instruction, reason = env.controller.resolve_resume_target()
    assert instruction is not None, f'旧进度必须可恢复:{reason}'
    assert instruction.task_id == env.task_id('学习区')


def test_a04_media_advance_completes_takeover_exactly_once(env, client):
    """A04 对照面:媒体首次确认推进 → 接管四件事按序完成且恰好一次。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    env.controller._start_ffmpeg_stream = lambda epoch=None: True

    r = _start_specified(client, env, '游戏区', claim=claim)
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    controller = env.controller
    controller._effective_timer_view()  # 不炸即可
    assert controller.is_streaming
    assert controller.state.current_zone == '学习区', '接管前存档仍是旧会话'
    assert env.email.count('stream_running') == 0

    # 平台确认直播中 + 本地媒体推进:第一次观察先立基线,第二次推进才接管
    _arm_media(controller, seconds=100.0)
    controller._effective_clock.reset(confirmed=0, origin='live')
    controller._segment_monotonic = None
    assert controller._credit_effective_observation(0.0, 0.2) == 'anchor'
    assert controller.state.current_zone == '学习区', '仅立基线不算接管'
    assert env.email.count('stream_running') == 0

    controller._pusher_media_reader.media_seconds = 105.0
    outcome = controller._credit_effective_observation(0.2, 5.2)
    assert outcome in ('anchor', 'credited', 'dropped_stale', 'dropped_gap')
    # 接管完成:旧进度记 abandoned、新会话落盘、恰好一封成功通知
    assert controller.state.current_zone == '游戏区'
    assert controller.state.task_id == env.task_id('游戏区')
    assert controller.state.effective_seconds <= 5.5, '新会话进度从 0 起算'
    handover = env.read_handover()
    assert handover['outcome'] == 'abandoned'
    assert handover['old']['elapsed_seconds'] == 600
    assert env.email.count('stream_running') == 1

    # 后续观察不得重复接管/重复发信
    controller._pusher_media_reader.media_seconds = 110.0
    controller._credit_effective_observation(5.2, 10.2)
    assert env.email.count('stream_running') == 1


def test_a05_handover_bind_write_failure_refuses_start(env, client):
    """A05:交接绑定持久写失败 → 受理失败,不开房,旧进度可恢复。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()

    def broken_atomic_write(payload):
        raise IOError('synthetic handover disk failure')

    env.controller._atomic_write_handover = broken_atomic_write

    r = _start_specified(client, env, '游戏区', claim=claim)
    assert r.status_code in (409, 500), r.text
    assert env.controller.api.start_live_calls == [], '绑定失败不得打开平台房间'
    assert env.read_state() == before
    assert env.email.count('stream_running') == 0


def test_a05_abandoned_write_failure_aborts_takeover(env, client):
    """A05:接管时 abandoned 落盘失败 → 接管中止、不入账、不发成功信。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()
    env.controller._start_ffmpeg_stream = lambda epoch=None: True

    r = _start_specified(client, env, '游戏区', claim=claim)
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    controller = env.controller
    _arm_media(controller, seconds=100.0)
    controller._effective_clock.reset(confirmed=0, origin='live')
    controller._segment_monotonic = None
    controller._credit_effective_observation(0.0, 0.2)  # 立基线

    # 第二次观察(媒体推进)时 abandoned 写失败
    def broken_finalize(outcome, **kwargs):
        if outcome == 'abandoned':
            raise IOError('synthetic abandoned disk failure')
        return True

    controller._pusher_media_reader.media_seconds = 105.0
    with patch.object(controller, '_finalize_handover',
                      side_effect=broken_finalize):
        outcome = controller._credit_effective_observation(0.2, 5.2)
    assert outcome == 'paused_pusher', '接管失败本次观察不得入账'
    assert env.read_state() == before, '旧存档一个字节未动'
    assert env.email.count('stream_running') == 0
    assert controller.state.current_zone == '学习区', '接管失败不得写入新会话'


def test_a05_begin_session_persist_failure_never_claims_success(env, client):
    """A05:begin_session 落盘失败 → 绝不宣告成功;旧进度保持可读。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()
    env.controller._start_ffmpeg_stream = lambda epoch=None: True

    r = _start_specified(client, env, '游戏区', claim=claim)
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    controller = env.controller
    _arm_media(controller, seconds=100.0)
    controller._effective_clock.reset(confirmed=0, origin='live')
    controller._segment_monotonic = None
    controller._credit_effective_observation(0.0, 0.2)

    def broken_save():
        controller.state._last_save_error = 'synthetic state disk failure'
        return False

    controller._pusher_media_reader.media_seconds = 105.0
    with patch.object(controller.state, 'save', staticmethod(broken_save)):
        outcome = controller._credit_effective_observation(0.2, 5.2)
    assert env.email.count('stream_running') == 0, '落盘失败不得发成功通知'
    assert env.read_state() == before


def test_a05_manual_obs_mode_takes_over_on_platform_open(env, client):
    """A05:手动/OBS 模式的接管事实是平台开房,不冒称本地媒体。"""
    env2_zone = '游戏区'
    env.controller._get_stream_settings = lambda: ('manual', False)
    env.controller._start_ffmpeg_stream = lambda epoch=None: True

    r = _start_specified(client, env, env2_zone)
    assert r.status_code == 200 and r.json()['success'] is True, r.text
    env.join_worker()
    controller = env.controller
    assert controller.is_streaming
    # 平台开房即接管:无推迟窗口
    assert getattr(controller, '_deferred_takeover', None) is None
    assert controller.state.current_zone == env2_zone
    assert env.email.count('stream_running') == 1, '手动模式成功事实=平台开房'
    assert controller.state.effective_seconds == 0


def test_a05_resume_ffmpeg_mode_also_defers_takeover(env, client):
    """A05:恢复(继承旧进度)在 FFmpeg 模式下同样推迟接管。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    controller = env.controller
    controller._start_ffmpeg_stream = lambda epoch=None: True
    controller.video_finder.find_video = lambda zone: ''

    from app.models.schemas import LiveInstruction
    instruction, reason = controller.resolve_resume_target()
    assert instruction is not None, reason
    ok = controller.start_streaming(
        instruction, '', is_task_mode=True, source='resume',
        inherit_elapsed=600)
    assert ok is True
    env.join_worker()
    assert controller.is_streaming
    assert controller.state.current_zone == '学习区'
    assert controller.state.effective_seconds == 600, '接管前存档未动'
    assert env.email.count('stream_running') == 0

    _arm_media(controller, seconds=50.0)
    controller._effective_clock.reset(confirmed=600, origin='resume')
    controller._segment_monotonic = None
    controller._credit_effective_observation(0.0, 0.2)  # 基线
    controller._pusher_media_reader.media_seconds = 55.0
    controller._credit_effective_observation(0.2, 5.2)
    assert env.email.count('stream_running') == 1
    assert controller.state.effective_seconds >= 600, '继承进度必须保留'
    assert controller.state.current_zone == '学习区'
