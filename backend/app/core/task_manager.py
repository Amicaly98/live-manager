"""
task_manager.py - 任务管理核心（SQLite 版）

从 Excel 迁移至 SQLite 本地存储。
保持所有公式计算逻辑不变，Excel 仅作导入/导出。
"""

import json
import time
import re
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

from app.models.schemas import LiveInstruction, Task
from app.core.db import TaskDB

# 常量
REQUIRED_COLUMNS = [
    '优先度', '分区名', '类别', '需要完成天数', '已完成天数',
    '截止时间', '额外要求', '今日是否完成', '距离完成天数'
]


class TaskManager:
    """任务管理核心类（SQLite 存储，Excel 导入/导出）"""

    def __init__(self, db_path: str = "live_tasks.db", excel_path: str = "live_tasks.xlsx"):
        self.db = TaskDB(db_path)
        self.excel_path = Path(excel_path).resolve()  # 保留用于导入导出
        self.tasks: List[Task] = []
        self.last_reset_date: Optional[date] = None
        self._reset_lock = threading.Lock()
        self._scheduler_running = True
        self._is_resetting = False
        self._last_run_date_file = Path("task_manager_last_run.json")
        self._last_run_date: Optional[date] = None

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
        """从 SQLite 加载任务列表到内存"""
        try:
            rows = self.db.get_all_tasks()
            self.tasks = []
            for row in rows:
                task = Task(
                    priority=row.get('a_val', 9999),
                    zone_name=row['zone_name'],
                    category=row.get('category', 1),
                    total_days=row.get('total_days', 1),
                    days_done=row.get('days_done', 0),
                    deadline_formula=row.get('deadline_raw', ''),
                    today_done=row.get('today_done'),
                    remaining_days=row.get('remaining_days', 1),
                )
                self.tasks.append(task)
            logger.info(f" 成功从数据库加载 {len(self.tasks)} 个任务")
            return True
        except Exception as e:
            logger.error(f" 加载任务失败：{e}", exc_info=True)
            return False

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

    def _compute_and_save_static(self) -> bool:
        """计算 A/I/J 列静态值，写入数据库"""
        try:
            today = date.today()
            today_serial = (today - date(1899, 12, 30)).days

            computed = []
            logger.info(f"开始计算静态值，共 {len(self.tasks)} 个任务，today={today}")

            for t in self.tasks:
                cat = t.category
                actual_days = t.actual_days()

                # 已完成任务：确保 days_done = total_days
                if cat == 0 and t.days_done != t.total_days:
                    t.days_done = t.total_days
                    self.db.update_task(t.zone_name, {'days_done': t.total_days})

                # I 列：距离完成天数（category=0 表示已完成）
                if cat == 0:
                    i_val = 0
                else:
                    i_val = actual_days - t.days_done

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
                })

            # 按 A 列排序
            computed.sort(key=lambda x: x['a'])

            # 写入数据库
            self.db.update_static_values(computed)

            # 重新加载内存中的任务（顺序已更新）
            self.load_tasks()

            logger.info(f" 静态值计算完成，已按优先度排序（{len(computed)} 行）")
            return True
        except Exception as e:
            logger.error(f" 静态计算失败：{e}", exc_info=True)
            return False

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

    def mark_task_done(self, zone_name: str) -> bool:
        logger.info(f"开始标记任务完成：{zone_name}")
        task = None
        for t in self.tasks:
            if t.zone_name == zone_name and t.needs_execution():
                task = t
                break
        if not task:
            logger.error(f"未找到待执行任务：{zone_name}")
            return False
        logger.info(f"找到任务：{task!r}")
        task.days_done += 1
        task.today_done = 1
        # 如果已完成天数达到需要天数，标记为全部完成
        updates = {
            'days_done': task.days_done,
            'today_done': 1,
        }
        if task.days_done >= task.actual_days():
            task.category = 0
            updates['category'] = 0
            logger.info(f"任务 '{zone_name}' 已全部完成")
        else:
            logger.info(f"任务 '{zone_name}' 已标记今日完成")
        success = self.db.update_task(zone_name, updates)
        if success:
            self._save_last_run_date()
            return True
        else:
            logger.error(f"数据库更新失败：{zone_name}")
            return False

    def mark_task_all_done(self, zone_name: str) -> bool:
        """将任务标记为全部完成（category=0，已完成天数=total_days）"""
        logger.info(f" 标记任务全部完成：{zone_name}")
        task = None
        for t in self.tasks:
            if t.zone_name == zone_name:
                task = t
                break
        if not task:
            logger.error(f" 未找到任务：{zone_name}")
            return False
        task.category = 0
        task.days_done = task.total_days
        task.today_done = 1
        logger.info(f" 更新数据库：category=0, 已完成天数={task.days_done}")
        success = self.db.update_task(zone_name, {
            'category': 0,
            'days_done': task.days_done,
            'today_done': 1,
        })
        if success:
            self._save_last_run_date()  # 标记完成时同步更新日期
        return success

    # ==================== 每日重置 ====================

    def reset_daily_flags(self):
        with self._reset_lock:
            today = date.today()
            if self.last_reset_date == today:
                return
            self._is_resetting = True
            logger.info(f" 开始每日重置 | 日期：{today}")            # 重置前先捕获统计和 Top5（用于发送前一日简报）
            pre_reset_stats = None
            pre_reset_top5 = None
            try:
                pre_reset_stats = self.get_stats()
                pre_reset_top5 = sorted(
                    [t for t in self.tasks if t.category > 0],
                    key=lambda t: t.priority
                )[:5]
                pre_reset_top5 = [
                    {
                        'zone_name': t.zone_name,
                        'priority': t.priority,
                        'days_done': t.days_done,
                        'actual_days': t.actual_days(),
                        'remaining_days': t.remaining_days,
                    }
                    for t in pre_reset_top5
                ]
            except Exception:
                pass
            try:
                logger.info("=" * 60)
                logger.info(f" 执行每日重置 | 日期：{today}")
                logger.info("=" * 60)
                logger.info("【步骤 1】重新加载数据...")
                self.load_tasks()
                logger.info("【步骤 2】检查负优先度任务...")
                self._check_and_complete_negative_priority_tasks()
                logger.info("【步骤 3】清空'今日是否完成'标志...")
                self._clear_today_done_column()
                logger.info("【步骤 4】计算静态值并按优先度排序...")
                self._compute_and_save_static()
                self.last_reset_date = today
                logger.info(f" 每日重置完成 | 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info("=" * 60)
                # 发送每日简报邮件（使用重置前捕获的数据）
                self._send_daily_summary_email(pre_reset_stats or self.get_stats(), pre_reset_top5)
            finally:
                self._is_resetting = False

    def _send_daily_summary_email(self, stats=None, top5=None):
        """每日重置后发送前一日简报邮件（异步，不影响主流程）"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            if stats is None:
                stats = self.get_stats()
            if top5 is None:
                top5 = sorted(
                    [t for t in self.tasks if t.category > 0],
                    key=lambda t: t.priority
                )[:5]
                top5 = [
                    {
                        'zone_name': t.zone_name,
                        'priority': t.priority,
                        'days_done': t.days_done,
                        'actual_days': t.actual_days(),
                        'remaining_days': t.remaining_days,
                    }
                    for t in top5
                ]
            sender.send_daily_summary(stats, top5)
        except Exception as e:
            logger.debug(f" 每日简报邮件发送异常：{e}")

    # ==================== 调度器 ====================

    def _start_reset_scheduler(self):
        def reset_worker():
            logger.info(" 重置调度器已启动")
            while self._scheduler_running:
                now = datetime.now()
                next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if now >= next_reset:
                    next_reset += timedelta(days=1)
                sleep_seconds = (next_reset - now).total_seconds()
                time.sleep(max(1, sleep_seconds))
                if not self._scheduler_running:
                    break
                current = datetime.now()
                if current.hour == 0 and current.minute == 0 and self.last_reset_date != current.date():
                    self.reset_daily_flags()
        scheduler_thread = threading.Thread(target=reset_worker, daemon=True)
        scheduler_thread.start()
        logger.info(" 重置调度器线程已启动")

    def shutdown(self):
        self._scheduler_running = False
        self._save_last_run_date()
        logger.info(" 任务管理器已关闭")

    def is_resetting(self) -> bool:
        return self._is_resetting

    def wait_for_reset_complete(self, timeout: float = 120.0) -> bool:
        if not self._is_resetting:
            return True
        logger.info(" 等待每日重置完成...")
        start_time = time.time()
        while self._is_resetting:
            if time.time() - start_time > timeout:
                logger.warning(f"️ 等待重置超时（{timeout}秒）")
                return False
            time.sleep(1)
        logger.info(" 每日重置已完成")
        return True

    # ==================== 面向 API 的方法 ====================

    def get_tasks_as_list(self) -> List[dict]:
        """获取任务列表（序列化）"""
        return [
            {
                "priority": t.priority,
                "zone_name": t.zone_name,
                "category": t.category,
                "total_days": t.total_days,
                "actual_days": t.actual_days(),
                "days_done": t.actual_days() if t.category == 0 else t.days_done,
                "deadline_raw": t.deadline_formula,
                "today_done": t.today_done,
                "remaining_days": t.remaining_days,
                "needs_execution": t.needs_execution(),
                "is_completed": t.category == 0,
            }
            for t in self.tasks
        ]

    def get_tasks_detail(self) -> List[dict]:
        """获取任务详情（含数据库 ID 和计算列）"""
        rows = self.db.get_all_tasks()
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
                "remaining_days": t.remaining_days,
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
        """将数据库行转为 Task 模型"""
        return Task(
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
        """获取统计信息"""
        total = len(self.tasks)
        completed = sum(1 for t in self.tasks if t.category == 0)
        today_done = sum(1 for t in self.tasks if t.today_done == 1)
        today_pending = sum(1 for t in self.tasks if t.needs_execution())

        # 剩余时间 = Σ(I × category)：每天所需小时数 × 剩余天数
        today = date.today()
        remaining_time = 0
        i_vals_positive = []
        j_vals = []
        active_count = 0
        for t in self.tasks:
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

    # ==================== CRUD 方法（供 API 调用） ====================

    def create_task(self, data: dict, overwrite: bool = False) -> bool:
        """创建新任务，overwrite=True 时覆盖同名任务"""
        try:
            zone_name = data.get('zone_name', '')
            existing = self.db.get_task_by_zone(zone_name)
            if existing and overwrite:
                # 覆盖同名任务：更新现有记录
                updates = {
                    'category': data.get('category', 1),
                    'total_days': data.get('total_days', 1),
                    'days_done': data.get('days_done', 0),
                    'deadline_raw': data.get('deadline_raw', ''),
                    'today_done': data.get('today_done'),
                    'remaining_days': data.get('remaining_days', 1),
                }
                self.db.update_task(zone_name, updates)
                logger.info(f" 覆盖同名任务：{zone_name}")
            else:
                self.db.insert_task(data)
            self.load_tasks()
            self._compute_and_save_static()
            return True
        except Exception as e:
            logger.error(f" 创建任务失败：{e}")
            return False

    def update_task_fields(self, zone_name: str, data: dict) -> bool:
        """更新任务字段"""
        try:
            updates = dict(data)
            if 'deadline_formula' in updates:
                updates['deadline_raw'] = updates.pop('deadline_formula')
            success = self.db.update_task(zone_name, updates)
            if success:
                self.load_tasks()
                self._compute_and_save_static()
            return success
        except Exception as e:
            logger.error(f" 更新任务失败：{e}")
            return False

    def delete_task_by_zone(self, zone_name: str) -> bool:
        """删除任务"""
        try:
            success = self.db.delete_task(zone_name)
            if success:
                self.load_tasks()
                self._compute_and_save_static()
            return success
        except Exception as e:
            logger.error(f" 删除任务失败：{e}")
            return False

    # ==================== 导入/导出 ====================

    def import_from_excel(self, file_path: str, skip_zones: List[str] = None) -> dict:
        """从 Excel 文件导入任务，返回 {imported, updated, skipped, errors, message}"""
        result = self.db.import_from_excel(file_path, skip_zones)
        if result['imported'] + result['updated'] > 0:
            self.load_tasks()
            self._compute_and_save_static()
            self.load_tasks()
        # 构造友好消息
        parts = []
        if result['imported'] > 0:
            parts.append(f"新增 {result['imported']} 个")
        if result['updated'] > 0:
            parts.append(f"更新 {result['updated']} 个")
        if result['skipped'] > 0:
            parts.append(f"跳过 {result['skipped']} 个")
        result['message'] = f"导入成功：{'，'.join(parts)}" if parts else "导入完成（无变更）"
        return result

    def export_to_excel(self, file_path: str = None) -> str:
        """导出任务到 Excel 文件（含统计行）"""
        path = file_path or str(self.excel_path)
        stats = self.get_stats()
        self.db.export_to_excel(path, stats)
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
            logger.info(f"  {i:2d}. {task.zone_name:12s} | 进度:{task.days_done:2d}/{task.remaining_days + task.days_done:2d}")
        logger.info("=" * 70)
