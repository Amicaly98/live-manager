"""场次核心数据模型（核心重写版）。

职责边界见 ``docs/plans/live-session-core-reset-20260930/PHASE1A-DESIGN.md`` §1：

- 只有 **Goal** 表达用户意图（运行/停止、任务/单次、指定选择、停止原因）；
- 只有 **Run.clock** 提供业务时长（平台锚点＋可信检查点，一份数据）；
- **Step** 只是一个流程位置，不是一组可组合的布尔量；
- **EndDecision** 放在 Run 内，不另建交接文件；
- **Operation** 只存在于内存，重启不重放旧请求。

持久化只写一个快照。PID / 线程 / 邮件投递状态不入库。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

#: 新模型标识。被退役的原型写的是整数 ``3``，两者永不相等 —— 旧档会被判定为
#: “不支持的 schema”而不是被静默读成新模型（真正的一次性迁移属于阶段 2）。
SCHEMA_VERSION = 'core-reset-1a.1'

# ---- Goal.wanted：最新控制目标 ----
WANT_RUNNING = 'running'
WANT_STOPPED = 'stopped'

# ---- Goal.mode：任务 / 单次 ----
MODE_TASK = 'task'
MODE_SINGLE = 'single'

# ---- Goal.start_kind：显式区分"新场"与"原场续播" ----
START_NEW = 'new'
START_RESUME = 'resume'

# ---- Goal.selection.kind ----
SELECT_AUTO = 'auto'          # 按当前业务日排序自动选任务
SELECT_TASK = 'task'          # 指定任务
SELECT_SINGLE = 'single'      # 单次参数（分区/分区 ID/固定时长）

# ---- 推流方式：本场的一个参数，不是第二套状态机 ----
PUSH_FFMPEG = 'ffmpeg'
PUSH_OBS = 'obs'

# ---- 停止原因：停止态只由这些事实进入 ----
STOP_USER = 'user'
STOP_TARGET_REACHED = 'target_reached'
STOP_FACE_VERIFY = 'face_verify'
STOP_PLATFORM_REJECTED = 'platform_rejected'
STOP_AREA_UNAVAILABLE = 'area_unavailable'
STOP_ACCOUNT_EXIT = 'account_exit'
STOP_SELECTION_REJECTED = 'selection_rejected'

# ---- Run.end.reason：一场为何结束 ----
END_COMPLETED = 'completed'   # 业务目标达标
END_MIDNIGHT = 'midnight'     # 跨日（1B）
END_REPLACED = 'replaced'     # 用户在运行中换成另一场

# ---- Step：流程位置 ----
STEP_IDLE = 'idle'
STEP_WAIT_TASKS = 'wait_tasks'
STEP_CLOSE_QUERY = 'close_query'
STEP_CLOSE_STOP = 'close_stop'
STEP_CLOSE_CHECK = 'close_check'
STEP_CLOSE_DONE = 'close_done'
STEP_CHOOSE = 'choose'
STEP_RESUME_QUERY = 'resume_query'
STEP_OPEN = 'open'
STEP_LIVE = 'live'

#: 正在关房：这三步要把当前这一轮走完
CLOSE_STEPS = (STEP_CLOSE_QUERY, STEP_CLOSE_STOP, STEP_CLOSE_CHECK)
#: 关房流程的全部步骤（含已确认关闭、待收尾）
CLOSING_STEPS = CLOSE_STEPS + (STEP_CLOSE_DONE,)
#: 本程序已经承担了某个房间的步骤：改成停止目标时要做收尾
ACTIVE_STEPS = CLOSING_STEPS + (STEP_RESUME_QUERY, STEP_OPEN, STEP_LIVE)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else None


def _dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Selection:
    """用户这一次的选择。自动排序与指定任务/单次参数的唯一区别就在这里。"""

    kind: str = SELECT_AUTO
    task_id: Optional[int] = None
    zone_name: str = ''
    area_id: Optional[int] = None
    push_source: str = PUSH_FFMPEG
    target_seconds: int = 0
    # 指定任务票据只随这一份用户选择存在；自动选择没有人工版本屏障。
    expected_revision: Optional[int] = None
    business_date: Optional[str] = None

    def matches_run(self, run: 'Run', mode: str) -> bool:
        """当前 Run 是否已经满足这份**明确新开**的选择 —— 决定"用户是否要求换场"。

        比较必须涵盖这次开播的**有效参数**：只比名称/分区会把"同分区换时长、换模式、
        换推流来源"误判成"已经执行过了"（复核 R4b）。`mode` 由调用方从 Goal 传入，
        不往 Selection 里加字段（持久模型不变）。

        - 模式与推流来源对**任何**选择都必须一致；
        - **自动排序是策略**，不是一组具体参数：Goal 写着"自动"而 Run 上是它选出的
          某个具体任务，这是正常的，不能拿自动 Goal 的空名称/空分区/target=0 去比，
          否则恢复自动模式会不停换场；
        - 指定任务按已有身份（``task_id``）判定，不重新抽原 Run 的时长；
        - 单次参数逐项比较用户指定的固定目标时长与目标分区/名称。
        """
        if mode != run.mode:
            return False
        if self.push_source != run.push_source:
            return False
        if self.kind == SELECT_AUTO:
            return True
        if self.kind == SELECT_TASK:
            return run.task_id is not None and run.task_id == self.task_id
        if run.zone_name != self.zone_name:
            return False
        if self.area_id is not None and run.area_id != self.area_id:
            return False
        return abs(float(run.target_seconds) - float(self.target_seconds)) < 1e-6

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind,
            'task_id': self.task_id,
            'zone_name': self.zone_name,
            'area_id': self.area_id,
            'push_source': self.push_source,
            'target_seconds': int(self.target_seconds),
            'expected_revision': self.expected_revision,
            'business_date': self.business_date,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Selection':
        return cls(
            kind=str(data.get('kind') or SELECT_AUTO),
            task_id=data.get('task_id'),
            zone_name=str(data.get('zone_name') or ''),
            area_id=data.get('area_id'),
            push_source=str(data.get('push_source') or PUSH_FFMPEG),
            target_seconds=_i(data.get('target_seconds'), 0),
            expected_revision=(None if data.get('expected_revision') is None
                               else _i(data.get('expected_revision'), -1)),
            business_date=(None if data.get('business_date') is None
                           else str(data.get('business_date'))),
        )


@dataclass
class Goal:
    """用户的最新目标。**唯一**能表达用户意图的地方。

    ``revision`` 只在控制意图真的改变时自增；它用于识别"用户是否换了目标"，
    不用于作废仍在进行的操作结果。
    """

    revision: int = 0
    wanted: str = WANT_STOPPED
    mode: str = MODE_TASK
    selection: Selection = field(default_factory=Selection)
    start_kind: str = START_NEW
    stop_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'revision': int(self.revision),
            'wanted': self.wanted,
            'mode': self.mode,
            'selection': self.selection.to_dict(),
            'start_kind': self.start_kind,
            'stop_reason': self.stop_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Goal':
        return cls(
            revision=_i(data.get('revision'), 0),
            wanted=str(data.get('wanted') or WANT_STOPPED),
            mode=str(data.get('mode') or MODE_TASK),
            selection=Selection.from_dict(data.get('selection') or {}),
            start_kind=str(data.get('start_kind') or START_NEW),
            stop_reason=data.get('stop_reason') or None,
        )


@dataclass
class EndDecision:
    """这一场为什么结束、要不要记完成。放在 Run 内，不另建 handover。

    ``completed_at``：**达标确认时间** —— 由唯一形成结束决定的 :meth:`Core._freeze_end`
    在那一刻冻结并随 End 落盘，供"分区历史"按业务日保存发生时刻用。它与业务日分开
    保存（午夜后 30 秒窗口内仍合法归前一业务日）。旧快照没有这个字段时保持
    ``None``（未知），**不**在提交时临时填当前时间：那是伪造历史时刻。
    """

    reason: str
    business_day: str
    settle: bool
    effective_seconds: float
    ledger_receipt: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'reason': self.reason,
            'business_day': self.business_day,
            'settle': bool(self.settle),
            'effective_seconds': round(float(self.effective_seconds), 3),
            'ledger_receipt': self.ledger_receipt,
            'completed_at': self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EndDecision':
        return cls(
            reason=str(data.get('reason') or END_COMPLETED),
            business_day=str(data.get('business_day') or ''),
            settle=bool(data.get('settle', False)),
            effective_seconds=_f(data.get('effective_seconds'), 0.0),
            ledger_receipt=data.get('ledger_receipt') or None,
            completed_at=data.get('completed_at') or None,
        )


@dataclass
class PlatformClock:
    """本场的业务计时。**唯一**一份能结账的秒数。

    - ``checkpoint_seconds``：已确认（可信）的业务秒数；
    - ``interval_start_mono``：本进程内已确认开播区间的锚点；
    - ``interval_start_wall``：同一区间的墙钟锚点（重启重建用，1B 校验）；
    - ``projecting``：未知/重启后置假以暂停外推，锚点保留供后续校准；
    - ``display_anchor_mono``：发 Start 时的**乐观显示**锚点 ——
      :meth:`business_elapsed` 永不读它，因此它不能结算任何秒数。

    锚点 ``0.0`` 是合法值，判断是否初始化只能比 ``None``。
    """

    checkpoint_seconds: float = 0.0
    interval_start_mono: Optional[float] = None
    interval_start_wall: Optional[str] = None
    projecting: bool = False
    display_anchor_mono: Optional[float] = None

    # ---------- 业务计时 ----------

    def business_elapsed(self, now_mono: float) -> float:
        """可结算的业务秒数。只在**已确认**的锚点上外推。"""
        total = max(0.0, float(self.checkpoint_seconds))
        if self.projecting and self.interval_start_mono is not None:
            total += max(0.0, float(now_mono) - float(self.interval_start_mono))
        return max(0.0, total)

    def set_checkpoint(self, seconds: float) -> None:
        self.checkpoint_seconds = max(0.0, float(seconds))

    def confirm_live(self, now_mono: float, now_wall: Optional[datetime] = None) -> None:
        """平台确认本场在播。**一份**累计权威：这里同时推进检查点与锚点。

        - **首次**（没有原区间锚点）：从此刻起算，检查点冻结在"此刻已可信的秒数"，
          请求发出后的乐观等待**不被追认**；
        - **正常在播中的再次确认**（``projecting`` 仍为真）：把检查点推进到此刻、
          重设锚点 —— 数值与原先的外推一致，只是让检查点保持新鲜，之后确认关房
          "截掉未确认尾段"才有可信的目标可回退；
        - **未知之后的再次确认**（``projecting`` 被暂停、原锚点仍在）：先沿**原锚点**
          把未确认区间补接回来（未知本身不证明区间已结束，PLAN §1.3），**再**把此刻
          已确认的秒数冻结进检查点、重设一致锚点。

        补接分支以前只把 ``projecting`` 改回真、不动检查点，于是"已经确认过的秒数"
        仍停在旧检查点上：随后一次 closed/错区/unknown 调
        :meth:`discard_unconfirmed_tail` 时，退回的就是**上上次**的检查点，已经在
        90 秒确认过的进度被判成未确认尾段一并截掉（复核 R1）。累计只有这一处，
        不另建补偿系数或第二份权威。
        """
        if not self.projecting and self.interval_start_mono is not None:
            # 未知只暂停了外推：先把外推沿原锚点补回来，下面的冻结/重设才不会
            # 丢掉这段已经确认过的区间。
            self.projecting = True
        self.checkpoint_seconds = self.business_elapsed(now_mono)
        self.interval_start_mono = float(now_mono)
        if now_wall is not None:
            self.interval_start_wall = _iso(now_wall)
        self.projecting = True
        self.display_anchor_mono = None

    def resume_saved_interval(self, now_mono: float, now_wall: Optional[datetime],
                              credit_seconds: float) -> None:
        """重启后用**保存的墙钟区间**补接：只补可信的离线秒数，再从此刻重建本进程区间。

        ``credit_seconds`` 由调用方按"同日、墙钟未倒退"判据算好，这里只保证不为负。
        同一进程的业务秒数仍只用单调差不变量 —— 这条路径只在单调锚点为空时才走。
        """
        self.checkpoint_seconds = max(0.0, float(self.checkpoint_seconds)
                                      + max(0.0, float(credit_seconds)))
        self.interval_start_mono = float(now_mono)
        self.interval_start_wall = _iso(now_wall) if now_wall is not None else None
        self.projecting = True
        self.display_anchor_mono = None

    def suspend_projection(self) -> None:
        """未知/重启：显示回到**最后可信检查点**并暂停外推。

        不把"上次确认到现在"这段未确认的尾段计入业务秒数；锚点保留，
        供 1B 的"同场后来确认仍在播"路径校准补接。
        """
        self.projecting = False

    def discard_unconfirmed_tail(self, now_mono: Optional[float] = None) -> None:
        """确认关房或错分区：截掉未确认尾段，回到最后可信检查点。"""
        self.projecting = False
        self.interval_start_mono = None
        self.interval_start_wall = None
        self.display_anchor_mono = None

    # ---------- 乐观显示（不可结算） ----------

    def grant_display_anchor(self, now_mono: float) -> None:
        if self.display_anchor_mono is None:
            self.display_anchor_mono = float(now_mono)

    def revoke_display_anchor(self) -> None:
        """撤销一份**尚未发出**的开播留下的乐观显示锚点。

        发出前的必需保存失败时，这一轮开播没有被派发出去；乐观锚点必须一并撤回，
        否则重试成功后显示会把这中间的等待时间算成已经播过（复核 R4）。
        """
        self.display_anchor_mono = None

    @property
    def has_display_anchor(self) -> bool:
        return self.display_anchor_mono is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'checkpoint_seconds': round(float(self.checkpoint_seconds), 3),
            'interval_start_mono': self.interval_start_mono,
            'interval_start_wall': self.interval_start_wall,
            'projecting': bool(self.projecting),
            'display_anchor_mono': self.display_anchor_mono,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PlatformClock':
        mono = data.get('interval_start_mono')
        anchor = data.get('display_anchor_mono')
        return cls(
            checkpoint_seconds=_f(data.get('checkpoint_seconds'), 0.0),
            interval_start_mono=(None if mono is None else _f(mono, 0.0)),
            interval_start_wall=data.get('interval_start_wall') or None,
            projecting=bool(data.get('projecting', False)),
            display_anchor_mono=(None if anchor is None else _f(anchor, 0.0)),
        )


@dataclass
class Run:
    """这一场。身份、固定目标、业务日与计时检查点。

    房间来自当前登录上下文，不由任务或选择决定；``uid`` / ``room_id`` 只是
    本场实际用的账号房间，供请求与收尾使用。
    """

    id: str
    uid: int = 0
    room_id: int = 0
    area_id: Optional[int] = None
    zone_name: str = ''
    mode: str = MODE_TASK
    task_id: Optional[int] = None
    business_day: str = ''
    #: 本场只确定一次的目标秒数。0 = 不限时（仅单次模式允许）。
    target_seconds: float = 0.0
    #: 本场是由哪一次控制选择建立的；不保存第二份 Goal。
    selected_by_revision: int = 0
    push_source: str = PUSH_FFMPEG
    #: 选取本场任务时冻结的**人工进度变更代数**（不透明并发凭据）。
    #:
    #: 它只在用户真的改动这个任务的进度事实时才前进；自动结算、日切、别的任务、
    #: 派生值重算都不动它。自动结算事务里比较它，是为了回答一个只靠 End 与日期
    #: 回答不了的问题：「原场正常结束」与「用户已经手动更正了配额之后旧 End 才到」
    #: 在数据上无法区分。它不是第二份累计天数，也不参与任何显示。
    #: ``None`` = 这份快照没有冻结过凭据（旧格式）；提交时**不会**临时补当前值。
    settlement_guard: Optional[int] = None
    # 取数时的版本来源，仅供首次指定任务 Start 复核；不是恢复时的授权票据。
    task_source_revision: Optional[int] = None
    #: 平台判定本场分区已不存在/下架：先完成“不可完成移出”，再关房轮转。
    #: ``*_marked`` 记录这条任务事实已被执行层受理（幂等；重启可重发）。
    area_unavailable: bool = False
    area_unavailable_marked: bool = False
    clock: PlatformClock = field(default_factory=PlatformClock)
    end: Optional[EndDecision] = None

    def remaining(self, now_mono: float) -> Optional[float]:
        """距目标的剩余秒数；``None`` = 不限时。"""
        if self.target_seconds <= 0:
            return None
        return self.target_seconds - self.clock.business_elapsed(now_mono)

    def retains_checkpoint(self, today: str) -> bool:
        """同日、未作结束决定的一场可以留给"恢复原场"续播。"""
        return self.end is None and self.business_day == today

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'uid': int(self.uid),
            'room_id': int(self.room_id),
            'area_id': self.area_id,
            'zone_name': self.zone_name,
            'mode': self.mode,
            'task_id': self.task_id,
            'business_day': self.business_day,
            'target_seconds': round(float(self.target_seconds), 3),
            'selected_by_revision': int(self.selected_by_revision),
            'push_source': self.push_source,
            'settlement_guard': (None if self.settlement_guard is None
                                 else int(self.settlement_guard)),
            'task_source_revision': (None if self.task_source_revision is None
                                     else int(self.task_source_revision)),
            'area_unavailable': bool(self.area_unavailable),
            'area_unavailable_marked': bool(self.area_unavailable_marked),
            'clock': self.clock.to_dict(),
            'end': self.end.to_dict() if self.end else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Run':
        end = data.get('end')
        guard = data.get('settlement_guard')
        source_revision = data.get('task_source_revision')
        return cls(
            id=str(data.get('id') or ''),
            uid=_i(data.get('uid'), 0),
            room_id=_i(data.get('room_id'), 0),
            area_id=data.get('area_id'),
            zone_name=str(data.get('zone_name') or ''),
            mode=str(data.get('mode') or MODE_TASK),
            task_id=data.get('task_id'),
            business_day=str(data.get('business_day') or ''),
            target_seconds=_f(data.get('target_seconds'), 0.0),
            selected_by_revision=_i(data.get('selected_by_revision'), 0),
            push_source=str(data.get('push_source') or PUSH_FFMPEG),
            settlement_guard=(None if guard is None else _i(guard, 0)),
            task_source_revision=(None if source_revision is None
                                  else _i(source_revision, -1)),
            area_unavailable=bool(data.get('area_unavailable', False)),
            area_unavailable_marked=bool(
                data.get('area_unavailable_marked', False)),
            clock=PlatformClock.from_dict(data.get('clock') or {}),
            end=EndDecision.from_dict(end) if isinstance(end, dict) else None,
        )


@dataclass
class Snapshot:
    """唯一权威快照。写进 JSON 的就是它。"""

    schema_version: str = SCHEMA_VERSION
    goal: Goal = field(default_factory=Goal)
    run: Optional[Run] = None
    step: str = STEP_IDLE
    saved_wall: Optional[str] = None
    #: 写入/加载异常时的现场说明（损坏、保存失败）。不静默清零。
    health: Dict[str, Any] = field(default_factory=dict)

    @property
    def today(self) -> str:
        return business_day_of(datetime.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'goal': self.goal.to_dict(),
            'run': self.run.to_dict() if self.run else None,
            'step': self.step,
            'saved_wall': self.saved_wall,
            'health': dict(self.health or {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Snapshot':
        """**宽松**构造（夹具用）。

        权威读取不走这里：``SessionStore.load()`` 有自己的严格判定器，
        合法 JSON 不等于可信快照。
        """
        run = data.get('run')
        return cls(
            schema_version=str(data.get('schema_version') or SCHEMA_VERSION),
            goal=Goal.from_dict(data.get('goal') or {}),
            run=Run.from_dict(run) if isinstance(run, dict) else None,
            step=str(data.get('step') or STEP_IDLE),
            saved_wall=data.get('saved_wall'),
            health=dict(data.get('health') or {}),
        )

    def clone(self) -> 'Snapshot':
        return copy.deepcopy(self)


# ---- 业务日工具 ----

def business_day_of(moment: datetime) -> str:
    """业务日 = 本地日期。午夜 00:00 即翻篇。"""
    return moment.date().isoformat()


def day_deadline(business_day: str) -> datetime:
    """该业务日的截止：业务日之后的下一个本地午夜。"""
    moment = datetime.fromisoformat(business_day)
    return datetime(moment.year, moment.month, moment.day) + timedelta(days=1)


def next_midnight_after(moment: datetime) -> datetime:
    """严格晚于 ``moment`` 的下一个本地午夜。"""
    base = datetime(moment.year, moment.month, moment.day)
    if moment == base:
        return base
    return base + timedelta(days=1)


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)
