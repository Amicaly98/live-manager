"""真实数据端口：自动选任务 / 单次参数 / 完成账本。

它只回答"这一场播什么"与"把这份已持久 End 记一次"，不做别的：

- 自动选择走已验收的 ``TaskManager.select_for_run``（任务行、DB 版本与人工进度
  代数来自**同一份**读快照）；业务日先经 ``reset_daily_flags`` 幂等准备，且
  **不倒退**任务库已经走到的日期。
- 单次只用效果里已冻结的参数，不伪造 ``task_id``、不写任务账本；效果里没冻结
  显式分区 ID 时，用本实例**已注入的**分区缓存解析器把已冻结的分区名翻成 ID
  （解析不出合法正整数就走既有 terminal 拒绝出口，绝不带空目标开播）。
- 结算直接进 ``ledger.apply_completion``：不补 ``completed_at``、不把当前 guard
  当旧 guard、不把 ``terminal`` 改写成 credited。

**指定任务票据属于阶段 4**：隔离的 submit/加载边界已明确拒绝那条真实入口；
这里对漏进来的指定选择只做不可达的显式兜底，绝不映射成 ``no_tasks``，也不用
``Goal.revision`` 或当前 DB 版本临时补一张票据。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Callable, Optional

from ..session import ledger as session_ledger
from ..session import effects as fx
from ..session.events import LedgerResult, SelectionResult
from ..session.model import MODE_SINGLE, SELECT_SINGLE, SELECT_TASK
from ..task_manager import TaskMutationRejected


class UnsupportedNamedTicket(RuntimeError):
    """真实指定任务入口在 3A 未接线（用户确认票据留阶段 4）。"""


def parse_business_day(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class TaskDataPort:
    """一个实例一份：真实 TaskManager（真实 SQLite）＋真实账本适配器。"""

    def __init__(self, task_manager, store, clock, account,
                 area_lookup: Optional[Callable[[str], Optional[int]]] = None
                 ) -> None:
        self._task_manager = task_manager
        self._store = store
        self._clock = clock
        self._account = account
        self._area_lookup = area_lookup
        #: 证据用计数：本端口实际调用了几次真实选取入口。
        self.select_calls = 0
        self.reset_calls = 0

    # ---------------------------------------------------------------- 选任务

    def select(self, job) -> SelectionResult:
        effect = job.effect
        day = parse_business_day(effect.business_day)
        selection = dict(effect.selection or {})
        if day is None:
            return self._not_found(effect, 'bad_business_day')

        if selection.get('kind') == SELECT_TASK:
            task_id = selection.get('task_id')
            expected_revision = selection.get('expected_revision')
            ticket_day = selection.get('business_date')
            if (not isinstance(task_id, int) or isinstance(task_id, bool)
                    or task_id <= 0):
                return self._not_found(effect, 'named_ticket_missing_task_id',
                                       terminal=True)
            if (not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 0):
                return self._not_found(effect, 'named_ticket_missing_revision',
                                       terminal=True)
            if parse_business_day(ticket_day) is None:
                return self._not_found(effect, 'named_ticket_missing_business_date',
                                       terminal=True)
            if ticket_day != effect.business_day:
                return self._not_found(effect, 'named_ticket_business_date_mismatch',
                                       terminal=True)
            if self._day_has_passed(day):
                return self._not_found(effect, 'named_ticket_business_day_changed',
                                       terminal=True)
            self.reset_calls += 1
            if not self._task_manager.reset_daily_flags(target_date=day):
                return self._not_found(effect, 'daily_reset_failed')
            self.select_calls += 1
            try:
                instruction, guard = self._task_manager.select_for_run(
                    task_id=task_id, business_date=ticket_day,
                    expected_revision=expected_revision)
            except TaskMutationRejected as exc:
                return self._not_found(
                    effect, f'named_ticket_rejected:{exc.code}', terminal=True)
            except Exception as exc:
                return self._not_found(
                    effect, f'select_failed:{type(exc).__name__}')
            if instruction is None:
                return self._not_found(effect, 'named_ticket_task_missing',
                                       terminal=True)
            return SelectionResult(
                operation_id=effect.op_id, found=True,
                task_id=instruction.task_id,
                zone_name=instruction.zone_name,
                area_id=self._area_for(instruction.zone_name),
                target_seconds=float(instruction.duration_seconds),
                business_day=ticket_day,
                settlement_guard=guard,
                source_revision=getattr(instruction, 'source_revision', None))

        if effect.mode == MODE_SINGLE or selection.get('kind') == SELECT_SINGLE:
            zone = str(selection.get('zone_name') or '')
            area = selection.get('area_id')
            if area is None:
                # 真实单次入口（Dashboard 的 FFmpeg、任务列表的"手动模式"）只冻结
                # 分区名。本实例已经注入分区缓存解析器，任务分支也一直在用；这里
                # 之前**没有消费它**，于是 null 一路带到签名开播请求，而平台端口
                # 并不另按名称解析。只解析已经冻结的名称，不从当前账号/行/页面重选。
                area = self._area_for(zone)
            if not (isinstance(area, int) and not isinstance(area, bool) and area > 0):
                # 显式 ID 非法、或名称在本实例缓存里查不到：走既有明确拒绝出口，
                # 不带 null/0 调 Start，也不把配置缺失当网络故障去无限查询/重试。
                return self._not_found(effect, 'single_area_unresolved',
                                       terminal=True)
            return SelectionResult(
                operation_id=effect.op_id, found=True,
                zone_name=zone, area_id=area,
                target_seconds=float(selection.get('target_seconds') or 0),
                business_day=effect.business_day)

        if self._day_has_passed(day):
            # 任务库已经推进到更新的日期：不倒退它，也不拿旧日结果建场。
            return self._not_found(effect, 'business_day_changed')

        self.reset_calls += 1
        if not self._task_manager.reset_daily_flags(target_date=day):
            return self._not_found(effect, 'daily_reset_failed')

        self.select_calls += 1
        try:
            instruction, guard = self._task_manager.select_for_run(
                business_date=day.isoformat())
        except Exception as exc:
            return self._not_found(effect, f'select_failed:{type(exc).__name__}')
        if instruction is None:
            return self._not_found(effect, 'no_tasks')

        return SelectionResult(
            operation_id=effect.op_id, found=True,
            task_id=instruction.task_id,
            zone_name=instruction.zone_name,
            area_id=self._area_for(instruction.zone_name),
            target_seconds=float(instruction.duration_seconds),
            business_day=effect.business_day,
            # 与任务行同一份读快照里冻结的人工进度代数，原样带走。
            settlement_guard=guard)

    def _area_for(self, zone_name: str) -> Optional[int]:
        if self._area_lookup is None:
            return None
        try:
            return self._area_lookup(zone_name)
        except Exception:
            return None

    def _day_has_passed(self, day: date) -> bool:
        current = (self._task_manager.reset_completed_date
                   or self._task_manager.last_reset_date)
        return current is not None and current > day

    @staticmethod
    def _not_found(effect, reason: str, *, terminal: bool = False) -> SelectionResult:
        return SelectionResult(operation_id=effect.op_id, found=False,
                               reason=reason, business_day=effect.business_day,
                               terminal=terminal)

    # ------------------------------------------------------ Start 之前的任务预留

    def reserve_start(self, effect) -> tuple[bool, str]:
        """Start worker 在网络调用前以真实 DB 行/版本/guard 预留任务。"""
        if effect.task_id is None:
            return True, ''
        return self._task_manager.reserve_task_for_run(
            task_id=effect.task_id, run_id=effect.run_id,
            zone_name=effect.zone_name, business_date=effect.business_day,
            expected_revision=effect.expected_revision,
            settlement_guard=effect.settlement_guard)

    def release_reservation(self, run_id: str) -> None:
        self._task_manager.clear_active_task(run_id)

    # ---------------------------------------------------------------- 结算

    def settle(self, job) -> LedgerResult:
        effect = job.effect
        outcome = session_ledger.apply_completion(
            effect, self._store, self._task_manager,
            account=self._account, today=self.today())
        result = outcome.result
        detail = outcome.detail or {}
        if detail.get('days_done') is not None or detail.get('all_done') is not None:
            # 账本在**同一次事务回执**里给出的累计进度：只随这一条已确认事实
            # 交给通知，不参与任何决策，也不在投递时回读当前任务库。
            result = replace(result,
                             days_done=detail.get('days_done'),
                             all_done=detail.get('all_done'))
        return result

    def today(self) -> date:
        """业务日取**同一个时钟**的墙钟日期（不是进程的 ``date.today()``）。"""
        return self._clock.wall().date()
