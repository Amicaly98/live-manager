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
from app.core.db import (
    HISTORY_SOURCE_IMPORT,
    HISTORY_SOURCE_MIGRATION,
    HISTORY_SOURCE_NEGATIVE,
    HISTORY_SOURCE_USER,
    LEDGER_SOURCE_AUTO,
    LEDGER_SOURCE_MANUAL,
    META_CREATE_OPERATION_PREFIX,
    ProgressGuardError,
    TaskDB,
)
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

    def __init__(self, db_path: str = "live_tasks.db", excel_path: str = "live_tasks.xlsx",
                 reset_scheduler: bool = True,
                 last_run_date_file: Optional[str] = None,
                 seed_sample_data: bool = True,
                 import_from_excel: bool = True):
        """构造任务管理器。

        后三个参数是 **M0A 账号模式**要用的显式窄参数，缺省值与旧调用方逐字兼容：

        - ``last_run_date_file``：日切依据文件。缺省仍是**全局数据路径**下的
          ``task_manager_last_run.json``（旧行为）。账号模式必须显式给出账号目录里
          的那一份，否则两个 UID / 两个实例会串读同一份日切事实。
        - ``seed_sample_data``：空库时是否填示例任务。缺省 True（旧行为）。
          账号模式为 False —— 新账号首次使用必须是**空库**，不灌示例。
        - ``import_from_excel``：是否自动从 Excel 迁移/导入。缺省 True（旧行为）。
          账号模式为 False —— 不从一个与本账号无关的实例目录 Excel 偷读任务。

        三者都只是**构造期**判据，不改变公式、CRUD、导入事务或账本语义。
        """
        self.db = TaskDB(db_path)
        self.excel_path = Path(excel_path).resolve()  # 保留用于导入导出
        self.tasks: List[Task] = []
        self.last_reset_date: Optional[date] = None
        self._reset_lock = threading.Lock()
        self._scheduler_running = True
        self._is_resetting = False
        #: 旧日切后台线程是否由本实例启动。默认仍为真（旧生产行为不变）；
        #: 隔离运行可在**构造启动边界**显式传假，从而不存在那个后台写者 ——
        #: 不是"先起了线程再在测试里碰运气关掉"。
        self._reset_scheduler_enabled = bool(reset_scheduler)
        self._seed_sample_data_enabled = bool(seed_sample_data)
        self._import_from_excel_enabled = bool(import_from_excel)
        self._last_run_date_file = (Path(last_run_date_file) if last_run_date_file
                                    else get_data_path("task_manager_last_run.json"))
        self._last_run_date: Optional[date] = None

        # 所有写操作共用一把锁：改原字段 → 重算派生值 → 发布快照必须是一个
        # 不可分割的序列，否则并发读取会看到"改了一半"的任务列表。
        self._mutation_lock = threading.RLock()
        # 已发布的快照版本：任何一次**实际提交**都会前进，读者据此判断是否需要
        # 重取。它同时是**创建预检**的前置条件（见 `_preview_mismatch`），
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
        if self._import_from_excel_enabled:
            migrated = self.db.auto_migrate_from_excel(str(self.excel_path))
        else:
            migrated = False
            logger.info(" 未从 Excel 自动导入（构造参数 import_from_excel=False）")

        # 如果数据库仍为空，填充示例数据
        if self._seed_sample_data_enabled and not self.db.has_tasks():
            self._seed_sample_data()

        self._initialize_from_db()
        if self._reset_scheduler_enabled:
            self._start_reset_scheduler()
        else:
            self._scheduler_running = False
            logger.info(" 每日重置调度线程未启动（构造参数 reset_scheduler=False）")

    # ==================== 生命周期 ====================

    @property
    def reset_scheduler_enabled(self) -> bool:
        """旧日切后台线程是否由本实例启动（隔离运行显式关闭时为假）。"""
        return self._reset_scheduler_enabled

    @property
    def last_run_date_file(self) -> Path:
        """本实例实际使用的日切依据文件（证据用只读口，不参与任何判据）。"""
        return self._last_run_date_file

    @property
    def seed_sample_data_enabled(self) -> bool:
        """空库时是否填示例数据（账号模式为假）。"""
        return self._seed_sample_data_enabled

    @property
    def import_from_excel_enabled(self) -> bool:
        """是否自动从 Excel 迁移/导入（账号模式为假）。"""
        return self._import_from_excel_enabled


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
        """把"上次运行日"写成今天 —— **只在今天确实已经就绪时**。

        隔离模式（``reset_scheduler=False``）里，"今天跑过一次事务提交"**不等于**
        "今天的日切已经做过"。恢复路径上真实可达的顺序是：Bootstrap 先结算那份
        持久 End（走 Settle，**不经 Select**），一次补记**昨日**的账 → 用户不
        开播就关闭服务。此时若把今天写进这份文件，下一次构造会把"跨日"判成假，
        直接把"今天已就绪"塞进屏障，真实日切入口随即在 ``reset_daily_flags``
        里短路成功 —— 昨日的完成标记与简报依据就此永久丢失（实测：今天选不出
        任务、``found=False/no_tasks``、``summaries=[]``）。

        所以隔离模式只在 ``reset_completed_date == 今天``（本实例真的发布过今天）
        时才推进这份日期，把账本补记 / 人工完成 / 全部完成 / 关闭这**四个**调用点
        统一收在这一道门内（不再各调用点散加判断）；其余情况原样保留上一次的
        事实，让下一次构造与真实数据端口继续完成准备。旧默认构造
        （``reset_scheduler=True``）的写入与异常处理**逐字节不变**。
        """
        if not self._reset_scheduler_enabled:
            if self.reset_completed_date != date.today():
                logger.info(" 隔离模式：日切尚未准备完成，本次提交不代表今天已就绪，"
                            "不推进运行日期")
                return
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

    def _compute_static_rows(self, tasks: List[Task],
                             as_of: Optional[date] = None
                             ) -> Tuple[List[dict], List[dict]]:
        """按既有公式计算 A/I/J（**纯计算，不写库**）。

        返回 (computed, corrections)：
        - computed：[{zone_name, a, i, j}]，按 A 升序；
        - corrections：[{zone_name, updates}]，计算过程中必须修正的原字段
          （当前只有"已完成任务的 days_done 必须等于 total_days"）。
        公式与旧实现逐字一致，只把"写库"与"计算"拆开，便于放进同一事务。

        ``as_of`` 是**可选的计算日期参数**，不是第二份状态：省略时与旧实现
        逐字相同（取 ``date.today()``）。只有隔离模式下"今天尚未准备、却要先
        补记一笔昨日事实"的统一提交才会传入原数据日期 —— 那种提交不得把整库
        派生值提前按今天改写（日切自己的重算永远走省略参数的分支，按目标今日）。
        """
        today = as_of if as_of is not None else date.today()
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

    # ==================== 人工进度代数与窄历史（阶段 2） ====================

    #: "人工/导入真的改了进度事实"的判据字段。**不含** deadline_raw：截止日是
    #: 排序参数，不是进度事实；也不含 zone_name（改名不改进度，同一 task_id 仍然
    #: 是那一条记录）。today_done / last_done_date 参与，因为"今日已完成"是
    #: 完成状态的直接表达。
    PROGRESS_FIELDS = ('category', 'total_days', 'days_done', 'today_done',
                       'last_done_date')

    @classmethod
    def _progress_changed(cls, before: Optional[dict], after: dict) -> bool:
        """这次改动有没有真的动到**进度相关事实**（未改变 → 不推进人工代数）。"""
        if before is None:
            return True
        for key in cls.PROGRESS_FIELDS:
            if key not in after:
                continue
            left = before.get(key)
            right = after.get(key)
            if left != right:
                return True
        return False

    @staticmethod
    def _row_changed(before: Optional[dict], after: dict, fields) -> bool:
        """给定字段里有没有**任何一个**真的变了（无变化写不刷历史）。"""
        if before is None:
            return True
        for key in fields:
            if key not in after:
                continue
            if before.get(key) != after.get(key):
                return True
        return False

    def progress_guard(self, task_id: Optional[int]) -> int:
        """读该任务的人工进度变更代数（只读；键不存在时 0）。

        选取任务时冻结它、随 Run 保存；提交旧完成时在同一事务里再读一次比较。
        它是不透明并发凭据，**不是**第二份累计天数，也不落进 JSON 当权威。

        凭据**存在但读不出来**（损坏 / 非法 / SQL 故障）时抛
        :class:`~app.core.db.ProgressGuardError`，不返回 0：把读不出的凭据当成
        "从没人工改过"会让不该应用的旧自动完成重新生效（复核 R2）。
        """
        if task_id is None:
            raise ProgressGuardError('no_task_identity',
                                     '没有任务身份，无法读取人工进度代数')
        with self._mutation_lock:
            with self.db._get_conn() as conn:
                return self.db.read_progress_guard(conn, task_id)

    @staticmethod
    def _history_snapshot(row: Optional[dict]) -> Dict[str, Any]:
        if not row:
            return {}
        return {
            'zone_name': row.get('zone_name') or '',
            'category': row.get('category'),
            'total_days': row.get('total_days'),
            'days_done': row.get('days_done'),
            'deadline': row.get('deadline_raw'),
        }

    def _record_history(self, conn, task_id, event: str, *, before=None,
                        after=None, source: str = '', detail: str = '',
                        zone_name: str = '', area_id=None) -> None:
        """在同一事务里留一条任务历史事实。

        无变化更新、派生值重算、健康查询、重复回执都**不**调用这里；它只由
        真正的用户/导入/结算/移出/删除路径调用。快照只保存**当时真实**的参数：
        任务起始日没有可靠来源就不写，绝不从已播天数或 save 时间伪造。
        """
        if task_id is None:
            return
        before = self._history_snapshot(before)
        after = self._history_snapshot(after)
        self.db.insert_history(conn, {
            'task_id': int(task_id),
            'event': event,
            'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'zone_name': zone_name or after.get('zone_name') or before.get('zone_name') or '',
            'area_id': area_id,
            'category_before': before.get('category'),
            'total_days_before': before.get('total_days'),
            'days_done_before': before.get('days_done'),
            'deadline_before': before.get('deadline'),
            'category_after': after.get('category'),
            'total_days_after': after.get('total_days'),
            'days_done_after': after.get('days_done'),
            'deadline_after': after.get('deadline'),
            'source': source,
            'detail': detail,
        })

    def ensure_history_baseline(self) -> int:
        """只为**尚无历史**的现有任务补一次"现状收录"快照。

        重复初始化/迁移不会重复收录（判据是任务在该库里是否已有任何历史行）。
        旧资料里没有可靠来源的起始日、逐日时间与已丢失参数一律不猜。
        """
        with self._mutation_lock:
            try:
                with self.db.transaction() as conn:
                    known = self.db.history_task_ids(conn)
                    rows = TaskDB.rows_from_conn(conn)
                    added = 0
                    for row in rows:
                        if int(row['id']) in known:
                            continue
                        self._record_history(
                            conn, row['id'], 'baseline', after=row,
                            source=HISTORY_SOURCE_MIGRATION,
                            detail='初始化现状收录（旧数据无逐日时间与起始日）')
                        added += 1
            except Exception as exc:
                logger.warning(f"现状收录失败（不影响任务加载）：{exc}")
                return 0
        if added:
            logger.info(f" 已为 {added} 个尚无历史的现有任务补一次现状快照")
        return added

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
                    # 计算日期是一条**参数**，不是第二份状态：只有"隔离模式、
                    # 今天尚未准备"时，才把原数据日期传给同一条公式 —— 否则一笔
                    # 补记**昨日**的合法事务会顺手把整库派生优先度按今天改写，
                    # 抢在正式日切快照之前改掉昨日简报依据（实测 9098 → 8998）。
                    # 默认构造与日切自己的重算都走省略参数的分支，与原实现逐字
                    # 相同。
                    #
                    # 这个"原数据日期"的**来源优先级**：本实例最近一次**真正完成**
                    # 的日切日（``reset_completed_date``）是比"构造时从文件读入"
                    # 更近的实际事实 —— 服务连续运行跨过午夜后，日切更新的是内存里
                    # 的这个日期，而 ``_last_run_date`` 仍停留在启动那天（它只在
                    # 构造期加载）。只有从未完成过日切（该值为空）才回落到构造
                    # 读入的文件日期。这里**不能**再要求文件日期非空：首次启动没有
                    # 日期文件、但已经正常日切过一天时，完成日同样可信。
                    compute_day = None
                    if (not self._reset_scheduler_enabled
                            and self.reset_completed_date != date.today()):
                        compute_day = (self.reset_completed_date
                                       or self._last_run_date)
                    if compute_day is None:
                        computed, corrections = self._compute_static_rows(tasks)
                    else:
                        computed, corrections = self._compute_static_rows(
                            tasks, as_of=compute_day)
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
        crossed = bool(self._last_run_date) and self._last_run_date != today
        #: 跨日且是隔离构造（reset_scheduler=False）：**整段日切准备**（清标记、
        #: 负优先度移出、派生优先度重算、进度补正）都还没发生，必须原样留给真实
        #: 数据端口的 reset_daily_flags。初始化在这里只加载，不改写。
        defer_day_prep = crossed and not self._reset_scheduler_enabled
        if crossed and self._reset_scheduler_enabled:
            logger.info(f" 检测到跨日：上次运行={self._last_run_date}, 今天={today}")
            logger.info("步骤 2.1: 清空'今日是否完成'标志...")
            self._clear_today_done_column()
        elif crossed:
            # 隔离模式：跨日准备**统一**交给真实数据端口的 reset_daily_flags ——
            # 它在清 today_done 之前先冻结一份一致的昨日简报快照，之后才做移出／
            # 重算。所以下面步骤 3／4 **不能**在这里先跑一遍：那几个步骤本身就是
            # 日切准备（按今天的日期把负优先度任务移出、改派生优先度、补正进度），
            # 抢在正式快照之前执行就会把昨日简报所依赖的源行改写掉（实测
            # a_val 99 → -1）。只把清标记推迟是不够的 —— 只保留 today_done 一个
            # 字段，并不等于完整保留"重置前快照"。
            logger.info(f" 检测到跨日：上次运行={self._last_run_date}, 今天={today}"
                        "（隔离模式：整个日切准备交由 reset_daily_flags 统一完成，"
                        "初始化不改写昨日源行）")
        else:
            if self._last_run_date:
                logger.info(f" 同一天运行：{today}（上次运行：{self._last_run_date}）")
            else:
                logger.info(f" 首次运行或无上次运行记录：{today}")
        if not defer_day_prep:
            logger.info("步骤 3: 检查负优先度任务...")
            self._check_and_complete_negative_priority_tasks()
        logger.info("步骤 3.1: 为尚无历史的现有任务补一次现状快照...")
        self.ensure_history_baseline()
        if not defer_day_prep:
            logger.info("步骤 4: 计算静态值并按优先度排序...")
            self._compute_and_save_static()
        # 初始化完成后，"今天"的状态确实已经就绪（标志已清、优先度已重算）：
        # 把重置屏障推进到今天，跨日等待才有明确依据；否则监控会一直等到超时。
        # 隔离模式下**只有**"今天已经准备过"（上次运行就是今天）才这样推进 ——
        # 带昨日库启动时把准备留给 reset_daily_flags，同日重启仍不重复清标志/投递。
        if not defer_day_prep:
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

        def mutate(conn):
            rows = self._reload_rows(conn)
            return self._complete_negative_priority_in(
                conn, [self._row_to_task(r) for r in rows]) > 0

        # 走统一变更入口：移出、人工代数、历史、派生值重算与版本同一次提交。
        self._commit_change(mutate)
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

    def select_for_run(self, task_id: Optional[int] = None,
                       business_date: Optional[str] = None,
                       expected_revision: Optional[int] = None
                       ) -> Tuple[Optional[LiveInstruction], int]:
        """**一次一致性读取**：任务行、资格参数、当前版本与人工进度代数取自同一个 DB 快照。

        返回 ``(instruction, guard)``；没有可执行任务时 ``(None, 0)``。

        为什么必须一次读完：先后读"任务行"与"人工代数"会让一次并发的人工修改
        插在中间，冻结到的凭据与之对应的进度不一致；只读内存里的任务列表更糟 ——
        另一个 TaskManager（独立连接、独立实例锁，例如同库的另一个实例进程）刚改过
        每日时长时，内存可能还是旧值，于是"旧时长 + 新凭据"被拼成一次合法选择
        （复核 R1）。这里在**一个真实读事务**里取全表与该任务的人工代数，
        指令按既有公式从这一份行构造，不在选取引擎里复制第二份优先度公式。

        指定任务（``task_id``）的**版本屏障也取自这一份读取**：先前的写法拿内存
        版本过门、再读 DB 新参数，等于把"确认版本"和"任务行"分成两份事实 ——
        同库被另一个实例改动后，旧版本票据被接受并套上新参数，而**真正的**当前
        DB 版本反而被判成过期（复核 A1）。业务日/身份/可执行性同样按这一份行
        核对；得到的指令仍带上既有的 ``source_revision`` / ``source_business_date``
        前置条件载荷，语义与 :meth:`select_task_instruction` 一致。
        """
        with self._mutation_lock:
            with self.db.coherent_read() as conn:
                rows = self.db.rows_from_conn(conn)
                if task_id is None:
                    instruction = self._instruction_from_rows(rows, None)
                    if instruction is None:
                        logger.warning(" 无待执行任务")
                        return None, 0
                else:
                    self._check_named_business_day(business_date)
                    try:
                        named_id = int(task_id)
                    except (TypeError, ValueError):
                        raise TaskMutationRejected(
                            '任务身份不是可识别的整数 id',
                            code='start_task_missing')
                    row = self._row_by_id(rows, named_id)
                    current = self._snapshot_revision(conn, rows)
                    self._check_named_revision(
                        expected_revision, current,
                        (row or {}).get('zone_name') or '')
                    # strict：身份必须仍在这一份行里且仍待执行，否则明确拒绝。
                    instruction = self._instruction_from_rows(
                        rows, named_id, strict=True)
                    if instruction is None:      # strict=True 时不会返回 None
                        return None, 0
                    instruction.source_revision = int(current)
                    instruction.source_business_date = business_date
                guard = self.db.read_progress_guard(conn, instruction.task_id)
            # 与既有选取路径一样把结果写进日志，运维可见性不因为换了读取实现而降低。
            logger.info(f"→ 选定直播指令：{instruction}")
            return instruction, guard

    def _snapshot_revision(self, conn, rows: List[dict]) -> Optional[int]:
        """同一份 DB 读快照里、与这批行一致的版本号（**只读，不写元数据**）。

        口径与 :meth:`_resolve_revision` 相同：元数据与这批行一致时用已登记的
        版本；缺失或指纹不符（数据被"没登记版本"的一方改过）时按数据推进一档，
        让那一期间发出的确认作废。区别只在读取路径**不落盘对齐** —— 对齐留给
        下一次正常加载，这里不产生新版本。
        """
        try:
            fingerprint = TaskDB.fingerprint_rows(rows)
        except Exception as e:
            logger.error(f" 计算任务数据指纹失败（版本不可用）：{e}")
            return None
        stored_revision, stored_fingerprint = self.db.read_revision_meta(conn)
        if stored_revision is not None and stored_fingerprint == fingerprint:
            return int(stored_revision)
        if stored_revision is None:
            return self._derive_revision(fingerprint)
        return int(stored_revision) + 1

    @staticmethod
    def _row_by_id(rows: List[dict], task_id) -> Optional[dict]:
        """在一份已读到的行集合里按稳定身份取行（不另开连接、不读内存）。"""
        try:
            wanted = int(task_id)
        except (TypeError, ValueError):
            return None
        return next((r for r in rows
                     if r.get('id') is not None and int(r['id']) == wanted), None)

    def _prepared_business_day_iso(self) -> str:
        """自动场次预留核对的"当前业务日"：与 :meth:`_day_has_passed` 同源。

        真实数据层在选取前已经用**同一个业务日**做过 ``reset_daily_flags``；这里沿用
        它已发布的日期，缺失才回落系统日期 —— 不在预留路径上另建第二个"今天"，
        否则隔离时钟跨日时新的一天永远无法完成首场预留。
        """
        current = self.reset_completed_date or self.last_reset_date
        return (current or date.today()).isoformat()

    @staticmethod
    def _check_named_business_day(business_date) -> str:
        """业务日屏障：**缺失同样拒绝**（两条指定选择路径共用同一口径）。

        版本与业务日是"用户确认那一刻"的完整凭证，少一样就无法证明这次选择
        还是用户看到的那一份。
        """
        today_str = date.today().isoformat()
        if not business_date:
            raise TaskMutationRejected(
                '启动任务的请求缺少确认时的业务日（business_date）：'
                '请刷新任务列表后重新选择要开播的任务',
                code='start_requires_business_day')
        if business_date != today_str:
            raise TaskMutationRejected(
                f'这次选择的任务来自 {business_date}，当前业务日已是 '
                f'{today_str}：请刷新任务列表后重新选择要开播的任务',
                code='start_stale_business_day')
        return business_date

    @staticmethod
    def _check_named_revision(expected_revision, current_revision,
                              zone: str) -> int:
        """版本屏障：确认版本必须等于**当前**版本（两条指定选择路径共用）。

        ``current_revision`` 由调用方给出，两种来源**绝不混用**：
        :meth:`select_task_instruction` 给已发布的内存版本（那是该 API 的既有
        口径），:meth:`select_for_run` 给同一份 DB 读快照里的版本。把内存版本
        过门、再去另一个快照取参数，正是复核 A1 的现象。
        """
        if expected_revision is None:
            raise TaskMutationRejected(
                f'启动任务「{zone}」的请求没有带上确认时的'
                f'任务列表版本：请刷新任务列表后重新选择',
                code='start_requires_revision')
        if current_revision is None:
            raise TaskMutationRejected(
                f'无法确定「{zone}」的当前任务列表版本：'
                f'请刷新任务列表后重新选择',
                code='start_revision_unavailable')
        if int(expected_revision) != int(current_revision):
            raise TaskMutationRejected(
                f'任务列表在本次确认之后已被改动（当前版本 {int(current_revision)}，'
                f'本次选择基于版本 {int(expected_revision)}）：'
                f'请刷新任务列表后重新选择要开播的任务',
                code='start_stale_revision')
        return int(expected_revision)

    def _instruction_from_rows(self, rows: List[dict],
                               task_id: Optional[int],
                               strict: bool = False) -> Optional[LiveInstruction]:
        """按**既有规则**从一份已读到的任务行构造开播指令。

        - ``task_id is None``：自动排序 —— 行已按 ``a_val`` 升序，取第一条
          "今日未完成且 category>0"，与 :meth:`select_next_instruction` 同义；
        - 指定 task_id：身份必须仍在这一份行里且仍待执行，否则按既有语义明确
          拒绝（``strict`` 时抛 ``TaskMutationRejected``）。

        时长仍由 :meth:`Task.to_instruction` 唯一一处计算，不在这里重写公式。
        """
        tasks = [self._row_to_task(row) for row in rows]
        if task_id is None:
            chosen = next((t for t in tasks if t.needs_execution()), None)
            return chosen.to_instruction() if chosen is not None else None
        chosen = next((t for t in tasks
                       if t.id is not None and int(t.id) == int(task_id)), None)
        if chosen is None:
            if not strict:
                return None
            raise TaskMutationRejected(
                f'任务 {task_id} 已不在任务库里（可能刚被删除），请刷新任务列表',
                code='start_task_missing')
        if not chosen.needs_execution():
            raise TaskMutationRejected(
                f'任务「{chosen.zone_name}」当前无需执行（今日已完成或已标记为完成）',
                code='start_task_not_executable')
        return chosen.to_instruction()

    def select_task_instruction(self, task_id, expected_revision=None,
                                business_date=None) -> LiveInstruction:
        """按**稳定身份**解析"点哪一条就播哪一条"的开播指令。

        任务列表点某一行时，被执行的那一条必须是用户看到的**那一行**，而
        不是"当前优先度最高的那一条"。这里与覆盖写共用同一套前置条件语义
        （任务列表版本 + 业务日），并且要求任务现在仍然待执行：

        - ``task_id`` 必须仍存在于任务库（期间被删除 → 拒绝，不自动换一条）；
        - 名称必须是当前库里的名称（期间被同名重建/改名 → 拒绝）；
        - ``expected_revision`` 必须等于已发布版本（期间结算/编辑/导入/轮转
          都算变化 → 让用户重新确认，而不是替他换一个目标）；
        - ``business_date`` 必须还是今天（跨日的旧选择作废）；**缺失同样
          拒绝**——版本与业务日是"用户确认那一刻"的完整凭证，少一样就无法
          证明这次选择还是用户看到的那一份。

        时长公式与 :meth:`Task.to_instruction` 完全一致，这里不重新实现，也
        不因为"被点名"就改变抽样方式。

        屏障判定与 :meth:`select_for_run` 共用（:meth:`_check_named_business_day`
        / :meth:`_check_named_revision`），区别只在**版本来源**：这条公开路径
        沿用"已发布的内存版本"这一既有口径。
        """
        if task_id is None:
            raise TaskMutationRejected('缺少要启动的任务身份（task_id）',
                                       code='start_requires_task_id')
        self._check_named_business_day(business_date)
        with self._mutation_lock:
            try:
                row = self.db.get_task_by_id(int(task_id))
            except (TypeError, ValueError):
                raise TaskMutationRejected('任务身份不是可识别的整数 id',
                                           code='start_task_missing')
            if not row:
                raise TaskMutationRejected(
                    '该任务已不存在（可能刚被删除），请刷新任务列表',
                    code='start_task_missing')
            self._check_named_revision(expected_revision, self.tasks_revision,
                                       row.get('zone_name') or '')
            current = self.tasks_revision
            zone = row.get('zone_name') or ''
            task = next(
                (t for t in self.tasks
                 if t.id is not None and int(t.id) == int(task_id)), None)
            if task is None or task.zone_name != zone:
                raise TaskMutationRejected(
                    f'任务身份已变化（名字与该 id 不再对应），'
                    f'请刷新任务列表后重新选择',
                    code='start_task_changed')
            if not task.needs_execution():
                raise TaskMutationRejected(
                    f'任务「{zone}」当前无需执行（今日已完成或已标记为完成）',
                    code='start_task_not_executable')
            # 冻结前置条件随指令一起走：select 与 reserve 之间允许任何任务
            # CRUD 并发，预留/接管时刻据此在同一把 _mutation_lock 里复核。
            instruction = task.to_instruction()
            instruction.source_revision = int(current)
            instruction.source_business_date = business_date
            return instruction

    def validate_named_selection(self, task_id, zone_name,
                                 expected_revision, business_date,
                                 row) -> Optional[str]:
        """复核"用户点中的那一条"现在仍然成立；返回拒绝原因码，``None``=成立。

        **必须**在 :meth:`reserve_active_task` 的 ``_mutation_lock`` 临界区内
        调用（由控制器的 validator 回调进入）：select 与 reserve 之间允许任何
        任务 CRUD 并发——改名/结算/删除/编辑都可能插进来。逐项核对：

        - 业务日仍是今天且**存在**（缺失无法证明是用户确认的那一份）；
        - 任务列表版本仍等于确认时冻结的版本（任何提交都会推进版本）；
        - 行仍在、名称仍与该 id 对应、任务仍待执行。

        任何一项不成立都让这次选择作废，而不是启动一条用户没有确认过的旧分区。
        """
        if not business_date:
            return 'start_requires_business_day'
        if business_date != date.today().isoformat():
            return 'start_stale_business_day'
        if expected_revision is None:
            return 'start_requires_revision'
        current = self.tasks_revision
        if current is None:
            return 'start_revision_unavailable'
        try:
            if int(expected_revision) != int(current):
                return 'start_stale_revision'
        except (TypeError, ValueError):
            return 'start_revision_unavailable'
        if not row:
            return 'start_task_missing'
        if str(row.get('zone_name') or '') != str(zone_name or ''):
            return 'start_task_changed'
        try:
            task = next(
                (t for t in self.tasks
                 if t.id is not None and task_id is not None
                 and int(t.id) == int(task_id)), None)
        except (TypeError, ValueError):
            return 'start_task_missing'
        if task is None or task.zone_name != (row.get('zone_name') or ''):
            return 'start_task_changed'
        if not task.needs_execution():
            return 'start_task_not_executable'
        return None

    # ---------- 结算身份 ----------
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

    def reserve_task_for_run(self, *, task_id: int, run_id: str,
                             zone_name: str, business_date: str,
                             expected_revision: Optional[int] = None,
                             settlement_guard: Optional[int] = None
                             ) -> Tuple[bool, str]:
        """按真实 SQLite 快照复核任务身份、资格与 guard，再登记 Run 预留。

        ``expected_revision`` 只由仍持有原指定任务 Selection 的首次 Start 传入。
        自动选择、已确认场次的恢复/重试只核同一行、资格、业务日与可选 guard，
        不会被其他任务的合法修改误判成旧人工票据。
        """
        try:
            named_ticket = expected_revision is not None
            if named_ticket:
                self._check_named_business_day(business_date)
            elif business_date != self._prepared_business_day_iso():
                return False, 'start_task_business_day_changed'

            with self._mutation_lock:
                with self._active_lock:
                    if (self._active_run_id is not None
                            and self._active_run_id != run_id):
                        return False, 'start_another_run_reserved'

                with self.db.coherent_read() as conn:
                    rows = self.db.rows_from_conn(conn)
                    row = self._row_by_id(rows, task_id)
                    if row is None:
                        return False, 'start_task_missing'
                    if str(row.get('zone_name') or '') != str(zone_name or ''):
                        return False, 'start_task_changed'

                    if named_ticket:
                        current_revision = self._snapshot_revision(conn, rows)
                        self._check_named_revision(
                            expected_revision, current_revision,
                            row.get('zone_name') or '')

                    task = self._row_to_task(row)
                    if not task.needs_execution():
                        return False, 'start_task_not_executable'

                    current_guard = self.db.read_progress_guard(conn, task_id)
                    if (settlement_guard is not None
                            and int(current_guard) != int(settlement_guard)):
                        return False, 'start_task_guard_changed'

                with self._active_lock:
                    self._active_task_id = int(task_id)
                    self._active_run_id = str(run_id)
                return True, ''
        except TaskMutationRejected as exc:
            return False, exc.code
        except Exception as exc:
            logger.warning('Start 前任务预留复核失败（fail closed）：%s', exc)
            return False, f'start_reservation_failed:{type(exc).__name__}'

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

    # NOTE(M1)：旧"覆盖"前置条件的三条助手（`_removes_committed_progress` /
    # `_require_overwrite_preconditions` / `_consistent_last_done`）已随
    # UPDATE-旧-id 覆盖语义一并退役。新建入口现在一律产生新身份、新进度 0，
    # 不触碰任何已提交的进度事实，因此那三条判断在新路径上不可达；它们对应的
    # 确认门由 `_preview_mismatch`（冻结预检的身份/版本/业务日复核）承接。
    # 逐条登记见 docs/plans/multi-instance-console-20261005/M1-MIGRATION-REGISTER.md。

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
            # **账本优先**：完成账本是"按日是否已经记过"的唯一权威。用户刷新确认
            # 后把今日标记覆盖掉（这是既有的合法覆盖），并不能让这一天被记第二次；
            # 这里与 settle_completion 的先查顺序一致，也不做任何写入 —— 人工结果
            # 不被覆盖，账本记录也不因覆盖消失（阶段 2 §3.1/§3.3）。
            existing = self.db.lookup_completion(conn, real_id, exec_day)
            if existing:
                result['status'] = 'already'
                result['days_done'] = row.get('days_done')
                result['ledger_receipt'] = existing['receipt']
                result['ledger_source'] = existing['source']
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
            # 人工/兼容入口改了进度事实：推进本任务的人工进度代数，让「停止/重启后
            # 仍待提交的旧 End」不会在这一天再加一次或覆盖这次结果（阶段 2 §3.2）。
            self.db.bump_progress_guard(conn, real_id)
            # 这是一条**真实**的完成事实：与任务行、人工代数同事务记入账本，
            # 使随后到达的旧自动完成先查到 already，而不是再加一天。
            self.db.insert_completion(conn, {
                'task_id': real_id, 'business_day': exec_day,
                'run_id': str(run_id or ''), 'receipt':
                    self.db.completion_receipt(real_id, exec_day),
                'source': LEDGER_SOURCE_MANUAL,
                'zone_name': row.get('zone_name') or zone_name,
                'target_seconds': 0.0, 'effective_seconds': 0.0,
                'completed_at': None,
                'committed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'days_done_after': new_days_done, 'total_days_after': total_days})
            after = dict(row)
            after.update(updates)
            self._record_history(conn, real_id, 'completed', before=row,
                                 after=after, source=HISTORY_SOURCE_USER,
                                 detail='标记今日完成（兼容入口）')
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

    # ==================== 完成账本（阶段 2 的窄内部入口） ====================

    #: 一次提交的四种终态。回执必须能区分"计入 / 已经计入 / 不再应用 / 未提交"，
    #: 不能给后续阶段留一个"跳过也宣告成功"的假事实。
    COMPLETION_CREDITED = 'credited'
    COMPLETION_ALREADY = 'already'
    COMPLETION_NOT_APPLIED = 'not_applied'
    COMPLETION_FAILED = 'failed'

    def _completion_outcome(self, status: str, **extra) -> dict:
        outcome = {'status': status, 'reason': '', 'receipt': '', 'already': False,
                   'days_done': None, 'all_done': False, 'task_id': None,
                   'business_day': ''}
        outcome.update(extra)
        return outcome

    @staticmethod
    def _not_applied_receipt(reason: str, task_id, business_day) -> str:
        """"明确不再应用"的稳定终态标记。

        它随既有 ``EndDecision.ledger_receipt`` 一起跨重启保存，因此"未应用"不会被
        后来的重试改写成成功，也不会被误读成一次真实完成。
        """
        return f'not-applied:{reason}:{task_id}:{business_day}'

    def _is_prepared_business_day(self, business_day, today: date) -> bool:
        """这一份完成是否算"当前业务日的完成"（决定要不要写 ``today_done``）。

        正常时刻就是墙钟日这一天。差别只在业务日刚翻篇、而本实例还没做完这一天
        日切的那一小段：库里的"当前业务日"（``reset_completed_date`` /
        ``last_reset_date``）仍然停在刚结束的那一天，那一刻按**既定午夜完成窗口**
        计入旧日的完成，是那一场自己的当日完成。不这么认，00:00 冻结的昨日简报就会
        把这份已入账的完成记成 0 项（2026-09-21 DS4 的同一形状；本轮复核 R2）。

        **更早**的业务日（日切已经收走）永远不写标记：历史补记不污染今天，
        ``last_done_date`` 的既有保护也一字不动。
        """
        if str(business_day) == today.isoformat():
            return True
        prepared = self.reset_completed_date or self.last_reset_date
        return prepared is not None and str(business_day) == prepared.isoformat()

    def settle_completion(self, *, task_id, business_day, run_id: str,
                          settlement_guard, effective_seconds: float,
                          target_seconds: float = 0.0, zone_name: str = '',
                          area_id=None, completed_at: Optional[str] = None,
                          source: str = LEDGER_SOURCE_AUTO,
                          today: Optional[date] = None) -> dict:
        """**窄内部入口**：把一份已保存的可信结束事实记进真实账本。

        与公开的 :meth:`settle_task_done` 的区别是**它不放开日期屏障**：调用方
        （新核心的账本适配器）已经严格核对过"这是本进程持久 End 里那一份
        (run, task, business_day, settle=True, completed, 秒数达标)"。它只处理
        历史业务日的合法结算，不提供任何"历史补账 API"，也不改变公开入口的
        今日规则。

        提交顺序（全部在**同一连接、同一事务**）：

        ``先查既有账本 → 再核对稳定身份与人工进度凭据 → 按当前 DB 值加一天 →
        复用原公式重算并推进同库版本 → 写账本与稳定回执 → COMMIT``

        任一步失败整体回滚，绝不返回"已完成"。重复提交**先**返回既有回执，
        不先拿后来的人工凭据去拒绝一笔已经计过账的旧回执。

        ``settlement_guard`` 是选取任务时冻结的人工进度代数。**缺凭据**的历史
        快照不会在这里临时补当前值（那会让一份旧确认重新有效），除非账本里已经
        记过 —— 那仍然先返回 ``already``。
        """
        today = today or date.today()
        outcome = self._completion_outcome(self.COMPLETION_FAILED,
                                           task_id=task_id,
                                           business_day=str(business_day or ''))

        try:
            day = date.fromisoformat(str(business_day))
        except (TypeError, ValueError):
            return self._completion_outcome(self.COMPLETION_NOT_APPLIED,
                                            task_id=task_id,
                                            business_day=str(business_day or ''),
                                            reason='invalid_business_day')
        if day > today:
            # 未来业务日不是完成事实：不写账本、不加天。
            return self._completion_outcome(self.COMPLETION_NOT_APPLIED,
                                            task_id=task_id,
                                            business_day=str(business_day),
                                            reason='future_day')
        if task_id is None:
            return self._completion_outcome(self.COMPLETION_NOT_APPLIED,
                                            business_day=str(business_day),
                                            reason='no_task_identity')

        def mutate(conn) -> bool:
            # 1) 既有账本优先：重复提交不再比较新凭据、不重算、不加天。
            existing = self.db.lookup_completion(conn, task_id, business_day)
            if existing:
                outcome.update(status=self.COMPLETION_ALREADY, already=True,
                               receipt=existing['receipt'],
                               days_done=existing['days_done_after'],
                               task_id=existing['task_id'])
                return False
            if settlement_guard is None:
                reason = 'no_guard'
                outcome.update(status=self.COMPLETION_NOT_APPLIED, reason=reason,
                               receipt=self._not_applied_receipt(
                                   reason, task_id, business_day))
                return False

            # 2) 稳定身份与人工进度凭据（同一事务内核对）。
            row = conn.execute("SELECT * FROM tasks WHERE id = ?",
                               (int(task_id),)).fetchone()
            row = dict(row) if row else None
            if row is None:
                reason = 'task_missing'
                outcome.update(status=self.COMPLETION_NOT_APPLIED, reason=reason,
                               receipt=self._not_applied_receipt(
                                   reason, task_id, business_day))
                return False
            current_guard = self.db.read_progress_guard(conn, task_id)
            if int(current_guard) != int(settlement_guard):
                # 用户真的改过这个任务的进度事实：旧自动完成**不加天、不覆盖**，
                # 形成明确"不再应用"的终态，而不是无限重试或待核查阻断。
                reason = 'guard_changed'
                outcome.update(status=self.COMPLETION_NOT_APPLIED, reason=reason,
                               receipt=self._not_applied_receipt(
                                   reason, task_id, business_day))
                return False
            if int(row.get('category') or 0) == 0:
                # 已全部完成 / 已被"不可完成移出"：人工结果不覆盖。
                reason = 'task_closed'
                outcome.update(status=self.COMPLETION_NOT_APPLIED, reason=reason,
                               receipt=self._not_applied_receipt(
                                   reason, task_id, business_day))
                return False
            # 兼容列（旧代码也会写）的既有完成标记仍然成立，但**只针对当天**：
            # 历史业务日不读当天标记，也不因此改动它。
            if str(business_day) == today.isoformat():
                if int(row.get('today_done') or 0) == 1:
                    reason = 'today_already_marked'
                    outcome.update(status=self.COMPLETION_NOT_APPLIED,
                                   reason=reason,
                                   receipt=self._not_applied_receipt(
                                       reason, task_id, business_day))
                    return False
                if str(row.get('last_done_date') or '') == str(business_day):
                    reason = 'day_already_marked'
                    outcome.update(status=self.COMPLETION_NOT_APPLIED,
                                   reason=reason,
                                   receipt=self._not_applied_receipt(
                                       reason, task_id, business_day))
                    return False

            # 3) 按**当前 DB 值**加一天（不把 Run 里冻结的整行任务载荷写回库）。
            new_days = int(row.get('days_done') or 0) + 1
            total_days = int(row.get('total_days') or 1)
            all_done = new_days >= total_days
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            is_today = self._is_prepared_business_day(business_day, today)
            today_done = 1 if is_today else row.get('today_done')
            existing_last = str(row.get('last_done_date') or '')
            # 历史业务日不得把较新的 last_done_date 倒退回去，也不清今天已有完成。
            last_done = (str(business_day)
                         if not existing_last or existing_last <= str(business_day)
                         else existing_last)
            conn.execute(
                "UPDATE tasks SET days_done = ?, today_done = ?, last_done_date = ?"
                + (", category = 0" if all_done else "")
                + ", updated_at = ? WHERE id = ?",
                (new_days, today_done, last_done, now_str, row['id']))

            # 4) 账本与稳定回执（唯一约束由数据库承重）。
            receipt = self.db.completion_receipt(row['id'], business_day)
            self.db.insert_completion(conn, {
                'task_id': row['id'], 'business_day': str(business_day),
                'run_id': str(run_id or ''), 'receipt': receipt, 'source': source,
                'zone_name': zone_name or row.get('zone_name') or '',
                'area_id': area_id,
                'target_seconds': float(target_seconds or 0.0),
                'effective_seconds': float(effective_seconds or 0.0),
                'completed_at': completed_at,
                'committed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'days_done_after': new_days, 'total_days_after': total_days})
            after = dict(row)
            after.update({'days_done': new_days,
                          'category': 0 if all_done else row.get('category')})
            # 5) 历史：与账本、任务行同事务。最后一天的完成与账本一起提交。
            self._record_history(conn, row['id'], 'completed', before=row,
                                 after=after, source=source,
                                 detail=f'业务日 {business_day}')
            outcome.update(status=self.COMPLETION_CREDITED, receipt=receipt,
                           days_done=new_days, all_done=all_done,
                           task_id=row['id'])
            return True

        with self._mutation_lock:
            ok = self._commit_change(mutate)
        if not ok:
            if outcome['status'] == self.COMPLETION_NOT_APPLIED:
                return outcome
            # 未提交（含已被回滚）：先看账本里是不是已经有这一笔（例如两个独立
            # 连接争抢首次提交时的败者）。绝不在这里报告"已计入"。
            existing = self._lookup_completion_fresh(task_id, business_day)
            if existing:
                return self._completion_outcome(
                    self.COMPLETION_ALREADY, task_id=existing['task_id'],
                    business_day=str(business_day), already=True,
                    receipt=existing['receipt'],
                    days_done=existing['days_done_after'])
            outcome['status'] = self.COMPLETION_FAILED
            return outcome
        if outcome['status'] == self.COMPLETION_CREDITED:
            self._save_last_run_date()
        return outcome

    def _lookup_completion_fresh(self, task_id, business_day) -> Optional[dict]:
        """用**新连接**读账本（用于"提交后重启 / 回包丢失"的重试路径）。"""
        if task_id is None:
            return None
        try:
            with self.db._get_conn() as conn:
                return self.db.lookup_completion(conn, task_id, business_day)
        except Exception as exc:
            logger.warning(f"读取完成账本失败：{exc}")
            return None

    def lookup_completion(self, task_id, business_day) -> Optional[dict]:
        """只读查询：该 (task_id, business_day) 是否已经在账本里。"""
        return self._lookup_completion_fresh(task_id, business_day)

    def completion_ledger_rows(self) -> List[dict]:
        """账本全量（只读；测试与报告用，不参与直播决策）。"""
        with self.db._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM task_completion_ledger ORDER BY id").fetchall()]

    def task_history_rows(self, task_id=None) -> List[dict]:
        """任务历史（只读；测试与报告用）。"""
        with self.db._get_conn() as conn:
            if task_id is None:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM task_history ORDER BY id").fetchall()]
            return [dict(r) for r in conn.execute(
                "SELECT * FROM task_history WHERE task_id = ? ORDER BY id",
                (int(task_id),)).fetchall()]

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
            # 人工"全部完成"改了进度事实：推进人工代数，并留下"填满前的真实进度"，
            # 不能事后从 category=0 猜成"逐日都播满了"。
            self.db.bump_progress_guard(conn, row['id'])
            after = dict(row)
            after.update({'category': 0, 'days_done': total_days, 'today_done': 1})
            self._record_history(conn, row['id'], 'all_done', before=row, after=after,
                                 source=HISTORY_SOURCE_USER,
                                 detail='人工标记全部完成（非自动播满）')
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
            # 重置前先冻结**一份一致**的简报快照（统计 + 优先度前 10 项同源同版本，
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
        """按当前已发布快照取优先度前 10 个未完成任务（保留旧方法名）。"""
        return self._compute_top5_from_rows(self.get_snapshot()[1])

    def _compute_top5_from_rows(self, rows: List[dict]) -> List[dict]:
        """从**冻结行**计算优先度前 10 项（保留旧方法名与快照键）。

        ``remaining_days`` 用权威派生口径 ``Task.remaining_exec_days``（与
        计算列 I 同义），不再读旧列——旧列默认 1 且从不重算，是"邮件里
        剩余天数全为 1"的根源（2026-09-21 DS1）。
        """
        tasks = [self._row_to_task(r) for r in rows]
        top = sorted([t for t in tasks if t.category > 0],
                     key=lambda t: t.priority)[:10]
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
        """冻结一份简报快照：统计与优先度前 10 项来自同一份行、同一版本。

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
        """事务内处理负优先度任务（既有业务规则，不改公式）。

        这一步会把 ``category`` 置 0、``days_done`` 填到 ``total_days``。因此必须
        在**同一事务**里留下"不可完成移出"与**填满前的真实进度**：事后从
        category=0 无法区分"逐日播满"与"超期移出"，也就无法给后续的历史查询
        一个诚实答案。同时推进人工进度代数，使该任务上仍待提交的旧自动完成
        不再加天或覆盖这次移出。
        """
        negative = [t for t in tasks if t.priority < 0 and t.category > 0]
        if not negative:
            return 0
        logger.warning(f" 检测到 {len(negative)} 个负优先度未完成任务")
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for task in negative:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?",
                               (task.id,)).fetchone()
            before = dict(row) if row else {
                'zone_name': task.zone_name, 'category': task.category,
                'total_days': task.total_days, 'days_done': task.days_done,
                'deadline_raw': task.deadline_formula,
            }
            conn.execute(
                "UPDATE tasks SET category = 0, days_done = ?, updated_at = ? "
                "WHERE id = ?",
                (task.total_days, now, task.id))
            self.db.bump_progress_guard(conn, task.id)
            after = dict(before)
            after.update({'category': 0, 'days_done': task.total_days})
            self._record_history(
                conn, task.id, 'moved_out', before=before, after=after,
                source=HISTORY_SOURCE_NEGATIVE,
                detail=f'负优先度（{task.priority}）不可完成移出；'
                       f'移出前实际进度 {before.get("days_done")}/'
                       f'{before.get("total_days")} 天')
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
        # 日期写入**统一**交给 ``_save_last_run_date`` 自带的那道就绪门（隔离模式
        # 下"今天运行/关闭过"不等于"今天已经重置"）：未准备就关闭时不会推进
        # 运行日期，下一次构造与真实日切入口仍能完成准备；旧默认构造行为不变。
        # 这里不再各调用点散写一遍同样的判断（那个重复门在统一之后已无效果）。
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

    # ==================== 新建预检 / 一次替换事务 / 创建回执（M1） ====================
    #
    # 新建是**产生新身份**的操作，不是改写旧记录：同分区已有任务时，旧行终结为
    # "被新任务覆盖"并留在历史与账本里，新行拿新的 AUTOINCREMENT id、进度从 0
    # 开始。旧 ``create_task(overwrite=True)`` 的 UPDATE 旧 id 语义按用户本次明确
    # 要求退役（见 M1-MIGRATION-REGISTER.md）。

    #: 插入新任务时**只**以"字段是否缺失"判缺省，绝不用 truthiness：``category``
    #: 的合法业务值 0 表示"已完成/不要求时长"，把它当成缺失改写成 1 会把用户
    #: 标成完成的记录变成一条待执行任务（复核 R2）。``total_days`` 的合法下界
    #: 是 1（0 天任务没有意义），因此它仍按"缺失或非正"回落默认值。
    CREATE_FIELD_DEFAULTS = {'category': 1, 'total_days': 1, 'deadline_raw': ''}

    @classmethod
    def _normalize_create_command(cls, command: dict) -> dict:
        """把一份创建命令规范成**唯一**的字段解释，写入与指纹共用这一份。

        指纹与 INSERT 若各自解释"缺省"，同一条令牌就会在不同分支下对应不同的
        新行（例如 ``category`` 缺失时指纹按 0 算、INSERT 落成 1）。这里一次
        定死：``None`` 才是缺省，``0`` 是真实提交值。
        """
        normalized = dict(command)
        category = command.get('category')
        normalized['category'] = (cls.CREATE_FIELD_DEFAULTS['category']
                                  if category is None else int(category))
        total_days = command.get('total_days')
        if total_days is None or int(total_days) <= 0:
            total_days = cls.CREATE_FIELD_DEFAULTS['total_days']
        normalized['total_days'] = int(total_days)
        deadline = command.get('deadline_raw')
        normalized['deadline_raw'] = (cls.CREATE_FIELD_DEFAULTS['deadline_raw']
                                      if deadline is None else str(deadline))
        return normalized

    @staticmethod
    def _is_unfinished_row(row) -> bool:
        """任务**总体**是否仍未完成：待完成且 ``days_done < total_days``。

        这是覆盖确认的判据，**不是** "今天是否需要执行"（``needs_execution``）：
        今天已经做完但总体 3/7 的任务仍然未完成，仍然要确认（C03）。
        """
        if not row:
            return False
        category = int(row.get('category') or 0)
        if category <= 0:
            return False
        return int(row.get('days_done') or 0) < int(row.get('total_days') or 0)

    def preview_creation(self, zone_name: str) -> dict:
        """该分区的一致创建预检：同一份读快照里取行、版本、业务日与占用事实。

        不拼接列表 API / 版本 API / 日期 API 的不同时间回包，也不以缓存列表决定
        冲突。只读：结束一律回滚，不写任何字节。
        """
        zone = str(zone_name or '').strip()
        with self._mutation_lock:
            with self.db.coherent_read() as conn:
                rows = TaskDB.rows_from_conn(conn)
                current = next(
                    (r for r in rows if str(r.get('zone_name') or '') == zone), None)
                revision = self._snapshot_revision(conn, rows)
                business_day = self._prepared_business_day_iso()
                active_id = self.active_task_id()

        reserved = bool(current) and active_id is not None \
            and int(current['id']) == int(active_id)
        unfinished = self._is_unfinished_row(current)
        allowed, reason = True, ''
        if not zone:
            allowed, reason = False, '分区名不能为空'
        elif reserved:
            allowed, reason = False, f'任务「{zone}」正在执行，请先人工下播后重试'
        elif revision is None:
            allowed = False
            reason = f'无法确定「{zone}」的当前任务版本：请刷新任务列表后重试'

        existing = None
        if current is not None:
            existing = {
                'id': int(current['id']),
                'category': int(current.get('category') or 0),
                'total_days': int(current.get('total_days') or 0),
                'days_done': int(current.get('days_done') or 0),
                'deadline_raw': str(current.get('deadline_raw') or ''),
                'last_done_date': current.get('last_done_date'),
            }
        return {
            'zone_name': zone,
            'tasks_revision': revision,
            'business_day': business_day,
            'existing_task': existing,
            'existing_is_unfinished': bool(unfinished),
            'is_currently_reserved': bool(reserved),
            'creation_allowed': bool(allowed),
            'reason': reason,
        }

    @classmethod
    def _create_operation_key(cls, operation_token: str) -> str:
        """把不透明请求令牌映射到有界长度、不泄漏原文的元数据键。"""
        digest = hashlib.sha256(operation_token.encode('utf-8')).hexdigest()
        return f'{META_CREATE_OPERATION_PREFIX}{digest}'

    @classmethod
    def _create_operation_fingerprint(cls, command: dict) -> str:
        """冻结命令的规范指纹：UID、分区、新参数、原预检身份/版本/业务日、确认语义。

        只比新任务名称是不够的：同一份载荷在不同账号、不同预检版本或不同确认语义
        下是**不同的操作**，令牌复用必须能被认出来。
        """
        preview = command.get('preview') or {}
        canonical = {
            'uid': command.get('uid'),
            'zone_name': str(command.get('zone_name') or '').strip(),
            'category': command.get('category'),
            'total_days': command.get('total_days'),
            'deadline_raw': command.get('deadline_raw'),
            'confirmed': bool(command.get('confirmed')),
            'preview': {
                'task_id': preview.get('task_id'),
                'revision': preview.get('revision'),
                'business_day': preview.get('business_day'),
                'existing_is_unfinished': preview.get('existing_is_unfinished'),
            },
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str)
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _read_create_operation(self, conn, operation_token: str) -> Optional[dict]:
        """在**当前事务里**读已提交的创建回执。

        记录存在但读不出来（JSON 坏了 / 形状不对）必须明确失败：把读不出的凭据
        当成"没做过"，正好会让一次已经生效的创建被重放成第二份。
        """
        key = self._create_operation_key(operation_token)
        try:
            record = self.db.read_meta_json(conn, key)
        except Exception as exc:
            raise TaskMutationRejected(
                '无法核对该创建操作的历史结果，请刷新后重新发起创建',
                code='create_replay_unverifiable') from exc
        if record is None:
            return None
        if not isinstance(record, dict) or not isinstance(record.get('result'), dict):
            raise TaskMutationRejected(
                '无法核对该创建操作的历史结果，请刷新后重新发起创建',
                code='create_replay_unverifiable')
        return record

    def _write_create_operation(self, conn, operation_token: str,
                                fingerprint: str, result: dict) -> None:
        """在**任务变更的同一个事务**里落定创建回执。

        刻意**不设容量上限**：一次创建留一行已提交事实，删不掉也复用不了。
        导入回执的容量预算是防重放的容量保护，复制到普通创建会把它变成"终身
        创建次数上限"（交办 §5.2）。
        """
        self.db.write_meta_json(conn, self._create_operation_key(operation_token),
                                {'fingerprint': fingerprint, 'result': dict(result)})

    @staticmethod
    def _preview_mismatch(preview, current, revision, business_day, zone) -> Optional[tuple]:
        """冻结预检与**提交这一刻**的真实状态是否还一致。

        返回 ``(code, reason)``；一致返回 None。只读比较，不做任何写入。
        """
        if revision is None:
            return ('create_revision_unavailable',
                    f'无法确定「{zone}」的当前任务版本：请刷新任务列表后重新确认')
        want_rev = (preview or {}).get('revision')
        if want_rev is None:
            return ('create_revision_unavailable',
                    f'本次创建确认没有带上预检时的任务版本：请刷新后重新确认')
        if int(want_rev) != int(revision):
            return ('create_stale_revision',
                    f'「{zone}」在本次确认之后已被改动（当前版本 {int(revision)}，'
                    f'本次确认基于版本 {int(want_rev)}）：请刷新任务列表后重新确认')
        want_day = (preview or {}).get('business_day')
        if not want_day or str(want_day) != str(business_day):
            return ('create_stale_business_day',
                    f'这次确认来自 {want_day or "未知业务日"}，当前业务日已是 '
                    f'{business_day}：请刷新任务列表后重新确认')
        want_id = (preview or {}).get('task_id')
        if want_id is None:
            if current is not None:
                return ('create_target_changed',
                        f'预检时「{zone}」没有当前任务，提交前出现了另一条记录：'
                        f'请刷新任务列表后重新确认')
            return None
        if current is None or int(current['id']) != int(want_id):
            return ('create_target_changed',
                    f'预检时的「{zone}」任务记录已不存在或被替换：'
                    f'请刷新任务列表后重新确认')
        return None

    def create_new_task(self, command: dict) -> dict:
        """一次创建：预检复核、必要的替换、新身份与新进度、稳定回执，同一事务。

        返回 ``{'status': 'committed'|'replayed'|'not_committed', 'task_id': ...,
        'replaced_task_id': ..., 'revision': ..., ...}``。

        回执重放**在真正变更/重算/发布之前**返回：``mutate`` 返回 False 会让
        ``_commit_change`` 走 ``_NotCommitted``（回滚且不重算派生值、不推进版本、
        不发布快照），绝不用它的返回值假装"只读重放"。
        """
        zone = str(command.get('zone_name') or '').strip()
        if not zone:
            raise TaskMutationRejected('分区名不能为空', code='create_requires_zone')
        raw_token = command.get('token')
        token = raw_token.strip() if isinstance(raw_token, str) else ''
        command = self._normalize_create_command(command)
        preview = command.get('preview') or None
        confirmed = bool(command.get('confirmed'))
        fingerprint = self._create_operation_fingerprint(command) if token else ''

        result: Dict[str, Any] = {
            'status': 'committed', 'zone_name': zone, 'task_id': None,
            'replaced_task_id': None, 'revision': None,
        }
        replay_result: Dict[str, Any] = {}

        def mutate(conn) -> bool:
            nonlocal replay_result
            if token:
                record = self._read_create_operation(conn, token)
                if record is not None:
                    if record.get('fingerprint') != fingerprint:
                        raise TaskMutationRejected(
                            f'该创建令牌已经用于另一份创建内容：请刷新后重新发起创建',
                            code='create_replay_conflict')
                    replay_result = dict(record.get('result') or {})
                    replay_result['status'] = 'replayed'
                    replay_result['replayed'] = True
                    return False

            rows = TaskDB.rows_from_conn(conn)
            current = next(
                (r for r in rows if str(r.get('zone_name') or '') == zone), None)
            if preview is not None:
                mismatch = self._preview_mismatch(
                    preview, current, self._snapshot_revision(conn, rows),
                    self._prepared_business_day_iso(), zone)
                if mismatch is not None:
                    raise TaskMutationRejected(mismatch[1], code=mismatch[0])

            # 在播保护：正在执行的旧任务不能从创建口移除，也不自动停播。
            self._guard_active_row(current, '替换')

            if current is not None and self._is_unfinished_row(current):
                # 未完成的旧任务只能被**针对这一个 id** 的明确确认替换。
                if not (confirmed and preview is not None
                        and preview.get('task_id') is not None
                        and int(preview['task_id']) == int(current['id'])):
                    raise TaskMutationRejected(
                        f'「{zone}」有尚未完成的任务（{int(current.get("days_done") or 0)}/'
                        f'{int(current.get("total_days") or 0)} 天）：'
                        f'请重新取得预检并确认用新任务替换它',
                        code='create_requires_confirmation')
                self._record_history(
                    conn, current['id'], 'replaced', before=current,
                    source=HISTORY_SOURCE_USER,
                    detail=(f'被新任务覆盖；终结前实际进度 '
                            f'{int(current.get("days_done") or 0)}/'
                            f'{int(current.get("total_days") or 0)} 天'))

            replaced_id = None
            if current is not None:
                replaced_id = int(current['id'])
                # 已终结的任务保留原终结事实（completed / all_done / moved_out），
                # 不改写成 replaced / deleted。
                known = self.db.history_task_ids(conn)
                if replaced_id not in known:
                    self._record_history(
                        conn, replaced_id, 'baseline', after=current,
                        source=HISTORY_SOURCE_MIGRATION,
                        detail='替换前现状收录（旧数据无逐日时间与起始日）')
                conn.execute("DELETE FROM tasks WHERE id = ?", (replaced_id,))

            # 新身份：AUTOINCREMENT 取新 id，**不**重置 sqlite_sequence 复用旧 id。
            # 新任务一律从 0 开始：不继承旧 today_done / last_done_date / 有效时长 /
            # Run / 人工凭据。参数取**规范化后**的那一份（与指纹同源）：category 的
            # 0 是真实提交值，不是缺省。
            total_days = int(command['total_days'])
            cursor = conn.execute(
                """INSERT INTO tasks
                (zone_name, category, total_days, days_done, deadline_raw,
                today_done, remaining_days)
                VALUES (?, ?, ?, 0, ?, NULL, ?)""",
                (zone, int(command['category']), total_days,
                 str(command['deadline_raw']), total_days))
            new_id = int(cursor.lastrowid)
            created = dict(conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone())
            self._record_history(
                conn, new_id, 'created', after=created, source=HISTORY_SOURCE_USER,
                detail=(f'新建任务（替换旧任务 {replaced_id}）' if replaced_id
                        else '新建任务'))
            result.update({
                'task_id': new_id,
                'replaced_task_id': replaced_id,
                'category': int(created.get('category') or 0),
                'total_days': int(created.get('total_days') or 0),
                'days_done': 0,
                'deadline_raw': str(created.get('deadline_raw') or ''),
            })
            return True

        def finalize(conn, _rows, revision):
            committed = dict(result)
            committed['revision'] = revision
            result['revision'] = revision
            if token:
                self._write_create_operation(conn, token, fingerprint, committed)

        ok = self._commit_change(mutate, finalize=finalize)
        if replay_result:
            return replay_result
        if not ok:
            result['status'] = 'not_committed'
        return result

    def creation_receipt(self, operation_token: str) -> Optional[dict]:
        """只读查已提交的创建回执（回包丢失后的核对入口）。

        缺记录**只表示**尚未查到成功，不证明请求没执行；这里不做任何写入、不重放
        创建、不发 start/stop。
        """
        if not isinstance(operation_token, str) or not operation_token.strip():
            return None
        key = self._create_operation_key(operation_token.strip())
        try:
            with self.db._get_conn() as conn:
                return self.db.read_meta_json(conn, key)
        except Exception as exc:
            raise TaskMutationRejected(
                '读取创建回执失败，请稍后重试',
                code='create_receipt_unavailable') from exc

    # ==================== 分区历史（只读窄查询） ====================

    #: 历史里表示"这个任务已经终结"的事件，以及它们对应的终结口径。
    TERMINAL_EVENTS = {
        'replaced': 'replaced',          # 被新任务覆盖
        'deleted': 'deleted',            # 人工删除
        'moved_out': 'moved_out',        # 不可完成移出
        'all_done': 'manual_all_done',   # 人工全部完成
    }

    #: 已保存完成事实的来源里，哪些表示"这是**人工**动作，不是平台播满"。
    #: 人工标记最后一天与自动结算都会留下 completed 事件，只有后者的播满是平台事实。
    MANUAL_COMPLETION_EVENT_SOURCES = frozenset({HISTORY_SOURCE_USER})
    MANUAL_COMPLETION_LEDGER_SOURCES = frozenset({LEDGER_SOURCE_MANUAL})

    #: 终结/完成**动作**事件：它们表达的是"这份任务怎么样了"（被覆盖、被删除、
    #: 移出、走完），**不**表达"用户又设置了一次每日要求"。动作前后行上的
    #: ``category`` 可能是被归零的完成标志，也可能是被搬来搬去的同一个值——
    #: 都不算一次参数设置。
    CARRY_COMPLETION_FLAG_EVENTS = frozenset(
        {'completed', 'all_done', 'moved_out', 'replaced', 'deleted'})

    @classmethod
    def _history_required_category(cls, events: List[dict],
                                   current: Optional[dict]) -> Optional[int]:
        """这份任务**当时要求**的每日直播时长（小时/天）。

        任务行的 ``category == 0`` 是当前业务的**完成标志**（"这份任务已经做完"），
        不是"当时要求 0 小时"；正在执行的行则还是真实要求。所以：

        1. 当前行仍在且 ``category > 0``：取现状真值（进行中的任务不改写）；
        2. 否则**顺着时间走一遍**，只认"这一次真的把要求设置/改成了某个值"的
           事件：``created`` / ``baseline`` 设置初值，其它参数事件（``updated`` /
           ``manual_progress`` / ``import``）只有在 ``category`` 前后**真的不同**
           时才算一次新设置。终结/完成动作（:data:`CARRY_COMPLETION_FLAG_EVENTS`）
           一律不算——任务完成之后，删除、改截止日、改进度这些操作带的行快照里仍
           是那个完成标志 0（复核 S1：不能因为后续操作重复携带 0 就把原要求抹掉，
           也**不**为删除、改截止日各加一把特例门）。
        3. 什么都不算 = 未记录（``None``）。原本就按 0 提交的输入照实记 0，旧资料
           缺证据留空。**不**恒取第一次 created、**不**取历史最大值、**不**用账本
           秒数倒推。
        """
        if current is not None and int(current.get('category') or 0) > 0:
            return int(current['category'])
        required: Optional[int] = None
        for event in events:
            kind = str(event.get('event') or '')
            if kind in cls.CARRY_COMPLETION_FLAG_EVENTS:
                continue
            after = event.get('category_after')
            if after is None:
                continue
            try:
                value = int(after)
            except (TypeError, ValueError):
                continue
            before = event.get('category_before')
            try:
                before_value = None if before is None else int(before)
            except (TypeError, ValueError):
                before_value = None
            # 初值（created / baseline，没有 before）或前后真的不同 = 一次真实设置。
            if before_value is None or before_value != value:
                required = value
        return required

    @classmethod
    def _history_params(cls, events: List[dict], current: Optional[dict]) -> dict:
        """该次任务的参数快照：优先现状行，其次最近一条带参数的历史事件。

        ``category``（**当时要求的每日时长**）单独解释，见
        :meth:`_history_required_category`：完成标志 0 不能替代原要求。
        ``total_days`` / ``days_done`` / ``deadline_raw`` 仍是最终进度与截止日，
        取现状行或最后一条带参数的事件快照。

        旧资料缺参数时留 None，由展示层写"未记录"——不从累计天数倒推。
        """
        required = cls._history_required_category(events, current)
        if current is not None:
            return {
                'category': required,
                'total_days': current.get('total_days'),
                'days_done': current.get('days_done'),
                'deadline_raw': current.get('deadline_raw'),
            }
        for event in reversed(events):
            if event.get('category_after') is not None:
                return {'category': required,
                        'total_days': event.get('total_days_after'),
                        'days_done': event.get('days_done_after'),
                        'deadline_raw': event.get('deadline_after')}
            if event.get('category_before') is not None:
                return {'category': required,
                        'total_days': event.get('total_days_before'),
                        'days_done': event.get('days_done_before'),
                        'deadline_raw': event.get('deadline_before')}
        return {'category': required, 'total_days': None, 'days_done': None,
                'deadline_raw': None}

    @staticmethod
    def _saved_completion(events: List[dict], ledger: List[dict]) -> Optional[dict]:
        """从**已保存**的 completed 事件 + 每日账本解释"这个任务真的走完了"。

        为什么不能只看当前行：任务被替换/删除之后当前行就不在了，可那次真实完成
        的事实仍然好好地留在历史与账本里（复核 R3——旧 id 的 terminal 从
        natural_complete 掉成 unknown 就是这么来的）。

        判据只用**可靠前后进度**（completed 事件自己记的 days_done_after /
        total_days_after）：只完成部分天数（1/2）不是整个任务完成。达标时刻取那
        一次结算在账本里的 ``completed_at``（原达标时刻），旧时刻缺失就保持
        "未记录"——**不**拿入账时间或查询时刻顶替。

        同时带回**这次完成是人工还是平台播满**：
        ``event_at`` 是事件自己记录的**发生时刻**；``manual`` 只看可靠来源
        （事件 source / 账本 source），不由"行归零了"倒推——人工标记最后一天
        同样会把行归零（复核 T1）。

        返回 ``{'at', 'event_at', 'business_day', 'source', 'manual'}``；
        没有"走完"的已保存事实返回 None。
        """
        for event in reversed(events):
            if str(event.get('event') or '') != 'completed':
                continue
            after_days = event.get('days_done_after')
            after_total = event.get('total_days_after')
            if after_days is None or after_total is None:
                continue
            try:
                done, total = int(after_days), int(after_total)
            except (TypeError, ValueError):
                continue
            if total <= 0 or done < total:
                return None                      # 最后一次完成也没走完整条任务
            business_day = ''
            detail = str(event.get('detail') or '')
            if detail.startswith('业务日 '):
                business_day = detail[len('业务日 '):].strip()
            at, ledger_source = None, ''
            for row in reversed(ledger):
                if business_day and str(row.get('business_day')) != business_day:
                    continue
                at = row.get('completed_at')
                ledger_source = str(row.get('source') or '')
                break
            source = str(event.get('source') or '')
            manual = (source in TaskManager.MANUAL_COMPLETION_EVENT_SOURCES
                      or ledger_source in TaskManager.MANUAL_COMPLETION_LEDGER_SOURCES)
            return {'at': at, 'event_at': event.get('at'),
                    'business_day': business_day, 'source': source,
                    'ledger_source': ledger_source, 'manual': manual}
        return None

    @staticmethod
    def _completion_terminal(saved: Optional[dict]) -> dict:
        """把一份**已保存**的完成事实翻成终结口径（人工与自然共用这一个出口）。

        两种完成的**来源**必须分开说：人工标记最后一天不是"平台播满"，所以它的
        终结时刻取**人工动作自己记录的发生时刻**（``event_at``），而账本里的
        ``completed_at`` 仍保持缺失——那个字段表达的是平台达标时刻，不拿人工
        动作去冒充它。自动结算的达标时刻仍取账本原 ``completed_at``。
        """
        if saved is None:
            return {'code': 'unknown', 'at': None, 'detail': ''}
        if saved.get('manual'):
            return {'code': 'manual_complete', 'at': saved.get('event_at'),
                    'detail': ''}
        return {'code': 'natural_complete', 'at': saved.get('at'), 'detail': ''}

    @classmethod
    def _history_terminal(cls, events: List[dict], current: Optional[dict],
                          ledger: Optional[List[dict]] = None) -> dict:
        """终结口径**只**认已经发生的事实，不按截止日推断移出、不伪造原因。

        解释顺序：

        1. 已保存的显式终结事件（replaced / deleted / moved_out / all_done）；
        2. 当前行还在时只有"还有时长"能直接说进行中——``category=0`` 只是完成
           标志，它自己证明不了自然播满，**不**拿它下结论，也**不**拿旧完成事实
           倒推（用户把 total_days 改大之后，旧的自然完成不该被读成这个身份的
           终结）；
        3. 其余情形（行还在但已归零 / 行已被移走）走**同一个**完成解释出口：
           真走完了才解释完成，并按那一次的来源区分 ``manual_complete`` 与
           ``natural_complete``；只走了一部分或根本没有事实就不是"完成"。
        """
        ledger = ledger or []
        for event in reversed(events):
            code = cls.TERMINAL_EVENTS.get(str(event.get('event') or ''))
            if code:
                return {'code': code, 'at': event.get('at'),
                        'detail': str(event.get('detail') or '')}
        if current is not None and int(current.get('category') or 0) > 0:
            return {'code': 'ongoing', 'at': None, 'detail': ''}
        saved = cls._saved_completion(events, ledger)
        if saved is not None:
            return cls._completion_terminal(saved)
        if current is not None:
            # 行还在、也没有一份可解释的完成事实：按现状保留进行中，不猜完成。
            return {'code': 'ongoing', 'at': None, 'detail': ''}
        return {'code': 'unknown', 'at': None, 'detail': ''}

    def zone_history(self, zone_name: str, offset: int = 0,
                     limit: int = 20) -> dict:
        """当前账号该分区的历次任务与每日账本（只读）。

        - 关联靠历史/账本/现状三处**可靠的分区快照**，不只查当前行 id；
        - 按 task_id 分组，组内给全部事件与全部每日记录，分页边界不切开组；
        - 不构造 TaskManager/Runtime、不领占用锁、不触发日切、不做历史补写。

        身份集合、事件、账本、当前行取**同一份 SQLite 读快照**（``coherent_read``
        的显式 BEGIN）：同一连接上的多条 SELECT 不自动等于同一快照，另一条连接
        的结算可能在它们之间提交，那样这份响应就会拿"提交前的事件"配"提交后的
        账本/现状"（复核 R4）。读取只读、结束一律回滚。

        读取与组装分成两步（M3 窄提取）：组装本身由 :meth:`assemble_zone_history`
        承担，**只要给一条已经有读快照的连接**就能跑——管理端对**已迁移离线账号**
        的只读历史正是用同一个组装函数，不复制第二套历史算法。这里的行为逐字节
        不变。
        """
        with self.db.coherent_read() as conn:
            return self.assemble_zone_history(conn, zone_name, offset, limit)

    @classmethod
    def assemble_zone_history(cls, conn, zone_name: str, offset: int = 0,
                              limit: int = 20) -> dict:
        """在**调用方给定的一条读快照连接**上组装分区历史（纯读取，不写任何字节）。

        这是 M1 已验收的历史解释的**唯一**出口：``zone_history``（当前账号、
        ``coherent_read`` 快照）与 M3 的离线账号只读查询（各自 ``mode=ro`` 连接 +
        显式 ``BEGIN``）共用它。参数解释、终结判据、分页语义全部按原样保留：不在
        这里另写一套"哪份算完成/哪份算移出"。
        """
        zone = str(zone_name or '').strip()
        offset = max(0, int(offset or 0))
        limit = max(1, min(200, int(limit or 20)))
        if not zone:
            return {'zone_name': zone, 'groups': [], 'total': 0,
                    'offset': offset, 'limit': limit, 'has_more': False}

        ids = TaskDB.zone_history_task_ids(conn, zone)
        events = TaskDB.history_rows_for_tasks(conn, ids)
        ledger = TaskDB.ledger_rows_for_tasks(conn, ids)
        currents = {int(r['id']): dict(r) for r in conn.execute(
            "SELECT * FROM tasks").fetchall()}

        grouped: Dict[int, Dict[str, Any]] = {
            int(tid): {'task_id': int(tid), 'events': [], 'ledger': [],
                       'current': currents.get(int(tid))}
            for tid in ids}
        for event in events:
            grouped[int(event['task_id'])]['events'].append(event)
        for row in ledger:
            grouped[int(row['task_id'])]['ledger'].append(row)

        groups = []
        for tid, bucket in grouped.items():
            current = bucket['current']
            group_events = bucket['events']
            group_ledger = bucket['ledger']
            stamps = [e.get('at') for e in group_events if e.get('at')]
            stamps += [l.get('committed_at') for l in group_ledger
                       if l.get('committed_at')]
            if current is not None:
                stamps += [current.get('created_at'), current.get('updated_at')]
            origin_code, origin_at = 'unknown', None
            for event in group_events:
                if event.get('event') in ('created', 'baseline'):
                    origin_code = str(event['event'])
                    origin_at = event.get('at')
                    break
            groups.append({
                'task_id': tid,
                'zone_name': ((current or {}).get('zone_name')
                              or (group_events[-1].get('zone_name') if group_events
                                  else zone)),
                'is_current': current is not None,
                'params': cls._history_params(group_events, current),
                'origin': {'code': origin_code, 'at': origin_at},
                'terminal': cls._history_terminal(group_events, current,
                                                  group_ledger),
                'last_at': max(stamps) if stamps else None,
                'events': group_events,
                'ledger': group_ledger,
            })
        groups.sort(key=lambda g: (str(g['last_at'] or ''), g['task_id']),
                    reverse=True)
        total = len(groups)
        page = groups[offset:offset + limit]
        return {'zone_name': zone, 'groups': page, 'total': total,
                'offset': offset, 'limit': limit,
                'has_more': offset + len(page) < total}

    # ==================== CRUD 方法（供 API 调用） ====================

    def create_task(self, data: dict, overwrite: bool = False) -> bool:
        """创建任务，或把已有的当前任务**替换**成一份新身份。

        统一新建入口：无论有没有旧任务，结果都是新 id、进度 0 的新记录；旧任务
        留在历史与账本里（未完成时终结为"被新任务覆盖"，已终结时保留原原因）。
        旧 ``overwrite=True`` 的 UPDATE 旧 id 语义已退役，见
        ``M1-MIGRATION-REGISTER.md``。

        ``overwrite=True`` 只是**兼容旧调用形状**：用 ``data['id']`` /
        ``expected_revision`` / ``business_date`` 组装一份"已确认"的预检，
        再走 :meth:`create_new_task` 的同一份事务。缺 id 的旧覆盖请求不再可执行
        ——未完成的旧任务会得到 ``create_requires_confirmation``。
        """
        zone_name = str(data.get('zone_name') or '').strip()
        preview = data.get('preview') or None
        confirmed = bool(data.get('confirm_replace'))
        if preview is None and overwrite and data.get('id') is not None:
            preview = {
                'task_id': int(data['id']),
                'revision': data.get('expected_revision'),
                'business_day': data.get('business_date'),
                'existing_is_unfinished': data.get('existing_is_unfinished'),
            }
            confirmed = True
        command = {
            'zone_name': zone_name,
            'category': data.get('category', 1),
            'total_days': data.get('total_days', 1),
            'deadline_raw': data.get('deadline_raw', ''),
            'token': data.get('operation_token'),
            'confirmed': confirmed,
            'preview': preview,
            'uid': data.get('uid'),
        }
        if command['preview'] is not None:
            command['preview'] = {
                'task_id': command['preview'].get('task_id'),
                'revision': command['preview'].get('revision'),
                'business_day': command['preview'].get('business_day'),
                'existing_is_unfinished': command['preview'].get(
                    'existing_is_unfinished'),
            }
        try:
            outcome = self.create_new_task(command)
        except TaskMutationRejected:
            raise
        if outcome.get('status') == 'not_committed':
            return False
        return outcome.get('task_id') is not None

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
            if cursor.rowcount == 0:
                return False
            after = dict(row)
            after.update(filtered)
            if not self._row_changed(row, after, filtered.keys()):
                # 无变化更新：不刷历史、不推进人工代数（派生值重算与版本也随之不动）。
                return True
            # 只有真的改了进度事实才推进人工代数：改名、改截止日一类的内容改动
            # 不该让"本任务相关的旧确认"失效。
            progressed = self._progress_changed(row, after)
            if progressed:
                self.db.bump_progress_guard(conn, row['id'])
            self._record_history(conn, row['id'],
                                 'manual_progress' if progressed else 'updated',
                                 before=row, after=after,
                                 source=HISTORY_SOURCE_USER,
                                 detail=('人工调整进度' if progressed
                                         else '修改任务参数'))
            return True

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
            if cursor.rowcount == 0:
                return False
            after = dict(row)
            after['zone_name'] = new_zone_name
            # 改名不改进度：不推进人工代数（同一 task_id 仍是那一条记录），
            # 但历史要留下"改名前后的分区快照"。
            self._record_history(conn, row['id'], 'updated', before=row, after=after,
                                 source=HISTORY_SOURCE_USER,
                                 detail=f'改名 {row.get("zone_name")} → {new_zone_name}')
            return True

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
            if cursor.rowcount == 0:
                return False
            # 删除**不删除历史**（历史表与 tasks 无外键、无级联）。删除本身也是一条
            # 事实：留一条记录说明哪个 task_id 在什么时候消失、删除前的参数是什么。
            self._record_history(conn, row['id'], 'deleted', before=row,
                                 source=HISTORY_SOURCE_USER, detail='删除任务')
            return True

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
                # 全覆盖导入会删掉库里的记录：删除本身也要留痕（历史表不随删除
                # 级联消失），并留下删除前的参数快照。
                for old in conn.execute("SELECT * FROM tasks").fetchall():
                    old = dict(old)
                    self._record_history(conn, old['id'], 'deleted', before=old,
                                         source=HISTORY_SOURCE_IMPORT,
                                         detail='全覆盖导入删除')
                conn.execute("DELETE FROM tasks")
            for item in plan['rows']:
                # 导入改动的**前后快照**必须逐条留痕（哪些是真的人工进度改动、
                # 哪些只是原样重存）。先取旧行，再写，再记历史。
                existing = conn.execute(
                    "SELECT * FROM tasks WHERE zone_name = ?",
                    (item['zone_name'],)).fetchone()
                existing = dict(existing) if existing else None
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
                written = conn.execute(
                    "SELECT * FROM tasks WHERE zone_name = ?",
                    (item['zone_name'],)).fetchone()
                written = dict(written) if written else None
                if written is None:
                    continue
                if existing is None:
                    self._record_history(conn, written['id'], 'created',
                                         after=written, source=HISTORY_SOURCE_IMPORT,
                                         detail='导入新建')
                    continue
                after = dict(existing)
                after.update(item)
                changed = self._progress_changed(existing, item)
                touched = self._row_changed(
                    existing, after,
                    self.PROGRESS_FIELDS + ('deadline_raw', 'zone_name'))
                if not touched:
                    # 与库里逐字段一致的导入行：不刷历史（无意义变化不留痕）。
                    continue
                self._record_history(
                    conn, written['id'], 'manual_progress' if changed else 'updated',
                    before=existing, after=after, source=HISTORY_SOURCE_IMPORT,
                    detail=('导入改变了进度事实' if changed else '导入未改变进度事实'))
                if changed:
                    # 导入**真实改变**了完成天数/总配额/完成状态：推进人工代数，
                    # 让这次改动之前的旧自动完成失效；未改变则不推进。
                    self.db.bump_progress_guard(conn, written['id'])
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

