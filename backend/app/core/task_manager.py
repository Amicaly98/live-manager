"""
task_manager.py - 任务管理核心（SQLite 版）

从 Excel 迁移至 SQLite 本地存储。
保持所有公式计算逻辑不变，Excel 仅作导入/导出。
"""

import json
import hashlib
import time
import re
import logging
import random
import threading
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

from app.models.schemas import LiveInstruction, Task
from app.core.db import TaskDB
from app.core.config import get_data_path

# 常量
REQUIRED_COLUMNS = [
    '优先度', '分区名', '类别', '需要完成天数', '已完成天数',
    '截止时间', '额外要求', '今日是否完成', '距离完成天数'
]


class _NotCommitted(Exception):
    """内部标记：变更条件不匹配，事务必须回滚且不视为异常。"""


class TaskMutationRejected(Exception):
    """写请求被明确拒绝（例如正在直播的任务不允许破坏性修改）。

    与"失败"区分：调用方（API）应转成 409 并给出可操作提示，而不是 500。
    """

    def __init__(self, reason: str, code: str = 'rejected'):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class TaskManager:
    """任务管理核心类（SQLite 存储，Excel 导入/导出）"""

    #: 单次变更的原子单位标记：列表/详情/统计/导出共用同一版本。
    REVISION_UNCHANGED = -1
    # Successful import intents are kept in the same SQLite metadata table as the
    # task revision.  This is deliberately durable and has no in-memory eviction:
    # once a destructive import has committed, a late request carrying its token
    # can never become a new import merely because a process-local cache forgot it.
    _IMPORT_OPERATION_KEY_PREFIX = 'task_import:'
    # Metadata is bounded for disk hygiene, but records are never silently
    # evicted: once the safety budget is full, a new token is explicitly rejected
    # so an old response can never become executable again after forgetting.
    _IMPORT_OPERATION_MAX = 4096

    def __init__(self, db_path: str = "live_tasks.db", excel_path: str = "live_tasks.xlsx"):
        self.db = TaskDB(db_path)
        self.excel_path = Path(excel_path).resolve()  # 保留用于导入导出
        self.tasks: List[Task] = []
        self.last_reset_date: Optional[date] = None
        self._reset_lock = threading.Lock()
        self._scheduler_running = True
        self._is_resetting = False
        self._last_run_date_file = get_data_path("task_manager_last_run.json")
        self._last_run_date: Optional[date] = None

        # 所有写操作共用一把锁：改原字段 → 重算派生值 → 发布快照必须是一个
        # 不可分割的序列，否则并发读取会看到"改了一半"的任务列表。
        self._mutation_lock = threading.RLock()
        # 已发布的快照版本：任何一次**实际提交**都会前进，读者据此判断是否需要
        # 重取。它同时是覆盖请求的前置条件（_require_overwrite_preconditions），
        # 因此必须与任务数据**同一份状态**：元数据存在同一个 SQLite 库、与任务
        # 变更同事务推进（见 TaskDB.write_revision_meta / _sync_revision_meta），
        # 不存在"数据已提交而版本没跟上"的窗口；元数据丢失时按数据指纹重新对齐。
        # None = 版本不可用（此时拒绝需要版本授权的覆盖，不放行旧确认）。
        self.tasks_revision: Optional[int] = None
        # 与 self.tasks 同时替换的 DB 行快照（详情/导出读取同一版本）。
        self._rows_snapshot: List[dict] = []
        # 每日重置屏障：只有 reset_completed_date == 目标日 才算"新的一天已就绪"。
        self.reset_completed_date: Optional[date] = None
        # 正在直播的任务身份（task_id + run_id）：用于拒绝运行中的破坏性修改。
        self._active_task_id: Optional[int] = None
        self._active_run_id: Optional[str] = None
        self._active_lock = threading.Lock()
        # 可中断的调度等待，shutdown 时立即唤醒而不是等满 time.sleep。
        self._scheduler_wake = threading.Event()

        self._load_last_run_date()

        # 自动迁移：如果 DB 为空但 Excel 存在，从 Excel 导入
        migrated = self.db.auto_migrate_from_excel(str(self.excel_path))

        # 如果数据库仍为空，填充示例数据
        if not self.db.has_tasks():
            self._seed_sample_data()

        self._initialize_from_db()
        self._start_reset_scheduler()

    # ==================== 生命周期 ====================

    def _load_last_run_date(self):
        if not self._last_run_date_file.exists():
            self._last_run_date = None
            return
        try:
            with open(self._last_run_date_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            last_run_str = data.get('last_run_date')
            if last_run_str:
                self._last_run_date = datetime.fromisoformat(last_run_str).date()
                logger.info(f" 加载上次运行日期：{self._last_run_date}")
            else:
                self._last_run_date = None
        except Exception as e:
            logger.debug(f" 加载上次运行日期失败：{e}")
            self._last_run_date = None

    def _save_last_run_date(self):
        try:
            with open(self._last_run_date_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'last_run_date': datetime.now().isoformat(),
                    'note': '程序关闭时保存'
                }, f, ensure_ascii=False, indent=2)
            logger.info(f" 已保存运行日期：{datetime.now().date()}")
        except Exception as e:
            logger.error(f" 保存运行日期失败：{e}")

    def _seed_sample_data(self):
        """当数据库为空时，填充示例数据（category=2表示每天2小时）"""
        samples = [
            {"zone_name": "学习区", "category": 2, "total_days": 3, "days_done": 0,
             "deadline_raw": "=DATE(2026,12,31)", "today_done": None},
            {"zone_name": "游戏区", "category": 3, "total_days": 2, "days_done": 0,
             "deadline_raw": "=DATE(2026,11,30)", "today_done": None},
            {"zone_name": "测试区", "category": 2, "total_days": 2, "days_done": 0,
             "deadline_raw": "=DATE(2026,10,15)", "today_done": None},
        ]
        for s in samples:
            self.db.insert_task(s)
        logger.info(f" 已创建 {len(samples)} 条示例任务")

    # ==================== 数据加载（从 SQLite） ====================

    def load_tasks(self) -> bool:
        """从 SQLite 加载任务列表到内存。

        先在局部构建完整列表，最后整体替换：加载期间并发读者只能看到旧完整
        快照或新完整快照，不会看到空表/半表（旧实现先 `self.tasks=[]` 再
        逐项 append，监控/重置/API 跨线程访问会读到半表）。

        "读当前行 → 对齐版本元数据 → 发布快照"这三步整体与 CRUD/结算/每日重置
        互斥（同一个 ``_mutation_lock``，可重入）。只读完行就放开锁是不够的：
        后台可以在这之后正常提交新版本，于是随后的版本对齐会把旧行当成"未登记
        的改动"写回更高版本，最后把旧行发布成一个**已被授权的新版本**——覆盖
        检查随即放行旧内容，已结算的一天被抹掉（监督 S3）。
        """
        with self._mutation_lock:
            try:
                rows = self.db.get_all_tasks()
            except Exception as e:
                logger.error(f" 加载任务失败：{e}", exc_info=True)
                return False
            rows = self._discard_stale_read(rows)
            revision = self._resolve_revision(rows)
            self._publish_rows(rows, revision=revision)
            if revision is None:
                # 连数据指纹都算不出来：版本必须显式不可用，不能沿用上一次的值，
                # 否则旧确认会被误判为"与当前数据一致"。
                self.tasks_revision = None
        logger.info(f" 成功从数据库加载 {len(self.tasks)} 个任务")
        return True

    def _discard_stale_read(self, rows: List[dict]) -> List[dict]:
        """如果这批行与版本元数据所登记的数据不是同一份，就重新读一次。

        元数据里的指纹与这批行的指纹不符有两种可能：数据被"没登记版本"的一方
        改过（旧版代码 / 回退期间 / 外部写入），或者这次读取本身与一次提交交错
        （读到的是提交前的旧行）。两种情况都以**最新读到的行**为准，因此这里
        不需要区分——关键是不管哪种情况，都绝不会把旧行的指纹以更高版本写回
        元数据：那等于给旧快照发了新版本的授权（监督 S3 的第二种形态）。

        正常情况下（已持 ``_mutation_lock``）这次重读与首次读到的是同一份数据，
        多花一次本地读取；只在指纹不符时才发生。
        """
        try:
            fingerprint = TaskDB.fingerprint_rows(rows)
        except Exception:
            return rows
        stored_revision, stored_fingerprint = self.db.read_revision_meta()
        if stored_revision is None or stored_fingerprint == fingerprint:
            # 元数据缺失（按数据推导版本）或本来就一致：无需重读。
            return rows
        try:
            latest = self.db.get_all_tasks()
            TaskDB.fingerprint_rows(latest)
        except Exception as e:
            logger.warning(f" 重新确认任务数据失败（沿用已读到的行）：{e}")
            return rows
        logger.warning(
            " 任务数据的读取与已提交的数据不一致：废弃这批行，改用最新读到的"
            "数据（重叠读不会把旧数据发布成新版本）")
        return latest

    def _publish_rows(self, rows: List[dict], revision: Optional[int] = None) -> int:
        """原子发布一次完整快照，返回新的 tasks_revision。

        ``revision`` 由调用方给出（提交路径来自同一事务，读取路径由数据推导）；
        不给则保持当前版本——读取不产生新版本。
        """
        tasks = []
        for row in rows:
            tasks.append(self._row_to_task(row))
        # 整体替换（不做 append 循环），读者看到的永远是完整列表。
        self.tasks = tasks
        self._rows_snapshot = list(rows)
        if revision is not None:
            self.tasks_revision = revision
        return self.tasks_revision

    def get_snapshot(self) -> Tuple[Optional[int], List[dict]]:
        """取当前已发布的完整快照（版本 + DB 行副本）。

        列表、详情、统计与导出都从这里取，保证同一版本内派生值一致。
        """
        with self._mutation_lock:
            return self.tasks_revision, list(self._rows_snapshot)

    def _reload_rows(self, conn) -> List[dict]:
        return TaskDB.rows_from_conn(conn)

    # ==================== 快照版本（与任务数据同库、同事务） ====================

    @staticmethod
    def _derive_revision(fingerprint: str) -> int:
        """元数据缺失时按**数据本身**推一个起点。

        不是"从 0 重新计数"：起点由已提交的数据决定，于是旧机制（从 0/1 起的小
        整数）发出的票据在迁移的这一刻全部作废；而数据没变时重算出的版本仍与
        之前一致，正常的重启不会误伤。
        """
        return int(fingerprint[:12], 16)

    def _sync_revision_meta(self, conn, rows: List[dict]) -> int:
        """在**任务变更的同一个事务**里把版本推进到与刚提交的数据一致。

        任何一步失败都会让整个事务回滚（数据与版本一起不生效），因此不会出现
        "数据已提交、版本没跟上"——那正是独立 JSON 保存失败后重启会让旧确认
        重新有效的原因（监督 S2）。
        """
        fingerprint = TaskDB.fingerprint_rows(rows)
        stored_revision, stored_fingerprint = self.db.read_revision_meta(conn)
        if stored_revision is None:
            revision = self._derive_revision(fingerprint)
        elif stored_fingerprint == fingerprint:
            # 这次提交没有改变有意义的状态（例如只刷新 updated_at 的静态重算）：
            # 版本不动——用户此前看到的那份状态仍然成立，不该被误伤。
            revision = int(stored_revision)
        else:
            revision = int(stored_revision) + 1
        self.db.write_revision_meta(conn, revision, fingerprint)
        return revision

    def _resolve_revision(self, rows: List[dict]) -> Optional[int]:
        """读取路径：先把版本与**当前数据**对齐，再发布。

        - 元数据齐全且指纹一致 → 用已登记的版本（保持单调计数）；
        - 元数据缺失 / 指纹不一致 → 数据被"没登记版本"的一方改过（旧版代码、
          回退期间、外部改动），按数据重算并前进一步，让期间的旧确认失效；
        - 连数据都读不出来 → None（覆盖请求据此拒绝，而不是放行）。
        """
        try:
            fingerprint = TaskDB.fingerprint_rows(rows)
        except Exception as e:
            logger.error(f" 计算任务数据指纹失败（版本不可用）：{e}")
            return None
        stored_revision, stored_fingerprint = self.db.read_revision_meta()
        if stored_revision is not None and stored_fingerprint == fingerprint:
            return int(stored_revision)
        revision = (self._derive_revision(fingerprint) if stored_revision is None
                    else int(stored_revision) + 1)
        if stored_revision is not None:
            logger.warning(
                " 任务数据发生过未登记版本的改动（旧版代码/回退期间/外部写入），"
                f"版本前进到 {revision}：那一期间发出的覆盖确认一律作废")
        self.db.commit_revision_meta(revision, fingerprint)
        return revision

    # ==================== 公式计算（核心逻辑，与原版完全一致） ====================

    @staticmethod
    def _parse_deadline(raw_val) -> Optional[date]:
        """解析截止日期：datetime对象 / 公式 / 序列号 / 字符串"""
        if raw_val is None:
            return None
        if isinstance(raw_val, datetime):
            return raw_val.date()
        if isinstance(raw_val, date):
            return raw_val
        s = str(raw_val).strip()
        if not s:
            return None
        m = re.match(r'=DATE\((\d+),(\d+),(\d+)\)', s, re.IGNORECASE)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        try:
            serial = int(float(s))
            if 40000 < serial < 80000:
                return date(1899, 12, 30) + timedelta(days=serial)
        except:
            pass
        for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S']:
            try:
                return datetime.strptime(s, fmt).date()
            except:
                pass
        return None

    def _compute_static_rows(self, tasks: List[Task]) -> Tuple[List[dict], List[dict]]:
        """按既有公式计算 A/I/J（**纯计算，不写库**）。

        返回 (computed, corrections)：
        - computed：[{zone_name, a, i, j}]，按 A 升序；
        - corrections：[{zone_name, updates}]，计算过程中必须修正的原字段
          （当前只有"已完成任务的 days_done 必须等于 total_days"）。
        公式与旧实现逐字一致，只把"写库"与"计算"拆开，便于放进同一事务。
        """
        today = date.today()
        today_serial = (today - date(1899, 12, 30)).days

        computed = []
        corrections = []

        for t in tasks:
            cat = t.category
            actual_days = t.actual_days()

            # 已完成任务：确保 days_done = total_days
            if cat == 0 and t.days_done != t.total_days:
                corrections.append({
                    'zone_name': t.zone_name,
                    'updates': {'days_done': t.total_days},
                })
                days_done = t.total_days
            else:
                days_done = t.days_done

            # I 列：距离完成天数（category=0 表示已完成）
            if cat == 0:
                i_val = 0
            else:
                i_val = actual_days - days_done

            raw_f = t.deadline_formula
            deadline = self._parse_deadline(raw_f)

            # J 列：松弛度
            if cat > 0 and deadline:
                j_val = (deadline - today).days + 1 - i_val
            else:
                j_val = -1

            # A 列：优先度
            deadline_serial = (deadline - date(1899, 12, 30)).days if deadline else 0
            if cat > 0:
                a_val = (j_val + 1) * 100 - i_val
            else:
                a_val = 10000 - deadline_serial + today_serial if deadline else 10000

            computed.append({
                'zone_name': t.zone_name,
                'a': int(a_val),
                'i': i_val,
                'j': j_val,
                # remaining_days：旧列的**例行维护写**（随 A/I/J 同一事务重算）。
                # 权威口径是读取时的派生函数 ``Task.remaining_exec_days``；
                # 这里写列只为回退旧版本时也有与 I 一致的合理数据，且让
                # Excel 导入带进来的陈旧值在下一次例行重算时自然归位——
                # 不是一次性的批量迁移（DS1：绝不批量改库）。
                'remaining_days': int(i_val),
            })

        computed.sort(key=lambda x: x['a'])
        return computed, corrections

    @staticmethod
    def _write_static_rows(conn, computed: List[dict]) -> None:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for item in computed:
            conn.execute(
                """UPDATE tasks
                SET a_val = ?, i_val = ?, j_val = ?, remaining_days = ?,
                updated_at = ?
                WHERE zone_name = ?""",
                (item['a'], item['i'], item['j'],
                 item.get('remaining_days', 0), now, item['zone_name'])
            )

    def _compute_and_save_static(self) -> bool:
        """计算 A/I/J 列静态值，写入数据库（独立调用时自成事务）。"""
        try:
            with self._mutation_lock:
                with self.db.transaction() as conn:
                    rows = self._reload_rows(conn)
                    tasks = [self._row_to_task(r) for r in rows]
                    computed, corrections = self._compute_static_rows(tasks)
                    for fix in corrections:
                        conn.execute(
                            "UPDATE tasks SET days_done = ?, updated_at = ? "
                            "WHERE zone_name = ?",
                            (fix['updates']['days_done'],
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                             fix['zone_name']))
                    self._write_static_rows(conn, computed)
                    rows = self._reload_rows(conn)
                    # 静态重算也会改库（派生列 + 已完成任务的补正），同样要在
                    # 同一事务里推进版本。
                    revision = self._sync_revision_meta(conn, rows)
                self._publish_rows(rows, revision=revision)
            logger.info(f" 静态值计算完成，已按优先度排序（{len(computed)} 行）")
            return True
        except Exception as e:
            logger.error(f" 静态计算失败：{e}", exc_info=True)
            return False

    # ==================== 统一变更入口 ====================

    def _commit_change(self, mutate, finalize=None) -> bool:
        """执行 mutate(conn) -> bool，成功后重算派生值并一次性发布。

        这是任务侧唯一的写路径：CRUD、完成结算、导入都必须经过它，避免
        "改了内存没写库"或"写了库没重算派生值"的半完成状态。
        """
        with self._mutation_lock:
            try:
                with self.db.transaction() as conn:
                    if not mutate(conn):
                        # 未提交（条件不匹配 / 任务不存在）：回滚，保持原状。
                        raise _NotCommitted()
                    rows = self._reload_rows(conn)
                    tasks = [self._row_to_task(r) for r in rows]
                    computed, corrections = self._compute_static_rows(tasks)
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    for fix in corrections:
                        conn.execute(
                            "UPDATE tasks SET days_done = ?, updated_at = ? "
                            "WHERE zone_name = ?",
                            (fix['updates']['days_done'], now, fix['zone_name']))
                    self._write_static_rows(conn, computed)
                    rows = self._reload_rows(conn)
                    # 版本元数据与任务变更**同一事务**：一起提交或一起回滚。
                    # 独立保存的辅助文件做不到这点——它写失败后重启会让版本号
                    # 退回、旧确认重新有效（监督 S2）。
                    revision = self._sync_revision_meta(conn, rows)
                    # An operation record which protects a replay must be written
                    # on this same connection, after the final revision is known.
                    # If this callback fails, the task mutation and the revision
                    # metadata roll back together.
                    if finalize is not None:
                        finalize(conn, rows, revision)
            except _NotCommitted:
                return False
            except TaskMutationRejected:
                # 明确拒绝（例如任务正在直播）：必须原样上抛，由 API 转成 409。
                # 与"写失败"混成同一个 False 会让客户端把 409 当成 400，
                # 也让调用方无法区分"被规则拒绝"与"写坏了"。
                raise
            except Exception as e:
                logger.error(f" 任务变更失败（已回滚）：{e}", exc_info=True)
                return False
            self._publish_rows(rows, revision=revision)
            return True

    # ==================== 初始化流程 ====================

    def _initialize_from_db(self):
        """从数据库初始化（替代原 _initialize_with_excel_sort）"""
        logger.info("【初始化】开始初始化流程...")
        logger.info("步骤 1: 加载任务数据...")
        if not self.load_tasks():
            logger.error(" 初始化失败：任务加载失败")
            return
        logger.info("步骤 2: 检查是否跨日...")
        today = date.today()
        if self._last_run_date and self._last_run_date != today:
            logger.info(f" 检测到跨日：上次运行={self._last_run_date}, 今天={today}")
            logger.info("步骤 2.1: 清空'今日是否完成'标志...")
            self._clear_today_done_column()
        else:
            if self._last_run_date:
                logger.info(f" 同一天运行：{today}（上次运行：{self._last_run_date}）")
            else:
                logger.info(f" 首次运行或无上次运行记录：{today}")
        logger.info("步骤 3: 检查负优先度任务...")
        self._check_and_complete_negative_priority_tasks()
        logger.info("步骤 4: 计算静态值并按优先度排序...")
        self._compute_and_save_static()
        # 初始化完成后，"今天"的状态确实已经就绪（标志已清、优先度已重算）：
        # 把重置屏障推进到今天，跨日等待才有明确依据；否则监控会一直等到超时。
        self.last_reset_date = today
        self.reset_completed_date = today
        logger.info(" 初始化完成")

    # ==================== 负优先度处理 ====================

    def _check_and_complete_negative_priority_tasks(self):
        negative_unfinished = [t for t in self.tasks if t.priority < 0 and t.category > 0]
        if not negative_unfinished:
            logger.debug(" 未检测到负优先度的未完成任务")
            return False
        logger.warning("=" * 60)
        logger.warning(f" 检测到 {len(negative_unfinished)} 个负优先度未完成任务")
        for task in negative_unfinished:
            logger.warning(f"  → {task.zone_name}: 优先度={task.priority}")
        logger.warning("=" * 60)
        for task in negative_unfinished:
            self.db.update_task(task.zone_name, {
                'category': 0,
                'days_done': task.total_days,
            })
            task.category = 0
            task.days_done = task.total_days
        logger.info(f" 已完成 {len(negative_unfinished)} 个负优先度任务")
        logger.warning("=" * 60)
        return True

    def _clear_today_done_column(self):
        """清空所有任务的今天完成标志"""
        self.db.clear_today_done()
        for t in self.tasks:
            t.today_done = None
        self._save_last_run_date()
        logger.info(f" 已清空 {len(self.tasks)} 个任务的 today_done 标志")

    # ==================== 任务操作 ====================

    def select_next_instruction(self) -> Optional[LiveInstruction]:
        for task in self.tasks:
            if task.needs_execution():
                instruction = task.to_instruction()
                logger.info(f"→ 选定直播指令：{instruction}")
                return instruction
        logger.warning(" 无待执行任务")
        return None

    # ---------- 结算身份 ----------

    def set_active_task(self, task_id: Optional[int], run_id: Optional[str] = None) -> None:
        """登记"正在直播的任务身份"。

        破坏性写操作（删除/全覆盖/覆盖同名/修改）据此拒绝；run_id 用于区分
        "同一次直播的不同阶段"与"另一场直播"，停止时只清理自己登记的身份。

        与提交共用 ``_mutation_lock``：如果登记和"检查 + 提交"各用一把锁，
        "检查通过 → 期间被登记为在播 → 提交"就会让保护形同虚设。
        """
        with self._mutation_lock:
            with self._active_lock:
                self._active_task_id = task_id
                self._active_run_id = run_id

    def reserve_active_task(self, task_id: Optional[int],
                            run_id: Optional[str] = None,
                            validate=None) -> bool:
        """原子地"确认任务仍存在（+ 业务校验）再登记为在播"，返回是否成功。

        与破坏性写共用同一把锁：要么先登记（之后的删除会被拒绝），要么删除先
        提交（此时这里查不到记录而拒绝开播）。不会出现"记录已删除、却仍被登记
        为在播"或反之的跨锁窗口。

        ``validate`` 把调用方的业务判定（例如"恢复目标不能是已完成/被替换的
        记录"）拉进**同一个临界区**：校验与预留之间没有窗口，删除/改名/完成
        只能排在这一步之前或之后，插不进中间。

        锁序：调用方（控制器）持 ``_start_lock`` 时进入这里取 ``_mutation_lock``；
        反向路径不存在——任务 CRUD 只取 ``_mutation_lock``，不会回头去取
        ``_start_lock``。
        """
        with self._mutation_lock:
            if task_id is not None:
                row = self.db.get_task_by_id(task_id)
                if not row:
                    logger.warning(f" 拒绝登记在播：任务 id={task_id} 已不存在")
                    return False
                if validate is not None:
                    reason = validate(dict(row))
                    if reason:
                        logger.warning(f" 拒绝登记在播（id={task_id}）：{reason}")
                        return False
            with self._active_lock:
                self._active_task_id = task_id
                self._active_run_id = run_id
            return True

    def clear_active_task(self, run_id: Optional[str] = None) -> None:
        """清理运行中的任务身份；run_id 不匹配则不清理（新会话不受影响）。"""
        with self._active_lock:
            if run_id is not None and self._active_run_id != run_id:
                return
            self._active_task_id = None
            self._active_run_id = None

    def active_task_id(self) -> Optional[int]:
        with self._active_lock:
            return self._active_task_id

    def _guard_not_active(self, zone_name: str, task_id: Optional[int] = None,
                          conn=None, action: str = '修改') -> None:
        """拒绝针对"正在直播的任务"的破坏性修改。

        名字不是稳定身份：删除后重建同名任务会让旧直播结算到新记录上，也会
        让轮转卡住。默认拒绝并提示先受控停止，不用 UI 隐藏按钮代替后端保护。

        调用方必须在**持有 _mutation_lock 的事务内**再调用一次（传 conn）：
        只在外层检查一次等于"先检查后提交"，中间的登记窗口会让删除照旧成功。
        """
        active_id = self.active_task_id()
        if active_id is None:
            return
        if task_id is not None and task_id != active_id:
            return
        if task_id is None:
            if conn is not None:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE zone_name = ?",
                    (zone_name,)).fetchone()
                row_id = row['id'] if row else None
            else:
                existing = self.db.get_task_by_zone(zone_name)
                row_id = existing.get('id') if existing else None
            if row_id != active_id:
                return
        raise TaskMutationRejected(
            f"任务「{zone_name}」正在直播，请先停止直播后再{action}",
            code='task_running')

    def _guard_active_row(self, row, action: str = '修改') -> None:
        """事务内复核：解析出来的这一行是否正是当前在播任务。

        与 :meth:`_guard_not_active` 的区别是不查库、只看已解析的行——
        用于 mutate 内部已经有 row 的场景（更新/删除/覆盖同名）。
        """
        if row is None:
            return
        active_id = self.active_task_id()
        if active_id is None or row.get('id') != active_id:
            return
        raise TaskMutationRejected(
            f"任务「{row.get('zone_name')}」正在直播，请先停止直播后再{action}",
            code='task_running')

    def _resolve_task_row(self, conn, zone_name: str, task_id: Optional[int]):
        """按稳定 id 定位；id 查不到时返回 None（调用方据此判定身份已变化）。"""
        if task_id is not None:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None
        row = conn.execute(
            "SELECT * FROM tasks WHERE zone_name = ?", (zone_name,)).fetchone()
        return dict(row) if row else None

    def _identity_status(self, conn, zone_name: str, task_id: Optional[int]) -> str:
        """区分"任务不存在"与"身份已被替换"（同名但换了记录）。"""
        if task_id is None:
            return 'not_found'
        row = conn.execute(
            "SELECT id FROM tasks WHERE zone_name = ?", (zone_name,)).fetchone()
        return 'replaced' if row else 'not_found'

    def _removes_committed_progress(self, row, data) -> bool:
        """这次覆盖是否会抹掉**已经提交**的完成事实。

        只看完成进度，不看名字/时长等普通字段：普通字段被旧载荷改回去顶多是
        内容陈旧，而完成进度被改回去是数据矛盾（进度没了，结算幂等标记还在，
        再结算返回 already，那一天就永远补不回来）。
        """
        if int(data.get('days_done') or 0) < int(row.get('days_done') or 0):
            return True
        if data.get('today_done') == 1:
            return False
        if row.get('today_done') == 1:
            return True
        return row.get('last_done_date') == date.today().isoformat()

    def _require_overwrite_preconditions(self, row, data) -> None:
        """覆盖的前置条件：用户确认时看到的那份状态现在还成立吗。

        - 带 ``expected_revision``（界面确认时冻结的 ``tasks_revision``）：必须
          等于**当前已发布版本**。期间任何一次提交（结算、编辑、每日重置、
          导入、别的面板）都会推进版本，本次确认即视为过期，明确冲突而不是
          重写最新进度；
        - 带 ``business_date``：必须还是今天，跨日的旧确认作废；
        - 两者都没带：只允许**不动已提交进度**的覆盖。破坏性覆盖必须带上版本
          重新确认——"缺少前置条件就照写"正是抹掉同一天新进度的那条路径，
          因此这里返回 409，绝不静默降级成无保护覆盖。

        校验发生在事务内（``_commit_change`` 全程持 ``_mutation_lock``），
        通过之后到 UPDATE 之间不存在"别人又提交了一次"的窗口。
        """
        business_date = data.get('business_date')
        today_str = date.today().isoformat()
        if business_date and business_date != today_str:
            raise TaskMutationRejected(
                f'覆盖「{row.get("zone_name")}」的确认来自 {business_date}，'
                f'当前业务日已是 {today_str}：请刷新任务列表后重新确认',
                code='overwrite_stale_business_day')
        expected = data.get('expected_revision')
        if expected is None:
            if self._removes_committed_progress(row, data):
                raise TaskMutationRejected(
                    f'覆盖「{row.get("zone_name")}」会清掉已提交的完成进度，'
                    f'但本次请求没有带上确认时的版本：'
                    f'请刷新任务列表后重新确认要覆盖的内容',
                    code='overwrite_requires_revision')
            return
        current = self.tasks_revision
        if current is None:
            # 版本无法确定（数据/元数据都读不出来）：不放行需要版本授权的覆盖。
            # 只影响"覆盖"这一条写路径，正常推流与结算不受影响。
            raise TaskMutationRejected(
                f'无法确定「{row.get("zone_name")}」的当前版本：'
                f'请刷新任务列表后重新确认要覆盖的内容',
                code='overwrite_revision_unavailable')
        if int(expected) != current:
            raise TaskMutationRejected(
                f'「{row.get("zone_name")}」在确认之后已被改动'
                f'（当前版本 {current}，本次确认基于版本 {int(expected)}）：'
                f'请刷新任务列表后重新确认要覆盖的内容',
                code='overwrite_stale_revision')

    def _consistent_last_done(self, row, data):
        """覆盖之后 ``last_done_date`` 的取值（与 ``today_done`` 保持一致）。

        规则：``last_done_date`` 只能声称"今天已经结算过"，而这句话只有在
        ``today_done == 1`` 同时成立时才为真——

        - 载荷声称今日完成 → 记今天（同日结算因此仍然幂等）；
        - 载荷不再声称今日完成、而原值正好是今天 → 清成 NULL（否则记录自相
          矛盾：既说"今天没完成"，又留着今天已结算的标记，再结算会被判
          already，那一天永远补不回来）；
        - 其余情况保留原值（例如跨日重置后留下的昨天）。

        这条规则**不是**用来掩盖"旧覆盖抹掉进度"：那种请求在
        :meth:`_require_overwrite_preconditions` 就被拒了，走不到这里；即使
        把保护撤掉，被抹掉的 ``days_done`` 也照样让判据失败。
        """
        if data.get('today_done') == 1:
            return date.today().isoformat()
        existing_last = row.get('last_done_date')
        today_str = date.today().isoformat()
        return None if existing_last == today_str else existing_last

    def settle_task_done(self, zone_name: str, execution_date: Optional[date] = None,
                         task_id: Optional[int] = None,
                         run_id: Optional[str] = None) -> dict:
        """按稳定身份结算一次"任务完成"。

        返回 ``{'status': ..., 'task_id': ..., 'days_done': ..., 'all_done': bool}``：
        - ``settled``：本次真正写入一天；
        - ``already``：同一执行日已结算过（幂等，不重复加天）；
        - ``not_found``：任务不存在；
        - ``replaced``：身份已变化（任务被删除/重建/改名），旧结算未落到新记录；
        - ``failed``：DB 失败，已回滚，磁盘与内存都保持上一个完整状态。

        ``execution_date`` 缺省为今天；调用方（自然到时的监控）应显式传入
        开播时捕获的执行日，跨日之后到达的旧结算不会记到新的一天。
        """
        exec_date = execution_date or date.today()
        exec_day = exec_date.isoformat()
        result: Dict[str, Any] = {
            'status': 'failed', 'task_id': task_id, 'zone_name': zone_name,
            'execution_date': exec_day, 'all_done': False, 'days_done': None}
        today = date.today()
        if exec_date != today:
            # 跨日屏障在这里也必须成立：只有"目标业务日已经就是今天"的结算
            # 才允许写库。旧执行日迟到到达不得设置今日标志（既有的"跨日中断
            # 不结算"语义），未来日期同样拒绝，避免伪造"今天已完成"。
            result['status'] = 'stale_day'
            result['expected_date'] = today.isoformat()
            logger.warning(
                f"拒绝结算：执行日 {exec_day} 与当前业务日 {today.isoformat()} "
                f"不一致，不设置今日完成（任务 {zone_name}）")
            return result

        def mutate(conn):
            row = self._resolve_task_row(conn, zone_name, task_id)
            if not row:
                result['status'] = self._identity_status(conn, zone_name, task_id)
                return False
            real_id = row['id']
            result['task_id'] = real_id
            # 身份校验：名字可以变，id 才代表"开播时选定的那条记录"。
            if task_id is not None and real_id != task_id:
                result['status'] = 'replaced'
                return False
            if row.get('category') == 0:
                # 已经全部完成：不是"今日完成"，不重复加天。
                result['status'] = 'already'
                result['all_done'] = True
                result['days_done'] = row.get('days_done')
                return False
            if row.get('today_done') == 1:
                result['status'] = 'already'
                result['days_done'] = row.get('days_done')
                return False
            last_done = row.get('last_done_date')
            if last_done and last_done == exec_day:
                # 同一业务日已经结算过（例如响应丢失后的重放）。
                result['status'] = 'already'
                result['days_done'] = row.get('days_done')
                return False

            new_days_done = int(row.get('days_done') or 0) + 1
            total_days = int(row.get('total_days') or 1)
            all_done = new_days_done >= total_days
            updates = {
                'days_done': new_days_done,
                'today_done': 1,
                'last_done_date': exec_day,
            }
            if all_done:
                updates['category'] = 0
            cursor = conn.execute(
                """UPDATE tasks
                SET days_done = ?, today_done = ?, last_done_date = ?""" +
                (", category = 0" if all_done else "") + """
                , updated_at = ?
                WHERE id = ? AND (today_done IS NULL OR today_done <> 1)
                AND (last_done_date IS NULL OR last_done_date <> ?)""",
                (new_days_done, 1, exec_day,
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 real_id, exec_day)
            )
            if cursor.rowcount == 0:
                # 条件竞争：另一路刚刚结算过同一执行日。
                result['status'] = 'already'
                return False
            result['status'] = 'settled'
            result['days_done'] = new_days_done
            result['all_done'] = all_done
            return True

        with self._mutation_lock:
            ok = self._commit_change(mutate)
        if not ok and result['status'] == 'settled':
            # 事务未提交（含"已写入但重算失败被回滚"）：不得报告已结算，
            # 否则监控会以为任务已完成而切走，实际磁盘上一天都没记上。
            result['status'] = 'failed'
        if result['status'] == 'settled':
            self._save_last_run_date()
        return result

    def mark_task_done(self, zone_name: str, execution_date: Optional[date] = None,
                       task_id: Optional[int] = None,
                       run_id: Optional[str] = None) -> bool:
        """兼容旧签名：结算成功或"已结算过"都算成功（幂等）。"""
        outcome = self.settle_task_done(zone_name, execution_date=execution_date,
                                        task_id=task_id, run_id=run_id)
        if outcome['status'] in ('settled', 'already'):
            logger.info(
                f"任务完成结算：{zone_name} → {outcome['status']}"
                f"（执行日 {outcome['execution_date']}）")
            return True
        logger.warning(f"任务完成未结算：{zone_name} → {outcome['status']}")
        return False

    def mark_task_all_done(self, zone_name: str,
                           task_id: Optional[int] = None) -> bool:
        """将任务标记为全部完成（category=0，已完成天数=total_days）

        与"今日完成"区分：全部完成是人工/超期按规则关闭，不改变执行日语义
        （不写 last_done_date），因此不会被同一天的自然到时结算重复加天。
        """
        logger.info(f" 标记任务全部完成：{zone_name}")
        result: Dict[str, Any] = {'status': 'failed'}

        def mutate(conn):
            row = self._resolve_task_row(conn, zone_name, task_id)
            if not row:
                result['status'] = 'not_found'
                return False
            if task_id is not None and row['id'] != task_id:
                result['status'] = 'replaced'
                return False
            total_days = int(row.get('total_days') or 1)
            cursor = conn.execute(
                """UPDATE tasks
                SET category = 0, days_done = ?, today_done = 1, updated_at = ?
                WHERE id = ?""",
                (total_days, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 row['id'])
            )
            if cursor.rowcount == 0:
                result['status'] = 'not_found'
                return False
            result['status'] = 'settled'
            return True

        ok = self._commit_change(mutate)
        if ok:
            self._save_last_run_date()
        return ok

    # ==================== 每日重置 ====================

    def reset_daily_flags(self, target_date: Optional[date] = None) -> bool:
        """执行每日重置，并把 reset_completed_date 推进到目标日。

        整个重置（清标志 → 负优先度处理 → 重算派生值 → 发布快照）在同一个事务
        内完成并一次发布：读者不会看到"标志已清、优先度还是旧的"的中间态。

        只有成功提交后才会设置 ``reset_completed_date``——跨日等待的调用方据此
        判断"新一天确实已经就绪"，而不是靠"调度线程没在跑"这种反证。
        """
        with self._reset_lock:
            today = target_date or date.today()
            if self.last_reset_date == today and self.reset_completed_date == today:
                return True
            self._is_resetting = True
            logger.info(f" 开始每日重置 | 日期：{today}")
            # 重置前先冻结**一份一致**的简报快照（统计 + Top5 同源同版本，
            # 短锁内本地捕获，无网络等待）。捕获失败如实标记不可用——
            # 旧实现在这里吞异常、随后用重置后的数据冒充"昨日"，把
            # today_done=1 说成 0（2026-09-21 DS4）。捕获有独立兜底：
            # 快照拿不到只影响简报，绝不能让重置本身失败或卡住屏障。
            try:
                snapshot = self._capture_daily_snapshot()
            except Exception as exc:
                logger.warning(" 每日简报快照捕获兜底（不影响重置）：%s", exc)
                snapshot = {'status': 'unavailable', 'as_of': datetime.now(),
                            'error': str(exc)}
            try:
                logger.info("=" * 60)
                logger.info(f" 执行每日重置 | 日期：{today}")
                logger.info("=" * 60)
                with self._mutation_lock:
                    with self.db.transaction() as conn:
                        logger.info("【步骤 1】重新加载数据...")
                        rows = self._reload_rows(conn)
                        tasks = [self._row_to_task(r) for r in rows]
                        logger.info("【步骤 2】检查负优先度任务...")
                        self._complete_negative_priority_in(conn, tasks)
                        logger.info("【步骤 3】清空'今日是否完成'标志...")
                        conn.execute(
                            "UPDATE tasks SET today_done = NULL, updated_at = ?",
                            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
                        logger.info("【步骤 4】计算静态值并按优先度排序...")
                        rows = self._reload_rows(conn)
                        tasks = [self._row_to_task(r) for r in rows]
                        computed, corrections = self._compute_static_rows(tasks)
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        for fix in corrections:
                            conn.execute(
                                "UPDATE tasks SET days_done = ?, updated_at = ? "
                                "WHERE zone_name = ?",
                                (fix['updates']['days_done'], now, fix['zone_name']))
                        self._write_static_rows(conn, computed)
                        rows = self._reload_rows(conn)
                        # 每日重置会改库（清空今日标记 + 重算），同样要在同一
                        # 事务里推进版本：跨日之后旧确认必须一律失效。
                        revision = self._sync_revision_meta(conn, rows)
                    self._publish_rows(rows, revision=revision)
                self.last_reset_date = today
                self.reset_completed_date = today
                self._save_last_run_date()
                logger.info(f" 每日重置完成 | 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info("=" * 60)
                # 简报在**重置提交之后**、任务锁之外投递：统计日是刚结束的
                # 那一天（重置发生在 00:00，内容是昨天的结果）；取数失败或
                # 空报跳过都不影响重置结果与直播。本调用自身吞掉一切异常。
                self._deliver_daily_summary(
                    snapshot, summary_date=today - timedelta(days=1))
                return True
            except Exception as e:
                logger.error(f" 每日重置失败（已回滚）：{e}", exc_info=True)
                return False
            finally:
                self._is_resetting = False

    def _top5_snapshot(self) -> List[dict]:
        """按当前已发布快照取优先度最高的 5 个未完成任务（与统计同源）。"""
        return self._compute_top5_from_rows(self.get_snapshot()[1])

    def _compute_top5_from_rows(self, rows: List[dict]) -> List[dict]:
        """从**冻结行**计算 Top5（纯计算，可复用于同一份快照）。

        ``remaining_days`` 用权威派生口径 ``Task.remaining_exec_days``（与
        计算列 I 同义），不再读旧列——旧列默认 1 且从不重算，是"邮件里
        剩余天数全为 1"的根源（2026-09-21 DS1）。
        """
        tasks = [self._row_to_task(r) for r in rows]
        top = sorted([t for t in tasks if t.category > 0],
                     key=lambda t: t.priority)[:5]
        return [
            {
                'zone_name': t.zone_name,
                'priority': t.priority,
                'days_done': t.days_done,
                'actual_days': t.actual_days(),
                'remaining_days': t.remaining_exec_days(),
            }
            for t in top
        ]

    def _compute_stats_from_rows(self, rows: List[dict],
                                 as_of: Optional[date] = None) -> dict:
        """从**冻结行**计算统计（get_stats 与简报快照共用的唯一实现）。

        ``as_of`` 是这份统计的口径日期：松弛/平均剩余里的"距截止"按它算，
        不再偷用调用时刻的 date.today()——过午夜发送昨天的简报时，旧实现
        会用新日期计算（2026-09-21 DS5）。
        """
        tasks = [self._row_to_task(r) for r in rows]
        total = len(tasks)
        completed = sum(1 for t in tasks if t.category == 0)
        today_done = sum(1 for t in tasks if t.today_done == 1)
        today_pending = sum(1 for t in tasks if t.needs_execution())

        # 剩余时间 = Σ(I × category)：每天所需小时数 × 剩余天数
        today = as_of or date.today()
        remaining_time = 0
        i_vals_positive = []
        j_vals = []
        active_count = 0
        for t in tasks:
            if t.category > 0:
                i_val = t.actual_days() - t.days_done
                remaining_time += i_val * t.category
                if i_val > 0:
                    i_vals_positive.append(i_val)
                dl = self._parse_deadline(t.deadline_formula)
                if dl and dl > today:
                    j_val = max(-1, (dl - today).days + 1 - i_val)
                    j_vals.append(j_val)
                    if j_val > -1:
                        active_count += 1

        avg_j = round(sum(j_vals) / len(j_vals), 1) if j_vals else 0
        avg_i = round(sum(i_vals_positive) / len(i_vals_positive), 1) if i_vals_positive else 0
        avg_remaining = round(avg_j + avg_i, 2)
        urgency = round(remaining_time / max(1, avg_remaining * 20), 4) if avg_remaining > 0 else 0

        return {
            "total": total,
            "completed": completed,
            "pending_total": total - completed,
            "today_done": today_done,
            "today_pending": today_pending,
            "remaining_time": remaining_time,
            "avg_remaining": avg_remaining,
            "urgency": urgency,
        }

    def _capture_daily_snapshot(self, as_of: Optional[date] = None) -> dict:
        """冻结一份简报快照：stats 与 Top5 来自**同一份行、同一版本**。

        旧实现分两次读可变的 ``self.tasks``，中间被结算/编辑穿插就会把
        "旧统计 + 新 Top5"拼进同一封邮件（2026-09-21 DS3 实测：总剩余
        16 小时配 3/10 的进度）。这里在 ``_mutation_lock`` 内做**一次**
        已发布快照读取，两个口径都从这份冻结行计算；锁只覆盖本地数据
        捕获，网络投递绝不在这里发生。

        失败时如实返回 ``status='unavailable'``，由调用方决定跳过——
        绝不回落到重置后的数据冒充昨日（DS4）。
        """
        stamp = datetime.now()
        try:
            with self._mutation_lock:
                revision, rows = self.get_snapshot()
                stats = self._compute_stats_from_rows(rows, as_of)
                top5 = self._compute_top5_from_rows(rows)
                # 完成标记（today_done）属于哪个业务日，必须与数据**同一次**
                # 冻结（2026-09-21 R2：旧实现只冻结数值、不冻结归属日，
                # 多日未重置时 9/16 的完成被硬贴成 target-1 的结果）。
                # 依据都是既有事实：行上的 last_done_date 与管理器的
                # reset_completed_date；字段缺失/不一致 → 明确未知，不归 0。
                done_flags = self._derive_done_flags_window(rows, as_of)
        except Exception as exc:
            logger.warning(" 每日简报快照捕获失败（不伪造昨日数据）：%s", exc)
            return {'status': 'unavailable', 'as_of': stamp, 'error': str(exc)}
        return {
            'status': 'ok' if rows else 'empty',
            'as_of': stamp,
            'stats_date': as_of or stamp.date(),
            'revision': revision,
            'rows': len(rows),
            'stats': stats,
            'top5': top5,
            'done_flags': done_flags,
        }

    @staticmethod
    def _parse_iso_day(value) -> Optional[date]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    def _derive_done_flags_window(self, rows: List[dict],
                                  as_of: Optional[date]) -> dict:
        """推导当前完成标记（today_done=1）所属的业务日窗口。

        - 有完成行且都带同一个 last_done_date：该日期才是可信窗口；
          日期混合不能把所有完成数归给最大日期，窗口早于最近一次重置完成日
          也属于**不一致**（那些标记早应被清掉，多半是手工改库/回填）
          → 如实未知；
        - 有完成行但缺日期：完成数可信、归属日不可信 → 未知；
        - 无完成行：窗口 = 最近一次重置完成日（标记在那个 00:00 被清空，
          此后没有任何完成记录）；从未重置过 → 未知。
        这里只做**报告口径**的归属判断，不改任何任务数据。
        """
        done_rows = [r for r in rows if r.get('today_done') == 1]
        reset_day = self.reset_completed_date
        dated = [d for d in (self._parse_iso_day(r.get('last_done_date'))
                             for r in done_rows) if d is not None]
        dateless = len(done_rows) - len(dated)
        if done_rows and dated:
            distinct_days = set(dated)
            window = next(iter(distinct_days)) if len(distinct_days) == 1 else None
            consistent = (window is not None and
                          (reset_day is None or window >= reset_day))
            confident = dateless == 0 and consistent
            if not confident:
                window = None
        elif done_rows:
            window = None                  # 全部缺日期：归属不可信
            confident = False
        else:
            window = reset_day             # 空窗口 = 自上次重置起无完成
            confident = window is not None
        return {
            'window': window,
            'confident': confident,
            'done_rows': len(done_rows),
            'dateless_done_rows': dateless,
            'distinct_done_dates': len(set(dated)),
            'last_reset_completed': reset_day,
        }

    def _deliver_daily_summary(self, snapshot: dict,
                               summary_date: Optional[date]) -> None:
        """把冻结快照交给通知器；空报政策与失败边界在这里统一。

        - 快照不可用 → 不发、不伪造（绝不拿重置后的 today_done=0 冒充
          昨天的结果，DS4）；重置与直播照常，不受通知影响；
        - 快照有效但无可报内容（无待办、统计日无完成/活动）→ 跳过例行
          空报并留跳过原因；仍待办的实例照发（停播/暂停不等于空）；
        - 网络/渠道失败只记日志，绝不影响重置结果。
        日志只含实例、统计日、快照版本/行数、触发原因、结果与跳过原因，
        不记录收件人/凭据。
        """
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            status = snapshot.get('status')
            if status == 'unavailable':
                logger.warning(
                    " 每日简报跳过 | 统计日：%s | 原因：快照不可用（%s），"
                    "不使用重置后数据伪造昨日结果",
                    summary_date, snapshot.get('error'))
                return
            stats = snapshot.get('stats') or {}
            top5 = snapshot.get('top5') or []
            has_content = bool(stats.get('pending_total')) \
                or bool(stats.get('today_done')) \
                or bool(stats.get('today_pending'))
            if status == 'empty' or not has_content:
                logger.info(
                    " 每日简报跳过 | 统计日：%s | 原因：可信快照确认无待办且"
                    "统计日无完成/活动（revision=%s，rows=%s），例行空报不发",
                    summary_date, snapshot.get('revision'), snapshot.get('rows'))
                return
            result = sender.send_daily_summary(
                stats, top5, summary_date=summary_date,
                snapshot_meta={
                    'as_of': snapshot.get('as_of'),
                    'revision': snapshot.get('revision'),
                    'rows': snapshot.get('rows'),
                    # 完成标记的归属窗口与统计日**一起**交给邮件（R2）：
                    # 邮件据此决定完成数能否归入统计日。
                    'done_flags': snapshot.get('done_flags'),
                })
            logger.info(
                " 每日简报已处理 | 实例：%s | 统计日：%s | 触发：每日重置 | "
                "快照版本：%s | 行数：%s | 结果：%s",
                sender._instance_label(), summary_date,
                snapshot.get('revision'), snapshot.get('rows'), result)
        except Exception as exc:
            logger.warning(" 每日简报投递异常（不影响重置与直播）：%s", exc)

    def _complete_negative_priority_in(self, conn, tasks: List[Task]) -> int:
        """事务内处理负优先度任务（既有业务规则，不改公式）。"""
        negative = [t for t in tasks if t.priority < 0 and t.category > 0]
        if not negative:
            return 0
        logger.warning(f" 检测到 {len(negative)} 个负优先度未完成任务")
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for task in negative:
            conn.execute(
                "UPDATE tasks SET category = 0, days_done = ?, updated_at = ? "
                "WHERE id = ?",
                (task.total_days, now, task.id))
        logger.info(f" 已完成 {len(negative)} 个负优先度任务")
        return len(negative)

    # ==================== 调度器 ====================

    def _start_reset_scheduler(self):
        def reset_worker():  # noqa: C901
            logger.info(" 重置调度器已启动")
            while self._scheduler_running:
                now = datetime.now()
                next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if now >= next_reset:
                    next_reset += timedelta(days=1)
                sleep_seconds = max(0.0, (next_reset - now).total_seconds())
                # 可中断等待：shutdown 时立即醒来，不再卡在长 time.sleep 上。
                if self._scheduler_wake.wait(sleep_seconds):
                    self._scheduler_wake.clear()
                if not self._scheduler_running:
                    break
                # 补执行：只要"今天还没重置"就执行，不再要求恰好醒在 00:00 那一分钟。
                # 调度延迟/休眠唤醒错过整点时，旧实现会漏掉当天的重置。
                current = datetime.now()
                if (self.reset_completed_date != current.date()
                        and self.last_reset_date != current.date()):
                    logger.info(
                        f" 触发每日重置（当前 {current:%H:%M:%S}，"
                        f"上次重置 {self.last_reset_date}）")
                    self.reset_daily_flags(current.date())
        scheduler_thread = threading.Thread(target=reset_worker, daemon=True,
                                            name="DailyResetScheduler")
        scheduler_thread.start()
        logger.info(" 重置调度器线程已启动")

    def shutdown(self):
        self._scheduler_running = False
        self._scheduler_wake.set()
        self._save_last_run_date()
        logger.info(" 任务管理器已关闭")

    def is_resetting(self) -> bool:
        return self._is_resetting

    def day_ready(self, target_date: Optional[date] = None) -> bool:
        """目标业务日是否已经发布（跨日屏障的唯一判据）。

        - ``_is_resetting`` 为真 → 未就绪（重置过程中的快照是中间态）；
        - ``reset_completed_date == target`` → 就绪；
        - ``reset_completed_date is None`` → 这个实例从未发布过任何一天
          （没有"上一日快照"可以被误用），按就绪处理，避免阻塞正常启动；
        - 其余（残留旧日期）→ 未就绪，必须等。
        """
        # 统一走公开判定 is_resetting()：内部标量可以被替身/子类改写，
        # 直接读私有属性会让"已声明正在重置"被忽略掉。
        if self.is_resetting():
            return False
        target = target_date or date.today()
        if self.reset_completed_date is None:
            return True
        return self.reset_completed_date == target

    def ensure_day_ready(self, target_date: Optional[date] = None,
                         timeout: float = 120.0,
                         cancel: Optional[threading.Event] = None,
                         cancel_check=None) -> bool:
        """跨日屏障：**所有**选任务/开播入口都必须先经过它。

        旧实现只在 ``is_resetting()`` 为真时才等一次，而且等待失败还继续往下
        走；调度线程尚未开始重置时该标志本来就是 False，于是直接拿着上一日的
        排序选任务。这里改为无条件确认"目标日已发布"，失败即暂停。
        """
        target = target_date or date.today()
        if self.day_ready(target):
            return True
        logger.info(f"跨日屏障：目标日 {target} 尚未发布，等待重置完成...")
        return self.wait_for_reset_complete(timeout=timeout, target_date=target,
                                           cancel=cancel, cancel_check=cancel_check)

    def wait_for_reset_complete(self, timeout: float = 120.0,
                                target_date: Optional[date] = None,
                                cancel: Optional[threading.Event] = None,
                                cancel_check=None) -> bool:
        """等待**目标日**的重置确实完成。

        旧实现只等 `_is_resetting` 变 False：若调度线程还没开始重置，该标志本来
        就是 False，于是"等待"立刻成功，调用方会拿着上一日的 today_done 与排序
        继续选任务。这里改为等待屏障值，超时必须返回 False（不得谎报成功）。

        等待本身有界且可取消：只读两个标量，**不去抢 _reset_lock**——重置整段
        都持有那把锁，等待方一旦去抢就再也无法被 timeout/取消打断。
        """
        target = target_date or date.today()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not self.is_resetting() and self.reset_completed_date == target:
                return True
            if cancel is not None and cancel.is_set():
                logger.info(f"等待每日重置被取消（目标日 {target}）")
                return False
            if callable(cancel_check) and cancel_check():
                logger.info(f"等待每日重置期间收到取消（目标日 {target}）")
                return False
            if time.monotonic() >= deadline:
                logger.warning(
                    f" 等待每日重置超时（{timeout}s，目标日 {target}）："
                    f"不以旧日状态继续执行")
                return False
            time.sleep(0.2)

    # ==================== 面向 API 的方法 ====================

    def get_tasks_as_list(self) -> List[dict]:
        """获取任务列表（序列化）。

        带 ``id``：写请求（删除/修改/标记完成）必须按**稳定身份**定位。
        名字会在删除重建/改名后指向另一条记录，只按名字写会让"响应丢失后的
        重放"作用到重建出来的新任务上。
        """
        return [
            {
                "id": t.id,
                "priority": t.priority,
                "zone_name": t.zone_name,
                "category": t.category,
                "total_days": t.total_days,
                "actual_days": t.actual_days(),
                "days_done": t.actual_days() if t.category == 0 else t.days_done,
                "deadline_raw": t.deadline_formula,
                "today_done": t.today_done,
                # 旧列保持兼容输出；权威口径是 remaining_exec_days（DS1）。
                "remaining_days": t.remaining_days,
                "remaining_exec_days": t.remaining_exec_days(),
                "needs_execution": t.needs_execution(),
                "is_completed": t.category == 0,
            }
            for t in self.tasks
        ]

    def get_tasks_detail(self) -> List[dict]:
        """获取任务详情（含数据库 ID 和计算列）

        从**已发布的同一版本快照**读取，保证与列表/统计/导出看到的是同一次提交
        的结果（旧实现另开连接读库，可能读到刚改原字段、派生值还没重算的中间态）。
        """
        _revision, rows = self.get_snapshot()
        result = []
        for row in rows:
            t = self._row_to_task(row)
            result.append({
                "id": row['id'],
                "zone_name": t.zone_name,
                "category": t.category,
                "total_days": t.total_days,
                "actual_days": t.actual_days(),
                "days_done": t.actual_days() if t.category == 0 else t.days_done,
                "deadline_raw": row.get('deadline_raw', ''),
                "priority": t.priority,
                "today_done": t.today_done,
                # 旧列保持兼容输出；权威口径是 remaining_exec_days（DS1）。
                "remaining_days": t.remaining_days,
                "remaining_exec_days": t.remaining_exec_days(),
                "a_val": row.get('a_val', 9999),
                "i_val": row.get('i_val', 0),
                "j_val": row.get('j_val', -1),
                "needs_execution": t.needs_execution(),
                "is_completed": t.category == 0,
                "created_at": row.get('created_at'),
                "updated_at": row.get('updated_at'),
            })
        return result

    def _row_to_task(self, row: dict) -> Task:
        """将数据库行转为 Task 模型（带上数据库主键作为稳定身份）"""
        return Task(
            id=row.get('id'),
            priority=row.get('a_val', 9999),
            zone_name=row['zone_name'],
            category=row.get('category', 1),
            total_days=row.get('total_days', 1),
            days_done=row.get('days_done', 0),
            deadline_formula=row.get('deadline_raw', ''),
            today_done=row.get('today_done'),
            remaining_days=row.get('remaining_days', 1),
        )

    def get_stats(self) -> dict:
        """获取统计信息（从当前已发布快照计算，与列表/详情/导出同源）。"""
        return self._compute_stats_from_rows(self.get_snapshot()[1],
                                             date.today())

    # ==================== CRUD 方法（供 API 调用） ====================

    def create_task(self, data: dict, overwrite: bool = False) -> bool:
        """创建新任务；``overwrite=True`` 时覆盖**指定的那一条记录**。

        覆盖必须带稳定身份（``data['id']`` = 用户在确认框里看到的那一条）：
        名字不是身份，删除重建之后同名指向的是另一条记录，响应丢失后的重放也
        会落到别人身上。因此：

        - 缺 id：明确拒绝（``overwrite_requires_id``），不静默退回按名字匹配；
        - id 指的记录已不存在（被删除/已被替换）：拒绝
          （``overwrite_target_missing``），既不改同名的那条、也不当新建；
        - id 指的记录已改名：拒绝（``overwrite_target_renamed``），名字变了就
          不是用户确认时看到的那一条；
        - 确认之后记录被改动过（版本前进 / 跨日）：拒绝
          （``overwrite_stale_revision`` / ``overwrite_stale_business_day``），
          见 :meth:`_require_overwrite_preconditions`。旧实现只核对 id，同一
          条记录上刚结算出来的一天会被旧载荷抹掉，且 ``last_done_date`` 仍留
          着今天，再结算被判 already——那一天再也补不回来。
        """
        zone_name = data.get('zone_name', '')
        target_id = data.get('id')
        # 覆盖同名 = 破坏性写：正在直播的任务必须先停止（这条保护优先于
        # "请求是否合规"的检查，最坏情况下也要先拦住对在播任务的破坏）。
        if overwrite:
            self._guard_not_active(zone_name, target_id)
        if overwrite and target_id is None:
            raise TaskMutationRejected(
                f'覆盖任务「{zone_name}」需要目标记录的稳定身份（id）：'
                f'请刷新任务列表后重新确认要覆盖的那一条',
                code='overwrite_requires_id')

        def mutate(conn):
            existing = self._resolve_task_row(
                conn, zone_name, target_id if overwrite else None)
            if overwrite and existing is None:
                raise TaskMutationRejected(
                    f'要覆盖的任务记录已不存在（可能刚被删除或重建）：'
                    f'请刷新后重新确认要覆盖的「{zone_name}」',
                    code='overwrite_target_missing')
            if overwrite and existing.get('zone_name') != zone_name:
                raise TaskMutationRejected(
                    f'要覆盖的记录已改名为「{existing.get("zone_name")}」，'
                    f'与本次提交的「{zone_name}」不是同一条：请刷新后重试',
                    code='overwrite_target_renamed')
            self._guard_active_row(existing, '覆盖')
            if existing and overwrite:
                # 前置条件先判、且在同一事务内：通过之后就再没有窗口让别的
                # 提交插进来（旧实现只看 id/名字就照写，同一条记录上刚结算
                # 出来的一天会被旧载荷抹掉，而 last_done_date 还留着今天）。
                self._require_overwrite_preconditions(existing, data)
                last_done = self._consistent_last_done(existing, data)
                conn.execute(
                    """UPDATE tasks SET category=?, total_days=?, days_done=?,
                    deadline_raw=?, today_done=?, remaining_days=?,
                    last_done_date=?, updated_at=?
                    WHERE id=?""",
                    (data.get('category', 1), data.get('total_days', 1),
                     data.get('days_done', 0), data.get('deadline_raw', ''),
                     data.get('today_done'), data.get('remaining_days', 1),
                     last_done,
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S'), existing['id']))
                logger.info(f" 覆盖同名任务：{zone_name}")
                return True
            if existing and not overwrite:
                return False
            conn.execute(
                """INSERT INTO tasks
                (zone_name, category, total_days, days_done, deadline_raw,
                today_done, remaining_days)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (zone_name, data.get('category', 1), data.get('total_days', 1),
                 data.get('days_done', 0), data.get('deadline_raw', ''),
                 data.get('today_done'), data.get('remaining_days', 1)))
            return True

        return self._commit_change(mutate)

    def update_task_fields(self, zone_name: str, data: dict,
                           task_id: Optional[int] = None,
                           expect: Optional[dict] = None,
                           rename_to: Optional[str] = None) -> bool:
        """更新任务字段（按 id 定位；未提供 id 时按分区名定位）。

        ``rename_to`` 与其它字段在**同一个事务**里提交：旧实现先调用
        rename_task 再调用本方法，第二步失败会留下"名字改了、字段没改"的
        半次用户操作。

        在播保护是显式的：命中当前在播任务的一律拒绝（见 _guard_active_row），
        不允许"改原字段"绕开删除/覆盖的同等保护。
        """
        updates = dict(data)
        if 'deadline_formula' in updates:
            updates['deadline_raw'] = updates.pop('deadline_formula')
        allowed = {'zone_name', 'category', 'total_days', 'days_done',
                   'deadline_raw', 'today_done', 'remaining_days'}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if rename_to is not None:
            filtered['zone_name'] = rename_to
        if not filtered:
            return False

        def mutate(conn):
            row = self._resolve_task_row(conn, zone_name, task_id)
            if not row:
                return False
            # 原子边界：与 _guard_not_active 的预检查不同，这里与提交同处
            # 一个事务、且持有同一把变更锁，中途被登记为在播也不会漏掉。
            self._guard_active_row(row, '修改')
            if expect:
                for key, val in expect.items():
                    if row.get(key) != val:
                        return False
            set_clause = ', '.join(f"{k} = ?" for k in filtered)
            values = list(filtered.values()) + [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']]
            cursor = conn.execute(
                f"UPDATE tasks SET {set_clause}, updated_at = ? WHERE id = ?", values)
            return cursor.rowcount > 0

        return self._commit_change(mutate)

    def rename_task(self, task_id: int, new_zone_name: str) -> bool:
        """按 id 原子重命名：不会把目标分区已存在的任务当成更新源。"""
        def mutate(conn):
            row = self._resolve_task_row(conn, '', task_id)
            if not row:
                return False
            self._guard_active_row(row, '重命名')
            cursor = conn.execute(
                "UPDATE tasks SET zone_name=?, updated_at=? WHERE id=?",
                (new_zone_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 task_id))
            return cursor.rowcount > 0

        return self._commit_change(mutate)

    def delete_task_by_zone(self, zone_name: str,
                            task_id: Optional[int] = None) -> bool:
        """删除任务（拒绝删除正在直播的任务）"""
        self._guard_not_active(zone_name, task_id, action='删除')

        def mutate(conn):
            row = self._resolve_task_row(conn, zone_name, task_id)
            if not row:
                return False
            # 事务内复核：预检查之后、提交之前被登记为在播同样必须拒绝。
            self._guard_active_row(row, '删除')
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (row['id'],))
            return cursor.rowcount > 0

        return self._commit_change(mutate)

    # ==================== 导入/导出 ====================

    @classmethod
    def _import_operation_key(cls, operation_token: str) -> str:
        """Map an opaque request token to a bounded SQLite metadata key."""
        digest = hashlib.sha256(operation_token.encode('utf-8')).hexdigest()
        return f'{cls._IMPORT_OPERATION_KEY_PREFIX}{digest}'

    @classmethod
    def _import_operation_fingerprint(cls, plan: dict, mode: str,
                                      skip_zones: Optional[List[str]]) -> str:
        """Fingerprint the requested import independent of current DB contents.

        ``imported`` versus ``updated`` is intentionally omitted: another task
        created between a lost response and its replay must not turn the same
        intent into a different operation.  The actual input rows, mode and
        validation outcome still identify accidental token reuse.
        """
        canonical = {
            'mode': mode,
            'skip_zones': sorted(set(skip_zones or [])),
            'rows': plan.get('rows') or [],
            'skipped': int(plan.get('skipped') or 0),
            'errors': list(plan.get('errors') or []),
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str)
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _read_import_operation(self, conn, operation_token: str) -> Optional[dict]:
        """Read a previously committed import intent from the current transaction."""
        key = self._import_operation_key(operation_token)
        row = conn.execute(
            "SELECT value FROM tasks_meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            record = json.loads(row['value'])
            if not isinstance(record, dict) or not isinstance(record.get('result'), dict):
                raise ValueError('invalid operation record')
            return record
        except Exception as exc:
            raise TaskMutationRejected(
                '无法核对该导入操作的历史结果，请发起新的导入操作',
                code='import_replay_unverifiable') from exc

    def _write_import_operation(self, conn, operation_token: str,
                                fingerprint: str, result: dict) -> None:
        """Persist the successful import result in the task/revision transaction."""
        key = self._import_operation_key(operation_token)
        exists = conn.execute(
            "SELECT 1 FROM tasks_meta WHERE key = ?", (key,)).fetchone()
        if exists is None:
            count = conn.execute(
                "SELECT COUNT(*) FROM tasks_meta WHERE key LIKE ?",
                (f'{self._IMPORT_OPERATION_KEY_PREFIX}%',)).fetchone()[0]
            if count >= self._IMPORT_OPERATION_MAX:
                raise TaskMutationRejected(
                    '导入操作保护记录已达上限，请联系管理员归档后再导入',
                    code='import_replay_capacity')
        record = {
            'fingerprint': fingerprint,
            'result': dict(result),
        }
        conn.execute(
            "INSERT OR REPLACE INTO tasks_meta(key, value) VALUES (?, ?)",
            (key,
             json.dumps(record, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), default=str)))

    def import_from_excel(self, file_path: str, skip_zones: List[str] = None,
                          mode: str = 'merge',
                          operation_token: Optional[str] = None) -> dict:
        """从 Excel 文件导入任务，返回 {imported, updated, skipped, errors, message}

        先完整解析校验（不写库），再在**单事务**内落盘；成功后一次性发布新快照。
        ``mode='replace'`` 需要明确覆盖范围：没有有效行时拒绝替换，避免把"文件
        全是非法的行"解释成"用户想清空任务库"。
        """
        if mode == 'replace' and self.active_task_id() is not None:
            raise TaskMutationRejected(
                '有任务正在直播，无法执行全覆盖导入；请先停止直播',
                code='task_running')

        plan = self.db.plan_import_from_excel(file_path, skip_zones)
        if mode == 'replace' and not plan['rows']:
            logger.warning('全覆盖导入：没有有效行，已拒绝替换')
            return {
                'imported': 0, 'updated': 0, 'skipped': plan['skipped'],
                'errors': plan['errors'] + ['没有有效行，未执行全覆盖替换'],
                'rejected': True,
                'message': '导入未完成：没有有效行，旧数据保持不变',
            }
        # 覆盖/删除正在直播的任务同样属于破坏性写。
        if self.active_task_id() is not None:
            for item in plan['rows']:
                self._guard_not_active(item['zone_name'])

        # An operation token is an intent identity, not permission to perform a
        # fresh write.  The lookup below is repeated inside ``mutate`` so two
        # concurrent requests with the same token serialize safely under the
        # mutation lock and only one can commit.
        operation_token = (operation_token.strip()
                           if isinstance(operation_token, str) else '')
        operation_fingerprint = None
        if operation_token:
            operation_fingerprint = self._import_operation_fingerprint(
                plan, mode, skip_zones)

        result = {
            'imported': plan['imported'], 'updated': plan['updated'],
            'skipped': plan['skipped'], 'errors': plan['errors'],
            'rejected': False,
        }
        parts = []
        if result['imported'] > 0:
            parts.append(f"新增 {result['imported']} 个")
        if result['updated'] > 0:
            parts.append(f"更新 {result['updated']} 个")
        if result['skipped'] > 0:
            parts.append(f"跳过 {result['skipped']} 个")
        result['message'] = f"导入成功：{'，'.join(parts)}" if parts else "导入完成（无变更）"
        replay_result = None

        def mutate(conn):
            nonlocal replay_result
            if operation_token:
                record = self._read_import_operation(conn, operation_token)
                if record is not None:
                    if record.get('fingerprint') != operation_fingerprint:
                        raise TaskMutationRejected(
                            '该导入票据已经用于另一份导入内容，请发起新的导入操作',
                            code='import_replay_conflict')
                    replay_result = dict(record['result'])
                    replay_result['replayed'] = True
                    # Returning False makes _commit_change roll back without
                    # running static recomputation or advancing the revision.
                    return False
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # 事务内复核在播身份：预检查之后被登记为在播的同样必须拒绝。
            active_id = self.active_task_id()
            if active_id is not None:
                if mode == 'replace':
                    row = conn.execute("SELECT id FROM tasks WHERE id = ?",
                                       (active_id,)).fetchone()
                    if row:
                        raise TaskMutationRejected(
                            '有任务正在直播，无法执行全覆盖导入；请先停止直播',
                            code='task_running')
                for item in plan['rows']:
                    self._guard_not_active(item['zone_name'], conn=conn,
                                           action='覆盖')
            if mode == 'replace':
                conn.execute("DELETE FROM tasks")
            for item in plan['rows']:
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
                     item['remaining_days'], now))
            return True

        def finalize(conn, _rows, revision):
            if operation_token:
                committed_result = dict(result)
                committed_result['revision'] = revision
                self._write_import_operation(
                    conn, operation_token, operation_fingerprint,
                    committed_result)
            # Publish the revision to the caller only after the receipt write
            # succeeded.  If receipt durability fails, the surrounding SQLite
            # transaction rolls back and a rejected response must not advertise
            # the rolled-back revision as committed.
            result['revision'] = revision

        ok = self._commit_change(mutate, finalize=finalize)
        if replay_result is not None:
            return replay_result
        result['rejected'] = not ok
        if not ok:
            result['message'] = '导入未提交：已回滚，旧数据保持不变'
            return result
        return result

    def export_to_excel(self, file_path: str = None) -> str:
        """导出任务到 Excel 文件（含统计行）

        用**已发布快照**导出：导出期间任务被修改也不会产出半新半旧的表。
        "距离完成"列按权威派生口径覆盖：DB 的 i_val 在新建行上要等下一次
        例行重算才有值，导出/邮件/API 三个口径必须一致（2026-09-21 DS1）。
        """
        path = file_path or str(self.excel_path)
        _revision, rows = self.get_snapshot()
        for row in rows:
            row['i_val'] = self._row_to_task(row).remaining_exec_days()
        stats = self.get_stats()
        self.db.export_rows_to_excel(rows, path, stats)
        return path

    # ==================== 工具方法 ====================

    def print_summary(self):
        stats = self.get_stats()
        logger.info("=" * 70)
        logger.info(f" 任务摘要 | 时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"   总任务数       : {stats['total']}")
        logger.info(f"   已彻底完成     : {stats['completed']}")
        logger.info(f"   今日已完成     : {stats['today_done']}")
        logger.info(f"   今日待执行     : {stats['today_pending']}")
        logger.info("-" * 70)
        active_tasks = [t for t in self.tasks if t.needs_execution()][:10]
        for i, task in enumerate(active_tasks, 1):
            # 进度分母是 actual_days（旧实现用 remaining_days+days_done 拼接，
            # remaining_days 是从不重算的旧列，摘要因此显示错误总天数）。
            logger.info(f"  {i:2d}. {task.zone_name:12s} | "
                        f"进度:{task.days_done:2d}/{task.actual_days():2d} | "
                        f"剩余待执行 {task.remaining_exec_days()} 天")
        logger.info("=" * 70)
