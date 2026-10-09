"""真实媒体端口：本实例自有 FFmpeg 子进程的**唯一一份**资源权威。

它是 3A 里 ``StandinMediaPort`` 的真实实现（见同目录 ``media_port.py`` 的上游说明与
``docs/plans/live-session-core-reset-20260930/PHASE3B-DESIGN.md``）：

- **一份记录**：每个实例一份 ``MediaInstance``，持有 Windows Job Object 与一份只记
  **物理事实**的归属记录（实例标识、物理身份、出生证据、逻辑 Run 绑定）。记录里**没有**
  Goal、有效时长、平台请求、业务 Step 或重试次数 —— 它不是第二个 Run，也不是状态机。
- **一份调和**：``handle`` 对每条 Sync/Reclaim 只做**一轮**，共享本轮绝对预算；端口内
  没有重试循环、没有"五次失败等五分钟"、也没有每次重试新线程。下一次尝试由 Core 的
  既有预约驱动。
- **一份事实**：可关闭的观察线程只产生"存活/输出"事实，经 ``PusherReport`` 送回
  Runtime。媒体输出**从不**进入业务计时，也不主动下播。

精确归属靠**真身份**，不靠 PID 单值、文件里一行 PID 或进程名：出生身份是
``(pid, creation_time)``，生命周期归属由**每实例一个 Job Object** 给出。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from ..ffmpeg_log_progress import (
    FFmpegLogProgressReader, capture_ffmpeg_log_baseline,
)
from ..session import effects as fx
from ..session.events import PusherReport
from .media_os import (
    OsOps, ProcessIdentity, SpawnUnavailable, SpawnUnreclaimed,
    default_os_ops, identity_is_conclusive,
)
from .media_playlist import MediaCommand, NoCompatibleVideos, NoVideosAvailable

logger = logging.getLogger(__name__)

#: 默认一轮媒体动作的工程预算（秒）。是本地健康的工程预算，不是平台/磁盘 SLA。
DEFAULT_ATTEMPT_BUDGET = 3.0
#: 首次输出观察窗口：刚启动不因"还没有输出"立刻杀进程重建（防启动自激）。
DEFAULT_STARTUP_OBSERVE = 5.0
#: 判定"已输出后停滞"的窗口。
DEFAULT_STALL_WINDOW = 3.0
#: 温和停止留给 OS 的片段，其余预算留给强制结束与核验。
DEFAULT_GENTLE_SLICE = 1.0
#: 观察线程的采样周期。
DEFAULT_WATCH_INTERVAL = 0.05
#: 单个对象自有日志的上限，超过即由端口自行轮转。
DEFAULT_LOG_MAX_BYTES = 4 * 1024 * 1024
#: 每次 tick 最多排空的 stderr 字节数。
DRAIN_CHUNK_BYTES = 64 * 1024
#: 每次 tick 最多增量读日志的次数（有界）。
READS_PER_TICK = 64

RECORD_STATE_CREATING = 'creating'
RECORD_STATE_OWNED = 'owned'

#: 记录声明的实例名分类（库存/重核/关闭**共用同一份**归属分类；返修 R4）。
#: ``''``＝无声明/读不出，不下结论，交回既有判据。
RECORD_INSTANCE_THIS = 'this'
RECORD_INSTANCE_LEGACY = 'legacy'
RECORD_INSTANCE_FOREIGN = 'foreign'

#: 旧公共媒体实例名（与 ``production.PRODUCTION_INSTANCE_ID`` 同值；语义唯一来源在
#: 此分类）。返修 R1 起新记录用媒体实例目录导出的标识；旧 ``prod`` 为所有部署共用，
#: 它既不能证明记录属于本实例，**也**不能证明记录属于别的实例 —— 归属**未核验**，
#: 不是"确证外来"（返修 R4）。
LEGACY_MEDIA_INSTANCE_ID = 'prod'


class UnsupportedMediaEffect(RuntimeError):
    """端口不认识的效果：明确报错，不静默丢弃。"""


class ResourceVerificationError(RuntimeError):
    """库存无法核验（记录损坏/权限不足）：禁止新 spawn，且不发送可信空库存。"""


# ---------------------------------------------------------------- 归属记录


@dataclass(frozen=True)
class RecordEntry:
    """一份**只记物理事实**的资源归属记录。"""

    pending_id: str
    physical_id: str
    pid: int
    creation_time: int
    run_id: str
    state: str
    created_wall: str = ''

    def to_dict(self) -> dict:
        return {
            'pending_id': self.pending_id,
            'physical_id': self.physical_id,
            'pid': int(self.pid),
            'creation_time': int(self.creation_time),
            'run_id': self.run_id,
            'state': self.state,
            'created_wall': self.created_wall,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional['RecordEntry']:
        if not isinstance(raw, dict):
            return None
        try:
            entry = cls(
                pending_id=str(raw['pending_id']),
                physical_id=str(raw.get('physical_id') or ''),
                pid=int(raw.get('pid') or 0),
                creation_time=int(raw.get('creation_time') or 0),
                run_id=str(raw.get('run_id') or ''),
                state=str(raw.get('state') or ''),
                created_wall=str(raw.get('created_wall') or ''))
        except (KeyError, TypeError, ValueError):
            return None
        if entry.state not in (RECORD_STATE_CREATING, RECORD_STATE_OWNED):
            return None
        if not entry.pending_id:
            return None
        return entry


class MediaRecordStore:
    """归属记录的原子读写。

    ``read()`` 返回 ``(entry, error)``：``error`` 非空代表**无法核验**（损坏/权限），
    此时调用方必须保留原字节、拒绝新 spawn，而不是当成"没有自有对象"。
    """

    def __init__(self, path) -> None:
        self.path = Path(path)

    def read(self) -> Tuple[Optional[RecordEntry], str]:
        if not self.path.exists():
            return None, ''
        try:
            raw_bytes = self.path.read_bytes()
        except OSError as exc:
            return None, f'unreadable:{type(exc).__name__}'
        try:
            document = json.loads(raw_bytes.decode('utf-8'))
        except Exception as exc:                      # noqa: BLE001
            return None, f'corrupt:{type(exc).__name__}'
        if not isinstance(document, dict) or int(document.get('version') or 0) != 1:
            return None, 'corrupt:schema'
        entry = RecordEntry.from_dict(document.get('entry'))
        if entry is None:
            return None, 'corrupt:entry'
        return entry, ''

    def write(self, entry: RecordEntry) -> bool:
        document = {'version': 1, 'entry': entry.to_dict()}
        temporary = self.path.with_suffix('.json.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, ensure_ascii=False),
                                 encoding='utf-8')
            os.replace(temporary, self.path)
            return True
        except OSError:
            return False

    def clear(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def raw_bytes(self) -> bytes:
        try:
            return self.path.read_bytes() if self.path.exists() else b''
        except OSError:
            return b''


# ---------------------------------------------------------------- 实例上下文


class MediaInstance:
    """一个实例的媒体生命周期上下文：Job Object ＋ 归属记录 ＋ 目录。

    它比单个 :class:`MediaPort` 活得久：端口重建（同一监督进程内）时沿用**同一个**
    ``MediaInstance``，因此已创建但业务 Run 已丢弃的对象仍可被核验与精确收尾。
    监督进程真正消亡时，``KILL_ON_JOB_CLOSE`` 让 OS 收回组内子进程。
    """

    def __init__(self, instance_id: str, instance_dir, *, os_ops: Optional[OsOps] = None,
                 log_max_bytes: int = DEFAULT_LOG_MAX_BYTES) -> None:
        self.instance_id = str(instance_id)
        self.dir = Path(instance_dir)
        self.media_dir = self.dir / 'media'
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.os_ops = os_ops or default_os_ops()
        self.job = self.os_ops.create_job()
        self.records = MediaRecordStore(self.media_dir / 'owned.json')
        self.log_max_bytes = int(log_max_bytes)

    @property
    def job_available(self) -> bool:
        return bool(self.job)

    def close(self) -> None:
        """实例拆卸：关闭 job（组内残留子进程由 OS 收回）。"""
        if self.job:
            self.os_ops.close_job(self.job)
            self.job = None


@dataclass(frozen=True)
class InventoryReport:
    """启动库存：本实例自有的存活对象。

    ``ok=False`` 表示**没能核验清楚**（记录损坏 / 存在无法核验逻辑绑定的自有对象）：
    此时端口禁止新 spawn，并把物理 token 一并交出以便 Core 安排回收。
    """

    owned: Tuple[str, ...] = ()
    unverified: Tuple[str, ...] = ()
    detail: str = ''
    job_available: bool = False

    @property
    def ok(self) -> bool:
        return not self.detail

    def trusted_empty(self) -> bool:
        return not self.owned and not self.unverified and not self.detail


@dataclass(frozen=True)
class MediaCloseReport:
    """一次媒体资源关闭的**实际**结果。未收回来的资源如实为假。"""

    closed: bool = False
    resources_cleared: bool = True
    readers_stopped: bool = True
    objects: int = 0
    alive_after: int = 0
    detail: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            'closed': self.closed,
            'resources_cleared': self.resources_cleared,
            'readers_stopped': self.readers_stopped,
            'objects': self.objects,
            'alive_after': self.alive_after,
            'detail': list(self.detail),
        }


# ---------------------------------------------------------------- 自有对象


class OwnedMedia:
    """端口当前持有的**唯一**一路自有媒体对象（资源对象，不是第二个 Run）。"""

    def __init__(self, *, instance_id: str, physical_id: str, pid: int,
                 creation_time: int, run_id: str, proc=None, pending_id: str = '',
                 log_path=None, cleanup_paths: Tuple[str, ...] = (),
                 end_authorized: bool = True) -> None:
        self.instance_id = str(instance_id)
        self.physical_id = str(physical_id)
        self.pending_id = str(pending_id)
        self.pid = int(pid)
        self.identity = ProcessIdentity(pid=self.pid, creation_time=int(creation_time))
        self.run_id = str(run_id)
        self.proc = proc
        self.log_path = Path(log_path) if log_path else None
        self.cleanup_paths = tuple(cleanup_paths)
        #: 是否**持有结束授权**（关闭出口据此决定发不发结束动作）。存在核算与结束
        #: 授权分开：由真实事实（自有引用、原生组归属、或记录与出生身份/存活的
        #: 确证一致）产生的对象才是 True；仅为"已知存在但未核验"的线索对象为
        #: False——含旧公共标识 ``prod`` 的记录线索（名字不能证明归属，返修 R4）——
        #: 关闭时如实保留、绝不因此发送结束动作（返修 R2/R3/R4）。
        self.end_authorized = bool(end_authorized)

        self.lock = threading.Lock()
        self.created_mono: Optional[float] = None
        self.last_media_seconds: Optional[float] = None
        self.last_progress_mono: Optional[float] = None
        self.first_output_mono: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.watcher: Optional['OwnedMediaWatcher'] = None
        self.last_reported: Optional[Tuple[bool, bool]] = None

    # ---------- 事实 ----------

    def alive(self, os_ops: OsOps) -> bool:
        if self.proc is not None:
            return os_ops.is_alive(self.proc)
        return os_ops.pid_alive(self.pid)

    def note_progress(self, media_seconds: float, now_mono: float) -> None:
        with self.lock:
            if (self.last_media_seconds is None
                    or media_seconds > self.last_media_seconds):
                self.last_media_seconds = media_seconds
                self.last_progress_mono = now_mono
                if self.first_output_mono is None:
                    self.first_output_mono = now_mono

    def outputting(self, now_mono: float, stall_window: float) -> bool:
        with self.lock:
            if self.proc is None:
                # 恢复来的对象没有本站观察器：只有**真观察到**才敢说失效。
                # 未观察到 = 未知，不当作"停滞"（不把未知当确定失败）。
                return True
            if self.last_progress_mono is None:
                return False
            return (now_mono - self.last_progress_mono) <= float(stall_window)


class OwnedMediaWatcher(threading.Thread):
    """一个对象的可关闭观察线程：排空 stderr、解析本对象媒体时间、采样存活。

    职责只有**存活与输出事实**：不 spawn、不轮转业务、不下播、不记账、不写业务 JSON。
    订阅读取器数量固定为**每对象一个**；对象释放即退出。
    """

    def __init__(self, port: 'MediaPort', owned: OwnedMedia, *,
                 interval: float, stall_window: float, log_max_bytes: int,
                 os_ops: OsOps, name: str) -> None:
        super().__init__(name=name, daemon=True)
        self._port = port
        self._owned = owned
        self._os = os_ops
        self._interval = max(0.005, float(interval))
        self._stall_window = float(stall_window)
        self._log_max = int(log_max_bytes)
        self._stop_event = threading.Event()
        self.exited = threading.Event()
        self._handle = None
        self._reader = None
        self.last_error = ''

    # ---------- 生命周期 ----------

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._open_log()
            while not self._stop_event.is_set():
                self._drain_and_read()
                alive = self._port._is_alive(self._owned)
                if not alive:
                    self._drain_and_read()
                    self._port._on_object_unexpected_exit(self._owned)
                    return
                self._emit_if_changed(True)
                self._stop_event.wait(self._interval)
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f'{type(exc).__name__}: {exc}'
            logger.warning('媒体观察线程异常退出：%s', self.last_error)
        finally:
            self._close_log()
            self.exited.set()

    # ---------- 日志与进度 ----------

    def _open_log(self) -> None:
        path = self._owned.log_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        self._handle = path.open('ab', buffering=0)
        self._reader = FFmpegLogProgressReader(
            path, capture_ffmpeg_log_baseline(path, start_offset=0))

    def _close_log(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        path = self._owned.log_path
        if path is None or self._handle is None:
            return
        try:
            if self._handle.tell() <= self._log_max:
                return
        except OSError:
            return
        self._close_log()
        try:
            rotated = path.with_suffix(path.suffix + '.1')
            os.replace(path, rotated)
        except OSError:
            pass
        self._open_log()

    def _drain_and_read(self) -> None:
        if self._handle is not None:
            try:
                chunk = self._os.read_available(self._owned.proc,
                                                DRAIN_CHUNK_BYTES)
            except Exception:                         # noqa: BLE001
                chunk = b''
            if chunk:
                try:
                    self._handle.write(chunk)
                except OSError:
                    pass
        if self._reader is None:
            return
        for _ in range(READS_PER_TICK):
            try:
                reading = self._reader.read()
            except Exception as exc:                  # noqa: BLE001
                self.last_error = f'reader:{type(exc).__name__}: {exc}'
                return
            if reading.outcome == 'available' and reading.media_seconds is not None:
                self._owned.note_progress(reading.media_seconds,
                                          self._port._now())
            if reading.bytes_read == 0 and reading.outcome != 'new_baseline':
                break
        self._rotate_if_needed()

    def _emit_if_changed(self, alive: bool) -> None:
        self._port._maybe_report(self._owned, alive, observed=True)

    def facts(self) -> dict:
        with self._owned.lock:
            return {
                'media_seconds': self._owned.last_media_seconds,
                'first_output': self._owned.first_output_mono is not None,
            }


# ---------------------------------------------------------------- 端口


class _MonotonicClock:
    __slots__ = ()

    def mono(self) -> float:
        return time.monotonic()


def _physical_id(instance_id: str, pid: int, creation_time: int) -> str:
    return f'media:{instance_id}:{pid}:{creation_time}'


def _safe_filename(name: str) -> str:
    """把物理标识变成合法文件名（Windows 不允许 ``:``）。"""
    return ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in str(name))


class MediaPort:
    """本实例自有推流的一轮调和端口。构造后可直接交给 Runtime 的媒体工作者。"""

    def __init__(self, instance: MediaInstance, command_builder, *,
                 attempt_budget_seconds: float = DEFAULT_ATTEMPT_BUDGET,
                 startup_observe_seconds: float = DEFAULT_STARTUP_OBSERVE,
                 stall_window_seconds: float = DEFAULT_STALL_WINDOW,
                 gentle_slice_seconds: float = DEFAULT_GENTLE_SLICE,
                 watch_interval_seconds: float = DEFAULT_WATCH_INTERVAL,
                 clock=None, name: str = 'p3b') -> None:
        self._instance = instance
        self._builder = command_builder
        self._os = instance.os_ops
        self._attempt_budget = float(attempt_budget_seconds)
        self._startup_observe = float(startup_observe_seconds)
        self._stall_window = float(stall_window_seconds)
        self._gentle_slice = max(0.0, float(gentle_slice_seconds))
        self._watch_interval = float(watch_interval_seconds)
        self._clock = clock or _MonotonicClock()
        self._name = name

        self._lock = threading.RLock()
        self._owned: Optional[OwnedMedia] = None
        self._sink: Optional[Callable[[Any], None]] = None
        self._closing = False
        self._spawn_blocked = ''
        self._verified = False
        self._watch_seq = 0

        #: 证据计数（只读，不参与任何决策）
        self.spawns = 0
        self.os_errors = 0
        self._last_spawn_error = ''
        self.reclaims = 0
        self.reports = 0
        self.recovery_attempts = 0
        self.bindings = 0

    # ---------------------------------------------------------------- 时间

    def _now(self) -> float:
        return float(self._clock.mono())

    def _remaining(self, deadline: float) -> float:
        return max(0.0, float(deadline) - self._now())

    # ---------------------------------------------------------------- 装配

    def attach_sink(self, sink: Callable[[Any], None]) -> None:
        """只用于把事实送回 Runtime；端口绝不通过它发起业务动作。"""
        self._sink = sink

    def _emit(self, event: Any) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception as exc:                      # noqa: BLE001
            logger.warning('媒体事实投递失败：%s', exc)

    # ---------------------------------------------------------------- 库存

    def prepare_verified_inventory(self) -> InventoryReport:
        """在**任一 spawn 之前**完成库存核验；失败明确返回，绝不造可信空表。

        判据只有两条，且**同一份**用于记录与原生库存：出生身份是否**确证**、以及
        进程是否在**本实例的生命周期组**里。读不出身份 ≠ 已退出 ≠ 不是我们的；
        只有"确证退出""确证身份不匹配""确证不在本实例组内"才允许判为过期。

        原生组枚举本身失败时**不再往下走**：那既不是"确证空组"，也不是"该 OS 本来
        不支持 Job"，而是**不可核验** —— 不认领、不清记录、不放引用、不发结束动作、
        不放行第二路，只如实交出失败事实。
        """
        job_available = self._instance.job_available
        job_pids: set = set()
        group_error = ''
        if job_available:
            try:
                job_pids = set(self._os.job_pids(self._instance.job))
            except Exception as exc:                  # noqa: BLE001
                self.os_errors += 1
                group_error = (
                    f'枚举实例生命周期组失败（{type(exc).__name__}: {exc}）：'
                    '归属不可核验，禁止新 spawn')
                logger.warning('%s', group_error)
        if group_error:
            self._spawn_blocked = group_error
            self._verified = True
            return InventoryReport(detail=group_error,
                                   job_available=job_available)

        entry, error = self._instance.records.read()
        detail_parts: List[str] = []
        owned_tokens: List[str] = []
        unverified: List[str] = []
        #: 已经**真的被计入**报告的 PID（认领为 owned 或保留为 unverified）。
        #: 只有它们才能挡住下面"Job 里还有、记录没覆盖到"的补充 —— 拿"记录里出现过
        #: 这个 PID"当挡箭牌，会让被判过期/他人的记录把一个**真实的原生成员**从报告
        #: 里抹掉，凑出可信空库存。
        surfaced_pids = set()

        if error:
            detail_parts.append(f'归属记录不可核验（{error}）：保留原字节，禁止新 spawn')

        if entry is not None:
            identity = self._safe_identity(entry.pid) if entry.pid else None
            conclusive = identity_is_conclusive(identity)
            liveness = self._pid_liveness(entry.pid) if entry.pid else False
            matches = bool(conclusive and identity is not None and identity.matches(
                ProcessIdentity(pid=entry.pid,
                                creation_time=entry.creation_time)))
            member = bool(job_available and entry.pid in job_pids)
            #: 记录声明的实例名分类（与重核/关闭**同一份**判据）：``foreign``＝确证
            #: 可区分的别的实例；``legacy``＝旧公共标识，归属**未核验**（返修 R4）。
            declared = self._declared_instance_class(entry.physical_id)
            #: **确证**不归属本实例：本 OS 有组时看组（记录说它在、实际不在）；
            #: 或记录明确声明**可区分**的别的实例名。旧公共标识不在其列 —— 改名本身
            #: 不是新获得的物理否定事实，"旧名字"证明不了记录是别人的。
            foreign = bool(
                (job_available and entry.pid and not member)
                or declared == RECORD_INSTANCE_FOREIGN)
            #: 归属由**真实事实**承重：存活**确证**在跑 ＋ 出生身份确证 ＋
            #: （在本实例组内 或 本 OS 无组）。未知读取不参与证明，因此"读不出"
            #: 不能变成认领理由 —— 保留记录线索是一回事，把 PID 放进 ``_owned``
            #: （＝授予结束权限）是另一回事。
            ours = bool(liveness and matches and (member or not job_available))
            if liveness is False:
                # 确证已退出（含尚未 reaped 的僵尸）或压根没有进程：可证明的过期记录。
                # 注意判据是 **is False**：``None``（读不出）不是"已退出"。
                self._instance.records.clear()
            elif conclusive and not matches:
                # 出生身份**确证**对不上：PID 被无关进程复用，不是我们那一份。
                self._instance.records.clear()
            elif foreign:
                # **确证不在本实例生命周期组**里：记录说明不了它属于本实例，因此
                # 不认领、不授予结束权限（另一实例的真实对象）。这条**必须先于**任何
                # "未知"分支 —— 一个未知事实不能把已确证的"不归属"翻回来。
                self._instance.records.clear()
                logger.info('库存记录不在本实例生命周期组内：不认领、不发结束动作')
            elif declared == RECORD_INSTANCE_LEGACY and not job_available:
                # 无组平台上的旧公共标识：归属**未核验** —— 不认领、不授予结束权限，
                # 但记录原字节必须保留并如实列入未核验：名字换了既不能证明是自己的，
                # 也不能证明是别人的，不发明"确证外来"（返修 R4）。原生组读得出时不
                # 走这里：组是权威，成员/否定按既有判据处理。
                token = entry.physical_id or _physical_id(
                    self._instance.instance_id, entry.pid, entry.creation_time)
                unverified.append(token)
                surfaced_pids.add(entry.pid)
                # 记录**原样留在盘上**（不 clear）：它仍是这个对象唯一的存在线索。
            elif ours:
                # 真实存活、出生身份确证、归属也被原生组证明：我们那一份对象。
                self._adopt_recovered(entry)
                owned_tokens.append(entry.run_id or entry.physical_id)
                surfaced_pids.add(entry.pid)
            else:
                # 剩下的都是**未核验**（存活读不出、或出生身份读不出）：保留记录与
                # 引用线索是事实，但**不**把记录里的 PID 放进 ``_owned`` —— 认领需要
                # 上面的真实事实，未知本身不是授权。
                token = entry.physical_id or _physical_id(
                    self._instance.instance_id, entry.pid, entry.creation_time)
                unverified.append(token)
                surfaced_pids.add(entry.pid)
                # 记录**原样留在盘上**（不 clear）：它仍是这个对象唯一的逻辑绑定线索。

        # job 里还有、记录没覆盖到的自有进程：它天然属于本实例，保留事实。
        for pid in sorted(job_pids):
            if pid in surfaced_pids:
                continue
            identity = self._safe_identity(pid)
            creation = identity.creation_time if identity_is_conclusive(identity) \
                else 0
            token = _physical_id(self._instance.instance_id, pid, creation)
            unverified.append(token)
            if self._owned is None:
                self._adopt_recovered(RecordEntry(
                    pending_id='job-adopted', physical_id=token, pid=pid,
                    creation_time=creation, run_id=token,
                    state=RECORD_STATE_OWNED))

        if unverified:
            detail_parts.append(
                f'存在 {len(unverified)} 个无法核验逻辑绑定的对象：禁止新 spawn，先收尾')
        if detail_parts:
            self._spawn_blocked = '；'.join(detail_parts)
        else:
            self._spawn_blocked = ''
        self._verified = True
        return InventoryReport(
            owned=tuple(owned_tokens + unverified),
            unverified=tuple(unverified),
            detail='；'.join(detail_parts),
            job_available=job_available)

    def _pid_liveness(self, pid: int) -> Optional[bool]:
        """存活事实的三态读取：``True`` / ``False`` / ``None``（读不出）。

        ``None`` **不等于**"已退出"：它只是"没读到"。调用方必须把"读不出"当作未核验
        保留，而不是据此清记录、放引用或放行第二路。
        """
        try:
            return bool(self._os.pid_alive(int(pid)))
        except Exception as exc:                      # noqa: BLE001
            self.os_errors += 1
            logger.warning('读取 PID 存活失败（%s）：按不可核验处理，不当作已退出', exc)
            return None

    def _safe_identity(self, pid: int):
        try:
            return self._os.pid_identity(int(pid))
        except Exception as exc:                      # noqa: BLE001
            self.os_errors += 1
            logger.warning('读取进程出生身份失败（%s）', exc)
            return None

    def _adopt_recovered(self, entry: RecordEntry) -> None:
        self._adopt_facts(pid=entry.pid, creation_time=entry.creation_time,
                          run_id=entry.run_id or entry.physical_id,
                          physical_id=entry.physical_id)

    def _adopt_facts(self, *, pid: int, creation_time: int, run_id: str,
                     physical_id: str = '') -> 'OwnedMedia':
        """把一份**已经确证**的真实对象事实放进唯一的资源槽（``_owned``）。

        这是全端口**唯一**的"存在"核算槽：认领（＝持有引用与结束权限）只由真实事实
        触发，调用方负责给出那些事实。不新增第二份台账、不写业务状态。
        """
        with self._lock:
            if self._owned is not None:
                return self._owned
            resolved = physical_id or _physical_id(
                self._instance.instance_id, pid, creation_time)
            self._owned = OwnedMedia(
                instance_id=self._instance.instance_id, physical_id=resolved,
                pid=int(pid), creation_time=int(creation_time),
                run_id=str(run_id) if run_id else resolved, proc=None,
                log_path=self._instance.media_dir / (
                    f'{_safe_filename(physical_id)}.log')
                if physical_id else None)
            return self._owned

    def _retain_created(self, effect: fx.SyncPusher, exc: BaseException,
                        pending_id: str) -> None:
        """把"原生创建成功、未能移交"的真实对象交给**同一份**资源核算。

        与 :meth:`_adopt_facts` 走同一个槽（``_owned``）与同一份归属记录：因此创建门
        （``_owned is not None``）、回收与 close 都按同一份事实走，不会因为"引用为空"
        就把真实对象漏出报告。记录同时被改写成真实的 PID/出生身份，取代 ``creating``
        与 ``pid=0`` —— 后者是**文字**，不是可继续处理的资源引用。
        """
        retained = getattr(exc, 'retained', None)
        if retained is None:
            # 连持有者都没拿到：只保留现场事实（creating 记录 + 阻断），并**不**声称
            # 已经清理。这是本出口唯一无法交出资源引用的情形。
            logger.error('原生创建后未能取得可收尾的资源引用：仅保留现场与阻断')
            return
        pid = int(getattr(retained, 'pid', 0) or 0)
        identity = self._safe_identity(pid) if pid else None
        creation_time = identity.creation_time \
            if identity_is_conclusive(identity) else 0
        physical_id = _physical_id(self._instance.instance_id, pid, creation_time)
        with self._lock:
            if self._owned is not None:
                return                                # 复核：同一实例最多一路
            owned = OwnedMedia(
                instance_id=self._instance.instance_id, physical_id=physical_id,
                pid=pid, creation_time=creation_time, run_id=effect.run_id,
                proc=retained, pending_id=pending_id,
                log_path=self._instance.media_dir
                / f'{_safe_filename(physical_id)}.log')
            owned.created_mono = self._now()
            self._owned = owned
        registered = self._instance.records.write(RecordEntry(
            pending_id=pending_id, physical_id=physical_id, pid=pid,
            creation_time=creation_time, run_id=effect.run_id,
            state=RECORD_STATE_OWNED,
            created_wall=datetime.now().isoformat(timespec='seconds')))
        if not registered:
            self._last_spawn_error = '保留对象登记失败：已保留真实对象，只记事实'
            logger.error('%s', self._last_spawn_error)
        logger.info('原生创建成功但未完成移交：真实对象已进入资源核算（PID=%s）', pid)

    # ---------------------------------------------------------------- 调和

    def handle(self, effect: Any) -> List[Any]:
        if isinstance(effect, fx.SyncPusher):
            return self._sync(effect)
        if isinstance(effect, fx.ReclaimPusher):
            return self._reclaim(effect)
        raise UnsupportedMediaEffect(f'媒体端口不认识的效果：{effect!r}')

    def _sync(self, effect: fx.SyncPusher) -> List[Any]:
        deadline = self._now() + self._attempt_budget
        owned, failure = self._verified_snapshot()
        if failure:
            return [self._resource_failure_report(effect.run_id, failure)]

        if owned is not None and self._is_alive(owned):
            if owned.run_id != effect.run_id:
                self._bind(owned, effect.run_id)
            elif self._needs_recovery(owned):
                # 同一路活着但持续没有输出：**一次**有预算的精确恢复尝试。
                self.recovery_attempts += 1
                self._reclaim_object(owned, deadline)
            return [self._report(owned)]

        if owned is not None:
            if not self._release(owned, deadline):
                # 还没真正释放（观察线程未退出）：保留引用，本轮**不**另起第二路。
                return [PusherReport(run_id=effect.run_id, alive=False,
                                     outputting=False)]

        created = self._spawn(effect, deadline)
        if created is None and self._spawn_blocked:
            return [self._resource_failure_report(effect.run_id,
                                                  self._spawn_blocked)]
        if created is None:
            return [PusherReport(run_id=effect.run_id, alive=False,
                                 outputting=False)]
        return [self._report(created)]

    def _reclaim(self, effect: fx.ReclaimPusher) -> List[Any]:
        deadline = self._now() + self._attempt_budget
        owned, failure = self._verified_snapshot()
        if failure:
            return [self._resource_failure_report(effect.run_id, failure)]
        if owned is None:
            # ``_owned`` 为空**不等于**本实例没有已知存在的对象：库存未核验、或原生
            # 创建后未完成移交的对象都还真实存在。沿既有归属事实在**本轮内**重新核验，
            # 而不是直接回一条"已退出"（那会让 Core 划掉真正还在的库存）。
            owned, matched, unknown = self._reverify_retained(effect.run_id)
            if owned is None:
                if matched and unknown:
                    # 已知存在、但本轮**确证不了**是否已退出：如实保留，不伪造 gone。
                    return [PusherReport(run_id=effect.run_id, alive=True,
                                         outputting=False)]
                return [PusherReport(run_id=effect.run_id, alive=False,
                                     outputting=False)]
        if effect.run_id not in (owned.run_id, owned.physical_id):
            # 旧 Run / 旧对象 / 无关对象的请求：不动当前新对象，也不冒充它退出。
            return []
        self.reclaims += 1
        self._reclaim_object(owned, deadline)
        return [self._report(owned)]

    # ---------------------------------------------------------------- 资源核算

    def _declared_instance_class(self, physical_id: Any) -> str:
        """记录**自己声明的**实例名分类（库存/重核/关闭共用同一份归属事实）。

        ``media:<instance>:<pid>:<creation>`` 的前缀就是记录自己的声明：

        - ``this``：声明本实例名 —— 可作"自有"证据参与既有确证（仍要出生身份/存活）；
        - ``legacy``：声明旧公共标识 ``prod``（返修前所有部署共用）。名字换了并不能
          得到确证：它既不能证明记录属于本实例，**也**不能证明记录属于别的实例 ——
          归属**未核验**，只按存在线索处理（返修 R4）；
        - ``foreign``：声明**可区分的别的**实例名 —— 确证外来（不认领、不授权）；
        - ``''``：物理编号不带 ``media:`` 实例声明（例如 ``creating`` 中间态）——
          不下否定结论，交回既有判据。
        """
        text = str(physical_id or '')
        if not text.startswith('media:'):
            return ''
        declared = text[len('media:'):].partition(':')[0]
        if declared == self._instance.instance_id:
            return RECORD_INSTANCE_THIS
        if declared == LEGACY_MEDIA_INSTANCE_ID:
            return RECORD_INSTANCE_LEGACY
        return RECORD_INSTANCE_FOREIGN

    def _job_members(self) -> Tuple[Tuple[int, ...], str]:
        """实例生命周期组的**原生**成员枚举（现算）。返回 ``(pids, error)``。

        ``error`` 非空代表**读不出**（组查询失败）—— 那不是"确证空组"，调用方据此
        **不得**报"资源已清"。
        """
        if not self._instance.job_available:
            return (), ''
        try:
            return tuple(self._os.job_pids(self._instance.job)), ''
        except Exception as exc:                      # noqa: BLE001
            self.os_errors += 1
            message = (f'枚举实例生命周期组失败（{type(exc).__name__}: {exc}）：'
                       '本实例原生库存不可核验')
            logger.warning('%s', message)
            return (), message

    def _accountable_objects(self) -> Tuple[List[OwnedMedia], str, str]:
        """本实例当前要为哪些**真实对象**负责（每次现算，不是第二份台账）。

        返回 ``(objects, group_error, record_error)``。**存在核算与结束授权分开**：

        - ``objects`` 回答"存在"：``_owned`` 持有的自有对象（含原生创建成功但未完成
          移交的保留对象）、实例生命周期组的**原生成员**、以及无法用原生组排除的
          记录线索；每个对象的 ``end_authorized`` 回答"谁可以结束"。
        - 结束授权只由真实事实给出：自有引用、原生组归属、或（无组平台上）记录与
          出生身份确证一致且存活确证在跑。记录里的 PID/生日/自称实例前缀**都不是**
          原生组的授权（返修 R3），身份/存活读不出也不授予（返修 R2）；旧公共标识
          ``prod`` 的线索对象**永**不授予结束权限（名字不能证明归属，返修 R4）。

        归属否认的判据：

        - 原生组**读得出**时，组是权威来源：组成员由下方循环覆盖；记录声明的 PID
          不在组里就是**原生否定**，不被记录内容翻回（Windows 回归 R3）。
        - 原生组**读不出**（无组平台或枚举失败）时，记录是"已创建但引用不在手上"
          的对象唯一的存在线索：写着**可区分的**别的实例、确证 PID 复用、或确证已
          退出的都不计入；其余（含旧公共标识 ``prod`` —— 改名不是否定事实）按
          "存在但未核验"计入 —— 阻止 close 谎报"已清理"，但**不**获得结束授权。
        """
        objects: List[OwnedMedia] = []
        with self._lock:
            owned = self._owned
        if owned is not None:
            objects.append(owned)
        seen = {owned.pid} if owned is not None else set()
        pids, group_error = self._job_members()
        group_known = bool(self._instance.job_available) and not group_error
        entry, record_error = self._instance.records.read()
        if entry is not None and entry.pid and entry.pid not in seen:
            declared = self._declared_instance_class(entry.physical_id)
            if declared != RECORD_INSTANCE_FOREIGN:
                identity = self._safe_identity(entry.pid)
                conclusive = identity_is_conclusive(identity)
                matches = bool(conclusive and identity.matches(
                    ProcessIdentity(pid=entry.pid,
                                    creation_time=entry.creation_time)))
                liveness = self._pid_liveness(entry.pid)
                expired = bool(liveness is False or (conclusive and not matches))
                if not expired and not group_known:
                    # 授权与计入分开：只有在"记录不假、身份匹配、存活确证、且本平台
                    # 没有原生组可作权威"时才授予结束权限；其余一律按存在线索计入。
                    # 旧公共标识只作存在线索：**永不**授予结束权限（返修 R4）。
                    if declared == RECORD_INSTANCE_LEGACY:
                        authorized = False
                    else:
                        authorized = bool(matches and liveness is True
                                          and not self._instance.job_available)
                    seen.add(entry.pid)
                    objects.append(OwnedMedia(
                        instance_id=self._instance.instance_id,
                        physical_id=entry.physical_id or _physical_id(
                            self._instance.instance_id, entry.pid,
                            entry.creation_time),
                        pid=entry.pid,
                        creation_time=identity.creation_time if conclusive
                        else entry.creation_time,
                        run_id=entry.run_id or entry.physical_id, proc=None,
                        end_authorized=authorized))
        if pids:
            entry, _record_error = self._instance.records.read()
            for pid in pids:
                if pid in seen:
                    continue
                seen.add(pid)
                identity = self._safe_identity(pid)
                creation = identity.creation_time \
                    if identity_is_conclusive(identity) else 0
                physical_id = _physical_id(self._instance.instance_id, pid,
                                           creation)
                run_id = entry.run_id if (entry is not None and entry.pid == pid
                                          and entry.run_id) else physical_id
                objects.append(OwnedMedia(
                    instance_id=self._instance.instance_id,
                    physical_id=physical_id, pid=pid, creation_time=creation,
                    run_id=run_id, proc=None))
        return objects, group_error, record_error

    def _reverify_retained(self, token: str) -> Tuple[Optional[OwnedMedia], bool, bool]:
        """``_owned`` 为空时，沿**既有归属事实**在本轮内重新核验一个已知自有对象。

        返回 ``(owned, matched, unknown)``：

        - 确认是**本实例**的、且**确证仍在跑** → 认领并返回 ``OwnedMedia``；
        - **确证**已退出 / 出生身份被复用 / 不在本实例生命周期组 → ``(None, True, False)``
          —— 可以如实说"不在"；
        - 已知存在但本轮读不出 → ``(None, True, True)``，调用方**不得**据此报"已退出"。

        哪些否定才成立，取决于**这一步读出了什么**：出生身份、存活、组归属各自都要
        "读成功"才谈得上"确证不符"。**组枚举读不出**（``job_error``）既不是"确证空组"、
        也不是"确证外来"，它只能走未知 —— 把读不出当成否定分支，就是换个地方伪造退出。

        认领（＝授予结束权限）只由真实事实承重：外来对象与 PID 复用在这里**得不到**任何
        授权，与库存分类同一条规则；旧公共标识的候选同样不认领，只保留"已知存在"
        事实（返修 R4）。
        """
        entry, _record_error = self._instance.records.read()
        pids, job_error = self._job_members()
        job_available = self._instance.job_available
        #: 组归属**读成功**才允许用"在不在组里"下否定结论。
        group_known = bool(job_available) and not job_error

        candidates: List[Tuple[int, int, str, str]] = []
        if entry is not None and entry.pid:
            candidates.append((entry.pid, entry.creation_time,
                               entry.run_id or entry.physical_id,
                               entry.physical_id))
        for pid in pids:
            if entry is not None and pid == entry.pid:
                continue                              # 记录已经覆盖它，不重复放
            identity = self._safe_identity(pid)
            creation = identity.creation_time \
                if identity_is_conclusive(identity) else 0
            physical_id = _physical_id(self._instance.instance_id, pid, creation)
            candidates.append((pid, creation, physical_id, physical_id))

        matched = False
        unknown = False
        for pid, creation, run_id, physical_id in candidates:
            if token not in (run_id, physical_id):
                continue
            matched = True
            declared = self._declared_instance_class(physical_id)
            if declared == RECORD_INSTANCE_FOREIGN:
                return None, True, False              # 记录明确属于可区分的别的实例：不动手
            member = bool(group_known and pid in pids)
            if group_known and not member:
                return None, True, False              # 确证不在本实例组内：外来对象
            identity = self._safe_identity(pid)
            conclusive = identity_is_conclusive(identity)
            matches = bool(conclusive and identity.matches(
                ProcessIdentity(pid=pid, creation_time=creation)))
            if conclusive and not matches:
                return None, True, False              # 确证 PID 复用
            liveness = self._pid_liveness(pid)
            if liveness is False:
                return None, True, False              # 确证已退出
            if liveness and matches and (member or not job_available):
                if declared == RECORD_INSTANCE_LEGACY and not member:
                    #: 旧公共标识＋没有原生归属承重：名字不能认领（返修 R4）——
                    #: 只如实保留"已知存在、本轮未核验"，交由既有出口/操作者确认。
                    unknown = True
                    continue
                adopted = self._adopt_facts(
                    pid=pid, creation_time=creation, run_id=run_id,
                    physical_id=physical_id)
                return adopted, True, False
            unknown = True                            # 已知存在但读不出
        return None, matched, unknown

    # ---------------------------------------------------------------- 事实

    def _verified_snapshot(self) -> Tuple[Optional[OwnedMedia], str]:
        with self._lock:
            owned = self._owned
        if owned is None:
            return None, ''
        if owned.proc is None and identity_is_conclusive(owned.identity):
            current = self._safe_identity(owned.pid)
            if current is not None and identity_is_conclusive(current) \
                    and not current.matches(owned.identity):
                # PID 被无关进程**确证**复用：我们那一份已经不在了，绝不对新进程动手。
                self._release_reference_only(owned)
                return None, ''
            # 身份读不出（或读出来但没有创建时间）= 未核验，**不等于**已退出：
            # 保留引用，交给真实存活事实判定，绝不因此另起第二路。
        return owned, ''

    def _is_alive(self, owned: OwnedMedia) -> bool:
        """收口预期 OS 读取错误：读不出来时**保守判为存活**。

        这正是不能让 Runtime 的通用异常兜底把仍存活的对象报成不存在的地方：未知
        不等于已退出，否则会伪装成"已回收"并诱导第二路 spawn。
        """
        if owned is None:
            return False
        try:
            return bool(owned.alive(self._os))
        except Exception as exc:                      # noqa: BLE001
            self.os_errors += 1
            logger.warning('读取存活事实失败（%s）：保守判为存活，不伪装成已退出', exc)
            return self._conservative_alive(owned)

    @staticmethod
    def _conservative_alive(owned: OwnedMedia) -> bool:
        proc = owned.proc
        if proc is not None:
            try:
                return proc.poll() is None
            except Exception:                         # noqa: BLE001
                return True
        return True

    def _safe_terminate(self, pid: int, *, force: bool,
                        deadline: Optional[float] = None) -> bool:
        """精确结束请求：OS 拒绝或抛错都只是"没成功"，绝不当作已退出。

        ``deadline`` 是本轮**那一份**绝对截止：通过 ``termination_deadline`` 交给 OS 层，
        让结束动作只消费剩余时间（被测试注入层包裹的原语同样收到）。
        """
        try:
            scope = getattr(self._os, 'termination_deadline', None)
            if deadline is None or scope is None:
                return bool(self._os.terminate_tree(int(pid), force=force))
            with scope(deadline):
                return bool(self._os.terminate_tree(int(pid), force=force))
        except Exception as exc:                      # noqa: BLE001
            self.os_errors += 1
            logger.warning('精确结束请求失败（%s）', exc)
            return False

    def _needs_recovery(self, owned: OwnedMedia) -> bool:
        now = self._now()
        with owned.lock:
            created = owned.created_mono
            first = owned.first_output_mono
            last = owned.last_progress_mono
        if first is None:
            if created is None:
                return False
            return (now - created) > self._startup_observe
        if last is None:
            return False
        return (now - last) > self._stall_window

    def _bind(self, owned: OwnedMedia, run_id: str) -> None:
        """同一物理对象改绑新的逻辑 Run：不 spawn，内容暂不一致可接受。"""
        with owned.lock:
            owned.run_id = str(run_id)
        self.bindings += 1
        entry, _error = self._instance.records.read()
        if entry is not None and entry.physical_id == owned.physical_id:
            self._instance.records.write(replace(
                entry, run_id=str(run_id), state=RECORD_STATE_OWNED))

    def _report(self, owned: OwnedMedia) -> PusherReport:
        alive = self._is_alive(owned)
        outputting = alive and owned.outputting(self._now(), self._stall_window)
        return PusherReport(run_id=owned.run_id, alive=alive,
                            outputting=bool(outputting))

    @staticmethod
    def _resource_failure_report(run_id: str, detail: str) -> PusherReport:
        logger.warning('媒体资源核验失败（%s）：不把幸存对象伪装成不存在', detail)
        return PusherReport(run_id=run_id, alive=False, outputting=False)

    def _maybe_report(self, owned: OwnedMedia, alive: bool,
                      observed: bool = True) -> None:
        with self._lock:
            if owned is not self._owned:
                return                                # 旧对象的迟到观察：不动新对象
        outputting = alive and owned.outputting(self._now(), self._stall_window)
        if not observed:
            outputting = False
        marker = (bool(alive), bool(outputting))
        with owned.lock:
            if owned.last_reported == marker:
                return                                # 重复同一事实合并，不每条日志投递
            owned.last_reported = marker
        self.reports += 1
        self._emit(PusherReport(run_id=owned.run_id, alive=bool(alive),
                                outputting=bool(outputting)))

    def _on_object_unexpected_exit(self, owned: OwnedMedia) -> None:
        """自有对象自然/意外退出：只投递窄事实，由 Core 决定下一次 Sync。"""
        with owned.lock:
            owned.exit_code = self._os.exit_code(owned.proc) \
                if owned.proc is not None else None
        self._maybe_report(owned, alive=False)

    # ---------------------------------------------------------------- 创建

    def _spawn(self, effect: fx.SyncPusher, deadline: float) -> Optional[OwnedMedia]:
        if not self._verified:
            # **端口自身**阻断未核验 spawn：不靠调用方的顺序，也不把未知当空库存。
            self._spawn_blocked = '尚未完成库存核验：拒绝创建自有推流'
            logger.error('%s', self._spawn_blocked)
            return None
        if self._spawn_blocked:
            return None          # 库存不可核验：在解决之前**禁止**创建
        with self._lock:
            if self._closing:
                return None
            if self._owned is not None:
                return None                           # 复核：绝不新开第二路
            pending_id = uuid.uuid4().hex
            creating = RecordEntry(
                pending_id=pending_id, physical_id='', pid=0, creation_time=0,
                run_id=effect.run_id, state=RECORD_STATE_CREATING,
                created_wall=datetime.now().isoformat(timespec='seconds'))
            if not self._instance.records.write(creating):
                self._last_spawn_error = '媒体归属记录写入失败：本轮不创建'
                logger.error('%s', self._last_spawn_error)
                return None

        # 命令构建在锁外：ffprobe/文件准备可能很慢，必须只吃本轮剩余预算。
        try:
            command: MediaCommand = self._builder.build(
                effect.zone_name, deadline, self._clock)
        except (NoVideosAvailable, NoCompatibleVideos) as exc:
            self._clear_creating(pending_id)
            logger.info('本轮不创建推流进程：%s', exc)
            return None
        except Exception as exc:                      # noqa: BLE001
            self._clear_creating(pending_id)
            logger.warning('媒体命令构建失败：%s', exc)
            raise
        if not command.runnable:
            # 本轮预算已被探测/准备吃光：没有可启动命令，不创建（也不留 creating 记录）。
            self._clear_creating(pending_id)
            return None

        # **创建入口与关闭收敛在同一把锁上**：一旦 close 拿到锁，此后不可能再出现
        # 未受管理的自有进程；子进程**出生即在实例生命周期组内**（无 Popen→Job 窗口）。
        with self._lock:
            if self._closing:
                self._clear_creating(pending_id)
                logger.info('关闭期间命令构建完成：本轮不创建自有对象')
                return None
            if self._owned is not None:
                self._clear_creating(pending_id)
                return None
            job = self._instance.job if self._instance.job_available else None
            if job is None and self._os.job_capable:
                # 本 OS 应当有原生生命周期组，但本实例没拿到：**不**降级成"无归属创建"。
                # 只在这一条上明确失败，等下一次（组可用时）再创建。
                self._clear_creating(pending_id)
                self._discard_prepared(command)
                self._last_spawn_error = '实例生命周期组不可用：本轮不创建无归属对象'
                logger.error('%s', self._last_spawn_error)
                return None
            if self._remaining(deadline) <= 0:
                # 命令构建（探测/过滤/文件准备）本身可能吃掉整份预算：创建入口消费
                # **同一份**绝对截止，耗尽即不创建，并收回本轮准备件。
                self._clear_creating(pending_id)
                self._discard_prepared(command)
                logger.info('媒体预算在创建入口已耗尽：本轮不创建自有对象')
                return None
            try:
                scope = getattr(self._os, 'termination_deadline', None)
                if scope is None:
                    proc = self._os.spawn(
                        command.argv, cwd=str(self._instance.dir), job=job)
                else:
                    # 创建**及**"创建后未能移交时把它收回来确认退出"共用本轮那**一份**
                    # 绝对截止（见 ``WindowsOsOps.spawn_in_job``）：不新建超时常量。
                    with scope(deadline):
                        proc = self._os.spawn(
                            command.argv, cwd=str(self._instance.dir), job=job)
            except SpawnUnreclaimed as exc:
                # 原生创建**已经成功**，只是没能确证把它收回：这是"有对象"的失败出口，
                # 不是"没有对象"。把真实对象交给**同一份**资源核算（而不是只留异常文字
                # 里的 PID 与一条 pid=0 的 creating 记录），如实报本轮未建立推流，并阻断
                # 第二路 —— 创建门、回收、close 与报告自此读同一份事实。
                self._discard_prepared(command)
                self.os_errors += 1
                self._retain_created(effect, exc, pending_id)
                self._spawn_blocked = (
                    f'原生创建后未能确认回收（{exc}）：保留现场，禁止新 spawn')
                self._last_spawn_error = self._spawn_blocked
                logger.error('%s', self._spawn_blocked)
                return None
            except SpawnUnavailable as exc:
                self._clear_creating(pending_id)
                self._discard_prepared(command)
                self._last_spawn_error = f'组内原子创建失败：本轮不创建（{exc}）'
                logger.error('%s', self._last_spawn_error)
                return None
            except Exception as exc:                  # noqa: BLE001
                self._clear_creating(pending_id)
                self.os_errors += 1
                logger.warning('创建子进程失败（%s）：本轮不持有对象', exc)
                return None
            # 创建成功即持有：**出生身份读不出、登记失败都不会让引用消失**。
            identity = self._safe_identity(int(proc.pid))
            creation_time = identity.creation_time \
                if identity_is_conclusive(identity) else 0
            physical_id = _physical_id(self._instance.instance_id, int(proc.pid),
                                       creation_time)
            owned = OwnedMedia(
                instance_id=self._instance.instance_id, physical_id=physical_id,
                pid=int(proc.pid), creation_time=creation_time,
                run_id=effect.run_id, proc=proc, pending_id=pending_id,
                log_path=self._instance.media_dir
                / f'{_safe_filename(physical_id)}.log',
                cleanup_paths=command.cleanup_paths)
            owned.created_mono = self._now()
            self._owned = owned
            self._watch_seq += 1
            watcher = OwnedMediaWatcher(
                self, owned, interval=self._watch_interval,
                stall_window=self._stall_window,
                log_max_bytes=self._instance.log_max_bytes,
                os_ops=self._os,
                name=f'{self._name}-media-watch-{self._watch_seq}')
            owned.watcher = watcher

        registered = self._instance.records.write(RecordEntry(
            pending_id=pending_id, physical_id=physical_id, pid=owned.pid,
            creation_time=creation_time, run_id=effect.run_id,
            state=RECORD_STATE_OWNED,
            created_wall=datetime.now().isoformat(timespec='seconds')))
        if not registered:
            # 登记失败**只记事实**：真实对象已经被持有，引用与创建事实都不丢。
            self._last_spawn_error = '媒体归属记录登记失败：已保留真实对象，只记事实'
            logger.error('%s', self._last_spawn_error)
        if not creation_time:
            logger.warning('新建进程的出生身份读不出：归属由活动句柄与实例生命周期组承重')
        watcher.start()
        self.spawns += 1
        logger.info('已创建自有推流对象 %s（PID=%s，重编码=%s）',
                    physical_id, owned.pid, command.reencode)
        return owned

    def _clear_creating(self, pending_id: str) -> None:
        """只清掉**这一轮自己写的** ``creating`` 记录，不动别人的事实。"""
        entry, _error = self._instance.records.read()
        if entry is not None and entry.pending_id == pending_id \
                and entry.state == RECORD_STATE_CREATING:
            self._instance.records.clear()

    @staticmethod
    def _discard_prepared(command: MediaCommand) -> None:
        """创建入口放弃本轮准备件（concat 播放列表等），只删本轮自己写的路径。"""
        for path in getattr(command, 'cleanup_paths', ()) or ():
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------------- 回收

    def _reclaim_object(self, owned: OwnedMedia, deadline: float) -> None:
        """温和停止 → 预算内强制结束 → 核验退出。未退出则**保留**引用。

        温和、强制、等待都只消费**同一份**绝对截止；截止耗尽就**不再发新动作**。
        未退出时**不**停观察线程：管道继续被排空，卡住的进程不会因为 stderr 写满
        而永远拿不到退出机会。只在确认退出后才释放引用与临时件。
        """
        if not self._is_alive(owned):
            self._release(owned, deadline)
            return
        self._safe_terminate(owned.pid, force=False, deadline=deadline)  # 温和
        gentle_deadline = min(deadline, self._now() + self._gentle_slice)
        self._wait_exit(owned, gentle_deadline)
        if self._is_alive(owned) and self._remaining(deadline) > 0:
            self._safe_terminate(owned.pid, force=True, deadline=deadline)  # 强制
            self._wait_exit(owned, deadline)
        if not self._is_alive(owned):
            self._release(owned, deadline)
        else:
            logger.warning('自有推流对象未在预算内退出：保留引用，不另起第二路（%s）',
                           owned.physical_id)

    def _wait_exit(self, owned: OwnedMedia, deadline: float) -> None:
        step = 0.02
        while self._is_alive(owned) and self._now() < deadline:
            time.sleep(min(step, max(0.0, deadline - self._now())))

    def _stop_watcher(self, owned: OwnedMedia,
                      deadline: Optional[float] = None) -> bool:
        watcher = owned.watcher
        if watcher is None:
            return True
        watcher.stop()
        timeout = self._remaining(deadline) if deadline is not None else 2.0
        watcher.join(timeout=max(0.0, timeout))
        return not watcher.is_alive()

    def _release(self, owned: OwnedMedia,
                 deadline: Optional[float] = None) -> bool:
        """确认退出后释放引用、清记录、收临时件。身份不匹配时不动。

        **只有本对象的观察线程确证退出才销账**：线程还在就保留引用与记录、如实返回
        ``False``（由下一次 close／回收继续完成），绝不把"已经发过 stop"当成"线程已
        退出"——那会让 close 谎报"读者已停、资源已清"。
        """
        if not self._stop_watcher(owned, deadline):
            logger.warning('观察线程未在预算内退出：保留引用与记录，等下一次释放（%s）',
                           owned.physical_id)
            return False
        entry, _error = self._instance.records.read()
        if entry is not None and (
                entry.physical_id == owned.physical_id
                or (owned.pending_id and entry.pending_id == owned.pending_id)):
            self._instance.records.clear()
        with self._lock:
            if owned is self._owned:
                self._owned = None
        self._close_streams(owned, deadline)
        self._cleanup_paths(owned)
        owned.watcher = None
        return True

    def _release_reference_only(self, owned: OwnedMedia) -> None:
        """对象已不是我们那一份（PID 复用/已退出）：只清引用，不结束无关进程。"""
        with self._lock:
            if owned is self._owned:
                self._owned = None
        self._close_streams(owned, None)
        owned.watcher = None

    def _close_streams(self, owned: OwnedMedia,
                       deadline: Optional[float] = None) -> None:
        """释放本对象持有的管道与进程句柄。

        只在**确认退出之后**调用，因此这里**不**再等进程：等一个还活着的进程只会把
        本轮那份绝对截止烧光，而释放语义本来就不该发生在活着的时候（真发生时如实告警）。
        """
        proc = owned.proc
        if proc is not None and proc.poll() is None:
            logger.warning('在对象仍存活时释放引用（%s）：只关句柄与管道，不等退出',
                           owned.physical_id)
        stream = getattr(proc, 'stderr', None) if proc is not None else None
        if stream is not None:
            try:
                stream.close()
            except Exception:                         # noqa: BLE001
                pass
        dispose = getattr(proc, 'dispose', None)
        if dispose is not None:
            try:
                dispose()                             # 关进程句柄：防句柄泄漏
            except Exception:                         # noqa: BLE001
                pass

    def _cleanup_paths(self, owned: OwnedMedia) -> None:
        paths = list(owned.cleanup_paths)
        if owned.log_path is not None:
            paths.append(str(owned.log_path))
            paths.append(str(owned.log_path) + '.1')
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------------- 关闭

    def release_ownership(self, deadline: Optional[float] = None) -> bool:
        """放弃内存引用但**不回收**真实对象（端口重建 / 交接用）。

        这不是关闭也不是用户停止：对象、归属记录与实例生命周期组都原样保留，
        新端口可以从核验库存里把它认回来并精准收尾。观察线程会被停掉（管道不再
        被本站排空），因此重建场景按"只核验与收尾"口径使用。
        """
        with self._lock:
            owned = self._owned
        if owned is None:
            return True
        self._stop_watcher(owned)
        with self._lock:
            if owned is self._owned:
                self._owned = None
        owned.watcher = None
        return True

    def begin_close(self) -> None:
        """只关闭创建入口：幂等、不回收、不 join。**不是**用户停播。"""
        with self._lock:
            self._closing = True

    def close(self, deadline: float) -> MediaCloseReport:
        """在**共享的绝对截止**内回收**本实例已知存在**的原生资源并停止观察线程。

        核算口径是"事实"，不是"引用是否为空"：``_owned``、实例生命周期组里的**原生
        成员**、以及创建后未完成移交的保留对象都在同一份核算里。**存在核算与结束
        授权分开**：只有持有真实事实（自有引用、原生组归属、记录与出生身份/存活
        确证一致）的对象才会收到结束动作；"已知存在但未核验"的线索对象如实计入
        存在事实（阻止把"不能获得结束权限"翻成"对象不存在"），但**不**被结束——
        旧公共标识 ``prod`` 的线索对象永不获得结束授权（返修 R4）。
        读不出组或读不出归属记录都不报"已经清干净"；收尾结果也从真实事实数
        （对象是否仍存活 ∪ 组里还剩谁），而不是从"引用有没有被清掉"推断。

        这是资源关闭，不是用户停止：不写业务状态、不发平台 Stop、不改 Goal。
        """
        with self._lock:
            self._closing = True
        detail: List[str] = []

        objects, group_error, record_error = self._accountable_objects()
        if group_error:
            detail.append(group_error)
        if record_error:
            detail.append(f'归属记录不可核验（{record_error}）：'
                          '保留原字节，不报已清理')
        for obj in objects:
            if not obj.end_authorized:
                detail.append(f'未经核验的对象（{obj.physical_id}）：'
                              '保留线索，不发结束动作')
                continue
            try:
                self._reclaim_object(obj, deadline)
            except Exception as exc:                  # noqa: BLE001
                self.os_errors += 1
                detail.append(f'reclaim:{type(exc).__name__}: {exc}')

        readers_ok = self._stop_all_watchers(deadline)
        if not readers_ok:
            detail.append('观察线程未在预算内退出')

        # 收尾结果从**真实事实**核对：对象是否仍存活、原生组里还剩什么。
        still_alive = {obj.pid for obj in objects if self._is_alive(obj)}
        residual, residual_error = self._job_members()
        if residual_error:
            detail.append(residual_error)
        alive_after = len(still_alive | set(residual))
        if alive_after:
            detail.append('仍有真实对象未在预算内退出：保留事实，不报已清理')

        with self._lock:
            remaining_owned = self._owned is not None
        resources_cleared = (alive_after == 0 and not remaining_owned
                             and not residual_error and not record_error)
        report = MediaCloseReport(
            closed=True, resources_cleared=resources_cleared,
            readers_stopped=readers_ok, objects=len(objects),
            alive_after=alive_after, detail=tuple(detail))
        logger.info('媒体端口关闭：objects=%d alive_after=%d readers_stopped=%s',
                    len(objects), alive_after, readers_ok)
        return report

    def _stop_all_watchers(self, deadline: float) -> bool:
        with self._lock:
            owned = self._owned
        watcher = owned.watcher if owned is not None else None
        if watcher is None:
            return True
        watcher.stop()
        watcher.join(timeout=max(0.0, self._remaining(deadline)))
        return not watcher.is_alive()

    # ---------------------------------------------------------------- 证据

    def stats(self) -> dict:
        with self._lock:
            owned = self._owned
            closing = self._closing
        entry, error = self._instance.records.read()
        payload = {
            'name': self._name,
            'closing': closing,
            'spawn_blocked': self._spawn_blocked,
            'verified': self._verified,
            'spawns': self.spawns,
            'os_errors': self.os_errors,
            'last_spawn_error': self._last_spawn_error,
            'reclaims': self.reclaims,
            'recovery_attempts': self.recovery_attempts,
            'bindings': self.bindings,
            'reports': self.reports,
            'job_available': self._instance.job_available,
            'record_error': error,
            'record_state': entry.state if entry is not None else '',
            'owned': None,
        }
        if owned is not None:
            with owned.lock:
                payload['owned'] = {
                    'physical_id': owned.physical_id,
                    'run_id': owned.run_id,
                    'pid': owned.pid,
                    'creation_time': owned.identity.creation_time,
                    'alive': self._is_alive(owned),
                    'media_seconds': owned.last_media_seconds,
                    'first_output': owned.first_output_mono is not None,
                    'has_watcher': owned.watcher is not None,
                    'log_path': str(owned.log_path) if owned.log_path else '',
                }
        return payload

    def owned_media(self) -> Optional[OwnedMedia]:
        with self._lock:
            return self._owned

    def live_object_count(self) -> int:
        owned = self.owned_media()
        return 1 if owned is not None and self._is_alive(owned) else 0
