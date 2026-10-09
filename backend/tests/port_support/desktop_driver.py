"""桌面移植阶段 1 的窄场景驱动：**一个场景一个进程**。

为什么必须分进程：两个仓库同用 ``app`` 包名（不能在同一解释器里混导入），而且
``config.init`` 与 ``production.configure_boundaries`` 都是进程级一次性状态 ——
同一个进程里构造不出第二份"真实默认组装"。

调用方式（由 ``test_desktop_port_phase1.py`` 用 ``sys.executable`` 拉起）：

    python -m port_support.desktop_driver <scenario> <params.json>

输出：**最后一行**是 JSON 结果；其余行是后端日志（原样透传，便于失败时人工核对）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 面板崩溃/统计用的旧桌面 live_state.json 字段形状（1.1.x 实际写出的键）。
LEGACY_PHASE_LIVE = 'live'


# ==================== 旧资料夹具 ====================

def write_legacy_state(data_dir: Path, *, room_id: int, zone: str,
                       elapsed: float, duration: int,
                       run_id: str = '', streaming: bool = True,
                       business_day: Optional[str] = None,
                       phase: str = LEGACY_PHASE_LIVE) -> Dict[str, Any]:
    """写一份桌面旧版 ``live_state.json``（字段与旧 ``LiveState.to_dict()`` 同形）。"""
    day = business_day or datetime.now().date().isoformat()
    document = {
        'is_streaming': bool(streaming),
        'current_zone': zone,
        'effective_seconds': float(elapsed),
        'elapsed_seconds': int(elapsed),
        'accumulated_seconds': int(elapsed),
        'room_id': int(room_id),
        'run_id': run_id,
        'task_id': None,
        'execution_date': day,
        'duration_seconds': int(duration),
        'duration_known': True,
        'phase': phase if streaming else 'idle',
        'source_mode': 'resume',
        'start_time': f'{day}T01:02:44.521678',
        'last_update': f'{day}T01:46:13.840196',
        'auto_resume': True,
        'resume_blocked_reason': '',
    }
    (Path(data_dir) / 'live_state.json').write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return document


def write_legacy_cookies(data_dir: Path, *, uid: int, room_id: int,
                         sessdata: str = 'isolated-sessdata',
                         csrf: str = 'isolated-jct') -> None:
    document = {
        'cookies': {
            'SESSDATA': sessdata,
            'bili_jct': csrf,
            'DedeUserID': str(uid),
            'room_id': str(room_id),
            'uid': str(uid),
        },
        'logout_tokens': {},
        'last_update': datetime.now().isoformat(),
    }
    (Path(data_dir) / 'bili_cookies.json').write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')


#: 桌面旧版 ``live_tasks.db`` 的表形状（只有 ``tasks`` 与 ``tasks_meta``）。
LEGACY_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
id INTEGER PRIMARY KEY AUTOINCREMENT,
zone_name TEXT NOT NULL UNIQUE,
category INTEGER NOT NULL DEFAULT 1,
total_days INTEGER NOT NULL DEFAULT 1,
days_done INTEGER NOT NULL DEFAULT 0,
deadline_raw TEXT NOT NULL DEFAULT '',
today_done INTEGER DEFAULT NULL,
remaining_days INTEGER NOT NULL DEFAULT 1,
a_val INTEGER NOT NULL DEFAULT 9999,
i_val INTEGER NOT NULL DEFAULT 0,
j_val INTEGER NOT NULL DEFAULT -1,
created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def write_legacy_database(data_dir: Path, *, tasks, owner_uid=None) -> Path:
    """写一份**桌面旧形状**的任务库（不含账本/历史表，属于待迁移对象）。

    ``owner_uid`` 给出时同时写进 ``tasks_meta.account_owner_uid`` —— 那是"这份库
    登记属于谁"的既有归属依据；不给出时库是**未标注**的（归属线索只能来自场次档，
    或者根本没有线索）。
    """
    path = Path(data_dir) / 'live_tasks.db'
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(LEGACY_TASKS_SCHEMA)
        connection.execute(
            'CREATE TABLE IF NOT EXISTS tasks_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if owner_uid is not None:
            connection.execute(
                'INSERT OR REPLACE INTO tasks_meta (key, value) VALUES (?, ?)',
                ('account_owner_uid', str(int(owner_uid))))
        for task in tasks:
            connection.execute(
                'INSERT INTO tasks (zone_name, category, total_days, days_done,'
                ' deadline_raw, remaining_days, a_val) VALUES (?,?,?,?,?,?,?)',
                (task['zone_name'], task.get('category', 1),
                 task.get('total_days', 5), task.get('days_done', 0),
                 task.get('deadline_raw', ''), task.get('remaining_days', 5),
                 task.get('a_val', 9999)))
        connection.commit()
    finally:
        connection.close()
    return path


def write_area_cache(data_dir: Path, zones) -> None:
    """写一份**合成分区缓存**（数据目录内的运行时缓存，不是联网拉取）。

    有了它，旧场的 ``zone_name`` 才能被既有 ``_bind_original_area`` 解析出真实
    ``area_id``；没有它时迁移出的原场没有分区 ID，平台观察会按 ordinary unknown
    处理（这是既有规则，不是缺陷）。
    """
    nodes = [{'id': 1, 'name': '合成大区', 'parent_id': 0, 'parent_name': '合成大区',
              'children': [{'id': int(area_id), 'name': name, 'parent_id': 1,
                            'parent_name': '合成大区', 'children': []}
                           for name, area_id in dict(zones).items()]}]
    (Path(data_dir) / 'bili_areas_full.json').write_text(
        json.dumps(nodes, ensure_ascii=False, indent=2), encoding='utf-8')


def write_push_cache(data_dir: Path, *, room_id: int,
                     addr: str = 'rtmp://127.0.0.1/isolated',
                     code: str = '?streamkey=isolated') -> None:
    """预置本实例的推流地址缓存：证明"取址"这一步不打真实端点。

    它不是业务事实的替身，只是既有缓存文件（生产同样先读它，未命中才联网）。
    """
    (Path(data_dir) / 'rtmp_cache.json').write_text(
        json.dumps({str(int(room_id)): {'rtmp_addr': addr, 'rtmp_code': code}},
                   ensure_ascii=False, indent=2), encoding='utf-8')


def write_settings(data_dir: Path, **overrides) -> None:
    payload = {
        'video_path': '', 'stream_mode': 'ffmpeg',
        'email_enabled': False, 'notification_enabled': False,
        'email_daily_summary': False, 'auto_open_video': False,
    }
    payload.update(overrides)
    (Path(data_dir) / 'settings.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


# ==================== 计数器（只在驱动进程内生效） ====================

def install_counters() -> Dict[str, int]:
    """给旧控制器构造与新控制链的平台出口装**计数探针**。

    - 旧 ``LiveController``：默认入口**一次都不许**构造它；
    - ``BilibiliApi`` 的三个"一轮"出口：由 ``PlatformHttpPort`` 唯一调用，计数就是
      "本进程向平台发了多少次真实动作"。真实 HTTP 仍会打到注入的 loopback 上。
    """
    import app.core.live_controller as module

    counters = {'live_controller_ctor': 0,
                'platform_start': 0, 'platform_stop': 0, 'platform_query': 0}

    def counting_init(self, *args, **kwargs):                # noqa: ANN001
        counters['live_controller_ctor'] += 1
        raise AssertionError('桌面默认入口不得构造旧 LiveController')

    module.LiveController.__init__ = counting_init

    probes = (('start_live_once_with_budget', 'platform_start'),
              ('stop_live_once_with_budget', 'platform_stop'),
              ('get_info_once_with_budget', 'platform_query'))
    for name, key in probes:
        original = getattr(module.BilibiliApi, name, None)
        if original is None:
            raise AssertionError(f'BilibiliApi 缺少 {name}：共享链的接缝已变化')

        def make(original, key):                             # noqa: ANN001
            def wrapped(self, *args, **kwargs):
                counters[key] += 1
                return original(self, *args, **kwargs)
            return wrapped

        setattr(module.BilibiliApi, name, make(original, key))
    return counters


# ==================== 场景 ====================

def _snapshot_summary(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    goal = snapshot.get('goal') or {}
    run = snapshot.get('run') or {}
    clock = run.get('clock') or {}
    return {
        'goal_wanted': goal.get('wanted'),
        'goal_revision': goal.get('revision'),
        'goal_mode': goal.get('mode'),
        'goal_start_kind': goal.get('start_kind'),
        'goal_stop_reason': goal.get('stop_reason'),
        'step': snapshot.get('step'),
        'run_id': run.get('id'),
        'run_uid': run.get('uid'),
        'run_room_id': run.get('room_id'),
        'run_zone': run.get('zone_name'),
        'run_business_day': run.get('business_day'),
        'run_target_seconds': run.get('target_seconds'),
        'run_checkpoint': clock.get('checkpoint_seconds'),
        'run_end': run.get('end'),
        'run_area_id': run.get('area_id'),
    }


def _run_lifespan(params: Dict[str, Any]) -> Dict[str, Any]:
    """真实默认 lifespan（``app.main`` 的 ASGI 应用）跑一遍并收集事实。"""
    from app.core import config
    from app.core.session_runtime import production

    config.init(data_dir=params['data_dir'], account_root=params['account_root'])
    if params.get('endpoint_base'):
        production.configure_boundaries(endpoint_base=params['endpoint_base'])

    counters = install_counters()

    from app.main import app
    from app.dependencies import (
        get_application_owner, get_control_service, capture_account_work,
    )
    from starlette.testclient import TestClient

    out: Dict[str, Any] = {'counters': counters}
    with TestClient(app) as client:
        owner = get_application_owner()
        control = get_control_service()
        out['health'] = client.get('/api/health').json()
        out['account_mode'] = bool(getattr(owner, 'account_mode', False))
        out['account_root'] = str(owner.account_root or '')
        out['takeover'] = owner.takeover.to_dict()
        out['video_base'] = str(getattr(owner.boundaries, 'video_base', '') or '')
        out['instance_errors'] = list(owner.instance_errors)
        out['runtime_takeover'] = None
        try:
            from app.core.session_runtime import desktop
            out['runtime_takeover'] = desktop.current_takeover_facts()
        except Exception:                                    # noqa: BLE001
            pass
        snapshot = control.read() if control is not None else None
        out['snapshot'] = _snapshot_summary(snapshot)
        out['counters_after_start'] = dict(counters)

        if params.get('check_foreign_write') is not None:
            from app.core.session_runtime.accounts import AccountContextExpired
            try:
                capture_account_work(params['check_foreign_write'])
                out['foreign_write'] = {'refused': False}
            except AccountContextExpired as exc:
                out['foreign_write'] = {'refused': True, 'reason': str(exc)}

        if params.get('resume'):
            run = (snapshot or {}).get('run') or {}
            goal = (snapshot or {}).get('goal') or {}
            receipt = control.resume(
                'desktop-resume-token-1',
                run_id=run.get('id'), goal_revision=goal.get('revision'))
            out['resume'] = receipt.to_dict()
            # 同令牌重放：必须回原回执，不得再产生一份效果。
            replay = control.resume(
                'desktop-resume-token-1',
                run_id=run.get('id'), goal_revision=goal.get('revision'))
            out['resume_replay_same_status'] = (
                replay.status == receipt.status
                and replay.projection == receipt.projection)
            out['snapshot_after_resume'] = _snapshot_summary(control.read())
            # 受理只表示 Goal 落盘；真正的平台开播由 actor 异步推进。这里**有界**
            # 等一小会儿，好让"恢复确实会去开平台"成为可观察的事实而不是时序巧合。
            deadline = time.monotonic() + float(params.get('settle_seconds', 8.0))
            while counters['platform_start'] == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            out['counters_after_resume'] = dict(counters)

        if params.get('shutdown_stop_live') is not None:
            # 退出提交边界要能观察到"是否放行进程退出"：uvicorn 句柄在真实进程里由
            # run.py 注入，这里放一个只读的替身，读它的 should_exit。
            import types
            app.state.uvicorn_server = types.SimpleNamespace(should_exit=False)
            response = client.post(
                f"/api/shutdown?stop_live={str(bool(params['shutdown_stop_live'])).lower()}")
            try:
                out['shutdown'] = {'status_code': response.status_code,
                                   'body': response.json()}
            except Exception:                                # noqa: BLE001
                out['shutdown'] = {'status_code': response.status_code,
                                   'body': response.text[:400]}
            out['server_should_exit'] = bool(
                app.state.uvicorn_server.should_exit)
            out['counters_after_shutdown'] = dict(counters)
    out['counters_final'] = dict(counters)
    return out


def _run_lock(params: Dict[str, Any]) -> Dict[str, Any]:
    """原生账号占用：同一 UID 的第二次领取必须在开库/开播前被拒。"""
    from app.core import config
    from app.core.session_runtime import production

    config.init(data_dir=params['data_dir'], account_root=params['account_root'])
    if params.get('endpoint_base'):
        production.configure_boundaries(endpoint_base=params['endpoint_base'])

    from app.core.session import AccountContext
    from app.core.session_runtime import accounts
    from app.core.session_runtime.production import build_application_owner

    owner = build_application_owner(data_dir=Path(params['data_dir']))
    account = AccountContext(uid=int(params['uid']), room_id=int(params['room_id']),
                             zone_name=params.get('zone') or '隔离分区',
                             area_id=int(params.get('area_id') or 100))
    out: Dict[str, Any] = {'account_root': str(owner.account_root)}
    binding = owner.open_account_binding(account)
    out['held_after_open'] = sorted(accounts.held_leases())
    try:
        accounts.acquire_account_lease(owner.account_root, account.uid)
        out['second_acquire'] = {'refused': False}
    except accounts.AccountBusy as exc:
        out['second_acquire'] = {'refused': True, 'reason': str(exc)}
    out['account_dir_exists'] = accounts.account_paths(
        owner.account_root, account.uid).account_dir.exists()

    report = binding.close(float(params.get('close_budget', 5.0)))
    info = report if isinstance(report, dict) else {}
    out['close_report'] = info
    out['held_after_close'] = sorted(accounts.held_leases())
    # 收齐才算解锁；未收齐必须**仍然**持有（这里如实回报，不替产品下结论）。
    out['lock_released_matches_report'] = (
        bool(info.get('lock_released')) == (not out['held_after_close']))
    return out


def _run_migration_only(params: Dict[str, Any]) -> Dict[str, Any]:
    """只跑既有显式迁移工具：证明拒绝路径不发布、不建空目标。

    单次（``uid`` / ``room_id``）与多次（``attempts``）都支持：多次用来证明**同一份
    保留的来源**不会被第二次发布给另一个账号。
    """
    from app.core import config
    from app.core.session_runtime import production

    config.init(data_dir=params['data_dir'], account_root=params['account_root'])
    if params.get('endpoint_base'):
        production.configure_boundaries(endpoint_base=params['endpoint_base'])

    from app.core.session_runtime import desktop
    from app.core.session import AccountContext

    data_dir = Path(params['data_dir'])
    account_root = Path(params['account_root'])
    attempts = params.get('attempts') or [
        {'uid': params['uid'], 'room_id': params['room_id']}]

    out: Dict[str, Any] = {'attempts': []}
    # 来源快照取自**本进程**：``config.init`` 会在数据目录里落下可写探测文件，那
    # 是初始化的一部分，不是迁移改了原件；由本进程前后各取一次才对得上。
    out['source_before'] = _tree_bytes(data_dir)
    for attempt in attempts:
        uid = int(attempt['uid'])
        account = AccountContext(uid=uid, room_id=int(attempt['room_id']))
        refusal = desktop.prepare_account_target(data_dir, account_root, account)
        from app.core.session_runtime.accounts import account_paths
        paths = account_paths(account_root, uid)
        out['attempts'].append({
            'uid': uid,
            'refusal': refusal,
            'account_dir_exists': paths.account_dir.exists(),
            'db_exists': paths.db_path.exists(),
            'takeover': desktop.current_takeover_facts(),
            'task_rows': _account_task_rows(account_root, uid),
        })
    # 单次调用沿用旧字段名（既有用例按这些名字读）。
    last = out['attempts'][-1]
    out.update({'refusal': last['refusal'],
                'account_dir_exists': last['account_dir_exists'],
                'db_exists': last['db_exists'],
                'takeover': last['takeover']})
    out['source_after'] = _tree_bytes(data_dir)
    out['source_unchanged'] = out['source_after'] == out['source_before']
    out['source_changed'] = sorted(
        name for name in out['source_before']
        if out['source_before'][name] != out['source_after'].get(name))
    out['source_added'] = sorted(set(out['source_after']) - set(out['source_before']))
    return out


def _tree_bytes(directory: Path) -> Dict[str, str]:
    """目录下每个文件的相对路径 → sha256（"一个字节都不动"的完整判据）。"""
    import hashlib
    return {
        path.relative_to(directory).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(directory).rglob('*')) if path.is_file()
    }


def _account_task_rows(account_root: Path, uid: int) -> List[list]:
    """账号库里 ``tasks`` 表的事实行（只读打开；库不存在就是空列表）。"""
    path = Path(account_root) / 'accounts' / str(int(uid)) / 'live_tasks.db'
    if not path.is_file():
        return []
    connection = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    try:
        return [list(row) for row in connection.execute(
            'SELECT id, zone_name, category, total_days, days_done FROM tasks')]
    finally:
        connection.close()


def _run_login_session(params: Dict[str, Any]) -> Dict[str, Any]:
    """真实默认 owner 的登录/换号序列：每个账号的激活结论、接管事实与任务行。

    ``params['logins']`` 是一串 ``{'uid', 'room_id', 'action'}``：``action`` 为
    ``login``（默认）或 ``logout``。走的是**真实** ``ControlService`` 登录/登出
    出口（与桌面扫码登录同一条职责），因此"首次登录被拒"这类出口问题在这里是
    可直接观察的事实，不是推断。
    """
    from app.core import config
    from app.core.session_runtime import production

    config.init(data_dir=params['data_dir'], account_root=params['account_root'])
    if params.get('endpoint_base'):
        production.configure_boundaries(endpoint_base=params['endpoint_base'])

    counters = install_counters()

    from app.core.session import AccountContext
    from app.core.session_runtime import desktop
    from app.core.session_runtime.desktop import build_desktop_owner

    account_root = Path(params['account_root'])
    owner = build_desktop_owner(data_dir=Path(params['data_dir']))
    out: Dict[str, Any] = {'account_root': str(owner.account_root),
                           'counters': counters, 'steps': []}
    try:
        for index, step in enumerate(params['logins']):
            action = str(step.get('action') or 'login')
            if action == 'logout':
                receipt = owner.control.logout(
                    f'desktop-logout-{index}',
                    timeout=float(params.get('logout_timeout', 5.0)))
                out['steps'].append({'action': 'logout',
                                     'receipt': receipt.to_dict()})
                continue
            uid = int(step['uid'])
            account = AccountContext(
                uid=uid, room_id=int(step.get('room_id') or 0),
                zone_name=params.get('zone') or '隔离分区',
                area_id=int(params.get('area_id') or 100))
            receipt = owner.control.login(
                f'desktop-login-{index}', account,
                dict(step.get('credentials') or {
                    'SESSDATA': 'synthetic', 'bili_jct': 'synthetic',
                    'DedeUserID': str(uid), 'uid': str(uid)}))
            out['steps'].append({
                'action': 'login', 'uid': uid,
                'receipt': receipt.to_dict(),
                'instance_errors': list(owner.instance_errors),
                'installed': owner.control.instance is not None,
                'takeover': desktop.current_takeover_facts(),
                'task_rows': _account_task_rows(account_root, uid),
                'counters': dict(counters),
            })
    finally:
        out['close'] = owner.close()
    out['counters_final'] = dict(counters)
    return out


SCENARIOS = {
    'lifespan': _run_lifespan,
    'lock': _run_lock,
    'migration_only': _run_migration_only,
    'login_session': _run_login_session,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=sorted(SCENARIOS))
    parser.add_argument('params')
    args = parser.parse_args(argv)
    params = json.loads(Path(args.params).read_text(encoding='utf-8'))
    try:
        result = SCENARIOS[args.scenario](params)
    except BaseException as exc:                             # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        print(json.dumps({'driver_error': f'{type(exc).__name__}: {exc}'},
                         ensure_ascii=False))
        return 1
    sys.stdout.flush()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
