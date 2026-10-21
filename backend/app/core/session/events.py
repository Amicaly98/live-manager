"""进入唯一决策点的事件，以及决策点的回执。

事件只有四类作用（对应 PLAN §2.3 的四个入口）：

1. 保存最新目标：:class:`UserGoal`；
2. 记录本轮事实：:class:`PlatformResult` / :class:`SelectionResult` / :class:`LedgerResult`；
3. 预约/唤醒：:class:`Wake` / :class:`Timeout` / :class:`TasksChanged` / :class:`Bootstrap`。

事件都是不可变值对象。核心 ``ingest`` 一个事件，返回一个回执和一组效果；
核心自身不做网络、不启进程、不发邮件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# ---- 平台操作的结果分类 ----
OUTCOME_OK = 'ok'                         # 平台业务成功
OUTCOME_REJECTED = 'rejected'             # 明确的非网络拒绝（1B 接入）
OUTCOME_FACE_VERIFY = 'face_verify'       # 平台要求人脸验证（1B 接入）
OUTCOME_AREA_UNAVAILABLE = 'area_unavailable'  # 平台判定分区不存在/已下架
OUTCOME_UNKNOWN = 'unknown'               # 超时/网络不明；不等于平台没做

# ---- 房间状态观察 ----
STATUS_LIVE = 'live'
STATUS_CLOSED = 'closed'
STATUS_ROUND = 'round'
STATUS_UNKNOWN = 'unknown'

# ---- 预约种类 ----
WAKE_TARGET = 'target'                    # 本场到期
WAKE_WATCH = 'watch'                      # 低频状态观察
WAKE_MIDNIGHT = 'midnight'                # 业务日翻篇（1B）

#: 一次平台操作只允许有一种类型在途
OP_QUERY = 'query'
OP_STOP = 'stop'
OP_START = 'start'
OP_SELECT = 'select'
OP_SETTLE = 'settle'
OP_MARK_AREA = 'mark_area'


class Event:
    """事件基类（仅用于类型标注）。"""


class Result:
    """决策点回执基类。"""


@dataclass(frozen=True)
class Accepted(Result):
    """控制目标已落盘。**不代表**房间已经开/关。"""


@dataclass(frozen=True)
class Recorded(Result):
    """本轮结果/唤醒已记录，并已按最新目标推进过一轮。"""


@dataclass(frozen=True)
class Ignored(Result):
    """与当前事实无关的输入（旧操作、旧预约），未改变任何状态。"""

    reason: str = ''


@dataclass(frozen=True)
class Rejected(Result):
    """输入不被接受（校验失败、必需保存失败）。"""

    reason: str = ''


# ---------------- 1. 用户目标 ----------------

@dataclass(frozen=True)
class UserGoal(Event):
    """用户最新的运行/停止意图。

    任务/单次与 FFmpeg/OBS 都是**参数**，不各建一套状态机。
    ``start_kind`` 显式区分"新场"与"原场续播"：新场一律先关再开，
    续播直接沿原目标与检查点继续。
    """

    wanted: str
    mode: str = 'task'
    selection: Any = None                 # model.Selection；None = 自动
    start_kind: str = 'new'
    stop_reason: Optional[str] = None
    expected_run_id: Optional[str] = None
    expected_goal_revision: Optional[int] = None
    abandon_run_id: Optional[str] = None
    abandon_goal_revision: Optional[int] = None
    #: 当前一次放弃/重开请求附带的旧选择快照；仅用于 actor 冲突核验，不持久化。
    abandon_selection: Any = None


@dataclass(frozen=True)
class UserConfirmation(Event):
    """人工"已验证 / 继续当前停止原因"（例如人脸验证之后的重试）。

    它不是第二套人脸状态机，也不带 token / 二维码 / 邮件：只是一份**窄输入**，
    携带发出确认那一刻冻结的原目标上下文。核心只在当前目标仍是那一份、且账号
    上下文未变时，把它**转换**成同一个 :class:`UserGoal` 控制入口；否则忽略 ——
    后来的用户停止或账号变更都不受这份旧确认影响。真实人脸流程属于后续阶段。
    """

    goal_revision: int
    uid: int
    room_id: int


# ---------------- 2. 本轮结果 ----------------

@dataclass(frozen=True)
class PlatformResult(Event):
    """一次平台操作（query / stop / start）的结果。

    ``operation_id`` 由执行层从冻结请求里原样带回；``room_id``/``run_id`` 是
    本地冻结请求的主体标记（**不要求**平台返回本地身份）。

    ``status`` 只在 ``kind == query`` 时有意义；``area_id`` 是平台同时报告的
    房间当前分区，缺/非法一律按 ``unknown`` 处理，不作为收件门。
    """

    operation_id: str
    kind: str
    run_id: Optional[str] = None
    room_id: Optional[int] = None
    outcome: str = OUTCOME_UNKNOWN
    status: str = STATUS_UNKNOWN
    area_id: Optional[int] = None
    detail: str = ''


@dataclass(frozen=True)
class SelectionResult(Event):
    """选任务 / 确定单次参数的取数结果。

    只回答"这一场播什么"。推流方式（FFmpeg/OBS）是用户控制意图的参数，
    不由任务记录决定，因此这里没有 ``push_source``。

    ``settlement_guard`` 是该任务在**这一次读取的同一一致性区间**里冻结的人工
    进度代数（不透明并发凭据）。取数端口读不到（旧快照 / 单次模式）时为 ``None``，
    核心照原样保存；提交时**不会**临时补当前值。
    """

    operation_id: str
    found: bool
    task_id: Optional[int] = None
    zone_name: str = ''
    area_id: Optional[int] = None
    target_seconds: float = 0.0
    business_day: str = ''
    reason: str = ''
    settlement_guard: Optional[int] = None
    source_revision: Optional[int] = None
    terminal: bool = False


@dataclass(frozen=True)
class StartReservationResult(Event):
    """真实任务行在 Start 前无法按 Run 身份预留的 actor 事实。"""

    operation_id: str
    run_id: str
    reason: str
    named_ticket: bool = False


@dataclass(frozen=True)
class TaskAreaMarkResult(Event):
    """把“分区已不存在/下架”的任务移出不可完成区的执行回执。"""

    operation_id: str
    ok: bool = False
    reason: str = ''


@dataclass(frozen=True)
class LedgerResult(Event):
    """完成账本提交回执。

    - ``already`` = 该 (task_id, business_day) 已记过（**不是**本次新计入）；
    - ``terminal`` = 这笔完成**明确不再应用**（人工已改过该任务的进度、身份已变、
      或旧快照缺凭据）。它是终态：Core 保存这条回执后继续既有收尾，不无限重试。
      它不等于"已计入"，回执内容必须能被后续阶段区分出来。
    """

    operation_id: str
    ok: bool = True
    already: bool = False
    receipt: str = ''
    detail: str = ''
    terminal: bool = False
    reason: str = ''
    #: 账本端口在**同一次事务回执**里给出的累计进度（只用于已确认事实的通知，
    #: 不参与任何决策）。旧端口/替身不给时保持 ``None``，通知如实写"未取得"。
    days_done: Optional[int] = None
    all_done: Optional[bool] = None


@dataclass(frozen=True)
class PusherReport(Event):
    """媒体端口报告的**窄事实**：本程序那一路上推流的实际状态。

    它不是第二个房间决策入口：只用来（a）确认自有归属是否已真正回收、
    （b）在媒体失效时合并登记一次调和。媒体输出**从不**参与业务计时。
    """

    run_id: str
    alive: bool
    outputting: bool = False


@dataclass(frozen=True)
class PusherInventory(Event):
    """执行端口报告的**本实例自有**存活推流对象清单（启动/对齐时的窄库存输入）。

    归属事实由**执行边界**给出 —— 它只列本实例自己创建、仍然存活的那几路，
    因此不等同于"还保留着同一个业务 Run"：跨日或停止后重启，业务进度可以照常
    丢弃，物理对象的收尾能力不会随之消失。空清单 = 本实例没有自有对象，
    用户自己的 OBS / 别的实例的对象都不在可信范围里。

    它不携带 PID（真实进程识别属于阶段 3），也不落盘。
    """

    owned: tuple = ()


# ---------------- 3. 预约与唤醒 ----------------

@dataclass(frozen=True)
class Wake(Event):
    """到点的预约。只标记需要推进，**不**直接开关房。"""

    key: str
    kind: str
    run_id: Optional[str] = None


@dataclass(frozen=True)
class Timeout(Event):
    """本轮操作预算到期。归约为一次 ``unknown`` 结果，不是平台撤销请求。"""

    operation_id: str


@dataclass(frozen=True)
class TasksChanged(Event):
    """任务库变化 / 业务日变化，用于唤醒"运行中但暂无场次"的等待。

    它不唤醒用户停止、人脸或明确拒绝 —— 那些必须由用户重新开播。
    """

    business_day: str = ''
    reason: str = 'changed'


@dataclass(frozen=True)
class Bootstrap(Event):
    """进程启动/状态重载后进入唯一推进点。"""

    reason: str = 'startup'
