"""2026-09-28 收口批次·缺口1/2:指定任务原子校验与放弃凭证核对。

对应验收矩阵 A01/A02/A03 与服务器 `9f6a112` 的 RENAME/MISSING_DAY/
OLD_PROGRESS_CHANGED 三条反例,全部走真实 HTTP 入口(`/api/live/start`):

- A01:点哪条开哪条 —— 请求必须携带冻结的 task_id,不得由后端按优先度
  自行挑选;缺冻结前置条件(expected_revision/business_date)必须明确拒绝;
- A02:确认(弹框)与受理之间任务被改名/删除重建/结算 → 409,平台不开播,
  旧进度不变,无成功通知;不得"降级成下一个任务";
- A03:业务日过期拒绝;旧进度 600→900 后的旧确认拒绝且 900 原样保留。

只替换平台边界与数据目录;路由、控制器、任务库均为生产实现。修前应整文件红。
"""

import json
import time
from datetime import date, timedelta
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

AREA_ID = {'学习区': 101, '游戏区': 102}


def _env(tmp_path, zones=('学习区', '游戏区')):
    env = CloseoutEnv(tmp_path, zones=zones,
                      settings={'stream_mode': 'manual', 'auto_open_video': False})
    env.area_file.write_text(json.dumps(AREA_FILE, ensure_ascii=False),
                             encoding='utf-8')
    env.controller.area_loader._load_areas()
    env.controller.video_finder.find_video = lambda zone: ''
    return env


def _client(env):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import dependencies
    dependencies.init_globals(env.task_manager, env.controller, env.email)
    client = TestClient(app, raise_server_exceptions=True)
    return client


def _auto_pick(env):
    ins = env.task_manager.select_next_instruction()
    return ins.task_id if ins else None


def AREA_ID_inverse(calls):
    area_to_zone = {v: k for k, v in AREA_ID.items()}
    return [area_to_zone.get(c['area_id'], f"area:{c['area_id']}") for c in calls]


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
    r = client.get('/api/live/operation-ticket')
    return r.json()['ticket']


def _start(client, payload, token=None):
    headers = {'X-Operation-Token': token or _ticket(client)}
    return client.post('/api/live/start', json=payload, headers=headers)


def test_a01_clicked_row_is_started_not_priority_first(env, client):
    """A01:请求携带的 task_id 必须被执行,而不是优先度最高者。"""
    auto_id = _auto_pick(env)
    assert auto_id is not None
    # 点的是"非自动首位"的那条:两个任务里选 auto 之外的一个
    clicked = '游戏区' if auto_id == env.task_id('学习区') else '学习区'
    clicked_id = env.task_id(clicked)
    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': env.revision(),
                        'business_date': date.today().isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['success'] is True, body
    assert wait_until(lambda: env.controller.api.start_live_calls, timeout=5)
    zones = AREA_ID_inverse(env.controller.api.start_live_calls)
    assert zones == [clicked], f"点的是{clicked},平台开播的却是{zones}"
    # 受理响应冻结基线:run/任务身份与点击目标一致
    accepted = body.get('accepted') or {}
    assert accepted.get('task_id') == clicked_id
    assert accepted.get('zone_name') == clicked
    assert accepted.get('run_id')


def test_a01_task_id_without_frozen_preconditions_is_rejected(env, client):
    """A01:缺 expected_revision / business_date 的指定开播必须 409 明确拒绝。"""
    clicked_id = env.task_id('学习区')
    r1 = _start(client, {'task_id': clicked_id})
    assert r1.status_code == 409
    assert ('revision' in r1.json()['detail']
            or 'business' in r1.json()['detail'])
    r2 = _start(client, {'task_id': clicked_id,
                         'expected_revision': env.revision()})
    assert r2.status_code == 409
    assert 'business' in r2.json()['detail']
    assert env.controller.api.start_live_calls == [], '拒绝路径不得触碰平台'


def test_a02_rename_between_confirm_and_start_is_rejected(env, client):
    """A02:确认后目标被改名 → 409;不开播、不降级、旧进度不变。"""
    clicked_id = env.task_id('学习区')
    frozen_revision = env.revision()
    frozen_day = date.today().isoformat()
    # 确认之后、请求之前,另一会话把目标改名(版本随之推进)
    assert env.task_manager.update_task_fields(
        '学习区', {}, task_id=clicked_id, rename_to='学习区改名')
    env.controller.state.begin_session(
        '学习区改名', 42, 3600, source='new', task_id=clicked_id,
        execution_date=frozen_day, initial_elapsed=600, run_id='run-old')
    env.controller.state.save()
    before = env.read_state()

    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': frozen_revision,
                        'business_date': frozen_day})
    assert r.status_code == 409, r.text
    assert env.controller.api.start_live_calls == []
    assert env.read_state() == before, '拒绝路径不得改写已保存进度'
    assert env.email.count('stream_running') == 0


def test_a02_settled_target_is_rejected_without_start(env, client):
    """A02:确认后目标被结算(今日完成)→ 409,不得把它当"下一个"重选。"""
    clicked_id = env.task_id('学习区')
    frozen_revision = env.revision()
    frozen_day = date.today().isoformat()
    status = env.task_manager.settle_task_done('学习区',
                                               execution_date=date.today(),
                                               task_id=clicked_id)
    assert status['status'] == 'settled'

    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': frozen_revision,
                        'business_date': frozen_day})
    assert r.status_code == 409, r.text
    assert env.controller.api.start_live_calls == []


def test_a02_deleted_target_is_rejected(env, client):
    """A02:确认后目标被删除 → 409,不得静默改开别的任务。"""
    clicked_id = env.task_id('学习区')
    frozen_revision = env.revision()
    frozen_day = date.today().isoformat()
    assert env.task_manager.delete_task_by_zone('学习区', task_id=clicked_id)
    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': frozen_revision,
                        'business_date': frozen_day})
    assert r.status_code == 409, r.text
    zones = AREA_ID_inverse(env.controller.api.start_live_calls)
    assert '学习区' not in zones
    assert env.controller.api.start_live_calls == [], '拒绝路径不得触碰平台'


def test_a03_stale_business_day_is_rejected(env, client):
    """A03:business_date 非今天的指定开播必须拒绝。"""
    clicked_id = env.task_id('学习区')
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': env.revision(),
                        'business_date': yesterday})
    assert r.status_code == 409, r.text
    assert env.controller.api.start_live_calls == []


def test_a03_changed_old_progress_claim_is_rejected_and_preserved(env, client):
    """A03:旧进度 600→900 后,600 的放弃确认必须 409 且 900 原样保留。"""
    saved = env.seed_saved_session('学习区', seconds=600, run_id='run-old')
    claim = env.make_claim('学习区', seconds=600, run_id='run-old')
    # 确认之后、受理之前,旧进度被(外部编辑/并发)推进到 900
    env.controller.state.elapsed_seconds = 900
    env.controller.state.save()
    clicked_id = env.task_id('游戏区')

    r = _start(client, {'task_id': clicked_id,
                        'expected_revision': env.revision(),
                        'business_date': date.today().isoformat(),
                        'abandon_saved': claim})
    assert r.status_code == 409, r.text
    assert 'elapsed' in r.json()['detail'], r.json()['detail']
    state = env.read_state()
    assert state['elapsed_seconds'] == 900, '900 必须原样保留'
    assert state['task_id'] == saved
    assert env.controller.api.start_live_calls == []
    handover = env.read_handover()
    assert not handover or handover.get('outcome') != 'abandoned'


def test_a01_manual_mode_still_uses_specified_zone(env, client):
    """A01:手动模式按用户指定分区执行,不受指定任务协议影响。"""
    r = _start(client, {'zone_name': '游戏区', 'duration_seconds': 60})
    assert r.status_code == 200 and r.json()['success'] is True, r.text
    assert wait_until(lambda: env.controller.api.start_live_calls, timeout=5)
    zones = AREA_ID_inverse(env.controller.api.start_live_calls)
    assert zones == ['游戏区']
    accepted = r.json().get('accepted') or {}
    assert accepted.get('source') == 'manual' or accepted.get('task_id') is None
