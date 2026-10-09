"""唯一的**内存**时刻表。

它只回答两件事：

1. 到点了没有 —— :meth:`collect_due` 按时刻顺序取出到期项，交给核心当事件处理；
2. 现在能不能再发同一个动作 —— :meth:`ready` 是 ``issue()`` 的节流门，
   让"未知响应预约下一轮"不会在同一个事件里立刻自激重发。

一条 key 对应一条预约，key 由「步骤 ＋ 动作 ＋ 场次」派生，所以：

- 同一动作的下一轮重试会挡住立即重发（不需要在核心写迭代上限）；
- 旧场次的预约随 :meth:`cancel_for_run` 一起撤掉，不会结束新场。

时刻表不落盘：重启后由快照与时钟重建，因此没有"持久平台请求队列"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .events import OP_SETTLE, WAKE_TARGET, WAKE_WATCH, Event, Timeout, Wake

#: 操作预算到期的内部种类，不对外暴露为产品状态
KIND_TIMEOUT = 'timeout'


@dataclass
class _Entry:
    at_mono: float
    kind: str
    run_id: Optional[str] = None
    op_id: Optional[str] = None


class Scheduler:
    """同一时刻表管理到期、观察和一轮重试。"""

    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}

    # ---------- 登记与撤销 ----------

    def arm(self, key: str, at_mono: float, kind: str,
            run_id: Optional[str] = None) -> None:
        self._entries[key] = _Entry(float(at_mono), kind, run_id)

    def arm_operation_timeout(self, op_id: str, deadline_mono: float,
                              run_id: Optional[str] = None) -> None:
        self._entries['op:' + op_id] = _Entry(float(deadline_mono), KIND_TIMEOUT,
                                              run_id, op_id=op_id)

    def cancel(self, key: str) -> None:
        self._entries.pop(key, None)

    def cancel_for_run(self, run_id: Optional[str]) -> None:
        """旧场次的预约全部撤掉：旧预约不能结束新场。"""
        if run_id is None:
            return
        for key in [k for k, e in self._entries.items() if e.run_id == run_id]:
            self._entries.pop(key, None)

    def cancel_kind(self, kind: str) -> None:
        for key in [k for k, e in self._entries.items() if e.kind == kind]:
            self._entries.pop(key, None)

    # ---------- 查询 ----------

    def ready(self, key: str, now_mono: float) -> bool:
        """这一轮是否可以发；已有未到时刻的同类预约时返回假。"""
        entry = self._entries.get(key)
        return entry is None or entry.at_mono <= float(now_mono)

    def pending(self, key: str) -> bool:
        return key in self._entries

    def next_due(self) -> Optional[float]:
        if not self._entries:
            return None
        return min(e.at_mono for e in self._entries.values())

    def keys(self) -> List[str]:
        return sorted(self._entries)

    # ---------- 到点 ----------

    def collect_due(self, now_mono: float) -> List[Event]:
        """取出所有到期项（消费掉），按时刻与 key 排序，保证可复跑。"""
        due = [(e.at_mono, key, e) for key, e in self._entries.items()
               if e.at_mono <= float(now_mono)]
        due.sort(key=lambda item: (item[0], item[1]))
        events: List[Event] = []
        for _, key, entry in due:
            self._entries.pop(key, None)
            if entry.kind == KIND_TIMEOUT and entry.op_id:
                events.append(Timeout(operation_id=entry.op_id))
            else:
                events.append(Wake(key=key, kind=entry.kind, run_id=entry.run_id))
        return events

    # ---------- key 约定（核心与测试共用，避免散落的字符串拼写） ----------

    @staticmethod
    def key(step: str, action: str, run_id: Optional[str]) -> str:
        return f'{step}:{action}:{run_id or "-"}'

    def arm_target(self, run_id: str, at_mono: float) -> None:
        self.arm(self.key('live', WAKE_TARGET, run_id), at_mono, WAKE_TARGET, run_id)

    def arm_watch(self, run_id: str, at_mono: float) -> None:
        self.arm(self.key('live', WAKE_WATCH, run_id), at_mono, WAKE_WATCH, run_id)

    def cancel_target(self, run_id: Optional[str]) -> None:
        if run_id is None:
            return
        self.cancel(self.key('live', WAKE_TARGET, run_id))
        self.cancel(self.key('live', WAKE_WATCH, run_id))

    def ready_for_settle(self, run_id: Optional[str], now_mono: float) -> bool:
        return self.ready(self.key('settle', OP_SETTLE, run_id), now_mono)
