"""
db.py - SQLite 数据库层

替代 Excel 作为任务数据的主存储。
提供完整的 CRUD、迁移、导入导出能力。
"""

import sqlite3
import json
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

def _default_db_path():
    from app.core.config import get_data_path
    return get_data_path("live_tasks.db")

DEFAULT_DB_PATH = None  # 延迟初始化，避免模块加载时 config 未初始化


class ProgressGuardError(RuntimeError):
    """人工进度凭据**存在但不可用**（损坏 / 非法 / SQL 读取失败）。

    "没有这个键"与"这个键坏了"是两件事：前者是旧库的兼容默认（代数 0），
    后者必须让调用方**明确失败并回滚**，不能被读成"从没人工改过"。
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ==================== Schema ====================

CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
id INTEGER PRIMARY KEY AUTOINCREMENT,
zone_name TEXT NOT NULL UNIQUE,
category INTEGER NOT NULL DEFAULT 1,
total_days INTEGER NOT NULL DEFAULT 1,
days_done INTEGER NOT NULL DEFAULT 0,
deadline_raw TEXT NOT NULL DEFAULT '',
today_done INTEGER DEFAULT NULL,
remaining_days INTEGER NOT NULL DEFAULT 1,
-- 计算列（排序用，由 _compute_static 更新）
a_val INTEGER NOT NULL DEFAULT 9999,
i_val INTEGER NOT NULL DEFAULT 0,
j_val INTEGER NOT NULL DEFAULT -1,
created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

CREATE_INDEX_ZONE = """
CREATE INDEX IF NOT EXISTS idx_tasks_zone ON tasks(zone_name);
"""

CREATE_INDEX_PRIORITY = """
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(a_val, category);
"""

#: 版本元数据表。放在**同一个库**里，才能与任务变更同事务提交；独立 JSON 文件
#: 做不到（写入失败后重启会让版本退回、旧覆盖确认重新有效）。
CREATE_TASKS_META = """
CREATE TABLE IF NOT EXISTS tasks_meta (
key TEXT PRIMARY KEY,
value TEXT NOT NULL
);
"""

#: 版本元数据的键。
META_REVISION_KEY = 'tasks_revision'

#: 账号库归属登记键（M0A）：该库属于哪个 UID。
#:
#: 它与任务版本同住 ``tasks_meta``，因此"这份库是谁的"和任务数据在**同一份
#: SQLite** 里，不需要第二份权威文件；写入走本表既有的 OR REPLACE 能力，不新增
#: 表、不改既有列。**只登记**归属，不参与任何排序/结算/日切判据。
META_ACCOUNT_OWNER_KEY = 'account_owner_uid'

#: **创建**操作回执在 tasks_meta 里的键前缀（M1）。
#:
#: 与导入回执 ``task_import:`` 同表不同键空间：一次创建留一行已提交事实，
#: 供回包丢失后按同一令牌取回原结果。任务创建是人手驱动的低频操作，因此这里
#: **不设容量上限** —— 导入回执的 ``_IMPORT_OPERATION_MAX`` 是"防重放的容量
#: 预算"，把它复制过来会让普通创建变成有终身次数上限的操作（交办 §5.2）。
META_CREATE_OPERATION_PREFIX = 'task_create:'

#: 逐任务**人工进度变更代数**在 tasks_meta 里的键前缀。
#:
#: 它与 ``tasks_revision`` 是两件事：版本覆盖全库（日切、别的任务、派生值重算
#: 都会前进），拿它当"这个任务的进度有没有被人工改过"的凭据会误伤合法完成。
#: 这个代数只在该任务的**进度相关事实**真的被改动时前进，且与任务变更同事务。
META_PROGRESS_GUARD_PREFIX = 'task_progress_guard:'

#: 完成账本：``(task_id, business_day)`` 最多一行。
#:
#: 承重的是**数据库级唯一约束**，不是 Python 查重或一把实例内锁：两个独立连接
#: 争抢同一天的首次提交也最多 +1。记录是**已落定的事实**（业务日、稳定回执、
#: 来源、达标确认时间、入账时间、本次计入后的进度），不是待执行命令，因此没有
#: pending/发送/重试状态。它不随任务删除级联消失，也不随日切/重启/导入被清掉。
CREATE_TASK_COMPLETION_LEDGER = """
CREATE TABLE IF NOT EXISTS task_completion_ledger (
id INTEGER PRIMARY KEY AUTOINCREMENT,
task_id INTEGER NOT NULL,
business_day TEXT NOT NULL,
run_id TEXT NOT NULL DEFAULT '',
receipt TEXT NOT NULL,
source TEXT NOT NULL DEFAULT 'auto',
zone_name TEXT NOT NULL DEFAULT '',
area_id INTEGER,
target_seconds REAL NOT NULL DEFAULT 0,
effective_seconds REAL NOT NULL DEFAULT 0,
completed_at TEXT,
committed_at TEXT NOT NULL,
days_done_after INTEGER NOT NULL DEFAULT 0,
total_days_after INTEGER NOT NULL DEFAULT 0,
UNIQUE (task_id, business_day)
);
"""

CREATE_INDEX_LEDGER_DAY = """
CREATE INDEX IF NOT EXISTS idx_ledger_business_day
ON task_completion_ledger(business_day);
"""

#: 窄任务历史：只记录**事实**（创建、参数变更、人工进度调整、播满、不可完成移出、
#: 删除），没有 pending/处理/重试状态，不参与任何直播决策。
#:
#: 与 tasks 表**没有外键**：任务删除不得带走历史。前后快照至少保留分区、每日要求
#: 时长（category）、总天数、当时进度与截止日；任务起始日没有可靠来源，不伪造。
CREATE_TASK_HISTORY = """
CREATE TABLE IF NOT EXISTS task_history (
id INTEGER PRIMARY KEY AUTOINCREMENT,
task_id INTEGER NOT NULL,
event TEXT NOT NULL,
at TEXT NOT NULL,
zone_name TEXT NOT NULL DEFAULT '',
area_id INTEGER,
category_before INTEGER,
total_days_before INTEGER,
days_done_before INTEGER,
deadline_before TEXT,
category_after INTEGER,
total_days_after INTEGER,
days_done_after INTEGER,
deadline_after TEXT,
source TEXT NOT NULL DEFAULT '',
detail TEXT NOT NULL DEFAULT ''
);
"""

CREATE_INDEX_HISTORY_TASK = """
CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id);
"""

#: 分区历史入口按**分区快照**关联历次 task_id（M1）。历史表与账本都保留
#: 事件发生时的分区名，因此这是"该分区的历次身份"的可靠来源，不靠名字模糊匹配。
CREATE_INDEX_HISTORY_ZONE = """
CREATE INDEX IF NOT EXISTS idx_task_history_zone ON task_history(zone_name);
"""

CREATE_INDEX_LEDGER_ZONE = """
CREATE INDEX IF NOT EXISTS idx_ledger_zone ON task_completion_ledger(zone_name);
"""

#: 账本与历史里出现的来源标记。
LEDGER_SOURCE_AUTO = 'auto'                    # 新核心的可信 End（唯一自动入口）
LEDGER_SOURCE_LEGACY = 'legacy_observed'       # 迁移时吸收的、有日期证据的旧完成
LEDGER_SOURCE_MANUAL = 'mark_done'             # 既有"标记今日完成"入口（人工确认）

HISTORY_SOURCE_USER = 'user'
HISTORY_SOURCE_IMPORT = 'import'
HISTORY_SOURCE_AUTO = 'auto'
HISTORY_SOURCE_NEGATIVE = 'negative_priority'
HISTORY_SOURCE_MIGRATION = 'migration'


class TaskDB:
    """SQLite 任务数据库管理器（线程安全）"""

    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = Path(db_path).resolve()
        else:
            self.db_path = _default_db_path().resolve()
        self._lock = threading.Lock()
        self._init_db()

    # ==================== 初始化 ====================

    def _init_db(self):
        """创建表和索引，自动迁移旧 schema"""
        with self._get_conn() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(tasks)").fetchall()]
            if 'extra_hours' in cols or 'priority' in cols:
                logger.info("检测到旧版 schema，正在迁移...")
                old_data = []
                if cols:
                    old_data = [dict(r) for r in conn.execute(
                        "SELECT * FROM tasks").fetchall()]
                conn.execute("DROP TABLE IF EXISTS tasks")
                conn.execute(CREATE_TASKS_TABLE)
                for row in old_data:
                    conn.execute(
                        """INSERT INTO tasks
                        (zone_name, category, total_days, days_done,
                        deadline_raw, today_done, remaining_days,
                        a_val, i_val, j_val)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row.get('zone_name', ''),
                            row.get('category', 1),
                            row.get('total_days', 1),
                            row.get('days_done', 0),
                            row.get('deadline_raw', ''),
                            row.get('today_done'),
                            row.get('remaining_days', 1),
                            row.get('a_val', 9999),
                            row.get('i_val', 0),
                            row.get('j_val', -1),
                        )
                    )
                conn.commit()
                logger.info(f"迁移完成，已恢复 {len(old_data)} 条任务")
            else:
                conn.execute(CREATE_TASKS_TABLE)
                conn.execute(CREATE_INDEX_ZONE)
                conn.execute(CREATE_INDEX_PRIORITY)
                conn.commit()
            # 追加列的轻量迁移：只加可空列，不重解释既有数据。
            self._ensure_columns(conn)
            # 版本元数据表（旧库升级路径：CREATE TABLE IF NOT EXISTS）。
            conn.execute(CREATE_TASKS_META)
            # 阶段 2 的两张窄表：同样只追加，不动既有数据、不 DROP 任何列。
            conn.execute(CREATE_TASK_COMPLETION_LEDGER)
            conn.execute(CREATE_INDEX_LEDGER_DAY)
            conn.execute(CREATE_TASK_HISTORY)
            conn.execute(CREATE_INDEX_HISTORY_TASK)
            conn.execute(CREATE_INDEX_HISTORY_ZONE)
            conn.execute(CREATE_INDEX_LEDGER_ZONE)
            conn.commit()
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(tasks)").fetchall()]
            logger.info(f"数据库就绪：{self.db_path}（列：{len(cols)}）")

    def _ensure_columns(self, conn) -> None:
        """补齐后加的可空列（旧库升级路径，全部默认 NULL/默认值）。

        last_done_date：最近一次"按日结算"的业务日期。仅用于防止旧结算落到
        已经结算过同一天的新记录上；为 NULL 的旧数据不受约束（不重解释既有
        结算、不重置用户任务）。
        """
        existing = {r[1] for r in conn.execute(
            "PRAGMA table_info(tasks)").fetchall()}
        for column, ddl in (
            ('last_done_date', 'ALTER TABLE tasks ADD COLUMN last_done_date TEXT'),
        ):
            if column not in existing:
                conn.execute(ddl)
                logger.info(f"已为 tasks 表补充列：{column}")
        conn.commit()

    # ==================== 版本元数据（与 tasks 同库） ====================

    #: 参与指纹的字段。**不**含 updated_at/created_at：它们是"何时写的"，
    #: 不是"写成什么"。重算派生值/重启都会刷新它们，算进去会让"数据没变"的
    #: 重启也被当成一次变更，把用户的合法确认一并作废。
    FINGERPRINT_FIELDS = (
        'id', 'zone_name', 'category', 'total_days', 'days_done',
        'deadline_raw', 'today_done', 'remaining_days', 'last_done_date',
        'a_val', 'i_val', 'j_val',
    )

    @staticmethod
    def fingerprint_rows(rows: List[Dict[str, Any]]) -> str:
        """对任务行取确定性指纹（覆盖所有有意义的状态，见 FINGERPRINT_FIELDS）。

        排序按 ``id`` 而不是 SQL 的返回顺序：``ORDER BY a_val`` 在并列时顺序
        不确定，直接哈希会把"同一份数据"算成两个指纹。
        """
        ordered = sorted(rows, key=lambda r: (str(r.get('id') or ''),
                                              str(r.get('zone_name') or '')))
        payload = json.dumps(
            [{k: r.get(k) for k in TaskDB.FINGERPRINT_FIELDS} for r in ordered],
            ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def read_revision_meta(self, conn=None):
        """读版本元数据，返回 ``(revision, fingerprint)``；不可用则为 (None, None)。"""
        if conn is None:
            with self._get_conn() as own:
                return self._read_revision_meta(own)
        return self._read_revision_meta(conn)

    def _read_revision_meta(self, conn):
        try:
            row = conn.execute(
                "SELECT value FROM tasks_meta WHERE key = ?",
                (META_REVISION_KEY,)).fetchone()
        except sqlite3.Error as e:
            logger.error(f"读取版本元数据失败：{e}")
            return None, None
        if not row:
            return None, None
        try:
            data = json.loads(row['value'])
            return int(data['revision']), str(data.get('fingerprint') or '')
        except Exception as e:
            logger.error(f"版本元数据损坏：{e}")
            return None, None

    def write_revision_meta(self, conn, revision: int, fingerprint: str) -> None:
        """写版本元数据。**必须在任务变更的同一个事务连接里调用**。"""
        conn.execute(
            "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
            (META_REVISION_KEY, json.dumps({
                'revision': int(revision),
                'fingerprint': fingerprint,
                'updated_at': datetime.now().isoformat(),
            }, ensure_ascii=False)))

    def commit_revision_meta(self, revision: int, fingerprint: str) -> bool:
        """独立（非任务变更）写入一次版本元数据，返回是否成功。

        只在读取路径对齐版本时用：失败不影响本次读取——版本本身是由数据推导
        出来的，下次启动会重新对齐。
        """
        try:
            with self._get_conn() as conn:
                self.write_revision_meta(conn, revision, fingerprint)
                conn.commit()
            return True
        except Exception as e:
            logger.warning(f"保存版本元数据失败（下次启动按数据重新对齐）：{e}")
            return False

    # ==================== 账号库归属（与任务数据同库） ====================

    def claim_account_owner(self, uid: int) -> None:
        """把这份库登记为某个 UID 的账号资料。

        只写一条元数据并提交：不改任务行、不建表、不动历史与账本。调用方
        （``session_runtime.accounts``）已经**只读地**核验过形状与既有归属，
        这里只负责落登记。
        """
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
                (META_ACCOUNT_OWNER_KEY, str(int(uid))))
            conn.commit()

    # ==================== 逐任务人工进度代数（与 tasks 同库、同事务） ====================

    @staticmethod
    def _progress_guard_key(task_id) -> str:
        return f'{META_PROGRESS_GUARD_PREFIX}{int(task_id)}'

    def read_progress_guard(self, conn, task_id) -> int:
        """读该任务的**人工进度变更代数**。

        三种情形**必须**互相区分（复核 R2）：

        - **键不存在**（旧库 / 旧版本从未写过这个键）→ 返回 0，等价于
          "没有记录过人工改动"，这是唯一的兼容默认值；
        - **键存在但不合法**（JSON 坏了、不是整数、负数、读不出 guard）→
          抛 :class:`ProgressGuardError`，绝不静默降级成 0 —— 把损坏的凭据读成
          "从没改过"正好会让一份人工更正之后的旧自动完成重新生效；
        - **SQL 读取失败** → 同样抛 :class:`ProgressGuardError`（读不出事实
          不等于"事实是 0"）。

        "是整数"按 JSON 类型判定，**不做数字转换**：``0.5`` 被 ``int()`` 截成
        0 之后，一份损坏的凭据就重新变成了一次合法授权（复核 A2）。
        ``True``/``False`` 在 JSON 里是布尔值，同样不是代数；字符串也不强制
        转换。只有真正的非负整数（以及"键不存在"这个唯一的兼容默认）有效。

        读取路径是只读的：不存在的键**不会**被建出来，因此"缺凭据"与
        "凭据为 0"不会被写成同一件事的两种说法。
        """
        if task_id is None:
            raise ProgressGuardError('no_task_identity',
                                     '没有任务身份，无法读取人工进度代数')
        try:
            row = conn.execute(
                "SELECT value FROM tasks_meta WHERE key = ?",
                (self._progress_guard_key(task_id),)).fetchone()
        except sqlite3.Error as e:
            raise ProgressGuardError('guard_read_failed', str(e)) from e
        if row is None:
            return 0
        raw = row['value']
        try:
            guard = json.loads(raw)['guard']
        except Exception as e:
            raise ProgressGuardError('guard_corrupt', repr(raw)) from e
        if isinstance(guard, bool) or not isinstance(guard, int) or guard < 0:
            raise ProgressGuardError('guard_corrupt', repr(raw))
        return guard

    def bump_progress_guard(self, conn, task_id) -> int:
        """在同一事务里推进该任务的人工进度代数，返回新值。

        只有**真实的人工进度改动**才调用它；自然结算靠账本防重，不冒充人工更正
        （否则同一天的另一笔合法完成会被误伤）。

        现存凭据损坏时**明确失败**（异常沿调用方的事务回滚），不 `INSERT OR
        REPLACE` 把它覆盖成"从 1 开始"：那样等于用一次编辑抹掉"这里曾经有过
        一份读不出来的凭据"，之后任何旧确认都会被重新授权。
        """
        current = self.read_progress_guard(conn, task_id)
        value = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
            (self._progress_guard_key(task_id),
             json.dumps({'guard': value,
                         'updated_at': datetime.now().isoformat()},
                        ensure_ascii=False)))
        return value

    # ==================== 完成账本 ====================

    @staticmethod
    def lookup_completion(conn, task_id, business_day) -> Optional[Dict[str, Any]]:
        """查该 (task_id, business_day) 是否已经记过。**只读**，不做任何写。"""
        row = conn.execute(
            "SELECT * FROM task_completion_ledger WHERE task_id = ? "
            "AND business_day = ?", (int(task_id), str(business_day))).fetchone()
        return dict(row) if row else None

    @staticmethod
    def insert_completion(conn, record: Dict[str, Any]) -> None:
        """写入一条完成事实。唯一约束由数据库承重，不在 Python 里查重。"""
        conn.execute(
            """INSERT INTO task_completion_ledger
            (task_id, business_day, run_id, receipt, source, zone_name, area_id,
             target_seconds, effective_seconds, completed_at, committed_at,
             days_done_after, total_days_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(record['task_id']), str(record['business_day']),
             str(record.get('run_id') or ''), str(record['receipt']),
             str(record.get('source') or LEDGER_SOURCE_AUTO),
             str(record.get('zone_name') or ''), record.get('area_id'),
             float(record.get('target_seconds') or 0.0),
             float(record.get('effective_seconds') or 0.0),
             record.get('completed_at'), str(record['committed_at']),
             int(record.get('days_done_after') or 0),
             int(record.get('total_days_after') or 0)))

    @staticmethod
    def completion_receipt(task_id, business_day) -> str:
        """稳定回执：同一个 (task_id, business_day) 在任何进程/重启后都一样。"""
        return f'L{int(task_id)}-{str(business_day)}'

    # ==================== 任务历史 ====================

    @staticmethod
    def insert_history(conn, record: Dict[str, Any]) -> None:
        """写一条任务历史事实。与它所属的任务变更**同事务**提交或回滚。"""
        conn.execute(
            """INSERT INTO task_history
            (task_id, event, at, zone_name, area_id,
             category_before, total_days_before, days_done_before, deadline_before,
             category_after, total_days_after, days_done_after, deadline_after,
             source, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(record['task_id']), str(record['event']), str(record['at']),
             str(record.get('zone_name') or ''), record.get('area_id'),
             record.get('category_before'), record.get('total_days_before'),
             record.get('days_done_before'), record.get('deadline_before'),
             record.get('category_after'), record.get('total_days_after'),
             record.get('days_done_after'), record.get('deadline_after'),
             str(record.get('source') or ''), str(record.get('detail') or '')))

    @staticmethod
    def history_task_ids(conn) -> set:
        """已经有历史记录的 task_id 集合（"现状收录"只做一次的判据）。"""
        return {int(r['task_id']) for r in conn.execute(
            "SELECT DISTINCT task_id FROM task_history").fetchall()}

    @staticmethod
    def completion_ledger_task_ids(conn) -> set:
        return {int(r['task_id']) for r in conn.execute(
            "SELECT DISTINCT task_id FROM task_completion_ledger").fetchall()}

    @staticmethod
    def read_meta_json(conn, key: str) -> Optional[dict]:
        """按元数据键读一份 JSON 事实；键不存在返回 None。

        **键不存在**与**键存在但读不出来**必须分开：前者是"没有这件事"（正常），
        后者交给调用方明确失败，绝不静默当成"没有"。
        """
        row = conn.execute(
            "SELECT value FROM tasks_meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row['value'])

    @staticmethod
    def write_meta_json(conn, key: str, record: dict) -> None:
        """在**调用方的事务连接**里写一份 JSON 事实（不自行提交）。"""
        conn.execute(
            "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
            (key, json.dumps(record, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str)))

    # ==================== 分区历史（只读窄查询） ====================

    @staticmethod
    def zone_history_task_ids(conn, zone_name: str) -> List[int]:
        """该分区关联的**历次** task_id：当前行 ∪ 历史 ∪ 每日账本。

        每个来源都带各自的可靠分区快照：历史行记的是事件发生时的分区名（改名前的
        旧名会留在更早的事件里），账本记的是入账时的分区名，当前行记的是现状。
        不按名字模糊匹配，也不合并别的账号的同数值 id（各账号一份库）。
        """
        ids = set()
        for sql, args in (
                ("SELECT DISTINCT task_id FROM task_history WHERE zone_name = ?",
                 (zone_name,)),
                ("SELECT DISTINCT task_id FROM task_completion_ledger "
                 "WHERE zone_name = ?", (zone_name,)),
                ("SELECT id FROM tasks WHERE zone_name = ?", (zone_name,))):
            for row in conn.execute(sql, args).fetchall():
                if row[0] is not None:
                    ids.add(int(row[0]))
        return sorted(ids)

    @staticmethod
    def history_rows_for_tasks(conn, task_ids: List[int]) -> List[dict]:
        if not task_ids:
            return []
        marks = ','.join('?' for _ in task_ids)
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM task_history WHERE task_id IN ({marks}) ORDER BY id",
            [int(i) for i in task_ids]).fetchall()]

    @staticmethod
    def ledger_rows_for_tasks(conn, task_ids: List[int]) -> List[dict]:
        if not task_ids:
            return []
        marks = ','.join('?' for _ in task_ids)
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM task_completion_ledger WHERE task_id IN ({marks}) "
            f"ORDER BY id", [int(i) for i in task_ids]).fetchall()]

    @contextmanager
    def _get_conn(self):
        """获取数据库连接（线程安全）"""
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """跨多步写入的单连接事务。

        `_get_conn` 每次调用开一个新连接并提交，只能保护"单条 SQL 原子"。
        任务完成这类操作是"改原字段 → 重算派生值 → 发布快照"的序列，中间任何
        一步失败都会留下半完成状态；因此这类序列必须共用一个连接、一次提交。

        异常时回滚：已提交的内容不会被"补写一半"破坏，磁盘保持上一个完整状态。
        """
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def coherent_read(self):
        """一个**真正的一致性读取**：同一连接、同一读快照里读任务行与人工代数。

        为什么不能只"把两条 SELECT 放到同一连接上"：Python 的 ``sqlite3`` 默认
        不为 SELECT 开事务，两条语句各自取一次快照，中间可以被另一个连接（另一个
        TaskManager / 另一个实例的进程）改写。这里显式 ``BEGIN`` 一个 deferred 读
        事务 —— WAL 模式下第一条 SELECT 建立快照，后续读取都看同一份数据。

        只读：结束一律 ROLLBACK，不写任何库。
        """
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            yield conn
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()

    @staticmethod
    def rows_from_conn(conn) -> List[Dict[str, Any]]:
        """在给定连接内读取全表（保证与同一事务内的写入一致）。"""
        rows = conn.execute("SELECT * FROM tasks ORDER BY a_val ASC").fetchall()
        return [dict(r) for r in rows]

    # ==================== 基础 CRUD ====================

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY a_val ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_task_by_zone(self, zone_name: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE zone_name = ?", (zone_name,)
            ).fetchone()
            return dict(row) if row else None

    def get_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def insert_task(self, task_data: dict) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO tasks
                (zone_name, category, total_days, days_done, deadline_raw,
                today_done, remaining_days)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_data['zone_name'],
                    task_data.get('category', 1),
                    task_data.get('total_days', 1),
                    task_data.get('days_done', 0),
                    task_data.get('deadline_raw', ''),
                    task_data.get('today_done'),
                    task_data.get('remaining_days', 1),
                )
            )
            conn.commit()
            return cursor.lastrowid

    def update_task(self, zone_name: str, updates: dict) -> bool:
        allowed = {
            'category', 'total_days', 'days_done', 'deadline_raw',
            'today_done', 'remaining_days',
            'a_val', 'i_val', 'j_val'
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        filtered['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        set_clause = ', '.join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [zone_name]
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE zone_name = ?", values
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_task_by_id(self, task_id: int, updates: dict) -> bool:
        allowed = {
            'zone_name', 'category', 'total_days', 'days_done', 'deadline_raw',
            'today_done', 'remaining_days',
            'a_val', 'i_val', 'j_val'
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        filtered['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        set_clause = ', '.join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [task_id]
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?", values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_task(self, zone_name: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM tasks WHERE zone_name = ?", (zone_name,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_task_by_id(self, task_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM tasks WHERE id = ?", (task_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def clear_today_done(self):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET today_done = NULL, updated_at = ?",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
            )
            conn.commit()
            logger.info("已清空 today_done 标志")

    def update_static_values(self, computed: List[dict]):
        with self._get_conn() as conn:
            for item in computed:
                conn.execute(
                    """UPDATE tasks
                    SET a_val = ?, i_val = ?, j_val = ?,
                    updated_at = ?
                    WHERE zone_name = ?""",
                    (
                        item['a'], item['i'], item['j'],
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        item['zone_name'],
                    )
                )
            conn.commit()
            logger.info(f"静态值已更新（{len(computed)} 行）")

    def update_task_conditional(self, zone_name: str, updates: dict,
                                expect: Optional[dict] = None) -> bool:
        """带前置条件的更新：条件不匹配则一行都不改。

        用于给"结算"这类并发写加上执行身份约束——条件里带 task_id 与执行日，
        任务被删除后重建的同名记录不会匹配旧条件，旧结算就落不到新任务上。
        返回 False 既可能是"条件不匹配"，也可能是"没有该任务"，调用方按
        "未提交"处理，不得当作成功。
        """
        allowed = {
            'category', 'total_days', 'days_done', 'deadline_raw',
            'today_done', 'remaining_days',
            'a_val', 'i_val', 'j_val'
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        filtered['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        set_clause = ', '.join(f"{k} = ?" for k in filtered)
        values = list(filtered.values())
        where = ["zone_name = ?"]
        values.append(zone_name)
        for key, val in (expect or {}).items():
            if key not in allowed and key != 'id':
                continue
            if val is None:
                where.append(f"{key} IS ?")
            else:
                where.append(f"{key} = ?")
            values.append(val)
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE {' AND '.join(where)}", values
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_task_by_id_conditional(self, task_id: int, updates: dict,
                                      expect: Optional[dict] = None) -> bool:
        """按 id 的条件更新（重命名等场景用 id 定位，不把目标分区当更新源）。"""
        allowed = {
            'zone_name', 'category', 'total_days', 'days_done', 'deadline_raw',
            'today_done', 'remaining_days',
            'a_val', 'i_val', 'j_val'
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        filtered['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        set_clause = ', '.join(f"{k} = ?" for k in filtered)
        values = list(filtered.values())
        where = ["id = ?"]
        values.append(task_id)
        for key, val in (expect or {}).items():
            if key not in allowed and key != 'id':
                continue
            if val is None:
                where.append(f"{key} IS ?")
            else:
                where.append(f"{key} = ?")
            values.append(val)
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE {' AND '.join(where)}", values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_all_tasks(self):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM tasks")
            conn.commit()
            logger.info("已清空所有任务")

    # ==================== 计划式导入（解析 → 校验 → 单事务替换） ====================

    def plan_import_from_excel(self, excel_path: str,
                               skip_zones: List[str] = None,
                               max_rows: int = 20000) -> dict:
        """只解析和校验，不写库。

        返回 {rows, imported, updated, skipped, errors, truncated}：
        - rows：待提交集合（每行已校验过天数/截止日期）；
        - errors：被跳过的行及原因（不代表会回滚，因为还没有写入）。

        早期"先扫描分区名再边解析边写"的做法无法保证失败时回滚，因此解析必须
        在写入之前完整完成。
        """
        from openpyxl import load_workbook

        wb = load_workbook(excel_path)
        ws = wb.active
        skip_set = set(skip_zones or [])

        plan = {'rows': [], 'imported': 0, 'updated': 0, 'skipped': 0,
                'errors': [], 'truncated': False}
        try:
            existing_zones = {r['zone_name'] for r in self.get_all_tasks()}
            max_row = min(ws.max_row, max_rows + 1)
            if ws.max_row > max_rows + 1:
                plan['truncated'] = True
                plan['errors'].append(
                    f'文件行数超过 {max_rows} 行，只处理前 {max_rows} 行')
            for row in range(2, max_row + 1):
                zone_name = str(ws.cell(row=row, column=2).value or '').strip()
                if not zone_name:
                    continue
                if zone_name in skip_set:
                    continue

                category = self._safe_int(ws.cell(row=row, column=3).value, None)
                if category is None:
                    category = 2

                total_days = self._safe_int(ws.cell(row=row, column=4).value, None)
                if total_days is None or total_days <= 0:
                    plan['skipped'] += 1
                    plan['errors'].append(
                        f"第{row}行「{zone_name}」：需要完成天数无效，已跳过")
                    continue

                days_done = self._safe_int(ws.cell(row=row, column=5).value, 0)

                deadline_raw = str(ws.cell(row=row, column=6).value or '').strip()
                if not deadline_raw or not self._validate_deadline(deadline_raw):
                    plan['skipped'] += 1
                    plan['errors'].append(
                        f"第{row}行「{zone_name}」：截止时间无效（'{deadline_raw}'），已跳过")
                    continue

                today_done = self._safe_int_or_none(ws.cell(row=row, column=7).value)
                remaining_days = self._safe_int(
                    ws.cell(row=row, column=8).value, None)
                if remaining_days is None:
                    remaining_days = max(0, total_days - days_done)

                is_update = zone_name in existing_zones
                plan['rows'].append({
                    'zone_name': zone_name,
                    'category': category,
                    'total_days': total_days,
                    'days_done': days_done,
                    'deadline_raw': deadline_raw,
                    'today_done': today_done,
                    'remaining_days': remaining_days,
                })
                if is_update:
                    plan['updated'] += 1
                else:
                    plan['imported'] += 1
        finally:
            wb.close()
        return plan

    def apply_import_plan(self, plan: dict, mode: str = 'merge') -> dict:
        """在同一事务内落盘整个导入计划。

        mode='replace' 时先清空再写入，两步共用一个连接：清空之后解析/写入
        再失败也能整体回滚，不会留下空库。
        """
        rows = list(plan.get('rows') or [])
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.transaction() as conn:
            if mode == 'replace':
                conn.execute("DELETE FROM tasks")
            for item in rows:
                conn.execute(
                    """INSERT INTO tasks
                    (zone_name, category, total_days, days_done,
                    deadline_raw, today_done, remaining_days, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(zone_name) DO UPDATE SET
                    category=excluded.category,
                    total_days=excluded.total_days,
                    days_done=excluded.days_done,
                    deadline_raw=excluded.deadline_raw,
                    today_done=excluded.today_done,
                    remaining_days=excluded.remaining_days,
                    updated_at=excluded.updated_at""",
                    (item['zone_name'], item['category'], item['total_days'],
                     item['days_done'], item['deadline_raw'], item['today_done'],
                     item['remaining_days'], now)
                )
        return {
            'imported': plan.get('imported', 0),
            'updated': plan.get('updated', 0),
            'skipped': plan.get('skipped', 0),
            'errors': list(plan.get('errors') or []),
        }

    def count_tasks(self) -> int:
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def has_tasks(self) -> bool:
        return self.count_tasks() > 0

    @staticmethod
    def read_zone_names_from_excel(excel_path: str) -> List[str]:
        from openpyxl import load_workbook
        wb = load_workbook(excel_path)
        ws = wb.active
        zones = []
        for row in range(2, ws.max_row + 1):
            zone = str(ws.cell(row=row, column=2).value or '').strip()
            if zone:
                zones.append(zone)
        wb.close()
        return zones

    def import_from_excel(self, excel_path: str, skip_zones: List[str] = None,
                          mode: str = 'merge') -> dict:
        """从 Excel 导入任务到数据库，返回 {imported, updated, skipped, errors}

        先完整解析校验为待提交计划，再在**单个事务**内落盘：解析阶段任何失败
        都不会写库；replace 模式的清空与写入同属一个事务，失败可整体回滚。

        校验规则（与旧实现一致，不在此放宽或收紧）：
        - B列（分区名）为空 跳过
        - D列（需要完成天数）为空或0 跳过并记录错误
        - F列（截止时间）无法解析为有效日期 跳过并记录错误
        - C列（类别）默认2（非数字或<=0 默认2）
        - skip_zones 中的分区名 跳过
        """
        plan = self.plan_import_from_excel(excel_path, skip_zones=skip_zones)
        # 全覆盖：全部有效行为 0 时拒绝替换，避免把一个空/全非法的文件
        # 解释成"用户想清空任务库"（历史上曾出现磁盘空、内存仍显示旧任务）。
        if mode == 'replace' and not plan['rows']:
            logger.warning('全覆盖导入：没有有效行，已拒绝替换（旧数据保持不变）')
            return {
                'imported': 0, 'updated': 0, 'skipped': plan['skipped'],
                'errors': plan['errors'] + ['没有有效行，未执行全覆盖替换'],
                'rejected': True,
            }
        result = self.apply_import_plan(plan, mode=mode)
        logger.info(
            f"从 Excel 导入（{mode}）：新增 {result['imported']}，"
            f"更新 {result['updated']}，跳过 {result['skipped']}")
        if result['errors']:
            for err in result['errors']:
                logger.warning(f" {err}")
        return result

    @staticmethod
    def _validate_deadline(raw_val: str) -> bool:
        import re as _re
        from datetime import date as _date, datetime as _dt, timedelta as _td
        s = raw_val.strip()
        if not s:
            return False
        m = _re.match(r'=DATE\((\d+),(\d+),(\d+)\)', s, _re.IGNORECASE)
        if m:
            try:
                _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return True
            except ValueError:
                return False
        try:
            serial = int(float(s))
            if 40000 < serial < 80000:
                return True
        except BaseException:
            pass
        for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S']:
            try:
                _dt.strptime(s, fmt)
                return True
            except ValueError:
                pass
        return False

    def export_to_excel(self, excel_path: str, stats: dict = None):
        """导出任务到 Excel（自行读库；需要版本一致快照时用 export_rows_to_excel）"""
        self.export_rows_to_excel(self.get_all_tasks(), excel_path, stats)

    def export_rows_to_excel(self, rows: List[Dict[str, Any]], excel_path: str,
                             stats: dict = None):
        """导出**给定行快照**到 Excel，保证与统计值同属一个版本。
        格式：宋体 16pt；任务行 A-I + J/K 列放统计值
        列宽 = 表头除括号外字数（分区名=10汉字）
        """
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"

        font = Font(name='宋体', size=16)
        font_bold = Font(name='宋体', size=16, bold=True)
        align_left = Alignment(horizontal='left', vertical='center')
        align_center = Alignment(horizontal='center', vertical='center')

        # 表头
        headers = [
            '优先度',  # A
            '分区名',  # B（居中）
            '类别(每天需要完成小时数，默认2，0为已完成状态)',  # C
            '需要完成天数',  # D
            '已完成天数',  # E
            '截止时间（默认为当天的23：59）',  # F
            '今日是否完成（完成扣1）',  # G
            '距离完成',  # H
            '松弛度',  # I
        ]
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = font_bold
            if col_idx == 2:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

        # 数据行
        tasks = rows
        for i, t in enumerate(tasks, start=2):
            j_val = t.get('j_val', -1)
            ws.cell(row=i, column=1, value=t.get('a_val', 9999)).font = font
            ws.cell(row=i, column=2, value=t['zone_name']).font = font
            ws.cell(row=i, column=3, value=t['category']).font = font
            ws.cell(row=i, column=4, value=t['total_days']).font = font
            ws.cell(row=i, column=5, value=t['days_done']).font = font
            dl_str = self._format_deadline(t.get('deadline_raw', ''))
            ws.cell(row=i, column=6, value=dl_str).font = font
            ws.cell(row=i, column=7, value=t.get('today_done')).font = font
            ws.cell(row=i, column=8, value=t.get('i_val', 0)).font = font
            ws.cell(row=i, column=9, value=j_val).font = font
            for col in range(1, 10):
                ws.cell(row=i, column=col).alignment = align_left

        # 统计值：J/K 列，第 1-3 行显示
        stats = stats or {}
        stat_font = Font(name='宋体', size=16, bold=True, color='CC0000')

        stat_items = [
            ('剩余时间：', stats.get('remaining_time', 0)),
            ('平均剩余时间：', round(stats.get('avg_remaining', 0), 2)),
            ('紧迫率：', f"{round(stats.get('urgency', 0) * 100, 2)}%"),
        ]

        for idx, (item_label, item_value) in enumerate(stat_items):
            row_num = idx + 1
            cell_j = ws.cell(row=row_num, column=10, value=item_label)
            cell_j.font = stat_font
            cell_j.alignment = align_center
            cell_k = ws.cell(row=row_num, column=11, value=item_value)
            cell_k.font = stat_font
            cell_k.alignment = align_center

        # 列宽
        widths = {
            'A': 10, 'B': 30, 'C': 7,
            'D': int(6 * 3.3), 'E': int(5 * 3.3),
            'F': 15, 'G': int(6 * 3.3),
            'H': int(4 * 3.3), 'I': 10,
            'J': int(7 * 3.3), 'K': int(4 * 3.3),
        }
        for col_letter, w in widths.items():
            ws.column_dimensions[col_letter].width = w

        wb.save(excel_path)
        wb.close()
        logger.info(f"导出 {len(tasks)} 条任务到 {excel_path}")

    @staticmethod
    def _format_deadline(raw_val: str) -> str:
        import re as _re
        from datetime import date as _date, datetime as _dt, timedelta as _td

        s = raw_val.strip() if raw_val else ''
        if not s:
            return ''
        # =DATE(Y,M,D)
        m = _re.match(r'=DATE\((\d+),(\d+),(\d+)\)', s, _re.IGNORECASE)
        if m:
            try:
                d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return f"{d.year}/{d.month}/{d.day}"
            except ValueError:
                return s
        # 序列号
        try:
            serial = int(float(s))
            if 40000 < serial < 80000:
                d = _date(1899, 12, 30) + _td(days=serial)
                return f"{d.year}/{d.month}/{d.day}"
        except BaseException:
            pass
        # 日期字符串（含时间部分）
        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d']:
            try:
                d = _dt.strptime(s, fmt).date()
                return f"{d.year}/{d.month}/{d.day}"
            except ValueError:
                pass
        return s

    def auto_migrate_from_excel(self, excel_path: str) -> bool:
        if self.has_tasks():
            logger.info("数据库已有数据，跳过迁移")
            return False
        excel = Path(excel_path)
        if not excel.exists():
            logger.info("Excel 文件不存在，跳过迁移")
            return False
        logger.info(f"检测到 Excel 文件，开始自动迁移：{excel}")
        result = self.import_from_excel(str(excel))
        logger.info(
            f"自动迁移完成：新增 {result['imported']}，"
            f"更新 {result['updated']}，跳过 {result['skipped']}")
        return result['imported'] + result['updated'] > 0

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int_or_none(value):
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            s = str(value).strip()
            if s == '1':
                return 1
            return None
