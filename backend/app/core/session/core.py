"""唯一推进主干：``Core.ingest(event) -> (Result, effects)``。

决定模型（不是被退役原型的 84 个方法搬家）：

- **Goal** 是唯一的用户目标；**Run** 是本场的固定身份与计时；**Step** 只是流程位置；
  **Operation** 是内存里唯一那一项在途外部操作；**EndDecision** 放在 Run 内。
- 事件入口只做四件事：保存最新目标 / 记录本轮事实 / 标记需要推进 / 记录预约到点，
  然后调用同一个 :meth:`Core._advance`。
- 平台 Start/Stop/Query、选任务、结算的决定**只**从 :meth:`Core._advance` 发出；
  回调不各自开下一场。
- 外部动作全部作为效果返回，由执行层在**状态锁之外**完成；核心内没有网络、
  没有 sleep、没有进程 join、没有 SMTP。

所有新场次先关再开（``close_query → close_stop → close_check → close_done → choose → open``）；
同日原场恢复直接续播（``resume_query → live``），不额外下播一次。
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import effects as fx
from .events import (
    OP_MARK_AREA,
    OP_QUERY,
    OP_SELECT,
    OP_SETTLE,
    OP_START,
    OP_STOP,
    OUTCOME_AREA_UNAVAILABLE,
    OUTCOME_FACE_VERIFY,
    OUTCOME_OK,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    STATUS_CLOSED,
    STATUS_LIVE,
    STATUS_ROUND,
    STATUS_UNKNOWN,
    WAKE_MIDNIGHT,
    WAKE_TARGET,
    WAKE_WATCH,
    Accepted,
    Bootstrap,
    Event,
    Ignored,
    LedgerResult,
    PlatformResult,
    PusherInventory,
    PusherReport,
    Recorded,
    Rejected,
    Result,
    SelectionResult,
    StartReservationResult,
    TaskAreaMarkResult,
    TasksChanged,
    Timeout,
    UserConfirmation,
    UserGoal,
    Wake,
)
from .model import (
    ACTIVE_STEPS,
    CLOSING_STEPS,
    END_COMPLETED,
    END_MIDNIGHT,
    END_REPLACED,
    MODE_SINGLE,
    MODE_TASK,
    PUSH_FFMPEG,
    SELECT_AUTO,
    SELECT_SINGLE,
    SELECT_TASK,
    START_NEW,
    START_RESUME,
    STEP_CHOOSE,
    STEP_CLOSE_CHECK,
    STEP_CLOSE_DONE,
    STEP_CLOSE_QUERY,
    STEP_CLOSE_STOP,
    STEP_IDLE,
    STEP_LIVE,
    STEP_OPEN,
    STEP_RESUME_QUERY,
    STEP_WAIT_TASKS,
    STOP_ACCOUNT_EXIT,
    STOP_AREA_UNAVAILABLE,
    STOP_FACE_VERIFY,
    STOP_PLATFORM_REJECTED,
    STOP_SELECTION_REJECTED,
    STOP_TARGET_REACHED,
    STOP_USER,
    WANT_RUNNING,
    WANT_STOPPED,
    EndDecision,
    Goal,
    Run,
    Selection,
    Snapshot,
    business_day_of,
    day_deadline,
)
from .scheduler import Scheduler
from .store import VERDICT_TRUSTED, VERDICT_UNTRUSTED, SessionStore

logger = logging.getLogger(__name__)

#: 任务端口回答"今天没有合格任务"时用的原因码；其它原因码按**暂时失败**重试。
SELECT_REASON_NO_TASKS = 'no_tasks'

#: 一次 advance 至多消化的纯本地步骤数。它不是节流阀，而是自激的报警器：
#: 正常路径最多 4 步就发出外部工作或停下。
_MAX_LOCAL_STEPS = 16

#: 浮点比较容差（秒）
_EPS = 1e-6

#: 保存重试的预约 key
_KEY_SAVE_RETRY = 'save_retry'

#: 媒体回收重试的预约 key 前缀（不占操作槽）
_KEY_MEDIA_RECLAIM = 'media_reclaim'

#: 媒体期望调和（SyncPusher）的预约 key 前缀。它是一次性声明，不是房间动作。
_KEY_MEDIA_SYNC = 'media_sync'

#: 自有推流**持续失败**时的技术性防空转（秒，PLAN §1.6）。
#: 它不是平台被反复掐断的退避（那是 30 秒节奏），也不是 SLA 配置：只保证同一
#: 瞬间至多一次 Sync，重复失败合并进同一项预约（复核 S2）。
_MEDIA_RETRY_SECONDS = 1.0

#: 午夜优先窗口（PLAN §1.4）：预计到期落在午夜前后 30 秒内可等到 00:00:30。
MIDNIGHT_WINDOW_SECONDS = 30.0

#: 平台反复关房才退避：连续第几次关闭之后，重开按既有的重试节奏等待。
_CLOSE_BACKOFF_AFTER = 2

#: ``_emit_fact(task_id=...)`` 的"未指定"哨兵：与显式的 ``None``（确实没有
#: 任务身份，例如单次模式）区分开。
_UNSET = object()

#: 平台拒绝开播/恢复时的人脸验证页（旧版 ``live_controller`` 同款口径）：
#: 平台回包没有给出可用地址时，用它把 ``fact.reason`` 变成邮件/面板可点击的
#: 链接，而不是把平台文案当"验证地址"传给用户。
_FACE_VERIFY_URL_TEMPLATE = (
    'https://www.bilibili.com/blackboard/live/face-auth-middle.html'
    '?source_event=400&mid={uid}')


def face_verify_reason(detail: Any, uid: Any) -> str:
    """人脸事实的 reason：回包带合法 http(s) 地址就用它；否则构造 B 站验证页地址。

    地址判定与 ``session_runtime.projection._is_http_url`` 同口径（scheme +
    hostname，且不带 userinfo）；``urlsplit`` 解析失败按"不是地址"处理。
    ``mid`` 用当前账号 uid。
    """
    text = str(detail or '').strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None
    if (parsed is not None and parsed.scheme in ('http', 'https')
            and parsed.hostname and not parsed.username
            and not parsed.password):
        return text
    return _FACE_VERIFY_URL_TEMPLATE.format(uid=uid or '')


class RequiredSaveFailed(RuntimeError):
    """必需的状态提交失败：停止本轮推进，不派发依赖它的下一步动作。"""


@dataclass(frozen=True)
class AccountContext:
    """当前登录上下文。任务不能切换账号或房间，房间只能来自这里。

    ``display_name`` 只是**已核验账号**的显示名（通知署名用）：未登录/未核验时
    为空串，绝不伪造。它不参与任何决策，也不进入平台请求。
    """

    uid: int
    room_id: int
    zone_name: str = ''
    area_id: Optional[int] = None
    display_name: str = ''


@dataclass(frozen=True)
class Tuning:
    """少量时间常数。不是为每种失败各留一个开关。"""

    #: 一轮平台操作（含选任务、结算）的**唯一**预算
    op_budget_seconds: float = 30.0
    #: 未知/暂时失败后下一轮同一步骤的间隔
    retry_delay_seconds: float = 30.0
    #: 在播期间的低频状态观察间隔
    watch_interval_seconds: float = 30.0
    #: 下播受理后到复核之间的间隔（0 = 立即核对，不等固定 30 秒监控）
    close_check_delay_seconds: float = 0.0
    #: 自有推流"回收未确认"时的重试次数上限（C09 的"有预算"）。到顶只保留归属事实，
    #: 不再空转；媒体端口的重试不是第二个房间决策入口。
    reclaim_attempts: int = 3


@dataclass
class Operation:
    """内存里唯一那一项在途操作。重启后丢弃，不重放旧 RPC。"""

    id: str
    kind: str
    step: str                 # 发出时的步骤：结果归属以此为准
    run_id: Optional[str]
    room_id: Optional[int]
    goal_revision: int        # 发出时的控制版本；**不是**结果丢弃门
    request: Dict[str, Any]
    deadline_mono: float


class Core:
    """独立场次核心。``ingest`` 之外没有第二个决策入口。"""

    def __init__(self, store: SessionStore, clock, account: AccountContext,
                 tuning: Optional[Tuning] = None, *,
                 push_source_provider: Optional[Callable[[], str]] = None
                 ) -> None:
        self._store = store
        self._clock = clock
        self._account = account
        self._tuning = tuning or Tuning()
        #: 执行边界注入的"当前有效推流来源"读取口（真实装配接设置页，隔离夹具可省）。
        #: Core **不**导入 API、不读 settings.json：设置怎么解释由边界那一处决定，
        #: 这里只消费结论。缺省 None = 沿用每份选择自己冻结的来源（纯 Core 行为不变）。
        self._push_source_provider = push_source_provider
        self._lock = threading.RLock()
        self._scheduler = Scheduler()
        self._operation: Optional[Operation] = None
        self._seq = 0
        self._dirty = False
        self._watch_due = False
        #: "关房核对 → 选任务"这一轮需要重走：任务库可能变了，或用户改了运行目标
        self._recheck_due = False
        #: 本进程自己那一路上推流的归属（不是 PID）。仅 ffmpeg 会占用它。
        self._pusher_run_id: Optional[str] = None
        #: 执行端口在启动时报告的**本实例自有**存活对象（run id 集合）。可信归属
        #: 来自执行边界，不等同于"还保留着同一个业务 Run"（复核 S4）：昨天半场
        #: 照常丢弃，物理对象的收尾能力不随之消失。它是内存事实，每次启动由端口
        #: 重新提交，因此不持久化、不落 PID、不按进程名批量杀。
        self._own_inventory: set = set()
        #: 自有推流是否已被媒体端口证实还活着（False = 尚未确认，不是"已回收"）
        self._pusher_alive = False
        #: 本轮**已确认转换**的窄事实（通知出口）。本轮必需保存成功之后才追加到
        #: 效果末端；保存失败整批丢弃 —— 绝不发布一条没有落盘的事实。
        self._facts: List[Any] = []
        #: 有什么时候该向媒体端口**声明**一次期望（一次性事实，不是持续状态）
        self._media_sync_due = False
        #: 上一次声明出去的 SyncPusher 是否还在等回执：用来把"自家进程突然退出"
        #: 与"我们刚发的 Sync 又失败"分开（前者立即重试，后者技术性防空转），
        #: 从而不会让同一瞬间的失败回执自我循环（复核 S2）。
        self._media_sync_outstanding = False
        #: 本轮"回收未确认"的尝试次数（有预算，防忙循环）
        self._reclaim_tries = 0
        #: 本轮结束决定已经试过结算且失败：不再挡住房收尾（关妥后重试同一笔）。
        self._settle_deferred_run: Optional[str] = None
        #: 平台反复关房退避：连续关闭次数与"连续稳定在播"的起点（内存，重启即清）
        self._close_streak = 0
        self._stable_mono: Optional[float] = None
        self._loaded_status = VERDICT_TRUSTED
        self._snapshot = self._load()
        loaded_run = self._snapshot.run
        self._account_mismatch = bool(
            loaded_run is not None
            and (loaded_run.uid != self._account.uid
                 or loaded_run.room_id != self._account.room_id))
        if self._account_mismatch:
            self._snapshot.health['account_mismatch'] = True
        #: 本次 Bootstrap 若恢复的是与仍在生效 Goal 相同、且此前已经确认在播的
        #: Run，保留这份短暂来源事实。落盘的 STEP_LIVE 是既有确认事实；Run.id 与
        #: Goal.revision 的对应关系避免把关场/切换期间的新 Goal 错当旧场恢复。它
        #: 不进入模型或 JSON，进程下一次启动仍只从同一份存档重新判断。
        self._restored_live_run_id = (
            loaded_run.id
            if (loaded_run is not None
                and self._snapshot.step == STEP_LIVE
                and self._snapshot.goal.wanted == WANT_RUNNING
                and self._snapshot.goal.revision == loaded_run.selected_by_revision
                and loaded_run.end is None)
            else None)
        #: 档案不可信：保留原字节，不受理新开播、不覆盖存档（C04）。
        self._writes_blocked = self._loaded_status == VERDICT_UNTRUSTED
        self._normalize_loaded()

    # ---------------------------------------------------------------- 装载

    def _load(self) -> Snapshot:
        result = self._store.load()
        self._loaded_status = result.status
        snapshot = result.snapshot
        snapshot.health = dict(snapshot.health or {})
        snapshot.health['load'] = result.status
        if result.status == VERDICT_UNTRUSTED:
            snapshot.health['corrupt'] = True
        if result.error:
            snapshot.health['load_error'] = result.error
        return snapshot

    def _normalize_loaded(self) -> None:
        """重载后的安全归一：另一进程的单调锚点在本进程没有意义。

        - **单调锚点一律丢弃**（含"启动时是 0"的合法锚点），业务秒数退回最后可信
          检查点，不按旧进程的坐标继续外推；
        - **保留墙钟区间锚点**：有可信原区间、同日、且随后查询证实仍在目标分区时，
          用它补接离线区间（:meth:`_confirm_live`），不接受倒退或跨日的墙钟；
        - 在播/开播中的场次退回"重新核对"；**已经发出下播的步骤也退回查询** ——
          重启不重放旧的 Stop RPC，只重新核对本次房间再接着收尾（1B B07）；
        - 关房各子步、等待与空闲保持原步；
        - **装载接缝上处置过期的旧场**：继承来的场次如果在它的业务日截止之后才被
          装载，就没有任何可追溯到截止之前的可信原区间，那份墙钟区间一并作废
          （PLAN §1.4「截止前未形成可信决定，服务在截止后才恢复则保守丢弃旧场」）。
          已有可信结束决定的场次不在此列：它按原业务日幂等结算（A11）。
        """
        snapshot = self._snapshot
        if snapshot.run is None:
            return
        clock = snapshot.run.clock
        clock.projecting = False
        clock.interval_start_mono = None          # 旧进程的单调坐标作废
        clock.display_anchor_mono = None
        if (snapshot.run.end is None
                and self._clock.wall() > day_deadline(snapshot.run.business_day)):
            # 截止之后才装载的旧场：丢掉那份"可信原区间"。正常到期于是只按统一
            # 业务时钟、业务日、既有 End 与午夜窗口判定，不会被事后墙钟追认。
            clock.interval_start_wall = None
        if snapshot.step in (STEP_LIVE, STEP_OPEN):
            snapshot.step = STEP_RESUME_QUERY
        elif snapshot.step in (STEP_CLOSE_QUERY, STEP_CLOSE_STOP, STEP_CLOSE_CHECK):
            snapshot.step = STEP_CLOSE_QUERY
        self._dirty = self._loaded_status == VERDICT_TRUSTED and snapshot.run is not None

    # ---------------------------------------------------------------- 只读

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def operation(self) -> Optional[Operation]:
        return self._operation

    def next_due_mono(self) -> Optional[float]:
        with self._lock:
            return self._scheduler.next_due()

    def take_due_events(self, now_mono: Optional[float] = None) -> List[Event]:
        """取出到点的预约（消费掉）。执行层把它们当事件送回 :meth:`ingest`。"""
        with self._lock:
            return self._scheduler.collect_due(self._now() if now_mono is None else now_mono)

    @property
    def _interval_reconstructed(self) -> bool:
        """当前**旧业务日**场次是不是没有可追溯到业务日截止之前的可信区间。

        判据只有两个既有事实：本场的业务日，以及时钟上那份**可信原区间**
        ``clock.interval_start_wall``。它既不是"本场曾重启"，也不再读会被每次
        确认刷新的那个锚点 —— 那正是复核 T1 指出的误判来源：``confirm_live``
        每 30 秒就把锚点推到当下，"00:00:05 的一次正常在播确认"与"服务在
        00:00:05 才恢复"在那份锚点上不可区分，于是从未重启的正常场次也被否决。

        装载接缝（:meth:`_normalize_loaded`）已经把"截止后才恢复的旧场"处置掉：
        它拿不到可信原区间，这里于是为真 —— 旧日完成被拒，随后由午夜规则保守
        丢弃（PLAN §1.4）。同业务日的场次恒为假：它的区间起点就在今天，本来就
        不需要追溯到昨天。
        """
        run = self._snapshot.run
        if run is None or run.business_day == self._business_day():
            return False
        return _parse_wall(run.clock.interval_start_wall) is None

    # ---------------------------------------------------------------- 入口

    def ingest(self, event: Event) -> Tuple[Result, List[Any]]:
        """唯一公开入口。返回 (回执, 要执行的效果)。"""
        effects: List[Any] = []
        with self._lock:
            if self._account_mismatch:
                # 新凭据不能用旧 Run.room_id 发查询/停止；保持原存档字节不变，
                # 由所属旧账号的移交流程收尾，或要求明确恢复旧账号上下文。
                return Rejected('运行存档账号与当前账号不匹配，已拒绝启动'), effects
            if self._writes_blocked:
                # 档案不可信：保留原字节，不受理新开播、不覆盖存档、不推进
                # （PLAN §1.10 / C04）。恢复可信输入的维护流程不在本轮实现。
                if isinstance(event, UserGoal):
                    return Rejected('状态档案不可信，已拒绝受理控制目标'), effects
                return Ignored('state_untrusted'), effects
            try:
                reply = self._dispatch(event, effects)
                self._advance(effects)
                self._flush()
                # 已确认事实**最后**追加：本轮必需保存已成功，且不改变
                # `_advance` / clock / ledger 的既有顺序。
                if self._facts:
                    effects.extend(self._facts)
                    self._facts = []
            except RequiredSaveFailed as failure:
                self._facts = []
                self._arm_save_retry()
                return Rejected(f'状态未能保存：{failure}'), effects
        return reply, effects

    # ---------------------------------------------------------------- 输入

    def _dispatch(self, event: Event, effects: List[Any]) -> Result:
        if isinstance(event, UserGoal):
            return self._on_user_goal(event)
        if isinstance(event, UserConfirmation):
            return self._on_user_confirmation(event)
        if isinstance(event, PlatformResult):
            return self._on_platform_result(event)
        if isinstance(event, SelectionResult):
            return self._on_selection_result(event)
        if isinstance(event, StartReservationResult):
            return self._on_start_reservation_result(event)
        if isinstance(event, TaskAreaMarkResult):
            return self._on_task_area_mark_result(event)
        if isinstance(event, LedgerResult):
            return self._on_ledger_result(event)
        if isinstance(event, PusherInventory):
            return self._on_pusher_inventory(event)
        if isinstance(event, PusherReport):
            return self._on_pusher_report(event)
        if isinstance(event, Timeout):
            return self._on_timeout(event)
        if isinstance(event, Wake):
            return self._on_wake(event)
        if isinstance(event, TasksChanged):
            self._recheck_due = True
            return Recorded()
        if isinstance(event, Bootstrap):
            # 启动/重载后重新进入唯一推进点。等待中的运行目标要重新取一次今天的任务；
            # 停止/人脸/拒绝等待不会被这条唤醒（它们本来就不在 wait_tasks）。
            s = self._snapshot
            if s.goal.wanted == WANT_RUNNING and s.step == STEP_WAIT_TASKS:
                self._recheck_due = True
            return Recorded()
        return Rejected('unsupported_event')

    def _on_user_goal(self, event: UserGoal) -> Result:
        """保存最新目标。**不**清在途操作，不抢断当前这一轮。"""
        resume_error = self._validate_resume_intent(event)
        if resume_error:
            # 恢复请求的原场身份已变化：明确拒绝，不改 JSON、不发新开播效果。
            return Rejected(resume_error)
        if any(value is not None for value in (
                event.abandon_run_id, event.abandon_goal_revision,
                event.abandon_selection)):
            error = self._validate_abandon_intent(
                event, stopped_only=(event.wanted == WANT_STOPPED))
            if error:
                return Rejected(error)
            if event.wanted == WANT_STOPPED:
                return self._abandon_stopped_run()
            if event.wanted != WANT_RUNNING:
                return Rejected('受控放弃只能用于停止或重新开播')

        goal, error = self._goal_from_event(event)
        if goal is None:
            return Rejected(error)
        if self._goal_equal(self._snapshot.goal, goal):
            # 完全相同的重复输入：幂等 —— 不推进版本，也不额外唤醒。
            return Accepted()
        candidate = self._snapshot.clone()
        candidate.goal = goal
        candidate.goal.revision = self._snapshot.goal.revision + 1
        if (event.start_kind == START_RESUME
                and candidate.run is not None
                and candidate.run.end is None
                and candidate.run.push_source != goal.selection.push_source):
            # 推流方式只由设置页决定（ControlService 在受理时已把选择换成当前
            # 设置值）：恢复必须让**本场**也切到该来源，不能只改 Goal 而让 Run
            # 继续旧来源。与 Goal 同一次保存落盘（原子）；不改目标/进度/身份。
            candidate.run.push_source = goal.selection.push_source
        if not self._store.save(candidate):
            # 先保存再答复：失败绝不假称受理，也不接受一个没落盘的意图。
            self._arm_save_retry()
            return Rejected('未能保存操作，请重试')
        self._snapshot = candidate
        self._dirty = False
        if goal.wanted == WANT_RUNNING:
            # 真实改变的**运行**目标要能在当前操作边界被真正执行：等待中的场次重新
            # 走一遍关房核对与选择，而不是干等一个与它无关的 TasksChanged（复核 R4a）。
            # 这里只登记"该重走"，仍然由 _advance 决定下一项效果。
            # 停止目标不登记 —— 等待不因任务变化或目标改写被重新开播。
            self._recheck_due = True
        return Accepted()

    def _validate_resume_intent(self, event: UserGoal) -> str:
        """恢复请求夹带的原场身份在 **actor 终点**复核（保存新 Goal 之前）。

        控制服务在入口做过一次快检，但两次调用之间 actor 可能已经接受更新的用户
        目标（例如刚发出的停止）：只有在形成/保存新 Goal 之前按**同一当前快照**
        再核一次原 Run.id 与 Goal revision，旧恢复才不会晚到覆盖较新的意图。

        只有在调用方**显式携带** ``expected_run_id``/``expected_goal_revision`` 时
        才生效：内部可信调用（如夹具直接 ``user_resume``，不带这两项）与普通最新
        意图的 Start/Stop 都不受影响，不建立"全局版本相等"门去锁死日常控制。
        不比较自然增长的 elapsed，不重抽目标，也不新增 Run/字段/时钟。
        """
        if event.start_kind != START_RESUME:
            return ''
        expected_run = event.expected_run_id
        expected_revision = event.expected_goal_revision
        if expected_run is None and expected_revision is None:
            return ''
        run = self._snapshot.run
        if expected_run is None or run is None or str(run.id) != str(expected_run):
            return '待恢复的原场已变化，请刷新后重试'
        current_revision = self._snapshot.goal.revision
        try:
            stale = (expected_revision is None
                     or int(expected_revision) != int(current_revision))
        except (TypeError, ValueError):
            stale = True
        if stale:
            return '待恢复的原场已变化，请刷新后重试'
        return ''

    def _validate_abandon_intent(self, event: UserGoal, *,
                                 stopped_only: bool) -> str:
        """校验单条 actor 输入携带的旧 Run/Goal/选择快照，不比自然 elapsed。"""
        run = self._snapshot.run
        selection = self._snapshot.goal.selection
        if (run is None or event.abandon_run_id != run.id
                or event.abandon_goal_revision != self._snapshot.goal.revision):
            return '待放弃的运行状态已变化，请刷新后重试'
        if event.abandon_selection is None:
            return '放弃请求缺少原选择快照'
        if not isinstance(event.abandon_selection, (Selection, dict)):
            return '放弃请求的原选择快照无效'
        supplied = (event.abandon_selection
                    if isinstance(event.abandon_selection, Selection)
                    else Selection.from_dict(event.abandon_selection))
        if supplied.to_dict() != selection.to_dict():
            return '待放弃的原选择已变化，请刷新后重试'
        if run.end is not None:
            return '场次已经形成结束决定，必须先完成原收尾'
        if not self._run_belongs_to_account(run):
            return '待放弃的场次不属于当前账号'
        if stopped_only and (self._snapshot.goal.wanted != WANT_STOPPED
                             or self._snapshot.step != STEP_IDLE
                             or not run.retains_checkpoint(self._business_day())):
            return '只有同日停止并保留的场次可以单独放弃'
        return ''

    def _abandon_stopped_run(self) -> Result:
        """经 Core/Store 清除同日停止 Run；不由控制外壳直接改业务 JSON。"""
        candidate = self._snapshot.clone()
        selection = candidate.goal.selection
        candidate.run = None
        candidate.goal.selection = Selection(kind=SELECT_AUTO,
                                             push_source=selection.push_source)
        candidate.goal.start_kind = START_NEW
        candidate.goal.stop_reason = STOP_USER
        candidate.goal.revision += 1
        if not self._store.save(candidate):
            self._arm_save_retry()
            return Rejected('未能保存放弃操作，请重试')
        self._snapshot = candidate
        self._dirty = False
        return Accepted()

    def _on_user_confirmation(self, event: UserConfirmation) -> Result:
        """人工"继续当前停止原因"：当前有效才**转换**成同一个 UserGoal 入口。

        冻结的上下文（发出确认那一刻的 Goal 版本与账号房间）必须仍与当前状态一致：
        后来的用户停止会推进 revision，账号变更会改变 uid/room，两者都使这份旧确认
        失效（复核 C1）。旧平台结果走的是 ``PlatformResult`` 那条路，不是这里。
        """
        goal = self._snapshot.goal
        if (event.goal_revision != goal.revision
                or event.uid != self._account.uid
                or event.room_id != self._account.room_id):
            return Ignored('stale_confirmation')
        if goal.wanted != WANT_STOPPED:
            # 没有等待人工确认的停止：不把已运行的场次改写成"新开"。
            return Ignored('nothing_to_confirm')
        selection = goal.selection
        if (goal.stop_reason == STOP_FACE_VERIFY
                and selection.kind == SELECT_TASK
                and selection.business_date != self._business_day()):
            # 同日人脸前尚未确认开播时仍按原票据重试；跨日后原票据已经过期，
            # 人工确认只继续这份运行意图，任务由今天唯一的真实自动选取入口决定。
            selection = Selection(kind=SELECT_AUTO,
                                  push_source=selection.push_source)
        return self._on_user_goal(UserGoal(
            wanted=WANT_RUNNING, mode=goal.mode, selection=selection,
            start_kind=START_NEW))

    def _on_platform_result(self, event: PlatformResult) -> Result:
        op = self._operation
        if op is None or event.operation_id != op.id:
            return Ignored('old_operation')
        if not self._same_subject(op, event):
            return Ignored('wrong_subject')
        self._release_operation(op)
        self._record_platform(op, event)
        return Recorded()

    def _on_selection_result(self, event: SelectionResult) -> Result:
        op = self._operation
        if op is None or op.kind != OP_SELECT or event.operation_id != op.id:
            return Ignored('old_operation')
        self._release_operation(op)
        self._record_selection(op, event)
        return Recorded()

    def _on_start_reservation_result(self, event: StartReservationResult) -> Result:
        op = self._operation
        if (op is None or op.kind != OP_START or event.operation_id != op.id
                or event.run_id != op.run_id):
            return Ignored('old_start_reservation')
        self._release_operation(op)
        run = self._snapshot.run
        if run is None or run.id != event.run_id:
            return Recorded()

        s = self._snapshot
        if s.goal.revision == op.goal_revision and event.named_ticket:
            self._set_goal(WANT_STOPPED, STOP_SELECTION_REJECTED)
            self._park_stopped_run(run)
            self._move_to(STEP_IDLE)
            return Recorded()
        if s.goal.revision == op.goal_revision:
            if (s.goal.start_kind == START_RESUME
                    or run.id == self._restored_live_run_id):
                # 原场的身份/资格/guard 失效时只拒绝这次恢复，保留原 Run 与检查点；
                # 不把它伪装成一次新的自动选取。
                self._set_goal(WANT_STOPPED, STOP_SELECTION_REJECTED)
                self._park_stopped_run(run)
                self._move_to(STEP_IDLE)
                return Recorded()
            # 未确认的自动选择在预留失败后按当前排序重取；不保留失败选择。
            self._discard_run()
            self._move_to(STEP_CHOOSE)
            return Recorded()

        # Start 尚未实际调用，较新的目标胜出；恢复目标仍核对原场，其他目标按
        # 唯一 Core 流程丢弃未确认 Run 并重新关房/选取。
        if s.goal.wanted == WANT_RUNNING and s.goal.start_kind == START_RESUME:
            self._move_to(STEP_IDLE)
            return Recorded()
        self._discard_run()
        self._move_to(STEP_IDLE)
        return Recorded()

    def _on_ledger_result(self, event: LedgerResult) -> Result:
        op = self._operation
        if op is None or op.kind != OP_SETTLE or event.operation_id != op.id:
            return Ignored('old_operation')
        self._release_operation(op)
        self._record_ledger(op, event)
        return Recorded()

    def _on_pusher_inventory(self, event: PusherInventory) -> Result:
        """执行端口报告**本实例自有**的存活对象清单（窄的可信库存输入）。

        这是边界事实，不是第二条业务权威：内容只用于"本实例还有哪几路推流需要
        调和/回收"。提交发生在启动（或端口重新对齐）时，因此与昨天的业务 Run
        是否存在无关（复核 S4）。空清单表示本实例没有自有对象 —— 此时用户自己
        的 OBS / 别的实例的对象都不会被追。
        """
        self._own_inventory = {str(item) for item in (event.owned or ())}
        return Recorded()

    def _on_pusher_report(self, event: PusherReport) -> Result:
        """媒体端口的**窄事实**：只更新自有归属与一次调和的登记。

        它不决定房间动作：媒体输出从不参与业务计时，也不在这里开关房。
        归属只认**执行边界报过的自有对象**（见 :meth:`_adopt_inventory`）：重启后
        内存 owner 为空时由启动库存重建，任意旧 Run / 用户 OBS / 别的进程一律不认。

        退出与"存活但无输出"都**立即**驱动同一条媒体调和（复核 R6/S1）：下一次
        尝试由 :meth:`_emit_media_expectation` 在唯一推进主干里声明；本方法只登记
        它该在什么时候发生 —— 自家进程突然退出是**即时**尝试，而**刚发出去的**
        Sync 又失败则按 `_MEDIA_RETRY_SECONDS` 技术性防空转合并，避免同一瞬间反复
        Query+Sync（复核 S2）。
        """
        from_sync = self._media_sync_outstanding
        self._media_sync_outstanding = False
        if self._pusher_run_id is None:
            if event.run_id not in self._own_inventory:
                return Ignored('old_media')
            self._pusher_run_id = event.run_id
            self._reclaim_tries = 0
        elif event.run_id != self._pusher_run_id:
            return Ignored('old_media')
        self._pusher_alive = bool(event.alive)
        if not event.alive:
            # 这一路自有对象已经真的不在了：忘掉归属，并从库存里划掉。
            self._pusher_run_id = None
            self._own_inventory.discard(event.run_id)
            self._scheduler.cancel(self._media_sync_key(event.run_id))
            self._reclaim_tries = 0
        if (not event.alive or not event.outputting) and self._pusher_wanted():
            # 只有本场仍要求一路自有推流时才调和：停止 / OBS / 旧归属不会到这里。
            self._note_media_trouble(event.run_id, from_sync=from_sync)
        return Recorded()

    def _note_media_trouble(self, run_id: str, *, from_sync: bool) -> None:
        """合并登记一次媒体调和（重复失败收敛到同一项，不每次加一轮）。

        - 自家进程突然退出 / 断推（**不是**我们这次尝试的结果）：立即声明一次，
          并合并唤醒一次单飞平台查询（PLAN §1.6 的"断推/输出停滞额外唤醒"）；
        - 刚发出去的 Sync 又失败：按 :data:`_MEDIA_RETRY_SECONDS` 技术性防空转
          再试，不重复唤醒平台查询，也不在同一瞬间零时间重发。
        """
        if not from_sync:
            self._watch_due = True
        key = self._media_sync_key(run_id)
        if self._media_sync_due or self._scheduler.pending(key):
            return                     # 已经有待发尝试：合并
        if from_sync:
            self._scheduler.arm(key, self._now() + _MEDIA_RETRY_SECONDS,
                                _KEY_MEDIA_SYNC, run_id)
        else:
            self._media_sync_due = True

    def _on_timeout(self, event: Timeout) -> Result:
        """本轮等待结束。**不等于**平台撤销了请求。"""
        op = self._operation
        if op is None or op.id != event.operation_id:
            return Ignored('old_timeout')
        if op.kind == OP_SELECT:
            return self._on_selection_result(SelectionResult(
                operation_id=op.id, found=False, reason='timeout'))
        if op.kind == OP_SETTLE:
            return self._on_ledger_result(LedgerResult(
                operation_id=op.id, ok=False, detail='timeout'))
        if op.kind == OP_MARK_AREA:
            return self._on_task_area_mark_result(TaskAreaMarkResult(
                operation_id=op.id, ok=False, reason='timeout'))
        return self._on_platform_result(PlatformResult(
            operation_id=op.id, kind=op.kind, run_id=op.run_id, room_id=op.room_id,
            outcome=OUTCOME_UNKNOWN, status=STATUS_UNKNOWN, detail='timeout'))

    def _on_wake(self, event: Wake) -> Result:
        if not self._wake_belongs(event):
            return Ignored('old_wake')
        if event.kind == WAKE_MIDNIGHT:
            # 午夜不在这里形成决定：同一份校准时钟的判定在 _advance 里做，
            # 这样"同时到期"只可能记一次 completed（PLAN §1.4）。
            if event.run_id is None:
                # Run 尚未建立的午夜预约：唯一作用是让"运行中的任务等待"按**今天**
                # 重新取一次数（空库仍回等待并重排下一午夜），不直接开播。
                self._recheck_due = True
            return Recorded()
        if event.kind == WAKE_WATCH:
            self._watch_due = True
        elif event.kind == WAKE_TARGET:
            pass      # 到期由 _advance 读同一份业务时钟判定；预约本身不直接开关房
        elif event.kind == _KEY_MEDIA_SYNC:
            # 防空转到点：声明一次媒体期望（仍然由 _advance 统一发出）。
            self._media_sync_due = True
        return Recorded()

    def _wake_belongs(self, event: Wake) -> bool:
        run = self._snapshot.run
        if run is not None:
            return event.run_id == run.id
        # 没有本场时的预约只可能是"运行中任务等待"的下一午夜：其余一律不认。
        s = self._snapshot
        return (event.kind == WAKE_MIDNIGHT and event.run_id is None
                and s.goal.wanted == WANT_RUNNING and s.goal.mode == MODE_TASK
                and s.step == STEP_WAIT_TASKS)

    # ---------------------------------------------------------------- 推进

    def _advance(self, effects: List[Any]) -> None:
        self._freeze_end_if_due()
        self._day_end_step()
        # 媒体调和**独立于房间操作槽**（复核 S1/S4）：慢查询在途不拖住本地推流
        # 恢复，业务 Run 已丢弃也不影响自有对象的收尾。它只产生媒体端口效果，
        # 不开第二项房间请求、不改业务秒数。
        self._adopt_inventory()
        self._advance_media(effects)
        self._emit_media_expectation(effects)

        if self._operation is not None:
            # 当前一轮没有结束：不发第二项房间请求。
            return

        # 平台已判定分区下架：先把任务按“不可完成”移出（幂等、重启可重发），
        # 再走关房核对与下一轮选场。它占用同一个在途操作槽。
        if self._area_mark_pending(self._snapshot):
            return self._issue(OP_MARK_AREA, effects)

        if self._snapshot.step != STEP_WAIT_TASKS:
            # 等任务之外的任何位置都不再需要这次"重走关房核对"的唤醒。
            self._recheck_due = False
        if self._snapshot.step != STEP_LIVE:
            # 低频观察的唤醒只服务于当前这一场，不能留到下一场变成一次空查。
            self._watch_due = False

        for _ in range(_MAX_LOCAL_STEPS):
            s = self._snapshot

            # 0) 用户在关房途中改停，而本程序**没有**任何场次：完成手上一轮即止，
            #    不再为本程序从未承担过的外部房间新发下播。
            if (s.goal.wanted == WANT_STOPPED and s.run is None
                    and s.step in CLOSING_STEPS):
                self._move_to(STEP_IDLE)
                continue

            # 1) 关房流程已经开始就把这一步走完（与 Goal 无关）。
            if s.step in (STEP_CLOSE_QUERY, STEP_CLOSE_CHECK):
                return self._issue(OP_QUERY, effects)
            if s.step == STEP_CLOSE_STOP:
                return self._issue(OP_STOP, effects)
            if s.step == STEP_CLOSE_DONE:
                if self._ledger_pending(s):
                    return self._issue(OP_SETTLE, effects)
                self._finish_closed_step()
                continue

            # 2) 停止目标：必要收尾做完就空闲，不开下一场。
            if s.goal.wanted == WANT_STOPPED:
                if self._run_needs_cleanup(s):
                    self._enter_closing(s)
                    continue
                if self._ledger_pending(s):
                    return self._issue(OP_SETTLE, effects)
                self._enter_idle(s)
                return

            # 3) 已形成结束决定：先幂等记一次，再关房。
            if s.run is not None and s.run.end is not None:
                if (self._ledger_pending(s)
                        and self._settle_deferred_run != s.run.id):
                    return self._issue(OP_SETTLE, effects)
                self._enter_closing(s)
                continue

            # 4) 空闲：原场续播直接查；新场先核对原房。
            if s.step == STEP_IDLE:
                if self._valid_saved_resume(s):
                    self._bind_resume(s)
                else:
                    self._enter_closing(s)
                continue

            # 5) 运行中但今天没有合格场次：停在等待，不空查任务库。
            #    唤醒只来自任务库变化或一份新的运行目标，二者都要求重走关房核对。
            if s.step == STEP_WAIT_TASKS:
                if self._recheck_due:
                    self._recheck_due = False
                    self._enter_closing(s)
                    continue
                return

            # 6) 选任务 / 确定单次参数
            if s.step == STEP_CHOOSE:
                return self._issue(OP_SELECT, effects)

            # 7) 恢复原场：先查房间
            if s.step == STEP_RESUME_QUERY:
                return self._issue(OP_QUERY, effects)

            # 8) 开播（含错分区纠偏：同一个 Run/目标换个分区重发）
            if s.step == STEP_OPEN:
                return self._issue(OP_START, effects)

            # 9) 在播
            if s.step == STEP_LIVE:
                if (s.goal.start_kind == START_RESUME
                        and s.goal.revision != s.run.selected_by_revision):
                    # 合法续播绑定新的控制版本，但不更换 Run 与时长。
                    s.run.selected_by_revision = s.goal.revision
                    self._touch()
                if self._wants_different_run(s):
                    self._freeze_end(END_REPLACED, settle=False)
                    continue
                self._rebuild_appointments(s)
                if self._watch_due:
                    self._watch_due = False
                    return self._issue(OP_QUERY, effects)
                return

            return  # 未知 Step：不做任何平台动作（store 也已把它判为不可信）

        raise RuntimeError('advance 未能收敛：本地步骤出现自激')

    # ---------------------------------------------------------------- 发出

    @staticmethod
    def _area_mark_pending(s: Snapshot) -> bool:
        run = s.run
        return (run is not None and run.area_unavailable
                and not run.area_unavailable_marked
                and run.task_id is not None)

    def _issue(self, kind: str, effects: List[Any]) -> None:
        """发出下一项动作。平台/选任务/结算动作都只从这里出去。

        **发出前全部必需保存先完成，之后才安装在途槽、预算与效果**（复核 R4）。
        先装槽再落盘的旧顺序会留下"预留但没有发出、也没有超时"的幽灵操作：
        保存失败时既没有真实的平台请求，时刻表里也没有回收它的路径，流程永久
        卡死。未发出的命令可以整轮不安装；**已经发生的房间事实**不在这里回滚。
        """
        assert self._operation is None, '同时只允许一项在途操作'
        s = self._snapshot
        run_id = s.run.id if s.run is not None else None
        key = Scheduler.key(s.step, kind, run_id)
        now = self._now()
        if not self._scheduler.ready(key, now):
            # 本步骤本轮的预约还没到点：不自激重发，等预约事件。
            return
        # 门是**一次性**的：通过后即撤，避免过期项以后被当成陈旧唤醒再送一遍。
        self._scheduler.cancel(key)

        granted_anchor = False
        if kind == OP_START and s.run.clock.display_anchor_mono is None:
            # 立即授予**乐观显示**锚点：可显示，不可结算。它与本轮快照在同一次
            # 必需保存里落盘，因此不存在"锚点已存、操作没发"的中间态。
            s.run.clock.grant_display_anchor(now)
            granted_anchor = True
            self._touch()
        if kind == OP_STOP and s.run is not None and s.run.end is None:
            # **自己发出可能关房的动作**：此后平台区间不再可信，等待本身不授权
            # 任何秒数，因此不再无条件外推（旧的目标预约就不能凭这段等待形成
            # completed）。复用既有的 ``suspend_projection``：检查点与**区间锚点
            # 都保留**，所以同一场后来确认仍在播时由
            # :meth:`PlatformClock.confirm_live` 沿原锚点一次补接回来 —— 不丢整段
            # 关房时间、也不新增第二份补偿累计。
            #
            # 这不是"猜平台已经关房"：不写 closed 时刻、不截尾、不把网络超时当
            # closed；真实的关闭事实仍只由后续观察给出（明确 closed 才截尾）。
            s.run.clock.suspend_projection()
            self._touch()
        try:
            self._flush()
        except RequiredSaveFailed:
            if granted_anchor:
                # 这一轮开播还没有派发出去：乐观显示锚点一并撤回，重试时从零显示。
                s.run.clock.revoke_display_anchor()
            raise

        room_id = self._request_room(kind, s)
        operation = Operation(
            id=self._new_id('op'),
            kind=kind,
            step=s.step,
            run_id=run_id,
            room_id=room_id,
            goal_revision=s.goal.revision,
            request=self._freeze_request(kind, s, room_id),
            deadline_mono=now + self._tuning.op_budget_seconds,
        )
        # 到这里全部必需保存都已成功：现在才安装内存槽、预算与效果。
        self._operation = operation
        self._scheduler.arm_operation_timeout(operation.id, operation.deadline_mono, run_id)
        effects.append(self._effect_for(operation))

    def _effect_for(self, operation: Operation) -> Any:
        request = operation.request
        if operation.kind == OP_START:
            return fx.StartPlatform(op_id=operation.id, run_id=operation.run_id or '',
                                    room_id=operation.room_id or 0,
                                    area_id=request.get('area_id'),
                                    task_id=request.get('task_id'),
                                    zone_name=request.get('zone_name') or '',
                                    business_day=request.get('business_day') or '',
                                    settlement_guard=request.get('settlement_guard'),
                                    expected_revision=request.get('expected_revision'),
                                    ticket_business_date=request.get(
                                        'ticket_business_date'))
        if operation.kind == OP_STOP:
            return fx.StopPlatform(op_id=operation.id, run_id=operation.run_id,
                                   room_id=operation.room_id or 0)
        if operation.kind == OP_QUERY:
            return fx.QueryPlatform(op_id=operation.id, run_id=operation.run_id,
                                    room_id=operation.room_id or 0,
                                    step=operation.step)
        if operation.kind == OP_SELECT:
            return fx.SelectTask(op_id=operation.id, revision=operation.goal_revision,
                                 mode=request.get('mode', MODE_TASK),
                                 selection=dict(request.get('selection') or {}),
                                 business_day=request.get('business_day', ''))
        if operation.kind == OP_SETTLE:
            return fx.SettleTask(op_id=operation.id, run_id=operation.run_id or '',
                                 task_id=request.get('task_id'),
                                 business_day=request.get('business_day', ''),
                                 elapsed_seconds=float(request.get('elapsed_seconds') or 0.0),
                                 target_seconds=float(request.get('target_seconds') or 0.0),
                                 completed_at=request.get('completed_at'),
                                 settlement_guard=request.get('settlement_guard'))
        if operation.kind == OP_MARK_AREA:
            return fx.MarkTaskAreaUnavailable(
                op_id=operation.id, run_id=operation.run_id or '',
                task_id=request.get('task_id'),
                zone_name=request.get('zone_name') or '',
                business_day=request.get('business_day') or '',
                reason=request.get('reason') or 'area_unavailable')
        raise RuntimeError(f'未知操作类型：{operation.kind}')

    def _request_room(self, kind: str, s: Snapshot) -> Optional[int]:
        if kind in (OP_SELECT, OP_SETTLE, OP_MARK_AREA):
            return s.run.room_id if s.run is not None else None
        if s.run is not None:
            return s.run.room_id
        # 还没有本程序场次：核对的是当前登录上下文的房间。
        return self._account.room_id

    def _freeze_request(self, kind: str, s: Snapshot, room_id: Optional[int]) -> Dict[str, Any]:
        """冻结本轮参数。发出后不再读"当前分区"，避免把老请求变成新请求。"""
        request: Dict[str, Any] = {'kind': kind, 'step': s.step}
        if kind == OP_START:
            run = s.run
            selection = s.goal.selection
            ticket = (selection if run is not None
                      and s.goal.revision == run.selected_by_revision
                      and selection.kind == SELECT_TASK
                      and selection.task_id == run.task_id else None)
            request.update({
                'run_id': run.id, 'room_id': run.room_id,
                'area_id': run.area_id, 'zone_name': run.zone_name,
                'push_source': run.push_source,
                'task_id': run.task_id,
                'business_day': run.business_day,
                'settlement_guard': run.settlement_guard,
                'expected_revision': (ticket.expected_revision
                                     if ticket is not None else None),
                'ticket_business_date': (ticket.business_date
                                         if ticket is not None else None),
                # 拒绝/不可用后的处置方式取决于**原意图**（自动排序 / 指定 /
                # 恢复），不能读取可能已经被更新的 Goal。
                'mode': s.goal.mode,
                'selection_kind': selection.kind,
                'start_kind': s.goal.start_kind,
            })
        elif kind == OP_MARK_AREA:
            run = s.run
            request.update({
                'run_id': run.id if run is not None else None,
                'room_id': run.room_id if run is not None else room_id,
                'task_id': run.task_id if run is not None else None,
                'zone_name': run.zone_name if run is not None else '',
                'business_day': run.business_day if run is not None else '',
                'reason': 'area_unavailable',
            })
        elif kind in (OP_QUERY, OP_STOP):
            request.update({
                'run_id': s.run.id if s.run is not None else None,
                'room_id': room_id,
                'zone_name': s.run.zone_name if s.run is not None else '',
            })
        elif kind == OP_SELECT:
            selection = s.goal.selection.to_dict()
            request.update({
                'mode': s.goal.mode,
                'selection': selection,
                'business_day': (selection.get('business_date')
                                 if selection.get('kind') == SELECT_TASK
                                 else self._business_day()),
            })
        elif kind == OP_SETTLE:
            run = s.run
            request.update({
                'run_id': run.id,
                'task_id': run.task_id,
                'business_day': run.end.business_day if run.end else run.business_day,
                'elapsed_seconds': run.end.effective_seconds if run.end else 0.0,
                # 达标确认时间与人工进度凭据都是**冻结请求**的一部分：它们在
                # End 形成/任务选取时就已经确定，执行层照原样带过去，不得在
                # 提交那一刻重读当前值。
                'target_seconds': float(run.target_seconds or 0.0),
                'completed_at': (run.end.completed_at if run.end else None),
                'settlement_guard': run.settlement_guard,
            })
        return request

    def _emit_media_expectation(self, effects: List[Any]) -> None:
        """媒体是独立端口：只表达"希望这个房间有一路**自有**推流"。

        本方法由**唯一推进主干**调用，且**在房间操作槽检查之外** —— 房间查询在途
        不会拖住本地推流恢复（复核 S1）。它本身不启线程、不写业务状态、不动房间。

        - OBS 模式核心不进这个分支，因此不产生任何本地进程动作；
        - 换场时先取消旧的一路再请求新的一路：**改选项不能抹掉既有归属**，
          回收失败仍由媒体端口沿用同房原一路（不创建第二路）；
        - 回收由 :meth:`_advance_media` 统一负责，这里只管"要这一路"；
        - **下一次尝试只由这里负责**（复核 S2）：声明期望是一次性的事实，失败
          回执只是把下一次尝试排到 ``_MEDIA_RETRY_SECONDS`` 之后。于是同一瞬间
          至多一次 Sync，重复失败被合并进那一项预约，不组成 Query+Sync 反馈环。

        技术性防空转用它自己的约一秒节奏（PLAN §1.6），不套平台反复关房的退避，
        也不设终身次数上限。
        """
        desired = self._snapshot.run if self._pusher_wanted() else None
        if desired is None:
            return
        key = self._media_sync_key(desired.id)
        if self._pusher_run_id != desired.id:
            if self._pusher_run_id is not None:
                self._reclaim_owned(self._pusher_run_id, reason='rotating', effects=effects)
            self._pusher_run_id = desired.id
            self._pusher_alive = False
            if not self._scheduler.pending(key):
                # 刚绑定这一路：第一次声明不必等防空转门（也保证重启后的残留
                # 会被立刻调和，而不是空等到下一个观察周期）。
                self._media_sync_due = True
        if not self._media_sync_due:
            # 没有待发尝试：媒体期望是**一次性**声明，不是每次推进都重发。
            return
        self._media_sync_due = False
        self._scheduler.cancel(key)
        self._media_sync_outstanding = True
        effects.append(fx.SyncPusher(run_id=desired.id, zone_name=desired.zone_name))

    def _media_sync_key(self, run_id: str) -> str:
        return Scheduler.key('media', _KEY_MEDIA_SYNC, run_id)

    def _reclaim_owned(self, run_id: str, reason: str, effects: List[Any]) -> None:
        """请求回收这一路自有对象，并撤掉它的媒体预约（换场/停止共用）。"""
        effects.append(fx.ReclaimPusher(run_id=run_id, reason=reason))
        self._scheduler.cancel(Scheduler.key('media', _KEY_MEDIA_RECLAIM, run_id))
        self._scheduler.cancel(self._media_sync_key(run_id))
        self._reclaim_tries = 0

    # ---------------------------------------------------------------- 结果映射

    def _record_platform(self, op: Operation, result: PlatformResult) -> None:
        if op.kind == OP_START:
            self._record_start(op, result)
            return
        if op.kind == OP_STOP:
            # 受理、超时、网络不明都只说明"下播请求已发出"，都不是关闭事实。
            self._move_to(STEP_CLOSE_CHECK)
            self._arm_at(Scheduler.key(STEP_CLOSE_CHECK, OP_QUERY, self._run_id()),
                         self._tuning.close_check_delay_seconds)
            return

        # ---- query ----
        step = op.step
        confirmed = result.outcome == OUTCOME_OK and result.status != STATUS_UNKNOWN
        run = self._snapshot.run

        if step in (STEP_CLOSE_QUERY, STEP_CLOSE_CHECK):
            # 关房过程中的**同场、目标分区**观察也进入统一计时转换（唯一的
            # :meth:`_apply_observation_clock`）：live 校准、unknown 暂停、closed
            # 截尾。它**不改变**下面的流程结果，也不再漏掉这段计时 —— 否则自家
            # 关播动作之后的等待会一路外推，旧的目标预约就在那段时间里形成完成。
            # 房间不属于本程序（``run is None``）时没有业务时钟可算，保持原样。
            if run is not None:
                self._apply_observation_clock(run, result, confirmed)
            if not confirmed:
                self._arm_retry(Scheduler.key(step, OP_QUERY, self._run_id()))
                return
            # 轮播不是本程序的直播，stopLive 也不会结束它；与 closed 同样
            # 视为"可以进入开播"，否则会在轮播房间上反复下播。
            if result.status in (STATUS_CLOSED, STATUS_ROUND):
                self._move_to(STEP_CLOSE_DONE)
                return
            # 房间仍在播（本程序或其他客户端）→ 必须先下播，绝不带着开着的
            # 房间进开播。
            self._move_to(STEP_CLOSE_STOP)
            return

        if step == STEP_RESUME_QUERY:
            self._record_observation(step, result, confirmed)
            return

        if step == STEP_LIVE:
            self._record_observation(step, result, confirmed)
            return

    def _record_start(self, op: Operation, result: PlatformResult) -> None:
        run = self._snapshot.run
        if run is None:
            return
        if result.outcome == OUTCOME_OK:
            # 平台业务成功：建立/更新可信进度与时间锚点。
            self._confirm_live(run)
            self._move_to(STEP_LIVE)
            self._rebuild_appointments(self._snapshot)
            self._emit_fact(
                fx.FACT_SESSION_OPEN,
                result=(fx.FACT_RESULT_RESUMED
                        if self._snapshot.goal.start_kind == START_RESUME
                        else fx.FACT_RESULT_STARTED),
                source=('resume'
                        if self._snapshot.goal.start_kind == START_RESUME
                        else 'new'))
            return
        if result.outcome == OUTCOME_AREA_UNAVAILABLE:
            self._record_area_unavailable(op, result)
            return
        if result.outcome in (OUTCOME_REJECTED, OUTCOME_FACE_VERIFY):
            # 人脸 / 明确非网络拒绝：落到 stopped＋原因，随后走同一条关房收尾；
            # 不假装成功、不留自动开播。网络 unknown **不**走这里（见下）。
            still_current = self._snapshot.goal.revision == op.goal_revision
            self._refuse_start(
                STOP_FACE_VERIFY if result.outcome == OUTCOME_FACE_VERIFY
                else STOP_PLATFORM_REJECTED, op.goal_revision)
            if still_current:
                # 只有这次开播**仍是当前意图**时它的拒绝才是用户可见事实；fact 在
                # `_refuse_start` **之后**形成，冻结的是拒绝落地后的人工确认版本。
                self._emit_fact(
                    fx.FACT_SESSION_OPEN,
                    result=(fx.FACT_RESULT_FACE_VERIFY
                            if result.outcome == OUTCOME_FACE_VERIFY
                            else fx.FACT_RESULT_REJECTED),
                    reason=(face_verify_reason(result.detail,
                                               self._account.uid)
                            if result.outcome == OUTCOME_FACE_VERIFY
                            else str(result.detail or '')))
            return
        # 网络不明/超时：不假称成功，随后用查询纠正（乐观值不结算）。
        self._retry_start_by_query()

    def _record_area_unavailable(self, op: Operation,
                                 result: PlatformResult) -> None:
        """平台判定分区已不存在/下架。

        - 自动场：标记 Run 待移出（``area_unavailable``），不停止 Goal、不发
          拒绝事实，随后由 ``_advance`` 先执行不可完成移出，再关房轮转；
        - 单次/指定/恢复：落 ``stopped + area_unavailable`` 并留一条拒绝事实，
          不移出任务（用户明确指定的场次由用户处置）。
        """
        s = self._snapshot
        run = s.run
        request = op.request or {}
        still_current = s.goal.revision == op.goal_revision
        auto_skip = (still_current and run is not None
                     and request.get('mode') == MODE_TASK
                     and request.get('selection_kind') == SELECT_AUTO
                     and request.get('start_kind') != START_RESUME
                     and run.task_id is not None)
        if auto_skip:
            run.area_unavailable = True
            run.area_unavailable_marked = False
            self._touch()
            # 不停止 Goal、不发 session_open 事实（避免〔异常〕刷屏）。
            self._move_to(STEP_CLOSE_QUERY)
            return
        self._refuse_start(STOP_AREA_UNAVAILABLE, op.goal_revision)
        if still_current:
            self._emit_fact(
                fx.FACT_SESSION_OPEN, result=fx.FACT_RESULT_REJECTED,
                reason='该分区已不存在（或已下架）')

    def _on_task_area_mark_result(self, event: TaskAreaMarkResult) -> Result:
        op = self._operation
        if op is None or op.kind != OP_MARK_AREA or event.operation_id != op.id:
            return Ignored('old_mark_area_result')
        self._release_operation(op)
        run = self._snapshot.run
        if run is not None and run.id == op.run_id and run.area_unavailable:
            # 成功与否都视为已受理：失败只记录并继续轮转，不阻塞直播。
            run.area_unavailable_marked = True
            if not event.ok:
                logger.warning('分区不可用任务移出未生效（%s）：继续轮转',
                               event.reason or 'unknown')
            self._touch()
        return Recorded()

    def _refuse_start(self, reason: str, op_revision: int) -> None:
        """开播被拒/需人脸：只在仍属于当前意图时落停止目标，且绝不跳过关房核对。"""
        s = self._snapshot
        if s.goal.revision == op_revision:
            # 这次开播就是当前意图要的：同一 Goal、同一 revision 入口落成 stopped＋原因。
            self._set_goal(WANT_STOPPED, reason)
        # 无论是否停止都回到关房核对：本次开播没有成功事实，房间状态未知；
        # 用户的较新目标不被旧拒绝改写，由 advance 按最新意图推进。
        self._move_to(STEP_CLOSE_QUERY)

    def _retry_start_by_query(self) -> None:
        run = self._snapshot.run
        if run is None:
            return
        self._move_to(STEP_RESUME_QUERY)
        self._arm_at(Scheduler.key(STEP_RESUME_QUERY, OP_QUERY, run.id),
                     self._tuning.close_check_delay_seconds)

    def _apply_observation_clock(self, run: Run, result: PlatformResult,
                                 confirmed: bool) -> None:
        """一份**同场、目标分区**的平台观察对业务时钟的**唯一**转换。

        整个核心只有这一处回答"这条观察把这一场的可信秒数推到哪"，因此
        resume 观察、在播观察与**关房过程**中的观察共用同一份口径，不存在每个
        结果分支各带一套计时规则的写法：

        - 未确认（unknown/超时）→ 显示回最后可信检查点、暂停外推；区间锚点**保留**，
          同一场后来确认仍在播时按它补接（未知本身不证明区间已结束）；
        - 明确 closed / 平台轮播（``live_status=2``）→ 截掉未确认尾段；
        - ``live`` 缺/非法 ``area_id`` → 按普通 unknown，不新造"分区缺失状态"；
        - ``live`` 错分区 → 截尾：绝不借外部房间或错误分区给原 Run 补时；
        - ``live`` 且分区正确 → 用**同一份** :class:`PlatformClock` 校准（未知之后
          的补接由时钟自己的 ``confirm_live`` 决定，不另建补偿累计）。

        已经形成结束决定的场次不在此列：观察不得把一份持久 End 重新拉回计费。
        """
        if run.end is not None:
            return
        if not confirmed:
            run.clock.suspend_projection()
            return
        if result.status in (STATUS_CLOSED, STATUS_ROUND):
            run.clock.discard_unconfirmed_tail()
            return
        if result.status == STATUS_LIVE:
            if result.area_id is None or int(result.area_id) <= 0:
                run.clock.suspend_projection()
                return
            if run.area_id is not None and int(result.area_id) != int(run.area_id):
                run.clock.discard_unconfirmed_tail()
                return
            self._confirm_live(run)
            return
        run.clock.suspend_projection()

    def _record_observation(self, step: str, result: PlatformResult,
                            confirmed: bool) -> None:
        """同类观察的**唯一**校准入口：resume_query 与 live 观察共用。

        计时转换先交给 :meth:`_apply_observation_clock`（与关房观察同一处，
        含 unknown/closed/缺分区/错分区），这里只把结果翻译成流程位置与预约：

        - 未知/超时：暂停外推、撤到期预约、按既有重试节奏再查；
        - 明确 closed：截尾、按原目标重开（停止目标由 advance 收尾）；
        - live 错分区：用同一个 Run/目标在原分区重发 Start 纠偏；
        - live 且分区正确：进入在播并重排本场预约。
        """
        run = self._snapshot.run
        if run is None:
            self._move_to(STEP_CHOOSE)
            return
        if run.end is not None:
            return          # 已冻结：观察不得把它重新拉回在播计费
        self._apply_observation_clock(run, result, confirmed)
        if not confirmed:
            self._on_unknown_observation(run, step)
            return
        if result.status == STATUS_CLOSED:
            self._on_closed_observation(run, step)
            return
        if result.status == STATUS_ROUND:
            self._on_round_observation(run)
            return
        if result.status == STATUS_LIVE:
            if result.area_id is None or int(result.area_id) <= 0:
                # 缺/非法分区按普通 unknown（时钟已在上面暂停）。
                self._on_unknown_observation(run, step)
                return
            if run.area_id is not None and int(result.area_id) != int(run.area_id):
                self._on_wrong_area_observation(run)
                return
            self._move_to(STEP_LIVE)
            self._rebuild_appointments(self._snapshot)
            if step == STEP_RESUME_QUERY:
                # 恢复原场经平台确认在播：这是**进入在播**的一次转换事实。
                # 稳态的 30 秒观察走的是 STEP_LIVE，不在这里重复发事实。
                self._emit_fact(fx.FACT_SESSION_OPEN,
                                result=fx.FACT_RESULT_RESUMED, source='resume')
            return
        self._on_unknown_observation(run, step)

    def _on_unknown_observation(self, run: Run, step: str) -> None:
        """未知：显示回最后可信检查点、暂停外推与到期；原区间锚点保留供补接。"""
        run.clock.suspend_projection()
        self._scheduler.cancel_target(run.id)
        self._arm_retry(Scheduler.key(step, OP_QUERY, run.id))

    def _on_closed_observation(self, run: Run, step: str) -> None:
        """明确 closed：未确认尾段不计入；运行目标下按原目标重开，不轮转。"""
        run.clock.discard_unconfirmed_tail()
        self._scheduler.cancel_target(run.id)
        self._note_platform_close()
        # 停止目标由 advance 收尾；运行目标下从这里回到既有的 open 步骤。
        self._move_to(STEP_OPEN)

    def _on_round_observation(self, run: Run) -> None:
        """平台轮播不是本程序的直播：按 closed 处理，直接按原目标重开。

        ``live_status=2`` 是平台明确返回的合法状态，不能当成 ordinary unknown
        空转重查。它也不是**本程序**的直播，不能进入在播计费；但平台的轮播状态
        不会因为 stopLive 而结束（实测会一直停在 2），而 startLive 可以直接接管
        轮播房间（旧控制器的既有行为）。因此这里与 closed 同路：截尾、撤预约、
        按原 Run/目标重新开播，绝不把它当成"房间还开着"去反复下播。
        """
        run.clock.discard_unconfirmed_tail()
        self._scheduler.cancel_target(run.id)
        self._note_platform_close()
        self._move_to(STEP_OPEN)

    def _on_wrong_area_observation(self, run: Run) -> None:
        """错分区：截尾、撤到期，用**同一个** Run/固定目标在原分区重发 Start 纠偏。

        不调用 Stop、不换 Run/目标、不走新场轮转；纠偏成功后只从正确区间续算。
        """
        run.clock.discard_unconfirmed_tail()
        self._scheduler.cancel_target(run.id)
        self._move_to(STEP_OPEN)

    def _note_platform_close(self) -> None:
        """平台确认关房：反复关房才退避，重开按既有重试节奏等待。

        连续稳定在播一个观察周期后复位（见 :meth:`_confirm_live`）—— 不因一条
        live 响应就清零。这里只登记退避时刻，不产生第二个房间决策入口。
        """
        self._close_streak += 1
        self._stable_mono = None
        run = self._snapshot.run
        if run is None:
            return
        if self._close_streak >= _CLOSE_BACKOFF_AFTER:
            self._scheduler.arm(Scheduler.key(STEP_OPEN, OP_START, run.id),
                                self._now() + self._tuning.retry_delay_seconds,
                                OP_START, run.id)

    def _record_selection(self, op: Operation, result: SelectionResult) -> None:
        s = self._snapshot
        if s.goal.revision != op.goal_revision:
            # 旧 Goal 的任何取数结论（包括 terminal 拒绝）都不能覆盖已经受理的新意图。
            # 释放操作后由下方唯一推进点按当前 Goal 继续，而不是回放旧票据。
            return
        if not result.found:
            if result.terminal:
                # 明确票据/资格拒绝不可伪装成 no_tasks，也不可无限重试原载荷。
                if s.goal.wanted == WANT_RUNNING:
                    self._emit_fact(fx.FACT_SESSION_OPEN,
                                    result=fx.FACT_RESULT_FAILED,
                                    reason=result.reason or 'terminal')
                    self._set_goal(WANT_STOPPED, STOP_SELECTION_REJECTED)
                if s.run is not None and s.run.end is None:
                    self._park_stopped_run(s.run)
                self._move_to(STEP_IDLE)
                return
            if result.reason and result.reason != SELECT_REASON_NO_TASKS:
                # 取数失败（含超时）是暂时故障：留在选任务步骤重试。
                self._arm_retry(Scheduler.key(STEP_CHOOSE, OP_SELECT, self._run_id()))
                return
            if s.run is not None and s.run.end is not None:
                self._discard_run()
            self._move_to(STEP_WAIT_TASKS)   # Goal 仍 running：这是等待，不是用户停止
            if s.goal.wanted == WANT_RUNNING:
                # 运行目标下"今天没有合格场次"是一次**已确认的开放结果**（无下一任务）。
                self._emit_fact(fx.FACT_SESSION_OPEN, result=fx.FACT_RESULT_NONE,
                                reason=result.reason or SELECT_REASON_NO_TASKS)
            return

        if result.business_day and result.business_day != self._business_day():
            # 取数跨越了午夜：不拿昨天的结果建"昨天的新场"，按今天重新取一次。
            self._arm_retry(Scheduler.key(STEP_CHOOSE, OP_SELECT, self._run_id()))
            return

        s.run = self._build_run(result)
        self._settle_deferred_run = None
        self._touch()
        self._move_to(STEP_OPEN)

    def _record_ledger(self, op: Operation, result: LedgerResult) -> None:
        run = self._snapshot.run
        if run is None or run.end is None:
            return
        if result.ok:
            run.end.ledger_receipt = result.receipt or ('already' if result.already else 'ok')
            self._settle_deferred_run = None
            self._touch()
            self._emit_ledger_fact(
                run, fx.FACT_RESULT_ALREADY if result.already
                else fx.FACT_RESULT_CREDITED, result)
            return
        if result.terminal:
            # 明确"不再应用"的终态（人工已改过该任务进度、身份已变、旧快照缺凭据）：
            # 保存一条**能跨重启区分**的回执，然后按既有收尾继续 —— 既不无限重试，
            # 也不把它记成一次成功计入。真实记账结果属于阶段 4 的展示口径。
            run.end.ledger_receipt = (result.receipt
                                      or f'not-applied:{result.reason or "unknown"}')
            self._settle_deferred_run = None
            self._touch()
            self._emit_ledger_fact(run, fx.FACT_RESULT_TERMINAL, result)
            return
        # 本轮失败：结束决定不变。尚未关房先收尾，关妥后再预约重试同一笔。
        self._settle_deferred_run = run.id
        self._arm_retry(Scheduler.key(STEP_CLOSE_DONE, OP_SETTLE, run.id))
        self._emit_ledger_fact(run, fx.FACT_RESULT_SETTLE_FAILED, result)

    def _emit_ledger_fact(self, run: Run, result: str,
                          ledger: LedgerResult) -> None:
        """一份已持久 End 的账本回执收口：**一次**完成事实（幂等重试只会重发同一份）。

        ``completed_at`` 取 End 冻结的达标确认时间，绝不用晚到的提交时刻顶替；
        ``effective_seconds`` 取 End 的有效秒数，不重算。
        """
        if run.end is None or run.end.reason != END_COMPLETED:
            return
        self._emit_fact(
            fx.FACT_RUN_COMPLETED, result=result,
            reason=str(ledger.reason or ledger.detail or ''),
            run_id=run.id, zone_name=run.zone_name,
            business_day=run.end.business_day,
            task_id=run.task_id,
            target_seconds=float(run.target_seconds or 0.0),
            effective_seconds=float(run.end.effective_seconds or 0.0),
            completed_at=run.end.completed_at,
            days_done=ledger.days_done, all_done=ledger.all_done)

    # ---------------------------------------------------------------- 主干 helper

    def _freeze_end_if_due(self) -> bool:
        """只读统一业务时钟。到期就形成**唯一**一次结束决定。"""
        s = self._snapshot
        run = s.run
        if run is None or run.end is not None or run.target_seconds <= 0:
            return False
        if run.mode == MODE_TASK and not self._same_day(run):
            # 跨日的任务半场只在**窗口内、且由连续进程内区间**证明时才记旧日完成；
            # 否则交给午夜规则丢弃（重启/迟到确认不得按事后墙钟补记昨天）。
            if not self._old_day_completion_allowed(run):
                return False
        if run.clock.business_elapsed(self._now()) + _EPS < run.target_seconds:
            return False
        self._freeze_end(END_COMPLETED, settle=(run.mode == MODE_TASK))
        return True

    def _same_day(self, run: Run) -> bool:
        return run.business_day == self._business_day()

    def _old_day_completion_allowed(self, run: Run) -> bool:
        """旧业务日的任务达标是否仍可记：窗口内 ＋ 预计到期也落在窗口内。

        正常运行到期**只读**统一业务时钟、业务日与午夜窗口：

        - 现在不能已经越过 00:00:30（窗口边界）；
        - 本场不能是"截止之后才装载、已经被装载接缝收走可信原区间"的旧场
          （:attr:`_interval_reconstructed`）——那属于事后墙钟追认，一律不记；
        - 预计到期（区间锚点 ＋ 检查点算出的固定时刻）必须落在午夜前后 30 秒内。

        **本场今天曾重启过**不是否决条件（复核 S3）：22:00:30 重启并立即成功恢复
        的原场，之后近两小时持续正常确认，仍属于"截止前按校准计时达标"，必须能
        在窗口内记旧日一次。判据不再来自会被每次确认刷新的锚点（复核 T1）。
        """
        now_wall = self._clock.wall()
        if now_wall > day_deadline(run.business_day) + timedelta(
                seconds=MIDNIGHT_WINDOW_SECONDS):
            return False
        if self._interval_reconstructed:
            # 装载后才取得事实的旧场：没有截止前建立的可信区间，不凭墙钟补账。
            return False
        return self._projected_end_in_window(run)

    def _day_end_step(self) -> None:
        """午夜与结束共用同一份校准时钟，只可能形成一份 EndDecision。

        - 只有**任务**模式预约午夜；单次（有限或 0 不限时）不因午夜轮转；
        - 达标优先：已按校准秒数达标时交给 :meth:`_freeze_end_if_due` 记 completed，
          这里不再另记 midnight，因此"同时到期"也只有一条关房流程；
        - 已确认开播的旧场预计在午夜前后 30 秒内到期 → 保留到 00:00:30；
          窗口内仍未知或未达标 → 00:00:30 丢弃半场，settle=False；
        - 截止以后才取得事实不能复活旧场（旧日 Run 由上面那条排除）；
        - **Run 尚未建立**（运行中的任务等待）也有一次**下一午夜**的预约，
          见 :meth:`_midnight_without_a_run`。
        """
        s = self._snapshot
        run = s.run
        if run is None:
            self._midnight_without_a_run(s)
            return
        key = Scheduler.key(STEP_LIVE, WAKE_MIDNIGHT, run.id)
        if (run.end is not None or run.mode != MODE_TASK
                or run.target_seconds <= 0 or s.goal.wanted != WANT_RUNNING):
            # 停止目标下没有跨日轮转：午夜既不当成"又该开一场"，也不改停止态的半场。
            self._scheduler.cancel(key)
            return
        now_mono = self._now()
        now_wall = self._clock.wall()
        reached = run.clock.business_elapsed(now_mono) + _EPS >= run.target_seconds
        if reached and self._same_day(run):
            # 达标由到期分支处理（同一份时钟，只记一次 completed）。**只限当日**：
            # 旧日的任务半场即使秒数够也不在这里收（见 _old_day_completion_allowed），
            # 否则两边都不收，重排的到期预约会在同一时刻反复自激。
            self._scheduler.cancel(key)
            return
        deadline = day_deadline(run.business_day)
        window_end = deadline + timedelta(seconds=MIDNIGHT_WINDOW_SECONDS)
        if now_wall < deadline:
            self._scheduler.arm(key, now_mono + (deadline - now_wall).total_seconds(),
                                WAKE_MIDNIGHT, run.id)
            return
        if now_wall < window_end and self._projected_end_in_window(run):
            self._scheduler.arm(key, now_mono + (window_end - now_wall).total_seconds(),
                                WAKE_MIDNIGHT, run.id)
            return
        self._scheduler.cancel(key)
        self._freeze_end(END_MIDNIGHT, settle=False)

    def _midnight_without_a_run(self, s: Snapshot) -> None:
        """Run 尚未建立时的午夜预约：运行中的任务等待也要能被午夜本身唤醒。

        已定需求是"午夜本身也是唤醒源"（PLAN §1.4）：等待态不能依赖任务模块恰好
        额外送一次 ``TasksChanged``。预约是**一次性**的，到点后只按**当天**重新取数
        （空库仍回等待，并重排下一个午夜），不直接开播；停止 / 单次不在这里预约，
        也不会被它唤醒。不新增周期线程、不空跑任务库 —— 走的还是同一个内存时刻表。
        """
        key = Scheduler.key(STEP_WAIT_TASKS, WAKE_MIDNIGHT, None)
        if (s.goal.wanted != WANT_RUNNING or s.goal.mode != MODE_TASK
                or s.step != STEP_WAIT_TASKS):
            self._scheduler.cancel(key)
            return
        deadline = day_deadline(self._business_day())
        self._scheduler.arm(
            key, self._now() + (deadline - self._clock.wall()).total_seconds(),
            WAKE_MIDNIGHT, None)

    def _projected_end_in_window(self, run: Run) -> bool:
        """旧场的**预计到期**是否落在午夜前后 30 秒内（可保留到 00:00:30）。

        用**区间锚点 ＋ 检查点**推：这一对在每次确认时同时写下，所以预计到期是一个
        不随 unknown 漂移的固定墙钟时刻 —— 不能用"现在 ＋ 剩余"代替，那会在未知
        把检查点冻住之后越推越远，正好丢掉窗口内的场次。
        """
        start_wall = _parse_wall(run.clock.interval_start_wall)
        if start_wall is None:
            return False      # 没有可信原区间（closed/错区已截尾）：不保留窗口
        remaining = max(0.0, run.target_seconds - run.clock.checkpoint_seconds)
        projected = start_wall + timedelta(seconds=remaining)
        return abs((projected - day_deadline(run.business_day)).total_seconds()) \
            <= MIDNIGHT_WINDOW_SECONDS

    def _adopt_inventory(self) -> None:
        """绑定执行端口在本实例启动时报告的自有对象（与业务 Run 无关）。

        可信归属来自**执行边界**：它只列本实例自己创建、仍然存活的那几路推流，
        因此即使昨天的业务 Run 已经被丢弃（跨日、停止后重启），收尾能力也不会
        跟着消失（复核 S4）。不持久化 PID、不按进程名批量杀、不收养陌生对象。
        """
        if self._pusher_run_id is not None or not self._own_inventory:
            return
        self._pusher_run_id = sorted(self._own_inventory)[0]
        self._reclaim_tries = 0

    def _advance_media(self, effects: List[Any]) -> None:
        """自有推流回收：与房间动作并行、不占操作槽、不阻塞最新目标的受理。

        只有 `_advance` 决定媒体期望；回收确认由媒体端口的窄事实
        (:class:`PusherReport`) 带回。回收失败时**保留归属**并按既有重试节奏再试，
        有次数预算 —— 媒体端口的重试不是第二个房间决策入口。
        """
        if self._pusher_run_id is None:
            return
        if self._pusher_wanted():
            return
        key = Scheduler.key('media', _KEY_MEDIA_RECLAIM, self._pusher_run_id)
        if not self._scheduler.ready(key, self._now()):
            return
        if self._reclaim_tries >= self._tuning.reclaim_attempts:
            return
        self._reclaim_tries += 1
        self._scheduler.arm(key, self._now() + self._tuning.retry_delay_seconds,
                            _KEY_MEDIA_RECLAIM, self._pusher_run_id)
        # 这一路已经不需要了：撤掉它的声明预约，别让待发的 Sync 把停止意图翻回去。
        self._scheduler.cancel(self._media_sync_key(self._pusher_run_id))
        self._media_sync_due = False
        effects.append(fx.ReclaimPusher(run_id=self._pusher_run_id, reason='closing'))

    def _pusher_wanted(self) -> bool:
        s = self._snapshot
        return (s.goal.wanted == WANT_RUNNING and s.run is not None
                and s.step in (STEP_RESUME_QUERY, STEP_OPEN, STEP_LIVE)
                and s.run.push_source == PUSH_FFMPEG)

    # ---------------------------------------------------------------- 已确认事实

    def _emit_fact(self, kind: str, *, result: str = '', reason: str = '',
                   source: str = '', run_id: Optional[str] = None,
                   zone_name: Optional[str] = None,
                   business_day: Optional[str] = None,
                   task_id: Any = _UNSET, target_seconds: Optional[float] = None,
                   effective_seconds: Optional[float] = None,
                   completed_at: Optional[str] = None,
                   days_done: Optional[int] = None,
                   all_done: Optional[bool] = None) -> None:
        """登记一条**已确认转换**的窄事实（不参与决策、不落盘、不阻塞）。

        全部身份与时间在**形成点**冻结成不可变值：执行层随后把它原样交给既有
        异步通知入口，投递时不得回读当前账号/当前 Run。事件时刻取同一业务时钟
        的墙钟；``completed_at`` 只来自已冻结的 End。事实在本轮必需保存成功之后
        才追加到效果末端（见 :meth:`Core.ingest`）。
        """
        run = self._snapshot.run
        current = (run.clock.business_elapsed(self._now()) if run is not None
                   else 0.0)
        if task_id is _UNSET:
            resolved_task_id = run.task_id if run is not None else None
        else:
            resolved_task_id = None if task_id is None else int(task_id)
        self._facts.append(fx.ConfirmedFact(
            kind=kind,
            run_id=(run_id if run_id is not None
                    else (run.id if run is not None else '')),
            uid=int(getattr(self._account, 'uid', 0) or 0),
            room_id=int(getattr(self._account, 'room_id', 0) or 0),
            account_name=str(getattr(self._account, 'display_name', '') or ''),
            zone_name=(zone_name if zone_name is not None
                       else (run.zone_name if run is not None else '')),
            business_day=(business_day if business_day is not None
                          else (run.business_day if run is not None
                                else self._business_day())),
            task_id=resolved_task_id,
            goal_revision=int(self._snapshot.goal.revision),
            target_seconds=float(
                run.target_seconds if (target_seconds is None
                                       and run is not None)
                else (target_seconds or 0.0)),
            effective_seconds=float(current if effective_seconds is None
                                    else effective_seconds),
            happened_at=self._clock.wall().isoformat(timespec='seconds'),
            completed_at=completed_at,
            result=result, reason=reason, source=source,
            days_done=days_done, all_done=all_done))

    def _freeze_end(self, reason: str, settle: bool) -> None:
        run = self._snapshot.run
        if run is None or run.end is not None:
            return
        seconds = run.clock.business_elapsed(self._now())
        run.end = EndDecision(reason=reason, business_day=run.business_day,
                              settle=settle, effective_seconds=seconds,
                              ledger_receipt=None,
                              # 达标确认时间在这里**冻结一次**并随 End 落盘：它是
                              # 历史记录要的事实，重试提交时不能改成"当前时间"。
                              completed_at=self._clock.wall().isoformat(
                                  timespec='seconds'))
        # 计时冻结：之后不再增长，显示与结算继续读同一份校准数据。
        run.clock.suspend_projection()
        run.clock.checkpoint_seconds = seconds
        self._settle_deferred_run = None
        self._scheduler.cancel_target(run.id)
        self._touch()

    def _finish_closed_step(self) -> None:
        """已确认关房。先处理结束的 Run，再按此刻**最新** Goal 决定下一步。"""
        s = self._snapshot
        run = s.run
        if (run is not None and run.end is not None
                and not self._ledger_pending(s)):
            # 单次达标只终结**属于这一场**的那份意图：收尾期间若已受理更新的控制
            # 意图（revision 已经走到前面），旧场只处理事实，不改别人的目标。
            single_done = (run.mode == MODE_SINGLE and run.end.reason == END_COMPLETED
                           and run.selected_by_revision == s.goal.revision)
            self._discard_run()
            if single_done:
                # 单次场次达标即该次意图完成：不轮转（PLAN §1.4）。
                self._set_goal(WANT_STOPPED, STOP_TARGET_REACHED)
                return self._move_to(STEP_IDLE)

        s = self._snapshot
        if s.goal.wanted == WANT_STOPPED:
            # 停止转换只有这一处出口：统一冻结保留进度（见 _enter_idle）。
            return self._enter_idle(s)
        if self._resume_after_close(s):
            return self._move_to(STEP_OPEN)   # 本次已确认 closed，无需再下播一次
        self._discard_run()                   # 已被替换/跨日的半场让位
        return self._move_to(STEP_CHOOSE)     # 这里才按当前日期/排序选择

    def _enter_closing(self, s: Snapshot) -> None:
        if s.step in CLOSING_STEPS:
            return                       # 已在本次关房流程里，不重新初始化
        self._move_to(STEP_CLOSE_QUERY)

    def _enter_idle(self, s: Snapshot) -> None:
        """稳定停止的**唯一**转换点。仅在 ``goal.wanted == stopped`` 时到达。"""
        run = s.run
        account_exit = (s.goal.stop_reason == STOP_ACCOUNT_EXIT)
        if run is not None:
            if account_exit:
                # 退出账号只在既有结束/平台/自有媒体收尾后到此处丢旧进度；普通
                # UserStop 仍保留同日 Run 供显式恢复。
                self._discard_run()
            else:
                # 先把已播秒数冻结成可信检查点、撤掉本场预约，再决定这一场去留。
                self._park_stopped_run(run)
                if s.step != STEP_IDLE:
                    # 只在**转换点**产生停止事实：已被 park 的同日 Run 会让本方法
                    # 在同一轮推进里再次被调用，重复事实会变成重复通知。
                    self._emit_stop_fact(s, run)
                if not run.retains_checkpoint(self._business_day()):
                    self._discard_run()
        if account_exit:
            # 不把旧账号的指定票据留给下一账号；退出 Goal 本身保持 stopped，
            # 下一次开播仍须由新账号提交一份新的用户目标。
            selection = s.goal.selection
            cleared = Selection(kind=SELECT_AUTO,
                                push_source=selection.push_source)
            if selection.to_dict() != cleared.to_dict():
                s.goal.selection = cleared
                self._touch()
        self._move_to(STEP_IDLE)

    def _emit_stop_fact(self, s: Snapshot, run: Run) -> None:
        """稳定停止转换点上的一条**已确认**停止事实。

        只在用户可见的停止原因（``user`` / ``target_reached``）上产生：账号退出、
        人脸、平台明确拒绝各有自己的通知事实（后者由 ``session_open`` 承载），
        这里不重复、也不把"受理"说成"已下播"。已计时取刚冻结的可信检查点
        （``_park_stopped_run`` 已把未确认尾段截掉），不重算、不外推。
        """
        reason = s.goal.stop_reason or STOP_USER
        if reason not in (STOP_USER, STOP_TARGET_REACHED):
            return
        self._emit_fact(fx.FACT_SESSION_STOPPED, result=reason, source=run.mode,
                        run_id=run.id, zone_name=run.zone_name,
                        business_day=run.business_day, task_id=run.task_id,
                        target_seconds=float(run.target_seconds or 0.0),
                        effective_seconds=float(run.clock.checkpoint_seconds))

    def _park_stopped_run(self, run: Optional[Run]) -> None:
        """停止转换的冻结：保留同日未完成 Run 与固定目标，只留实际播过的检查点。

        - 暂停业务外推、清掉本进程内的区间锚点与乐观显示锚点，因此**停播间隔
          不会补进恢复后的有效秒数**（恢复时 :meth:`PlatformClock.confirm_live`
          从这份检查点重新起算）；
        - 撤掉本场剩余的业务到期预约与低频观察预约，旧的计时不会在 idle 里把
          已经关掉的房间记成任务完成；
        - 已经形成结束决定的场次**不动** —— 那笔完成仍按原决定收尾，不因用户
          之后停止而被取消；也没有"Goal=stopped 就永远不许结算"的补丁状态。
        """
        if run is None or run.end is not None:
            return
        run.clock.checkpoint_seconds = run.clock.business_elapsed(self._now())
        run.clock.discard_unconfirmed_tail()
        self._scheduler.cancel_target(run.id)
        self._touch()

    def _run_needs_cleanup(self, s: Snapshot) -> bool:
        """依据当前 Step 判断本程序是否还承担着某个房间的收尾。

        不能写成 ``goal.wanted == running``（用户一改停就以为无需关房），
        也不能写成"Run 非空就关"（同日已停止的存档也有 Run）。
        """
        return s.run is not None and s.step in ACTIVE_STEPS

    def _resume_after_close(self, s: Snapshot) -> bool:
        """刚确认 closed，且用户要的是这一场续播 → 直接从原目标重开。"""
        if s.goal.start_kind != START_RESUME:
            return False
        run = s.run
        return run is not None and run.end is None and self._run_belongs_to_account(run)

    def _valid_saved_resume(self, s: Snapshot) -> bool:
        if s.goal.wanted != WANT_RUNNING or s.goal.start_kind != START_RESUME:
            return False
        run = s.run
        if run is None or run.end is not None:
            return False
        if run.business_day != self._business_day():
            return False
        return self._run_belongs_to_account(run)

    def _bind_resume(self, s: Snapshot) -> None:
        s.run.selected_by_revision = s.goal.revision
        self._touch()
        self._move_to(STEP_RESUME_QUERY)

    def _run_belongs_to_account(self, run: Run) -> bool:
        return run.uid == self._account.uid and run.room_id == self._account.room_id

    def _wants_different_run(self, s: Snapshot) -> bool:
        """已有本场时，这份**未绑定**的新开选择是否要求换场。

        这里是"当前场是否已满足新开选择"的**唯一**判断口：模式、推流来源与单次/
        指定任务的参数都由 :meth:`Selection.matches_run` 一处比较，不为"改时长/
        改模式/改来源"各复制一套流程。

        - 续播（``START_RESUME``）按定义就是原场续播，不参与换场比较；
        - 意图版本未变（同一份选择已经绑到本场）时不换场，保证重复输入幂等；
        - 自动排序 Goal 不会因为"选出的是具体任务"而被误判成不同选择。
        """
        if s.run is None or s.goal.wanted != WANT_RUNNING:
            return False
        if s.goal.start_kind == START_RESUME:
            return False
        if s.goal.revision == s.run.selected_by_revision:
            return False
        return not s.goal.selection.matches_run(s.run, s.goal.mode)

    def _rebuild_appointments(self, s: Snapshot) -> None:
        """维持本场的预约。**已有**的观察预约不被无关输入延期。

        - 精确到期的时刻由 Run 的固定目标算出，与"现在几点推进"无关；
        - 低频观察只在**上一条已经被消费**（或刚完成一次有效观察）之后才登记
          下一条 —— 重复目标、TasksChanged 之类的无关输入反复推进主干时，
          不能把观察时刻一路改写成 now+30 而饿死健康查询。
        """
        run = s.run
        if run is None or run.end is not None:
            return
        now = self._now()
        if run.target_seconds > 0 and run.clock.projecting:
            remaining = max(0.0, run.target_seconds - run.clock.business_elapsed(now))
            if remaining > _EPS:
                self._scheduler.arm_target(run.id, now + remaining)
            else:
                # 已经走到目标：结束判定在主干顶部用同一份时钟处理；这里不再排一个
                # 零延迟的到期预约（那会让"结束判定被拒绝"的分支在同一时刻反复自激）。
                self._scheduler.cancel_target(run.id)
        else:
            self._scheduler.cancel_target(run.id)
        watch_key = Scheduler.key(STEP_LIVE, WAKE_WATCH, run.id)
        if not self._scheduler.pending(watch_key):
            self._scheduler.arm_watch(run.id, now + self._tuning.watch_interval_seconds)

    def _confirm_live(self, run: Run) -> None:
        """平台确认本场在播：冻结已确认部分、重设锚点、续排到期。

        - 首次确认时检查点仍是 0 —— 请求发出后的乐观显示秒数不被追认；
        - 同进程、先前只是 unknown 暂停：沿**原区间锚点**补接未确认区间；
        - **重启后**（本进程没有可信单调锚点）才允许用保存的墙钟区间补时，
          且必须同日、墙钟未倒退；否则只退回检查点继续核对 —— 不凭空增加秒数。
        """
        if run.end is not None:
            return
        now_mono = self._now()
        now_wall = self._clock.wall()
        clock = run.clock
        if clock.interval_start_mono is None and clock.interval_start_wall:
            credit = 0.0
            start_wall = _parse_wall(clock.interval_start_wall)
            if (start_wall is not None
                    and business_day_of(start_wall) == self._business_day()):
                credit = max(0.0, (now_wall - start_wall).total_seconds())
            clock.resume_saved_interval(now_mono, now_wall, credit)
        else:
            clock.confirm_live(now_mono, now_wall)
        goal = self._snapshot.goal
        selection = goal.selection
        if (run.mode == MODE_TASK and run.task_id is not None
                and run.selected_by_revision == goal.revision
                and selection.kind == SELECT_TASK
                and selection.task_id == run.task_id):
            # Start OK 与 Start unknown 后 Query(live/目标分区) 共用这一确认点。
            # 同一用户版本的首场指定选择在此消费，后续完成自然按自动排序。
            goal.selection = Selection(kind=SELECT_AUTO,
                                       push_source=selection.push_source)
        if self._stable_mono is None:
            self._stable_mono = now_mono
        elif now_mono - self._stable_mono >= self._tuning.watch_interval_seconds:
            # 连续稳定在播一个观察周期后才复位退避；不因一条 live 响应就清零。
            self._close_streak = 0
        self._touch()

    def _effective_push_source(self, fallback: str) -> str:
        """本场**有效**推流来源：由执行边界注入的读取口给出的当前设置值。

        这是"人脸确认后继续"与"自动轮转下一场"**共同**消费来源的唯一接缝 ——
        两条路径都在 :meth:`_build_run` 建立新 Run 时经过这里，不各自解释设置。

        没有注入读取口（纯 Core 夹具）时返回 ``fallback``（这份选择自己冻结的
        来源），行为与接线前一致。读取失败只在原地记录并沿用冻结值：设置读取
        绝不改变登录、运行授权或任务数据。
        """
        provider = self._push_source_provider
        if provider is None:
            return fallback
        try:
            source = str(provider() or '').strip()
        except Exception as exc:                          # noqa: BLE001
            logger.warning('读取当前推流设置失败，沿用本场冻结来源：%s', exc)
            return fallback
        return source or fallback

    def _build_run(self, result: SelectionResult) -> Run:
        """本场只从**当前 Goal 的选择**取参数；任务记录不能反过来改推流方式。

        推流来源在**本场建立时**取当前有效设置（见
        :meth:`_effective_push_source`）：保存设置不打断正在执行的那一场，但
        下一场（含人脸确认后继续的那一场）按新设置开。只改这份选择的 ``push_source``
        一个字段，不推进 revision、不动票据/目标/业务日，因此不是新的开播意图，
        也不会被 :meth:`_wants_different_run` 判成换场（它按 revision 相等短路）。

        ``settlement_guard`` 按取数端口报回的**同一一致性区间**里的值原样冻结。
        端口没给（旧快照 / 单次模式）就保持 ``None``：这一场之后的自动完成会在
        提交时因为"缺凭据"落成明确不再应用，而不是在提交那一刻读一个当前值补上。
        """
        goal = self._snapshot.goal
        selection = goal.selection
        source = self._effective_push_source(selection.push_source)
        if source != selection.push_source:
            updated = copy.copy(selection)
            updated.push_source = source
            goal.selection = updated
            selection = updated
        mode = goal.mode
        zone = result.zone_name or selection.zone_name
        area = result.area_id if result.area_id is not None else selection.area_id
        target = float(result.target_seconds or 0.0)
        if target <= 0 and mode == MODE_SINGLE:
            target = float(selection.target_seconds or 0)
        guard = getattr(result, 'settlement_guard', None)
        source_revision = getattr(result, 'source_revision', None)
        return Run(
            id=self._new_id('run'),
            uid=self._account.uid,
            room_id=self._account.room_id,
            area_id=area,
            zone_name=zone,
            mode=mode,
            task_id=result.task_id if mode == MODE_TASK else None,
            business_day=self._business_day(),
            target_seconds=target,
            selected_by_revision=goal.revision,
            push_source=selection.push_source,
            settlement_guard=(None if guard is None else int(guard)),
            task_source_revision=(None if source_revision is None
                                  else int(source_revision)),
        )

    def _discard_run(self) -> None:
        run = self._snapshot.run
        if run is None:
            return
        self._scheduler.cancel_for_run(run.id)
        self._snapshot.run = None
        self._settle_deferred_run = None
        self._touch()

    def _set_goal(self, wanted: str, stop_reason: Optional[str]) -> None:
        """核心侧落定用户目标（目前只用于"单次场次达标即意图完成"）。

        走同一个 Goal、同一个 revision 计数、同一个保存入口，不建第二份意图权威。
        """
        goal = self._snapshot.goal
        goal.wanted = wanted
        goal.stop_reason = stop_reason
        goal.revision += 1
        self._touch()

    def _move_to(self, step: str) -> None:
        if self._snapshot.step == step:
            return
        self._snapshot.step = step
        self._touch()

    # ---------------------------------------------------------------- 预约与保存

    def _arm_retry(self, key: str) -> None:
        self._arm_at(key, self._tuning.retry_delay_seconds)

    def _arm_at(self, key: str, delay: float) -> None:
        self._scheduler.arm(key, self._now() + delay, _kind_of_key(key), self._run_id())

    def _arm_save_retry(self) -> None:
        self._scheduler.arm(_KEY_SAVE_RETRY,
                            self._now() + self._tuning.retry_delay_seconds,
                            _KEY_SAVE_RETRY)

    def _touch(self) -> None:
        self._dirty = True

    def _flush(self) -> None:
        """必需提交。失败抛 :class:`RequiredSaveFailed`，由 ingest 统一收口。"""
        if not self._dirty:
            return
        if not self._store.save(self._snapshot):
            raise RequiredSaveFailed(self._store.last_error or '未知保存失败')
        self._dirty = False

    # ---------------------------------------------------------------- 小工具

    def _now(self) -> float:
        return float(self._clock.mono())

    def _business_day(self) -> str:
        return business_day_of(self._clock.wall())

    def _run_id(self) -> Optional[str]:
        run = self._snapshot.run
        return run.id if run is not None else None

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f'{prefix}-{self._seq}'

    def _release_operation(self, op: Operation) -> None:
        self._operation = None
        self._scheduler.cancel('op:' + op.id)

    def _same_subject(self, op: Operation, event: PlatformResult) -> bool:
        """结果主体核对：以**冻结请求**带出的身份为准。

        分区是否等于目标**不**作为收件门 —— 错分区是待处理的事实，
        不是本地请求身份。
        """
        if event.kind != op.kind:
            return False
        if event.room_id is not None and op.room_id is not None:
            if int(event.room_id) != int(op.room_id):
                return False
        if event.run_id is not None and op.run_id is not None:
            if event.run_id != op.run_id:
                return False
        return True

    def _ledger_pending(self, s: Snapshot) -> bool:
        run = s.run
        return (run is not None and run.end is not None and run.end.settle
                and not run.end.ledger_receipt)

    def _goal_from_event(self, event: UserGoal) -> Tuple[Optional[Goal], str]:
        if event.wanted not in (WANT_RUNNING, WANT_STOPPED):
            return None, f'非法的运行目标：{event.wanted!r}'
        if event.mode not in (MODE_TASK, MODE_SINGLE):
            return None, f'非法的模式：{event.mode!r}'
        if event.start_kind not in (START_NEW, START_RESUME):
            return None, f'非法的开播方式：{event.start_kind!r}'
        if event.stop_reason is not None and event.stop_reason not in (
                STOP_USER, STOP_TARGET_REACHED, STOP_FACE_VERIFY,
                STOP_PLATFORM_REJECTED, STOP_ACCOUNT_EXIT):
            return None, f'非法的停止原因：{event.stop_reason!r}'

        selection = event.selection
        if selection is None:
            selection = Selection(kind=(SELECT_SINGLE if event.mode == MODE_SINGLE
                                        else SELECT_AUTO))
        elif isinstance(selection, dict):
            selection = Selection.from_dict(selection)
        if not isinstance(selection, Selection):
            return None, '非法选择'
        if event.mode == MODE_SINGLE and selection.kind != SELECT_SINGLE:
            return None, '单次模式必须给出具体参数'
        if event.mode == MODE_TASK and selection.kind == SELECT_SINGLE:
            return None, '任务模式不接受单次参数'
        if selection.kind not in (SELECT_AUTO, SELECT_TASK, SELECT_SINGLE):
            return None, '非法选择类型'
        if selection.kind == SELECT_TASK:
            if (not isinstance(selection.task_id, int)
                    or isinstance(selection.task_id, bool)
                    or selection.task_id <= 0):
                return None, '指定任务缺少有效 task_id'
            if (not isinstance(selection.expected_revision, int)
                    or isinstance(selection.expected_revision, bool)
                    or selection.expected_revision < 0):
                return None, '指定任务缺少有效 expected_revision 票据'
            try:
                from datetime import date
                ticket_day = date.fromisoformat(selection.business_date or '')
            except (TypeError, ValueError):
                return None, '指定任务缺少有效 business_date 票据'
            if ticket_day.isoformat() != selection.business_date:
                return None, '指定任务 business_date 票据格式无效'
        elif (selection.expected_revision is not None
              or selection.business_date is not None):
            return None, '自动/单次选择不能携带指定任务票据'
        if event.wanted == WANT_STOPPED and event.start_kind != START_NEW:
            return None, '停止目标不能带续播方式'

        goal = Goal(
            revision=self._snapshot.goal.revision,
            wanted=event.wanted,
            mode=event.mode,
            selection=copy.deepcopy(selection),
            start_kind=event.start_kind,
            stop_reason=event.stop_reason,
        )
        return goal, ''

    @staticmethod
    def _goal_equal(current: Goal, candidate: Goal) -> bool:
        return (current.wanted == candidate.wanted
                and current.mode == candidate.mode
                and current.start_kind == candidate.start_kind
                and current.stop_reason == candidate.stop_reason
                and current.selection.to_dict() == candidate.selection.to_dict())


def _kind_of_key(key: str) -> str:
    """预约 key 的种类：``step:kind:run`` 中的 kind。"""
    parts = key.split(':')
    return parts[1] if len(parts) > 1 else key


def _parse_wall(value: Any) -> Optional[datetime]:
    """把保存的墙钟锚点解析回 ``datetime``；非法一律 ``None``（不猜）。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
