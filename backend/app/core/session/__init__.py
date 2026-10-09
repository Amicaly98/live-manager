"""直播场次核心（server 版，2026-09-30 从空白重写决策层）。

规范见 ``docs/plans/live-session-core-reset-20260930/PLAN.md``，
字段/Step/推进顺序见同目录 ``PHASE1A-DESIGN.md``。

- ``model``     —— Goal / Run / Step / EndDecision / PlatformClock / Snapshot
- ``events``    —— 进入唯一决策点的输入与回执
- ``effects``   —— 决策点输出、由执行层在**锁外**完成的外部动作
- ``scheduler`` —— 唯一的内存时刻表（到期、观察、一轮重试）
- ``store``     —— 快照的原子持久化与唯一读取判定
- ``core``      —— ``Core.ingest(event) -> (Result, effects)``（唯一决策点）
- ``ledger``    —— 阶段 2：已持久 End → 真实 SQLite 完成账本的窄适配器
- ``migrate``   —— 阶段 2：旧状态/旧标记的一次性**副本**迁移（不改流程）

**仍未接入任何生产入口**：本包只被 ``backend/tests/`` 引用。``ledger`` 与
``migrate`` 也不在导入时引用 ``TaskManager``/依赖：账本端口由调用方注入。

"""

from .clock import Clock, SystemClock, VirtualClock
from .core import AccountContext, Core, Operation, RequiredSaveFailed, Tuning
from .events import (
    Ignored,
    LedgerResult,
    PlatformResult,
    Rejected,
    SelectionResult,
    TasksChanged,
    Timeout,
    UserGoal,
    Wake,
)
from .model import (
    END_COMPLETED,
    END_MIDNIGHT,
    END_REPLACED,
    MODE_SINGLE,
    MODE_TASK,
    PUSH_FFMPEG,
    PUSH_OBS,
    SELECT_AUTO,
    SELECT_SINGLE,
    SELECT_TASK,
    START_NEW,
    START_RESUME,
    WANT_RUNNING,
    WANT_STOPPED,
    EndDecision,
    Goal,
    PlatformClock,
    Run,
    Selection,
    Snapshot,
)
from .scheduler import Scheduler
from .store import SessionStore

__all__ = [
    'Clock', 'SystemClock', 'VirtualClock',
    'Core', 'AccountContext', 'Tuning', 'Operation', 'RequiredSaveFailed',
    'Scheduler', 'SessionStore',
    'Goal', 'Run', 'Selection', 'Snapshot', 'EndDecision', 'PlatformClock',
    'UserGoal', 'PlatformResult', 'SelectionResult', 'LedgerResult',
    'Wake', 'Timeout', 'TasksChanged', 'Ignored', 'Rejected',
    'WANT_RUNNING', 'WANT_STOPPED',
    'MODE_TASK', 'MODE_SINGLE',
    'START_NEW', 'START_RESUME',
    'SELECT_AUTO', 'SELECT_TASK', 'SELECT_SINGLE',
    'PUSH_FFMPEG', 'PUSH_OBS',
    'END_COMPLETED', 'END_MIDNIGHT', 'END_REPLACED',
]
