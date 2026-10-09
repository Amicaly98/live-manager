"""真实完成账本的**窄适配器**：消费已持久 End，提交到真实 SQLite。

它回答的问题只有一个：**把这一份已经在磁盘上的结束事实，幂等地计进真账本**。
它不是第二个决策入口 —— 核心仍然只通过 ``SettleTask`` 效果请求这件事，账本
结果原样回给核心的 ``LedgerResult`` 收口。

三条边界（PHASE2-PLAN §3.3）：

1. **只消费已持久 End。** 结算前严格读取 ``SessionStore``（不持任何核心锁），
   与效果里的 run/task/day 逐项核对，并检查模式、completed、settle、有限正目标、
   结束秒数达标、账号/数据目录归属、业务日非未来。**不**重新计算时长，也**不**
   设"新鲜度 TTL"：这份决定是核心在截止前形成并落盘的事实，晚到仍然归原业务日。
2. **旧日请求本身不是证据。** 一个"业务日 + 秒数达标"的请求不构成完成；
   公开的"标记今日完成"走它自己的入口，不经过这里。
3. **验证在写事务之外，身份与凭据在事务之内。** 本模块先读 JSON、再调用
   ``TaskManager.settle_completion``；后者在自己的写事务里重新核对任务身份与
   人工进度代数。JSON 与 SQLite **不是**跨文件事务，断点靠幂等重试闭合：
   已提交但回执丢失 → 新连接查到既有回执返回 ``already``。

阶段 2 只实现这个适配器与真实数据用例；它**不**接生产入口。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

from .effects import SettleTask
from .events import LedgerResult
from .model import END_COMPLETED, MODE_TASK
from .store import VERDICT_TRUSTED, SessionStore

#: 秒数比较容差（与核心业务时钟同一口径）
_EPS = 1e-6


def canonical_dir(path) -> str:
    """把一份路径归一成可比较的**数据目录**：绝对化 + 规范化大小写分隔符。

    只做路径规范化，不猜"哪个目录才算实例目录"：比较的两边都必须由调用方以
    既有上下文给出（SessionStore 所在的目录、TaskManager.db 所在的目录）。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def same_data_directory(store: SessionStore, task_manager) -> bool:
    """这一份 SessionStore 与这个 TaskManager 是否属于**同一个数据目录**。

    完成账本在**每个实例自己的 SQLite** 里，``task_id`` 只在该库内有意义；两个
    不同数据目录的实例完全可能有相同的 uid / room_id 与同数值的 ``task_id``。
    因此适配器在写库前必须核对"这份 End/store 与这个账本库同属一个实例目录"
    （PHASE2-PLAN §3.3、§3.6；复核 R3）。这是**内部端口错接**的反例，不是跨实例
    网络攻击面，也不需要实例清单或全局任务编号。

    管理器没有可比较的路径上下文（旧调用方/替身）时不做额外拦截：这里只回答
    "两者都在、且不同"这一种情况。
    """
    db_path = getattr(getattr(task_manager, 'db', None), 'db_path', None)
    if not db_path:
        return True
    store_dir = canonical_dir(Path(store.path).parent)
    db_dir = canonical_dir(Path(db_path).parent)
    return store_dir == db_dir


@dataclass
class CompletionFacts:
    """从严格读取的持久快照里取出的**结束事实**（只读，不改任何状态）。"""

    run_id: str
    task_id: int
    business_day: str
    target_seconds: float
    effective_seconds: float
    completed_at: Optional[str]
    settlement_guard: Optional[int]
    zone_name: str
    area_id: Optional[int]


@dataclass
class LedgerOutcome:
    """一次提交的完整结果，供执行层/证据使用。"""

    result: LedgerResult
    facts: Optional[CompletionFacts] = None
    rejection: str = ''
    detail: dict = None

    def __post_init__(self) -> None:
        if self.detail is None:
            self.detail = {}


def _reject(op_id: str, reason: str) -> LedgerOutcome:
    """核对不通过：**不是**账本故障，是"这份请求不构成完成事实"。

    核心据此收尾（不无限重试）；它不会被记成一次计入。
    """
    return LedgerOutcome(
        result=LedgerResult(
            operation_id=op_id, ok=False, terminal=True,
            receipt=f'not-applied:{reason}', reason=reason,
            detail=f'账本端口拒绝：{reason}'),
        rejection=reason)


def read_persisted_end(store: SessionStore, effect: SettleTask,
                       account=None,
                       today: Optional[date] = None) -> Tuple[Optional[CompletionFacts], str]:
    """严格读取**已持久**的结束事实；返回 ``(facts, 拒绝原因)``。"""
    loaded = store.load()
    if loaded.status != VERDICT_TRUSTED or loaded.snapshot is None:
        return None, f'state_{loaded.status}'
    snapshot = loaded.snapshot
    run = snapshot.run
    if run is None:
        return None, 'no_persisted_run'
    end = run.end
    if end is None:
        return None, 'no_persisted_end'
    # 与效果逐项核对：调用方请求的必须是磁盘上那一份，不能"顺手"换成别的。
    if effect.run_id and run.id != effect.run_id:
        return None, 'run_mismatch'
    if not end.settle:
        return None, 'end_not_settled'
    if end.reason != END_COMPLETED:
        return None, 'end_not_completed'
    if run.mode != MODE_TASK:
        return None, 'not_task_mode'
    if run.task_id is None or int(run.task_id) <= 0:
        return None, 'no_task_identity'
    if effect.task_id is not None and int(effect.task_id) != int(run.task_id):
        return None, 'task_mismatch'
    if str(effect.business_day) != str(end.business_day):
        return None, 'business_day_mismatch'
    if not str(end.business_day):
        return None, 'business_day_missing'
    target = float(run.target_seconds or 0.0)
    if not (target > 0) or target != target or target == float('inf'):
        return None, 'invalid_target'
    effective = float(end.effective_seconds or 0.0)
    if effective + _EPS < target:
        return None, 'below_target'
    if int(run.uid) <= 0 or int(run.room_id) <= 0:
        return None, 'account_context_missing'
    if account is not None and (int(run.uid) != int(account.uid)
                               or int(run.room_id) != int(account.room_id)):
        return None, 'account_mismatch'
    try:
        day = date.fromisoformat(str(end.business_day))
    except ValueError:
        return None, 'business_day_invalid'
    current_day = today or date.today()
    if day > current_day:
        return None, 'business_day_future'
    return CompletionFacts(
        run_id=run.id, task_id=int(run.task_id),
        business_day=str(end.business_day), target_seconds=target,
        effective_seconds=effective, completed_at=end.completed_at,
        settlement_guard=run.settlement_guard,
        zone_name=run.zone_name, area_id=run.area_id), ''


def apply_completion(effect: SettleTask, store: SessionStore, task_manager,
                     account=None, today: Optional[date] = None) -> LedgerOutcome:
    """把一份已持久 End 计进真账本。返回完整结果（含拒因与事实）。"""
    facts, reason = read_persisted_end(store, effect, account=account, today=today)
    if facts is None:
        return _reject(effect.op_id, reason)

    # 实例目录归属：这份 End/store 必须与目标账本库同属一个数据目录。两个隔离
    # 实例可以有相同的 uid/room_id 和同数值的 task_id，账号与身份检查都过得去，
    # 但把 A 的完成记进 B 的库仍然是错的（复核 R3）。核对在**写之前**完成。
    if not same_data_directory(store, task_manager):
        return _reject(effect.op_id, 'instance_directory_mismatch')

    outcome = task_manager.settle_completion(
        task_id=facts.task_id,
        business_day=facts.business_day,
        run_id=facts.run_id,
        settlement_guard=facts.settlement_guard,
        effective_seconds=facts.effective_seconds,
        target_seconds=facts.target_seconds,
        zone_name=facts.zone_name,
        area_id=facts.area_id,
        completed_at=facts.completed_at,
        today=today)

    status = outcome.get('status')
    credited = status == task_manager.COMPLETION_CREDITED
    already = status == task_manager.COMPLETION_ALREADY
    terminal = status == task_manager.COMPLETION_NOT_APPLIED
    ok = credited or already
    result = LedgerResult(
        operation_id=effect.op_id, ok=ok, already=already,
        receipt=outcome.get('receipt') or '',
        detail=(outcome.get('reason') or status or ''),
        terminal=terminal,
        reason=outcome.get('reason') or '')
    return LedgerOutcome(result=result, facts=facts,
                         detail={'status': status,
                                 'reason': outcome.get('reason') or '',
                                 'receipt': outcome.get('receipt') or '',
                                 'days_done': outcome.get('days_done'),
                                 'all_done': outcome.get('all_done')})
