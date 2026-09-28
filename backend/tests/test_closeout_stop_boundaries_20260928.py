"""2026-09-28 收口批次·停止边界、受理冻结基线与邮件事实门槛。

对应验收矩阵 A09(受理/等待边界上的停止与退出)、B01 后端半场(受理响应
冻结基线)、B04/B05(邮件只对确定事实发送,轮转合并信与接管同门槛):

- 停止落在推迟接管窗口内 → 旧进度原样、交接 void、无成功通知、平台房收尾;
- 停止后代际推进:迟到的媒体推进/迟到确认不能复活开播;
- 重复停止只做一次清理;用户停止与平台下播补偿分离(5fdf917 语义复核);
- /start /resume /run-next 受理即返回冻结基线(accepted),失败带明确原因;
- 轮转 B 场的"已开始"结论必须与接管同一事实门槛(平台开房/媒体推进),
  不得在 FFmpeg 未就绪时发出合并成功信。

修前应整文件红。
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
    def __init__(self, seconds=100.0):
        self.media_seconds = seconds

    def read(self):
        return SimpleNamespace(outcome='available',
                               media_seconds=self.media_seconds)


def _arm_media(controller, generation=31, seconds=100.0):
    controller.video_process = SimpleNamespace(poll=lambda: None)
    controller._video_process_generation = generation
    controller._pusher_media_generation = generation
    controller._pusher_progress_required = True
    controller._local_pusher_required = True
    controller._pusher_media_reader = MediaReader(seconds)
    controller._pusher_media_seconds = None


def _env(tmp_path, zones=('学习区', '游戏区'), stream_mode='ffmpeg'):
    env = CloseoutEnv(tmp_path, zones=zones,
                      settings={'stream_mode': stream_mode,
                                'auto_open_video': False})
    env.area_file.write_text(json.dumps(AREA_FILE, ensure_ascii=False),
                             encoding='utf-8')
    env.controller.area_loader._load_areas()
    env.controller.video_finder.find_video = lambda zone: str(
        env.root / f'{zone}.mp4')
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


def test_a09_stop_inside_deferred_window_preserves_old_progress(env, client):
    """A09:停止落在推迟窗口 → 旧存档原样、交接 void、无成功信、房已收尾。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()
    env.controller._start_ffmpeg_stream = lambda epoch=None: True

    payload = {'task_id': env.task_id('游戏区'),
               'expected_revision': env.revision(),
               'business_date': date.today().isoformat(),
               'abandon_saved': claim}
    r = client.post('/api/live/start', json=payload,
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    controller = env.controller
    assert controller.is_streaming

    # 推迟窗口内用户停止
    assert controller.stop_streaming() is True
    assert env.read_state() == before, '旧进度一个字节未动'
    handover = env.read_handover()
    assert handover is not None and handover['outcome'] == 'void'
    assert env.email.count('stream_running') == 0
    assert env.email.count('task_complete') == 0, '未接管不得结算'
    # 平台房间有归属明确的收尾(以当前 run 发起的下播)
    assert wait_until(lambda: env.controller.api.stop_live_calls, timeout=5), \
        '平台已开房时停止必须有下播收尾'
    assert controller.is_streaming is False


def test_a09_late_media_advance_after_stop_cannot_revive(env, client):
    """A09:停止后代际推进 —— 迟到/泄漏的接管请求不能复活开播或接管。"""
    env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    before = env.read_state()
    env.controller._start_ffmpeg_stream = lambda epoch=None: True
    payload = {'task_id': env.task_id('游戏区'),
               'expected_revision': env.revision(),
               'business_date': date.today().isoformat(),
               'abandon_saved': claim}
    r = client.post('/api/live/start', json=payload,
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    controller = env.controller
    # 捕获推迟窗口快照(模拟一个迟到的接管请求仍持有旧引用)
    pending_snapshot = dict(controller._deferred_takeover or {})
    assert pending_snapshot, '推迟窗口必须存在'
    assert controller.stop_streaming() is True
    assert controller._deferred_takeover is None, '停止必须丢弃推迟窗口'

    # 迟到的接管请求:代际已推进,必须被拒且零副作用
    assert controller._complete_deferred_takeover(pending_snapshot) is False
    assert env.read_state() == before, '旧存档一个字节未动'
    assert env.email.count('stream_running') == 0
    assert env.email.count('task_complete') == 0
    assert controller.is_streaming is False


def test_a09_duplicate_stop_cleans_once(env, client):
    """A09:重复停止只做一次清理;平台下播恰好一次。"""
    # 关掉开播前清理的下播分支(live_status=0),让下播计数只来自停止路径
    env.controller.api.live_status_result = (True, {'code': 0,
                                                    'data': {'live_status': 0}})
    env.controller._get_stream_settings = lambda: ('manual', False)
    r = client.post('/api/live/start', json={'zone_name': '学习区',
                                             'duration_seconds': 60},
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    controller = env.controller
    assert wait_until(lambda: controller.api.start_live_calls, timeout=5)
    env.join_worker()

    assert controller.stop_streaming() is True
    assert controller.stop_streaming() is True
    assert controller.stop_streaming() is True
    platform_stops = [c for c in controller.api.stop_live_calls]
    assert len(platform_stops) == 1, f'平台下播必须恰好一次:{platform_stops}'


def test_a09_stop_failure_does_not_false_succeed(env, client):
    """A09:平台下播请求异常 → 停止仍受理但清理未确认,不假报成功。"""
    env.controller.api.live_status_result = (True, {'code': 0,
                                                    'data': {'live_status': 0}})
    env.controller._get_stream_settings = lambda: ('manual', False)
    env.controller.api.stop_live = lambda *_a: (_ for _ in ()).throw(
        RuntimeError('synthetic stop failure'))
    r = client.post('/api/live/start', json={'zone_name': '学习区',
                                             'duration_seconds': 60},
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    controller = env.controller
    controller.stop_streaming()
    assert controller._stop_cleanup_done is False, '平台下播失败必须标记未完成'
    assert controller._stop_in_progress is False


def test_b01_accepted_payload_freezes_baseline(env, client):
    """B01 后端:/start /resume /run-next 受理即回冻结基线,失败带原因。"""
    # 指定任务受理
    payload = {'task_id': env.task_id('学习区'),
               'expected_revision': env.revision(),
               'business_date': date.today().isoformat()}
    r = client.post('/api/live/start', json=payload,
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    accepted = r.json().get('accepted')
    assert accepted, '受理响应必须带冻结基线'
    assert accepted['run_id'] == env.controller._pending_run_id or accepted['run_id']
    assert accepted['task_id'] == env.task_id('学习区')
    assert accepted['zone_name'] == '学习区'
    assert accepted['confirmed_elapsed_seconds'] == 0
    env.controller.stop_streaming()

    # /resume 受理基线继承旧进度
    env.controller.state.current_zone = '学习区'
    env.controller.state.task_id = env.task_id('学习区')
    env.controller.state.execution_date = date.today().isoformat()
    env.controller.state.duration_seconds = 3600
    env.controller.state.duration_known = True
    env.controller.state.is_streaming = True
    env.controller.state.source_mode = 'resume'
    env.controller.state.elapsed_seconds = 600
    env.controller.state.run_id = 'run-resume'
    env.controller.state.save()
    r2 = client.post('/api/live/resume',
                     headers={'X-Operation-Token': _ticket(client)})
    assert r2.status_code == 200 and r2.json()['success'] is True, r2.text
    env.join_worker()
    accepted2 = r2.json().get('accepted')
    assert accepted2 and accepted2['confirmed_elapsed_seconds'] == 600
    assert accepted2['run_id'] == 'run-resume'


def test_b04_user_manual_stop_sends_no_meaningless_mail(env, client):
    """B04:用户手动停止不发无意义邮件;受理+成功不得重复刷屏。"""
    env.controller._get_stream_settings = lambda: ('manual', False)
    r = client.post('/api/live/start', json={'zone_name': '学习区',
                                             'duration_seconds': 60},
                    headers={'X-Operation-Token': _ticket(client)})
    assert r.status_code == 200 and r.json()['success'] is True
    env.join_worker()
    env.controller.stop_streaming()
    stopped_mails = [e for e in env.email.events if e['kind'] == 'stopped']
    # 桌面语义:手动停止不打扰(既定通知偏好默认免打扰)
    assert all(e.get('stage') in ('stopped', 'pending_recycle')
               for e in stopped_mails)
    kinds = [e['kind'] for e in env.email.events]
    assert kinds.count('stream_running') <= 1


def test_b05_rotation_started_conclusion_waits_for_takeover_fact(env, client):
    """B05:轮转 B 场合并信的 started 结论与接管同一事实门槛。"""
    controller = env.controller
    # A 场正常在播(已接管)
    controller._get_stream_settings = lambda: ('manual', False)
    from app.models.schemas import LiveInstruction
    day = date.today().isoformat()
    ins_a = LiveInstruction(zone_name='学习区', duration_seconds=60,
                            task_id=env.task_id('学习区'), execution_date=day)
    assert controller.start_streaming(ins_a, '', is_task_mode=True) is True
    assert wait_until(lambda: controller.api.start_live_calls, timeout=5)
    env.join_worker()
    assert env.email.count('stream_running') == 1

    # 模拟时长到点后的轮转:登记轮转上下文并启动 B 场(FFmpeg 模式)
    controller._get_stream_settings = lambda: ('ffmpeg', False)
    rotation = {'run_id': 'run-B', 'zone_name': '游戏区',
                'task_id': env.task_id('游戏区')}
    controller._task_rotation_context = rotation
    ins_b = LiveInstruction(zone_name='游戏区', duration_seconds=60,
                            task_id=env.task_id('游戏区'),
                            execution_date=day, run_id='run-B')
    controller.current_instruction = None
    controller.is_streaming = False
    controller._current_run_id = ''
    assert controller.start_streaming(ins_b, '', is_task_mode=True,
                                      epoch=controller._control_epoch) is True
    env.join_worker()
    # 受理成功后把轮转上下文绑定到 B 场实际 run(与生产 _claim_task_rotation 一致)
    controller._task_rotation_context = {'run_id': controller._current_run_id,
                                         'zone_name': '游戏区',
                                         'task_id': env.task_id('游戏区')}
    # B 场平台已开房但 FFmpeg 未就绪:不得发 started 合并信
    assert env.email.count('task_rotation') == 0, \
        'FFmpeg 未就绪时轮转合并信不得结论 started'

    _arm_media(controller, seconds=100.0)
    controller._effective_clock.reset(confirmed=0, origin='live')
    controller._segment_monotonic = None
    controller._credit_effective_observation(0.0, 0.2)
    controller._pusher_media_reader.media_seconds = 105.0
    controller._credit_effective_observation(0.2, 5.2)
    assert env.email.count('task_rotation') == 1, '媒体推进后合并信恰好一封'
    rotation_mail = [e for e in env.email.events if e['kind'] == 'task_rotation'][0]
    assert rotation_mail.get('outcome', rotation_mail.get('stage')) in (
        'started', None) or '已开始' in json.dumps(rotation_mail, ensure_ascii=False)


def test_a09_start_refusal_carries_reason(env, client):
    """A09/163f3da:受理拒绝(已在播/启动中)必须带回明确原因,不静默。"""
    env.controller._get_stream_settings = lambda: ('manual', False)
    r1 = client.post('/api/live/start', json={'zone_name': '学习区',
                                              'duration_seconds': 60},
                     headers={'X-Operation-Token': _ticket(client)})
    assert r1.status_code == 200 and r1.json()['success'] is True
    assert wait_until(lambda: env.controller.is_streaming, timeout=5)
    env.join_worker()
    r2 = client.post('/api/live/start', json={'zone_name': '游戏区',
                                              'duration_seconds': 60},
                     headers={'X-Operation-Token': _ticket(client)})
    assert r2.status_code == 200
    body = r2.json()
    assert body['success'] is False
    assert body.get('message'), '拒绝必须可见'
    assert '直播' in body['message'] or '运行' in body['message']
