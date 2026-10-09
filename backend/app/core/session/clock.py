"""时钟抽象。

业务计时只认**单调钟**：它不受系统时间跳变影响，重启后由墙钟锚点重建。
墙钟只用于两件事：业务日（日期）与预约时刻（到期、午夜）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """只读时钟端口。"""

    def mono(self) -> float:
        """单调秒。跨进程不可比较，同一进程内严格单调。"""

    def wall(self) -> datetime:
        """本地墙钟。"""


class SystemClock:
    """生产时钟：直接转发给 time / datetime。"""

    __slots__ = ()

    def mono(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now()


class VirtualClock:
    """虚拟时钟：墙钟与单调钟各自可独立推进。

    - :meth:`advance` 同时推进两者，模拟"时间流逝"。
    - :meth:`skew_wall` 只动墙钟，模拟系统时间跳变（NTP/手动改表）。
    - :meth:`restart` 把单调钟重置到新的起点，模拟进程重启（墙钟不动）。

    测试不得使用真实 ``sleep``。
    """

    def __init__(self, wall_start: datetime, mono_start: float = 1_000.0):
        self._wall = wall_start
        self._mono = float(mono_start)
        self._mono_start = float(mono_start)

    def mono(self) -> float:
        return self._mono

    def wall(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError('虚拟时钟只能向前推进')
        self._wall = self._wall + timedelta(seconds=seconds)
        self._mono += float(seconds)

    def skew_wall(self, seconds: float) -> None:
        """只跳墙钟（模拟系统时间校正），单调钟不受影响。"""
        self._wall = self._wall + timedelta(seconds=seconds)

    def restart(self, mono_start: float | None = None) -> None:
        """模拟进程重启：单调钟回到新起点，墙钟保持当前值。"""
        self._mono = (float(mono_start) if mono_start is not None
                      else (self._mono_start + 10_000.0))
