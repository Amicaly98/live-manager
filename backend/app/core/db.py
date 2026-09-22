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
