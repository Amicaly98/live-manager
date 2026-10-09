"""account_history.py - 管理端对**已迁移账号**的分区历史只读查询（M3）。

职责（见 ``docs/plans/multi-instance-console-20261005/M3-DESIGN.md`` §7）：

- **账号目录**：从既有**同机共享账号根** ``<root>/accounts/*`` 枚举**可靠**的已迁移
  账号——目录名是合法正整数 UID，且该账号库里既有 owner 登记与目录 UID 一致。
  其余（缺库、未标注、归属不符、损坏、不可读、非数字目录）单列为错误，**不**把外来
  库贴成所选的 UID。账号列表来自共享根下的已迁移资料，**不**以在线目标列表代替。
- **一致只读**：每个 UID 一份**真实 SQLite 只读连接**（``mode=ro``，**不**用
  ``immutable``——那会丢掉活动 WAL 里已提交的事实）+ 显式 ``BEGIN`` 读快照；同一
  快照内核 owner 归属与业务读取。读取结束一律 ``ROLLBACK`` + 关闭。
- **不**调用 ``open_account_binding``（不领取运行占用）、不构造 TaskManager/Runtime/
  日切线程、不切换全局 config、不自动迁移/修复/补 baseline/写历史。
- **不**复制第二套历史算法：组装一律走 ``TaskManager.assemble_zone_history``，
  与当前账号的 ``zone_history`` 是同一条解释。

跨账号**不**要求全局原子快照：每个 UID 各自一份读快照，合并只发生在读取完成之后。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core import config
from app.core.db import META_ACCOUNT_OWNER_KEY
from app.core.session_runtime.accounts import ACCOUNTS_SUBDIR, account_paths
from app.core.task_manager import TaskManager

logger = logging.getLogger(__name__)

#: 单账号历史的读取上限（与 M1 `zone_history` 的分页上界一致）。
MAX_HISTORY_LIMIT = 200

#: 一次「全部账号」查询最多枚举的账号数：既有资料目录的上界保护，超限**明确拒绝**
#: 而不是悄悄截断（截断会让用户以为"全部账号"已经查完）。
MAX_ACCOUNTS_PER_QUERY = 64


class AccountHistoryError(RuntimeError):
    """一条只读历史查询的**明确失败**（不是"没有历史"）。

    ``reason`` 是稳定的机器可读分类，``detail`` 是如实的原因文本。调用方必须把它
    单列成错误，绝不能显示成"暂无记录"。
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(detail or reason)
        self.reason = str(reason)
        self.detail = str(detail or reason)

    def to_dict(self) -> Dict[str, str]:
        return {'reason': self.reason, 'detail': self.detail}


def _root(root: Any = None) -> Path:
    base = Path(root) if root is not None else config.account_root()
    if base is None:
        raise AccountHistoryError(
            'no_account_root',
            '本实例未启用共享账号根（account-data）：无法只读查询其他已迁移账号')
    return Path(base)


# ==================== 账号目录 ====================

def _account_entry(name: str) -> Optional[int]:
    """把 ``accounts/`` 下的一个条目名解释成合法 UID；不是正整数就返回 None。"""
    try:
        uid = int(str(name).strip())
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _inspect_owner(conn: sqlite3.Connection) -> Dict[str, Any]:
    """在读快照连接里读 owner 归属登记（与 M0A 同一判据，只读）。"""
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        return {'state': 'unreadable', 'detail': str(exc)}
    if 'tasks' not in tables:
        return {'state': 'unreadable', 'detail': '缺少 tasks 表'}
    if 'tasks_meta' not in tables:
        return {'state': 'unlabeled', 'detail': '缺少归属登记表'}
    row = conn.execute("SELECT value FROM tasks_meta WHERE key = ?",
                       (META_ACCOUNT_OWNER_KEY,)).fetchone()
    if row is None:
        return {'state': 'unlabeled', 'detail': '没有归属登记'}
    raw = row['value']
    try:
        return {'state': 'ok', 'owner_uid': int(str(raw).strip())}
    except (TypeError, ValueError):
        # 登记存在却读不出来是**损坏**，不是"没有登记"（读不出事实不等于事实不存在）。
        return {'state': 'corrupt', 'detail': f'归属登记不可解释：{raw!r}'}


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """真实 SQLite **只读**连接。

    刻意**不用** ``immutable=1``：那会跳过 WAL 与共享内存索引，把"活动 WAL 里已提交
    的事实"整片丢掉（PLAN §5.3 明确禁止）。``mode=ro`` 仍会读 WAL。
    """
    uri = f'file:{Path(db_path).as_posix()}?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def list_migrated_accounts(root: Any = None) -> Dict[str, Any]:
    """共享账号根下**可靠的**已迁移账号清单（只读，不改任何字节）。

    返回 ``{'root': str, 'accounts': [{'uid': int}], 'errors': [{...}]}``；
    目录不存在 = 还没有已迁移账号（空表，不是错误）。
    """
    base = _root(root)
    accounts_dir = base / ACCOUNTS_SUBDIR
    accounts: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    if not accounts_dir.is_dir():
        return {'root': str(base), 'accounts': [], 'errors': []}
    try:
        entries = sorted(accounts_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise AccountHistoryError('accounts_unreadable',
                                  f'账号目录不可读：{exc}') from exc
    for entry in entries:
        name = entry.name
        uid = _account_entry(name) if entry.is_dir() else None
        if uid is None:
            errors.append({'name': name, 'reason': 'invalid_account_entry',
                           'detail': '账号目录下只接受以正整数 UID 命名的目录'})
            continue
        paths = account_paths(base, uid)
        if not paths.db_path.is_file():
            errors.append({'name': name, 'uid': uid, 'reason': 'missing_database',
                           'detail': f'UID {uid} 的账号目录下没有任务库'})
            continue
        try:
            connection = _open_readonly(paths.db_path)
        except sqlite3.Error as exc:
            errors.append({'name': name, 'uid': uid,
                           'reason': 'database_unreadable', 'detail': str(exc)})
            continue
        try:
            connection.execute('BEGIN')
            info = _inspect_owner(connection)
        except sqlite3.Error as exc:
            errors.append({'name': name, 'uid': uid,
                           'reason': 'database_unreadable', 'detail': str(exc)})
            continue
        finally:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            connection.close()
        state = info.get('state')
        if state != 'ok':
            errors.append({'name': name, 'uid': uid, 'reason': f'database_{state}',
                           'detail': info.get('detail') or f'账号库状态：{state}'})
            continue
        if int(info['owner_uid']) != uid:
            # 归属不符：**不**把这份库贴成目录所声称的 UID，也不更正外来资料。
            errors.append({
                'name': name, 'uid': uid, 'reason': 'owner_mismatch',
                'detail': f'账号库登记的归属是 UID {info["owner_uid"]}，'
                          f'与目录 {uid} 不一致：拒绝按该 UID 读取'})
            continue
        accounts.append({'uid': uid})
    return {'root': str(base), 'accounts': accounts, 'errors': errors}


# ==================== 单账号一致读 ====================

class _AccountReadSession:
    """一个 UID 的**一份**读快照：一条 ``mode=ro`` 连接 + 一次显式 ``BEGIN``。

    归属核验与**所有分块**都在这条连接上做。分块续读必须如此：每块各开一条连接
    就是每块一份快照，中途别人的一次提交会让同一次响应里混进两份数据（复核 C3：
    205 份历史、查 offset=190/limit=20 时第一块读完有第二次提交，返回的 16 行里
    只有 15 个任务身份，第 6 个重复）。

    跨账号**不**要求全局原子快照（合并发生在各账号读完之后）；这里只是把**同一
    个 UID** 的多次读取收进同一份快照。
    """

    def __init__(self, uid: int, base: Path) -> None:
        self.uid = int(uid)
        self.db_path = account_paths(base, self.uid).db_path
        self.connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> '_AccountReadSession':
        if not self.db_path.is_file():
            raise AccountHistoryError(
                'missing_database', f'UID {self.uid} 的账号目录下没有任务库')
        try:
            self.connection = _open_readonly(self.db_path)
        except sqlite3.Error as exc:
            raise AccountHistoryError('database_unreadable', str(exc)) from exc
        connection = self.connection
        try:
            # 显式 deferred 读事务：第一条 SELECT 建立快照，后续读取都看**同一份**
            # 数据（含活动 WAL 里已提交的事实）。结束一律 ROLLBACK，不写任何字节。
            connection.execute('BEGIN')
            info = _inspect_owner(connection)
            state = info.get('state')
            if state != 'ok':
                raise AccountHistoryError(
                    f'database_{state}',
                    info.get('detail') or f'账号库状态：{state}')
            if int(info['owner_uid']) != self.uid:
                raise AccountHistoryError(
                    'owner_mismatch',
                    f'账号库登记的归属是 UID {info["owner_uid"]}，与请求的 '
                    f'{self.uid} 不一致：拒绝读取，原字节保留')
        except AccountHistoryError:
            self.close()
            raise
        except sqlite3.Error as exc:
            self.close()
            raise AccountHistoryError('database_unreadable', str(exc)) from exc
        return self

    def read(self, zone_name: str, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
        """在这**一份**快照里组装一页（沿用 M1 的唯一解释出口）。"""
        try:
            return TaskManager.assemble_zone_history(
                self.connection, zone_name, offset, limit)
        except sqlite3.Error as exc:
            raise AccountHistoryError('database_unreadable', str(exc)) from exc

    def close(self) -> None:
        connection = self.connection
        self.connection = None
        if connection is None:
            return
        try:
            connection.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        connection.close()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def zone_history_for_account(uid: Any, zone_name: str, offset: int = 0,
                             limit: int = 20, root: Any = None) -> Dict[str, Any]:
    """一个 UID 的分区历史（**一份**读快照里组装）。

    归属核在**同一个读快照**里做：业务表、版本元数据与读取期间都没有写入，目录与库
    身份不一致时明确拒绝，绝不把外来库当成所选 UID。

    读取**不**领取运行占用、不构造 TaskManager、不触发日切、不做历史补写。
    """
    base = _root(root)
    with _AccountReadSession(int(uid), base) as session:
        result = session.read(zone_name, offset, limit)
    return {**result, 'uid': int(uid)}


def _account_window(uid: int, zone_name: str, base: Path,
                    page_window: int) -> tuple:
    """该账号排在**前 ``page_window``** 的组（必要时按单账号上界分块续读）。

    单账号组装 ``TaskManager.assemble_zone_history`` 把 ``limit`` 封顶
    :data:`MAX_HISTORY_LIMIT`：一次调用最多取回 200 组。管理端要按**全局** offset
    分页，就必须持有每个账号"排在前 ``offset+limit``"的全部组——否则会在被截断的
    集合里二次切页，越界成永久空页（复核 R6：205 份同分区历史里 offset=200 拿到
    0 份却报 ``total=205`` / ``has_more=true``）。

    这里**不是**调高上界，而是按上界分块续读：每块用 ``offset=已取组数`` 继续，直到
    取满 ``min(page_window, total)`` 组或该账号读完。分页语义完全保留。

    全部续读落在 :class:`_AccountReadSession` 的**同一条连接/同一个 BEGIN** 上：
    中途别人的提交不会让这一次响应混进两份快照（复核 C3）。
    """
    groups: List[Dict[str, Any]] = []
    total = 0
    with _AccountReadSession(int(uid), base) as session:
        while True:
            want = page_window - len(groups)
            if want <= 0:
                break
            chunk = session.read(zone_name, len(groups),
                                 min(MAX_HISTORY_LIMIT, want))
            total = int(chunk['total'])
            if not chunk['groups']:
                break
            groups.extend(chunk['groups'])
            if len(groups) >= min(page_window, total):
                break
    return groups, total


# ==================== 多账号合并 ====================

def zone_history_for_accounts(uid_scope: Optional[Sequence[Any]], zone_name: str,
                              offset: int = 0, limit: int = 20,
                              root: Any = None) -> Dict[str, Any]:
    """多个（或全部）已迁移账号的分区历史，按全局顺序分页。

    - ``uid_scope=None`` = 全部可靠的已迁移账号；
    - 每账号各读一份快照（**不**要求跨账号全局原子快照）；
    - 单账号失败只进 ``errors``，**不**吞掉其他账号的结果；
    - 全局排序 ``(last_at, uid, task_id)`` 降序；每账号只取前 ``offset+limit`` 组
      ——该账号中任何排在其后的组都不可能进入全局前 ``offset+limit``，因此分页在
      跨账号时仍然正确且不丢数据。
    """
    base = _root(root)
    listing = list_migrated_accounts(base)
    known = {int(item['uid']) for item in listing['accounts']}
    errors: List[Dict[str, Any]] = list(listing['errors'])
    if uid_scope is None:
        uids = sorted(known)
        scope_label = 'all'
    else:
        uids = []
        for raw in uid_scope:
            uid = int(raw)
            if uid not in known:
                raise AccountHistoryError(
                    'unknown_account',
                    f'UID {uid} 不是本机共享账号根下可靠的已迁移账号：'
                    f'拒绝按该 UID 读取历史')
            if uid not in uids:
                uids.append(uid)
        scope_label = str(uids[0]) if len(uids) == 1 else 'all'
    offset = max(0, int(offset or 0))
    limit = max(1, min(MAX_HISTORY_LIMIT, int(limit or 20)))
    if len(uids) > MAX_ACCOUNTS_PER_QUERY:
        raise AccountHistoryError(
            'too_many_accounts',
            f'一次查询的账号数超过上限 {MAX_ACCOUNTS_PER_QUERY}：'
            f'请按账号分别查询')

    page_window = offset + limit
    entries: List[Dict[str, Any]] = []
    total = 0
    for uid in uids:
        try:
            # 每账号持有"排在前 page_window"的组（超过单账号上界则分块续读）：
            # 该账号中任何排在其后的组都不可能进入全局前 offset+limit，因此分页在
            # 跨账号时仍然正确且不丢数据；反过来，截断这份窗口就会造出空页。
            account_groups, account_total = _account_window(
                uid, zone_name, base, page_window)
        except AccountHistoryError as exc:
            errors.append({'uid': uid, **exc.to_dict()})
            continue
        for group in account_groups:
            group['uid'] = uid
            entries.append(group)
        total += account_total
    entries.sort(key=lambda g: (str(g.get('last_at') or ''), int(g['uid']),
                                int(g['task_id'])), reverse=True)
    page = entries[offset:offset + limit]
    return {
        'zone_name': str(zone_name or '').strip(),
        'uid_scope': scope_label,
        'groups': page,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + len(page) < total,
        #: 有账号读不出来时如实标出：``total`` 只统计**读成功**的账号，
        #: 此时它不是一个完整的总数，调用方不得把它当成"全部都是这些"。
        'partial': bool(errors),
        'errors': errors,
    }
