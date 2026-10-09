"""阶段 3A 事件执行器：唯一调用 ``Core.ingest`` 的线程 ＋ 有界工作者。

职责固定在三个地方，不再多一个：

- **actor**：唯一调用 ``Core.ingest``／运行期保存／``take_due_events`` 的线程；
  收到输入或预约到点就推进一轮，然后按最近预约等待。
- **固定工作者**：按**冻结封套**做一轮平台调用或数据调用；不读 Goal/Run、
  不决定重试、不写业务 JSON，结果按原 ``op_id`` 送回 actor。
- **媒体端口**：独立线程，只做 Sync/Reclaim 调和（3A 是替身，真实实现属于 3B）。

结构约束（见同目录 ``PHASE3A-DESIGN.md``）：

- 一份 Core、一个 Condition、一个短事件队列、固定工作者；
- ``submit`` 的成功是**核心完成保存的回执**，不是"入队了"；
- 待发操作槽只有 1 个；超时只来自 Core，端口内没有重试循环；
- ``close`` 幂等、有预算、所有 join 都在核心/队列/状态锁之外；
- 资源关闭**不是**用户停止：不写 stopped、不发平台 Stop。

本包只被 ``backend/tests/`` 引用，不接任何生产入口。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..session import effects as fx
from ..session.events import (
    OUTCOME_UNKNOWN,
    OP_QUERY,
    OP_SELECT,
    OP_SETTLE,
    OP_START,
    OP_STOP,
    STATUS_UNKNOWN,
    LedgerResult,
    PlatformResult,
    PusherReport,
    Rejected,
    SelectionResult,
    StartReservationResult,
    TasksChanged,
    UserConfirmation,
    UserGoal,
)
from ..session.model import WANT_RUNNING, WANT_STOPPED

#: "短事件队列"的上界。满了**明确拒绝**，不静默丢弃。
DEFAULT_INBOX_LIMIT = 256

#: 待发操作槽的上界：Core 同一时刻只有一项在途 Operation。
PENDING_LIMIT = 1

#: 默认关闭预算（秒）。它只是"有预算地收资源"的工程预算，不是磁盘/平台 SLA。
DEFAULT_CLOSE_BUDGET = 5.0

#: 工作者等待新工作的周期。它不是业务节奏：真实节奏由 Core 的预约决定。
_IDLE_POLL_SECONDS = 0.02

#: 连续没有取得进展的外层轮数上限。它不是节流阀，而是"零时间自激"的报警器。
_MAX_IDLE_SPINS = 20_000

# 已过期指定票据的兼容错误文案；有效票据由 Core/真实任务端口处理。
UNSUPPORTED_NAMED_TICKET = '指定任务确认票据无效或已过期'

_PLATFORM_EFFECTS = (fx.StartPlatform, fx.StopPlatform, fx.QueryPlatform)
_DATA_EFFECTS = (fx.SelectTask, fx.SettleTask)
_MEDIA_EFFECTS = (fx.SyncPusher, fx.ReclaimPusher)

logger = logging.getLogger(__name__)


class UnexpectedEffect(RuntimeError):
    """执行器不认识的效果：记录并如实报告，不静默丢弃。"""


class Reply:
    """一次 ``submit`` 的回执。

    ``result()`` 返回的是**核心处理完这一条输入后的 Result**；未完成即代表核心
    还没有处理它 —— **排队成功不是受理**。它不暴露回调接口：``set_result``
    只置值并唤醒等待者，因此 actor 内不会执行调用方的任意阻塞代码。
    """

    __slots__ = ('_event', '_value')

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None

    def set_result(self, value: Any) -> None:
        if self._event.is_set():
            return
        self._value = value
        self._event.set()

    def done(self) -> bool:
        return self._event.is_set()

    def result(self, timeout: Optional[float] = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError('核心尚未处理该输入')
        return self._value


@dataclass(frozen=True)
class Job:
    """一次操作执行的**封套**：只装不可变事实，不落盘、不是第二份 Operation。"""

    effect: Any
    deadline_mono: float
    credentials: Any = None


@dataclass(frozen=True)
class CloseReport:
    """一次资源关闭的**实际**结果。未收回来的工作者如实为假。"""

    converted: bool = False
    actor_stopped: bool = False
    worker_stopped: bool = False
    media_stopped: bool = False
    pump_stopped: bool = True
    dropped_jobs: int = 0
    skipped_jobs: int = 0
    pending_jobs: int = 0
    inbox_size: int = 0
    actor_error: str = ''
    adapter_errors: Tuple[str, ...] = ()
    #: 媒体端口是否真的执行了资源关闭（3A 替身没有 close 时为 False）。
    media_close_performed: bool = False
    #: 端口自报的真实资源结果：自有子进程/管道/日志是否真的收干净。
    media_resources_closed: bool = True
    media_readers_stopped: bool = True
    media_alive_after: int = 0
    media_close_detail: Tuple[str, ...] = ()
    #: 这次资源关闭是否**得到过核验**。只有"没有 close 钩子的旧替身"才允许为真而
    #: 未实际核验；有钩子却抛错/没返回报告时为假 —— 结果明确不干净。
    media_close_verified: bool = True

    def clean(self) -> bool:
        return (self.actor_stopped and self.worker_stopped and self.media_stopped
                and self.pump_stopped and self.pending_jobs == 0
                and self.media_resources_closed and self.media_readers_stopped
                and self.media_close_verified)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'converted': self.converted,
            'actor_stopped': self.actor_stopped,
            'worker_stopped': self.worker_stopped,
            'media_stopped': self.media_stopped,
            'pump_stopped': self.pump_stopped,
            'dropped_jobs': self.dropped_jobs,
            'skipped_jobs': self.skipped_jobs,
            'pending_jobs': self.pending_jobs,
            'inbox_size': self.inbox_size,
            'actor_error': self.actor_error,
            'adapter_errors': list(self.adapter_errors),
            'media_close_performed': self.media_close_performed,
            'media_resources_closed': self.media_resources_closed,
            'media_readers_stopped': self.media_readers_stopped,
            'media_alive_after': self.media_alive_after,
            'media_close_detail': list(self.media_close_detail),
            'media_close_verified': self.media_close_verified,
        }


class LatestView:
    """只保留最新一份副本的发布槽。写者（actor）**永不等待**读者。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._value: Optional[Dict[str, Any]] = None
        self._seq = 0

    def publish(self, value: Dict[str, Any]) -> None:
        # ``Snapshot.to_dict()`` 每次都构造全新字典，因此这里存下的已经与 Core
        # 的对象脱钩；读者再拿一份深拷贝，改副本不可能改到 Core。
        with self._cond:
            self._value = value
            self._seq += 1
            self._cond.notify_all()

    def read(self) -> Optional[Dict[str, Any]]:
        with self._cond:
            return None if self._value is None else copy.deepcopy(self._value)

    def stamp(self) -> int:
        with self._cond:
            return self._seq

    def wait(self, since: int, timeout: float) -> bool:
        with self._cond:
            if self._seq != since:
                return True
            self._cond.wait(timeout=timeout)
            return self._seq != since


def selection_kind(selection: Any) -> str:
    """从 UserGoal 的各种选择形态里取出 kind（None/字典/Selection 都支持）。"""
    if selection is None:
        return ''
    if isinstance(selection, dict):
        return str(selection.get('kind') or '')
    return str(getattr(selection, 'kind', '') or '')


def _op_id(effect: Any) -> Optional[str]:
    return getattr(effect, 'op_id', None)


class Runtime:
    """事件执行器。构造后 ``start()``，用完 ``close()``。"""

    def __init__(self, core, clock, *,
                 platform_port, task_port, media_port,
                 credentials: Optional[Callable[[], Any]] = None,
                 account: Any = None,
                 close_budget_seconds: float = DEFAULT_CLOSE_BUDGET,
                 inbox_limit: int = DEFAULT_INBOX_LIMIT,
                 facts_sink: Optional[Callable[[Any], None]] = None,
                 name: str = 'p3a') -> None:
        self._core = core
        self._clock = clock
        self._platform_port = platform_port
        self._task_port = task_port
        self._media_port = media_port
        self._credentials = credentials
        #: **已确认事实**的唯一出口（窄通知适配器）。它只入队，不做网络；未注入时
        #: （旧夹具/隔离）事实被忽略，行为与不产生事实时一致。
        self._facts_sink = facts_sink
        # 账号上下文在 Core 构造时就固定，之后不再变化；调用方可以显式传**同一个**
        # 对象，否则只读一次核心自己那份事实。这里不构造第二份身份。
        self._account = account if account is not None \
            else getattr(core, '_account', None)
        self._close_budget = float(close_budget_seconds)
        self._inbox_limit = int(inbox_limit)
        self._name = name

        self._condition = threading.Condition()
        self._inbox: Deque[Tuple[Any, Optional[Reply]]] = deque()
        self._pending: Optional[Job] = None
        self._media_queue: Deque[Any] = deque()
        self._inflight = 0
        self._media_inflight = 0
        self._dropped = 0
        self._skipped = 0
        self._media_merged = 0
        self._unmatched = 0
        self._closing = False
        self._dispatch_closed = False
        self._started = False
        self._actor_error = ''
        self._fault_reason = ''
        self._adapter_errors: List[str] = []
        self._unsupported_reason = ''

        self._view = LatestView()
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._observer_errors: List[str] = []

        self._shutdown = threading.Event()
        self._close_gate = threading.Lock()
        self._close_report: Optional[CloseReport] = None

        self._actor: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._media_worker: Optional[threading.Thread] = None
        self._pump: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- 装载边界

    def check_load_boundary(self) -> str:
        """Store 已校验被持久化的指定任务原票据；有效快照可正常恢复。"""
        if getattr(self._core, '_account_mismatch', False):
            self._unsupported_reason = (
                '运行存档账号与当前账号不匹配，已拒绝启动')
        return self._unsupported_reason

    @property
    def unsupported_reason(self) -> str:
        return self._unsupported_reason

    @property
    def started(self) -> bool:
        return self._started

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> 'Runtime':
        """起线程。加载边界拒绝时**不起任何线程**，原存档保持原样。"""
        with self._condition:
            if self._started:
                return self
            self.check_load_boundary()
            if self._unsupported_reason:
                return self
            self._started = True
        self._actor = self._spawn(self._actor_loop, 'actor')
        self._worker = self._spawn(self._worker_loop, 'platform-worker')
        self._media_worker = self._spawn(self._media_loop, 'media-worker')
        return self

    def _spawn(self, target, role: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=f'{self._name}-{role}',
                                  daemon=True)
        thread.start()
        return thread

    # ---------------------------------------------------------------- 输入

    def submit(self, event: Any) -> Reply:
        """把一条输入交给唯一决策点。返回的回执由 actor 在处理完后置值。"""
        reply = Reply()
        with self._condition:
            if self._unsupported_reason:
                reply.set_result(Rejected(self._unsupported_reason))
                return reply
            if self._fault_reason:
                # 事件线程已经逃逸退出：入口**当场**拒绝，不再往无人处理的队列里放。
                reply.set_result(Rejected(self._fault_reason))
                return reply
            if self._closing:
                reply.set_result(Rejected('执行器正在关闭，未受理'))
                return reply
            if len(self._inbox) >= self._inbox_limit:
                reply.set_result(Rejected('执行器输入队列已满'))
                return reply
            self._inbox.append((event, reply))
            self._condition.notify_all()
        return reply

    def tasks_changed(self, business_day: str = '', reason: str = 'changed') -> Reply:
        """任务库/业务日变化后由隔离外壳投递一次唤醒（不直接开播）。"""
        return self.submit(TasksChanged(business_day=business_day, reason=reason))

    # ---------------------------------------------------------------- 观察

    def snapshot_copy(self) -> Optional[Dict[str, Any]]:
        """外部读者拿到的**副本**；改它不会改到 Core。"""
        return self._view.read()

    def wait_for_snapshot(self, predicate, timeout: Optional[float] = None
                          ) -> Optional[Dict[str, Any]]:
        """等待 actor 发布满足条件的只读状态；使用版本通知，不做业务轮询。"""
        deadline = (None if timeout is None
                    else time.monotonic() + max(0.0, float(timeout)))
        while True:
            stamp = self._view.stamp()
            payload = self._view.read()
            if payload is not None and predicate(payload):
                return payload
            remaining = (None if deadline is None
                         else deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                return None
            if not self._view.wait(stamp, remaining):
                return None

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """订阅最新副本。回调在独立的 pump 线程执行，慢/抛错都不影响 actor。"""
        with self._condition:
            self._subscribers.append(callback)
            if self._pump is None and self._started:
                self._pump = self._spawn(self._pump_loop, 'pump')

    @property
    def observer_errors(self) -> List[str]:
        return list(self._observer_errors)

    # ---------------------------------------------------------------- actor

    def _pop_input(self) -> Optional[Tuple[Any, Optional[Reply]]]:
        with self._condition:
            if self._inbox:
                return self._inbox.popleft()
        return None

    def _idle_for_close(self) -> bool:
        with self._condition:
            return (not self._inbox and self._inflight == 0
                    and self._pending is None and self._media_inflight == 0
                    and not self._media_queue)

    def _actor_loop(self) -> None:
        spins = 0
        current: Optional[Reply] = None
        try:
            while True:
                for event in self._core.take_due_events(self._clock.mono()):
                    self._apply(event, None)
                item = self._pop_input()
                if item is not None:
                    spins = 0
                    current = item[1]
                    self._apply(*item)
                    current = None
                    continue
                if self._closing and self._idle_for_close():
                    break
                due = self._core.next_due_mono()
                wait_for: Optional[float] = None
                if due is not None:
                    wait_for = max(0.0, float(due) - float(self._clock.mono()))
                if self._closing:
                    # 关闭中：短周期醒来检查是否已经收干净。用**有界等待**而不是
                    # 立即 continue —— 后者会变成零睡眠忙循环。
                    wait_for = _IDLE_POLL_SECONDS if wait_for is None \
                        else min(wait_for, _IDLE_POLL_SECONDS)
                if wait_for is None or wait_for > 0.0:
                    spins = 0
                elif not self._closing:
                    spins += 1
                    if spins > _MAX_IDLE_SPINS:
                        raise RuntimeError('执行器未收敛：疑似预约零时间自激')
                with self._condition:
                    if self._inbox:
                        continue
                    self._condition.wait(timeout=wait_for)
        except BaseException as exc:            # 不静默：close 会如实报告
            self._actor_error = f'{type(exc).__name__}: {exc}'
            self._fail_replies(current)
        finally:
            with self._condition:
                self._condition.notify_all()

    def _fail_replies(self, current: Optional[Reply]) -> None:
        """事件线程逃逸异常：在**同一个受理边界**收口。

        当前这一条与队列里还没轮到的回执都拿到明确拒绝，此后 ``submit`` 立即拒绝；
        故障原因原样保留。已经落盘的目标**一个字节都不改**（不写 stopped、不重放、
        不重启决策线程、不补任何房间动作），已经完成的回执也不被覆盖 ——
        ``Reply.set_result`` 是幂等的。
        """
        reason = f'执行器事件线程已故障（{self._actor_error}），该输入未被受理'
        with self._condition:
            self._fault_reason = reason
            self._dispatch_closed = True
            queued = list(self._inbox)
            self._inbox.clear()
            self._condition.notify_all()
        if current is not None:
            current.set_result(Rejected(reason))
        for _event, reply in queued:
            if reply is not None:
                reply.set_result(Rejected(reason))

    def _apply(self, event: Any, reply: Optional[Reply]) -> None:
        prior_run = self._core.snapshot.run
        prior_run_id = prior_run.id if prior_run is not None else None
        result, effects = self._core.ingest(event)
        current = self._core.snapshot
        current_run = current.run
        if (prior_run_id is not None
                and (current_run is None or current_run.id != prior_run_id
                     or current.step == 'idle')):
            release = getattr(self._task_port, 'release_reservation', None)
            if release is not None:
                release(prior_run_id)
        if reply is not None:
            reply.set_result(result)
        self._view.publish(self._core.snapshot.to_dict())
        # 已确认事实：本轮处理完之后、在 actor 边界之外交给**只入队**的窄出口。
        # 它不读 Goal/Run、不做网络、不阻塞决策线程。
        self._emit_facts(effects)
        if self._closing or self._dispatch_closed:
            return                    # 关闭中：不再派发新的外部效果
        jobs, media = self._freeze(effects)
        for job in jobs:
            self._dispatch(job)
        for effect in media:
            self._dispatch_media(effect)

    def _emit_facts(self, effects: List[Any]) -> None:
        """把本轮产生的**已确认事实**原样交给窄出口（只入队，不做网络）。

        出口抛错只记入边界错误，绝不影响 actor 推进：通知失败不是业务失败。
        """
        sink = self._facts_sink
        if sink is None:
            return
        for effect in effects:
            if not isinstance(effect, fx.ConfirmedFact):
                continue
            try:
                sink(effect)
            except Exception as exc:                  # noqa: BLE001
                if len(self._adapter_errors) < 64:
                    self._adapter_errors.append(
                        f'facts: {type(exc).__name__}: {exc}')

    def _freeze(self, effects: List[Any]) -> Tuple[List[Job], List[Any]]:
        """把效果封成套封：冻结效果、Operation 的原截止与当刻凭据快照。"""
        operation = self._core.operation
        jobs: List[Job] = []
        media: List[Any] = []
        for effect in effects:
            if isinstance(effect, fx.ConfirmedFact):
                continue              # 已确认事实由 _emit_facts 单独交付
            if isinstance(effect, _MEDIA_EFFECTS):
                media.append(effect)
                continue
            if isinstance(effect, fx.PublishState):
                continue              # 3A 不发 PublishState；观察走 LatestView
            if isinstance(effect, _PLATFORM_EFFECTS + _DATA_EFFECTS):
                if operation is None or _op_id(effect) != operation.id:
                    # 没有对应的在途操作：不派发，也不把旧请求改绑成新请求。
                    self._unmatched += 1
                    continue
                jobs.append(Job(effect=effect,
                                deadline_mono=float(operation.deadline_mono),
                                credentials=self._freeze_credentials()))
                continue
            self._adapter_errors.append(
                f'UnexpectedEffect: {type(effect).__name__}')
        return jobs, media

    def _freeze_credentials(self) -> Any:
        if self._credentials is None:
            return None
        try:
            value = self._credentials()
        except Exception as exc:
            self._adapter_errors.append(f'credentials: {type(exc).__name__}: {exc}')
            return None
        return copy.deepcopy(value) if isinstance(value, (dict, list)) else value

    # ---------------------------------------------------------------- 派发

    def _dispatch(self, job: Job) -> None:
        with self._condition:
            if self._closing or self._dispatch_closed:
                self._dropped += 1
                return
            if self._pending is not None:
                # 待发槽只有 1 个。新效果出现意味着旧 Operation 已被结果或超时
                # 释放，因此被替换的这一项必然已经过期。
                self._dropped += 1
            self._pending = job
            self._condition.notify_all()

    def _dispatch_media(self, effect: Any) -> None:
        with self._condition:
            if self._closing or self._dispatch_closed:
                self._dropped += 1
                return
            if self._media_queue and _same_media_fact(self._media_queue[-1], effect):
                # 有界合并：同一路、同一类型的最新期望胜出。绝不跨类型覆盖
                # （Sync 与 Reclaim 是相反意图），因此不会把停止意图改写成重推。
                self._media_queue[-1] = effect
                self._media_merged += 1
            else:
                self._media_queue.append(effect)
            self._condition.notify_all()

    def _claim(self, job: Job) -> bool:
        """派发去重：只核对"尚未发出的工作仍是 Core 那项操作且未过期"。

        它**不是**开播前的关房许可，也不看"Goal 还是不是 running"：当前操作遇到
        更新的停止目标时，它的结果仍要照常记录。
        """
        operation = self._core.operation
        if operation is None or operation.id != _op_id(job.effect):
            return False
        return float(self._clock.mono()) <= float(job.deadline_mono)

    def post_result(self, event: Any) -> None:
        """把一条**工作者结果**送回 actor。端口投递迟到结果也走同一扇门。"""
        with self._condition:
            self._inbox.append((event, None))
            self._condition.notify_all()

    # ---------------------------------------------------------------- 工作者

    def _take_pending(self, timeout: float) -> Optional[Job]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._pending is not None:
                    job, self._pending = self._pending, None
                    return job
                if self._shutdown.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def _worker_loop(self) -> None:
        while True:
            job = self._take_pending(_IDLE_POLL_SECONDS)
            if job is None:
                if self._shutdown.is_set():
                    return
                continue
            if not self._claim(job):
                # 待发槽由 ``_take_pending`` 取出、由 close 清空；这里**不能**
                # 顺手把它置空 —— 工作者在忙的时候，槽里可能已经放进了一项
                # **新的**合法操作，置空等于把它悄悄丢掉。
                with self._condition:
                    self._skipped += 1
                    self._condition.notify_all()
                continue
            with self._condition:
                self._inflight += 1
                self._condition.notify_all()
            try:
                event = self._run_job(job)
            except Exception as exc:
                self._adapter_errors.append(
                    f'{type(exc).__name__}: {exc} @ {type(job.effect).__name__}')
                event = _failure_event(job.effect, exc)
            if event is not None:
                self.post_result(event)
            with self._condition:
                self._inflight -= 1
                self._condition.notify_all()

    def _run_job(self, job: Job) -> Optional[Any]:
        effect = job.effect
        if isinstance(effect, fx.StartPlatform) and effect.task_id is not None:
            reserver = getattr(self._task_port, 'reserve_start', None)
            if reserver is None:
                return StartReservationResult(
                    operation_id=effect.op_id, run_id=effect.run_id,
                    reason='start_reservation_port_unavailable',
                    named_ticket=effect.expected_revision is not None)
            accepted, reason = reserver(effect)
            if not accepted:
                return StartReservationResult(
                    operation_id=effect.op_id, run_id=effect.run_id,
                    reason=reason,
                    named_ticket=effect.expected_revision is not None)
        if isinstance(effect, _PLATFORM_EFFECTS):
            return self._platform_port.execute(job)
        if isinstance(effect, fx.SelectTask):
            return self._task_port.select(job)
        if isinstance(effect, fx.SettleTask):
            return self._task_port.settle(job)
        raise UnexpectedEffect(f'工作者不认识的效果：{effect!r}')

    def _take_media(self, timeout: float) -> Optional[Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._media_queue:
                    return self._media_queue.popleft()
                if self._shutdown.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def _media_loop(self) -> None:
        while True:
            effect = self._take_media(_IDLE_POLL_SECONDS)
            if effect is None:
                if self._shutdown.is_set():
                    return
                continue
            with self._condition:
                self._media_inflight += 1
            try:
                events = self._media_port.handle(effect) or []
            except Exception as exc:
                # 不能悄悄退出：如实回报"这一路不存活"，由 Core 决定下一步。
                self._adapter_errors.append(
                    f'media: {type(exc).__name__}: {exc}')
                events = [PusherReport(run_id=getattr(effect, 'run_id', '') or '',
                                       alive=False, outputting=False)]
            for event in events:
                self.post_result(event)
            with self._condition:
                self._media_inflight -= 1
                self._condition.notify_all()

    # ---------------------------------------------------------------- 订阅泵

    def _pump_loop(self) -> None:
        seen = self._view.stamp()
        while not self._shutdown.is_set():
            if not self._view.wait(seen, _IDLE_POLL_SECONDS):
                continue
            if self._shutdown.is_set():
                return
            seen = self._view.stamp()
            payload = self._view.read()
            for callback in list(self._subscribers):
                try:
                    callback(payload)
                except Exception as exc:
                    if len(self._observer_errors) < 64:
                        self._observer_errors.append(
                            f'{type(exc).__name__}: {exc}')
        return

    # ---------------------------------------------------------------- 关闭

    def close(self, budget: Optional[float] = None) -> CloseReport:
        """幂等、有预算的资源关闭。**不是**用户停止：不改 Goal、不写盘、不发 Stop。"""
        with self._close_gate:
            if self._close_report is not None:
                return self._close_report
            self._close_report = self._close_once(budget)
            return self._close_report

    @property
    def close_report(self) -> Optional[CloseReport]:
        return self._close_report

    def _close_once(self, budget: Optional[float]) -> CloseReport:
        limit = self._close_budget if budget is None else float(budget)
        deadline = time.monotonic() + max(0.0, limit)
        with self._condition:
            self._closing = True
            self._dispatch_closed = True
            pending = self._pending
            self._pending = None
            if pending is not None:
                self._dropped += 1
            self._condition.notify_all()
        # 先关端口的**创建入口**（幂等、不回收、不 join）：关闭期间不再新起自有进程。
        self._begin_media_close()
        # 全部 join 在核心锁、队列锁与状态锁**之外**。
        _join(self._actor, deadline)
        self._shutdown.set()
        with self._condition:
            self._pending = None
            self._media_queue.clear()
            self._condition.notify_all()
        _join(self._worker, deadline)
        _join(self._media_worker, deadline)
        _join(self._pump, deadline)
        # 再用**同一个绝对截止**接上端口的真实资源关闭：worker 的 join 与端口的回收
        # 共用一份预算，不给端口另发一份。没有 close 的旧替身保持原默认行为；
        # 有钩子却抛错时**不**沿用"看起来干净"的默认值。
        media_report, media_error, media_hook = self._close_media_resources(deadline)
        media_verified = media_hook and media_report is not None
        with self._condition:
            return CloseReport(
                converted=True,
                actor_stopped=not _alive(self._actor),
                worker_stopped=not _alive(self._worker),
                media_stopped=not _alive(self._media_worker),
                pump_stopped=not _alive(self._pump),
                dropped_jobs=self._dropped,
                skipped_jobs=self._skipped,
                pending_jobs=(1 if self._pending is not None else 0),
                inbox_size=len(self._inbox),
                actor_error=self._actor_error,
                adapter_errors=tuple(self._adapter_errors[:64]),
                media_close_performed=media_hook,
                media_resources_closed=(
                    media_report.resources_cleared if media_verified
                    else not media_hook),
                media_readers_stopped=(
                    media_report.readers_stopped if media_verified
                    else not media_hook),
                media_alive_after=(
                    media_report.alive_after if media_verified else 0),
                media_close_detail=(
                    media_report.detail if media_verified
                    else ((media_error,) if media_hook else ())),
                media_close_verified=(not media_hook) or media_verified,
            )

    def _begin_media_close(self) -> None:
        """只关端口的创建入口。端口没有该钩子时保持原行为。"""
        starter = getattr(self._media_port, 'begin_close', None)
        if starter is None:
            return
        try:
            starter()
        except Exception as exc:                      # noqa: BLE001
            self._adapter_errors.append(
                f'media_begin_close: {type(exc).__name__}: {exc}')

    def _close_media_resources(self, deadline: float):
        """取回端口的真实资源关闭结果。返回 ``(报告|None, 错误文本, 是否有真实钩子)``。

        三种情况必须分开：**没有** close 钩子的旧替身（第三个值为假 → 保持兼容默认、
        不声称已核验）；有钩子且返回报告（用报告的真实结果）；有钩子但抛错/没返回
        报告（第三个值为真、报告为 None → Runtime 如实给出"不干净/未核验"的结果）。
        """
        closer = getattr(self._media_port, 'close', None)
        if closer is None:
            return None, '', False         # 3A 替身：没有真实资源可关
        try:
            return closer(deadline), '', True
        except Exception as exc:                      # noqa: BLE001
            return None, f'media_close: {type(exc).__name__}: {exc}', True

    # ---------------------------------------------------------------- 证据辅助

    def stats(self) -> Dict[str, Any]:
        """执行器自身的边界事实（供验收读取，不参与任何决策）。"""
        with self._condition:
            return {
                'started': self._started,
                'closing': self._closing,
                'unsupported_reason': self._unsupported_reason,
                'inbox_size': len(self._inbox),
                'pending_jobs': 1 if self._pending is not None else 0,
                'pending_limit': PENDING_LIMIT,
                'media_queued': len(self._media_queue),
                'inflight': self._inflight,
                'media_inflight': self._media_inflight,
                'dropped_jobs': self._dropped,
                'skipped_jobs': self._skipped,
                'media_merged': self._media_merged,
                'unmatched_effects': self._unmatched,
                'actor_error': self._actor_error,
                'fault_reason': self._fault_reason,
                'adapter_errors': list(self._adapter_errors[:64]),
                'threads': [t.name for t in (self._actor, self._worker,
                                             self._media_worker, self._pump)
                            if t is not None],
            }


def _same_media_fact(older: Any, newer: Any) -> bool:
    """两条媒体效果是不是**同一路同一意图**（可合并队尾）。

    只有类型与 run_id 都相同才合并：Sync 与 Reclaim 是相反意图，绝不互相覆盖。
    """
    if type(older) is not type(newer):
        return False
    if getattr(older, 'run_id', None) != getattr(newer, 'run_id', None):
        return False
    if isinstance(older, fx.ReclaimPusher):
        return getattr(older, 'reason', '') == getattr(newer, 'reason', '')
    return True


def _failure_event(effect: Any, exc: BaseException) -> Optional[Any]:
    """工作者内的适配异常 → 原身份上的失败事实（不能让线程悄悄退出）。"""
    reason = f'adapter_error:{type(exc).__name__}'
    if isinstance(effect, _PLATFORM_EFFECTS):
        kind = {fx.StartPlatform: OP_START, fx.StopPlatform: OP_STOP,
                fx.QueryPlatform: OP_QUERY}.get(type(effect), OP_QUERY)
        return PlatformResult(operation_id=effect.op_id, kind=kind,
                              run_id=getattr(effect, 'run_id', None),
                              room_id=getattr(effect, 'room_id', None),
                              outcome=OUTCOME_UNKNOWN, status=STATUS_UNKNOWN,
                              detail=reason)
    if isinstance(effect, fx.SelectTask):
        return SelectionResult(operation_id=effect.op_id, found=False,
                               reason=reason, business_day=effect.business_day)
    if isinstance(effect, fx.SettleTask):
        return LedgerResult(operation_id=effect.op_id, ok=False, detail=reason)
    return None


def _alive(thread: Optional[threading.Thread]) -> bool:
    return thread is not None and thread.is_alive()


def _join(thread: Optional[threading.Thread], deadline: float) -> None:
    if thread is None:
        return
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
