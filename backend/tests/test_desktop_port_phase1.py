"""桌面移植阶段 1 的直接事实（每个场景一个真实子进程）。

覆盖交办要求的直接证据：

1. 真实桌面默认 lifespan 装配新 owner，旧 ``LiveController`` 默认构造次数为 0；
2. 持有旧可恢复场次启动时平台 Start 为 0，显式恢复后身份/目标/检查点保持；
3. 真实 SQLite/JSON 迁移前后参数与已有记录一致；失败与归属冲突不发布、不建空目标；
4. 换号不串任务：旧 UID 写请求被拒、同 UID 第二次领取被原生锁拒绝、未收齐仍持锁；
5. 同令牌控制只有一份效果（在场景 2 内联）。
6. 桌面设置页的视频目录真正进入选片根（settings 是权威）。

平台侧用**本地 loopback HTTP 服务**（``port_support/loopback.py``，逐字节取自服务端
``tests/session_runtime/loopback.py``）：真实收请求、真实计次，不连任何真实端点。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent

sys.path.insert(0, str(TESTS_DIR))

from port_support import desktop_driver as driver              # noqa: E402
from port_support.loopback import LoopbackPlatform             # noqa: E402


# ==================== 驱动 ====================

def run_scenario(tmp_path: Path, scenario: str, params: dict,
                 timeout: float = 180.0) -> dict:
    """在**独立进程**里跑一个场景（两仓同用 app 包名，不能混导入）。"""
    params_file = tmp_path / f'params-{scenario}.json'
    params_file.write_text(json.dumps(params, ensure_ascii=False), encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        [str(BACKEND_DIR), env.get('PYTHONPATH', '')]).rstrip(os.pathsep)
    env['BILIBILI_SKIP_MIGRATION'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env.pop('BILIBILI_DATA_DIR', None)
    env.pop('ACCOUNT_DATA_ROOT', None)
    completed = subprocess.run(
        [sys.executable, str(TESTS_DIR / 'port_support' / 'desktop_driver.py'),
         scenario, str(params_file)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=env, timeout=timeout, cwd=str(BACKEND_DIR))
    lines = [line for line in (completed.stdout or '').splitlines() if line.strip()]
    if not lines:
        raise AssertionError(
            f'驱动没有输出（rc={completed.returncode}）\n'
            f'--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}')
    payload = json.loads(lines[-1])
    assert 'driver_error' not in payload, (
        f'驱动失败：{payload["driver_error"]}\n--- stdout ---\n{completed.stdout}\n'
        f'--- stderr ---\n{completed.stderr}')
    return payload


@pytest.fixture()
def platform():
    server = LoopbackPlatform().start()
    try:
        yield server
    finally:
        server.stop()


def _layout(tmp_path: Path):
    data_dir = tmp_path / 'data'
    account_root = tmp_path / 'account-data'
    videos = tmp_path / 'videos'
    for path in (data_dir, account_root, videos, videos / 'default'):
        path.mkdir(parents=True, exist_ok=True)
    return data_dir, account_root, videos


def _snapshot_bytes(data_dir: Path):
    return {name: (data_dir / name).read_bytes()
            for name in ('live_state.json', 'live_tasks.db')
            if (data_dir / name).is_file()}


# ==================== 1. 默认 lifespan 装配 ====================

def test_default_lifespan_wires_new_owner_without_old_controller(tmp_path, platform):
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))

    result = run_scenario(tmp_path, 'lifespan', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'endpoint_base': platform.base_url + '/',
        'shutdown_stop_live': False,
    })

    # 旧控制器一次都不许被构造；新链路确实起来了。
    assert result['counters']['live_controller_ctor'] == 0
    assert result['health']['live_controller'] is None
    assert result['health']['control_service'] is True
    assert result['account_mode'] is True
    assert result['account_root'] == str(account_root)

    # 桌面设置页的视频目录真正进入选片根（不是只有诊断接口能读到）。
    assert result['video_base'] == str(videos)

    # 没有旧资料、没有账号：启动本身不向平台发任何动作。
    assert result['counters_final']['platform_start'] == 0
    assert result['counters_final']['platform_stop'] == 0
    assert result['counters_final']['platform_query'] == 0
    assert platform.count('start') == 0 and platform.count('query') == 0

    # 「不停止并退出」：不请求平台下播，本地资源按统一关闭门收齐。
    assert result['shutdown']['status_code'] == 200, result['shutdown']
    assert result['shutdown']['body']['stop_live'] is False
    assert result['shutdown']['body']['stop'] is None
    assert result['counters_final']['platform_stop'] == 0


# ==================== 2. 旧可恢复场次：不自动开播 + 显式恢复 ====================

def test_legacy_resumable_session_starts_nothing_and_resumes_in_place(
        tmp_path, platform):
    data_dir, account_root, videos = _layout(tmp_path)
    zone, room, uid = '无限暖暖', 22467469, 91001
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_cookies(data_dir, uid=uid, room_id=room)
    driver.write_push_cache(data_dir, room_id=room)
    driver.write_area_cache(data_dir, [(zone, 1001)])
    driver.write_legacy_state(data_dir, room_id=room, zone=zone,
                              elapsed=5672, duration=7736,
                              run_id='legacy-run-1')
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': zone, 'category': 2, 'total_days': 5, 'days_done': 2,
         'deadline_raw': '2027-12-31', 'remaining_days': 3},
    ])
    (data_dir / 'stop_intent.json').write_text(
        json.dumps({'stopped_at': '2026-05-20T01:46:13', 'zone': zone,
                    'epoch': 7}, ensure_ascii=False), encoding='utf-8')
    before = _snapshot_bytes(data_dir)

    result = run_scenario(tmp_path, 'lifespan', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'endpoint_base': platform.base_url + '/',
        'check_foreign_write': 4242,
        'resume': True,
        'shutdown_stop_live': True,
    })

    assert result['counters']['live_controller_ctor'] == 0
    assert result['instance_errors'] == [], result['instance_errors']

    # 接管成功：候选发布到 accounts/<uid>/。
    assert result['runtime_takeover']['status'] == 'published', result['runtime_takeover']
    account_dir = account_root / 'accounts' / str(uid)
    assert account_dir.is_dir()

    # **启动本身零平台动作**：旧存档是可恢复的，桌面也不许自动开播。
    assert result['counters_after_start'] == {
        'live_controller_ctor': 0,
        'platform_start': 0, 'platform_stop': 0, 'platform_query': 0,
    }, result['counters_after_start']

    # 身份、固定目标与检查点全部保留；Goal 落在 stopped（供显式恢复）。
    snapshot = result['snapshot']
    assert snapshot['goal_wanted'] == 'stopped'
    assert snapshot['goal_stop_reason'] == 'user'
    assert snapshot['step'] == 'idle'
    assert snapshot['run_id'] == 'legacy-run-1'
    assert snapshot['run_zone'] == zone
    assert snapshot['run_room_id'] == room
    assert snapshot['run_uid'] == uid
    assert float(snapshot['run_target_seconds']) == 7736
    assert float(snapshot['run_checkpoint']) == 5672
    assert snapshot['run_end'] is None

    # 旧资料原封不动（原件保留用于恢复）。
    assert _snapshot_bytes(data_dir) == before

    # 换号隔离：旧 UID 的写请求被明确拒绝。
    assert result['foreign_write']['refused'] is True
    assert '4242' in result['foreign_write']['reason']

    # 显式恢复：只有这一步才允许真的去开平台；身份/目标/检查点仍然保持。
    assert result['resume']['accepted'] is True, result['resume']
    # 同令牌重放：回原回执，不产生第二份效果。
    assert result['resume_replay_same_status'] is True
    assert result['counters_after_resume']['platform_start'] == 1, (
        result['counters_after_resume'], platform.timeline())
    after = result['snapshot_after_resume']
    assert after['goal_wanted'] == 'running'
    assert after['run_id'] == 'legacy-run-1'
    assert after['run_zone'] == zone
    assert after['run_room_id'] == room
    assert float(after['run_target_seconds']) == 7736
    assert float(after['run_checkpoint']) == 5672
    assert after['run_end'] is None

    # 「停止并退出」：经同一 ControlService 发停止意图，并真的请求平台下播；
    # 受理不等于平台已关房 —— 这里分别核对两件事。
    assert result['shutdown']['body']['stop_live'] is True
    assert result['shutdown']['body']['stop']['status'] == 'accepted', result['shutdown']
    assert platform.count('stop') >= 1, platform.timeline()
    assert platform.count('start') == 1, platform.timeline()

    # 迁移前后任务参数一致（真实 SQLite）。
    import sqlite3
    connection = sqlite3.connect(str(account_dir / 'live_tasks.db'))
    try:
        row = connection.execute(
            'SELECT zone_name, category, total_days, days_done, deadline_raw'
            ' FROM tasks WHERE zone_name = ?', (zone,)).fetchone()
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert row == (zone, 2, 5, 2, '2027-12-31')
    assert {'task_completion_ledger', 'task_history'} <= tables


# ==================== 3. 归属冲突 / 读取失败：不发布、不建空目标 ====================

def test_legacy_material_from_another_room_is_never_reassigned(tmp_path):
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_cookies(data_dir, uid=91002, room_id=22222)
    driver.write_legacy_state(data_dir, room_id=11111, zone='旧账号分区',
                              elapsed=100, duration=3600, run_id='old-run')
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '旧账号分区', 'category': 1, 'total_days': 3, 'days_done': 1},
    ])
    before = _snapshot_bytes(data_dir)

    result = run_scenario(tmp_path, 'migration_only', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'uid': 91002, 'room_id': 22222,
    })

    # 旧资料写着别家的房间：确证不属于本次登录账号 → 不接管，也不建空目标。
    assert result['refusal'] is None
    assert result['takeover']['reason'] == 'room_mismatch', result['takeover']
    assert result['account_dir_exists'] is False
    assert result['db_exists'] is False
    assert _snapshot_bytes(data_dir) == before


def test_unreadable_legacy_database_blocks_activation(tmp_path):
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_cookies(data_dir, uid=91003, room_id=33333)
    driver.write_legacy_state(data_dir, room_id=33333, zone='坏库分区',
                              elapsed=100, duration=3600, run_id='bad-run')
    # 读取失败（不是"没有旧资料"）：主库存在但不可读。
    (data_dir / 'live_tasks.db').write_bytes(b'not a sqlite database at all')

    result = run_scenario(tmp_path, 'migration_only', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'uid': 91003, 'room_id': 33333,
    })

    # 拒绝激活：绝不留下一个会被当成"接管成功"的空目标。
    assert result['refusal'], result
    assert result['account_dir_exists'] is False
    assert result['db_exists'] is False
    assert (data_dir / 'live_tasks.db').read_bytes() == b'not a sqlite database at all'


# ==================== 4. 原生账号占用 ====================

def test_second_lease_for_same_uid_is_refused_natively(tmp_path):
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))

    result = run_scenario(tmp_path, 'lock', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'uid': 91004, 'room_id': 44444,
    })

    assert result['account_dir_exists'] is True
    assert len(result['held_after_open']) == 1, result['held_after_open']
    assert result['second_acquire']['refused'] is True, result['second_acquire']
    # 收齐即释放，且"报称已释放"与"注册表里确实没有"必须一致。
    assert result['close_report']['task_manager_shutdown'] is True
    assert result['close_report']['lock_released'] is True
    assert result['held_after_close'] == []
    assert result['lock_released_matches_report'] is True


# ==================== 5. 桌面停止意图与选片根的纯函数事实 ====================

def test_stop_intent_read_failure_is_not_treated_as_absent(tmp_path):
    from app.core.session_runtime import desktop

    assert desktop.read_stop_intent(tmp_path)['status'] == 'missing'

    (tmp_path / 'stop_intent.json').write_text(
        json.dumps({'stopped_at': 'x', 'zone': 'z', 'epoch': 3}),
        encoding='utf-8')
    good = desktop.read_stop_intent(tmp_path)
    assert good['status'] == 'ok'
    assert good['facts']['zone'] == 'z'

    (tmp_path / 'stop_intent.json').write_text('{ not json', encoding='utf-8')
    broken = desktop.read_stop_intent(tmp_path)
    assert broken['status'] == 'unreadable'
    assert broken['detail']

    # 转换只**产出内容**，绝不落到来源目录：读不出也必须按"存在停止意图"带上。
    assert '读不出' in desktop.stop_intent_note(broken)
    payload = json.loads(desktop.stop_marker_content(broken))
    assert payload['intent'] == 'unreadable'
    assert payload['source'] == 'desktop-restart-policy'
    assert not (tmp_path / '.no_auto_resume').exists()


def test_video_base_from_settings_ignores_blank_and_invalid():
    from app.core.session_runtime import desktop

    class _S:
        def __init__(self, value):
            self.video_path = value

    assert desktop.video_base_from_settings(_S('')) is None
    assert desktop.video_base_from_settings(_S('   ')) is None
    assert desktop.video_base_from_settings(_S(None)) is None
    assert desktop.video_base_from_settings(_S('/tmp/videos')) == Path('/tmp/videos')
