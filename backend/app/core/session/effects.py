"""唯一决策点输出的效果：执行层在**状态锁之外**逐个完成的外部动作。

核心自身不做网络、不启进程、不发通知；它只把决定落盘，然后把效果交给执行层。
因此网络等待、进程 join、SMTP 永远不会发生在 JSON 写锁里，离线模拟只需替换执行层。

平台动作（Start / Stop / Query / Select / Settle）共用**一个**在途操作槽，
每个都带唯一 ``op_id`` 与冻结请求；执行层把 ``op_id`` 原样带回结果即可，
不要求平台返回本地身份。媒体动作（Sync / Reclaim）是**独立端口**，
不占用操作槽，也不能让核心停住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class Effect:
    """效果基类。"""


# ---------------- 平台动作（占用唯一操作槽） ----------------

@dataclass(frozen=True)
class StartPlatform(Effect):
    """请求平台开播。只在**本次已确认关房**之后发出。"""

    op_id: str
    run_id: str
    room_id: int
    area_id: Optional[int] = None
    task_id: Optional[int] = None
    zone_name: str = ''
    business_day: str = ''
    settlement_guard: Optional[int] = None
    # 仅首次仍由该指定选择建立的 Run 携带用户票据；resume/auto 留空。
    expected_revision: Optional[int] = None
    ticket_business_date: Optional[str] = None


@dataclass(frozen=True)
class StopPlatform(Effect):
    """请求平台下播。受理不等于已关，随后必须查询确认。"""

    op_id: str
    run_id: Optional[str]
    room_id: int


@dataclass(frozen=True)
class QueryPlatform(Effect):
    """查询房间状态（关房核对、恢复核对、低频观察共用同一个动作）。"""

    op_id: str
    run_id: Optional[str]
    room_id: int
    step: str = ''


@dataclass(frozen=True)
class SelectTask(Effect):
    """按当前业务日与用户选择取下一场的目标。"""

    op_id: str
    revision: int
    mode: str
    selection: Dict[str, Any] = field(default_factory=dict)
    business_day: str = ''


@dataclass(frozen=True)
class SettleTask(Effect):
    """在账本里为 (task_id, business_day) 幂等记一次完成。

    ``completed_at`` 是 End 形成时冻结的**达标确认时间**（可空 = 未知），
    ``settlement_guard`` 是选取任务时冻结的人工进度代数。两者都只是原样转交
    给账本端口的事实，执行层不得在提交时补值。
    """

    op_id: str
    run_id: str
    task_id: Optional[int]
    business_day: str
    elapsed_seconds: float
    target_seconds: float = 0.0
    completed_at: Optional[str] = None
    settlement_guard: Optional[int] = None


@dataclass(frozen=True)
class MarkTaskAreaUnavailable(Effect):
    """把平台判定“分区已下架”的任务按负优先度规则不可完成移出。

    数据端口执行（保留任务记录）；它占用同一个在途操作槽，因此不会与关房/
    开播请求并发。
    """

    op_id: str
    run_id: str
    task_id: Optional[int]
    zone_name: str = ''
    business_day: str = ''
    reason: str = 'area_unavailable'


# ---------------- 媒体端口（不占用操作槽） ----------------

@dataclass(frozen=True)
class SyncPusher(Effect):
    """本程序希望这个房间有一路**自有**推流。

    只在 ``push_source == ffmpeg`` 时发出；OBS 模式核心不产生任何本地进程动作。
    """

    run_id: str
    zone_name: str


@dataclass(frozen=True)
class ReclaimPusher(Effect):
    """精确回收本程序自己的推流进程（按进程组，不按进程名全局杀）。"""

    run_id: str
    reason: str = ''


# ---------------- 对外发布 ----------------

@dataclass(frozen=True)
class PublishState(Effect):
    """把最新快照交给 UI。核心只发布，不改 view。"""

    snapshot: Dict[str, Any] = field(default_factory=dict)


# ---------------- 已确认事实（窄通知出口） ----------------

#: ``ConfirmedFact.kind`` 的取值：**已确认**的转换，不是 latest-only 快照推断。
FACT_SESSION_OPEN = 'session_open'        # 进入在播 / 等待 / 被拒 的一次转换
FACT_RUN_COMPLETED = 'run_completed'      # 一份已持久 End 的账本回执已收口
FACT_RUN_ENDED = 'run_ended'              # 一份不结算的结束决定（午夜/被替换）
FACT_SESSION_STOPPED = 'session_stopped'  # 平台已确认关房且用户目标为停止

#: ``ConfirmedFact.result`` 的取值：这一条转换的**实际**结果。
FACT_RESULT_STARTED = 'started'
FACT_RESULT_RESUMED = 'resumed'
FACT_RESULT_FAILED = 'failed'
FACT_RESULT_FACE_VERIFY = 'face_verify'
FACT_RESULT_REJECTED = 'rejected'
FACT_RESULT_NONE = 'none'
FACT_RESULT_CREDITED = 'credited'
FACT_RESULT_ALREADY = 'already'
FACT_RESULT_TERMINAL = 'terminal'
FACT_RESULT_SETTLE_FAILED = 'settle_failed'


@dataclass(frozen=True)
class ConfirmedFact(Effect):
    """**一次已确认转换**的不可变事实承载（窄通知出口，不是事件总线）。

    它只承载"已经发生、且已被平台或账本确认"的那一条事实，全部身份与时间在
    形成点**冻结**：投递时不得回读当前账号/当前 Run，也不得用投递时刻顶替事件
    时刻。它不参与任何决策，也不是第二份运行状态：核心把它追加到本轮效果末端，
    执行层原样交给既有异步通知入口。

    ``happened_at``：事实成立那一刻的业务时钟墙钟（ISO，秒）。
    ``completed_at``：取已冻结 End 的达标确认时间，绝不用晚到的提交时刻顶替。
    """

    kind: str
    run_id: str = ''
    uid: int = 0
    room_id: int = 0
    account_name: str = ''
    zone_name: str = ''
    business_day: str = ''
    task_id: Optional[int] = None
    #: 事实形成那一刻的用户目标版本：人工确认必须带着它，旧停止/新目标后自然作废。
    goal_revision: int = 0
    target_seconds: float = 0.0
    effective_seconds: float = 0.0
    happened_at: str = ''
    completed_at: Optional[str] = None
    result: str = ''
    reason: str = ''
    source: str = ''
    days_done: Optional[int] = None
    all_done: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind,
            'run_id': self.run_id,
            'uid': int(self.uid),
            'room_id': int(self.room_id),
            'account_name': self.account_name,
            'zone_name': self.zone_name,
            'business_day': self.business_day,
            'task_id': self.task_id,
            'goal_revision': int(self.goal_revision),
            'target_seconds': round(float(self.target_seconds), 3),
            'effective_seconds': round(float(self.effective_seconds), 3),
            'happened_at': self.happened_at,
            'completed_at': self.completed_at,
            'result': self.result,
            'reason': self.reason,
            'source': self.source,
            'days_done': self.days_done,
            'all_done': self.all_done,
        }
