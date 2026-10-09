"""M0A：**单账号显式迁移**（离线工具 / 窄内部入口）。

产品口径（交办 §7）：**只按明确的 ``来源目录 → UID`` 映射迁移**。当前 cookie 不能
证明整个旧库/历史归属，因此本工具**不猜**：

- 输入是明确 UID、明确来源目录、共享目标根（可选显式房间号证据）；
- 先**只读预检**，再在独立候选目录组装/验证，**最后一步才发布**；
- 已有可信账号目录**不覆盖**；同一份迁移重跑核对既有结果并返回"已完成"，
  不是再导入一次；
- 归属不明 / 多来源 / 目标已有数据 → 明确拒绝并保留原字节。

"首次新账号为空"与"旧账号迁移未确认"是**两种不同情形**：有旧资料证据时绝不
静默走空库成功。

一致性副本机制（见 ``_staged_database`` / ``_consistent_copy``）：先把源库**整族**
（``live_tasks.db`` 与它的 ``-wal``/``-shm``）按**纯字节**复制到暂存目录，之后所有
SQLite 操作都在副本上做 —— 先 ``wal_checkpoint(TRUNCATE)`` 把已提交的 WAL 折回主
文件，再 ``VACUUM INTO`` 产出一份自洽的独立库。这样做有两层理由：

1. ``immutable=1`` 直接读主文件会**漏掉已提交但尚未 checkpoint 的 WAL 内容**，
   这里不会；
2. 任何 SQLite 连接（含 ``mode=ro``）在 WAL 模式下都会在**源目录**创建/触碰
   ``-shm``/``-wal``；先暂存因此保证源目录**一个字节都不变、也不多出文件**。

实测 WAL 情形与源目录文件集合不变见 ``tests/account/test_m0a_migration.py``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .accounts import (
    DB_FILE,
    STATE_FILE,
    AccountBusy,
    AccountDataError,
    AccountError,
    account_database_is_empty,
    account_paths,
    acquire_account_lease,
    claim_account_owner,
    inspect_account_database,
)

logger = logging.getLogger(__name__)

LAST_RUN_FILE = 'task_manager_last_run.json'
EXCEL_FILE = 'live_tasks.xlsx'

#: 与账号恢复语义相关的兼容标记（旧目录里可能存在的"停止意图 / 人脸待验证 /
#: 进度移交"资料）。它们随账号资料一起搬，迁移不改其含义、不重新解释。
AUXILIARY_FILES = (
    STATE_FILE + '.bak',
    '.no_auto_resume',
    '.face_verify_pending',
    '.progress_handover.json',
)

#: 迁移回执键前缀：``account_migration:<来源指纹>``。
MIGRATION_RECEIPT_PREFIX = 'account_migration:'


class MigrationRefusedError(RuntimeError):
    """预检或组装阶段明确拒绝。``reason`` 是稳定判据码；原字节未改动。"""

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class MigrationRequest:
    """一次显式迁移的**唯一**输入。没有明确来源就不猜。"""

    uid: int
    source_dir: Path
    shared_root: Path
    #: 显式来源证据（可选，给定时逐项核对）：房间号 / 运行 id / goal revision。
    room_id: Optional[int] = None
    run_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {'uid': int(self.uid), 'source_dir': str(self.source_dir),
                'shared_root': str(self.shared_root),
                'room_id': self.room_id, 'run_id': self.run_id}


@dataclass
class MigrationReport:
    status: str                     # published / already_done / refused
    reason: str = ''
    detail: str = ''
    uid: Optional[int] = None
    target_dir: str = ''
    source_fingerprint: str = ''
    facts: Dict[str, Any] = field(default_factory=dict)
    steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {'status': self.status, 'reason': self.reason, 'detail': self.detail,
                'uid': self.uid, 'target_dir': self.target_dir,
                'source_fingerprint': self.source_fingerprint,
                'facts': dict(self.facts), 'steps': list(self.steps)}


# ==================== 来源证据 ====================

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
    except OSError:
        return ''
    return digest.hexdigest()


def source_fingerprint(source_dir: Path) -> str:
    """来源身份指纹：库主文件 + 未 checkpoint 的 WAL + 状态/日切件的内容摘要。

    它**不**声称"历史属于谁"，只回答"这次迁移的输入是不是上一次那一份"。
    """
    source_dir = Path(source_dir)
    parts = []
    for name in (DB_FILE, DB_FILE + '-wal', STATE_FILE, LAST_RUN_FILE):
        path = source_dir / name
        if path.is_file():
            parts.append({'name': name, 'size': path.stat().st_size,
                          'sha256': _sha256_file(path)})
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def read_source_evidence(source_dir: Path) -> Dict[str, Any]:
    """只读读取旧资料的**可核对事实**（不解释、不改写、不迁移）。

    所有 SQLite 读取都在**暂存副本**上做：源目录只被纯字节读取碰过，不会因此
    多出 ``-wal``/``-shm`` 之类的旁链文件。

    状态资料的"有没有 / 是谁的"取自**既有 SessionStore 的有效资料选择**，不是
    "主档文件在不在"：主档不可用时它会采用最后可用副本，因此"主档缺失但旁边有一份
    可信 ``.bak``"是**有**有效资料的。用文件存在性当判据会把那份有效资料当成不存在，
    于是把它照搬过去、最终把别人的 Run 发布到本账号目录（复核 R5）。
    """
    source_dir = Path(source_dir)
    db_path = source_dir / DB_FILE
    evidence: Dict[str, Any] = {
        'db_present': db_path.is_file(),
        'wal_present': (source_dir / (DB_FILE + '-wal')).is_file(),
    }
    if db_path.is_file():
        with _staged_database(db_path) as staged:
            info = inspect_account_database(staged)
            evidence['db_owner_state'] = info['state']
            evidence['db_owner_uid'] = info.get('owner_uid')
            evidence['db_has_facts'] = not account_database_is_empty(staged)
            evidence['db_counts'] = _table_counts(staged)
    identity = effective_state_identity(source_dir)
    evidence['state_present'] = bool(identity.get('present'))
    if identity.get('present'):
        evidence['state_status'] = identity.get('status')
        evidence['state_source'] = identity.get('source')
        if identity.get('uid'):
            evidence['state_run_uid'] = int(identity['uid'])
        if identity.get('room_id'):
            evidence['state_run_room'] = int(identity['room_id'])
        if identity.get('run_id'):
            evidence['state_run_id'] = str(identity['run_id'])
        if identity.get('legacy'):
            evidence['state_legacy'] = identity['legacy']
    return evidence


def effective_state_identity(directory: Path) -> Dict[str, Any]:
    """一个目录里**有效状态资料**的身份线索（只读，不改字节）。

    优先级与默认入口的 ``take_over_state`` **完全同源**，不另写一套"哪份算数"：

    1. 主档可信 → 用主档 Run 的 uid/房间/运行 id；
    2. 主档是**已识别的合法旧档** → 用旧档自己的房间号/运行 id（旧格式没有 UID），
       即使旁边放着一份可信的新格式 ``.bak`` 也以合法旧主档为准；
    3. 主档不可用 / 不是可转换旧档，但**最后可用副本可信** → 用副本 Run 的身份；
    4. 两份都没有（``first_start``）或都不可信 → ``present`` 为 False / 不强加身份。

    返回 ``{'present', 'status', 'source', 'uid', 'room_id', 'run_id', 'legacy'}``。
    """
    from ..session.store import VERDICT_TRUSTED, SessionStore

    state_path = Path(directory) / STATE_FILE
    loaded = SessionStore(state_path).load()
    identity: Dict[str, Any] = {
        'present': loaded.status != 'first_start',
        'status': loaded.status,
        'source': loaded.source,
        'uid': 0,
        'room_id': 0,
        'run_id': '',
    }
    if (loaded.status == VERDICT_TRUSTED and loaded.source != 'backup'
            and loaded.snapshot is not None):
        _merge_run_identity(identity, loaded.snapshot.run)
        return identity
    legacy = _legacy_state_identity(state_path)
    if legacy is not None:
        identity['legacy'] = legacy
        identity['room_id'] = int(legacy.get('room_id') or 0)
        identity['run_id'] = str(legacy.get('run_id') or '')
        return identity
    if loaded.status == VERDICT_TRUSTED and loaded.snapshot is not None:
        _merge_run_identity(identity, loaded.snapshot.run)
    return identity


def _merge_run_identity(identity: Dict[str, Any], run: Any) -> None:
    if run is None:
        return
    identity['uid'] = int(getattr(run, 'uid', 0) or 0)
    identity['room_id'] = int(getattr(run, 'room_id', 0) or 0)
    identity['run_id'] = str(getattr(run, 'id', '') or '')


def _legacy_state_identity(state_path: Path) -> Optional[Dict[str, Any]]:
    """旧格式 ``LiveState`` 里可核对的**运行身份**线索（只读原始 JSON）。

    旧格式没有 UID，但有 ``room_id`` 与 ``run_id``。它们只是"来源证据"：不构成
    归属权威（当前 cookie 同样不是），但**与显式映射矛盾时必须拒绝**，不能把一份
    明确写着别家房间的旧档当成"归属不明所以照搬"。
    """
    try:
        document = json.loads(Path(state_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    # 与默认入口**同一份**格式判定器：不在这里另写一套"看起来像旧档"的猜测。
    from ..session.migrate import detect_format
    if detect_format(document) != 'legacy_livestate':
        return None
    identity: Dict[str, Any] = {}
    room = document.get('room_id')
    run_id = document.get('run_id') or document.get('task_id')
    if isinstance(room, int) and not isinstance(room, bool):
        identity['room_id'] = room
    if isinstance(run_id, str) and run_id:
        identity['run_id'] = run_id
    return identity or None


def _table_counts(db_path: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    try:
        uri = f'file:{Path(db_path).as_posix()}?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.Error:
        return counts
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('tasks', 'task_history', 'task_completion_ledger'):
            if table in tables:
                counts[table] = int(connection.execute(
                    f'SELECT COUNT(*) FROM {table}').fetchone()[0])
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return counts


# ==================== 一致性副本（不碰源目录） ====================

@contextmanager
def _staged_database(source_db: Path):
    """把源库**整族**字节复制到仓库外暂存目录，并 yield 暂存后的主文件路径。

    为什么要先暂存而不是直接开源库：任何 SQLite 连接（包括 ``mode=ro``）在
    WAL 模式下都会在**源目录**里创建/触碰 ``-shm`` 与 ``-wal``。本工具因此只用
    纯字节读取碰源目录：``live_tasks.db`` 与它的 ``-wal``/``-shm`` 一起复制，
    之后所有 SQLite 操作都发生在暂存副本上。源目录因此不会多出任何文件。

    ``immutable=1`` 那条路被明确排除：它读不到已提交但尚未 checkpoint 的 WAL 内容。
    """
    source_db = Path(source_db)
    staging = Path(tempfile.mkdtemp(prefix='m0a-src-'))
    try:
        for suffix in ('', '-wal', '-shm'):
            part = Path(str(source_db) + suffix)
            if part.is_file():
                shutil.copy2(part, staging / (source_db.name + suffix))
        yield staging / source_db.name
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _consistent_copy(source_db: Path, target_db: Path) -> None:
    """取一份**自洽**的账号库：暂存整族 → checkpoint → ``VACUUM INTO``。

    暂存副本带着已提交的 WAL 内容；``wal_checkpoint(TRUNCATE)`` 把 WAL 折回主文件
    之后 ``VACUUM INTO`` 产出一份紧凑、无 WAL 旁链的独立库。产物不含 ``-wal``/
    ``-shm``，因此发布到账号目录的就是一份可直接打开的完整资料。
    """
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if target_db.exists():
        target_db.unlink()
    with _staged_database(source_db) as staged:
        if not staged.is_file():
            raise MigrationRefusedError('source_db_missing',
                                        f'来源没有任务库：{source_db}')
        connection = None
        try:
            connection = sqlite3.connect(str(staged))
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            connection.execute('VACUUM INTO ?', (str(target_db),))
            connection.commit()
        except sqlite3.Error as exc:
            raise MigrationRefusedError('source_db_unreadable',
                                        f'来源库无法安全读取：{exc}') from exc
        finally:
            if connection is not None:
                connection.close()


def _source_db_counts(source_db: Path) -> Dict[str, int]:
    """在**暂存副本**上数事实：源目录同样一个字节都不碰。"""
    try:
        with _staged_database(source_db) as staged:
            if not staged.is_file():
                return {}
            return _table_counts(staged)
    except Exception:                                     # noqa: BLE001
        return {}


# ==================== 预检（只读） ====================

def preflight(request: MigrationRequest) -> MigrationReport:
    """**只读**预检：来源是否可信、归属是否明确、目标是否可发布。"""
    source = Path(request.source_dir)
    paths = account_paths(request.shared_root, request.uid)
    report = MigrationReport(status='refused', uid=int(request.uid),
                             target_dir=str(paths.account_dir))
    if not source.is_dir():
        report.reason = 'source_missing'
        report.detail = f'来源目录不存在：{source}'
        return report
    evidence = read_source_evidence(source)
    report.facts['source_evidence'] = evidence
    report.source_fingerprint = source_fingerprint(source)

    if not evidence.get('db_present') and not evidence.get('state_present'):
        report.reason = 'source_empty'
        report.detail = '来源目录既没有任务库也没有状态资料：没有可迁移的账号事实'
        return report

    # 归属必须**明确**：库已经登记给别的 UID → 拒绝；状态快照冻结的 run 身份与
    # 显式映射不符 → 拒绝（不把当前 cookie 当历史权威）。
    if evidence.get('db_owner_state') == 'ok' and evidence.get('db_owner_uid') is not None:
        if int(evidence['db_owner_uid']) != int(request.uid):
            report.reason = 'owner_mismatch'
            report.detail = (f'来源库登记属于 UID {evidence["db_owner_uid"]}，'
                             f'与显式映射 {request.uid} 不符')
            return report
    run_uid = int(evidence.get('state_run_uid') or 0)
    if run_uid and run_uid != int(request.uid):
        report.reason = 'state_owner_mismatch'
        report.detail = f'来源状态快照的运行账号是 UID {run_uid}，与显式映射不符'
        return report
    if request.room_id is not None:
        run_room = int(evidence.get('state_run_room') or 0)
        if run_room and run_room != int(request.room_id):
            report.reason = 'room_mismatch'
            report.detail = f'来源状态快照的房间是 {run_room}，与显式证据不符'
            return report
    if request.run_id is not None:
        run_id = str(evidence.get('state_run_id') or '')
        if run_id and run_id != str(request.run_id):
            report.reason = 'run_mismatch'
            report.detail = f'来源状态快照的运行 id 是 {run_id}，与显式证据不符'
            return report
    # 旧格式 ``LiveState`` 没有 UID，但它写下的房间号/运行 id 一旦与显式映射**矛盾**
    # 就必须拒绝：不能把"写着别家房间"的旧档当成"归属不明所以照搬"。
    legacy = evidence.get('state_legacy') or {}
    legacy_room = int(legacy.get('room_id') or 0)
    if request.room_id is not None and legacy_room and legacy_room != int(request.room_id):
        report.reason = 'room_mismatch'
        report.detail = (f'旧格式状态档记录的房间是 {legacy_room}，'
                         f'与显式证据 {request.room_id} 不符')
        return report
    legacy_run = str(legacy.get('run_id') or '')
    if request.run_id is not None and legacy_run and legacy_run != str(request.run_id):
        report.reason = 'run_mismatch'
        report.detail = (f'旧格式状态档记录的运行 id 是 {legacy_run}，'
                         f'与显式证据 {request.run_id} 不符')
        return report

    # 目标：已有可信账号目录不覆盖；同一份迁移重跑返回"已完成"。
    if paths.db_path.is_file():
        info = inspect_account_database(paths.db_path)
        if info['state'] == 'ok' and int(info['owner_uid']) == int(request.uid):
            receipt = _read_receipt(paths.db_path, report.source_fingerprint)
            if receipt is not None:
                report.status = 'already_done'
                report.facts['receipt'] = receipt
                report.detail = '目标账号目录已由同一份来源完成迁移；未再次导入'
                return report
        report.reason = 'target_exists'
        report.detail = ('目标账号目录已有任务库且不是同一份迁移的产物：'
                         '拒绝覆盖，原字节保留')
        return report

    report.status = 'ready'
    report.detail = '预检通过：来源可信、归属明确、目标可发布'
    return report


def _read_receipt(db_path: Path, fingerprint: str) -> Optional[Dict[str, Any]]:
    try:
        connection = sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro',
                                     uri=True, timeout=10.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT value FROM tasks_meta WHERE key = ?",
            (f'{MIGRATION_RECEIPT_PREFIX}{fingerprint}',)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return {'raw': repr(row[0])}


# ==================== 候选组装 ====================

def _copy_auxiliary(source_dir: Path, candidate: Path) -> List[str]:
    copied: List[str] = []
    for name in (STATE_FILE, LAST_RUN_FILE, EXCEL_FILE) + AUXILIARY_FILES:
        part = Path(source_dir) / name
        if part.is_file():
            shutil.copy2(part, candidate / name)
            copied.append(name)
    return copied


def _write_receipt(db_path: Path, fingerprint: str, request: MigrationRequest,
                   facts: Dict[str, Any]) -> None:
    """把迁移回执**写进候选库**（发布之前），因此重跑可以只读核对。"""
    receipt = {
        'uid': int(request.uid),
        'source_dir': str(Path(request.source_dir).resolve()),
        'source_fingerprint': fingerprint,
        'room_id': request.room_id,
        'run_id': request.run_id,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'counts': facts.get('counts') or {},
    }
    connection = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
            (f'{MIGRATION_RECEIPT_PREFIX}{fingerprint}',
             json.dumps(receipt, ensure_ascii=False)))
        connection.commit()
    finally:
        connection.close()


# ==================== 执行 ====================

def run_migration(request: MigrationRequest) -> MigrationReport:
    """执行一次显式迁移：**先领取 → 候选组装 → 全程验证 → 原子发布**。"""
    plan = preflight(request)
    if plan.status != 'ready':
        return plan
    paths = account_paths(request.shared_root, request.uid)
    lease = None
    candidate: Optional[Path] = None
    try:
        lease = acquire_account_lease(request.shared_root, int(request.uid))
    except AccountBusy as exc:
        return MigrationReport(status='refused', reason='account_busy',
                               detail=f'目标 UID 已被占用：{exc.detail}',
                               uid=int(request.uid),
                               target_dir=str(paths.account_dir))
    except AccountError as exc:
        return MigrationReport(status='refused', reason=exc.reason,
                               detail=exc.detail or str(exc),
                               uid=int(request.uid),
                               target_dir=str(paths.account_dir))
    report = MigrationReport(status='refused', uid=int(request.uid),
                             target_dir=str(paths.account_dir),
                             source_fingerprint=plan.source_fingerprint)
    try:
        # 领取之后**再查一次**目标：另一个进程可能刚好发布了同一份迁移。
        if paths.db_path.is_file():
            info = inspect_account_database(paths.db_path)
            if (info['state'] == 'ok' and int(info['owner_uid']) == int(request.uid)
                    and _read_receipt(paths.db_path,
                                      report.source_fingerprint) is not None):
                report.status = 'already_done'
                report.detail = '目标已由同一份来源完成迁移（领取后复检）'
                return report
            report.reason = 'target_exists'
            report.detail = '目标账号目录已有任务库：拒绝覆盖，原字节保留'
            return report

        request.shared_root.mkdir(parents=True, exist_ok=True)
        candidate = Path(tempfile.mkdtemp(
            prefix=f'.migrating-{int(request.uid)}-',
            dir=str(paths.root)))
        source = Path(request.source_dir)
        report.steps.append(f'候选目录已建立：{candidate.name}')

        if (source / DB_FILE).is_file():
            _consistent_copy(source / DB_FILE, candidate / DB_FILE)
            report.steps.append('任务库一致性副本已准备（含已提交 WAL）')
        copied = _copy_auxiliary(source, candidate)
        report.steps.append(f'账号资料已复制：{",".join(copied) or "无"}')

        # 归属登记 + 形状核验（在**候选**上做，源目录原字节未动）。
        #
        # 注意这里的判据与运行时**不同源**：运行时（``accounts.verify_account_database``）
        # 遇到"未标注且带数据的库"必须**拒绝**——那是"归属不明，不许当空账号"。
        # 迁移工具恰好相反：那份未标注的旧库正是要被迁移的对象，授权来自**显式的
        # 来源→UID 映射**（预检已经逐项核对过 owner/run/room 证据）。
        from ..task_manager import TaskManager
        task_manager = TaskManager(
            db_path=str(candidate / DB_FILE),
            excel_path=str(candidate / EXCEL_FILE),
            reset_scheduler=False,
            last_run_date_file=str(candidate / LAST_RUN_FILE),
            seed_sample_data=False,
            import_from_excel=False)
        try:
            info = inspect_account_database(candidate / DB_FILE)
            if info['state'] == 'ok' and int(info['owner_uid']) != int(request.uid):
                raise MigrationRefusedError(
                    'owner_mismatch',
                    f'候选库登记属于 UID {info["owner_uid"]}，与显式映射不符')
            if info['state'] in ('corrupt', 'unreadable'):
                raise MigrationRefusedError(
                    'candidate_db_unusable',
                    f'候选库形状不可信：{info.get("detail") or info["state"]}')
            claim_account_owner(task_manager.db, int(request.uid))
            report.facts['source_counts'] = _table_counts(candidate / DB_FILE)
            report.facts['owner_registered'] = int(request.uid)
            _write_receipt(candidate / DB_FILE, report.source_fingerprint,
                           request, report.facts)
            report.steps.append('归属已登记、迁移回执已写入候选库')
        finally:
            task_manager.shutdown()

        # 状态资料：**有效资料**（主档，或主档不可用时的最后可用副本）按既有阶段 2
        # 解释器显式转换；转换不通过就拒绝，绝不发布半成品。判据与预检、与运行时
        # 归属核验同源——不用"主档文件在不在"代替"有没有有效状态资料"。房间号缺失时
        # 用有效资料自己的房间号或显式证据补齐（没有就不猜）。
        room_id = request.room_id
        effective = effective_state_identity(candidate)
        if effective.get('present'):
            from ..session.core import AccountContext
            from .production import take_over_state
            if room_id is None:
                room_id = int(effective.get('room_id') or 0)
            account = AccountContext(uid=int(request.uid), room_id=int(room_id or 0))
            takeover = take_over_state(data_dir=candidate, account=account,
                                       settings={}, now=None)
            report.facts['takeover'] = takeover.to_dict()
            report.steps.append(f'状态诊断：{takeover.status}')
            if not takeover.state_usable:
                report.reason = takeover.reason or 'state_unusable'
                report.detail = takeover.detail or '状态资料无法解释成可信账号事实'
                return report
            from ..session.store import VERDICT_TRUSTED, SessionStore
            loaded = SessionStore(candidate / STATE_FILE).load()
            if loaded.status != VERDICT_TRUSTED:
                report.reason = 'candidate_state_untrusted'
                report.detail = loaded.error or '候选状态快照不可信'
                return report
            # **候选身份复核**（发布之前的最后一道）：被搬过来的有效资料冻结的运行
            # 账号 / 房间 / 运行 id 必须与显式映射一致。只按"有没有主档文件"判断，
            # 会把一份仅存在于 ``.bak`` 的有效资料当成"没有状态资料"照搬过去，最终
            # 把别人的 Run 发布到本账号目录（复核 R5）。
            published = effective_state_identity(candidate)
            report.facts['candidate_state'] = published
            run_uid = int(published.get('uid') or 0)
            if run_uid and run_uid != int(request.uid):
                report.reason = 'state_owner_mismatch'
                report.detail = (f'候选有效状态资料属于 UID {run_uid}，'
                                 f'与显式映射 {request.uid} 不符：拒绝发布')
                return report
            if request.room_id is not None:
                run_room = int(published.get('room_id') or 0)
                if run_room and run_room != int(request.room_id):
                    report.reason = 'room_mismatch'
                    report.detail = (f'候选有效状态资料的房间是 {run_room}，'
                                     f'与显式证据 {request.room_id} 不符')
                    return report
            if request.run_id is not None:
                run_id = str(published.get('run_id') or '')
                if run_id and run_id != str(request.run_id):
                    report.reason = 'run_mismatch'
                    report.detail = (f'候选有效状态资料的运行 id 是 {run_id}，'
                                     f'与显式证据 {request.run_id} 不符')
                    return report
        else:
            report.steps.append('来源没有状态资料：只迁移任务库与日切依据')

        # 发布：目标此刻仍不存在（领取后复检过），目录改名是最后一步。
        if paths.account_dir.exists():
            report.reason = 'target_exists'
            report.detail = '发布前发现目标目录出现：拒绝覆盖，原字节保留'
            return report
        paths.account_dir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(candidate), str(paths.account_dir))
        candidate = None
        report.steps.append('候选已原子发布到账号目录')
        report.status = 'published'
        report.detail = '迁移完成：账号目录已由候选发布'
        return report
    except MigrationRefusedError as exc:
        report.reason = exc.reason
        report.detail = exc.detail
        return report
    except AccountDataError as exc:
        report.reason = exc.reason
        report.detail = exc.detail
        return report
    except Exception as exc:                              # noqa: BLE001
        report.reason = 'migration_error'
        report.detail = f'{type(exc).__name__}: {exc}'
        return report
    finally:
        if candidate is not None:
            shutil.rmtree(candidate, ignore_errors=True)
        if lease is not None:
            lease.release()


# ==================== CLI ====================

def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='M0A 单账号显式迁移（离线）。来源不明时不猜、不触碰真实库。')
    parser.add_argument('--uid', type=int, required=True, help='明确的目标 UID')
    parser.add_argument('--source', required=True, help='明确的旧实例数据目录')
    parser.add_argument('--shared-root', required=True, help='共享账号根目录')
    parser.add_argument('--room-id', type=int, default=None, help='显式房间号证据')
    parser.add_argument('--run-id', default=None, help='显式运行 id 证据')
    parser.add_argument('--preflight', action='store_true', help='只做只读预检')
    parser.add_argument('--json-out', default=None, help='把报告写到该文件')
    args = parser.parse_args(argv)

    request = MigrationRequest(uid=int(args.uid), source_dir=Path(args.source),
                               shared_root=Path(args.shared_root),
                               room_id=args.room_id, run_id=args.run_id)
    report = preflight(request) if args.preflight else run_migration(request)
    text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding='utf-8')
    return 0 if report.status in ('published', 'already_done', 'ready') else 1


if __name__ == '__main__':                                # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
