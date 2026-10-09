"""桌面移植阶段 1 集中返修的回归守卫（独立复核 R1–R4）。

四个冻结反例来自独立复核（``deliveries/desktop-port-phase1-review-f918cf-20261009``），
它们断言的是**出口事实**，不依赖第二阶段页面：

- R1 没有保存过场次的新账号必须能登录（``first_start`` ≠ 状态不可信）；
- R2 自动接管前必须先有**可信归属**：归属不明不发给任何账号（含第一个），
  确证属于本账号才接管、确证属于别人则保留原件照常新开；
- R3 平台未确证关房时不允许"停止并退出"放行进程退出；
- R4 拒绝迁移不能先改写来源目录的停止标记。

平台侧仍是**本地 loopback HTTP 服务**真实收请求、真实计次；不接真实 B 站 / RTMP /
SMTP / 账号。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from test_desktop_port_phase1 import (       # noqa: F401  (复用同一套夹具与驱动)
    _layout, platform, run_scenario,
)

from port_support import desktop_driver as driver           # noqa: E402
from port_support.loopback import Plan, LoopbackPlatform     # noqa: E402


def _always_refuse_stop(platform: LoopbackPlatform) -> None:
    """让本地平台的 ``stop`` 端点**始终**拒绝且不关房（复现"平台仍在播"）。

    覆盖 ``take_plan`` 而不是排固定条数的计划：排 N 条会在用尽后悄悄退回默认
    "关房"，那是一次夹具前提错误，不能用来指控产品。
    """
    original = platform.take_plan
    platform.take_plan = lambda endpoint: (                # type: ignore[assignment]
        Plan(code=10001, side='none') if endpoint == 'stop' else original(endpoint))


# ==================== R1：新账号首次登录 ====================

def test_fresh_account_first_login_activates_without_auto_start(tmp_path):
    """没有旧资料、没有状态档的正常新账号：激活成功，且启动零平台动作。"""
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))

    result = run_scenario(tmp_path, 'login_session', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'logins': [{'uid': 92001, 'room_id': 56001}],
    })

    step = result['steps'][0]
    assert step['receipt']['accepted'] is True, step
    assert step['installed'] is True, step
    assert step['instance_errors'] == [], step['instance_errors']
    # 正常首次启动：没有旧资料可接管，也**不**自动开播。
    assert step['takeover'] == {}, step['takeover']
    assert step['counters']['platform_start'] == 0, step['counters']
    assert step['counters']['platform_stop'] == 0, step['counters']
    assert step['counters']['platform_query'] == 0, step['counters']
    # 新账号拿到自己的空库。
    assert step['task_rows'] == []
    assert (account_root / 'accounts' / '92001' / 'live_tasks.db').is_file()


def test_legacy_tasks_without_session_state_and_with_owner_registration(tmp_path):
    """只有旧任务库、没有场次档，但库**已登记本账号**：按既有归属依据接管。"""
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ], owner_uid=92002)

    result = run_scenario(tmp_path, 'login_session', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'logins': [{'uid': 92002, 'room_id': 56002}],
    })

    step = result['steps'][0]
    assert step['receipt']['accepted'] is True, step
    assert step['installed'] is True, step
    assert step['instance_errors'] == [], step['instance_errors']
    assert step['takeover']['status'] == 'published', step['takeover']
    assert len(step['task_rows']) == 1, step['task_rows']


def test_unattributed_legacy_family_reaches_no_account_at_all(tmp_path):
    """归属**不明**的旧任务库（没有登记、没有场次身份）：第一个候选也不接管。

    "禁止第二个人领取"不能证明"第一个人有权领取"：本次扫码 UID 不是历史权威，
    因此既不发布、也不建空库把归属问题藏起来。
    """
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])

    result = run_scenario(tmp_path, 'login_session', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'logins': [{'uid': 91002, 'room_id': 55002}],
    })

    step = result['steps'][0]
    assert step['receipt']['accepted'] is False, step
    assert step['installed'] is False, step
    assert step['takeover']['reason'] == 'ownership_unknown', step['takeover']
    # 既没有发布账号目录，也没有留下一个会被当成接管成功的空库。
    assert step['task_rows'] == [], step['task_rows']
    assert not (account_root / 'accounts' / '91002').exists(), step
    # 原件保留：来源库仍躺在数据目录里。
    assert (data_dir / 'live_tasks.db').is_file()


# ==================== R2：一份旧资料不会被自动再发给别的账号 ====================

def test_legacy_family_with_trusted_ownership_is_never_reassigned(tmp_path):
    """换号守卫：给 A 可信历史归属后 A 接管，B 不继承那份旧任务族。"""
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    # A 的可信归属依据：旧场次档记录的房间与 A 登录的房间一致。
    driver.write_legacy_state(data_dir, room_id=55001, zone='隔离分区',
                              elapsed=1234, duration=5000, run_id='legacy-A')
    driver.write_area_cache(data_dir, [('隔离分区', 1001)])
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])

    result = run_scenario(tmp_path, 'login_session', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'logins': [
            {'uid': 91001, 'room_id': 55001},
            {'action': 'logout'},
            {'uid': 91002, 'room_id': 55002},
        ],
    })

    first, _, second = result['steps']
    assert first['receipt']['accepted'] is True, first
    assert first['takeover']['status'] == 'published', first['takeover']
    assert len(first['task_rows']) == 1, first['task_rows']

    # B：旧资料**确证**记着 A 的房间 → 原件保留、本次按新账号继续、不继承。
    assert second['receipt']['accepted'] is True, second
    assert second['takeover']['reason'] == 'room_mismatch', second['takeover']
    # B 拿到的是自己的空库；旧任务族仍只在 A 那里。
    assert second['task_rows'] == [], second['task_rows']
    with sqlite3.connect(str(account_root / 'accounts' / '91001' / 'live_tasks.db')) as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    # 原件保留：来源库仍在数据目录里，没有被搬走或改写。
    assert (data_dir / 'live_tasks.db').is_file()


def test_migration_entry_refuses_unattributed_family_for_every_candidate(tmp_path):
    """直接验证迁移入口：归属不明的旧资料对**任何**候选 UID 都不产生迁移授权。

    两次尝试分别换 UID 与房间号：结论都必须是"归属问题未解决"，来源不进任何账号
    目录，也没有空目标被建出来。
    """
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])

    result = run_scenario(tmp_path, 'migration_only', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'attempts': [{'uid': 91001, 'room_id': 55001},
                     {'uid': 91002, 'room_id': 55002}],
    })

    for attempt in result['attempts']:
        assert attempt['refusal'], attempt
        assert '归属不明' in attempt['refusal'], attempt['refusal']
        assert attempt['takeover']['reason'] == 'ownership_unknown', attempt['takeover']
        assert attempt['account_dir_exists'] is False, attempt
        assert attempt['task_rows'] == [], attempt['task_rows']
    # 原件保留：来源库仍躺在数据目录里，没有被搬走。
    assert (data_dir / 'live_tasks.db').is_file()


# ==================== R3：退出提交边界 ====================

def test_stop_and_exit_requires_confirmed_platform_close(tmp_path, platform):
    """平台持续拒绝关房：503 + 不放行退出 + 房间确实仍开着。"""
    data_dir, account_root, videos = _layout(tmp_path)
    zone, room, uid = '隔离分区', 55001, 91001
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_legacy_cookies(data_dir, uid=uid, room_id=room)
    driver.write_push_cache(data_dir, room_id=room)
    driver.write_area_cache(data_dir, [(zone, 1001)])
    driver.write_legacy_state(data_dir, room_id=room, zone=zone,
                              elapsed=1234, duration=5000, run_id='legacy-A')
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': zone, 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])
    _always_refuse_stop(platform)

    result = run_scenario(tmp_path, 'lifespan', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'endpoint_base': platform.base_url + '/',
        'resume': True,
        'shutdown_stop_live': True,
    })

    assert result['resume']['accepted'] is True, result['resume']
    assert platform.room_open(room), platform.timeline()

    # 受理只表示 Goal 落盘；平台没关房就不许算"停止并退出"完成。
    assert result['shutdown']['status_code'] == 503, result['shutdown']
    assert result['shutdown']['body']['code'] == 'SHUTDOWN_NOT_CONFIRMED'
    assert result['shutdown']['body']['ok'] is False
    assert result['shutdown']['body']['stop']['accepted'] is True
    assert result['shutdown']['body']['stop']['settled'] is False
    # 不放行进程退出；平台仍然开着（未被伪报为已关房）。
    assert result['server_should_exit'] is False
    assert platform.room_open(room), platform.timeline()
    assert platform.count('stop') >= 1, platform.timeline()


# ==================== R4：拒绝迁移不改写原件 ====================

def test_rejected_takeover_preserves_every_source_byte(tmp_path):
    """房间不符被拒：来源目录（含它自己的停止标记）一个字节都不动。"""
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_area_cache(data_dir, [('隔离分区', 1001)])
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])
    driver.write_legacy_state(data_dir, room_id=55001, zone='隔离分区',
                              elapsed=1234, duration=5000, run_id='legacy-A')
    marker = data_dir / '.no_auto_resume'
    marker.write_bytes(b'original-user-stop-marker\n')

    result = run_scenario(tmp_path, 'migration_only', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'uid': 91002, 'room_id': 55002,
    })

    assert result['takeover']['reason'] == 'room_mismatch', result['takeover']
    assert result['refusal'] is None, result
    assert result['account_dir_exists'] is False
    # 全目录逐字节一致（不只是主档）：原来的停止标记必须原样保留。
    assert result['source_unchanged'] is True, (
        result['source_changed'], result['source_added'])
    assert marker.read_bytes() == b'original-user-stop-marker\n'


def test_published_takeover_lands_stop_policy_in_the_target_not_the_source(tmp_path):
    """迁移**成功**时：停止政策落在账号目录里，来源目录同样不变。"""
    data_dir, account_root, videos = _layout(tmp_path)
    driver.write_settings(data_dir, video_path=str(videos))
    driver.write_area_cache(data_dir, [('隔离分区', 1001)])
    driver.write_legacy_database(data_dir, tasks=[
        {'zone_name': '隔离分区', 'category': 2, 'total_days': 7, 'days_done': 3,
         'deadline_raw': '2027-12-31'},
    ])
    driver.write_legacy_state(data_dir, room_id=55001, zone='隔离分区',
                              elapsed=1234, duration=5000, run_id='legacy-A')
    (data_dir / '.no_auto_resume').write_bytes(b'original-user-stop-marker\n')

    result = run_scenario(tmp_path, 'migration_only', {
        'data_dir': str(data_dir),
        'account_root': str(account_root),
        'uid': 91001, 'room_id': 55001,
    })

    assert result['takeover']['status'] == 'published', result['takeover']
    assert result['source_unchanged'] is True, (
        result['source_changed'], result['source_added'])
    account_dir = account_root / 'accounts' / '91001'
    # 来源自己的标记按**原件**随候选发布，内容不被产品规则覆盖。
    assert (account_dir / '.no_auto_resume').read_bytes() == b'original-user-stop-marker\n'
    # 迁移出的快照本身就是 stopped：重开只提示恢复，不自动开播。
    document = json.loads((account_dir / 'live_state.json').read_text(encoding='utf-8'))
    assert document['goal']['wanted'] == 'stopped'
    assert document['goal']['stop_reason'] == 'user'
    assert document['run']['id'] == 'legacy-A'
    assert float(document['run']['target_seconds']) == 5000
    assert float(document['run']['clock']['checkpoint_seconds']) == 1234
