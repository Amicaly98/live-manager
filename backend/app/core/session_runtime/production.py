"""阶段 4B：**生产默认组装**。

把已验收的 Core / Runtime / 媒体端口 / 任务端口接成真正的默认入口，并只保留
一份运行意图权威：

- 一份 ``TaskManager``（``reset_scheduler=False``，应用生命周期，最后关闭）；
- 一份 ``EmailSender`` 与一份 :class:`~.facts.NotificationAdapter`；
- 一份应用时钟；
- 一个 ``ControlService``，持有 **0 或 1** 个固定账号 ``RuntimeInstance``。

**本模块不构造旧 ``LiveController``**，也不启动旧日切/推流/监控线程；``BilibiliApi``
只作为纯能力复用（QR/cookie/签名/房间 ID/分区/推流地址）。默认路径上每个账号实例
的 ``RuntimeInstance`` 都借用**应用**的 TaskManager（``owns_task_manager=False``）。

隔离边界（平台地址、通知端口、媒体输出、时钟、账号）只能在 owner 构造**之前**
显式注入，默认值全部是生产语义；注入本身会记录在 owner 报告里作为证据。
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ..session.core import AccountContext, Core, Tuning
from ..session.events import Accepted, UserGoal
from ..session.migrate import (
    DB_FILE,
    FACE_MARKER,
    HANDOVER_FILE,
    STATUS_ALREADY_NEW,
    STATUS_MIGRATED,
    STATE_FILE,
    STOP_MARKER,
    MigrationRefused,
    migrate_legacy_state,
)
from ..session.model import (
    MODE_TASK,
    START_NEW,
    STOP_USER,
    WANT_RUNNING,
    WANT_STOPPED,
    business_day_of,
)
from ..session.store import VERDICT_FIRST_START, VERDICT_TRUSTED, SessionStore
from .accounts import (
    AccountAcquisitionError,
    AccountError,
    AccountBusy,
    open_account_binding,
)
from .factory import InstanceConstructionError, RuntimeInstance
from .facts import NotificationAdapter
from .media_playlist import LocalCommandBuilder
from .media_port import (
    DEFAULT_STARTUP_OBSERVE, DEFAULT_WATCH_INTERVAL, LEGACY_MEDIA_INSTANCE_ID,
    MediaInstance, MediaPort,
)
from .platform import PlatformHttpPort, RemainingBudget
from .bounded_transport import BoundedHttpSender
from .runtime import Runtime
from .tasks import TaskDataPort

logger = logging.getLogger(__name__)

#: 未加自动恢复标记：进程被有意停止时写下的"下次启动不要自动恢复"要求。
NO_AUTO_RESUME_MARKER = '.no_auto_resume'

#: 接管前原字节的另存目录（证据，不是第二份运行权威，运行期从不读回）。
LEGACY_BACKUP_DIR = 'legacy-backup'

#: 生产账号实例的物理标识前缀（媒体归属与清理路径用）。**不再**作为实例身份本身：
#: 旧方案所有部署共用该常量，两个实例互相拿错记录时无法区分（返修 R1）。
#: 旧名的归属语义唯一来源在 ``media_port.LEGACY_MEDIA_INSTANCE_ID`` 的共用分类：
#: 旧 ``prod`` 记录按**未核验**处理（不认领、不授权），不是"确证外来"（返修 R4）。
PRODUCTION_INSTANCE_ID = LEGACY_MEDIA_INSTANCE_ID


def _media_instance_id(data_dir: Path) -> str:
    """从**媒体实例目录**导出稳定、可区分的实例物理标识（返修 R1）。

    归属锚点是媒体实例目录本身（不是账号 UID，也不是每次启动随机生成）：

    - 同一目录每次组装得到同一标识 —— 端口重建/重启后，旧记录里的实例名仍与本
      实例一致，资源线索不丢；
    - 不同目录得到不同标识 —— 两个部署实例互相拿错记录时可**确证**"不是本实例"，
      不认领、不授予结束权限；
    - 目录名只并入编号方便人读，唯一性由解析后绝对路径（normcase）的摘要承担。

    旧方案常量 ``prod``（所有实例共用）无法区分部署，已停止用于新记录。目录里
    遗留的旧 ``prod`` 记录的归属**未核验**（名字既不能证明属于本实例，也不能证明
    属于别的实例）：不认领、不授予结束权限、保留原字节；确证退出/身份复用后沿
    既有出口清理——分类唯一来源在 ``media_port._declared_instance_class``，勘误与
    登记见 PHASE5B-REWORK-R4-REPORT（返修 R4）。
    """
    resolved = os.path.normcase(str(Path(data_dir).resolve()))
    digest = hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:12]
    name = ''.join(ch if (ch.isalnum() or ch in '-_.') else '_'
                   for ch in (Path(resolved).name or 'instance'))
    return f'{PRODUCTION_INSTANCE_ID}-{name}-{digest}'

#: 生产推流地址解析的默认预算（秒）：取址与媒体构建共用同一份剩余时间。
PUSH_TARGET_BUDGET_SECONDS = 10.0


class MediaTargetUnavailable(RuntimeError):
    """冻结账号/房间没有可用的推流地址：**不**回落本地文件或空设备。"""


class LegacyRunAreaUnresolved(RuntimeError):
    """旧档留下了一个要复播的原场，却无法从可靠分区缓存解析出它的真实分区 ID。

    阶段 2 的旧格式没有分区 ID（新格式才有），解释器如实留空。默认入口在激活接缝
    补一次解析；解析不出来时**明确拒绝激活**这一份账号实例——绝不向平台发一个
    空分区目标，也不伪造一个 ID。
    """

    def __init__(self, zone_name: str) -> None:
        super().__init__(
            f'原场分区「{zone_name or "未知"}」无法从可靠分区缓存解析出 ID：'
            '拒绝以空目标复播')
        self.zone_name = zone_name


class BoundaryConfigurationError(RuntimeError):
    """隔离边界注入不合法，或注入发生在 owner 构造之后。"""


# ==================== 隔离边界（测试可显式注入；默认全是生产语义） ====================

@dataclass
class BoundaryOverrides:
    """只替换**物理外部 I/O**；业务对象（Core/Runtime/TaskManager/账本）不在此列。"""

    endpoint_base: Optional[str] = None       # None = 真实 B 站端点
    media_output: Optional[str] = None        # None = 按冻结账号/房间取真实推流地址
    media_builder_factory: Optional[Callable[..., Any]] = None
    notifications: Any = None                 # None = 真实 EmailSender
    clock: Any = None                         # None = SystemClock
    account: Any = None                       # 显式账号上下文（核验替身用）
    credentials: Optional[Dict[str, str]] = None
    video_base: Optional[str] = None
    close_budget_seconds: float = 5.0
    op_budget_seconds: float = 30.0
    retry_delay_seconds: float = 30.0
    watch_interval_seconds: float = 30.0
    #: ``configure_boundaries`` **真的**收到的字段名（不是第二份配置权威，只是
    #: "这一次边界是否被显式注入"的事实）。默认组装据此决定能不能用已保存的设置
    #: 覆盖 ``watch_interval_seconds``：显式注入永远优先。
    explicit_fields: frozenset = frozenset()


_BOUNDARIES: Optional[BoundaryOverrides] = None
_BOUNDARIES_FROZEN = False


def configure_boundaries(**kwargs: Any) -> BoundaryOverrides:
    """在 owner 构造前显式注入隔离边界；只能调用一次。"""
    global _BOUNDARIES
    if _BOUNDARIES_FROZEN:
        raise BoundaryConfigurationError('边界已在 owner 构造后冻结，不能再注入')
    if _BOUNDARIES is not None:
        raise BoundaryConfigurationError('隔离边界只能注入一次')
    unknown = set(kwargs).difference(BoundaryOverrides.__dataclass_fields__)
    if unknown:
        raise BoundaryConfigurationError(f'未知的边界字段：{sorted(unknown)}')
    _BOUNDARIES = BoundaryOverrides(**kwargs)
    _BOUNDARIES.explicit_fields = frozenset(kwargs)
    _validate_boundaries(_BOUNDARIES)
    return _BOUNDARIES


def current_boundaries() -> BoundaryOverrides:
    return _BOUNDARIES if _BOUNDARIES is not None else BoundaryOverrides()


def reset_boundaries_for_tests() -> None:
    """仅供同一进程内多次构造 owner 的测试夹具使用。"""
    global _BOUNDARIES, _BOUNDARIES_FROZEN
    _BOUNDARIES = None
    _BOUNDARIES_FROZEN = False


def _validate_boundaries(boundaries: BoundaryOverrides) -> None:
    endpoint = boundaries.endpoint_base
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.port is None:
            raise BoundaryConfigurationError('endpoint_base 必须是带端口的 http URL')
        host = parsed.hostname.strip('[]').lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == 'localhost'
        if not loopback or parsed.username or parsed.password:
            raise BoundaryConfigurationError('注入的 endpoint_base 必须是 loopback')
    if boundaries.media_builder_factory is not None and boundaries.media_output:
        raise BoundaryConfigurationError('media_output 与 media_builder_factory 互斥')


def _freeze_boundaries() -> BoundaryOverrides:
    global _BOUNDARIES_FROZEN
    boundaries = current_boundaries()
    _BOUNDARIES_FROZEN = True
    return boundaries


# ==================== 生产推流地址 ====================

def read_cached_push_url(cache_path: Path, room_id: int) -> str:
    """从本实例的 ``rtmp_cache.json`` 取**冻结房间**的推流地址（同旧口径拼接）。"""
    try:
        import json
        with open(cache_path, 'r', encoding='utf-8') as handle:
            cache = json.load(handle)
    except Exception:                                  # noqa: BLE001
        return ''
    entry = cache.get(str(room_id)) if isinstance(cache, dict) else None
    if not isinstance(entry, dict):
        return ''
    addr = str(entry.get('rtmp_addr') or '').strip()
    code = str(entry.get('rtmp_code') or '').strip()
    if not addr or not code:
        return ''
    return f"{addr.rstrip('/')}/{code}"


def resolve_push_target(*, api, room_id: int, cache_path: Path, budget,
                        endpoint_base: Optional[str] = None,
                        sender=None) -> str:
    """按**冻结房间**取推流地址：先读本实例缓存，未命中才在**本轮预算内**取址。

    缺链接一律抛 :class:`MediaTargetUnavailable` —— 绝不静默回落 ``local_out.flv``
    或空设备，也绝不写任何地址/推流码进日志或证据。
    """
    if int(room_id) <= 0:
        raise MediaTargetUnavailable('冻结账号没有可用房间，无法取推流地址')
    cached = read_cached_push_url(cache_path, int(room_id))
    if cached:
        return cached
    url = ("https://api.live.bilibili.com/room/v1/Room/get_info"
           f"?room_id={int(room_id)}")
    try:
        with api.bounded_attempt(budget, endpoint_base, sender):
            ok, payload = api._req('GET', url)          # noqa: SLF001 - 复用单轮接缝
    except MediaTargetUnavailable:
        raise
    except Exception as exc:                            # noqa: BLE001
        raise MediaTargetUnavailable(f'取推流地址失败：{type(exc).__name__}') from exc
    if not ok or not isinstance(payload, dict):
        raise MediaTargetUnavailable('取推流地址未取得有效回包')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise MediaTargetUnavailable('取推流地址回包缺少房间数据')
    rtmp = data.get('rtmp') if isinstance(data.get('rtmp'), dict) else {}
    addr = str(rtmp.get('addr') or data.get('rtmp_addr') or '').strip()
    code = str(rtmp.get('code') or data.get('rtmp_code') or '').strip()
    if not addr or not code:
        raise MediaTargetUnavailable('取推流地址回包缺少地址或推流码')
    return f"{addr.rstrip('/')}/{code}"


class ProductionPushCommandBuilder(LocalCommandBuilder):
    """把冻结账号/房间的**真实**推流地址绑定到本轮命令。

    它在 :meth:`build` 里先按本轮剩余预算解析目标，再交给既有构建逻辑；解析失败
    时**不**设置输出，因此既有逻辑不会拿到一个"默认本地文件"的落点，而是让本轮
    直接报媒体失败。
    """

    def __init__(self, *, output_resolver: Callable[[float, Any], str],
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._resolver = output_resolver
        self.last_target_error = ''

    def build(self, zone_name: str, deadline: float, clock=None):
        try:
            self._output = self._resolver(deadline, clock)
            self.last_target_error = ''
        except MediaTargetUnavailable as exc:
            self.last_target_error = str(exc)
            raise
        return super().build(zone_name, deadline, clock)


# ==================== 启动数据接管 ====================

@dataclass
class DataTakeover:
    """启动数据接管的**唯一**结论。它是报告，不是第二份运行权威。"""

    status: str                       # trusted / first_start / migrated / refused / account_required
    detail: str = ''
    reason: str = ''
    steps: List[str] = field(default_factory=list)
    report: Optional[Dict[str, Any]] = None

    @property
    def state_usable(self) -> bool:
        return self.status in ('trusted', 'first_start', 'migrated')

    @property
    def control_available(self) -> bool:
        return self.state_usable

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'detail': self.detail,
            'reason': self.reason,
            'steps': list(self.steps),
            'report': self.report,
        }


def _copy_state_bytes(source: Path, target_dir: Path) -> List[str]:
    """把状态文件、最后可用副本与**全部兼容标记**逐字节复制到独立暂存目录。

    阶段 2 的解释器按**同一份输入集**判定来源与停止意图。少拷一个标记，它就会把
    "人工继续的停止"（``.face_verify_pending``）、"旧进度已放弃"
    （``.progress_handover.json``）或"外部停止"（``.no_auto_resume``）重新解释成
    一次自动恢复：这不是缺省值问题，而是把错误意图真正落盘。
    """
    copied: List[str] = []
    for name in (STATE_FILE, STATE_FILE + '.bak', HANDOVER_FILE,
                 FACE_MARKER, STOP_MARKER):
        part = source / name
        if part.is_file():
            shutil.copy2(part, target_dir / name)
            copied.append(name)
    return copied


def _consistent_db_copy(source_db: Path, target_dir: Path) -> bool:
    """SQLite 一致性副本（``backup`` API）：WAL 里已 COMMIT 的内容也带上。"""
    if not source_db.is_file():
        return False
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / DB_FILE
    src = dst = None
    try:
        src = sqlite3.connect(str(source_db), timeout=30.0)
        dst = sqlite3.connect(str(target))
        src.backup(dst)
        dst.commit()
    except sqlite3.Error as exc:
        raise MigrationRefused('source_db_unreadable',
                               f'运行库无法安全读取：{exc}') from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
    return True


def _preserve_originals(data_dir: Path, staging_src: Path) -> List[str]:
    """把接管前的**原字节**另存为证据（不是第二份运行权威，也不被读回）。

    只在"确实要转换"的分支调用；每次覆盖同一份，内容始终是转换前的源字节。
    """
    target = data_dir / LEGACY_BACKUP_DIR
    target.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    for name in (STATE_FILE, STATE_FILE + '.bak', HANDOVER_FILE,
                 FACE_MARKER, STOP_MARKER, DB_FILE):
        part = staging_src / name
        if part.is_file():
            shutil.copy2(part, target / name)
            saved.append(name)
    return saved


def _state_is_legacy_document(state_path: Path) -> bool:
    """源主档是不是**已识别**的合法旧 ``LiveState``。

    只有已识别的合法旧格式才需要登录上下文（核对旧进度归属）。主档坏掉 / 缺失 /
    新旧混合 / 无法识别时都不是"可转换的旧档"，此时要么采用可信的 ``.bak``，
    要么拒绝 —— 两者都不需要账号。
    """
    try:
        import json
        document = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    from ..session.migrate import detect_format
    return detect_format(document) == 'legacy_livestate'


def take_over_state(*, data_dir: Path, account: Optional[AccountContext],
                    settings: Optional[Dict[str, Any]] = None,
                    now: Optional[datetime] = None) -> DataTakeover:
    """在未开放写入口、未 Bootstrap 的条件下完成原地旧格式接管。

    顺序固定：**可信判定 → 一致性副本 → 独立候选迁移 → 账本/历史先落地 →
    新状态最后原子发布**。中断后重跑只补未完成的一步（每一步都幂等）。
    """
    data_dir = Path(data_dir)
    state_path = data_dir / STATE_FILE
    loaded = SessionStore(state_path).load()
    if loaded.status == VERDICT_TRUSTED and loaded.source != 'backup':
        return DataTakeover(status='trusted', detail='已有可信新快照（主档可信）')
    if loaded.status == 'first_start':
        return DataTakeover(status='first_start', detail='没有任何状态资料，按首次启动')

    # 有资料但没有一份**主档**可信：只在已识别合法旧格式时才允许转换。这里刻意
    # 不把"仅备份可信"当成已有权威——一份陈旧的新格式 ``.bak`` 很可能只是上一轮
    # 投影回退留下的影子，而主档才是**真正在跑**的那份合法旧档（复核 R7）。来源
    # 优先级交给阶段 2 的迁移解释器统一判定：合法旧主档显式迁移；主档坏了才退回
    # 可信新备份；两份都不可信则保留原字节并拒绝。
    if not _state_is_legacy_document(state_path):
        # 没有可转换的合法旧格式：只可能是"主档坏了但备份可信"（直接采用备份，
        # 不需要登录上下文）或"两份都坏"（保留原字节、明确拒绝）。
        if loaded.status == VERDICT_TRUSTED:
            return DataTakeover(status='trusted',
                                detail='主档不可用，采用可信的最后可用副本')
        return DataTakeover(status='refused', reason='untrusted_state',
                            detail=loaded.error or '状态资料存在但严格判定不可信；原字节未改动')

    if account is None or int(getattr(account, 'room_id', 0) or 0) <= 0:
        return DataTakeover(
            status='account_required',
            detail='旧状态接管需要登录上下文核对归属；未登录时保留原字节',
            reason='account_required')

    staging_src = Path(tempfile.mkdtemp(prefix='bls-4b-src-'))
    candidate = Path(tempfile.mkdtemp(prefix='bls-4b-cand-'))
    steps: List[str] = []
    try:
        copied = _copy_state_bytes(data_dir, staging_src)
        if not copied:
            return DataTakeover(status='refused', reason='no_source_state',
                                detail=f'没有可迁移的 {STATE_FILE}；原字节未改动')
        steps.append(f'副本已准备：{",".join(copied)}')
        _consistent_db_copy(data_dir / DB_FILE, staging_src)
        steps.append('运行库一致性副本已准备')
        preserved = _preserve_originals(data_dir, staging_src)
        if preserved:
            steps.append(f'原字节已另存：{",".join(preserved)}')

        try:
            report = migrate_legacy_state(
                source_dir=staging_src, target_dir=candidate,
                account={'uid': int(account.uid), 'room_id': int(account.room_id)},
                settings=settings, now=now)
        except MigrationRefused as refused:
            return DataTakeover(status='refused', reason=refused.reason,
                                detail=refused.detail or str(refused),
                                steps=steps)

        status = report.get('status')
        if status == STATUS_ALREADY_NEW:
            # 源本身（含仅备份可信）已是可信新格式：不改字节，交由严格读取使用。
            return DataTakeover(status='trusted', detail='迁移工具判定已是可信新格式',
                                steps=steps, report=report)
        if status != STATUS_MIGRATED:
            return DataTakeover(status='refused',
                                reason=report.get('reason') or status or 'refused',
                                detail=report.get('detail') or '迁移未产出可信候选',
                                steps=steps, report=report)

        # 账本/历史先：直接落在**运行库**上。两步都是"缺才补"，绝不把候选库整份
        # 覆盖回运行目录，因此不会丢掉源目录较新的 CRUD/进度。
        from ..session.migrate import _absorb_db_completions, _ensure_history_baseline
        if (data_dir / DB_FILE).is_file():
            _absorb_db_completions(data_dir / DB_FILE, report)
            steps.append('账本完成事实已落地（幂等）')
            _ensure_history_baseline(data_dir / DB_FILE, report)
            steps.append('历史基线已收录（幂等）')

        candidate_store = SessionStore(candidate / STATE_FILE)
        candidate_loaded = candidate_store.load()
        if candidate_loaded.status != VERDICT_TRUSTED:
            return DataTakeover(status='refused', reason='candidate_untrusted',
                                detail=candidate_loaded.error or '候选快照不可信',
                                steps=steps, report=report)
        # 新状态**最后**发布：写到一半中断时运行目录仍是原来的资料，重跑收敛。
        if not SessionStore(state_path).save(candidate_loaded.snapshot):
            return DataTakeover(status='refused', reason='publish_failed',
                                detail='新状态发布失败；原字节与账本已落地部分保留',
                                steps=steps, report=report)
        steps.append('新状态已原子发布')
        published = SessionStore(state_path).load()
        if published.status != VERDICT_TRUSTED:
            return DataTakeover(status='refused', reason='publish_verify_failed',
                                detail=published.error or '发布后严格读取不可信',
                                steps=steps, report=report)
        return DataTakeover(status='migrated', detail='原地接管完成',
                            steps=steps, report=report)
    finally:
        shutil.rmtree(staging_src, ignore_errors=True)
        shutil.rmtree(candidate, ignore_errors=True)


# ==================== 账号实例组装 ====================

def _resolve_video_base(boundaries: BoundaryOverrides) -> Path:
    if boundaries.video_base:
        return Path(boundaries.video_base)
    from .. import config as app_config
    base = app_config.VIDEO_BASE_PATH
    if base is None:
        raise BoundaryConfigurationError('VIDEO_BASE_PATH 未初始化（config.init 未执行）')
    return Path(base)


def _area_lookup_for(data_dir: Path) -> Callable[[str], Optional[int]]:
    """分区名 → area_id 的**纯能力**查询（复用运行时分区缓存，不构造旧控制器）。"""
    from .area_reader import load_area_index
    index = load_area_index(data_dir)
    return lambda zone: index.get(str(zone or '').strip())


def _bind_original_area(*, store, area_lookup) -> str:
    """给**旧档留下来**、缺分区 ID 的待复播原场绑定真实分区 ID。

    旧 ``LiveState`` 根本没有分区 ID，阶段 2 的解释器如实留空。默认入口在这里用本
    实例的可靠分区缓存解析一次，把结果写回已发布的新快照：原场身份、固定时长、任务
    归属一律不动，只补一个可核对的目标。

    只在**本 Core 从未选过这一场**（``selected_by_revision == 0``，只有迁移产物才
    会这样）时才做：本 Core 选出来的场次，分区由选取路径决定，不在这里改写。能解析
    就绑上（对"显式恢复"同样有意义）；解析不出来且这一场正带运行目标待复播时，抛
    :class:`LegacyRunAreaUnresolved` 明确拒绝激活 —— 绝不向平台发空目标、也不伪造 ID。
    """
    loaded = store.load()
    if loaded.status != VERDICT_TRUSTED or loaded.snapshot is None:
        return ''
    snapshot = loaded.snapshot
    run = snapshot.run
    if run is None or run.area_id is not None or run.end is not None:
        return ''
    if int(run.selected_by_revision or 0) != 0:
        return ''
    zone = str(run.zone_name or '').strip()
    area_id = area_lookup(zone) if zone else None
    if not isinstance(area_id, int) or isinstance(area_id, bool) or area_id <= 0:
        if str(snapshot.goal.wanted) == WANT_RUNNING:
            raise LegacyRunAreaUnresolved(zone)
        return ''
    run.area_id = int(area_id)
    if not store.save(snapshot):
        raise LegacyRunAreaUnresolved(zone)
    return f'原场分区「{zone}」已按可靠缓存绑定 area_id={int(area_id)}'


def build_account_instance(*, data_dir: Path, account: AccountContext,
                           credentials: Dict[str, str], clock, task_manager,
                           notifications_sink, boundaries: BoundaryOverrides,
                           shared_task_manager: bool = True,
                           account_binding: Any = None) -> RuntimeInstance:
    """构造**一个**固定账号实例；失败时释放已经取得的一切（真实清理报告）。

    ``account_binding`` 给出时（M0A 账号模式）使用**账号目录**里的 SessionStore，
    并把绑定附着在实例上 —— 因此保留/释放候选实例就等于保留/释放它的任务库与
    账号占用。``data_dir`` 仍然是**实例**目录：凭据、RTMP 缓存、媒体库存与临时件
    继续实例归属，不随账号搬运。
    """
    from ..live_controller import BilibiliApi
    # 推流来源的唯一权威在设置页（``app.api.settings``）；这里只把**结论读取口**
    # 注入 Core —— Core 不导入 API、不读 settings.json，也不建第二份配置权威。
    from app.api.settings import current_push_source

    data_dir = Path(data_dir)
    video_base = _resolve_video_base(boundaries)
    owned: Dict[str, Any] = {'media_instance': None, 'runtime': None,
                             'task_manager': task_manager}
    try:
        store = (account_binding.store if account_binding is not None
                 else SessionStore(data_dir / STATE_FILE))
        area_lookup = _area_lookup_for(data_dir)
        _bind_original_area(store=store, area_lookup=area_lookup)
        tuning = Tuning(op_budget_seconds=float(boundaries.op_budget_seconds),
                        retry_delay_seconds=float(boundaries.retry_delay_seconds),
                        watch_interval_seconds=float(boundaries.watch_interval_seconds),
                        close_check_delay_seconds=0.0)
        core = Core(store, clock, account, tuning,
                    push_source_provider=current_push_source)
        task_port = TaskDataPort(task_manager, store, clock, account,
                                 area_lookup=area_lookup)
        api = BilibiliApi(cookie_file=str(data_dir / 'bili_cookies.json'))
        platform_port = PlatformHttpPort(api, clock,
                                         endpoint_base=boundaries.endpoint_base)

        media_instance = MediaInstance(_media_instance_id(data_dir), data_dir)
        owned['media_instance'] = media_instance
        if boundaries.media_builder_factory is not None:
            builder = boundaries.media_builder_factory(
                media_dir=media_instance.media_dir, video_base=video_base,
                default_folder=video_base / 'default')
        else:
            sender = BoundedHttpSender()
            cache_path = data_dir / 'rtmp_cache.json'
            room_id = int(account.room_id)

            def resolver(deadline: float, _clock) -> str:
                budget = RemainingBudget(
                    deadline, clock if _clock is None else _clock)
                return resolve_push_target(
                    api=api, room_id=room_id, cache_path=cache_path,
                    budget=budget, endpoint_base=boundaries.endpoint_base,
                    sender=sender)

            builder = ProductionPushCommandBuilder(
                media_dir=media_instance.media_dir, video_base=video_base,
                default_folder=video_base / 'default',
                output_resolver=resolver,
                output=boundaries.media_output or '')
        media_port = MediaPort(
            media_instance, builder, attempt_budget_seconds=10.0,
            # 首次输出宽限与观察周期取**端口自己的**一对设计值（见
            # PHASE3B-DESIGN §3）：宽限 5 秒、采样 0.05 秒。生产组装此前把它们
            # 覆写成 0.5 / 5.0 —— 观察线程创建时读一次然后等 5 秒，而 Core 约每
            # 1 秒（``_MEDIA_RETRY_SECONDS``）调解一次，于是**已经输出**的对象
            # 会在首输出被采到之前被判定为没有输出并发起回收。宽限必须覆盖远不止
            # 一个采样周期，采样周期也必须细于调和节奏，观察才有机会看到首次输出。
            # 这两个常数**不是**用户的失败阈值，而是本机实测的工程预算。
            startup_observe_seconds=DEFAULT_STARTUP_OBSERVE,
            stall_window_seconds=30.0,
            # 推流重试要尽快（用户确认）：本机推流不做温和片段，其余预算留给强制
            # 结束与核验。
            gentle_slice_seconds=0.0,
            watch_interval_seconds=DEFAULT_WATCH_INTERVAL,
            clock=clock, name=f'{PRODUCTION_INSTANCE_ID}-media')

        runtime = Runtime(
            core, clock, platform_port=platform_port, task_port=task_port,
            media_port=media_port, credentials=lambda: dict(credentials),
            account=account, facts_sink=notifications_sink,
            close_budget_seconds=float(boundaries.close_budget_seconds),
            name=f'{PRODUCTION_INSTANCE_ID}-runtime')
        owned['runtime'] = runtime
        from app import dependencies
        return RuntimeInstance(
            data_dir=data_dir, video_base=video_base, account=account,
            runtime=runtime, media_port=media_port, media_instance=media_instance,
            task_manager=task_manager, task_port=task_port, core=core,
            store=store, platform_port=platform_port,
            notifications=notifications_sink, dependencies=dependencies,
            previous_dependencies=None, clock=clock,
            close_budget_seconds=float(boundaries.close_budget_seconds),
            owns_task_manager=not shared_task_manager,
            account_binding=account_binding)
    except BaseException as exc:
        report = None
        if owned['runtime'] is not None:
            try:
                report = owned['runtime'].close(
                    float(boundaries.close_budget_seconds)).to_dict()
            except Exception as cleanup_exc:              # noqa: BLE001
                report = {'error': f'{type(cleanup_exc).__name__}: {cleanup_exc}'}
        media_instance = owned['media_instance']
        if media_instance is not None:
            try:
                media_instance.close()
            except Exception:                             # noqa: BLE001
                pass
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise InstanceConstructionError(exc, {'runtime': report,
                                              'shared_task_manager': True}) from exc


# ==================== 应用 owner ====================

class ApplicationOwner:
    """应用数据的唯一所有者：任务库、邮件、通知出口、控制服务与应用时钟。

    ``close`` 幂等、有预算，只释放资源：**不**调用用户 Stop/logout，**不**追房，
    **不**把 running 改成 stopped，也**不**删除任何持久 Goal/End。
    """

    def __init__(self, *, data_dir: Path, boundaries: BoundaryOverrides,
                 clock, task_manager, email_sender, notifications, control,
                 takeover: DataTakeover, account_display_name: str = '',
                 settings_snapshot: Optional[Dict[str, Any]] = None,
                 account_root: Optional[Path] = None):
        self.data_dir = Path(data_dir)
        self.boundaries = boundaries
        self.clock = clock
        #: 旧的**实例级**任务库。账号模式（``account_root`` 给出）下为 None：
        #: 那时任务数据由**账号绑定**持有，不在这里再造一份实例库。
        self._legacy_task_manager = task_manager
        self.account_root = (Path(account_root) if account_root is not None
                             else None)
        self.email_sender = email_sender
        self.notifications = notifications
        self.control = control
        self.takeover = takeover
        self.settings_snapshot: Dict[str, Any] = dict(settings_snapshot or {})
        self.account_display_name = account_display_name
        #: 已核验账号的头像 URL（面板左下角用）。与显示名一样只做展示，
        #: 不参与任何决策；未核验/未取得时为空串。
        self.account_face = ''
        self.instance_errors: List[str] = []
        self.boundary_injections = _boundary_evidence(boundaries)
        self._close_lock = threading.Lock()
        self._close_report: Optional[dict] = None
        #: 关闭门一旦进入，迟到的账号资料补齐结果不得再改展示字段或写凭据。
        #: 只读 ``__dict__``（``getattr`` 带默认值），因此 ``__new__`` 构造的
        #: 夹具对象也安全。
        self._identity_closed = False
        self._day_prep: Optional['DayPrepScheduler'] = None
        # ---- 桌面适配点（默认关闭：服务端语义逐字不变，见 session_runtime.desktop）
        #: True 时 Bootstrap **之前**一律落实"不自动开播"（桌面重启只提示恢复）。
        self.manual_resume_only = False
        #: 领取账号目录**之前**的旧资料准备钩子（桌面：``prepare_account_target``）。
        #: 返回非空字符串即拒绝本次激活，字符串就是如实原因。
        self.account_preparer: Optional[Callable[[Any], Optional[str]]] = None

    # ---------------------------------------------------------------- 只读口

    @property
    def account_mode(self) -> bool:
        """是否启用账号级数据路径（同机共享根 + 按 UID 分库）。"""
        return self.account_root is not None

    @property
    def task_manager(self):
        """**当前账号**的任务库；未登录（账号模式）时为 None。

        账号模式下任务数据属于 UID，因此这里读的是**已安装实例**身上的账号绑定：
        换账号就换库，未登录就没有匿名任务库。兼容模式仍返回实例级任务库。
        """
        instance = self.control.instance if self.control is not None else None
        binding = getattr(instance, 'account_binding', None)
        if binding is not None:
            return binding.task_manager
        return self._legacy_task_manager

    @property
    def account_binding(self):
        """当前已安装实例的账号数据绑定（未登录/兼容模式为 None）。"""
        instance = self.control.instance if self.control is not None else None
        return getattr(instance, 'account_binding', None)

    @property
    def control_service(self):
        return self.control

    @property
    def instance(self):
        return self.control.instance

    def control_available(self) -> bool:
        return self.takeover.control_available

    def health(self) -> dict:
        binding = self.account_binding
        return {
            'data_takeover': self.takeover.to_dict(),
            'account': (None if self.instance is None
                        else {'uid': int(getattr(self.instance.account, 'uid', 0)),
                              'room_id': int(getattr(self.instance.account, 'room_id', 0))}),
            'account_display_name': self.account_display_name,
            'legacy_controller': False,
            'boundary_injections': self.boundary_injections,
            'instance_errors': list(self.instance_errors),
            'account_mode': self.account_mode,
            'account_root': (str(self.account_root) if self.account_root is not None
                             else None),
            'account_binding': (binding.to_dict() if binding is not None else None),
        }

    def enrich_account_identity(self) -> None:
        """启动后**有界**补齐账号显示名/头像（只读 GET；不参与任何决策）。

        旧凭据只缓存了登录 cookies、没有 ``uname``/``face``，账号模式下启动时显示名
        为空：面板左下角会显示"未登录"，通知署名也会退化成实例标识。这里用同一份
        凭据问一次平台（与扫码/显式登录同一条只读路径 ``get_user_info``），成功则
        写入内存并缓存在凭据文件里（下次启动直接命中，不再联网）；任何失败只记日志，
        绝不影响登录、控制或数据。

        只由启动路径调用一次；在线程里执行，不阻塞应用启动。

        **落点不是无条件写入**（复核 R1）：发起时冻结本次请求所属的账号归属，
        网络等待全程在锁外；回包只有在**同一个受保护提交边界**里复核通过才应用 ——
        磁盘上仍是同一个 UID、仍持有会话、本 owner 未关闭。这里的提交只合并
        ``uname``/``face`` 两个**展示元数据**，不拿资料 GET 的旧快照替换当前整包
        认证凭据；退出、换号、关闭之后的迟到回包一律丢弃，既不写磁盘也不改展示
        字段。
        """
        def _drop(reason: str) -> None:
            logger.info('账号显示名补齐结果已丢弃（%s）：不改展示字段、不写凭据',
                        reason)

        def _worker() -> None:
            try:
                from ..live_controller import (
                    BilibiliApi, credential_commit_lock)
                cookie_file = self.data_dir / 'bili_cookies.json'
                api = BilibiliApi(cookie_file=str(cookie_file))
                if not api.is_logged_in():
                    return
                # 发起时冻结实际账号归属：回包必须仍然属于这个 UID 才可能被应用。
                frozen_uid = _as_positive_int(
                    dict(getattr(api, 'cookies', {}) or {}).get('DedeUserID'))
                if frozen_uid is None:
                    return
                ok, payload = api.get_user_info()
                data = payload.get('data') if isinstance(payload, dict) else None
                if not ok or not isinstance(data, dict):
                    logger.warning('账号显示名补齐：平台未返回用户信息（不影响控制）')
                    return
                name = str(data.get('name') or '').strip()
                face = str(data.get('face') or '').strip()
                if not name:
                    return
                # 复核与提交在**同一个**按文件串行的边界里完成，因此不存在
                # "先比较 UID、再无保护写回"的窗口。
                with credential_commit_lock(cookie_file):
                    if getattr(self, '_identity_closed', False):
                        return _drop('owner 已关闭')
                    current = api.read_cookie_snapshot()
                    if _as_positive_int(current.get('DedeUserID')) != frozen_uid:
                        return _drop('账号已更换或凭据已撤销')
                    if not str(current.get('SESSDATA') or '').strip():
                        return _drop('会话已撤销')
                    # 只更新展示元数据：以**当前**凭据为准合并，绝不用旧 GET 的
                    # 快照（那是发起那一刻的整包 cookies）覆盖现在这份认证凭据。
                    merged = dict(current)
                    merged['uname'] = name
                    if face:
                        merged['face'] = face
                    self.account_display_name = name
                    if face:
                        self.account_face = face
                    if not api.replace_cookies(merged):
                        logger.warning('账号显示名已生效，但未写入凭据缓存（下轮再补）')
                logger.info('账号显示名已补齐：%s', name)
            except Exception as exc:                          # noqa: BLE001
                logger.warning('账号显示名补齐失败（不影响控制）：%s', exc)

        threading.Thread(target=_worker, name='AccountIdentityEnrich',
                         daemon=True).start()

    # ---------------------------------------------------------------- 启动

    def prepare_data_and_account(self, *, account: Optional[AccountContext] = None,
                                 credentials: Optional[Dict[str, str]] = None,
                                 error: str = '') -> None:
        """启动数据接管的**唯一**顺序（同步慢活，由调用方放到事件循环外）。"""
        if error:
            self.takeover = DataTakeover(status='refused', reason='takeover_failed',
                                         detail=error)
            self.instance_errors.append(error)
            return
        if account is None:
            return
        instance = self.activate_account(account=account, credentials=credentials)
        if instance is None:
            return
        if not self.control.install_instance(instance):
            # 槽位已被并发扫码占用：不覆盖已有所有者，本次实例自己释放。
            self.instance_errors.append('账号插槽已被占用，本次实例已释放')
            try:
                instance.close(float(self.boundaries.close_budget_seconds))
            except Exception:                             # noqa: BLE001
                pass

    def activate_account(self, *, account: Optional[AccountContext],
                         credentials: Optional[Dict[str, str]]
                         ) -> Optional[RuntimeInstance]:
        """账号的**完整激活**：领取 → 数据接管 → 组装 → 真正可提交的 actor。

        启动路径与扫码/显式登录共用**同一条**职责。因此"安装成功"必然意味着实例
        已经起了 actor、提交过库存并完成 Bootstrap（复核 R1）——它不由某个后续
        按钮补齐。这里**不**把实例装到控制面：安装仍由 ``ControlService`` 在它自己
        的短锁语义下完成，调用方不越过那条边界。

        账号模式（``account_root`` 已配置）下的顺序是承重的：**先领取 UID 原生占用，
        再迁移/开库/构造 Runtime**。输者到此为止，绝不构造第二份 TaskManager 或
        Runtime。失败一律如实返回 ``None`` 并留下原因；已经取得的实例**经既有未释放
        候选门**移交（见 ``_hand_back_failed_instance``）—— 收齐就释放（绑定与占用
        一并释放），没收齐就保留真实实例、绑定与账号占用，绝不随返回一起丢引用。
        """
        if account is None:
            return None
        binding = None
        if self.account_mode:
            # 桌面适配点：**先接管旧资料，再建账号目录**。顺序反了就会先落一个空
            # 目标库，之后既没有合法覆盖路径、也说不清旧任务去了哪里。
            if self.account_preparer is not None:
                refusal = self.account_preparer(account)
                if refusal:
                    self.instance_errors.append(refusal)
                    return None
            try:
                binding = self.open_account_binding(account)
            except AccountBusy as exc:
                # 占用与读取失败分别说明：这里既没有开库、也没有构造 Runtime。
                self.instance_errors.append(f'账号已被占用：{exc.detail or exc.reason}')
                return None
            except AccountAcquisitionError as exc:
                # 取得绑定**之后**才失败的出口：没收齐的真实所有者随异常一起交回，
                # 不在这里丢掉引用、也不把它写成一句日志（复核 R3）。
                self.instance_errors.append(
                    f'账号数据不可用（{exc.reason}）：{exc.detail}')
                self._retain_acquisition_residual(exc.retained)
                return None
            except AccountError as exc:
                self.instance_errors.append(
                    f'账号数据不可用（{exc.reason}）：{exc.detail}')
                return None
            if binding is None:
                return None
        data_dir = binding.paths.account_dir if binding is not None else self.data_dir
        if self.account_mode:
            # 账号模式下接管是**按账号**的：换一个 UID 就换一份资料，因此每次激活
            # 都在**该账号目录**上跑一次同一个阶段 2 解释器。它是幂等的（已是可信
            # 新档时直接返回 trusted），所以这里不是"重复迁移"，而是"这一次解释的
            # 是这一个账号的资料"。
            self.takeover = take_over_state(
                data_dir=data_dir, account=account,
                settings=self.settings_snapshot or {}, now=self.clock.wall())
            if not self.takeover.state_usable:
                self.instance_errors.append(
                    f'账号数据接管未通过：{self.takeover.reason or self.takeover.detail}')
                self._release_unused_binding(binding)
                return None
        elif not self.takeover.state_usable:
            # 启动时没有账号 → 旧档只被解释成 account_required；现在有已核验的
            # 登录上下文了，用**同一个**解释器补做一次（A5），不是新规则。
            if not self.retake_over_for(account, data_dir=data_dir):
                return None
        try:
            instance = build_account_instance(
                data_dir=self.data_dir, account=account,
                credentials=dict(credentials or {}), clock=self.clock,
                task_manager=(binding.task_manager if binding is not None
                              else self._legacy_task_manager),
                notifications_sink=self.notifications.enqueue_frozen,
                boundaries=self.boundaries,
                account_binding=binding)
        except InstanceConstructionError as exc:
            self.instance_errors.append(f'账号实例组装失败：{exc.cause}')
            self._release_unused_binding(binding)
            return None
        except Exception as exc:                          # noqa: BLE001
            self.instance_errors.append(
                f'账号实例组装失败：{type(exc).__name__}: {exc}')
            self._release_unused_binding(binding)
            return None
        try:
            instance.start(pre_bootstrap=self._pre_bootstrap)
        except Exception as exc:                          # noqa: BLE001
            # **先**移交、**后**记失败原因：``instance_factory`` 用
            # ``instance_errors[-1]`` 作为回执原因，最近一条必须仍是这次启动失败的
            # 真实原因（移交说明紧挨在它前面，一起留在健康面上）。
            self._hand_back_failed_instance(instance)
            self.instance_errors.append(f'账号实例启动失败：{type(exc).__name__}: {exc}')
            return None
        return instance

    def open_account_binding(self, account: AccountContext) -> 'AccountBinding':
        """**先领取，再迁移/开库**：取得该账号的绑定，或抛出明确的失败。

        这里只做"占用 → 归属核验 → 任务库/SessionStore"这一段；Runtime 的构造在
        ``activate_account`` 里、绑定取得**之后**才发生。
        """
        return open_account_binding(self.account_root, int(account.uid))

    def _release_unused_binding(self, binding: Any) -> None:
        """装配失败、实例尚不存在时收回绑定：收齐即释放（含占用），未收齐则移交。

        **不在通用 finally 里无条件释放**：绑定与实例走**同一条**释放/保留规则
        ——``AccountBinding.close`` 的报告实现了与 ``InstanceCloseReport`` 相同的
        ``clean()`` 判据，因此这里直接把它交回 ``ControlService`` 的既有释放门。
        收齐就释放（任务库句柄 + 账号占用一并释放）；``close`` 抛错、拿不到可信
        报告或明确不干净时，由那条门把**真实绑定**保留为资源所有者，并让下一次
        登录在**工厂之前**被暂拒。不新增保留表、计数器或清理线程。

        （复核 R3：此前这里遇 close 未收齐只记一句原因，绑定与占用在无人持有的
        状态下继续存在，下一次激活还能在本进程内"再领一次"而成功。）
        """
        if binding is None:
            return
        dispose = getattr(getattr(self, 'control', None),
                          'dispose_replacement', None)
        if callable(dispose):
            note = dispose(binding)
            if note:
                self.instance_errors.append(note)
            return
        # 还没有控制面可移交（正常启动顺序里不会出现）：只做一次预算内释放，
        # 并如实记下结果，不假装已经收齐、也不丢引用。
        try:
            report = binding.close(float(self.boundaries.close_budget_seconds))
        except Exception as exc:                          # noqa: BLE001
            self.instance_errors.append(
                f'账号绑定释放异常：{type(exc).__name__}: {exc}')
            return
        clean = getattr(report, 'clean', None)
        if not (callable(clean) and clean()):
            self.instance_errors.append('账号绑定未收齐，且当时没有控制面可移交')

    def _retain_acquisition_residual(self, residual: Any) -> None:
        """把"取得绑定但资源没收齐"的**真实所有者**交回既有未释放候选门。

        与实例启动失败出口同一条门（``ControlService.dispose_replacement``）：
        门内用同一条 ``clean()`` 判据判断收齐与否——收齐就释放，未收齐就保留真实
        绑定与真实报告，并让下一次登录在**工厂之前**被拒（复核 R3 / A09）。
        """
        if residual is None:
            return
        dispose = getattr(getattr(self, 'control', None),
                          'dispose_replacement', None)
        if not callable(dispose):
            self.instance_errors.append(
                '账号数据资源未收齐，且当时没有控制面可移交')
            return
        note = dispose(residual)
        if note:
            self.instance_errors.append(note)

    def _hand_back_failed_instance(self, instance: RuntimeInstance) -> None:
        """启动失败的实例**经既有未释放候选门**移交，绝不丢引用（F1）。

        走到这里时实例是**真实存在**的：actor 线程、观察/媒体工作者可能都还活着
        （失败点常常就发生在 Bootstrap 之前的那一步里）。因此"启动失败"只等于
        "这一份不能作为账号安装"，绝不等于"已经释放干净"。

        这里复用 ``ControlService`` 的**同一条**释放门：收齐即释放；``close`` 抛错、
        拿不到可信报告或明确不干净时，由那条门把真实实例与**首份**真实报告保留为
        资源所有者，并让下一次登录在**工厂之前**被暂拒。不新增保留表、计数器、
        业务 Step、JSON 标记或清理线程 —— 保留与拦截都由既有门承担。

        调用约定不变：仍然返回 ``None``（启动路径 ``prepare_data_and_account`` 与
        账号工厂都按"失败即无实例"处理），只是资源不再随返回一起消失。
        """
        dispose = getattr(getattr(self, 'control', None),
                          'dispose_replacement', None)
        if not callable(dispose):
            # 还没有控制面可移交（正常启动顺序里不会出现）：只做一次预算内释放，
            # 并如实记下结果，不假装已经收齐。
            try:
                report = instance.close(
                    float(self.boundaries.close_budget_seconds))
            except Exception as exc:                      # noqa: BLE001
                self.instance_errors.append(
                    f'启动失败实例释放异常：{type(exc).__name__}: {exc}')
                return
            clean = getattr(report, 'clean', None)
            if not (callable(clean) and clean()):
                self.instance_errors.append('启动失败实例未收齐，且当时没有控制面可移交')
            return
        note = dispose(instance)
        if note:
            self.instance_errors.append(note)

    def retake_over_for(self, account: AccountContext,
                        data_dir: Optional[Path] = None) -> bool:
        """扫码/显式登录后补齐接管：启动时**无账号**留下的旧档在这里才可解释。

        ``account_required`` 不是终态（A5）：一旦有了已核验的登录上下文，就用同
        一个阶段 2 解释器把同一份输入重新解释一次。接管已经可用时**不重跑**——
        重跑会把已经取证的步骤记录覆盖成"已是可信新档"。

        ``data_dir`` 是**这次接管要解释的那一份资料所在目录**：账号模式下它是
        ``accounts/<uid>/``，兼容模式下仍是实例目录。
        """
        if self.takeover.state_usable:
            return True
        self.takeover = take_over_state(
            data_dir=Path(data_dir) if data_dir is not None else self.data_dir,
            account=account,
            settings=self.settings_snapshot or {}, now=self.clock.wall())
        if not self.takeover.state_usable:
            self.instance_errors.append(
                f'登录后数据接管仍未通过：{self.takeover.reason or self.takeover.detail}')
            return False
        return True

    def external_stop_markers(self, instance: RuntimeInstance) -> List[Path]:
        """本次 Bootstrap 之前必须落实的"外部停止"标记。

        标记与它要落实的**那份状态**必须一起看：

        - **实例本地**标记（运维停服务时写下）——既有规则，位置不变；
        - **账号目录**标记——账号模式下状态文件住在 ``accounts/<uid>/``，显式迁移
          会把来源目录的 ``.no_auto_resume`` 逐字节搬进账号目录；那是**这一个账号
          自己的**停止指令，与状态同住一目录。只从实例目录读标记会把"复制了文件"
          当成"保留了停止语义"，于是迁移后的可信新档照旧自动发 Start（复核 R4）。

        这里**只**看这两个目录：不去扫旧备份、别的实例目录或历史版本目录 —— 那些
        位置上的标记不构成"当前账号的指令"（交办 §7）。
        """
        candidates = [self.data_dir / NO_AUTO_RESUME_MARKER]
        binding = getattr(instance, 'account_binding', None)
        account_dir = getattr(getattr(binding, 'paths', None), 'account_dir', None)
        if account_dir is not None:
            candidates.append(Path(account_dir) / NO_AUTO_RESUME_MARKER)
        found: List[Path] = []
        for marker in candidates:
            if marker in found:
                continue
            try:
                if marker.exists():
                    found.append(marker)
            except OSError:
                continue
        return found

    def _pre_bootstrap(self, instance: RuntimeInstance) -> None:
        """Bootstrap **可派发 Start 之前**的唯一一步：落实 ``.no_auto_resume``。

        ``.no_auto_resume`` 是**外部停止指令**（运维停服务时写下，或随显式迁移从
        来源账号目录搬来），而它要落实的是**当前账号**那一份状态：账号模式下这两者
        不在同一个目录，因此标记按 :meth:`external_stop_markers` 取，状态从实例自己
        的 ``store``（账号目录）读写。判据取**磁盘上的权威快照**（此刻还没有任何输入
        被提交，文件就是事实来源），因此这一步不依赖"执行器已经发布了第一份视图"这种
        时序巧合。

        桌面适配点：``manual_resume_only`` 为真时**不需要**标记也必须走同一条路 ——
        桌面产品的规则是"重开应用只提示恢复、不自动开播"，因此只要磁盘上还留着一份
        要续播的原场，就在这里按既有用户 Stop 语义停一次。这不是"先自动开播再补
        Stop"：此函数在 Bootstrap 派发任何 Start 之前执行，且它**只改 Goal，不动
        Run/固定目标/检查点**，因此"继续任务"仍能按原 Run 与 Goal revision 恢复。

        三种状态读取结论分别走三条不同的路（复核 R1）：**没有**任何状态资料
        （``first_start``）按首次启动继续；**存在**可信快照时按上面的停止语义落实；
        状态**存在却不可信**才抛错、保留标记。前两者都不是"放过读失败"。
        """
        markers = self.external_stop_markers(instance)
        manual_only = bool(getattr(self, 'manual_resume_only', False))
        if not markers and not manual_only:
            return
        store = getattr(instance, 'store', None)
        if store is None:                                 # pragma: no cover - 契约护栏
            raise RuntimeError('实例没有可用的状态存储，外部停止要求无法落实，标记保留')
        loaded = store.load()
        if loaded.status == VERDICT_FIRST_START:
            # **没有任何状态资料**（主档与最后可用副本都不存在）：这是正常的首次
            # 启动，不是坏档。这里没有可续播的原场，也就没有任何东西需要停 ——
            # 外部停止要求（若有标记）此刻已被满足，消费标记后按首次启动继续。
            #
            # 把 ``first_start`` 与"状态存在却不可信"合并处理会把**每一个新账号**
            # 挡在登录之外（复核 R1）：新账号第一次扫码时账号目录刚建好、状态档
            # 还不存在，而 ``manual_resume_only`` 让本函数每次都会走到这里。
            for marker in markers:
                self._consume_marker(marker)
            return
        if loaded.status != VERDICT_TRUSTED or loaded.snapshot is None:
            # 状态**存在**却读不出 / 判为不可信：不消费标记，也不允许进入 Bootstrap
            # （否则会带着未被落实的停止要求开播）。读不出不等于没有 —— 这条出口
            # 与 :class:`SessionStore` 的 fail-closed 判据同源，不是"一律放过"。
            raise RuntimeError('状态不可信，外部停止要求无法落实，标记保留')
        snapshot = loaded.snapshot
        goal = snapshot.goal
        if goal.wanted != WANT_RUNNING and snapshot.run is None:
            # 已经是持久停止状态：外部停止要求已被满足，直接消费标记。
            for marker in markers:
                self._consume_marker(marker)
            return
        event = UserGoal(wanted=WANT_STOPPED, mode=goal.mode,
                         selection=copy.deepcopy(goal.selection),
                         start_kind=START_NEW, stop_reason=STOP_USER)
        reply = instance.submit(event, timeout=float(self.boundaries.op_budget_seconds))
        if not isinstance(reply, Accepted):
            raise RuntimeError(
                f'外部停止要求未被受理（{type(reply).__name__}）：'
                f'{getattr(reply, "reason", "")}')
        verified = store.load()
        if (verified.status != VERDICT_TRUSTED or verified.snapshot is None
                or verified.snapshot.goal.wanted != WANT_STOPPED):
            # 保存不成立就**不消费标记**：下次启动重试，本次不发任何 Start。
            raise RuntimeError('外部停止要求未能持久落实，标记保留')
        for marker in markers:
            self._consume_marker(marker)

    @staticmethod
    def _consume_marker(marker: Path) -> None:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------- 日切预约

    def start_day_prep(self, *, interval: float = 30.0) -> None:
        """薄日期预约：只调既有数据准备入口，不启旧日切线程、不扫任务库。

        ``interval`` 只用于**被保护窗口挡住之后的有界补做**（见
        :class:`DayPrepScheduler`），正常节奏仍是下一个午夜。
        """
        if self._day_prep is not None:
            return
        self._day_prep = DayPrepScheduler(self, interval=interval)
        self._day_prep.start()

    def can_prepare_day(self) -> Tuple[bool, str]:
        """「有**待结算的任务** End 时不抢跑旧日结算与简报」的唯一判据。

        要保护的是**实际义务**，不是"存在一份可恢复的原场"：

        - 保留可恢复的 Run（用户停止后仍留着进度、单次场、等待任务）都不是待结算
          任务：数据日切照常，旧秒数不需要用户先放弃（复核 R8 / C1 / C2）；
        - **仍在运行**的旧业务日任务场还没形成 End 时必须让路：Core 的午夜完成窗口
          允许它在截止后一小段内按校准计时达标、记进**旧日**账本。此刻冻结旧日简报
          会把这一场记成 0 完成（复核 R2）；先让现有场次决策收口（记旧日 / 窗口末
          丢弃），再冻结正确的旧日资料并准备今日。窗口本身是有界的，因此这不是
          无限等待；
        - 反过来，**用户已经停止**的旧日任务场（Goal 落盘 ``stopped``、只剩可恢复
          检查点、End 尚未形成）**没有**任何待结算义务：Core 已停止计时、也不会有
          任何后续决策替它补一个 End，日切保护不能因此无限期等待（复核 R1）。它与
          单次场、等待任务、已放弃原场一样属于"存在一份可恢复的原场"；
        - 任务模式的 End 已持久、``settle`` 已置位、而账本回执尚未落地的那一小段
          （含午夜优先窗口）同样必须让路（C3）。这道保护在下面**先于** stopped 判断
          生效，因此"停止途中已经形成的待结算 End"不会被一次停止越过。
        """
        try:
            snapshot = self.control.read()
        except Exception as exc:                          # noqa: BLE001
            return False, f'read_failed:{type(exc).__name__}'
        if not snapshot:
            return True, 'no_state'
        run = snapshot.get('run')
        if not run:
            return True, 'no_run'
        goal = snapshot.get('goal') or {}
        goal_mode = goal.get('mode')
        if str(run.get('mode') or goal_mode or '') != MODE_TASK:
            return True, 'not_a_task_run'
        end = run.get('end')
        if not end:
            business_day = str(run.get('business_day') or '')
            if business_day and business_day != business_day_of(self.clock.wall()):
                if str(goal.get('wanted') or '') == WANT_STOPPED:
                    # 已停止、无 End 的可恢复检查点：不是待结算任务，放行日切。
                    return True, 'stopped_residual'
                return False, 'old_day_window'
            return True, 'no_end'
        if not end.get('settle'):
            return True, 'end_without_settle'
        if end.get('ledger_receipt'):
            return True, 'ledger_settled'
        return False, 'ledger_in_flight'

    def prepare_day_now(self) -> bool:
        """今天的数据准备（日切）。

        账号模式下这是一段**真实账号工作**：它和任务 CRUD、账本结算走**同一份**
        绑定的在途持有 —— 先冻结当前绑定并进入工作，再判据、再执行既有的
        ``reset_daily_flags``。因此退出/关闭必须等这一段（含它内部已打开的 SQLite
        事务）真正收尾才关库放锁，日切也不会"先取得旧 manager、再借新账号的判据"。
        绑定已在收尾时**明确不开始**新工作，也绝不改去取别的账号的库（复核 R2）。
        """
        binding = self.account_binding
        if binding is None:
            # 兼容模式（实例级单库）或未登录：没有第二份账号资源需要持有。
            return self._prepare_day_with(self._legacy_task_manager)
        if not binding.try_enter_work():
            return False
        try:
            return self._prepare_day_with(binding.task_manager)
        finally:
            binding.leave_work()

    def _prepare_day_with(self, task_manager) -> bool:
        allowed, _reason = self.can_prepare_day()
        if not allowed:
            return False
        if task_manager is None:
            # 账号模式未登录：**不**构造匿名任务库、也不做匿名日切（A13）。
            return False
        today = self.clock.wall().date()
        try:
            return bool(task_manager.reset_daily_flags(target_date=today))
        except Exception as exc:                          # noqa: BLE001
            logger.warning('数据日切准备失败（不影响直播）：%s', exc)
            return False

    # ---------------------------------------------------------------- 任务唤醒

    def notify_tasks_changed(self, business_day: str = '',
                             reason: str = 'changed',
                             timeout: float = 2.0) -> dict:
        """提交事实已落盘之后的**窄唤醒**；投递失败如实返回，不谎称回滚。"""
        instance = self.instance
        if instance is None:
            return {'delivered': False, 'reason': 'no_account'}
        try:
            reply = instance.tasks_changed(business_day, reason, timeout=timeout)
        except Exception as exc:                          # noqa: BLE001
            return {'delivered': False,
                    'reason': f'{type(exc).__name__}: {exc}'}
        return {'delivered': True, 'result': type(reply).__name__}

    # ---------------------------------------------------------------- 关闭

    def _retry_unreleased_binding(self, instance_report: dict,
                                  budget: float) -> Optional[dict]:
        """实例首份报告说"账号绑定未收齐"时，在**同一份预算内**再收一次。

        实例的 ``close`` 有一道**幂等门**（F1：首份真实报告一经产出就不再重跑），
        因此"预算到点、在途工作未收齐"的结论会一直粘在那份实例上；而账号绑定自己的
        关闭门明确支持"在途工作收尾之后再试"（未收齐的结论刻意不缓存）。进程退出时
        把真实资源收齐是 owner 的职责，这里补一次**有界**的收尾，并且：

        - **不改写**实例的首份报告 —— 两个事实（首份未清 / 最终释放）分开记录；
        - 绑定不存在或首份报告说已经释放时什么都不做。
        """
        if instance_report is None:
            return None
        if instance_report.get('account_binding_released', True):
            return None
        binding = self.account_binding
        if binding is None:
            return None
        try:
            return dict(binding.close(max(0.0, float(budget))) or {})
        except Exception as exc:                          # noqa: BLE001
            return {'error': f'{type(exc).__name__}: {exc}'}

    # ---------------------------------------------------------------- 关闭门

    def close(self, budget: Optional[float] = None) -> dict:
        """统一幂等关闭门：账号实例 → 日切预约 → 通知出口 → 共享任务库。"""
        with self._close_lock:
            if self._close_report is not None:
                return self._close_report
            # 关闭入口先冻结**本次绝对截止**：它既是关闭宣告取锁的上限，也是后续清理
            # 的剩余预算。取锁等待与资源清理必须**共同**消费同一份预算 —— 先取锁再读
            # 预算，等于把无限长的等待白送给这次关闭（独立复核 R1：给定 0.05 秒预算时
            # 仍等到放行后 0.407 秒才返回，且交给 Runtime 的仍是完整的 0.05 秒）。
            limit = (float(self.boundaries.close_budget_seconds) if budget is None
                     else max(0.0, float(budget)))
            deadline = time.monotonic() + max(0.0, limit)
            # 关闭宣告与资料提交**共用同一个提交边界**：落旗必须按凭据文件取同一把
            # 串行锁（``enrich_account_identity`` 的短提交就在这把锁里读盘-合并-写回）。
            # 只在凭据锁内读一次标志再写回仍会留下"检查通过到写入完成"的窗口：资料
            # 线程可能已通过检查、正暂停在读盘入口，此时关闭宣告照样成立、close 照样
            # 返回，随后那次写回才落盘 —— 这正是独立复核 F1 的实证交错。
            # 取同一把锁之后，合法次序只剩两种：已经进入的短提交先跑完、随后 close
            # 才返回；或关闭先落旗，迟到结果在同一个边界里看见旗帜并被丢弃。
            #
            # 取锁**有界**（按剩余预算）：到点就如实记下这次资料提交尚未收齐 ——
            # 不落旗（落旗本身也必须在这把锁里，锁外落旗会把 F1 的窗口放回来）、
            # 不强行释放别的线程持有的锁（那不是本门的所有权）、也不把这次关闭报成
            # ok=true。持锁方只做一次短提交，正常预算下必然取到。
            from ..live_controller import credential_commit_lock
            commit_lock = credential_commit_lock(self.data_dir / 'bili_cookies.json')
            report: Dict[str, Any] = {'steps': [], 'errors': []}
            if commit_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                try:
                    self._identity_closed = True
                finally:
                    commit_lock.release()
                report['steps'].append('identity_commit_closed')
            else:
                report['errors'].append('identity_commit_pending')
                report['identity_commit'] = 'pending'
            # 剩下的才是资源清理的预算：等待时间已经从同一份截止里扣掉。
            limit = max(0.0, deadline - time.monotonic())
            scheduler = self._day_prep
            if scheduler is not None:
                try:
                    scheduler.stop()
                    report['steps'].append('day_prep_stopped')
                except Exception as exc:                  # noqa: BLE001
                    report['errors'].append(f'day_prep:{type(exc).__name__}')
            instance = self.control.instance
            if instance is not None:
                try:
                    report['instance'] = instance.close(limit).to_dict()
                    report['steps'].append('instance_closed')
                    retry = self._retry_unreleased_binding(report['instance'], limit)
                    if retry is not None:
                        report['account_binding_retry'] = retry
                        report['steps'].append('account_binding_retried')
                except Exception as exc:                  # noqa: BLE001
                    report['errors'].append(f'instance:{type(exc).__name__}: {exc}')
            # 未收齐的候选实例（复核 R2：没能安装、又没关干净）在进程退出前交回
            # **既有** close 门，避免留下孤儿 actor/观察线程；不把它当成账号。
            try:
                unreleased = self.control.close_unreleased(limit)
                if unreleased['count']:
                    report['unreleased_candidates'] = unreleased
                    report['steps'].append('unreleased_candidates_closed')
            except Exception as exc:                      # noqa: BLE001
                report['errors'].append(f'unreleased:{type(exc).__name__}: {exc}')
            try:
                self.notifications.reset()
            except Exception:                             # noqa: BLE001
                pass
            # 邮件先停受理（可能还有最后一条事实在队列里），共享任务库最后关。
            try:
                self.email_sender.shutdown()
                report['steps'].append('email_shutdown')
            except Exception as exc:                      # noqa: BLE001
                report['errors'].append(f'email:{type(exc).__name__}: {exc}')
            if self._legacy_task_manager is not None:
                # 兼容模式：实例级任务库由 owner 持有、最后关闭。账号模式没有这
                # 一份 —— 任务数据属于账号绑定，已随实例（或保留中的候选）关闭。
                try:
                    self._legacy_task_manager.shutdown()
                    report['steps'].append('task_manager_shutdown')
                except Exception as exc:                  # noqa: BLE001
                    report['errors'].append(
                        f'task_manager:{type(exc).__name__}: {exc}')
            report['ok'] = not report['errors']
            self._close_report = report
            return report


class DayPrepScheduler(threading.Thread):
    """只做"到点调一次既有数据准备入口"的薄预约线程（不是旧日切调度器）。

    ``interval`` 是**补做间隔**：一次 tick 没有真正推进日期（被 ``can_prepare_day``
    的保护窗口挡住，或准备本身失败）时按它重试，而不是等下一个午夜——保护窗口最宽
    只有 Core 的午夜完成窗口那么长，绝不能让补做落到一小时之后（复核 R2）。
    """

    def __init__(self, owner: ApplicationOwner, *, interval: float = 30.0) -> None:
        super().__init__(name='Phase4BDayPrep', daemon=True)
        self._owner = owner
        self._interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._prepared = False

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:                                # pragma: no cover - 计时
        # 启动补做：进程在午夜前后重启时，第一次 tick 就把漏掉的准备补上。
        self._prepared = self._tick()
        while not self._stop.wait(self._next_delay()):
            self._prepared = self._tick()

    def _tick(self) -> bool:
        """跑一次既有准备入口；返回"今天确实已准备"。异常按未完成处理并如实记录。"""
        try:
            return bool(self._owner.prepare_day_now())
        except Exception as exc:                          # noqa: BLE001
            logger.warning('日期预约失败（不影响直播）：%s', exc)
            return False

    def _next_delay(self) -> float:
        """已准备 → 等下一个午夜；未完成 → 按构造的补做间隔**有界**重试。"""
        to_midnight = self._seconds_to_next_midnight()
        if self._prepared:
            return to_midnight
        return min(self._interval, to_midnight)

    def _seconds_to_next_midnight(self) -> float:
        now = self._owner.clock.wall()
        tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
        delta = (tomorrow - now).total_seconds()
        return max(1.0, min(delta, 3600.0))


def _boundary_evidence(boundaries: BoundaryOverrides) -> Dict[str, Any]:
    """记录**是否**注入过隔离边界；不记录任何地址/推流码/凭据内容。"""
    return {
        'endpoint_overridden': bool(boundaries.endpoint_base),
        'notifications_overridden': boundaries.notifications is not None,
        'media_output_overridden': bool(boundaries.media_output
                                        or boundaries.media_builder_factory),
        'clock_overridden': boundaries.clock is not None,
        'account_overridden': boundaries.account is not None,
        #: 本实例在播核对的**生效**节拍（秒）：默认构造时来自已保存的
        #: ``scan_interval_seconds``，显式注入时来自注入值。只记数值，不记配置内容。
        'watch_interval_seconds': float(boundaries.watch_interval_seconds),
        'watch_interval_explicit': ('watch_interval_seconds'
                                    in boundaries.explicit_fields),
    }


# ==================== 默认组装入口 ====================

def build_application_owner(*, data_dir: Path,
                            notifier_policy: Optional[Callable[[Any], None]] = None,
                            ) -> ApplicationOwner:
    """构造应用 owner。**只构造**，不启动账号实例、不接管数据。

    ``notifier_policy`` 是**桌面适配点**（第 4 处接缝，默认 ``None``）：给定时在
    ``EmailSender`` 预热**之前**调用一次，让装配方先声明本产品对通知器的要求。
    默认 ``None`` 时本函数与服务端逐字一致——预热顺序、探测行为都不变。
    """
    from app.api.settings import load_settings
    from .. import config as app_config
    from ..task_manager import TaskManager
    from ..session.clock import SystemClock
    from .control import ControlService

    boundaries = _freeze_boundaries()
    data_dir = Path(data_dir)
    clock = boundaries.clock or SystemClock()

    settings = load_settings()
    _apply_saved_scan_interval(boundaries, settings)
    _apply_saved_video_base(boundaries, settings)
    account_root = app_config.account_root()
    if account_root is None:
        # 兼容模式：实例目录里的单份任务库（旧行为逐字保留）。
        task_manager = TaskManager(
            db_path=str(data_dir / 'live_tasks.db'),
            excel_path=str(data_dir / 'live_tasks.xlsx'),
            reset_scheduler=False)          # 默认入口**不**启动旧日切线程
    else:
        # 账号模式：任务数据属于 UID，由**账号绑定**持有；这里不构造实例级任务库，
        # 也不在未登录时造一份匿名库（A13）。
        task_manager = None
        logger.info('账号级数据路径已启用：共享账号根=%s', account_root)
    if boundaries.notifications is not None:
        email_sender = boundaries.notifications
    else:
        from ..email_sender import EmailSender
        email_sender = EmailSender()
        # 桌面适配点（默认 None：服务端语义逐字不变）。策略必须在预热**之前**生效：
        # 预热会触发一次后台探测，事后禁用只能拦住"下一次"，拦不住这一次的连接。
        if notifier_policy is not None:
            notifier_policy(email_sender)
        warmup = getattr(email_sender, 'warmup_host', None)
        if callable(warmup):
            warmup()
    notifications = NotificationAdapter(email_sender)

    owner = ApplicationOwner(
        data_dir=data_dir, boundaries=boundaries, clock=clock,
        task_manager=task_manager, email_sender=email_sender,
        notifications=notifications, control=None,
        takeover=DataTakeover(status='first_start', detail='尚未执行数据接管'),
        settings_snapshot=_settings_snapshot(settings),
        account_root=account_root)

    def instance_factory(account: AccountContext, credentials: Dict[str, str]):
        """控制面只认识"已经真正可提交"的实例：激活职责在 owner 这一侧闭环。"""
        instance = owner.activate_account(account=account, credentials=credentials)
        if instance is None:
            reason = (owner.instance_errors[-1] if owner.instance_errors
                      else '账号激活失败')
            raise InstanceConstructionError(RuntimeError(reason),
                                            {'runtime': None, 'owner': 'activation'})
        return instance

    owner.control = ControlService(
        None, instance_factory=instance_factory, submit_timeout=5.0,
        close_budget=float(owner.boundaries.close_budget_seconds))
    return owner


def _apply_saved_scan_interval(boundaries: BoundaryOverrides, settings: Any) -> None:
    """把保存的扫描间隔接到**默认构造**的在播核对节拍上。

    读的是设置页保存的 ``scan_interval_seconds``（"每隔多少秒检查一次直播状态"），
    落点是 ``BoundaryOverrides.watch_interval_seconds`` —— ``build_account_instance``
    已经把它原样传进 ``Tuning.watch_interval_seconds``，也就是真实 Core 的
    ``WAKE_WATCH`` 节奏。此前这条绑定只存在于旧 ``LiveController``，新默认入口把
    保存值读进 ``settings_snapshot`` 就不再往下传，于是**重建默认 owner 也不生效**。

    - **生效时点**：本次 owner 构造（与 ``settings_snapshot`` 同一时点）。运行中
      改设置要等下一次构造／实例构造 —— 本轮不扩成通用热更新框架，也不往设置页
      添加"用户只能重启"之外的新业务限制（旧入口同样是构造时读）。
    - **显式注入优先**：本次边界里显式给了 ``watch_interval_seconds`` 时不动它
      （测试/隔离注入不被设置默认值覆盖）。
    - **缺失/非法**按既有设置校验与兼容默认处理（``AppSettings`` 默认 30；非正值
      视为未设置），不另建配置权威，也不猜一个别的数。
    - 这**不是**媒体观察周期（``MediaPort.watch_interval_seconds`` 是另一个职责），
      也**不是**精确到期的来源：任务按目标秒数到期仍由独立预约负责。
    """
    if 'watch_interval_seconds' in boundaries.explicit_fields:
        return
    try:
        saved = int(getattr(settings, 'scan_interval_seconds', 0) or 0)
    except (TypeError, ValueError):
        saved = 0
    if saved > 0:
        boundaries.watch_interval_seconds = float(saved)


def _apply_saved_video_base(boundaries: BoundaryOverrides, settings: Any) -> None:
    """把设置页保存的视频目录接到**默认构造**的选片根上（桌面适配点）。

    桌面产品用 Electron 的目录选择器写 ``settings.json:video_path``；选片与进程
    创建读的是 ``BoundaryOverrides.video_base``（``_resolve_video_base`` 的第一优先
    级）。此前这条绑定只存在于旧 ``LiveController`` 的 ``VideoPathFinder``，新默认
    入口会一路回落到常量兜底，于是"设置页改目录"只对诊断接口可见、对真实推流无效。

    - **生效时点**：本次 owner 构造（与 ``settings_snapshot`` 同一时点），与
      ``_apply_saved_scan_interval`` 同一条口径，不另建热更新框架。
    - **显式注入优先**：本次边界里显式给了 ``video_base`` 时不覆盖。
    - **空值不解释**：字段为空串时保持既有兜底（``config.VIDEO_BASE_PATH``），
      绝不把"没设置过"当成"根目录 = 当前工作目录"。
    """
    if 'video_base' in boundaries.explicit_fields:
        return
    value = getattr(settings, 'video_path', None)
    if not isinstance(value, str) or not value.strip():
        return
    try:
        boundaries.video_base = str(Path(value).expanduser())
    except (OSError, ValueError, TypeError):
        logger.warning('设置里的视频目录无法解释，沿用既有视频根：%r', value)


def _settings_snapshot(settings: Any) -> Dict[str, Any]:
    """启动时刻的少量设置快照（仅供媒体/时长口径参考；不复制成第二份配置）。"""
    return {
        'ffmpeg_path': getattr(settings, 'ffmpeg_path', 'ffmpeg'),
        'stream_mode': getattr(settings, 'stream_mode', 'manual'),
        'ffmpeg_reencode': bool(getattr(settings, 'ffmpeg_reencode', True)),
        'scan_interval_seconds': int(getattr(settings, 'scan_interval_seconds', 30)),
    }


# ==================== 账号凭据（纯能力） ====================

def read_account_context(*, data_dir: Path, clock,
                         boundaries: BoundaryOverrides) -> Tuple[
                             Optional[AccountContext], Dict[str, str], str]:
    """从凭据文件读出**已登录**的账号上下文（纯读，不访问平台、不关房间）。

    返回 ``(account, credentials, reason)``。凭据缺失/不完整 → ``(None, {}, 原因)``，
    绝不伪造 UID/房间。``display_name`` 只在凭据里已经缓存过时才带上。
    """
    from ..live_controller import BilibiliApi
    if boundaries.account is not None:
        account = boundaries.account
        return account, dict(boundaries.credentials or {}), ''
    cookie_file = Path(data_dir) / 'bili_cookies.json'
    if not cookie_file.is_file():
        return None, {}, 'no_credentials'
    api = BilibiliApi(cookie_file=str(cookie_file))
    cookies = dict(getattr(api, 'cookies', {}) or {})
    if not cookies.get('SESSDATA'):
        return None, {}, 'no_session'
    uid = _as_positive_int(cookies.get('DedeUserID') or cookies.get('uid'))
    room_id = _as_positive_int(cookies.get('room_id') or cookies.get('roomId'))
    if uid is None:
        return None, {}, 'no_uid'
    if room_id is None:
        # 房间 ID 未缓存时**不**伪造：实例留空，认证路由可在登录后补齐。
        return None, {}, 'no_room_id'
    display_name = str(cookies.get('uname') or cookies.get('display_name') or '')
    account = AccountContext(uid=uid, room_id=room_id, display_name=display_name)
    return account, cookies, ''


def _as_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def recover_room_id(*, data_dir: Path, clock, boundaries: BoundaryOverrides,
                    uid: int, budget_seconds: float = 10.0) -> Optional[int]:
    """旧版只存了 ``SESSDATA/bili_jct/DedeUserID`` 时，**有界**补一次房间号。

    这是"不要求人工编辑 cookies"的那条恢复路径：只用既有单轮接缝问一次平台，问不到
    就如实返回 ``None``（不伪造房间）。成功后的写入由调用方用同一份凭据完成。
    """
    from ..live_controller import BilibiliApi
    if int(uid or 0) <= 0:
        return None
    api = BilibiliApi(cookie_file=str(Path(data_dir) / 'bili_cookies.json'))
    budget = RemainingBudget(float(clock.mono()) + float(budget_seconds), clock)
    try:
        with api.bounded_attempt(budget, boundaries.endpoint_base,
                                 BoundedHttpSender()):
            ok, payload = api.get_room_id_by_uid(int(uid))
    except Exception:                                     # noqa: BLE001
        return None
    if not ok or not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if not isinstance(data, dict):
        return None
    return _as_positive_int(data.get('room_id') or data.get('roomid'))


def resolve_account_context(*, data_dir: Path, clock,
                            boundaries: BoundaryOverrides
                            ) -> Tuple[Optional[AccountContext],
                                       Dict[str, str], str]:
    """启动读取账号上下文的**唯一**入口：纯读优先，旧凭据按需有界补齐房间号。

    只在"凭据合法但缺房间号"时补一次；补到的房间号与已有凭据一起原子写回，因此
    下一次启动就是普通可信读取。任何失败都如实返回未登录原因，不伪造身份。
    """
    account, credentials, reason = read_account_context(
        data_dir=data_dir, clock=clock, boundaries=boundaries)
    if account is not None or reason != 'no_room_id':
        return account, credentials, reason

    cookies_path = Path(data_dir) / 'bili_cookies.json'
    try:
        import json
        document = json.loads(cookies_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, {}, 'no_room_id'
    cookies = document.get('cookies') if isinstance(document, dict) else None
    if not isinstance(cookies, dict):
        return None, {}, 'no_room_id'
    uid = _as_positive_int(cookies.get('DedeUserID') or cookies.get('uid'))
    if uid is None:
        return None, {}, 'no_uid'

    room_id = recover_room_id(data_dir=data_dir, clock=clock,
                              boundaries=boundaries, uid=uid)
    if room_id is None:
        logger.info('旧凭据缺少房间号且本轮未能补齐：保持未登录（可重新扫码）')
        return None, {}, 'no_room_id'
    from ..live_controller import BilibiliApi
    api = BilibiliApi(cookie_file=str(cookies_path))
    payload = dict(cookies)
    payload['room_id'] = str(room_id)
    if not api.replace_cookies(payload):
        logger.warning('补齐的房间号未能写入凭据文件；本次仍按已核验上下文继续')
    return read_account_context(data_dir=data_dir, clock=clock,
                                boundaries=boundaries)
