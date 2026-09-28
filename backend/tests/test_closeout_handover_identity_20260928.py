"""2026-09-28 收口批次·缺口4:交接归属、终态不阻断、人脸验证身份冻结。

对应验收矩阵 A06/A07/A08 与服务器 `ead7ca0`、`58af5ab`:

- A06:交接读—核—写同锁;旧线程(迟到 void)不能覆盖/结束新记录;
- A07:历史 void/abandoned/superseded 不拦截普通恢复与普通开播;
  只有当前意图要求的交接缺失/被换/损坏/已结论才拒绝接管;
- A08:60024/60043 后确认重试延续原交接身份;旧确认对新意图零副作用;
- 重启把 pending 清为 void(幂等),旧进度可恢复。

修前应整文件红(桌面尚无 handover 状态机)。
"""

import json
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests._closeout_20260928 import CloseoutEnv, wait_until

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


def _env(tmp_path, worker_mode="thread"):
    env = CloseoutEnv(tmp_path, zones=('学习区', '游戏区'),
                      settings={'stream_mode': 'ffmpeg', 'auto_open_video': False},
                      worker_mode=worker_mode)
    env.area_file.write_text(json.dumps(AREA_FILE, ensure_ascii=False),
                             encoding='utf-8')
    env.controller.area_loader._load_areas()
    env.controller.video_finder.find_video = lambda zone: ''
    return env


@pytest.fixture()
def env(tmp_path):
    e = _env(tmp_path)
    yield e
    e.close()


def _record_handover(env, claim, target_zone='游戏区', epoch=None):
    from app.models.schemas import LiveInstruction
    target = LiveInstruction(zone_name=target_zone, duration_seconds=3600,
                             task_id=env.task_id(target_zone),
                             execution_date=date.today().isoformat())
    ok, code, handover_id = env.controller.record_saved_abandon_handover(
        claim, target, epoch=epoch)
    return ok, code, handover_id


def test_a06_late_void_cannot_override_new_record(tmp_path):
    """A06:锁外交错的迟到 void 不能改写并发登记的新交接。"""
    env = _env(tmp_path)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        claim_a = env.make_claim('学习区', seconds=600, run_id='run-old',
                                 confirm_id='confirm-A')
        ok, code, handover_id_a = _record_handover(env, claim_a)
        assert ok, code

        # 新意图在同一把锁内登记自己的交接(旧的被 superseded)
        claim_b = env.make_claim('学习区', seconds=600, run_id='run-old',
                                 confirm_id='confirm-B')
        ok2, code2, handover_id_b = _record_handover(env, claim_b)
        assert ok2, code2
        record = env.read_handover()
        assert record['handover_id'] == handover_id_b

        # 旧 worker 的迟到 void:必须按 (handover_id, target run, epoch) 归属被拒
        assert env.controller._finalize_handover(
            'void', handover_id=handover_id_a,
            target_run_id='run-new-owner', epoch=env.controller._control_epoch
        ) is False, '旧交接身份不能结束新记录'
        after = env.read_handover()
        assert after['handover_id'] == handover_id_b

        # 无归属参数的尽力收尾也不得覆盖已有结论不同的新记录
        assert env.controller._finalize_handover('void') is True
        assert env.read_handover()['outcome'] in ('void', 'pending')
    finally:
        env.close()


def test_a06_record_is_atomic_under_state_lock(tmp_path):
    """A06:record 在状态写锁内重核+落盘,并发状态改写必被拒或被察觉。"""
    env = _env(tmp_path)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        claim = env.make_claim('学习区', seconds=600, run_id='run-old')
        # 在 record 的重核与落盘之间,把旧进度改成 900(模拟并发 /state/update)
        original = env.controller.verify_saved_abandon_claim

        def verify_then_race(*args, **kw):
            result = original(*args, **kw)
            env.controller.state.elapsed_seconds = 900
            env.controller.state.save()
            return result

        with patch.object(env.controller, 'verify_saved_abandon_claim',
                          side_effect=verify_then_race):
            ok, code, handover_id = _record_handover(env, claim)
        assert not ok, f'重核到变化必须拒绝:{code}'
        assert 'elapsed' in code
    finally:
        env.close()


@pytest.mark.parametrize('terminal', ['void', 'abandoned', 'superseded'])
def test_a07_terminal_states_do_not_block_plain_recovery(tmp_path, terminal):
    """A07:历史终态文件不拦截普通恢复;当前意图无交接要求时不读它。"""
    env = _env(tmp_path)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        # 铺一份已结论的历史交接
        record = {
            'handover_id': 'hist-1', 'confirm_id': 'hist-confirm',
            'outcome': terminal, 'created_at': '2026-09-28T00:00:00',
            'epoch': 0,
            'old': {'run_id': 'hist-run', 'task_id': env.task_id('学习区'),
                    'zone_name': '学习区',
                    'execution_date': date.today().isoformat(),
                    'elapsed_seconds': 1},
            'target': {'task_id': env.task_id('游戏区'), 'zone_name': '游戏区',
                       'duration_seconds': 3600, 'run_id': 'hist-target'},
        }
        env.controller._atomic_write_handover(record)

        instruction, reason = env.controller.resolve_resume_target()
        assert instruction is not None, f'{terminal} 终态不得拦截恢复:{reason}'
        assert instruction.task_id == env.task_id('学习区')
        ok = env.controller.start_streaming(
            instruction, '', is_task_mode=True, source='resume',
            inherit_elapsed=600)
        assert ok is True, '普通恢复不得被历史终态拒绝'
        env.join_worker()
        assert env.controller.is_streaming
    finally:
        env.close()


def test_a07_required_handover_missing_or_swapped_refuses_takeover(tmp_path):
    """A07:当前意图要求的交接缺失/被换/已结论 → 拒绝接管,旧存档不变。"""
    for scenario in ('missing', 'swapped', 'concluded'):
        env = _env(tmp_path / scenario)
        try:
            env.seed_saved_session('学习区', seconds=600, run_id='run-old')
            claim = env.make_claim('学习区', seconds=600, run_id='run-old',
                                   confirm_id=f'c-{scenario}')
            ok, code, handover_id = _record_handover(env, claim)
            assert ok, code

            controller = env.controller
            from app.models.schemas import LiveInstruction
            target = LiveInstruction(zone_name='游戏区', duration_seconds=3600,
                                     task_id=env.task_id('游戏区'),
                                     execution_date=date.today().isoformat())
            ok_start = controller.start_streaming(
                target, '', is_task_mode=True, source='new',
                handover_confirmed=True, expected_handover_id=handover_id)
            assert ok_start is True
            env.join_worker()
            assert controller.is_streaming

            if scenario == 'missing':
                controller._handover_file().unlink()
            elif scenario == 'swapped':
                controller._atomic_write_handover({
                    'handover_id': 'other-id', 'confirm_id': 'x',
                    'outcome': 'pending', 'created_at': 'now', 'epoch': 0,
                    'old': {}, 'target': {}})
            else:
                controller._finalize_handover('void')

            _arm_media(controller, seconds=100.0)
            controller._effective_clock.reset(confirmed=0, origin='live')
            controller._segment_monotonic = None
            controller._credit_effective_observation(0.0, 0.2)
            controller._pusher_media_reader.media_seconds = 105.0
            outcome = controller._credit_effective_observation(0.2, 5.2)
            assert outcome == 'paused_pusher', f'{scenario}:接管必须被拒'
            assert controller.state.current_zone == '学习区'
            assert env.email.count('stream_running') == 0
        finally:
            env.close()


def test_restart_clears_pending_handover_to_void_and_progress_recoverable(tmp_path):
    """重启:pending 交接清为 void(幂等),旧进度可正常恢复。"""
    root = tmp_path / 'restart'
    env = _env(root)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        claim = env.make_claim('学习区', seconds=600, run_id='run-old')
        ok, code, handover_id = _record_handover(env, claim)
        assert ok, code
        assert env.read_handover()['outcome'] == 'pending'
    finally:
        env.close()

    # 模拟重启:同一数据目录新建控制器
    env2 = CloseoutEnv(root, zones=('学习区', '游戏区'),
                       settings={'stream_mode': 'ffmpeg'})
    try:
        record = env2.read_handover()
        assert record['outcome'] == 'void', '重启必须把 pending 清为 void'
        instruction, reason = env2.controller.resolve_resume_target()
        assert instruction is not None, 'void 后旧进度必须可恢复'
        assert instruction.task_id == env2.task_id('学习区')
    finally:
        env2.close()


def test_a08_face_retry_keeps_handover_identity(tmp_path):
    """A08:60024 拦截后确认重试延续原交接身份到媒体就绪才结论。"""
    env = _env(tmp_path)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        claim = env.make_claim('学习区', seconds=600, run_id='run-old')
        ok, code, handover_id = _record_handover(env, claim)
        assert ok, code

        controller = env.controller
        from app.models.schemas import LiveInstruction
        target = LiveInstruction(zone_name='游戏区', duration_seconds=3600,
                                 task_id=env.task_id('游戏区'),
                                 execution_date=date.today().isoformat())
        # 第一次开播被 60024 人脸验证拦截
        def face_blocked(room_id, area_id, csrf):
            controller._set_pending_face_verify(
                'https://www.bilibili.com/synthetic-verify',
                run_id=controller._pending_run_id, epoch=controller._control_epoch)
            return False, {'code': 60024, 'msg': '人脸验证'}

        controller.api.start_live = face_blocked
        ok_start = controller.start_streaming(
            target, '', is_task_mode=True, source='new',
            handover_confirmed=True, expected_handover_id=handover_id)
        assert ok_start is True
        env.join_worker()
        assert controller._pending_face_verify is True

        # 验证意图冻结了本次交接身份
        intent = controller.face_verify_intent()
        assert intent is not None and intent.get('handover_id') == handover_id, \
            '验证意图必须冻结原交接身份'

        # 平台开房成功(重试),媒体推进接管
        controller.api.start_live = (
            lambda room_id, area_id, csrf: (True, {'code': 0, 'data': {
                'rtmp': {'addr': 'rtmp://x', 'code': 'k'}}}))
        confirmed = controller.confirm_face_verify(
            run_id=controller._pending_run_id,
            room_id=controller.current_room_id,
            epoch=controller._control_epoch)
        assert confirmed is True
        ok_retry = controller.start_streaming(
            target, '', is_task_mode=True, source='new',
            handover_confirmed=True, expected_handover_id=handover_id)
        assert ok_retry is True, '确认后的重试必须按原交接身份受理'
        env.join_worker()

        _arm_media(controller, seconds=100.0)
        controller._effective_clock.reset(confirmed=0, origin='live')
        controller._segment_monotonic = None
        controller._credit_effective_observation(0.0, 0.2)
        controller._pusher_media_reader.media_seconds = 105.0
        controller._credit_effective_observation(0.2, 5.2)

        assert controller.state.current_zone == '游戏区'
        record = env.read_handover()
        assert record['outcome'] == 'abandoned'
        assert record['handover_id'] == handover_id, '结论必须落在原交接记录上'
        assert env.email.count('stream_running') == 1
    finally:
        env.close()


def test_a08_late_confirm_after_new_intent_has_zero_side_effect(tmp_path):
    """A08:新意图接管后迟到的旧确认零副作用:不重试、不下播新场、不动交接。"""
    env = _env(tmp_path)
    try:
        env.seed_saved_session('学习区', seconds=600, run_id='run-old')
        claim = env.make_claim('学习区', seconds=600, run_id='run-old')
        ok, code, handover_id = _record_handover(env, claim)
        assert ok, code

        controller = env.controller
        from app.models.schemas import LiveInstruction
        target = LiveInstruction(zone_name='游戏区', duration_seconds=3600,
                                 task_id=env.task_id('游戏区'),
                                 execution_date=date.today().isoformat())
        def face_blocked(room_id, area_id, csrf):
            controller._set_pending_face_verify(
                'https://www.bilibili.com/synthetic-verify',
                run_id=controller._pending_run_id, epoch=controller._control_epoch)
            return False, {'code': 60043, 'msg': '人脸验证'}

        controller.api.start_live = face_blocked
        assert controller.start_streaming(
            target, '', is_task_mode=True, source='new',
            handover_confirmed=True, expected_handover_id=handover_id) is True
        env.join_worker()
        frozen = controller.face_verify_intent()
        assert frozen is not None

        # 用户放弃验证,直接发起另一意图(推进代际)
        controller.stop_streaming()
        env.controller.api.start_live = (
            lambda room_id, area_id, csrf: (True, {'code': 0, 'data': {
                'rtmp': {'addr': 'rtmp://x', 'code': 'k'}}}))
        other = LiveInstruction(zone_name='学习区', duration_seconds=1800,
                                task_id=env.task_id('学习区'),
                                execution_date=date.today().isoformat())
        assert controller.start_streaming(other, '', is_task_mode=True,
                                          source='new') is True
        env.join_worker()
        new_epoch = controller._control_epoch
        new_run = controller._current_run_id

        # 迟到确认:必须被拒,且对新会话零影响
        start_calls_before = len(controller.api.start_live_calls)
        confirmed = controller.confirm_face_verify(
            run_id=frozen.get('run_id'),
            room_id=controller.current_room_id,
            epoch=frozen.get('epoch'))
        assert confirmed is False, '旧确认不能接管新意图'
        assert len(controller.api.start_live_calls) == start_calls_before
        assert controller._control_epoch == new_epoch
        assert controller._current_run_id == new_run
        assert controller.is_streaming, '新会话不受迟到确认影响'
    finally:
        env.close()
