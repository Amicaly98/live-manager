"""阶段 3A 执行层包：事件执行器 ＋ 有界平台 HTTP 适配 ＋ 真实数据端口。

规范与结构见 ``docs/plans/live-session-core-reset-20260930/``：
``PHASE3A-DESIGN.md``（本包固定结构）、``PHASE3-PLAN.md``、``PHASE3-ACCEPTANCE.md``。

- ``runtime``  —— 唯一调用 ``Core.ingest`` 的 actor ＋ 固定工作者 ＋ 幂等 close
- ``platform`` —— 一轮请求、一份预算、三类结果（ok / rejected / unknown）
- ``tasks``    —— 真实 ``select_for_run`` / ``reset_daily_flags`` / 账本适配器
- ``bounded_transport`` —— 一轮一份**绝对截止**的物理执行体，覆盖解析／连接／
  TLS 握手／发送／读取／解析（由平台端口注入 ``BilibiliApi`` 的单轮策略）
- ``media_port`` / ``media_os`` / ``media_playlist`` —— 阶段 3B 的**真实媒体端口**：
  本实例自有 FFmpeg 子进程的唯一一份资源权威、OS 出生身份/Job Object 归属、
  本地命令构建（``app.core.media_format`` 提供与旧控制器同一份的纯媒体规则）

**仍未接入任何生产入口**：本包只被 ``backend/tests/session_runtime/`` 引用。
隔离组装入口属于 3C；API/前端/邮件与生产生命周期属于阶段 4。
"""

from .bounded_transport import (
    BoundedHttpSender,
    DeadlineExceeded,
    orphan_resolvers,
    resolve_within,
)
from .media_os import (
    DEFAULT_TERMINATE_TIMEOUT,
    JobQueryError,
    LivenessUnknown,
    OsOps,
    ProcessIdentity,
    ProcessStats,
    RetainedChild,
    SpawnUnavailable,
    SpawnUnreclaimed,
    WinChild,
    WindowsOsOps,
    current_deadline,
    current_process_stats,
    deadline_scope,
    default_os_ops,
    identity_is_conclusive,
)
from .media_playlist import (
    NULL_OUTPUT,
    LocalCommandBuilder,
    MediaCommand,
    NoCompatibleVideos,
    NoVideosAvailable,
)
from .media_port import (
    InventoryReport,
    MediaCloseReport,
    MediaInstance,
    MediaPort,
    MediaRecordStore,
    OwnedMedia,
    RecordEntry,
    ResourceVerificationError,
    UnsupportedMediaEffect,
)
from .platform import (
    FACE_VERIFY_CODES,
    PlatformHttpPort,
    RemainingBudget,
    classify_outcome,
    parse_live_status,
    parse_observation,
    parse_positive_area_id,
)
from .runtime import (
    UNSUPPORTED_NAMED_TICKET,
    CloseReport,
    Job,
    LatestView,
    Reply,
    Runtime,
    UnexpectedEffect,
    selection_kind,
)
from .tasks import TaskDataPort, UnsupportedNamedTicket, parse_business_day

__all__ = [
    'Runtime', 'Job', 'Reply', 'CloseReport', 'LatestView', 'UnexpectedEffect',
    'UNSUPPORTED_NAMED_TICKET', 'selection_kind',
    'PlatformHttpPort', 'RemainingBudget', 'FACE_VERIFY_CODES',
    'classify_outcome', 'parse_observation', 'parse_live_status',
    'parse_positive_area_id',
    'TaskDataPort', 'UnsupportedNamedTicket', 'parse_business_day',
    'BoundedHttpSender', 'DeadlineExceeded', 'resolve_within',
    'orphan_resolvers',
    'MediaPort', 'MediaInstance', 'MediaCloseReport', 'InventoryReport',
    'OwnedMedia', 'MediaRecordStore', 'RecordEntry',
    'UnsupportedMediaEffect', 'ResourceVerificationError',
    'LocalCommandBuilder', 'MediaCommand', 'NoVideosAvailable',
    'NoCompatibleVideos', 'NULL_OUTPUT',
    'OsOps', 'WindowsOsOps', 'ProcessIdentity', 'ProcessStats',
    'default_os_ops', 'current_process_stats', 'identity_is_conclusive',
    'WinChild', 'deadline_scope', 'current_deadline', 'DEFAULT_TERMINATE_TIMEOUT',
    'SpawnUnavailable', 'JobQueryError', 'SpawnUnreclaimed', 'LivenessUnknown',
    'RetainedChild',
]
