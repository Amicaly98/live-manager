"""M0A：**账号级数据路径、原生占用与账号数据绑定**。

产品口径（用户 2026-10-05 确定，见 ``docs/plans/multi-instance-console-20261005/
PLAN.md`` §2.1/§2.2/§2.3）：

- 任务、进度、历史、每日账本与任务版本属于 **UID**；端口只是执行入口。
- 同机所有新版实例共用一个**明确配置**的账号根目录，按 UID 分库：

      <共享账号根>/
        accounts/<uid>/live_tasks.db               # 任务、历史、账本、版本
        accounts/<uid>/live_state.json             # 既有 Goal/Run/End 快照
        accounts/<uid>/task_manager_last_run.json  # 该账号自己的日切事实
        locks/<uid>.lock                           # 原生排他句柄的**载体文件**

  **锁文件存在本身不表示占用**：排他权来自操作系统持有的句柄，进程硬退出后由
  OS 释放。这里不删锁文件、不做心跳/TTL/陈旧 PID 猜测，也不需要 8000 协调。

- 实例目录仍然只放**实例归属**的东西：``settings.json`` / ``bili_cookies.json`` /
  ``rtmp_cache.json`` / 媒体库存与临时件。账号资料不随端口复制。

本模块**不**保存第二份 Goal、计时或业务进度：它是路径解析、原生占用、库归属核验
与"把 TaskManager/SessionStore 和占用绑成一份资源对象"的窄接缝。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ACCOUNTS_SUBDIR = 'accounts'
LOCKS_SUBDIR = 'locks'
DB_FILE = 'live_tasks.db'
STATE_FILE = 'live_state.json'
LAST_RUN_FILE = 'task_manager_last_run.json'
EXCEL_FILE = 'live_tasks.xlsx'

#: 账号库归属登记键。与任务版本元数据同住 ``tasks_meta`` —— 同一份 SQLite、
#: 同一个事务能力，因此"库属于哪个 UID"不需要第二个权威文件。
META_ACCOUNT_OWNER_KEY = 'account_owner_uid'

#: 判定"空库"时要确认**没有事实**的三张表：任务行、历史、完成账本。
_FACT_TABLES = ('tasks', 'task_history', 'task_completion_ledger')


class AccountError(RuntimeError):
    """账号路径/占用/归属层的一般失败。``reason`` 是稳定判据码。"""

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason = reason
        self.detail = detail


class AccountBusy(AccountError):
    """该 UID 已被占用：**本机另一个新版进程**持有原生排他句柄。"""

    def __init__(self, detail: str = '') -> None:
        super().__init__('account_busy', detail)


class AccountLockUnavailable(AccountError):
    """原生占用原语或锁目录不可用（读不出/建不出，不等于"没人占用"）。"""

    def __init__(self, detail: str = '') -> None:
        super().__init__('lock_unavailable', detail)


class AccountDataError(AccountError):
    """账号目录/库的形状或归属不可信：**保留原字节**，不回退、不当空账号。"""


class AccountAcquisitionError(AccountError):
    """取得账号数据资源的过程中失败（开库 / 登记归属 / 建 SessionStore…）。

    ``retained`` 是**没有收齐的真实资源所有者**（一份 :class:`AccountBinding`）：
    任务库句柄没能确认关闭时它就非空，调用方必须把它交给既有未释放候选门，绝不
    能随异常一起丢掉引用（那会让占用与句柄在无人持有的状态下继续存在）。收齐并
    释放干净时为 ``None``。
    """

    def __init__(self, reason: str, detail: str = '',
                 retained: Any = None) -> None:
        super().__init__(reason, detail)
        self.retained = retained


@dataclass(frozen=True)
class AccountPaths:
    """一个 UID 的账号资料路径。纯路径计算，不产生任何副作用。"""

    root: Path
    uid: int

    def __post_init__(self) -> None:
        if int(self.uid) <= 0:
            raise AccountDataError('bad_uid', f'UID 必须是正数：{self.uid!r}')

    @property
    def account_dir(self) -> Path:
        return Path(self.root) / ACCOUNTS_SUBDIR / str(int(self.uid))

    @property
    def db_path(self) -> Path:
        return self.account_dir / DB_FILE

    @property
    def state_path(self) -> Path:
        return self.account_dir / STATE_FILE

    @property
    def last_run_path(self) -> Path:
        return self.account_dir / LAST_RUN_FILE

    @property
    def excel_path(self) -> Path:
        return self.account_dir / EXCEL_FILE

    @property
    def lock_path(self) -> Path:
        return Path(self.root) / LOCKS_SUBDIR / f'{int(self.uid)}.lock'

    def to_dict(self) -> Dict[str, Any]:
        return {'root': str(self.root), 'uid': int(self.uid),
                'account_dir': str(self.account_dir)}


def account_paths(root: Any, uid: Any) -> AccountPaths:
    return AccountPaths(root=Path(root).resolve(), uid=int(uid))


# ==================== 原生账号占用（OS 持有的排他句柄） ====================

_IS_WINDOWS = os.name == 'nt'

#: 本进程持有的占用：``规范化锁路径 → 记录``。
#:
#: Windows 的 ``CreateFileW`` 共享模式是**系统级**的（同一进程再开一次同样会得到
#: 共享冲突），POSIX 的 ``flock`` 也不是同进程可重入的。因此这份注册表**不是**
#: 引用计数共享：它只记录"本进程此刻是否已经持有该 UID 的句柄"，重复领取一律
#: 明确拒绝（见 :func:`acquire_account_lease`）。``refs`` 因此恒为 1，保留它只是
#: 为了让"哪一份句柄还在"这一事实可观察（``held_leases``）。
_REGISTRY_LOCK = threading.Lock()
_LEASES: Dict[str, '_LeaseRecord'] = {}


class _LeaseRecord:
    __slots__ = ('handle', 'refs')

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.refs = 1


class AccountLease:
    """一份账号占用。**排他权来自句柄**，不是锁文件是否存在。

    不绑定线程：Windows 的 ``CloseHandle`` 与 POSIX 的 ``os.close`` 都可以由任意
    线程调用，因此这里**不采用**线程归属的 mutex/锁对象。
    """

    def __init__(self, key: str, record: _LeaseRecord, lock_path: Path) -> None:
        self._key = key
        self._record = record
        self.lock_path = lock_path
        self._released = False

    @property
    def acquired(self) -> bool:
        return not self._released

    def release(self) -> None:
        """释放**本份**占用；引用计数归零后才真正关闭句柄。幂等。"""
        if self._released:
            return
        self._released = True
        with _REGISTRY_LOCK:
            record = _LEASES.get(self._key)
            if record is not self._record:
                # 记录已被别人换掉（异常路径）：不再动它，也不谎称已解锁。
                return
            self._record.refs -= 1
            if self._record.refs > 0:
                return
            del _LEASES[self._key]
        _close_handle(self._record.handle)

    def __enter__(self) -> 'AccountLease':
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _close_handle(handle: Any) -> None:
    try:
        if _IS_WINDOWS:
            _kernel32().CloseHandle(handle)
        else:
            os.close(handle)
    except Exception as exc:                              # noqa: BLE001
        logger.warning('释放账号占用句柄失败（进程退出时由 OS 兜底）：%s', exc)


_kernel32_cache = None


def _kernel32():
    global _kernel32_cache
    if _kernel32_cache is None:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32_cache = k32
    return _kernel32_cache


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_ERROR_SHARING_VIOLATION = 32
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5


def _open_exclusive(lock_path: Path) -> Any:
    """取得原生排他**句柄**；失败时抛 :class:`AccountBusy` / :class:`AccountLockUnavailable`。

    Windows：``CreateFileW`` 共享模式填 **0**（谁都不许再打开），因此第二个进程
    （或同一进程的第二次打开）会拿到 ``ERROR_SHARING_VIOLATION`` —— 这就是"已被
    占用"的**操作系统事实**，不是"锁文件存在"。
    POSIX：``flock(LOCK_EX|LOCK_NB)``。两者都在进程退出时由内核释放。
    """
    if _IS_WINDOWS:
        k32 = _kernel32()
        handle = k32.CreateFileW(str(lock_path), _GENERIC_READ | _GENERIC_WRITE,
                                 0, None, _OPEN_ALWAYS,
                                 _FILE_ATTRIBUTE_NORMAL, None)
        if handle == ctypes.c_void_p(-1).value or handle is None:
            err = ctypes.get_last_error()
            if err == _ERROR_SHARING_VIOLATION:
                raise AccountBusy(f'锁已被占用（WinError {err}）')
            raise AccountLockUnavailable(f'创建锁句柄失败（WinError {err}）')
        return handle

    import fcntl
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise AccountLockUnavailable(f'打开锁文件失败：{exc}') from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (getattr(__import__('errno'), 'EACCES', 13),
                         getattr(__import__('errno'), 'EAGAIN', 11)):
            raise AccountBusy(f'锁已被占用（errno {exc.errno}）') from exc
        raise AccountLockUnavailable(f'flock 失败：{exc}') from exc
    return fd


def acquire_account_lease(root: Any, uid: Any) -> AccountLease:
    """**先领取**：非阻塞取得该 UID 的原生排他占用。

    调用约定与产品口径一致：输者到此为止，不得进入迁移 / 开库 / 构造 Runtime。
    锁目录不可用时**明确失败**（``AccountLockUnavailable``），不回落到"没有锁"。

    **本进程内的第二次领取同样算"被占用"**：同一 UID 的句柄已经由本进程持有时，
    这里如实抛 :class:`AccountBusy`，不因为是"自己人"就自动把引用计数加一。自动
    共享引用会把"同一账号被再次领取"（重复登录、资源未收齐时的下一次激活）伪装成
    "共用同一份占用"，从而绕过未收齐资源应有的拒绝。确实需要内部移交时，移交的是
    **资源的实际所有者**（``AccountBinding`` / 实例）本身，不经过本函数重新领取。
    """
    paths = account_paths(root, uid)
    try:
        paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AccountLockUnavailable(f'账号锁目录不可用：{exc}') from exc
    key = os.path.normcase(os.path.abspath(str(paths.lock_path)))
    with _REGISTRY_LOCK:
        if key in _LEASES:
            raise AccountBusy('该 UID 的原生占用已由本进程持有：同一账号不能重复领取')
        handle = _open_exclusive(paths.lock_path)
        record = _LeaseRecord(handle)
        _LEASES[key] = record
        return AccountLease(key, record, paths.lock_path)


def held_leases() -> Dict[str, int]:
    """只读观察口：本进程当前持有的占用（规范化路径 → 引用计数）。"""
    with _REGISTRY_LOCK:
        return {key: record.refs for key, record in _LEASES.items()}


# ==================== 库归属（tasks_meta 里的 owner UID） ====================

def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f'file:{db_path.as_posix()}?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def inspect_account_database(db_path: Path) -> Dict[str, Any]:
    """只读判定一份账号库的归属与形状。**绝不改写任何字节**。

    返回 ``{'state': ..., 'owner_uid': ...}``，``state`` ∈

    - ``missing``    —— 文件不存在（全新账号）；
    - ``ok``         —— 有合法 owner 登记；
    - ``unlabeled``  —— 库存在且有 ``tasks`` 表，但没有 owner 登记；
    - ``corrupt``    —— owner 登记存在却读不出来（不降级成"没有登记"）；
    - ``unreadable`` —— 打不开 / 不是 SQLite / 连 ``tasks`` 表都没有。
    """
    if not Path(db_path).is_file():
        return {'state': 'missing', 'owner_uid': None}
    try:
        connection = _connect_readonly(Path(db_path))
    except sqlite3.Error as exc:
        return {'state': 'unreadable', 'owner_uid': None, 'detail': str(exc)}
    try:
        tables = {row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' not in tables:
            return {'state': 'unreadable', 'owner_uid': None,
                    'detail': '缺少 tasks 表'}
        if 'tasks_meta' not in tables:
            return {'state': 'unlabeled', 'owner_uid': None}
        row = connection.execute(
            "SELECT value FROM tasks_meta WHERE key = ?",
            (META_ACCOUNT_OWNER_KEY,)).fetchone()
    except sqlite3.Error as exc:
        return {'state': 'unreadable', 'owner_uid': None, 'detail': str(exc)}
    finally:
        connection.close()
    if row is None:
        return {'state': 'unlabeled', 'owner_uid': None}
    raw = row['value']
    try:
        owner = int(str(raw).strip())
    except (TypeError, ValueError):
        # 登记存在却读不出来：这是**损坏**，不是"没有登记"（读不出事实不等于
        # 事实不存在）。按不可信拒绝，保留原字节。
        return {'state': 'corrupt', 'owner_uid': None, 'raw': repr(raw)}
    return {'state': 'ok', 'owner_uid': owner}


def account_database_is_empty(db_path: Path) -> bool:
    """库存在且**没有任何事实**（任务行 / 历史 / 完成账本）时返回 True。"""
    try:
        connection = _connect_readonly(Path(db_path))
    except sqlite3.Error:
        return False
    try:
        tables = {row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _FACT_TABLES:
            if table not in tables:
                continue
            found = connection.execute(
                f'SELECT 1 FROM {table} LIMIT 1').fetchone()
            if found is not None:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def claim_account_owner(task_db, uid: int) -> None:
    """在**任务库自己的连接**上登记 owner UID（同库、同事务能力）。

    ``task_db`` 是 ``app.core.db.TaskDB``。这里只转调它既有的窄元数据写口，
    不复用/复制任何任务事务逻辑。
    """
    task_db.claim_account_owner(int(uid))


def verify_account_database(paths: AccountPaths) -> str:
    """开库前的**只读**归属核验。返回实际发生的动作。

    - ``created``  —— 账号目录里还没有库：由调用方新建并登记 owner；
    - ``adopted``  —— 已存在**空**库（上一次激活中断留下的骨架）→ 登记 owner；
    - ``existing`` —— 已有属于本 UID 的库：**一个字节都不动**。

    其余情况一律抛 :class:`AccountDataError` 并**保留原字节**：不能把一份未标注
    的旧库补上当前 UID 当成迁移成功，也不能接管别人的库。
    """
    info = inspect_account_database(paths.db_path)
    state = info['state']
    if state == 'missing':
        return 'created'
    if state == 'ok':
        if int(info['owner_uid']) != int(paths.uid):
            raise AccountDataError(
                'owner_mismatch',
                f'账号目录 …/{ACCOUNTS_SUBDIR}/{paths.uid} 下的库登记属于 UID '
                f'{info["owner_uid"]}：拒绝打开，原字节保留')
        return 'existing'
    if state == 'unlabeled':
        if not account_database_is_empty(paths.db_path):
            raise AccountDataError(
                'unlabeled_database',
                '账号目录下已有一份**未标注归属**且带数据的任务库：不自动补当前 '
                'UID，也不当空账号；请走显式迁移登记来源')
        return 'adopted'
    if state == 'corrupt':
        raise AccountDataError('owner_unreadable',
                               '账号库的归属登记存在但读不出来：保留原字节，'
                               '不按"没有登记"处理')
    raise AccountDataError('database_unreadable',
                           f'账号库不可读或形状不完整：{info.get("detail") or state}')


class AccountContextExpired(RuntimeError):
    """请求携带的账号绑定在**执行点**已经不再有效。

    这不是"失败"，是"这次的账号上下文已经换了"：调用方必须**明确拒绝**，绝不能
    补最新上下文后自动重放（那会把旧页面的操作落到新账号上）。
    """

    def __init__(self, detail: str = '账号上下文已失效，请刷新后重试') -> None:
        super().__init__(detail)
        self.reason = 'account_context_expired'
        self.detail = detail


class FrozenAccountWork:
    """一个请求在**领取那一刻**冻结的账号绑定；执行点再核验它仍然有效。

    它是"请求归属"的载体，不是第二份队列：不排队、不重放、不缓存任何待办。用法
    与交办 §6 的参考结构一致::

        frozen = capture_account_work()
        def queued_write():
            with frozen.enter_valid_work():        # 核验 + 工作期间持有资源
                result = perform_transaction(frozen.task_manager)
                wake_same_binding_if_still_current(...)
                return result

    - ``binding`` 为 None 表示兼容模式（实例级任务库）：此时没有账号代次可言，
      冻结的只是"这一份任务库"；
    - 账号模式下 ``binding`` 是领取那一刻的 :class:`AccountBinding`。执行前若当前
      绑定已经不是它，``enter_valid_work`` 抛 :class:`AccountContextExpired`；
    - 工作期间绑定**被引用计数持有**，退出/关闭要等在途工作归零才关库放锁。
    """

    def __init__(self, binding: Any, current_binding) -> None:
        self._binding = binding
        self._current_binding = current_binding

    @property
    def binding(self) -> Any:
        return self._binding

    @property
    def uid(self) -> Optional[int]:
        return None if self._binding is None else int(self._binding.uid)

    @property
    def task_manager(self) -> Any:
        """冻结的那一份任务库；**没有账号绑定**时为 None（兼容模式/隔离夹具）。

        那种情形下"当前账号"这个概念不存在，取库由调用方沿用它们一贯的入口
        （见 ``app.api.tasks._task_manager_or_409``），不在这里另造第二份机制。
        """
        return None if self._binding is None else self._binding.task_manager

    def is_current(self) -> bool:
        return self._current_binding() is self._binding

    @contextmanager
    def enter_valid_work(self):
        if not self.is_current():
            raise AccountContextExpired('账号已切换，本次操作不再属于该账号')
        entered = False
        if self._binding is not None:
            entered = self._binding.try_enter_work()
            if not entered:
                raise AccountContextExpired('账号正在收尾，本次操作已被拒绝')
        try:
            yield self.task_manager
        finally:
            if entered and self._binding is not None:
                self._binding.leave_work()


# ==================== 账号数据绑定 ====================

def verify_account_state_owner(paths: AccountPaths) -> str:
    """只读核对账号目录里的**有效状态资料**不是别人的场次。

    这里只回答一件事：一份**可信**快照若已经冻结了运行账号，它的 ``uid`` 必须与
    账号目录一致。缺失 / 首次启动 / 旧格式（交给阶段 2 解释器）/ 快照里 uid 为 0
    （尚未冻结身份）/ 两份都不可信，都不在这里下结论 —— 本函数不改字节、不迁移、
    不清零。

    "有没有状态资料、那份资料是谁的"取自**与迁移来源预检、迁移候选核验完全同源**
    的有效资料解释（``account_migrate.effective_state_identity``，即既有
    ``SessionStore`` 的"主档优先、不可用则取最后可用副本"规则），不在这里另写一套
    "哪份算数"：主档文件不在**不代表**没有有效状态 —— 它可能正是旁边那份可信
    ``.bak``。用文件存在性当判据会在开库前漏掉归属核验，把**别人**的场次读进本
    账号运行时并改掉账号目录字节（复核 S2）。
    """
    from ..session.store import VERDICT_TRUSTED
    from .account_migrate import effective_state_identity

    identity = effective_state_identity(paths.account_dir)
    if not identity.get('present'):
        # 首次启动：主档与副本都不存在。**只有这一种**情形算"没有有效状态资料"。
        return 'absent'
    run_uid = int(identity.get('uid') or 0)
    if run_uid and run_uid != int(paths.uid):
        raise AccountDataError(
            'state_owner_mismatch',
            f'账号目录 …/{ACCOUNTS_SUBDIR}/{paths.uid} 下的有效运行快照（来源 '
            f'{identity.get("source")}）属于 UID {run_uid}：拒绝打开，原字节保留')
    if identity.get('status') == VERDICT_TRUSTED:
        return 'ok'
    return f'skipped_{identity.get("status")}'


def open_account_binding(root: Any, uid: int,
                         task_manager_factory=None) -> AccountBinding:
    """**先领取，再开库**：取得占用 → 只读核验 → 构造 TaskManager → 登记归属。

    release 路径是承重的（见交办 §5）：任何迁移、开库或构造 Runtime 都必须发生在
    原生占用**之后**，否则两个进程会在同一份账号目录上各建一份运行实例。取得占用
    之后若任一步失败，已取得的资源由本函数负责收尾：**收齐就释放**（关掉任务库后
    释放占用）；**没收齐**则把那份真实绑定随 :class:`AccountAcquisitionError`
    交给调用方，由既有的未释放候选门保留——绝不静默回落到实例库，也绝不丢引用。
    """
    paths = account_paths(root, uid)
    lease = acquire_account_lease(paths.root, uid)      # AccountBusy 直接向上抛
    task_manager = None
    binding = None
    try:
        paths.account_dir.mkdir(parents=True, exist_ok=True)
        # **先核状态资料归属，再核任务库**：两步都是开库前的只读预检，但顺序是
        # 承重的 —— 读任务库（哪怕 ``mode=ro``）会在 WAL 库的目录里落下
        # ``-wal``/``-shm``，于是"拒绝"本身就已经改动了账号目录的字节。状态资料
        # 的归属核验只读 JSON，先做它，被它拒绝时账号目录**一个字节都不动**
        # （复核 S2 的直接判据）。任务库自己的归属核验随后照旧。
        verify_account_state_owner(paths)
        action = verify_account_database(paths)
        if task_manager_factory is None:
            from ..task_manager import TaskManager

            def task_manager_factory(**kwargs):
                return TaskManager(**kwargs)
        task_manager = task_manager_factory(
            db_path=str(paths.db_path),
            excel_path=str(paths.excel_path),
            reset_scheduler=False,
            last_run_date_file=str(paths.last_run_path),
            seed_sample_data=False,
            import_from_excel=False)
        # 任务库句柄一经建立，绑定就是它的**所有者**：后续任何一步失败都从绑定
        # 收尾，而不是各自决定"要不要放掉占用"。
        binding = AccountBinding(paths=paths, lease=lease,
                                 task_manager=task_manager, store=None,
                                 uid=int(paths.uid))
        if action in ('created', 'adopted'):
            claim_account_owner(task_manager.db, int(paths.uid))
        from ..session.store import SessionStore
        binding.store = SessionStore(paths.state_path)
        logger.info('账号绑定已打开：uid=%s dir=%s（%s）',
                    paths.uid, paths.account_dir, action)
        return binding
    except BaseException as exc:
        if binding is None:
            # 还没取得任何数据资源（占用不算"数据资源"）：只还 lease，异常原样上抛
            # （类型不变，调用方既有的判据继续成立）。
            lease.release()
            raise
        try:
            report = binding.close(0.0)
        except Exception:                                 # noqa: BLE001
            report = None
        if _report_is_clean(report):
            # 收齐并释放干净：异常原样上抛，不改变它的类型与判据。
            raise
        # 任务库句柄未确认收齐：**保留真实所有者**，把它随异常交给调用方转交既有
        # 未释放候选门。不在这里假装已经收齐、也不把引用丢掉。
        logger.warning('账号数据资源未收齐，绑定与占用继续保留：uid=%s', paths.uid)
        reason = getattr(exc, 'reason', '') or 'account_open_failed'
        detail = (getattr(exc, 'detail', '') or str(exc)
                  if isinstance(exc, AccountError)
                  else f'{type(exc).__name__}: {exc}')
        raise AccountAcquisitionError(reason, detail,
                                      retained=binding) from exc


def _report_is_clean(report: Any) -> bool:
    """关闭报告是否通过核验（与实例/候选门**同一条** ``clean()`` 判据）。"""
    clean = getattr(report, 'clean', None)
    if not callable(clean):
        return False
    try:
        return bool(clean())
    except Exception:                                     # noqa: BLE001
        return False


class BindingCloseReport(dict):
    """账号绑定的关闭报告。

    它**既是**既有调用方读的字段字典（``info['lock_released']`` /
    ``info.get('work_drained')`` / ``json.dumps(info)``），又实现了与
    :class:`~.factory.InstanceCloseReport` **完全同一条** ``clean()`` 判据 —— 于是
    "未收齐的绑定"可以不经过任何转换就交给既有的未释放候选门
    （``ControlService.dispose_replacement``），不需要第二张保留表、第二套干净判定
    或一个只为绑定存在的包装类型。
    """

    __slots__ = ()

    def clean(self) -> bool:
        """收齐的判据：任务库句柄确实关掉，且**之后**占用确实释放。"""
        return bool(self.get('task_manager_shutdown')
                    and self.get('lock_released'))

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


class AccountBinding:
    """一份账号资源的**所有者**：账号目录、占用、TaskManager 与 SessionStore。

    它就是"当前账号的数据绑定"：任务 HTTP 依赖、TaskDataPort/账本端口、
    SessionStore 与账号日切预约都从**同一份**绑定取得，因此不存在"只换任务列表
    数据库、让 Runtime 仍结算旧库"的错接。

    本对象**不**保存 Goal/Run/elapsed：运行权威仍在 Core/Runtime 那一边。
    """

    def __init__(self, *, paths: AccountPaths, lease: AccountLease,
                 task_manager: Any, store: Any, uid: int) -> None:
        self.paths = paths
        self.lease = lease
        self.task_manager = task_manager
        self.store = store
        self.uid = int(uid)
        self._lock = threading.Lock()
        self._closed = False
        self._close_info: Optional[Dict[str, Any]] = None
        #: 在途**账号工作**（已经开始的真实事务）计数。退出/释放必须在它归零之后
        #: 才关库放锁；这不是第二份队列，只是一道"工作期间持有资源"的窄计数。
        self._work_cond = threading.Condition(threading.Lock())
        self._work = 0
        self._retired = False

    # ---------------------------------------------------------------- 只读口

    @property
    def account_dir(self) -> Path:
        return self.paths.account_dir

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def lease_held(self) -> bool:
        return bool(self.lease is not None and self.lease.acquired)

    def to_dict(self) -> Dict[str, Any]:
        return {'uid': self.uid, 'account_dir': str(self.account_dir),
                'lease_held': self.lease_held, 'closed': self._closed,
                'work_in_flight': self.work_in_flight,
                'retired': self._retired}

    # ---------------------------------------------------------------- 在途工作

    @property
    def work_in_flight(self) -> int:
        with self._work_cond:
            return self._work

    def try_enter_work(self) -> bool:
        """开始一段**账号工作**（一次真实的读/写事务）。

        返回 False 表示这份绑定已经开始收尾（或者已经关闭）：调用方**必须拒绝**
        这次请求，绝不能改去取"当前的另一个账号"。
        """
        with self._work_cond:
            if self._retired or self._closed or self._close_info is not None:
                return False
            self._work += 1
            return True

    def leave_work(self) -> None:
        with self._work_cond:
            if self._work > 0:
                self._work -= 1
            self._work_cond.notify_all()

    def retire(self) -> None:
        """不再接受新的工作；已经在途的照常收尾。"""
        with self._work_cond:
            self._retired = True

    def wait_idle(self, timeout: float) -> bool:
        """有界等待在途工作归零；到点未归零返回 False（不谎称已收齐）。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._work_cond:
            while self._work > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._work_cond.wait(remaining)
            return True

    # ---------------------------------------------------------------- 关闭

    def close(self, budget: Optional[float] = None) -> BindingCloseReport:
        """关闭账号数据句柄并释放占用。**幂等**，并缓存首份真实结果。

        释放顺序是承重的：先**退休**（不再接受新工作）→ 有界等在途账号事务收尾 →
        关掉任务库（真句柄）→ 确实关掉之后才释放原生占用。任一步不成立都**不释放
        占用**——锁释放不证明数据句柄或在途事务已经收齐，两者分开记录（见返回字典
        的 ``lock_released`` / ``work_drained`` / ``task_manager_shutdown``）。

        返回 :class:`BindingCloseReport`：字段访问与字典一致，另外提供与实例同源的
        ``clean()``，因此"没收齐"可以直接被既有保留门识别，不必另加判据。
        """
        with self._lock:
            if self._close_info is not None:
                return BindingCloseReport(self._close_info)
            self.retire()
            info: Dict[str, Any] = {
                'uid': self.uid,
                'work_in_flight': self.work_in_flight,
                'work_drained': False,
                'task_manager_shutdown': False,
                'task_manager_error': '',
                'lock_released': False,
                'lock_error': '',
            }
            drained = self.wait_idle(0.0 if budget is None else float(budget))
            info['work_drained'] = drained
            if not drained:
                # **不缓存**这份结论：绑定还没关，稍后在途事务收尾后必须还能再试。
                # （实例侧的"首份关闭报告"语义不受影响：那是 RuntimeInstance 的
                # 幂等门，它缓存的是它自己那一次的真实结果。）
                info['lock_error'] = (f'仍有 {self.work_in_flight} 笔账号事务在途，'
                                      '账号数据句柄与占用继续保留')
                return BindingCloseReport(info)
            self._closed = True
            try:
                if self.task_manager is not None:
                    self.task_manager.shutdown()
                info['task_manager_shutdown'] = True
            except Exception as exc:                      # noqa: BLE001
                info['task_manager_error'] = f'{type(exc).__name__}: {exc}'
            if info['task_manager_error']:
                info['lock_error'] = '任务库句柄未确认收齐，占用继续保留'
            else:
                try:
                    if self.lease is not None:
                        self.lease.release()
                    info['lock_released'] = True
                except Exception as exc:                  # noqa: BLE001
                    info['lock_error'] = f'{type(exc).__name__}: {exc}'
            self._close_info = info
            return BindingCloseReport(info)
