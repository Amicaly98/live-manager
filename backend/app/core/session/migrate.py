"""旧状态/旧标记 → 新模型的**一次性副本迁移**（阶段 2）。

它不是 `SessionStore.load()` 的一部分（那里只把旧格式判为不可信、fail closed），
也不是第二份运行时权威。它是一个显式、可重复、只读源、只写新目录的转换命令。

硬边界：

- **只动副本**。源 ``live_state.json`` / ``live_tasks.db`` / 标记文件一律只读，
  源 SHA 永远不变；源库若在用 WAL，用 SQLite ``backup()`` 取一致性副本，而不是
  拷单个 ``.db`` 文件。
- **不覆盖已有新快照**。目标目录里已经有一份合法的新格式快照时，返回
  ``already_new``，不拿旧源盖它。
- **不可信输入保留原字节并拒绝写入**，绝不用宽松默认值让它"从 0 开始"。
- **只吸收有日期证据的既有完成事实**。``days_done=N`` 不会被展开成 N 个历史日期；
  没有日期的 ``today_done`` 不会被猜成昨天或今天。
- 旧 ``save`` 时间（``last_update``）**不是**平台确认锚点，因此不据它补离线秒数；
  只迁入可信检查点，本进程区间留给第一次真实确认重新建立。
- 接手**同日未结束**的旧场时，按任务库副本冻结一次人工进度代数（见
  :func:`_freeze_takeover_guard`）：这是与"读任务行"同源的一致性读取，不是
  在提交那一刻补当前值，也不复活任何旧授权。

迁移报告是**来源记录**，不是第二份运行时授权文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .model import (
    MODE_SINGLE,
    MODE_TASK,
    PUSH_FFMPEG,
    PUSH_OBS,
    SELECT_AUTO,
    SELECT_SINGLE,
    START_NEW,
    START_RESUME,
    STEP_CLOSE_QUERY,
    STEP_IDLE,
    STEP_RESUME_QUERY,
    STEP_WAIT_TASKS,
    STOP_FACE_VERIFY,
    STOP_PLATFORM_REJECTED,
    STOP_USER,
    WANT_RUNNING,
    WANT_STOPPED,
    Goal,
    PlatformClock,
    Run,
    Selection,
    Snapshot,
    business_day_of,
)
from .store import VERDICT_TRUSTED, VERDICT_UNTRUSTED, SessionStore

logger = logging.getLogger(__name__)

#: 迁移实现版本（写进报告，便于区分"哪一版工具产出的"）。
MIGRATION_VERSION = 'phase2-migrate-1'

STATE_FILE = 'live_state.json'
DB_FILE = 'live_tasks.db'
SETTINGS_FILE = 'settings.json'
STOP_MARKER = '.no_auto_resume'
HANDOVER_FILE = '.progress_handover.json'
FACE_MARKER = '.face_verify_pending'

#: 新格式快照的版本前缀（与本阶段 `SCHEMA_VERSION` 同族）。
NEW_FORMAT_PREFIX = 'core-reset-'

#: 旧 LiveState 的特征键。旧格式的 ``schema_version`` 是**整数**，新格式是字符串，
#: 因此"同时像新旧两种"是可判定的，不需要靠"有几个旧键"来降级解释。
_LEGACY_KEYS = ('is_streaming', 'current_zone', 'effective_seconds',
                'elapsed_seconds', 'last_update', 'auto_resume', 'phase',
                'duration_seconds', 'execution_date', 'accumulated_seconds')

#: 旧 ``phase`` 里表示"这场还在跑"的取值。
_RUNNING_PHASES = ('starting', 'live', 'recovering')
_STOPPING_PHASES = ('stopping',)

#: 迁移结果状态。
STATUS_MIGRATED = 'migrated'
STATUS_ALREADY_NEW = 'already_new'
STATUS_NO_SOURCE = 'no_source'
STATUS_REFUSED = 'refused'


class MigrationRefused(RuntimeError):
    """输入不可信 / 目标不可写：保留原字节，拒绝转换。"""

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def detect_format(document: Any) -> str:
    """判定一份文档是**新核心快照**、**旧 LiveState**、还是不可识别。"""
    if not isinstance(document, dict):
        return 'unknown'
    version = document.get('schema_version')
    looks_new = (isinstance(version, str) and version.startswith(NEW_FORMAT_PREFIX)
                 and isinstance(document.get('goal'), dict))
    legacy_hits = [key for key in _LEGACY_KEYS if key in document]
    if looks_new and legacy_hits:
        # 新旧键混在一起：不能因为"有几个旧键"就降级解释。
        return 'mixed'
    if looks_new:
        return 'new_core_snapshot'
    if legacy_hits:
        return 'legacy_livestate'
    return 'unknown'


def _valid_day(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        date.fromisoformat(value[:10])
    except ValueError:
        return None
    return value[:10]


def _read_json(path: Path) -> Tuple[Optional[Any], str]:
    if not path.exists():
        return None, 'missing'
    try:
        return json.loads(path.read_text(encoding='utf-8')), ''
    except (OSError, ValueError) as exc:
        return None, f'unreadable（{exc}）'


# ---------------------------------------------------------------- 输入校验

@dataclass
class AccountContextView:
    """调用方给的账号/房间上下文（dict 或对象都能读）。"""

    uid: int = 0
    room_id: int = 0


def _account_view(account: Any) -> AccountContextView:
    if account is None:
        return AccountContextView()
    if isinstance(account, dict):
        return AccountContextView(uid=int(account.get('uid') or 0),
                                  room_id=int(account.get('room_id') or 0))
    return AccountContextView(uid=int(getattr(account, 'uid', 0) or 0),
                              room_id=int(getattr(account, 'room_id', 0) or 0))


@dataclass
class _Inputs:
    state_text: str
    state_sha: str
    document: Dict[str, Any]
    marker: bool
    handover: Optional[Dict[str, Any]]
    face_marker: bool
    settings: Dict[str, Any]


def _check_directories(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.is_absolute() or not target_dir.is_absolute():
        raise MigrationRefused('relative_path',
                               '源目录与目标目录都必须是绝对路径')
    if source_dir == target_dir:
        raise MigrationRefused('overlapping_paths', '源目录与目标目录不能相同')
    if source_dir in target_dir.parents or target_dir in source_dir.parents:
        raise MigrationRefused('overlapping_paths',
                               '目标目录不能位于源目录之内，也不能反过来包含源目录')


def _load_inputs(source_dir: Path, settings: Optional[Dict[str, Any]]) -> _Inputs:
    state_path = source_dir / STATE_FILE
    if not state_path.is_file():
        raise MigrationRefused('no_source_state', f'缺少 {STATE_FILE}')
    raw = state_path.read_bytes()
    text = raw.decode('utf-8', errors='replace')
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise MigrationRefused('untrusted_source',
                               f'状态文件无法解析（{exc}）；原字节已保留') from exc
    if not isinstance(document, dict):
        raise MigrationRefused('untrusted_source', '状态文件内容不是 JSON 对象')

    handover, _ = _read_json(source_dir / HANDOVER_FILE)
    if handover is not None and not isinstance(handover, dict):
        raise MigrationRefused('untrusted_source', '交接记录不是 JSON 对象')

    merged_settings = dict(settings or {})
    if not merged_settings:
        loaded, _why = _read_json(source_dir / SETTINGS_FILE)
        if isinstance(loaded, dict):
            merged_settings = loaded

    return _Inputs(state_text=text, state_sha=_sha256_bytes(raw),
                   document=document,
                   marker=(source_dir / STOP_MARKER).exists(),
                   handover=handover,
                   face_marker=(source_dir / FACE_MARKER).exists(),
                   settings=merged_settings)


# ---------------------------------------------------------------- 旧库读取

#: 旧库可能带的兄弟文件（WAL / 回滚日志）。
#:
#: ``-shm`` **故意不复制**：它只是 WAL 的索引缓存，不含任何持久内容 —— 少了它
#: SQLite 会按 ``-wal`` 重建；带上一份陈旧的索引反而更危险。
_DB_SIDECARS = ('-wal', '-journal')


class _LegacyDb:
    """旧任务库的**只读**视图，读的是私有暂存区里那份**归一化**结果。

    它不再直接读源目录：源目录只被读取字节（见 :func:`_stage_database`）。
    """

    def __init__(self, path: Optional[Path], source_sha: str = '') -> None:
        self.path = path
        self.source_sha = source_sha
        self.available = path is not None and Path(path).is_file()

    def _connect(self):
        # 归一化结果是我们自己的单文件副本；``mode=ro`` 不会给它建任何兄弟文件。
        return sqlite3.connect(f'file:{Path(self.path).as_posix()}?mode=ro', uri=True)

    def task(self, task_id: Optional[int], zone_name: str) -> Optional[dict]:
        if not self.available:
            return None
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if task_id is not None:
                    row = conn.execute("SELECT * FROM tasks WHERE id = ?",
                                       (int(task_id),)).fetchone()
                elif zone_name:
                    row = conn.execute("SELECT * FROM tasks WHERE zone_name = ?",
                                       (zone_name,)).fetchone()
                else:
                    return None
                return dict(row) if row else None
        except sqlite3.Error as exc:
            logger.warning('读取旧任务库失败（按无库处理）：%s', exc)
            return None

    def count_zone(self, zone_name: str) -> int:
        if not self.available or not zone_name:
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM tasks WHERE zone_name = ?",
                                   (zone_name,)).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0


def _stage_database(source_dir: Path, staging: Path) -> Optional[Path]:
    """把旧库的**完整文件集**复制到私有暂存区，并归一化成一份可读的事实源。

    为什么不能直接对着源用 ``immutable=1``：那等于声明"源主文件就是全部事实"，
    于是 WAL 里已经 COMMIT 的内容被静默忽略 —— 一份停写的完整副本明明提交过
    两天，目标却只读到零天（复核 R9）。

    ``mode=ro`` 也不行：SQLite 会为 WAL 库在**源目录**里创建 ``-shm`` / ``-wal``，
    那已经是在源目录写字了。迁移的输入按约定是**停写的副本**，因此这里只从源目录
    **读字节**：把 ``.db`` 连同它可能存在的 ``-wal`` / ``-journal`` 一起复制到
    私有暂存区，在那里正常打开（SQLite 会读到 WAL 里已提交的内容），再用
    ``backup()`` 生成一份归一化的单文件。

    产出的归一化库被**同一份**用于映射任务身份、抽取完成事实与作为目标库的底本；
    源目录逐字节不变。
    """
    source = source_dir / DB_FILE
    if not source.is_file():
        return None
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / ('legacy' + (source.suffix or '.db'))
    for suffix in ('',) + _DB_SIDECARS:
        part = source.with_name(source.name + suffix)
        if part.is_file():
            shutil.copy2(part, staged.with_name(staged.name + suffix))
    normalized = staging / 'normalized.db'
    src = None
    dst = None
    try:
        src = sqlite3.connect(str(staged), timeout=30.0)
        # 先摸一下结构：这一步就会触发 WAL 恢复/读取，坏库在这里明确失败。
        src.execute('SELECT count(*) FROM sqlite_master').fetchone()
        dst = sqlite3.connect(str(normalized))
        src.backup(dst)
        dst.commit()
    except sqlite3.Error as exc:
        raise MigrationRefused(
            'untrusted_source_db',
            f'旧任务库副本无法安全读取（{exc}）；源字节已保留，未写入目标'
        ) from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
    # 归一化结果转成回滚日志模式：读它不再需要 -wal/-shm 兄弟文件。
    cleanup = sqlite3.connect(str(normalized))
    try:
        cleanup.execute('PRAGMA journal_mode=DELETE')
    finally:
        cleanup.close()
    return normalized


def _absorb_db_completions(target_db: Path, report: Dict[str, Any]) -> None:
    """只吸收**有日期证据**的旧完成事实（不追加天数、不猜日期）。"""
    from app.core.db import LEDGER_SOURCE_LEGACY, TaskDB

    database = TaskDB(str(target_db))
    absorbed = []
    undated = []
    with database.transaction() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]
        for row in rows:
            days_done = int(row.get('days_done') or 0)
            day = _valid_day(row.get('last_done_date'))
            if days_done >= 1 and day:
                key = (int(row['id']), day)
                if database.lookup_completion(conn, *key) is None:
                    database.insert_completion(conn, {
                        'task_id': key[0], 'business_day': day,
                        'run_id': '', 'source': LEDGER_SOURCE_LEGACY,
                        'receipt': database.completion_receipt(*key),
                        'zone_name': row.get('zone_name') or '',
                        'target_seconds': 0.0, 'effective_seconds': 0.0,
                        'completed_at': None,
                        'committed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'days_done_after': days_done,
                        'total_days_after': int(row.get('total_days') or 0)})
                    absorbed.append({'task_id': key[0], 'business_day': day})
            elif days_done >= 1 or int(row.get('today_done') or 0) == 1:
                # 有完成计数但**没有日期**：保留计数，不追加天数，也不伪造历史日期。
                undated.append({'task_id': int(row['id']),
                                'zone_name': row.get('zone_name') or '',
                                'days_done': days_done,
                                'today_done': row.get('today_done')})
    report['absorbed_completions'] = absorbed
    report['undated_completions'] = undated
    if undated:
        report['limits'].append(
            '旧库里有完成计数但没有日期证据的条目：只保留计数，未追加账本天数，'
            '也未猜测完成日期')


def _ensure_history_baseline(target_db: Path, report: Dict[str, Any]) -> None:
    """只为尚无历史的现有任务补一次现状收录（重复迁移不重复）。"""
    from app.core.db import HISTORY_SOURCE_MIGRATION, TaskDB

    database = TaskDB(str(target_db))
    added = 0
    with database.transaction() as conn:
        known = database.history_task_ids(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]
        for row in rows:
            if int(row['id']) in known:
                continue
            database.insert_history(conn, {
                'task_id': int(row['id']), 'event': 'baseline',
                'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'zone_name': row.get('zone_name') or '', 'area_id': None,
                'category_before': None, 'total_days_before': None,
                'days_done_before': None, 'deadline_before': None,
                'category_after': row.get('category'),
                'total_days_after': row.get('total_days'),
                'days_done_after': row.get('days_done'),
                'deadline_after': row.get('deadline_raw'),
                'source': HISTORY_SOURCE_MIGRATION,
                'detail': '迁移现状收录（旧数据无逐日时间与起始日）'})
            added += 1
    report['history_baseline_added'] = added


# ---------------------------------------------------------------- 映射

@dataclass
class _Mapped:
    snapshot: Snapshot
    mapping: List[str]
    limits: List[str]


def _push_source(settings: Dict[str, Any]) -> str:
    """推流来源按**可靠设置**解释，不从业务模式反推。

    旧档的 ``source_mode`` 描述的是"这次会话怎么来的"（new / resume / reconnect /
    manual），其中 manual 表示"用户手动开的单次场" —— 那是**业务模式**，不是
    "谁在推流"。把 manual 读成 OBS 会把一个明确配置成 FFmpeg 推流的实例迁成
    "等外部推流器"，于是完全没有本地媒体动作（复核 R8）。

    旧档本身没有推流来源字段，唯一可靠来源是实例设置 ``stream_mode``；设置不可
    获得时按本产品的既有默认（本机 FFmpeg 推流）解释，不猜 OBS。
    """
    if str(settings.get('stream_mode') or '') == 'manual':
        return PUSH_OBS
    return PUSH_FFMPEG


#: 交接终态里表示"这一份旧进度**已经被放弃**"的取值。
#:
#: 只有 ``abandoned`` 证明旧进度被放弃。``void`` / ``superseded`` 不是同一个
#: 意思，不能并进来（复核 B1）：旧控制器 ``58af5ab`` 的启动与失败收尾把 ``void``
#: 解释为"这次交接**没有执行**、旧进度从来没被动过"，``superseded`` 是"先前那次
#: 确认被替换"，两者都没有接管、也没有丢弃旧检查点。本仓库既有的
#: ``test_handover_terminal_20260928.py`` 矩阵 1 同样要求 void 之后原检查点
#: 仍可正常恢复。
_ABANDONED_OUTCOMES = ('abandoned',)

#: 其余已经了结、但**不证明**旧进度被放弃的终态（只用于一次性文字说明）。
_OTHER_TERMINAL_OUTCOMES = ('void', 'superseded')


def _handover_targets_run(inputs: _Inputs) -> Optional[str]:
    """旧交接记录**是否指向本档正在保存的那个旧 Run**；是则返回它的 outcome。

    只读 status/outcome 文字不够：一份历史 abandoned 记录可能说的是很久以前的
    另一场。它对**这一次**迁移的意义必须按实际身份核对 —— ``run_id`` 是本场身份，
    必须一致；``task_id`` / ``execution_date`` 两边都有时也必须一致（复核 R6/R7）。
    """
    handover = inputs.handover
    if not isinstance(handover, dict):
        return None
    old = handover.get('old')
    if not isinstance(old, dict):
        return None
    document = inputs.document
    old_run = str(old.get('run_id') or '')
    main_run = str(document.get('run_id') or '')
    if not old_run or not main_run or old_run != main_run:
        return None
    old_task = old.get('task_id')
    main_task = document.get('task_id')
    if _is_int(old_task) and _is_int(main_task) and int(old_task) != int(main_task):
        return None
    old_day = _valid_day(old.get('execution_date'))
    main_day = _valid_day(document.get('execution_date'))
    if old_day and main_day and old_day != main_day:
        return None
    return str(handover.get('outcome') or '') or None


def _stop_intent(inputs: _Inputs) -> Tuple[Optional[str], List[str]]:
    """明确停止输入 → 停止原因；没有明确输入时返回 ``None``。

    明确用户停止、退出意图或停止标记都是**一次性兼容输入**：保留它们表达的意图，
    但不据此授权任何新场。
    """
    notes: List[str] = []
    marker = inputs.marker
    phase = str(inputs.document.get('phase') or '')
    handover = inputs.handover or {}
    outcome = str(handover.get('outcome') or '')

    stopped_reason: Optional[str] = None
    if inputs.face_marker or str(handover.get('reason') or '') == 'face_verify':
        stopped_reason = STOP_FACE_VERIFY
        notes.append('人脸/需人工继续的停止输入保留为 face_verify')
    elif str(handover.get('reason') or '') == 'platform_rejected':
        stopped_reason = STOP_PLATFORM_REJECTED
        notes.append('明确非网络拒绝保留为 platform_rejected')
    if stopped_reason is None and marker:
        stopped_reason = STOP_USER
        notes.append('检测到 .no_auto_resume：保留明确停止意图')
    if stopped_reason is None and phase in _STOPPING_PHASES:
        stopped_reason = STOP_USER
        notes.append('旧 phase=stopping：保留停止意图')
    if outcome in _ABANDONED_OUTCOMES:
        # 旧交接的终态只做一次兼容解释：已放弃的旧进度**不得复活**，但它也不
        # 阻挡普通新开或合法恢复（终态不是永久阻断）。是否指向本旧 Run 由
        # :func:`_handover_targets_run` 按实际身份判定，不只看这几行文字。
        notes.append(f'旧交接终态 {outcome} 只解释一次，不产生新的阻断状态')
    elif outcome in _OTHER_TERMINAL_OUTCOMES:
        # void / superseded 没有证明旧进度被放弃（见 _ABANDONED_OUTCOMES 的说明）：
        # 它们既不复活旧授权，也不丢弃可靠的旧检查点，只说"这次交接了结了"。
        notes.append(f'旧交接终态 {outcome} 未证明旧进度被放弃：'
                     '只做一次性文字说明，不决定检查点去留')
    elif outcome == 'pending':
        # pending 不证明旧任务已放弃，也不证明目标已开播：不重放旧目标。
        notes.append('旧交接 pending 不重放旧目标，也不当作自动接管授权')
    if stopped_reason is not None:
        notes.append('停止输入只产生 Goal=stopped，不自动 Start')
    return stopped_reason, notes


def _map_document(inputs: _Inputs, account: AccountContextView,
                  legacy_db: _LegacyDb, now: datetime) -> _Mapped:
    document = inputs.document
    mapping: List[str] = []
    limits: List[str] = []

    zone = str(document.get('current_zone') or '')
    streaming = bool(document.get('is_streaming', False))
    phase = str(document.get('phase') or '')
    source_mode = str(document.get('source_mode') or '')
    execution_date = _valid_day(document.get('execution_date'))
    last_update = str(document.get('last_update') or '')
    run_id = str(document.get('run_id') or '')
    room_id = document.get('room_id')
    try:
        old_room = int(room_id) if room_id is not None else 0
    except (TypeError, ValueError):
        old_room = 0

    if 'effective_seconds' in document:
        try:
            effective = float(document.get('effective_seconds') or 0.0)
        except (TypeError, ValueError):
            raise MigrationRefused('untrusted_source',
                                   'effective_seconds 不是数字')
        mapping.append('有效秒数沿用 effective_seconds（权威值）')
    else:
        # 旧格式没有有效时长字段：把**合法的**旧 elapsed 作为一次性迁移基线。
        # 整数别名（elapsed/accumulated）**不相加**。
        elapsed = document.get('elapsed_seconds')
        effective = float(elapsed) if _is_int(elapsed) or isinstance(elapsed, float) else 0.0
        if effective < 0:
            raise MigrationRefused('untrusted_source', 'elapsed_seconds 为负数')
        mapping.append('旧格式没有 effective_seconds：把 elapsed_seconds 作为'
                       '一次性迁移基线；整数别名不相加')
    if effective < 0:
        raise MigrationRefused('untrusted_source', '有效秒数为负数')

    try:
        duration = int(document.get('duration_seconds') or 0)
    except (TypeError, ValueError):
        duration = 0
    if 'duration_known' in document:
        duration_known = bool(document.get('duration_known'))
    else:
        duration_known = bool(zone) and duration > 0
    target_seconds = float(duration) if duration_known and duration > 0 else 0.0
    if duration_known and duration > 0:
        mapping.append('固定目标沿用旧 duration_seconds，不重抽')

    task_id = document.get('task_id')
    task_id = int(task_id) if _is_int(task_id) else None
    mode = MODE_SINGLE if source_mode == 'manual' else MODE_TASK
    push_source = _push_source(inputs.settings)
    mapping.append(f'模式按旧 source_mode={source_mode or "(空)"} 解释为 {mode}；'
                   f'推流来源按可靠设置解释为 {push_source}')

    # ---- 身份可靠性 ----
    identity_ok = True
    if old_room and old_room != account.room_id:
        identity_ok = False
        limits.append('旧存档的 room_id 与当前登录上下文不一致：不把旧进度接过来')
    if mode == MODE_TASK and zone:
        row = legacy_db.task(task_id, zone)
        if row is None:
            identity_ok = False
            limits.append('旧任务身份在任务库里找不到对应记录：不把进度接到别的任务上')
        else:
            if task_id is None:
                if legacy_db.count_zone(zone) == 1:
                    task_id = int(row['id'])
                    mapping.append('旧存档缺少 task_id：按唯一同名、同分区窄绑定')
                else:
                    identity_ok = False
                    limits.append('旧存档缺少 task_id 且同名任务不唯一：不做绑定')
            elif str(row.get('zone_name') or '') != zone:
                identity_ok = False
                limits.append('旧 task_id 与分区名不再对应（改名/重建）：不绑定')
    if mode == MODE_TASK and not zone and (streaming or phase in _RUNNING_PHASES):
        # 运行中的任务策略：不把"没有分区"误当成"没有意图"。
        mapping.append('旧档在运行态但没有当前分区：按等待今天任务归并')

    stopped_reason, stop_notes = _stop_intent(inputs)
    mapping.extend(stop_notes)

    # ---- Goal / Run ----
    #
    # 旧的自动任务模式迁入**自动策略**（``SELECT_AUTO``）：旧档里那个 ``task_id``
    # 描述的是"这一场恰好执行的是哪一条"，不是"用户希望从此按这一条播"（旧版本的
    # 任务模式本来就是按排序连续播，没有"指定任务"这个意图）。把它读成"指定任务"
    # 会把后续策略钉死在昨天那一条上（复核 R5）。当天这一场的身份仍然完整地留在
    # Run 里，两者不混。
    selection_kind = SELECT_AUTO
    if mode == MODE_SINGLE:
        selection_kind = SELECT_SINGLE
        task_id = None
    selection = Selection(
        kind=selection_kind, task_id=None,
        zone_name=zone if mode == MODE_SINGLE else '', area_id=None,
        push_source=push_source,
        # 自动策略没有"固定目标"这个属性：固定目标是**本场**的事实（在 Run 上）。
        target_seconds=int(target_seconds) if mode == MODE_SINGLE else 0)

    wants_running = (stopped_reason is None
                     and (streaming or phase in _RUNNING_PHASES))
    business_day = execution_date or _valid_day(last_update) or business_day_of(now)

    has_session = bool(zone) and (streaming or phase in _RUNNING_PHASES
                                  or phase in _STOPPING_PHASES
                                  or effective > 0)
    # 旧计数**已经达到固定目标**、却没有任何可信 End / 已入账日期：这不是
    # "未结束的半场"。迁移不据此授予新的自动完成，也不把这份旧计数重新武装成一次
    # 达标（复核 R4）；运行意图照旧，今天正常走一次新场。
    reached_target = target_seconds > 0 and effective >= target_seconds - 1e-9
    handover_outcome = _handover_targets_run(inputs)
    # 关于**这一份**旧检查点是否还能保留的判断，先独立成形，再让停止/运行意图
    # 各自解释（复核 B1）：交接里已经放弃的那一份旧进度不因为它同时又有一个停止
    # 标记就复活；反过来，void / superseded 没有证明放弃，可靠的同日半场仍按原规则
    # 保留。两件事分开判，不靠分支先后决定。
    checkpoint_abandoned = (handover_outcome
                            if handover_outcome in _ABANDONED_OUTCOMES else None)

    run: Optional[Run] = None
    step = STEP_IDLE
    start_kind = START_NEW

    if has_session and identity_ok:
        same_day = (business_day == business_day_of(now))
        if reached_target:
            limits.append('旧计数已达到固定目标，却没有可信结束决定/已入账日期：'
                          '不据它补一次自动完成，旧检查点也不接续')
            mapping.append('旧计数达标但无可信 End：不补完成、不接续旧检查点，'
                           '运行意图保留，今天走正常新场')
            step = STEP_IDLE
        elif checkpoint_abandoned is not None:
            # 交接已经放弃**这一份**旧进度：它的检查点不得复活，也不重放旧目标；
            # 旧房按既有流程收尾后再按今天取数（终态不是永久阻断）。这一支排在
            # 停止意图之前——停止标记只表达"用户停了"，它不能把一份已经被放弃的
            # 检查点重新变成可恢复进度（复核 B1）。
            step = STEP_CLOSE_QUERY
            mapping.append(f'匹配本旧 Run 的交接 {checkpoint_abandoned}：这一份旧检查点'
                           '已被放弃，不复活、不重放旧目标，收尾后取今天；'
                           '停止/运行意图照旧解释')
        elif stopped_reason is not None:
            if same_day:
                run = _build_run(run_id, account, zone, mode, task_id,
                                 business_day, target_seconds, push_source,
                                 effective)
                mapping.append('同日未结束的场次保留检查点，供**显式**恢复')
            else:
                mapping.append('停止目标下的旧日半场：丢弃无意义秒数，只保留停止意图')
            step = STEP_IDLE
        elif same_day and handover_outcome == 'pending':
            # pending 不证明旧目标已开播，也不授权自动恢复旧场：保留可证明的检查点
            # 供**显式**恢复，但不设"续播"这个自动入口；今天按正常新场走。
            run = _build_run(run_id, account, zone, mode, task_id, business_day,
                             target_seconds, push_source, effective)
            step = STEP_IDLE
            mapping.append('匹配本旧 Run 的交接 pending：保留检查点但不自动恢复原场，'
                           '今天走正常新场')
        elif same_day:
            run = _build_run(run_id, account, zone, mode, task_id, business_day,
                             target_seconds, push_source, effective)
            step = STEP_RESUME_QUERY
            start_kind = START_RESUME
            mapping.append('同日运行中的原场：保留 Run 与检查点，进入原场重新查询')
        else:
            # 旧日半场且没有可信 End：自动丢弃旧 Run，保留运行目标，旧房正常
            # 收尾后再按**今天**取数；不要求任务库先证明旧日已结算。
            step = STEP_CLOSE_QUERY
            mapping.append('旧日半场的运行意图保留，旧 Run 丢弃，先收尾旧房再取今天')
    elif not identity_ok:
        wants_running = False
        step = STEP_IDLE
        business_day = business_day_of(now)
        limits.append('身份不足：归并到 stopped，保留可证明资料并允许之后合法新开')
    else:
        # 没有本程序承担的房间：归并到 stopped，保留可证明资料，允许合法新开。
        step = STEP_IDLE
        business_day = business_day_of(now)

    wanted = WANT_RUNNING if (wants_running and stopped_reason is None) else WANT_STOPPED
    if wanted == WANT_STOPPED and stopped_reason is None and has_session:
        limits.append('无法证明运行意图：归并到 stopped，不自动开播')
    goal = Goal(revision=1, wanted=wanted, mode=mode, selection=selection,
                start_kind=start_kind if wanted == WANT_RUNNING else START_NEW,
                stop_reason=stopped_reason)

    snapshot = Snapshot(goal=goal, run=run, step=step)
    if (wanted == WANT_RUNNING and run is None and mode == MODE_TASK
            and step == STEP_IDLE):
        # 运行中的任务策略：没有可保留的本场时进入"等待今天任务"，
        # 不空查任务库。旧日半场已经安排了 close_query，不被这里改写。
        snapshot.step = STEP_WAIT_TASKS
    return _Mapped(snapshot=snapshot, mapping=mapping, limits=limits)


def _freeze_takeover_guard(mapped: _Mapped, target_dir: Path) -> str:
    """接手时按**任务库副本**冻结这一场本任务的人工进度代数。

    为什么可以在这里冻结：这是一次与"读任务行"同源的**一致性读取**，表达的只是
    "接手这一刻，这条任务已经记录了多少次人工进度改动"。旧库根本没有这个键，
    所以合理的值是 0（"没有记录过人工改动"），而不是"未知"。

    为什么必须有：`None` 会被结算端解释成"这份快照从未冻结过凭据"，于是这一场
    将来自然达标时会落成 `not_applied:no_guard` —— 一次**合法**完成被丢掉。
    冻结 0 不复活任何旧授权（旧档从来没有完成决定），却让"接手之后的人工修改"
    能按 §3.2 正常让旧完成失效。提交那一刻**仍然**不会读当前值补。

    只对**确实要接续的、同日未结束的半场**做 —— 也就是 :func:`_map_document`
    真的留下了 ``run`` 的那几种情形。已经达标却没有可信 End 的旧计数、被交接
    明确**放弃**（``abandoned``）的旧进度都不会走到这里（它们没有 Run），因此不会
    凭空得到一次自动完成的授权（复核 R4/R7）；``void`` / ``superseded`` 没有证明
    放弃，留下的可靠半场照原规则冻结凭据（复核 B1）。
    """
    run = mapped.snapshot.run
    if run is None or run.task_id is None:
        return ''
    path = target_dir / DB_FILE
    if not path.is_file():
        return ''
    from app.core.db import TaskDB

    try:
        database = TaskDB(str(path))
        with database._get_conn() as conn:
            run.settlement_guard = int(database.read_progress_guard(conn, run.task_id))
    except Exception as exc:                      # pragma: no cover - 防御
        mapped.limits.append(f'无法读取旧库的人工进度代数（{exc}）：这一场保持无凭据')
        return ''
    return (f'接手时按旧库现状冻结人工进度代数 = {run.settlement_guard}'
            f'（保护接手之后的人工修改；不复活任何旧授权）')


def _build_run(run_id: str, account: AccountContextView, zone: str, mode: str,
               task_id: Optional[int], business_day: str, target_seconds: float,
               push_source: str, effective: float) -> Run:
    """重建本场：只带可信检查点，不带旧单调坐标与离线秒数。"""
    return Run(
        id=run_id or f'migrated-{business_day}-{zone}',
        uid=int(account.uid),
        room_id=int(account.room_id),
        area_id=None,
        zone_name=zone,
        mode=mode,
        task_id=task_id if mode == MODE_TASK else None,
        business_day=business_day,
        target_seconds=float(target_seconds),
        selected_by_revision=0,
        push_source=push_source,
        settlement_guard=None,     # 旧档从未记录过人工进度凭据
        clock=PlatformClock(checkpoint_seconds=max(0.0, float(effective)),
                            interval_start_mono=None,
                            interval_start_wall=None,
                            projecting=False,
                            display_anchor_mono=None),
        end=None)


# ---------------------------------------------------------------- 入口

def migrate_legacy_state(*, source_dir, target_dir, account,
                         settings: Optional[Dict[str, Any]] = None,
                         now: Optional[datetime] = None) -> Dict[str, Any]:
    """把一份旧状态**副本**迁成新模型的隔离目录；返回迁移报告。

    ``account`` 可以是 ``{'uid':..,'room_id':..}`` 或任何带 ``uid``/``room_id``
    属性的对象。``now`` 只影响"业务日"的判断，测试可注入。
    """
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    _check_directories(source_dir, target_dir)
    now = now or datetime.now()
    account_view = _account_view(account)

    report: Dict[str, Any] = {
        'migration_version': MIGRATION_VERSION,
        'status': STATUS_REFUSED,
        'source_dir': str(source_dir),
        'target_dir': str(target_dir),
        'mapping': [],
        'limits': [],
        'notes': [],
    }
    if account_view.room_id <= 0:
        # 没有可核对的登录上下文时，无法判断旧进度是不是这个账号/房间的，
        # 也就不该把它接过来 —— 明确拒绝，不靠默认值蒙混。
        report['reason'] = 'account_required'
        report['detail'] = '迁移必须给出登录上下文（uid/room_id）才能核对身份归属'
        return report

    # ---- 目标入口：一律走严格读取决定"有没有可信新权威" ----
    #
    # 不能拿"主档是否存在"或"能否普通 json.loads"当判据：主档丢失、只剩一份
    # 可信 ``.bak`` 时，SessionStore 明确读到 backup，而 exists() 会说不存在，
    # 于是旧源把一份**较新的**意图覆盖掉（复核 C1）。这里复用与运行期同一个
    # 判定：可信（含仅备份可信）→ 已有权威，字节一律不动；有资料但不可信 →
    # 拒绝覆盖；两份都没有 → 首次启动，才允许准备迁移。
    target_state = target_dir / STATE_FILE
    target_loaded = SessionStore(target_state).load()
    if target_loaded.status == VERDICT_TRUSTED:
        # 合法新快照是**唯一**意图权威：不拿旧源覆盖它。
        report['status'] = STATUS_ALREADY_NEW
        report['notes'].append(
            '目标已有可信新快照（含仅备份可信），无需旧格式转换')
        return report
    if target_loaded.status == VERDICT_UNTRUSTED:
        raise MigrationRefused(
            'target_untrusted',
            '目标目录已有状态资料但严格判定不可信（'
            f'{target_loaded.status}：{target_loaded.error or "未知"}）；'
            '原字节已保留，拒绝覆盖')

    # ---- 源入口：严格读取先于"解析失败即拒绝" ----
    #
    # 源自己就是可信新快照时（主档坏/缺但 ``.bak`` 可信也算），本就没有旧格式
    # 可迁，直接 already_new；字节不动。关键是这一步**不能**晚于普通
    # ``json.loads`` 的失败判定：源主档解析不了并不等于"没有可信新权威"，那正好
    # 跳过了 SessionStore 认可的最后可用副本（复核 C1）。
    try:
        inputs = _load_inputs(source_dir, settings)
    except MigrationRefused as refused:
        loaded = SessionStore(source_dir / STATE_FILE).load()
        if loaded.status == VERDICT_TRUSTED:
            report['status'] = STATUS_ALREADY_NEW
            report['notes'].append('源主档不可用，但严格读取回退到最后可用副本：'
                                   '本身已是可信新格式，无需转换')
            return report
        report['status'] = STATUS_REFUSED
        report['reason'] = refused.reason
        report['detail'] = refused.detail
        return report

    report['source'] = {'file': STATE_FILE, 'sha256': inputs.state_sha,
                        'bytes': len(inputs.state_text.encode('utf-8'))}

    kind = detect_format(inputs.document)
    report['source']['kind'] = kind
    report['source']['schema_version'] = inputs.document.get('schema_version')
    if kind != 'legacy_livestate':
        # **只有**已识别的合法旧格式才走显式兼容转换。其余（未知 / 新旧混合 /
        # 自相矛盾的新格式）一律再问一次严格读取：可信新快照直接 already_new，
        # 有资料但不可信则保留原字节并拒绝，不降级解释。
        loaded = SessionStore(source_dir / STATE_FILE).load()
        if loaded.status == VERDICT_TRUSTED:
            report['status'] = STATUS_ALREADY_NEW
            report['notes'].append('源本身就是可信新格式（含仅备份可信），无需转换')
            return report
        report['status'] = STATUS_REFUSED
        report['reason'] = 'untrusted_source'
        report['detail'] = f'无法识别的状态格式（{kind}）；原字节已保留，未写入'
        return report

    target_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='bls-migrate-stage-'))
    try:
        # ---- 1) 先把旧库读成一份**归一化**事实源（源目录只读字节） ----
        source_db = source_dir / DB_FILE
        report['source']['db_sha256'] = (
            _sha256_file(source_db) if source_db.is_file() else '')
        try:
            normalized = _stage_database(source_dir, staging)
        except MigrationRefused as refused:
            report['status'] = STATUS_REFUSED
            report['reason'] = refused.reason
            report['detail'] = refused.detail
            return report
        legacy_db = _LegacyDb(normalized, report['source']['db_sha256'])

        mapped = _map_document(inputs, account_view, legacy_db, now)
        report['mapping'] = list(mapped.mapping)
        report['limits'] = list(mapped.limits)

        # ---- 2) 目标库来自**同一份**归一化结果 ----
        if legacy_db.available:
            shutil.copy2(normalized, target_dir / DB_FILE)
            report['db'] = {
                'path': DB_FILE,
                'source_sha256': report['source']['db_sha256'],
                'staged_sha256': _sha256_file(normalized)}

        # ---- 3) 接手时冻结人工进度凭据（只在要接续的同日未结束半场） ----
        guard_note = _freeze_takeover_guard(mapped, target_dir)
        if guard_note:
            report['mapping'].append(guard_note)

        # ---- 4) 数据库侧的全部准备：吸收已有完成事实、收录现状历史 ----
        # 这些必须在**发布新快照之前**完成：新 JSON 一旦写下去就是"唯一意图
        # 权威"，重跑会因为 already_new 直接返回，于是中断后永远补不上（复核 R10）。
        # 反过来，没有合法新快照时重跑可以安全地重做全部准备（幂等）。
        if legacy_db.available:
            _absorb_db_completions(target_dir / DB_FILE, report)
            _ensure_history_baseline(target_dir / DB_FILE, report)
            # 目标库的全部写入都已完成，这时才算它的最终指纹。
            report['db']['sha256'] = _sha256_file(target_dir / DB_FILE)
            report['db']['bytes'] = (target_dir / DB_FILE).stat().st_size

        # ---- 5) 最后发布可信新快照，并严格重读自证 ----
        store = SessionStore(target_dir / STATE_FILE)
        if not store.save(mapped.snapshot):
            raise MigrationRefused('target_write_failed',
                                   store.last_error or '未知写入失败')
        reloaded = store.load()
        if reloaded.status != VERDICT_TRUSTED:   # 自证：写出的必须是**可信**快照
            raise MigrationRefused('target_verify_failed', reloaded.error)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    report['status'] = STATUS_MIGRATED
    report['goal'] = mapped.snapshot.goal.to_dict()
    report['run'] = (mapped.snapshot.run.to_dict()
                     if mapped.snapshot.run else None)
    report['step'] = mapped.snapshot.step
    report['state'] = {'file': STATE_FILE,
                       'sha256': _sha256_file(target_dir / STATE_FILE)}
    report['limits'].append('迁移报告只是来源记录，不是第二份运行时授权文件')
    return report
