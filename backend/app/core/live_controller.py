"""
live_controller.py - 直播控制器（重构版）

从原 live_controller.py 提取核心类 LiveController、BilibiliApi、VideoPathFinder、AreaLoader 等。
为 API 层提供简洁的接口。
"""

import os
import sys
import re
import json
import time
import random
import secrets
import signal
import logging
import threading
import subprocess
import hashlib
import urllib.parse
import collections
import uuid
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Optional, Tuple, List, Dict, TYPE_CHECKING
from datetime import datetime, date, timedelta

import requests

from app.core import diagnostics
from app.core import media_format
from app.core.ffmpeg_log_progress import (
    FFmpegLogProgressReader, capture_ffmpeg_log_baseline,
)
from app.core.config import (
    VIDEO_BASE_PATH, DEFAULT_VIDEO_FOLDER,
    BILIBILI_APP_KEY, BILIBILI_APP_SEC,
    HEADERS, DEFAULT_LIVE_DURATION_BASE,
    MAX_RECONNECT_ATTEMPTS, MONITOR_INTERVAL,
    CROSS_DAY_CHECK_INTERVAL, get_data_path
)
from app.models.schemas import LiveInstruction, Task

if TYPE_CHECKING:
    from app.core.task_manager import TaskManager

logger = logging.getLogger(__name__)

# 服务端签发的操作票据：`<代际>:<序号>`。自带代际意味着过期判定不依赖登记表，
# 服务端签发票据的形状：`<boot8>:<epoch>:<seq>`。boot8 是**服务进程启动身份**
# （每进程随机一次），因此服务重启后旧票据会被识别为“别的进程的票据”而拒绝，
# 不会与新一代的序号撞车；epoch 让判定只比较前缀、不依赖登记表，
# 因此**不受任何缓存淘汰影响**。
_ISSUED_TICKET = re.compile(r'^([0-9a-f]{8}):(\d+):(\d+)$')

# 自定义票据（客户端 UUID 等）的保留时长。TTL 是 forgetting 的**唯一**常规机制：
# 容量上限只是内存安全阀。前端已接通服务端签发票据（见 /api/live/operation-ticket），
# 自定义票据只来自未升级的旧客户端，其边界是“超过 TTL 的重放按新意图登记”。
DEFAULT_TOKEN_TTL_SECONDS = 24 * 3600

# 退出总预算（秒）：低于现场 TimeoutStopSec=30，给排空与日志留出余量。
# 各阶段只能用剩余额度，外部接口永不返回时也能受控退出。
#: 退出预算（秒）。**不是硬上限**：它约束的是"每一步最多花掉剩余额度"，
#: 因此单步可被裁短、总体目标是 20s 量级；真正的硬保证来自每一步的有界等待
#: （join/超时）而不是这个数字本身。实测值见交付报告的退出计时证据。
SHUTDOWN_BUDGET_SECONDS = 20.0


def _parse_issued_ticket(token: str):
    """解析服务端签发票据 → (boot, epoch, seq)；自定义票据返回 (None, None, None)。"""
    match = _ISSUED_TICKET.match(token or '')
    if not match:
        return None, None, None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _register_face_verify_intent(controller, verify_url: str) -> bool:
    """Register face state; retain compatibility for lightweight test doubles."""
    setter = getattr(controller, '_set_pending_face_verify', None)
    if callable(setter):
        return bool(setter(verify_url))
    if not (getattr(controller, 'is_streaming', False)
            or getattr(controller, '_is_starting', False)):
        return False
    for name in ('_start_cancel', 'stop_monitor'):
        cancel = getattr(controller, name, None)
        if cancel is not None and cancel.is_set():
            return False
    controller._pending_face_verify = True
    controller._face_verify_url = verify_url
    controller._face_verify_run_id = (
        getattr(controller, '_pending_run_id', '')
        or getattr(controller, '_current_run_id', '')
        or getattr(getattr(controller, 'state', None), 'run_id', '') or '')
    controller._face_verify_epoch = getattr(controller, '_control_epoch', None)
    return True


class _OperationTokens:
    """控制操作票据登记表（识别“响应丢失后的重放”）。

    三类票据语义：

    - **服务端签发**（`<boot>:<epoch>:<seq>`，:meth:`LiveController.issue_operation`）：
      只比较前缀（boot + epoch），不查登记表，**永不受淘汰影响**——这是前端
      已接通的正式协议；
    - **自定义票据**（旧客户端的 UUID）：首次出现按当时代际登记，TTL 内保留；
      再次出现且代际已变 = “停止之后的重放”→ 拒绝。TTL 内**不做**条数淘汰
      （容量只是内存安全阀，默认 4096，远超面板真实操作量），因此“已取消的
      旧启动不会因缓存遗忘被接受”；
    - 超过 TTL 的票据被遗忘：这是**明确的有效期边界**（重放若迟到超过 TTL，
      会被当作新意图登记）；被拒绝的历史不能从一个陌生 UUID 无限推断。
    """

    def __init__(self, capacity: int = 4096, tombstone: int = 1024,
                 ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS,
                 boot_id: str = None):
        # RLock：accept()/register_stop() 持锁期间会调用 _reject()/_remember_evicted()，
        # 普通 Lock 会自锁死（表现为测试无输出挂起）。
        self._lock = threading.RLock()
        self.capacity = max(1, int(capacity))
        self._entries: 'collections.OrderedDict[str, tuple]' = collections.OrderedDict()
        self._tombstone: 'collections.deque[str]' = collections.deque(
            maxlen=max(1, int(tombstone)))
        self._tombstone_set = set()
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.boot_id = boot_id
        self.evicted_total = 0
        self.rejected = 0
        self.expired_total = 0
        self.last_reason = ''

    # ---------------- 内部 ----------------

    def _purge_expired_unlocked(self) -> None:
        """TTL 到期的登记条目直接遗忘（这是文档化的有效期边界，不入墓碑）。"""
        if not self._entries:
            return
        now = time.monotonic()
        expired = [token for token, (_, registered_at) in self._entries.items()
                   if now - registered_at > self.ttl_seconds]
        for token in expired:
            self._entries.pop(token, None)
            self.expired_total += 1

    def _remember_evicted(self, token: str) -> None:
        # deque 满时 append 会挤掉最旧的，需要先同步移出查询集。
        if self._tombstone.maxlen and len(self._tombstone) == self._tombstone.maxlen:
            self._tombstone_set.discard(self._tombstone[0])
        self._tombstone.append(token)
        self._tombstone_set.add(token)
        self.evicted_total += 1

    def _reject(self, reason: str):
        with self._lock:
            self.rejected += 1
            self.last_reason = reason
        return False, reason

    def _register_unlocked(self, token: str, epoch: int) -> None:
        self._entries[token] = (epoch, time.monotonic())
        self._entries.move_to_end(token)
        while len(self._entries) > self.capacity:
            evicted, _ = self._entries.popitem(last=False)
            self._remember_evicted(evicted)

    # ---------------- 开播/恢复/验证确认：登记 + 拒绝过期重放 ----------------

    def accept(self, token: str, current_epoch: int,
               allow_stale: bool = False) -> Tuple[bool, str]:
        """返回 ``(是否可执行, 原因)``。空票据不登记、不拦截。

        ``allow_stale=True``：只登记、不拒绝（旧版停止入口的兼容语义，
        正式停止协议见 :meth:`register_stop`）。
        非字符串（例如 FastAPI 未解析的 Header 默认值）一律按“没有票据”处理。
        """
        if not isinstance(token, str) or not token:
            return True, 'no_token'
        boot, epoch, _seq = _parse_issued_ticket(token)
        if boot is not None:
            # 服务端签发：boot + 代际都在票据里，与登记表无关。
            if self.boot_id is not None and boot != self.boot_id:
                return self._reject('foreign_boot_ticket')
            if epoch != current_epoch:
                return self._reject('issued_ticket_stale_epoch')
            return True, 'issued_ticket'
        with self._lock:
            self._purge_expired_unlocked()
            if allow_stale:
                # 显式登记即解除墓碑：该票据已被重新确认属于当前时刻。
                self._tombstone_set.discard(token)
                self._register_unlocked(token, current_epoch)
                return True, 'registered_allow_stale'
            if token in self._tombstone_set:
                return self._reject('replay_of_evicted_ticket')
            previous = self._entries.get(token)
            if previous is None:
                self._register_unlocked(token, current_epoch)
                return True, 'registered'
            self._entries.move_to_end(token)
            if previous[0] != current_epoch:
                return self._reject('replay_after_stop')
            return True, 'replay_same_epoch'

    # ---------------- 停止：登记并判断是否还需要执行 ----------------

    def register_stop(self, token: str, current_epoch: int) -> Tuple[bool, str]:
        """停止专用：返回 ``(是否需要执行, 原因)``。

        - 首次见到（或超出 TTL/容量已被遗忘）→ 登记并执行；
        - 同代际已登记 → 上一次执行没有推进代际（例如停止失败或空转），
          属于同一次操作的重试 → 再次执行；
        - 旧代际已登记 / 墓碑 / 其它服务进程的签发票据 → 该停止意图已经在
          **它自己的代际里处理完成**，重放只应确认既有结果，绝不再执行——
          否则响应丢失的旧停止会停掉后来明确开启的新直播。
        """
        if not isinstance(token, str) or not token:
            return True, 'no_token'
        boot, epoch, _seq = _parse_issued_ticket(token)
        if boot is not None:
            if self.boot_id is not None and boot != self.boot_id:
                return False, 'foreign_boot_ticket'
            if epoch != current_epoch:
                return False, 'already_handled_in_prior_epoch'
            return True, 'issued_ticket_current_epoch'
        with self._lock:
            self._purge_expired_unlocked()
            previous = self._entries.get(token)
            if previous is not None:
                self._entries.move_to_end(token)
                if previous[0] == current_epoch:
                    return True, 'retry_same_epoch'
                return False, 'already_handled_in_prior_epoch'
            if token in self._tombstone_set:
                return False, 'replay_of_evicted_ticket'
            self._register_unlocked(token, current_epoch)
            return True, 'first_seen'

    def forget(self, token: str) -> None:
        """登记失败（例如代际已被停止推进）时不要留下半成品登记。"""
        with self._lock:
            self._entries.pop(token, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                'tracked': len(self._entries),
                'capacity': self.capacity,
                'tombstoned': len(self._tombstone_set),
                'tombstone_capacity': self._tombstone.maxlen,
                'ttl_seconds': self.ttl_seconds,
                'expired_total': self.expired_total,
                'evicted_total': self.evicted_total,
                'rejected_total': self.rejected,
                'last_reject_reason': self.last_reason,
            }


AUTH_LOGOUT_TOKEN_TTL_SECONDS = 24 * 3600
AUTH_LOGOUT_TOKEN_CAPACITY = 1024


class _AuthLogoutLedger:
    """认证登出意图的有限幂等登记表。

    认证登出不能复用直播控制票据：它不推进推流代际，也不应因为控制器的
    停止语义而改变任务/推流状态。已处理的登出 token 在 TTL 内只确认原结果；
    到期 token 明确拒绝，容量耗尽时也拒绝新 token，避免静默淘汰后把旧重放
    当成新登出。
    """

    def __init__(self, capacity: int = AUTH_LOGOUT_TOKEN_CAPACITY,
                 ttl_seconds: float = AUTH_LOGOUT_TOKEN_TTL_SECONDS,
                 records: dict = None):
        self._lock = threading.RLock()
        self.capacity = max(1, int(capacity))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: 'collections.OrderedDict[str, dict]' = collections.OrderedDict()
        if isinstance(records, dict):
            for token, raw in records.items():
                if (isinstance(token, str) and isinstance(raw, dict)
                        and len(self._entries) < self.capacity):
                    state = raw.get('state')
                    expires_at = raw.get('expires_at')
                    result = raw.get('result')
                    if (state in ('pending', 'complete', 'expired')
                            and isinstance(expires_at, (int, float))):
                        self._entries[token] = {
                            'state': state,
                            'expires_at': float(expires_at),
                            'result': (dict(result) if isinstance(result, dict)
                                       else None),
                        }

    def claim(self, token: str):
        """返回 ``(kind, result)``，kind 为 first/replay/pending/expired/full."""
        if not isinstance(token, str) or not token.strip():
            return 'untracked', None
        token = token.strip()
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                if entry['expires_at'] <= time.time():
                    entry['state'] = 'expired'
                    return 'expired', None
                self._entries.move_to_end(token)
                if entry['state'] == 'pending':
                    return 'pending', None
                return 'replay', dict(entry['result'])
            if len(self._entries) >= self.capacity:
                return 'full', None
            self._entries[token] = {
                'state': 'pending',
                'expires_at': time.time() + self.ttl_seconds,
                'result': None,
            }
            return 'first', None

    def complete(self, token: str, result: dict) -> None:
        if not isinstance(token, str) or not token.strip():
            return
        with self._lock:
            entry = self._entries.get(token.strip())
            if entry is None:
                return
            entry['state'] = 'complete'
            entry['result'] = dict(result)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                token: {
                    'state': entry['state'],
                    'expires_at': entry['expires_at'],
                    'result': (dict(entry['result'])
                               if isinstance(entry.get('result'), dict) else None),
                }
                for token, entry in self._entries.items()
            }

    def release(self, token: str) -> None:
        """释放未执行的意图（例如活动直播拒绝），允许用户稍后重试同一票据。"""
        if not isinstance(token, str) or not token.strip():
            return
        with self._lock:
            self._entries.pop(token.strip(), None)


def _limit_ffmpeg_log(log_fp, max_bytes=10 * 1024 * 1024):
    """保留一份有大小上限的日志尾部，并原地截断，保持子进程写入句柄有效。"""
    log_fp.flush()
    if os.fstat(log_fp.fileno()).st_size < max_bytes:
        return
    log_path = Path(log_fp.name)
    with log_path.open('rb') as source:
        source.seek(-max_bytes, os.SEEK_END)
        tail = source.read(max_bytes)
    with Path(str(log_path) + '.1').open('wb') as backup:
        backup.write(tail)
    log_fp.truncate(0)
    log_fp.write("\n=== 日志达到大小上限，上一段尾部保存在 ffmpeg.log.1 ===\n")
    log_fp.flush()


class _PipeDrainer(threading.Thread):
    """最后一道兜底：由控制器自己把子进程的进度管道读干。

    诊断通道不可用（创建失败/已被回收）时使用。与诊断读取器的区别是这里
    没有解析与落盘，只有“读干”；连续读错误时退避重试，达到上限后判定为
    **不可恢复读端错误**并停止读取——但这不等于永久放弃：所有权层会记录
    该状态并限频重建消费者，管道恢复可读后仍有人接手。
    """

    MAX_CONSECUTIVE_ERRORS = 5
    RETRY_BACKOFF = 0.2

    def __init__(self, stream, generation: int):
        super().__init__(name='FFmpegPipeDrainer', daemon=True)
        self.stream = stream
        self.generation = generation
        self.lines = 0
        self.read_errors = 0
        self.unrecoverable = False
        self.exited = threading.Event()
        self._stop_requested = threading.Event()

    def stop(self):
        """被更新的消费者取代时调用：尽快退出，避免双读者。"""
        self._stop_requested.set()

    def run(self):
        consecutive = 0
        try:
            while not self._stop_requested.is_set():
                try:
                    raw = self.stream.readline()
                except Exception:
                    consecutive += 1
                    self.read_errors += 1
                    if consecutive >= self.MAX_CONSECUTIVE_ERRORS:
                        self.unrecoverable = True
                        logger.warning("排空读取器连续读失败，本轮判定为不可恢复读错误")
                        return
                    if self._stop_requested.wait(self.RETRY_BACKOFF):
                        return
                    continue
                if not raw:
                    return          # EOF：子进程已结束写入
                consecutive = 0
                self.lines += 1
        finally:
            self.exited.set()

    def stats(self) -> dict:
        return {'kind': 'pipe_drainer', 'generation': self.generation,
                'lines': self.lines, 'read_errors': self.read_errors,
                'unrecoverable': self.unrecoverable, 'alive': self.is_alive()}


# ==================== VideoPathFinder（原样保留） ====================
class VideoPathFinder:
    """视频路径查找器（优先分区名文件夹，其次 default）"""

    @staticmethod
    def find_video(zone_name: str) -> Optional[str]:
        zone_names = [
            zone_name,
            zone_name.replace("区", ""),
            zone_name.lower()
        ]
        for name in zone_names:
            zone_folder = VIDEO_BASE_PATH / name
            if zone_folder.exists() and zone_folder.is_dir():
                video_files = (list(zone_folder.glob("*.mp4")) +
                              list(zone_folder.glob("*.mkv")) +
                              list(zone_folder.glob("*.flv")))
                if video_files:
                    video_path = random.choice(video_files)
                    logger.info(f" 找到分区视频：{video_path}")
                    return str(video_path)
        if DEFAULT_VIDEO_FOLDER.exists() and DEFAULT_VIDEO_FOLDER.is_dir():
            video_files = (list(DEFAULT_VIDEO_FOLDER.glob("*.mp4")) +
                          list(DEFAULT_VIDEO_FOLDER.glob("*.mkv")) +
                          list(DEFAULT_VIDEO_FOLDER.glob("*.flv")))
            if video_files:
                video_path = random.choice(video_files)
                logger.info(f" 使用 default 视频：{video_path}")
                return str(video_path)
        logger.error(f" 未找到视频文件（分区：{zone_name}）")
        return None

    @staticmethod
    def ensure_folders():
        VIDEO_BASE_PATH.mkdir(parents=True, exist_ok=True)
        DEFAULT_VIDEO_FOLDER.mkdir(parents=True, exist_ok=True)
        logger.info(f" 视频文件夹已准备：{VIDEO_BASE_PATH}")


# ==================== 有界单轮接缝（阶段 3A） ====================
class _AttemptPolicy:
    """一次操作的**单轮**请求策略：共享预算 ＋ 显式测试端点 ＋ 有界传输。

    它只在本线程生效（存在 ``_retry_context`` 线程局部里），因此并发线程互不影响，
    也不会写进实例字段。**不在场时** ``_req`` 的旧行为（有限重试、10 秒超时、
    真实端点）逐字节不变 —— 旧生产路径不经过这里。

    ``budget`` 只要求有 ``remaining() -> float``（秒）；``endpoint_base`` 是形如
    ``http://127.0.0.1:8000`` 的显式端点，用于隔离验收把**同一条 URL 构造路径**
    指向本地服务（主机被替换，路径与查询串原样保留）。

    ``sender`` 是这一轮**物理执行**的接缝：调用约定是
    ``sender(method, url, params, data, headers, cookies, budget) -> (status, body, error)``，
    整轮共用同一份绝对截止。它由调用方注入（平台端口带的是
    ``session_runtime.bounded_transport.BoundedHttpSender``），因此本文件不依赖
    新包。**没有传输实现时这一轮不发请求**，而不是退回一个没有整轮截止的调用。
    """

    __slots__ = ('budget', 'endpoint_base', 'sender')

    def __init__(self, budget, endpoint_base: Optional[str] = None,
                 sender=None) -> None:
        self.budget = budget
        self.endpoint_base = (endpoint_base or '').rstrip('/') or None
        self.sender = sender

    def remaining(self) -> float:
        try:
            return max(0.0, float(self.budget.remaining()))
        except Exception:
            return 0.0

    def rewrite(self, url: str) -> str:
        """把绝对 URL 的主机换成显式端点；路径与查询串原样保留。"""
        if not self.endpoint_base:
            return url
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.endpoint_base)
        return urllib.parse.urlunsplit(
            (base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

    def describe(self, url: str) -> str:
        """日志用：只给主机与路径，不带查询串（避免把参数写进证据）。"""
        try:
            parts = urllib.parse.urlsplit(url)
            return f'{parts.scheme}://{parts.netloc}{parts.path}'
        except Exception:
            return '<url>'


# ==================== 凭据文件的进程内提交边界 ====================
_CREDENTIAL_LOCKS: Dict[str, threading.RLock] = {}
_CREDENTIAL_LOCKS_GUARD = threading.Lock()


def credential_commit_lock(cookie_file) -> threading.RLock:
    """同一份凭据文件**共享**的串行提交锁（按解析后的绝对路径登记）。

    这是"读当前身份 → 写入"能成为一个边界的前提：``BilibiliApi`` 对象各自持有
    的实例锁保护不了**同一个文件** —— 退出、扫码换号与启动后的资料补齐来自不同
    的对象，只有共用同一把按路径登记的锁，迟到的那一方才可能看见别人已经提交的
    事实（凭据被撤销 / 已换成另一个 UID）。

    RLock（可重入）：同一线程内的嵌套提交（例如补齐在锁内调用
    ``replace_cookies``，后者再取同一把锁）不会自锁。锁只是**进程内**语义，
    不冒充跨进程文件锁。
    """
    key = str(Path(cookie_file).resolve())
    with _CREDENTIAL_LOCKS_GUARD:
        lock = _CREDENTIAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _CREDENTIAL_LOCKS[key] = lock
        return lock


# ==================== BilibiliApi（核心 API 封装） ====================
class BilibiliApi:
    """B 站直播 API 封装（支持 cookies 持久化）"""

    def __init__(self, cookie_file: str = None):
        self.cookies = {}
        self.headers = HEADERS.copy()
        self._retry_context = threading.local()
        self._cookie_lock = threading.RLock()
        self._auth_revocations = {}
        self.cookie_file = Path(cookie_file) if cookie_file else get_data_path("bili_cookies.json")
        self._load_cookies()

    def _cookies_mutex(self):
        """返回凭据状态锁；测试替身用 ``__new__`` 构造时也可惰性补齐。"""
        lock = self.__dict__.get('_cookie_lock')
        if lock is None:
            lock = threading.RLock()
            self._cookie_lock = lock
        return lock

    def _cookie_snapshot(self) -> dict:
        with self._cookies_mutex():
            return dict(self.cookies)

    def _load_cookies(self):
        if not self.cookie_file.exists():
            return
        try:
            with open(self.cookie_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            loaded = data.get('cookies', {})
            self.cookies = dict(loaded) if isinstance(loaded, dict) else {}
            loaded_revocations = data.get('logout_tokens', {})
            self._auth_revocations = (
                dict(loaded_revocations)
                if isinstance(loaded_revocations, dict) else {})
            if self.cookies:
                logger.debug(f" 已加载 {len(self.cookies)} 个 cookies")
        except Exception as e:
            logger.debug(f" 加载 cookies 失败：{e}")

    def _save_cookies(self, cookies: dict = None) -> bool:
        """原子落盘凭据，空字典也必须写出撤销结果。

        返回 False 表示撤销/登录凭据没有得到持久化确认；调用方不能把这种
        情况报告成成功。临时文件与目标文件放在同一目录，``os.replace``
        保证重启时不会读到半个 JSON。

        写入取**按文件登记**的共享提交锁（见 :func:`credential_commit_lock`）：
        退出、换号与资料补齐来自不同的 ``BilibiliApi`` 对象，各自的实例锁挡不住
        对方，只有同一个文件的提交串行化，"读到的当前身份"才不会在写出前被改掉。
        """
        snapshot = self._cookie_snapshot() if cookies is None else dict(cookies)
        temp_name = None
        try:
            with credential_commit_lock(self.cookie_file):
                self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix=f'.{self.cookie_file.name}.', suffix='.tmp',
                    dir=str(self.cookie_file.parent))
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump({
                        'cookies': snapshot,
                        'logout_tokens': dict(getattr(self, '_auth_revocations', {}) or {}),
                        'last_update': datetime.now().isoformat()
                    }, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_name, self.cookie_file)
                temp_name = None
            return True
        except Exception as e:
            logger.warning(f" 保存 cookies 失败，认证状态未确认落盘：{e}")
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def read_cookie_snapshot(self) -> dict:
        """重新读**当前**磁盘凭据快照；不做身份解释、不改内存、不写文件。

        与 :meth:`_load_cookies` 的区别：这里不覆盖本对象的 ``cookies``，只把
        "磁盘上现在是谁"作为事实返回。调用方应把它放在
        :func:`credential_commit_lock` 里使用，才能与提交构成同一个边界。
        """
        try:
            data = json.loads(self.cookie_file.read_text(encoding='utf-8'))
        except Exception:                                  # noqa: BLE001
            return {}
        loaded = data.get('cookies') if isinstance(data, dict) else None
        return dict(loaded) if isinstance(loaded, dict) else {}

    def update_cookies(self, cookies: dict) -> bool:
        """持久化后才提交新的凭据到共享内存。"""
        if not isinstance(cookies, dict):
            return False
        with self._cookies_mutex():
            candidate = dict(self.cookies)
            candidate.update(cookies)
            if not self._save_cookies(candidate):
                return False
            self.cookies = candidate
        logger.info(f" Cookies 已更新（共{len(candidate)}项）")
        return True

    def replace_cookies(self, cookies: dict) -> bool:
        """以一个已验证的完整账号凭据替换当前账号，避免字段跨账号混并。"""
        if not isinstance(cookies, dict):
            return False
        with self._cookies_mutex():
            candidate = dict(cookies)
            if not self._save_cookies(candidate):
                return False
            self.cookies = candidate
        logger.info(f" Cookies 已替换（共{len(candidate)}项）")
        return True

    def get_auth_revocations(self) -> dict:
        """读取随凭据文件保存的认证登出票据。"""
        with self._cookies_mutex():
            return dict(getattr(self, '_auth_revocations', {}) or {})

    def save_auth_revocations(self, records: dict) -> bool:
        """原子保存认证登出票据，并保留当前凭据快照。"""
        if not isinstance(records, dict):
            return False
        with self._cookies_mutex():
            previous = dict(getattr(self, '_auth_revocations', {}) or {})
            self._auth_revocations = dict(records)
            if self._save_cookies(self._cookie_snapshot()):
                return True
            self._auth_revocations = previous
            return False

    def is_logged_in(self) -> bool:
        required_keys = ['SESSDATA', 'bili_jct', 'DedeUserID']
        snapshot = self._cookie_snapshot()
        return all(key in snapshot for key in required_keys)

    def validate_login(self) -> bool:
        if not self.is_logged_in():
            return False
        success, user_info = self.get_user_info()
        if success and user_info.get('code') == 0:
            uid = user_info.get('data', {}).get('mid')
            if uid:
                logger.debug(f" 登录验证成功 | UID: {uid}")
                return True
        logger.warning(" 登录验证失败，cookies 可能已过期")
        return False

    def _appsign(self, params: dict) -> dict:
        params = params.copy()
        params['appkey'] = BILIBILI_APP_KEY
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        sign = hashlib.md5((query + BILIBILI_APP_SEC).encode()).hexdigest()
        params['sign'] = sign
        return params

    def _get_buvid3_simple(self) -> str:
        """生成简单的 buvid3"""
        return hashlib.md5(
            f"{time.time()}_{random.randint(1000, 9999)}".encode()
        ).hexdigest()[:16]

    # ---------- 阶段 3A：有界单轮接缝 ----------

    def _current_attempt_policy(self):
        """本线程当前生效的单轮策略；不在 ``bounded_attempt`` 里时为 ``None``。"""
        return getattr(self._retry_context, 'attempt_policy', None)

    @contextmanager
    def bounded_attempt(self, budget, endpoint_base: Optional[str] = None,
                        sender=None):
        """本线程后续请求**只发一轮**、超时取共享预算的剩余量、可指向本地端点。

        它是阶段 3A 的隔离接缝，不改变任何默认行为：退出上下文后本线程立刻回到
        旧的重试语义。预算、端点与传输按**每次操作**传入，不进实例字段，所以两个
        线程可以各自持有不同的操作封套。
        """
        previous = getattr(self._retry_context, 'attempt_policy', None)
        self._retry_context.attempt_policy = _AttemptPolicy(budget, endpoint_base,
                                                            sender)
        try:
            yield
        finally:
            self._retry_context.attempt_policy = previous

    def _req_bounded(self, method: str, url: str, params: dict, data: dict,
                     policy) -> Tuple[bool, dict]:
        """**单轮**请求：不重试、整轮共用一份绝对截止、日志不含响应体与参数。

        物理执行全部交给策略携带的有界传输：DNS、连接、TLS 握手、发送、读取与
        解析都在**同一个**截止内退出（见 ``bounded_transport`` 的模块说明）。
        预算耗尽时直接返回"未发出"，绝不用 0 超时去打一次真实请求。
        """
        if policy.remaining() <= 0:
            return False, {"code": -1, "msg": "本轮预算已耗尽", "expired": True}
        sender = policy.sender
        if sender is None:
            # 没有传输实现就**不猜**：如实报告这一轮没有发出，也不退回到一个
            # 没有整轮截止的调用上去。
            logger.warning(f" 有界请求缺少传输实现：{policy.describe(url)}")
            return False, {"code": -1, "msg": "有界单轮缺少传输实现"}
        target = policy.rewrite(url.strip())
        override = getattr(self._retry_context, 'cookies_override', None)
        req_cookies = dict(override) if override is not None \
            else self._cookie_snapshot()
        if 'buvid3' not in req_cookies:
            buvid3 = self._get_buvid3_simple()
            if buvid3:
                req_cookies['buvid3'] = buvid3
        status, body, error = sender(method, target, params, data,
                                     self.headers, req_cookies, policy.budget)
        if error:
            logger.warning(f" 有界请求单轮失败（不重试）：{error}："
                           f"{policy.describe(target)}")
            return False, {"code": -1, "msg": "网络请求暂时失败", "retryable": True}
        if status >= 400:
            return False, {"code": status,
                           "msg": f"平台拒绝请求 HTTP {status}"}
        try:
            json_data = json.loads(body.decode('utf-8', 'replace'))
        except (ValueError, AttributeError):
            json_data = None
        if not isinstance(json_data, dict) or 'code' not in json_data:
            return False, {"code": -1, "msg": "上游响应格式异常",
                           "retryable": True}
        code = json_data.get("code", -1)
        msg = json_data.get("message") or json_data.get("msg", "")
        if code == 0:
            logger.debug(f" 有界请求成功：{msg}")
        else:
            logger.warning(f" 有界请求业务失败：code={code}, msg={msg}")
        return code == 0, json_data

    @contextmanager
    def retry_network_until_cancelled(self, cancel):
        """仅当前后台线程持续重试网络错误，不改变面板查询的重试策略。"""
        previous = getattr(self._retry_context, 'cancel', None)
        self._retry_context.cancel = cancel
        try:
            yield
        finally:
            self._retry_context.cancel = previous

    def _req(self, method: str, url: str, params: dict = None, data: dict = None,
             shutdown_once: bool = False) -> Tuple[bool, dict]:
        """面板查询有限重试；后台开播网络恢复不限次数，且可取消。"""
        # 阶段 3A：本线程处在有界单轮上下文里时不进入下面的重试循环。
        # ``shutdown_once`` 有自己的单轮语义，优先于任何策略。
        policy = None if shutdown_once else self._current_attempt_policy()
        if policy is not None:
            return self._req_bounded(method, url, params, data, policy)
        # Shutdown has already cancelled startup. Its platform stop is a separate,
        # best-effort operation: one short request must still reach the platform.
        cancel = (None if shutdown_once else
                  getattr(self._retry_context, 'cancel', None))
        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                return False, {"code": -1, "msg": "操作已取消", "cancelled": True}
            attempt += 1
            resp = None
            try:
                url = url.strip()
                override = getattr(self._retry_context, 'cookies_override', None)
                req_cookies = dict(override) if override is not None \
                    else self._cookie_snapshot()
                if not shutdown_once and 'buvid3' not in req_cookies:
                    buvid3 = self._get_buvid3_simple()
                    if buvid3:
                        req_cookies['buvid3'] = buvid3
                timeout = 3 if shutdown_once else 10
                if method == "GET":
                    resp = requests.get(url, params=params, cookies=req_cookies, headers=self.headers, timeout=timeout)
                else:
                    resp = requests.post(url, params=params, data=data, cookies=req_cookies, headers=self.headers, timeout=timeout)
                if cancel is not None and cancel.is_set():
                    return False, {"code": -1, "msg": "操作已取消", "cancelled": True}
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    raise requests.ConnectionError(f"上游暂不可用 HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    return False, {"code": resp.status_code, "msg": f"平台拒绝请求 HTTP {resp.status_code}"}
                try:
                    json_data = resp.json()
                    if not isinstance(json_data, dict) or 'code' not in json_data:
                        raise ValueError('响应缺少业务状态码')
                    code = json_data.get("code", -1)
                    msg = json_data.get("message") or json_data.get("msg", "")
                    if code == 0:
                        logger.debug(f" API 成功：{msg}")
                    else:
                        logger.warning(f" API 失败：code={code}, msg={msg}")
                    return code == 0, json_data
                except ValueError:
                    logger.error(f" JSON 解析失败：{resp.status_code} {resp.text[:100]}")
                    raise requests.ConnectionError('上游响应格式异常')
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException) as e:
                err_name = type(e).__name__
                logger.warning(f" 网络错误 {err_name} (第{attempt}次)：{url}")
            finally:
                if resp is not None:
                    resp.close()
            if shutdown_once or (cancel is None and attempt >= 3):
                return False, {"code": -1, "msg": "网络请求暂时失败", "retryable": True}
            wait = min(2 ** min(attempt - 1, 5), 30)
            if cancel is not None:
                cancel.wait(wait)
            else:
                time.sleep(wait)

    # ==================== 公开 API 方法 ====================
    def get_qrcode(self) -> Tuple[bool, dict]:
        """获取登录二维码"""
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        params = {"source": "main-fe-header"}
        return self._req("GET", url, params=params)

    def poll_qrcode(self, qrcode_key: str) -> Tuple[bool, dict]:
        """轮询二维码扫码状态"""
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        params = {"qrcode_key": qrcode_key}
        return self._req("GET", url, params=params)

    def save_qrcode_image(self, qr_url: str) -> Optional[str]:
        """下载并保存二维码图片到本地，返回文件路径"""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            import io as _io
            resp = requests.get(qr_url, timeout=10)
            if resp.status_code == 200:
                save_path = Path("_temp_qrcode.png")
                with open(save_path, 'wb') as f:
                    f.write(resp.content)
                logger.info(f" 二维码已保存：{save_path}")
                return str(save_path)
        except Exception as e:
            logger.error(f" 保存二维码失败：{e}")
        return None

    def get_user_info(self) -> Tuple[bool, dict]:
        """获取用户信息"""
        url = "https://api.bilibili.com/x/space/myinfo"
        return self._req("GET", url)

    def get_live_status(self, room_id: int) -> Tuple[bool, dict]:
        """获取直播间状态"""
        return self._req("GET", f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}")

    def create_room(self) -> Tuple[bool, dict]:
        """创建直播间"""
        url = "https://api.live.bilibili.com/room/v1/Room/create"
        data = {}
        return self._req("POST", url, data=data)

    def start_live(self, room_id: int, area_id: int, csrf: str) -> Tuple[bool, dict]:
        """长时间断网后重新取时间并签名，避免复用过期的开播请求。"""
        cancel = getattr(self._retry_context, 'cancel', None)
        while True:
            if cancel is not None and cancel.is_set():
                return False, {'code': -1, 'msg': '操作已取消', 'cancelled': True}
            with self.retry_network_until_cancelled(None):
                success, resp = self._start_live_once(room_id, area_id, csrf)
            if cancel is not None and cancel.is_set():
                return False, {'code': -1, 'msg': '操作已取消', 'cancelled': True}
            if success or not resp.get('retryable') or cancel is None:
                return success, resp
            cancel.wait(30)

    def _start_live_once(self, room_id: int, area_id: int, csrf: str) -> Tuple[bool, dict]:
        """开始直播（带 ts/build/version 签名，模拟直播姬）"""
        # 获取服务端时间戳
        success, ts_resp = self._req("GET", "https://api.bilibili.com/x/report/click/now")
        if not success:
            return False, ts_resp
        ts = ts_resp.get("data", {}).get("now", int(time.time()))

        # 获取直播姬版本信息
        v_params = self._appsign({"system_version": 2, "ts": ts})
        success, v_resp = self._req(
            "GET",
            "https://api.live.bilibili.com/xlive/app-blink/v1/liveVersionInfo/getHomePageLiveVersion",
            params=v_params
        )
        if not success:
            return False, v_resp

        v_data = v_resp.get("data", {})
        data = {
            "room_id": room_id,
            "platform": "pc_link",
            "area_v2": area_id,
            "backup_stream": "0",
            "csrf_token": csrf,
            "csrf": csrf,
            "build": v_data.get("build", "0"),
            "version": v_data.get("curr_version", "0.0.0"),
            "ts": ts,
        }
        url = "https://api.live.bilibili.com/room/v1/Room/startLive"
        return self._req("POST", url, data=self._appsign(data))

    @staticmethod
    def _stop_live_payload(room_id: int, csrf: str) -> dict:
        """stopLive 的请求体：三处调用共用，避免复制出第二份参数口径。"""
        return {
            "room_id": room_id,
            "platform": "pc_link",
            "csrf_token": csrf,
            "csrf": csrf,
        }

    def stop_live(self, room_id: int, csrf: str) -> Tuple[bool, dict]:
        """停止直播"""
        url = "https://api.live.bilibili.com/room/v1/Room/stopLive"
        return self._req("POST", url, data=self._stop_live_payload(room_id, csrf))

    def stop_live_for_shutdown(self, room_id: int, csrf: str) -> Tuple[bool, dict]:
        """Send at most one three-second stop request after startup was cancelled."""
        url = "https://api.live.bilibili.com/room/v1/Room/stopLive"
        return self._req("POST", url, data=self._stop_live_payload(room_id, csrf),
                         shutdown_once=True)

    # ---------- 阶段 3A：一轮调用，一份预算 ----------

    def start_live_once_with_budget(self, room_id: int, area_id: int, csrf: str,
                                    budget,
                                    endpoint_base: Optional[str] = None,
                                    sender=None
                                    ) -> Tuple[bool, dict]:
        """一轮开播：服务端时间戳／版本／最终 POST **共用同一份剩余预算**。

        它直接复用 ``_start_live_once`` 的签名与参数构造，只是把它放进有界上下文，
        因此既不给每段重发一整份预算，也不会进入 ``start_live`` 的无限重试循环。
        """
        with self.bounded_attempt(budget, endpoint_base, sender):
            return self._start_live_once(room_id, area_id, csrf)

    def stop_live_once_with_budget(self, room_id: int, csrf: str, budget,
                                   endpoint_base: Optional[str] = None,
                                   sender=None) -> Tuple[bool, dict]:
        """一轮下播请求。**受理不等于已关**：平台是否真关由后续查询判断。"""
        url = "https://api.live.bilibili.com/room/v1/Room/stopLive"
        with self.bounded_attempt(budget, endpoint_base, sender):
            return self._req("POST", url,
                             data=self._stop_live_payload(room_id, csrf))

    def get_info_once_with_budget(self, room_id: int, budget,
                                  endpoint_base: Optional[str] = None,
                                  sender=None) -> Tuple[bool, dict]:
        """一轮房间查询：**同一份** ``get_info`` 响应里同时取状态与分区。"""
        url = ("https://api.live.bilibili.com/room/v1/Room/get_info"
               f"?room_id={room_id}")
        with self.bounded_attempt(budget, endpoint_base, sender):
            return self._req("GET", url)

    def update_area(self, room_id: int, area_id: int, csrf: str) -> Tuple[bool, dict]:
        """切换直播分区"""
        url = "https://api.live.bilibili.com/room/v1/Room/update"
        data = {
            "room_id": room_id,
            "area_id": area_id,
            "platform": "pc_link",
            "csrf_token": csrf,
            "csrf": csrf,
        }
        return self._req("POST", url, data=data)

    def get_areas(self) -> Tuple[bool, dict]:
        """获取直播分区列表"""
        url = "https://api.live.bilibili.com/room/v1/Area/getList"
        return self._req("GET", url)

    def get_csrf(self) -> Optional[str]:
        """从 cookies 中获取 csrf token"""
        return self._cookie_snapshot().get('bili_jct')

    def get_room_id_by_uid(self, uid: int) -> Tuple[bool, dict]:
        """通过 UID 获取直播间 ID（多接口容错）"""
        import time as _time
        # 方法1
        try:
            url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                room_id = resp['data'].get('room_id') or resp['data'].get('roomid')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        _time.sleep(1.5)
        # 方法2
        try:
            url = f"https://api.bilibili.com/x/space/acc/info?mid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                live_room = resp['data'].get('live_room', {})
                room_id = live_room.get('roomid')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        _time.sleep(1.5)
        # 方法3
        try:
            url = f"https://api.live.bilibili.com/room/v2/Room/room_id_by_uid?uid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                room_id = resp['data'].get('room_id')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        return False, {'code': -1, 'msg': '无法获取直播间 ID'}

    def clear_cookies(self) -> bool:
        """清除 cookies，并在成功落盘前保留原内存凭据。"""
        with self._cookies_mutex():
            if not self._save_cookies({}):
                return False
            self.cookies = {}
        logger.info(" Cookies 已清除")
        return True

    @contextmanager
    def cookie_context(self, cookies: dict):
        """让当前线程的认证请求使用候选凭据，不改共享会话。

        扫码登录在提交前需要用候选账号查询用户和房间；候选账号不能先写入
        ``self.cookies``，否则退出或另一轮扫码可能读到未提交身份。
        """
        previous = getattr(self._retry_context, 'cookies_override', None)
        self._retry_context.cookies_override = dict(cookies)
        try:
            yield
        finally:
            self._retry_context.cookies_override = previous

    def get_push_url(self, room_id: int) -> Tuple[bool, dict]:
        """获取推流地址（RTMP 地址 + 推流码）"""
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
        success, resp = self._req("GET", url)
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            rtmp = data.get('rtmp', {})
            addr = rtmp.get('addr', '')
            code = rtmp.get('code', '')
            if addr and code:
                push_url = f"{addr}{code}" if not addr.endswith('/') else f"{addr}{code}"
                logger.info(f" 获取推流地址成功：{addr}...")
                return True, {'push_url': push_url, 'rtmp_addr': addr, 'rtmp_code': code}
            # 兼容其他字段名
            addr2 = data.get('rtmp_addr', '')
            code2 = data.get('rtmp_code', '')
            if addr2 and code2:
                push_url = f"{addr2}{code2}" if not addr2.endswith('/') else f"{addr2}{code2}"
                return True, {'push_url': push_url, 'rtmp_addr': addr2, 'rtmp_code': code2}
            logger.warning(f" 获取推流地址失败：响应中无 rtmp 数据")
        return False, {'msg': resp.get('msg', '获取推流地址失败')}


# ==================== AreaLoader（分区加载器） ====================
class AreaLoader:
    """直播分区动态加载器

    缓存位置：每实例数据目录（--data-dir）内的 bili_areas_full.json
    （app.core.area_data.area_cache_path），不再是代码目录下的受跟踪文件。
    没有缓存时分区功能暂不可用（areas 为空 + status 说明），但不影响面板启动。
    """

    def __init__(self, area_file: str = None):
        from app.core.config import area_file_path
        self.area_file = Path(area_file) if area_file else area_file_path()
        self.areas: List[dict] = []
        self._search_cache: dict = {}
        # 分区可用性状态：loaded / cache_missing / cache_corrupt / cache_invalid /
        # empty_response / invalid_payload / persist_failed / request_failed
        self.status: str = "unloaded"
        self.last_error: str = ""
        self._load_areas()

    def _load_areas(self):
        from app.core.area_data import read_cache
        areas, reason = read_cache(self.area_file)
        self.areas = areas if isinstance(areas, list) else []
        self.status = reason
        if self.areas:
            self.last_error = ""
            logger.info(f" 加载分区数据：{len(self.areas)} 项（{self.area_file}）")
        else:
            self.last_error = reason
            # 缺/坏都不是致命错误：面板照常启动，在线刷新或人工刷新可补齐
            logger.info(f" 分区数据暂不可用（{reason}）：{self.area_file}")

    @property
    def available(self) -> bool:
        return bool(self.areas)

    def fuzzy_search(self, keyword: str) -> List[dict]:
        """模糊搜索分区 — 子分区优先，支持拼音首字母检索"""
        if not keyword or not self.areas:
            return []
        keyword = keyword.lower().strip()
        if keyword in self._search_cache:
            return self._search_cache[keyword]

        # 扁平化所有分区（含 children）
        def flat_all(areas: list, parent_name: str = '') -> list:
            result = []
            for area in areas:
                pname = parent_name or area.get('parent_name', '')
                result.append({**area, 'parent_name': pname})
                for child in area.get('children', []):
                    result.extend(flat_all([child], area.get('name', '')))
            return result

        all_areas = flat_all(self.areas)

        # 搜索 + 子分区优先排序
        results = []
        for area in all_areas:
            name = str(area.get('name', '')).lower()
            parent = str(area.get('parent_name', '')).lower()
            pinyin = _to_pinyin_initials(name)
            matched = (
                keyword in name or
                keyword in parent or
                keyword in pinyin
            )
            if matched:
                score = 100 if area.get('parent_id', 0) != 0 else 0
                if keyword == name:
                    score += 50
                elif name.startswith(keyword):
                    score += 30
                if pinyin and keyword in pinyin:
                    score += 20  # 拼音匹配加分
                results.append((score, area))

        results.sort(key=lambda x: x[0], reverse=True)
        results = [area for _, area in results][:20]
        self._search_cache[keyword] = results
        return results

    def get_area_id(self, zone_name: str, auto_update: bool = False) -> Optional[int]:
        """根据分区名获取 area_id"""
        results = self.fuzzy_search(zone_name)
        if results:
            return results[0].get('id')
        logger.error(f" 未找到分区：{zone_name}")
        return None

    def fetch_and_save_areas(self, api: BilibiliApi) -> bool:
        """从 B 站 API 获取最新分区并写入本实例缓存。

        顺序：网络请求（不持 IO 锁）→ 结构校验 → 原子落盘 → 落盘成功后才
        切换内存数据并清搜索缓存。任何一步失败都保留当前可用数据并返回
        False（不返回假成功）。
        """
        from app.core.area_data import (
            validate_api_payload, validate_areas, write_cache)

        success, resp = api.get_areas()
        if not success or resp.get('code') != 0:
            self.status = "request_failed"
            self.last_error = str(resp.get('msg', '')) if isinstance(resp, dict) else ""
            logger.error(" 获取分区列表失败")
            return False
        data = resp.get('data', [])
        if not data:
            self.status = "empty_response"
            self.last_error = "empty_response"
            logger.error(" 获取的分区列表为空，保留当前缓存")
            return False

        def _as_id(v):
            """平台响应允许整数字符串 id；入库/内存统一规整为整数。"""
            return int(v) if isinstance(v, str) else v

        def flatten(areas, parent_id=0, parent_name=''):
            result = []
            for area in areas:
                item = {
                    "id": _as_id(area.get("id")),
                    "name": area.get("name"),
                    "parent_id": parent_id,
                    "parent_name": parent_name if parent_name else area.get("name", ""),
                    "children": []
                }
                children = area.get("list", [])
                if children:
                    item["children"] = flatten(children, _as_id(area.get("id")), area.get("name"))
                result.append(item)
            return result

        # 先按平台原始形状（list 嵌套）递归校验每一级节点；非法（含子分区缺
        # id/name、非对象元素）在这里就明确失败，不会让 flatten 抛异常。
        ok, reason = validate_api_payload(data)
        if not ok:
            self.status = "invalid_payload"
            self.last_error = reason
            logger.error(f" 获取的分区结构不合法（{reason}），保留当前缓存")
            return False

        flat = flatten(data)
        # 展平后（children 形状）再过一次同一约定，作为双保险。
        ok, reason = validate_areas(flat)
        if not ok:
            self.status = "invalid_payload"
            self.last_error = reason
            logger.error(f" 获取的分区结构不合法（{reason}），保留当前缓存")
            return False

        written, err = write_cache(self.area_file, flat)
        if not written:
            self.status = "persist_failed"
            self.last_error = err
            logger.error(f" 分区缓存写入失败（{err}），内存数据保持不变")
            return False

        self.areas = flat
        self._search_cache.clear()  # 清空旧缓存
        self.status = "loaded"
        self.last_error = ""
        logger.info(f" 成功获取并保存 {len(self.areas)} 个分区 → {self.area_file}")
        return True


# ==================== 拼音首字母工具 ====================
try:
    from pypinyin import lazy_pinyin, Style
    def _to_pinyin_initials(text: str) -> str:
        """将中文文本转换为拼音首字母（如 '王者荣耀' → 'wzry'）"""
        return ''.join(lazy_pinyin(text, style=Style.FIRST_LETTER))
except ImportError:
    def _to_pinyin_initials(text: str) -> str:
        return ''

# ==================== LiveState（直播状态持久化） ====================
#: 状态文件格式版本。旧文件缺字段时按"未知"处理，不猜成 0，也不静默清空。
STATE_SCHEMA_VERSION = 2

#: 会话来源：新建 / 恢复 / 重连 / 手动。恢复与重连都不允许重抽时长。
SOURCE_NEW = 'new'
PLATFORM_OUTAGE_ALERT_SECONDS = 300  # 已确认停播但持续重试时才提醒一次
#: `_platform_stop_live` 的返回值：guard 判定本次下发已不属于当前会话，
#: 调用方应把它当作"上下文已过期"处理，而不是"下播失败"。
PLATFORM_STOP_STALE = 'stale'
SOURCE_RESUME = 'resume'
SOURCE_RECONNECT = 'reconnect'
SOURCE_MANUAL = 'manual'
CROSS_DAY_PENDING_PREFIX = '跨日未核查存档待处理：'

#: 阶段：不是"画面健康"的同义词，只描述控制链路走到哪一步。
PHASE_IDLE = 'idle'
PHASE_STARTING = 'starting'
PHASE_LIVE = 'live'
PHASE_RECOVERING = 'recovering'
PHASE_STOPPING = 'stopping'
PHASE_BLOCKED = 'blocked'

# ==================== 有效直播时长（确认区间 + 待确认区间） ====================
# 目标：每日任务必须累计"平台确认过的连续在播时间"才完成，而不是"会话经过了多久"。
# 只有三个值，各自只有一个解释，不再和墙钟总时长混用：
#
# - ``confirmed``：累计**有效**时长（权威值）。只有当前会话所有者能推进；用于结算
#   与持久化。上次落盘后新增的区间必须重新经平台确认才算数。
# - ``pending``：最近一次可信观察之后的**待确认**区间。只允许有界显示推算，
#   不参与结算，不落盘。
# - **固定目标**：本次启动意图确定的 duration_seconds/duration_known。断流、恢复、
#   重启都不重新抽取（沿用既有随机额外时长，一次确定）。
#
# 观察新鲜度：一条"平台在播"的响应只能证明**该次查询期间**房间是在播的，证明不了
# 两次查询之间一直正常。因此给相邻两次可信观察的间隔设上限，超过就当作未知区间
# 丢弃、只重立锚点，绝不把长时间空档整段补记（宁可多播一点，也不要在没播够时结算）。
#
# 入账间隔上限的推导：监控循环每 ``monitor_interval`` 秒做一次状态查询，单次查询
# 在传输层重试后最多花掉 EFFECTIVE_QUERY_BUDGET_SECONDS。正常节奏下相邻两次
# "在播"返回之间的间隔 ≈ monitor_interval + 查询耗时，所以上限取
#     max(下限, 2 × monitor_interval + 查询预算)
# 2× 用来覆盖"一次查询变慢把下一次检查整体推迟"的情况。默认 monitor_interval=30s
# 时上限 = 90s：代价是"最多 90 秒的未观察区间仍会被当作在播"；收益是既不会因为一次
# 慢查询把正常直播判成断流，也不会把分钟级的线程阻塞整段补记。
EFFECTIVE_QUERY_BUDGET_SECONDS = 30.0
EFFECTIVE_MIN_CREDIT_GAP_SECONDS = 90.0

# 计时状态：只用于让面板选择一句简短文案，不承载任何区间/补偿明细。
TIMER_RUNNING = 'running'
TIMER_PAUSED_CLOSED = 'paused_closed'
TIMER_PAUSED_UNKNOWN = 'paused_unknown'
TIMER_PAUSED_BLOCKED = 'paused_blocked'
TIMER_PAUSED_PUSHER = 'paused_pusher'


def effective_credit_limit(monitor_interval: float) -> float:
    """相邻两次可信观察之间最多可入账的间隔（秒）。公式与代价见上方注释。"""
    try:
        interval = float(monitor_interval)
    except (TypeError, ValueError):
        interval = 0.0
    if interval < 0:
        interval = 0.0
    return max(EFFECTIVE_MIN_CREDIT_GAP_SECONDS,
               interval * 2.0 + EFFECTIVE_QUERY_BUDGET_SECONDS)


def _monotonic() -> float:
    """计时用的单调时钟（唯一读取点，可注入以便确定性验证）。

    有效时长、观察新鲜度、监控周期都以它为准：系统时钟被回拨或前跳都不会让
    进度倒退或翻倍。测试可以把它替换成"只在被推进时前进"的时钟，从而用有限的
    Event 交错复现多小时场景，而不必真的等下去。
    """
    return time.monotonic()


class EffectiveClock:
    """有效时长计时（单一所有权：只有当前会话所有者能推进或暂停）。

    计时**锚点**由控制器持有（``LiveController._segment_monotonic``），避免"锚点与
    累计值各存一份、彼此漂移"：

    - ``anchor is None`` ⇒ 暂停：没有开放区间，confirmed 不再增长；
    - ``anchor is not None`` ⇒ 正在计时，pending = now - anchor。

    本类只保存 confirmed 与新鲜度记录，自己不读时钟（``now`` 一律由调用方传入），
    因此可以用可注入的单调时钟做确定性验证。
    """

    __slots__ = ('confirmed', 'credit_limit', 'origin', 'paused_reason',
                 'last_request_start', 'last_ok_return')

    def __init__(self, credit_limit: float = EFFECTIVE_MIN_CREDIT_GAP_SECONDS,
                 origin: str = 'none'):
        self.confirmed: float = 0.0
        self.credit_limit: float = float(credit_limit)
        # 'none'（无进度）| 'live'（本次进程内确认）| 'legacy'（旧状态迁移基线）
        self.origin: str = origin
        self.paused_reason: str = ''
        self.last_request_start: Optional[float] = None
        self.last_ok_return: Optional[float] = None

    def reset(self, confirmed: float = 0.0, origin: str = 'none',
              credit_limit: Optional[float] = None) -> None:
        """重置为一次会话的起点（新开播/恢复继承/停止）。"""
        self.confirmed = max(0.0, float(confirmed or 0))
        self.origin = origin
        if credit_limit is not None:
            self.credit_limit = float(credit_limit)
        self.paused_reason = ''
        self.last_request_start = None
        self.last_ok_return = None

    def set_confirmed(self, seconds: float, origin: str = 'live') -> float:
        """抬高权威累计值（恢复继承 / 旧状态迁移 / 夹具注入）。

        只增不减：旧状态里的合法进度一次性继承，不清零、不倒扣历史。
        """
        value = max(0.0, float(seconds or 0))
        if value > self.confirmed:
            self.confirmed = value
            self.origin = origin
        return self.confirmed

    @staticmethod
    def pending(anchor: Optional[float], now: float) -> float:
        """待确认区间长度（秒）；没有锚点（暂停）时为 0。"""
        if anchor is None:
            return 0.0
        return max(0.0, float(now) - float(anchor))

    def observe_ok(self, anchor: Optional[float], request_start: float,
                   returned: float, max_credit: Optional[float] = None
                   ) -> Tuple[Optional[float], float, str]:
        """处理一次**可信在播观察**（平台明确返回 live_status == 1）。

        返回 ``(新锚点, 本次并入 confirmed 的秒数, 结论)``：

        - ``'anchor'``：此前没有锚点（新会话 / 恢复 / 暂停后首次在播）且**本次回复新鲜**
          → 只立锚点，不补算启动或离线等待；
        - ``'credited'``：与上一段连续、且间隔在可入账上限内 → 至多并入
          ``max_credit`` 秒（本地 FFmpeg 媒体推进量）；未提供时按整段入账，
          并以本次返回时刻重立锚点；
        - ``'dropped_stale'``：**本次响应本身太晚返回**（超过查询预算）→ 这个结果已经
          不代表"当时平台在播"：丢弃上一段，**并且不立新锚点**（保持无锚点，等下一次
          新鲜成功）；
        - ``'dropped_gap'``：本次回复新鲜，但距上次可信观察太久（超过可入账间隔上限）
          → 丢弃上一段，**可以**以本次返回时刻重立锚点（回复新鲜就证明"现在在播"）。

        锚点取**响应返回时刻**而不是请求开始时刻：响应只能证明"返回前后平台说在播"，
        按更保守的一端起算，避免把"还没观察到"的时间也记成有效时长。

        **新鲜度必须最先判断**（在"没有锚点就立锚"之前）：否则一条早已失去新鲜度的成功
        响应会成为下一段的起点，等于拿过期结论给接下来的整整一个入账上限授权——它证明
        不了等待期间平台在播，却让这段未知时间可能被计入任务。恢复/新会话的第一次观察
        同样受这条规则约束（无锚点不是免检通道）。
        """
        self.last_request_start = float(request_start)
        if float(returned) - float(request_start) > EFFECTIVE_QUERY_BUDGET_SECONDS:
            # 过期结果：既不并入 confirmed，也不立锚、不登记为"最后可信成功"。
            # 返回 None 让调用方保持无锚点（暂停），下一次新鲜成功才重新开始计时。
            self.paused_reason = TIMER_PAUSED_UNKNOWN
            return None, 0.0, 'dropped_stale'
        self.last_ok_return = float(returned)
        self.paused_reason = ''
        if anchor is None:
            return float(returned), 0.0, 'anchor'
        gap = float(returned) - float(anchor)
        if gap < 0:
            # 锚点比返回时刻还晚（时钟被注入/替换）：不产生负区间，重立锚点。
            return float(returned), 0.0, 'anchor'
        if gap > self.credit_limit:
            return float(returned), 0.0, 'dropped_gap'
        credited = (gap if max_credit is None else
                    min(gap, max(0.0, float(max_credit))))
        self.confirmed += credited
        return float(returned), credited, 'credited'

    def pause(self, reason: str, anchor: Optional[float],
              at: Optional[float] = None) -> float:
        """暂停计时（明确关闭 / 查询未知 / 恢复阻塞）：丢弃待确认区间，保留 confirmed。

        返回被丢弃的待确认秒数（0 表示本来就没有开放区间）。锚点由调用方（控制器）
        清空——丢弃与清锚点是同一件事的两面，必须一起做，所以这里也把它显式收进来。
        """
        dropped = self.pending(anchor, at if at is not None else 0.0)
        self.paused_reason = reason
        return dropped

    def snapshot(self, anchor: Optional[float], now: float) -> dict:
        """给 API 的计时视图：权威值 + 有界外推信息。"""
        pending = self.pending(anchor, now)
        remaining = max(0.0, self.credit_limit - pending) if anchor is not None else 0.0
        return {
            'confirmed': self.confirmed,
            'pending': pending,
            'pending_limit': self.credit_limit,
            'pending_remaining': remaining,
            'extrapolatable': bool(anchor is not None and remaining > 0),
            'state': TIMER_RUNNING if anchor is not None else (
                self.paused_reason or TIMER_PAUSED_UNKNOWN),
        }


class LiveState:
    """直播状态持久化管理器（会话快照 + 原子保存）。

    字段含义（每个字段只有一个解释）：
    - ``run_id``：本场直播（会话）身份。换任务/新开播都换；重连不变。
    - ``source_mode``：这次会话是 new / resume / reconnect / manual。
    - ``duration_known``：目标时长是否已知。**未知 != 0**，0 只表示业务"不限时"。
    - ``effective_seconds``：累计**有效**时长（权威值，保留小数精度，避免每次检查
      取整造成持续少算）。区间须经平台在播观察；本机 FFmpeg 模式还须避开已知
      本地进程断档。它不等于观众端实际观看时长。
    - ``elapsed_seconds``/``accumulated_seconds``：``effective_seconds`` 的整数
      兼容别名（旧版本、回退目标 cc191db、跨仓契约夹具都只认它）。别名**可写**：
      给 ``elapsed_seconds`` 赋值会同步改写 ``effective_seconds``，因此任何按旧字段
      写入的代码都不会与权威值分叉。
    - ``resumable``：可恢复性由统一判定给出（见 :meth:`resumable`），不再让
      "分区名非空"与"另一个布尔"各说一套。
    """

    def __init__(self, state_file: str = "live_state.json"):
        self.state_file = Path(state_file)
        # State snapshots and their temporary files are shared by controller,
        # monitor and shutdown threads.  Keep persistence operations serialized
        # per LiveState instance; the reentrant lock also covers checkpoint and
        # stop helpers that call save() recursively.
        self._state_io_lock = threading.RLock()
        self.is_streaming = False
        self.current_zone = ""
        # 先建权威值，再写兼容别名（别名 setter 会同步到权威值）。
        # 累计**有效**时长（权威值，保留小数精度）。
        self.effective_seconds: float = 0.0
        # 'none' | 'legacy'（旧状态迁移基线）| 'live'
        self.effective_origin: str = 'none'
        self.elapsed_seconds = 0
        self.room_id = 0
        self.start_time: Optional[str] = None  # ISO 格式
        # 保留任务进度与允许自动开播是两个独立状态。
        self.auto_resume = True  # 兼容无此字段的旧状态文件

        self.schema_version = STATE_SCHEMA_VERSION
        self.run_id: str = ''
        self.service_instance: str = ''
        self.session_version: int = 0
        self.source_mode: str = SOURCE_NEW
        self.phase: str = PHASE_IDLE
        self.task_id: Optional[int] = None
        self.execution_date: Optional[str] = None
        self.duration_seconds: int = 0
        self.duration_known: bool = False
        self.accumulated_seconds: int = 0
        self.resume_blocked_reason: str = ''
        self.stop_blocked_reason: str = ''
        # 状态文件损坏/半写：必须显式暴露，不能解释成"没有任务"。
        self.corrupt: bool = False
        self._last_save_error: str = ''
        self._checkpoint_failure_active: bool = False
        # begin_session 的持久化结果（True=live_state 已确认落盘）。失败时
        # 内存已切换而磁盘保持上一份完整状态，接管方据此拒绝"先宣称成功"。
        self.last_session_persist_ok: bool = True
        self._checkpoint_marker_file = self.state_file.parent / '.resume_checkpoint_failed'
        self._load()

    # ---------- 兼容别名（可读写，始终与权威值一致） ----------

    @property
    def elapsed_seconds(self) -> int:
        """已确认有效时长的整数别名（展示/兼容用）。"""
        return int(getattr(self, 'effective_seconds', 0.0) or 0.0)

    @elapsed_seconds.setter
    def elapsed_seconds(self, value):
        # 任何按旧字段写入的代码（旧版本回退、外部手工改状态、跨仓契约夹具）
        # 都必须落到权威值上，否则"写 elapsed、读 effective"会分叉成两份进度。
        self.effective_seconds = _safe_float(value, 0.0)

    # ---------- 持久化 ----------

    def _load(self):
        marker_path = self._checkpoint_marker_file
        marker_present = marker_path.exists()
        marker_reason = ''
        marker_state = None
        if marker_present:
            try:
                marker_text = marker_path.read_text(encoding='utf-8')
                try:
                    marker_data = json.loads(marker_text)
                except (TypeError, ValueError):
                    marker_data = None
                if isinstance(marker_data, dict):
                    marker_reason = str(marker_data.get('reason') or '')
                    marker_state = marker_data.get('state')
                else:
                    marker_reason = marker_text.strip()
            except OSError as exc:
                marker_reason = f'恢复状态保存失败标记无法读取（{exc}）'
        data = None
        state_error = None
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                state_error = e
        if isinstance(marker_state, dict):
            # Failed progress saves write the latest in-memory session to this
            # independent marker. Prefer it only when it is at least as recent as
            # the normal state file, preserving the last known identity/progress.
            marker_update = str(marker_state.get('last_update') or '')
            state_update = str((data or {}).get('last_update') or '')
            if data is None or marker_update >= state_update:
                data = marker_state
                state_error = None
        if data is None:
            if state_error is not None:
                # 文件损坏/写到一半：保留现场并标记，绝不覆盖、绝不解释成"没有任务"。
                self.corrupt = True
                self._last_save_error = f'状态文件无法解析：{state_error}'
                logger.error(f" 状态文件损坏，已保留现场不做覆盖：{self.state_file}（{state_error}）")
            elif not marker_present:
                return
            else:
                self.corrupt = True
                self.resume_blocked_reason = (marker_reason or
                    '恢复进度保存失败，但没有可读取的会话快照；已阻断自动续播')
                self._checkpoint_failure_active = True
                self.auto_resume = False
                logger.error("恢复状态失败标记存在但无会话快照，已阻断自动续播")
                return
        if not isinstance(data, dict):
            self.corrupt = True
            self._last_save_error = '状态文件内容不是对象'
            logger.error(f" 状态文件内容非法，已保留现场：{self.state_file}")
            return
        # 一般旧日空状态可清理；带着分区/进度的旧存档可能仍未结算，不能
        # 在加载时静默清零，更不能让今天的新任务覆盖它。
        last_update = data.get('last_update', '')
        cross_day_with_session = False
        if last_update:
            try:
                last_date = datetime.fromisoformat(last_update).date()
                if last_date != date.today():
                    cross_day_with_session = bool(data.get('current_zone'))
                    if not cross_day_with_session:
                        logger.info(f"状态文件来自 {last_date}（非今天），已清理空状态")
                        self._reset_memory()
                        self.save()
                        return
            except ValueError:
                pass
        self.is_streaming = bool(data.get('is_streaming', False))
        self.current_zone = data.get('current_zone', '') or ''
        legacy_elapsed = _safe_int(data.get('elapsed_seconds'), 0)
        if 'effective_seconds' in data:
            # 新状态：有效时长是权威值，elapsed/accumulated 是它的兼容别名。
            self.effective_seconds = _safe_float(data.get('effective_seconds'),
                                                 float(legacy_elapsed))
            self.effective_origin = str(data.get('effective_origin') or 'live')
        else:
            # 旧状态（无有效时长字段）：把**合法的旧 elapsed** 作为一次性迁移基线
            # 继承，并标注 legacy 来源。过去这段时间里是否包含断流无法追溯——
            # 不倒扣历史、不清零、也不改任务完成天数；重启后新增的区间必须重新确认。
            self.effective_seconds = float(legacy_elapsed)
            self.effective_origin = 'legacy' if legacy_elapsed > 0 else 'none'
        self.accumulated_seconds = _safe_int(data.get('accumulated_seconds'),
                                             self.elapsed_seconds)
        self.accumulated_seconds = max(self.accumulated_seconds,
                                       self.elapsed_seconds)
        self.room_id = _safe_int(data.get('room_id'), 0)
        self.duration_seconds = _safe_int(data.get('duration_seconds'), 0)
        # 旧文件没有 duration_known：有分区且时长>0 视为已知；否则未知（不猜 0）。
        if 'duration_known' in data:
            self.duration_known = bool(data.get('duration_known'))
        else:
            self.duration_known = bool(self.current_zone) and self.duration_seconds > 0
        self.start_time = data.get('start_time')
        self.auto_resume = bool(data.get('auto_resume', True))
        self.schema_version = _safe_int(data.get('schema_version'), 1)
        self.run_id = data.get('run_id') or ''
        self.service_instance = data.get('service_instance') or ''
        self.session_version = _safe_int(data.get('session_version'), 0)
        self.source_mode = data.get('source_mode') or SOURCE_NEW
        self.phase = data.get('phase') or PHASE_IDLE
        self.task_id = data.get('task_id')
        if self.task_id is not None:
            try:
                self.task_id = int(self.task_id)
            except (TypeError, ValueError):
                self.task_id = None
        self.execution_date = data.get('execution_date')
        self.resume_blocked_reason = data.get('resume_blocked_reason') or ''
        if marker_present:
            self._checkpoint_failure_active = True
            self.is_streaming = False
            self.auto_resume = False
            self.phase = PHASE_IDLE
            self.resume_blocked_reason = (
                marker_reason or self.resume_blocked_reason
                or '直播进度保存失败，已保留现场并阻断自动续播')
            logger.error("检测到恢复状态保存失败标记，已载入保护快照并阻断自动续播")
        if cross_day_with_session:
            previous_reason = str(self.resume_blocked_reason or '').strip()
            self.is_streaming = False
            self.auto_resume = False
            self.phase = PHASE_IDLE
            suffix = f'（原执行日 {self.execution_date or "未知"}）'
            if previous_reason:
                suffix += f'；原状态：{previous_reason}'
            self.resume_blocked_reason = CROSS_DAY_PENDING_PREFIX + suffix
            logger.error(
                "发现跨日且仍有分区/进度的旧状态，已保留原身份与进度并阻断自动续播：%s",
                self.state_file)
            self.save()
        if self.effective_origin == 'legacy':
            logger.info(
                "状态缺少有效时长字段：把旧的已播 %s 秒作为迁移基线一次性继承"
                "（来源标注 legacy；过去是否包含断流无法追溯，不清零不倒扣）",
                self.elapsed_seconds)
        if self.is_streaming:
            logger.debug(
                f"恢复直播状态：分区={self.current_zone}, "
                f"已播={self.elapsed_seconds // 60}分钟")

    def _reset_memory(self):
        self.is_streaming = False
        self.current_zone = ''
        self.effective_seconds = 0.0
        self.effective_origin = 'none'
        self.accumulated_seconds = 0
        self.room_id = 0
        self.duration_seconds = 0
        self.duration_known = False
        self.start_time = None
        self.phase = PHASE_IDLE
        self.source_mode = SOURCE_NEW
        self.task_id = None
        self.execution_date = None
        self.run_id = ''
        self.session_version = 0
        self.resume_blocked_reason = ''

    def to_dict(self) -> dict:
        # elapsed_seconds/accumulated_seconds 是 effective_seconds 的兼容别名：
        # 旧版本（含回退目标 cc191db）只认它们，回退期间读到的仍是合理进度。
        effective = max(0.0, float(self.effective_seconds or 0))
        return {
            'schema_version': STATE_SCHEMA_VERSION,
            'run_id': self.run_id,
            'service_instance': self.service_instance,
            'session_version': self.session_version,
            'source_mode': self.source_mode,
            'phase': self.phase,
            'task_id': self.task_id,
            'execution_date': self.execution_date,
            'is_streaming': self.is_streaming,
            'current_zone': self.current_zone or '',
            'effective_seconds': round(effective, 3),
            'effective_origin': self.effective_origin or 'none',
            'elapsed_seconds': int(effective),
            'accumulated_seconds': int(effective),
            'room_id': self.room_id,
            'duration_seconds': int(self.duration_seconds),
            'duration_known': bool(self.duration_known),
            'auto_resume': self.auto_resume,
            'resume_blocked_reason': self.resume_blocked_reason,
            'start_time': self.start_time or datetime.now().isoformat(),
            'last_update': datetime.now().isoformat(),
        }

    def save(self) -> bool:
        """原子保存：临时文件 + fsync + 读回校验 + os.replace。

        失败时保留磁盘上的上一个完整状态并显式报错，绝不"假成功"。
        """
        with self._state_io_lock:
            # Snapshot under the same lock as write/replace so overlapping saves
            # cannot share a PID-named temp file or validate another writer's data.
            data = self.to_dict()
            target = self.state_file
            tmp = target.with_name(
                f'{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                # 读回校验：写坏的内容不会替换掉好的状态
                with open(tmp, 'r', encoding='utf-8') as f:
                    check = json.load(f)
                if check.get('current_zone', '') != data['current_zone'] or \
                        int(check.get('elapsed_seconds', -1)) != int(data['elapsed_seconds']):
                    raise IOError('状态文件读回校验不一致')
                os.replace(tmp, target)
                self._last_save_error = ''
                return True
            except Exception as e:
                self._last_save_error = str(e)
                logger.error(f" 保存状态失败（已保留上一个完整状态）：{e}")
                try:
                    if tmp.exists():
                        os.unlink(tmp)
                except OSError:
                    pass
                return False

    def _record_checkpoint_failure(self, stage: str, error: str) -> str:
        """独立保存失败证据和最新会话快照，防止下一次重启静默归零。"""
        with self._state_io_lock:
            detail = str(error or '状态文件原子写入未确认成功')
            reason = f'{stage}保存失败（{detail}）；已保留恢复现场并阻断自动续播'
            self._checkpoint_failure_active = True
            self.auto_resume = False
            self.resume_blocked_reason = reason
            marker = self._checkpoint_marker_file
            tmp = marker.with_name(
                f'{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                payload = {'reason': reason, 'state': self.to_dict()}
                with open(tmp, 'w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, marker)
            except Exception as exc:
                logger.critical('无法写入恢复失败保护快照：%s', exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            logger.error(reason)
            return reason

    def _save_checkpoint(self, stage: str) -> bool:
        """保存会话；只有状态文件成功且失败标记已清除才解除恢复阻断。"""
        with self._state_io_lock:
            if self._checkpoint_failure_active:
                self.auto_resume = bool(self.current_zone)
                self.resume_blocked_reason = ''
            if not self.save():
                error = (self._last_save_error or '状态文件原子写入未确认成功')
                self._record_checkpoint_failure(stage, error)
                return False
            if self._checkpoint_marker_file.exists():
                try:
                    self._checkpoint_marker_file.unlink()
                except OSError as exc:
                    self._record_checkpoint_failure(
                        f'{stage}恢复标记清理', str(exc))
                    return False
            self._checkpoint_failure_active = False
            return True

    # ---------- 会话生命周期 ----------

    def begin_session(self, zone_name: str, room_id: int, duration_seconds: int,
                      duration_known: bool = True, source: str = SOURCE_NEW,
                      task_id: Optional[int] = None,
                      execution_date: Optional[str] = None,
                      initial_elapsed: int = 0,
                      service_instance: str = '',
                      run_id: Optional[str] = None) -> str:
        """开启一个会话（新开播/恢复）。重连**不**调用它。

        ``run_id`` 由调用方在**意图登记时**冻结后传入：恢复/验证后重试都沿用
        同一个身份，不会因为中间多等了一会儿就变成另一场会话。
        """
        with self._state_io_lock:
            self.is_streaming = True
            self.current_zone = zone_name
            self.room_id = room_id
            self.duration_seconds = int(duration_seconds or 0)
            self.duration_known = bool(duration_known)
            self.source_mode = source
            self.phase = PHASE_STARTING
            self.task_id = task_id
            self.execution_date = execution_date or date.today().isoformat()
            inherited = max(0.0, float(initial_elapsed or 0))
            self.effective_seconds = inherited
            # 继承来的进度的"来源"跟随旧值：恢复（沿用存档）或本次进程内确认。
            self.effective_origin = ('resume' if source == SOURCE_RESUME and inherited > 0
                                     else ('live' if inherited > 0 else 'none'))
            self.accumulated_seconds = self.elapsed_seconds
            self.start_time = datetime.now().isoformat()
            self.auto_resume = True
            self.resume_blocked_reason = ''
            self.corrupt = False
            if service_instance:
                self.service_instance = service_instance
            if source != SOURCE_RECONNECT:
                self.run_id = run_id or f"{int(time.time()):x}-{uuid.uuid4().hex[:8]}"
                self.session_version = 1
            else:
                self.session_version += 1
            # 持久化结果必须可被调用方审计：内存已切换、磁盘未必跟上。失败时
            # 返回值仍是 run_id（十几个调用方依赖该签名），但
            # ``last_session_persist_ok=False`` 与 ``_checkpoint_failure_active``
            # 让接管方能够拒绝"先宣称成功"。重启语义：保存失败 = 磁盘仍是
            # 上一份完整状态（本例即旧会话），旧进度随文件恢复，不会因半次
            # 接管丢失。
            self.last_session_persist_ok = self._save_checkpoint('会话状态')
            return self.run_id

    def start_streaming(self, zone_name: str, room_id: int):
        """兼容入口：无时长信息的新开播（时长视为未知）。"""
        self.begin_session(zone_name, room_id, 0, duration_known=False,
                           source=SOURCE_NEW)
        logger.info(f" 直播状态已保存：开始 {zone_name}")

    def start_streaming_with_duration(self, zone_name: str, room_id: int,
                                      duration_seconds: int,
                                      initial_elapsed: int = 0,
                                      **kwargs):
        """带时长信息的开始直播（兼容旧签名）。"""
        self.begin_session(zone_name, room_id, duration_seconds,
                           duration_known=True, initial_elapsed=initial_elapsed,
                           **kwargs)

    def mark_phase(self, phase: str, persist: bool = False) -> bool:
        with self._state_io_lock:
            self.phase = phase
            if persist:
                return self._save_checkpoint('直播阶段状态')
            return True

    def update_progress(self, elapsed: float,
                        expected_run_id: Optional[str] = None) -> Optional[bool]:
        """持久化可恢复的累计**有效**时长（监控周期调用，每次入账一次）。

        只接受已经确认过的值：待确认区间不落盘，重启后最多恢复到最后一次成功
        保存的保守进度，绝不编造未落盘的时间。
        """
        with self._state_io_lock:
            # A late monitor tick after stop/clear or session takeover must not
            # repopulate a stopped/empty state or attach progress to the new run.
            if not self.current_zone:
                return None if expected_run_id is not None else False
            if expected_run_id is not None and (
                    str(expected_run_id) != str(self.run_id or '')
                    or self.phase != PHASE_LIVE):
                return None
            value = max(0.0, float(elapsed or 0))
            self.effective_seconds = max(float(self.effective_seconds or 0), value)
            if self.effective_origin == 'none' and self.effective_seconds > 0:
                self.effective_origin = 'live'
            self.accumulated_seconds = self.elapsed_seconds
            return self._save_checkpoint('直播进度')

    def stop_streaming(self, preserve: bool = False):
        """停止/清空会话；只有确认保存后才返回成功。"""
        with self._state_io_lock:
            previous = self.__dict__.copy()
            if preserve:
                self.is_streaming = False
                self.phase = PHASE_IDLE
                self.effective_seconds = max(float(self.effective_seconds or 0),
                                             float(self.elapsed_seconds or 0))
                self.accumulated_seconds = max(self.accumulated_seconds,
                                               self.elapsed_seconds)
                saved = self._save_checkpoint('恢复状态')
            else:
                self._reset_memory()
                saved = self.save()
            if not saved:
                error = (getattr(self, '_last_save_error', '')
                         or '状态文件原子写入未确认成功')
                self.__dict__.clear()
                self.__dict__.update(previous)
                self._last_save_error = error
                self._record_checkpoint_failure(
                    '恢复状态' if preserve else '状态文件清理', error)
                return False
            if not preserve:
                self._checkpoint_failure_active = False
                try:
                    self._checkpoint_marker_file.unlink(missing_ok=True)
                except OSError as exc:
                    self.__dict__.clear()
                    self.__dict__.update(previous)
                    self._record_checkpoint_failure('状态文件清理恢复标记', str(exc))
                    return False
            if not preserve:
                logger.info(" 直播状态已重置")
            return True

    def reload(self):
        """重新从文件加载状态（方便手动编辑 live_state.json 后刷新）"""
        self._load()
        logger.debug(
            f"已重新加载状态文件：is_streaming={self.is_streaming}, "
            f"zone={self.current_zone}")

    # ---------- 可恢复性 / 校验 ----------

    def resumable(self) -> bool:
        """统一的可恢复判定：不再用"分区名非空"或另一个布尔各说一套。"""
        if self._checkpoint_failure_active:
            return False
        if self.corrupt:
            return False
        if str(self.resume_blocked_reason or '').startswith(
                CROSS_DAY_PENDING_PREFIX):
            return False
        if not self.current_zone:
            return False
        if not self.duration_known:
            # 目标时长未知也可以恢复（按已保存时长继续），但不能静默归零
            return True
        return True

    def resume_reason(self) -> str:
        """不可恢复时给出原因（可操作，不留空白）。"""
        if self._checkpoint_failure_active and self.resume_blocked_reason:
            return self.resume_blocked_reason
        if str(self.resume_blocked_reason or '').startswith(
                CROSS_DAY_PENDING_PREFIX):
            return self.resume_blocked_reason
        if self.corrupt:
            return '状态文件损坏，已保留现场未覆盖；请修复或清空后重试'
        if not self.current_zone:
            return '没有可恢复的会话（未保存分区）'
        return ''

    def matches_identity(self, zone_name: str = None,
                         task_id: Optional[int] = None,
                         execution_date: Optional[str] = None) -> bool:
        """恢复意图的身份核对：同名但换了记录/换了执行日都不算同一个任务。"""
        if zone_name is not None and self.current_zone != zone_name:
            return False
        if task_id is not None and self.task_id is not None \
                and int(task_id) != int(self.task_id):
            return False
        if execution_date is not None and self.execution_date \
                and self.execution_date != execution_date:
            return False
        return True

    def update_state(self, **kwargs):
        """更新状态字段并保存（仅在**未推流**时允许直接改恢复参数）。"""
        allowed = {
            'is_streaming': (bool,),
            'current_zone': (str,),
            'elapsed_seconds': (int,),
            'duration_seconds': (int,),
            'room_id': (int,),
        }
        validated = {}
        for key, val in kwargs.items():
            if key not in allowed:
                continue
            expected = allowed[key]
            # bool 是 int 的子类：True 不能当成 duration_seconds=1 混进来
            if expected == (int,) and (isinstance(val, bool)
                                       or not isinstance(val, int)):
                raise ValueError(f'字段 {key} 类型不合法：{type(val).__name__}')
            if not isinstance(val, expected):
                raise ValueError(f'字段 {key} 类型不合法：{type(val).__name__}')
            if expected == (int,) and val < 0:
                raise ValueError(f'字段 {key} 不能为负数：{val}')
            validated[key] = val

        with self._state_io_lock:
            previous = self.__dict__.copy()
            changed = False
            for key, val in validated.items():
                if key == 'duration_seconds':
                    if not self.duration_known:
                        self.duration_known = True
                        changed = True
                if getattr(self, key, None) != val:
                    setattr(self, key, val)
                    changed = True
                    logger.info(f" 状态更新：{key} = {val}")
            if changed:
                if kwargs.get('elapsed_seconds') is not None:
                    # 手工改写已播进度：elapsed 的可写别名已经把权威值一起改掉了，
                    # 这里只需同步 accumulated 与来源标注。
                    if self.effective_origin == 'none' and self.effective_seconds > 0:
                        self.effective_origin = 'live'
                    self.accumulated_seconds = self.elapsed_seconds
                try:
                    saved = self.save()
                except Exception as exc:
                    saved = False
                    save_error = str(exc)
                else:
                    save_error = (getattr(self, '_last_save_error', '')
                                  or '状态文件原子写入未确认成功')
                if saved is False:
                    self.__dict__.clear()
                    self.__dict__.update(previous)
                    self._last_save_error = save_error
                    self._record_checkpoint_failure('修改恢复参数', save_error)
                    raise OSError(self.resume_blocked_reason)
            return changed

    def complete_task(self):
        """完成任务时的状态重置"""
        self.stop_streaming()

    def is_cross_day(self) -> bool:
        if not self.start_time:
            return False
        try:
            start = datetime.fromisoformat(self.start_time)
            return start.date() != date.today()
        except Exception:
            return False

    def get_status(self) -> dict:
        return {
            'is_streaming': self.is_streaming,
            'current_zone': self.current_zone,
            'elapsed_seconds': self.elapsed_seconds,
            'room_id': self.room_id,
            'start_time': self.start_time,
        }

    def snapshot(self) -> dict:
        """对外暴露的会话快照（每个字段含义单一）。"""
        return {
            'run_id': self.run_id,
            'service_instance': self.service_instance,
            'session_version': self.session_version,
            'source_mode': self.source_mode,
            'phase': self.phase,
            'task_id': self.task_id,
            'execution_date': self.execution_date,
            'current_zone': self.current_zone or '',
            # 存档里的 elapsed_seconds 就是**已确认的有效时长**（整数显示别名）；
            # 待确认区间不落盘，因此存档值天然是保守值。
            'elapsed_seconds': int(self.effective_seconds),
            'effective_origin': self.effective_origin or 'none',
            'duration_seconds': int(self.duration_seconds),
            'duration_known': bool(self.duration_known),
            'room_id': self.room_id,
            'resumable': self.resumable(),
            'resume_blocked_reason': self.resume_blocked_reason,
            'auto_resume': self.auto_resume,
            'state_corrupt': self.corrupt,
            'last_save_error': self._last_save_error,
        }


def _safe_int(value, default: int = 0) -> int:
    """把外部/旧文件里的值收敛成非负整数（NaN/None/负数都不静默通过）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _safe_float(value, default: float = 0.0) -> float:
    """把外部/旧文件里的值收敛成非负浮点数（NaN/None/负数都不静默通过）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result if result >= 0 else default


# ==================== LiveController（核心控制器重构） ====================
class LiveController:
    """直播控制器（重构版，去除 GUI 依赖，面向 API）"""

    _CLASS_FALLBACK_LOCK = threading.Lock()

    # 类级默认：使用 __new__ 构造的测试替身也能安全读取这些字段。
    _status_query_failures = 0      # 连续状态查询失败次数（未知，不代表封禁）
    _status_query_failures_total = 0  # 累计处理过的未知查询失败次数（只增不减）
    _ffmpeg_unrecycled = False      # 上一次 FFmpeg 终止未在超时内确认回收
    _stop_cleanup_done = False      # 上一次停止是否确认完成（重复停止只确认）
    _platform_stop_done = False     # 平台下播是否已确认下发（失败可重试，成功不重复）
    # 控制代际与推流代际：由 __init__ 覆盖为实例字段；这里只作测试替身的兜底。
    _control_epoch = 0
    # 已接受的开播意图序号：每次 start_streaming 成功受理时 +1。
    # 用于区分“停止之后没有新意图”与“停止之后用户已新开播”——迟到的重连
    # 响应只在意图序号未变时才允许补偿下播，绝不代替新意图清理房间。
    _start_intent_id = 0
    _operation_seq = 0
    _operation_token_limit = 4096
    # 服务进程启动身份：签发票据带上前缀，服务重启后旧票据被识别为“别的
    # 进程的票据”而拒绝，不会与新一代的序号撞车。按进程随机一次即可，
    # 因此类属性在导入时求值一次，全部实例共享。
    _boot_id = secrets.token_hex(4)
    _pusher_generation = 0
    _video_process_generation = -1
    # 每个推流代只交付一次“只排空”兜底消费者，防止重复消费者与忙循环。
    _drain_owner_generation = -1
    _drain_owner = None
    # 会话/事件相关字段的类级兜底：`__new__` 构造的测试替身也要能安全读取。
    _segment_monotonic = None
    # 有效时长计时组件与本次会话实际使用的监控周期（由 _monitor_streaming 写入）。
    _effective_clock = None
    _monitor_interval_seconds = MONITOR_INTERVAL
    _stop_progress_accumulated = 0
    _current_source = SOURCE_NEW
    _pending_source = SOURCE_NEW
    _pending_inherit = 0
    _active_run_id = None
    _verify_retry_mode = None
    # 本场会话（内存中的当前直播）身份。手动模式**不写** live_state，因此
    # 必须有控制器自己持有的 run_id，否则状态接口只能退回上一个任务的存档。
    _current_run_id = ''
    _pending_run_id = ''
    _event_seq = 0
    _event_lock = threading.Lock()
    _backend_events = None

    def _clock(self) -> EffectiveClock:
        """有效时长计时组件（唯一实例；替身按需惰性创建）。"""
        clock = self.__dict__.get('_effective_clock')
        if clock is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                clock = self.__dict__.get('_effective_clock')
                if clock is None:
                    clock = EffectiveClock(
                        effective_credit_limit(
                            getattr(self, '_monitor_interval_seconds',
                                    MONITOR_INTERVAL)))
                    self._effective_clock = clock
        return clock

    def _pusher_mutex(self) -> threading.Lock:
        """推流代际锁：实例字段优先，替身按需惰性创建。"""
        lock = self.__dict__.get('_pusher_lock')
        if lock is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                lock = self.__dict__.get('_pusher_lock')
                if lock is None:
                    lock = threading.Lock()
                    self._pusher_lock = lock
        return lock

    def _start_mutex(self) -> threading.Lock:
        lock = self.__dict__.get('_start_lock')
        if lock is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                lock = self.__dict__.get('_start_lock')
                if lock is None:
                    lock = threading.Lock()
                    self._start_lock = lock
        return lock

    def _auth_mutex(self) -> threading.RLock:
        """认证提交锁；只保护内存/凭据文件短临界区，不包住平台请求。"""
        lock = self.__dict__.get('_auth_lock')
        if lock is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                lock = self.__dict__.get('_auth_lock')
                if lock is None:
                    lock = threading.RLock()
                    self._auth_lock = lock
        return lock

    def _auth_ledger(self) -> _AuthLogoutLedger:
        ledger = self.__dict__.get('_auth_logout_ledger')
        if ledger is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                ledger = self.__dict__.get('_auth_logout_ledger')
                if ledger is None:
                    records = {}
                    loader = getattr(getattr(self, 'api', None),
                                     'get_auth_revocations', None)
                    if callable(loader):
                        records = loader()
                    ledger = _AuthLogoutLedger(records=records)
                    self._auth_logout_ledger = ledger
        # Tests and controlled reloads may replace ``api`` after controller
        # construction; import its durable ledger before the first operation.
        if not ledger.snapshot():
            loader = getattr(getattr(self, 'api', None),
                             'get_auth_revocations', None)
            if callable(loader):
                records = loader()
                if records:
                    ledger = _AuthLogoutLedger(records=records)
                    self._auth_logout_ledger = ledger
        return ledger

    def _persist_auth_ledger(self) -> bool:
        saver = getattr(getattr(self, 'api', None),
                        'save_auth_revocations', None)
        if not callable(saver):
            return False
        return bool(saver(self._auth_ledger().snapshot()))

    def _register_qr_intent(self, qrcode_key: str, generation: int) -> bool:
        """登记真实签发的二维码及其认证代际；满额时拒绝新意图。"""
        if not isinstance(qrcode_key, str) or not qrcode_key:
            return False
        entries = self.__dict__.setdefault('_auth_qr_intents',
                                            collections.OrderedDict())
        if qrcode_key not in entries and len(entries) >= 1024:
            return False
        entries[qrcode_key] = (int(generation),
                               time.time() + 5 * 60)
        return True

    def _claim_qr_intent(self, qrcode_key: str, generation: int):
        """返回真实签发二维码的归属代际；未知 key 一律拒绝。"""
        entries = self.__dict__.setdefault('_auth_qr_intents',
                                            collections.OrderedDict())
        entry = entries.get(qrcode_key)
        if entry is None:
            return None
        if entry[1] <= time.time() or entry[0] != generation:
            return None
        return entry[0]

    @contextmanager
    def _auth_commit_guard(self):
        """认证提交与开播受理共用短锁，锁序固定为控制锁→认证锁。"""
        with self._start_mutex():
            with self._auth_mutex():
                yield

    def _tokens(self) -> _OperationTokens:
        tokens = self.__dict__.get('_operation_tokens')
        if tokens is not None and tokens.boot_id != self._boot_id:
            # 服务重启（boot 身份变化）：旧进程的登记全部作废——
            # 旧票据随后会按“别的进程的票据”拒绝，不会与新一代序号撞车。
            tokens = None
        if tokens is None:
            with LiveController._CLASS_FALLBACK_LOCK:
                tokens = self.__dict__.get('_operation_tokens')
                if tokens is not None and tokens.boot_id == self._boot_id:
                    return tokens
                tokens = _OperationTokens(self._operation_token_limit,
                                          boot_id=self._boot_id)
                self._operation_tokens = tokens
        return tokens

    def __init__(
        self,
        task_manager: 'TaskManager' = None,
        state_file: str = "live_state.json",
        area_file: str = "bili_areas_full.json"
    ):
        self.task_manager = task_manager
        self.api = BilibiliApi()
        self.area_loader = AreaLoader(area_file)
        self.video_finder = VideoPathFinder()
        self.state = LiveState(state_file)

        # B站认证所有权与推流控制代际分开：认证请求不能借用停止/开播票据。
        self._auth_lock = threading.RLock()
        self._auth_generation = 0
        # 仅复用已完成的登录查询结果供邮件署名；通知线程不为此另访平台。
        self._account_display_name = ''
        self._auth_logout_ledger = _AuthLogoutLedger(
            records=self.api.get_auth_revocations())
        self._auth_qr_intents = collections.OrderedDict()

        self.current_room_id: Optional[int] = None
        self.is_streaming = False
        self.stream_start_time: Optional[datetime] = None
        self.current_instruction: Optional[LiveInstruction] = None
        self.stop_monitor = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None

        # 视频进程
        self.video_process: Optional[subprocess.Popen] = None

        # 重连相关
        self.reconnect_attempts = 0
        self.max_reconnect = MAX_RECONNECT_ATTEMPTS
        self._retry_cooldown_until: Optional[datetime] = None  # 冷却结束时间
        self._retry_window_start: Optional[datetime] = None    # 当前重试窗口起点

        # 流模式：'task'（任务模式）| 'manual'（手动模式）| None
        self._stream_mode: Optional[str] = None
        # 会话快照相关：单调时钟锚点（不受系统时间回拨/后台节流影响）与来源
        self._segment_monotonic: Optional[float] = None
        # 本场使用本机 FFmpeg 时，平台房间在播不足以证明本地仍在送媒体。
        # 只在真正启动本地循环时置位；OBS/外部推流沿用平台观察口径。
        self._local_pusher_required = False
        # 有效时长计时（见 EffectiveClock）：只有当前会话所有者能推进/暂停。
        self._effective_clock = EffectiveClock(
            effective_credit_limit(MONITOR_INTERVAL))
        self._monitor_interval_seconds = MONITOR_INTERVAL
        self._stop_progress_accumulated: int = 0
        self._current_source: str = SOURCE_NEW
        self._pending_source: str = SOURCE_NEW
        self._pending_inherit: int = 0
        self._active_run_id: Optional[str] = None
        # 冻结的会话身份：在**意图登记时**生成，之后整个等待/恢复过程不再变。
        self._pending_run_id: str = ''
        self._current_run_id: str = ''

        # FFmpeg 推流状态
        self._ffmpeg_loop_thread: Optional[threading.Thread] = None
        self._ffmpeg_stop_event = threading.Event()  # 独立信号：停止 FFmpeg 循环
        self.ffmpeg_current_video: str = ''   # 当前正在推流的视频文件名

        # 人脸验证状态
        self._pending_face_verify: bool = False
        self._face_verify_url: str = ''
        self._face_verify_run_id: str = ''
        self._face_verify_epoch: Optional[int] = None
        self._face_verify_intent: Optional[dict] = None
        # 面板/邮件可能在 60024 通知仍阻塞时确认。此槽位把已冻结的确认交给
        # 原 start worker finally；不会因等待超时而丢掉唯一一次确认。
        self._queued_face_verify_retry_intent: Optional[dict] = None
        self._recovery_blocked: str = ''
        # 只记录已由平台状态明确确认的断播；状态查询未知不建故障。
        self._platform_outage: Optional[dict] = None
        self._platform_outage_seq = 0
        self._start_lock = threading.Lock()
        self._start_thread = None
        self._start_cancel = threading.Event()
        # 交接状态机的唯一写边界（RLock：record→superseded→finalize 会重入）。
        # record/bind/finalize 的"读文件、身份核对、条件修改、原子持久写"
        # 全部在它之内完成——`_atomic_write_json` 的 os.replace 只防半截
        # 文件，不防"校验通过后、写入前"被并发替换的记录覆盖新交接（RMW）。
        # 锁序（只许单向嵌套，防死锁）：
        #   _start_lock/_start_mutex → state._state_io_lock → _handover_lock
        # 本锁是叶子锁：持有时不得再请求 _start_lock 或 _state_io_lock，
        # 锁内只做交接文件的读写，绝不等待平台网络、FFmpeg、线程 join 或 SMTP。
        self._handover_lock = threading.RLock()
        # 当前开播意图冻结的交接要求身份（2026-09-28 R3）：用户确认放弃旧
        # 进度时，受理把交接记录的 handover_id 绑定进本次意图；没有确认放弃
        # 的普通开播/恢复/重连一律为 None——磁盘上的历史终态记录（void/
        # abandoned/superseded）对这类意图是无关历史，不得阻断接管。
        # 该身份从受理穿过后台 worker、deferred takeover 到最终结论，
        # 绝不读取共享的"上一场 handover"字段冒充当前意图。
        self._pending_handover_id: Optional[str] = None
        self._last_handover_id: Optional[str] = None
        # 自然完成 A 后的单次轮转结果：待绑定槽只按下一条指令对象身份匹配，
        # active 槽只服务对应 B 的 start worker；迟到结果不会跨到新意图。
        self._task_rotation_lock = threading.Lock()
        self._task_rotation_pending = None
        self._task_rotation_context = None
        self._task_rotation_notice_timeout_seconds = 120
        self._startup_cancel = threading.Event()
        # 「正在退出」的取消信号，与 _startup_cancel **刻意分开**。
        # 二者语义不同，过去被混为一谈：
        #   * _startup_cancel：让待启动/在途的启动意图作废。stop_streaming
        #     也会置位它（用户点了停止就不该再有启动工作继续）；
        #   * _shutdown_cancel：只在服务退出时置位，用来给"在途网络重试"
        #     设一个共同的上限，别把退避叠进退出预算。
        # 混用的后果（2026-09-25/26 实测）：停止路径用 _startup_cancel 包
        # 自己的下播调用，而 stop_streaming 恰好先把它置位了 → 这次下播被
        # 自己立刻取消、平台从未被告知 → 房间一直挂"在播" → 下一次开播被
        # "直播间仍在播"挡回、下播又同样被取消 → 重启前死锁。
        self._shutdown_cancel = threading.Event()
        self._shutdown_in_progress = False
        self._is_starting = False
        # 最近一次**同步**开播预检的拒绝原因（只在预检路径写入，见 _reject_start）。
        # 拒绝原因本身就是写给人看的中文句子，API 响应的 message 与它同源——
        # 旧实现只把它发进日志和面板事件、响应统一回"开播失败，请查看日志"，
        # 于是"先继续原任务或清空旧存档"这类可操作结论到不了用户眼前。
        self._last_start_reason = ''
        # 上一次停止的清理是否已经跑完（且此后没有新的开播意图被受理）。
        # 用于让"清理已完成、无新会话"的重复停止只确认既有结果，而不是再提交
        # 一次平台下播（重复清理会在新会话刚建立时把它误清理掉）。
        self._stop_cleanup_done = False
        # 平台下播与"本地 owned 进程回收"分开记录：确认下发后不再重复下播，
        # 未下发/失败则保留待办，允许后续显式停止重试（见 _cleanup_confirmed）。
        self._platform_stop_done = False

        # 控制代际：每次接受的停止都会让此前发起的操作失效。
        # 只比较 _is_starting 或共享 Event 的即时值无法区分“停止前挂起的请求”
        # 与“停止完成后用户新发的请求”，因此用单调递增的代际编号归属操作。
        self._control_epoch = 0
        self._start_intent_id = 0
        self._operation_seq = 0
        self._operation_token_limit = 4096
        # 重放票据登记表（TTL 保留 + 容量安全阀）：票据 → (登记代际, 登记时刻)。
        self._operation_tokens = _OperationTokens(self._operation_token_limit,
                                                  boot_id=self._boot_id)
        # 推流进程代际：只有归属同一代的循环才能清除/替换进程引用。
        self._pusher_generation = 0
        self._pusher_lock = threading.Lock()
        self._video_process_generation = -1
        # FFmpeg stderr time= is a bounded, independent local-output signal.
        # The file reader is per process generation; OBS mode never uses it.
        self._pusher_progress_required = False
        self._pusher_media_reader = None
        self._pusher_media_generation = -1
        self._pusher_media_seconds = None
        self._drain_owner_generation = -1
        self._drain_owner = None
        self._local_drainers: list = []

        # 后端事件日志（供前端轮询展示）+ 事件序号（同秒多事件不丢）
        self._backend_events: list = []
        self._event_seq = 0
        self._event_lock = threading.Lock()
        # 人脸验证确认后重试沿用的来源模式（手动验证不能变成任务模式）
        self._verify_retry_mode: Optional[str] = None

        self._apply_operator_stop_marker()
        # 重启后不存在任何在途开播线程：上一场遗留的 pending 交接不可能再被
        # 它的目标接管，明确写成"未执行"（幂等终态）；旧进度保持可恢复。
        # （没有遗留交接时 finalize 是无害 no-op，不打日志。）
        try:
            if self.handover_pending() \
                    and self._finalize_handover('void'):
                logger.info('启动清理：上一场遗留的交接已结论为"未执行"')
        except Exception:
            logger.debug('启动清理遗留交接失败（忽略）', exc_info=True)
        # 启动时断网也不能阻塞服务就绪；保留登录和恢复意图直至网络恢复。
        threading.Thread(target=self._bootstrap, name="LiveBootstrap", daemon=True).start()

    def _register_active_task(self, instruction: LiveInstruction,
                              run_id: Optional[str] = None,
                              validate=None) -> bool:
        """把"正在直播的任务身份"登记到任务管理器（用于拒绝破坏性修改）。

        走原子接口 ``reserve_active_task``：它同时确认任务记录**仍然存在**。
        旧实现无条件登记，于是"选任务之后、真正开播之前任务被删除"会留下
        "记录已删除却仍被登记为在播"的状态，反而挡不住重建。
        返回 False 表示预留失败（任务已不存在），调用方应放弃本次开播。
        """
        tm = getattr(self, 'task_manager', None)
        if not tm or not hasattr(tm, 'set_active_task'):
            return True
        run_id = run_id or self._current_run_id or self.state.run_id or None
        self._active_run_id = run_id
        task_id = getattr(instruction, 'task_id', None) if instruction else None
        try:
            reserve = getattr(tm, 'reserve_active_task', None)
            if callable(reserve) and task_id is not None:
                try:
                    reserved = reserve(task_id, run_id=run_id,
                                       validate=validate)
                except TypeError:
                    # 旧任务管理器没有 validate 形参：退回"只确认记录仍存在"。
                    reserved = reserve(task_id, run_id=run_id)
                if not reserved:
                    self._active_run_id = None
                    return False
                return True
            tm.set_active_task(task_id, run_id=run_id)
            return True
        except Exception as exc:
            logger.debug(f"登记运行中任务失败（不影响直播）：{exc}")
            return True

    def _release_active_task(self, run_id: Optional[str] = None) -> None:
        """解除在播/预留身份；只清理指定 run 的登记（新会话不受影响）。

        旧实现读的是"此刻的 _active_run_id"，于是旧监控退出时会把**新会话**
        刚登记的保护一起清掉。这里要求调用方显式给出它所属的 run。
        """
        target = self._active_run_id if run_id is None else run_id
        tm = getattr(self, 'task_manager', None)
        if tm is not None and hasattr(tm, 'clear_active_task'):
            try:
                tm.clear_active_task(run_id=target)
            except Exception as exc:
                logger.debug(f"清理运行中任务标记失败：{exc}")
        if run_id is None or run_id == self._active_run_id:
            self._active_run_id = None

    def _clear_active_task(self) -> None:
        """兼容入口：清理本控制器当前登记的那一场。"""
        self._release_active_task(None)

    def _owns_current_run(self, epoch: int, run_id: Optional[str]) -> bool:
        """退出段所有权核对：代际未推进，且当前会话仍是本监控所属的那一场。

        只比较 task_id 不够：同一任务可能被"停止 → 重新开播"成为新的一代，
        旧监控不能借 task_id 相同去下播新场、清它的引用或推进它的轮转。

        推迟接管窗口（FFmpeg 模式：平台房已开、本地媒体未就绪、begin_session
        尚未执行）：``state.run_id`` 仍是旧会话，与监控持有的新 run 不同——
        只要推迟接管记录与 (epoch, run_id) 相符，本代仍拥有运行权。
        """
        try:
            if not self._is_epoch_current(epoch):
                return False
        except Exception:
            return False
        current = getattr(self.state, 'run_id', '') or ''
        if run_id and current and current != run_id:
            pending = getattr(self, '_deferred_takeover', None)
            if pending is not None \
                    and str(pending.get('run_id') or '') == str(run_id) \
                    and int(pending.get('epoch') or 0) == int(epoch):
                return True
            return False
        return True

    def _ensure_day_ready(self, epoch: Optional[int] = None,
                          timeout: float = 120.0) -> bool:
        """跨日屏障：所有选任务/开播入口的统一检查点。

        旧实现只在 ``is_resetting()`` 为真时才等一次，等待失败还继续往下走；
        调度线程尚未开始重置时该标志本来就是 False，于是直接拿着上一日的
        today_done 与排序选任务。这里无条件确认"目标日已发布"，失败即暂停。
        等待有界、可取消（停止生效时立即退出）。
        """
        tm = getattr(self, 'task_manager', None)
        if tm is None:
            return True
        if epoch is None:
            epoch = self._control_epoch
        barrier = getattr(tm, 'ensure_day_ready', None)
        if callable(barrier):
            return bool(barrier(
                date.today(), timeout=timeout,
                cancel_check=lambda: not self._is_epoch_current(epoch)))
        # 兼容没有统一屏障的替身/旧实现：退回"正在重置才等"的判定，但这里
        # 与旧行为的关键差别是——**等待失败一律暂停**，不再"超时后继续执行"。
        is_resetting = getattr(tm, 'is_resetting', None)
        wait = getattr(tm, 'wait_for_reset_complete', None)
        if not callable(is_resetting) or not callable(wait):
            return True
        try:
            if not is_resetting():
                return True
        except Exception:
            return True
        logger.info("检测到每日重置进行中，等待完成（有界且可取消）...")
        try:
            return bool(wait(timeout=timeout))
        except TypeError:
            return bool(wait())

    def _current_elapsed(self) -> int:
        """页面可显示到的秒数 = 已确认 + 待确认推算（兼容旧调用）。

        **不是**结算依据：待确认区间只用于有界显示，见 :meth:`_confirmed_elapsed`。
        """
        if not self.is_streaming or self._segment_monotonic is None:
            return int(getattr(self.state, 'elapsed_seconds', 0) or 0)
        clock = self._clock()
        return int(clock.confirmed
                   + EffectiveClock.pending(self._segment_monotonic,
                                            _monotonic()))

    def _confirmed_elapsed(self) -> int:
        """权威**有效**时长（结算/持久化/API 用），不含待确认区间。

        进行中取计时器里已经平台确认过的累计值；不在进行中时取存档里的值
        （存档本身就是"最后成功保存的已确认进度"）。
        """
        if not self.is_streaming:
            return int(getattr(self.state, 'elapsed_seconds', 0) or 0)
        return int(self._clock().confirmed)

    def _credit_effective_observation(self, request_start: float,
                                      returned: float,
                                      expected_epoch: Optional[int] = None,
                                      expected_run_id: Optional[str] = None) -> str:
        """把一次**可信在播观察**交给计时器——有效时长的唯一入账点。

        持有 start lock 时只更新内存计时器并复核归属；文件 fsync 在释放
        controller lock 后执行，避免慢存储阻塞优先级更高的 stop 请求。
        本地 FFmpeg 模式还需要同一进程代际的媒体时间推进；该证据只能
        限制入账量，不能证明观众端收到画面。
        """
        # File I/O stays outside the control lock. The reader is bounded and
        # belongs to one FFmpeg attempt; a later owner check rejects a sample
        # collected while stop/reconnect replaced that attempt.
        with self._pusher_mutex():
            progress_required = getattr(self, '_pusher_progress_required', False)
            media_reader = getattr(self, '_pusher_media_reader', None)
            media_generation = getattr(self, '_pusher_media_generation', -1)
        media_reading = None
        if progress_required and media_reader is not None:
            try:
                media_reading = media_reader.read()
            except Exception as exc:
                logger.warning('读取 FFmpeg 媒体进度失败，暂停有效计时：%s', exc)
        with self._start_mutex():
            if (expected_epoch is not None
                    and not self._owns_current_run(expected_epoch,
                                                   expected_run_id)):
                return 'dropped_owner'
            # Keep claim/release and the in-memory credit in one short critical
            # section. A new FFmpeg attempt cannot replace the reader after
            # ownership validation but before the credit is recorded.
            with self._pusher_mutex():
                local_required = getattr(self, '_local_pusher_required', False)
                if local_required and not self._process_alive(self.video_process):
                    # A live platform room does not prove local output exists.
                    self._pause_effective_timer(TIMER_PAUSED_PUSHER, returned)
                    return 'paused_pusher'
                media_cap = None
                current_required = getattr(self, '_pusher_progress_required', False)
                if local_required and (progress_required or current_required):
                    owned = (progress_required and current_required
                             and self._pusher_media_reader is media_reader
                             and self._pusher_media_generation == media_generation
                             and self._video_process_generation == media_generation)
                    if (not owned or media_reading is None
                            or media_reading.outcome != 'available'
                            or media_reading.media_seconds is None):
                        if (owned and media_reading is not None
                                and media_reading.outcome == 'new_baseline'):
                            self._pusher_media_seconds = None
                        self._pause_effective_timer(TIMER_PAUSED_PUSHER, returned)
                        return 'paused_pusher'
                    previous_media = self._pusher_media_seconds
                    current_media = media_reading.media_seconds
                    self._pusher_media_seconds = current_media
                    if previous_media is None:
                        # First output record only establishes a media baseline.
                        self._segment_monotonic = None
                    elif current_media <= previous_media:
                        self._pause_effective_timer(TIMER_PAUSED_PUSHER, returned)
                        return 'paused_pusher'
                    else:
                        # 媒体**首次确认推进** = FFmpeg 模式可观察的就绪事实。
                        # 推迟的会话接管（交接结论/落盘/成功通知）只在此刻发生；
                        # 接管失败则本次观察不入账，收尾线程关房停流。
                        pending = getattr(self, '_deferred_takeover', None)
                        if pending is not None \
                                and not self._complete_deferred_takeover(pending):
                            self._pause_effective_timer(
                                TIMER_PAUSED_PUSHER, returned)
                            return 'paused_pusher'
                        media_cap = current_media - previous_media
                clock = self._clock()
                clock.credit_limit = effective_credit_limit(
                    getattr(self, '_monitor_interval_seconds', MONITOR_INTERVAL))
                anchor = self._segment_monotonic
                new_anchor, credited, outcome = clock.observe_ok(
                    anchor, request_start, returned, max_credit=media_cap)
                self._segment_monotonic = new_anchor
                confirmed = clock.confirmed
        if credited > 0 and getattr(self, '_stream_mode', '') != 'manual':
            if getattr(self, '_deferred_takeover', None) is not None:
                # 推迟接管窗口（FFmpeg 未就绪）：live_state 仍属于旧会话，
                # 没有可持久化的目标——入账只留在内存时钟；接管完成时由
                # begin_session 冻结基线，之后照常持久化。这不是丢失所有权。
                return outcome
            # 入账即持久化（每个监控周期最多一次，与既有落盘频率一致）。
            # 传浮点值，避免每次取整造成持续少算。
            saved = self.state.update_progress(
                confirmed, expected_run_id=expected_run_id)
            if saved is None:
                return 'dropped_owner'
            if saved is False:
                reason = (self.state.resume_blocked_reason
                          or '直播进度保存失败，恢复和任务结算已阻断')
                if not self._recovery_blocked:
                    self._recovery_blocked = reason
                    self._push_backend_event('恢复', 'danger', reason)
                logger.error('有效进度未确认落盘；保留当前会话并阻断自动结算/轮转')
            elif (not getattr(self.state, '_checkpoint_failure_active', False)
                  and '保存失败' in self._recovery_blocked):
                self._recovery_blocked = ''
                self._push_backend_event(
                    '恢复', 'info', '直播状态已重新可靠保存，恢复保护已解除')
        if outcome in ('dropped_gap', 'dropped_stale'):
            # 两种情况都"丢弃上一段、不补算"，但后果不同，文案必须分开：
            # - dropped_stale：本次响应本身过期 → 连锚点都不立（保持暂停）；
            # - dropped_gap：本次回复新鲜、只是距上次可信观察太久 → 重立锚点，继续计时。
            tail = ('本次响应超过查询预算，不立锚点，等待下一次新鲜观察'
                    if outcome == 'dropped_stale'
                    else '距上次可信观察超限，重立锚点，继续计时')
            logger.info(
                "有效计时丢弃一段未观察区间（%s）：%s；已确认 %.1f 秒保留",
                outcome, tail, confirmed)
            diagnostics.get_diagnostics().record(
                'effective_time_gap_dropped', outcome=outcome,
                anchor=anchor, returned=returned,
                limit=clock.credit_limit, confirmed=confirmed)
        return outcome

    def _pause_for_local_pusher_gap(self, owner_epoch: int,
                                    owner_run_id: Optional[str],
                                    at: Optional[float] = None) -> bool:
        """本场 FFmpeg 退出/重建时丢弃未确认区间，不影响接管后的新场。

        进程退出可能落在两次平台查询之间；若只在查询时看进程是否存活，
        5–8 秒断流已经重连的场景仍会把整个查询间隔计入有效时长。
        """
        with self._start_mutex():
            if (not self.is_streaming
                    or not getattr(self, '_local_pusher_required', False)
                    or not self._owns_current_run(owner_epoch, owner_run_id)):
                return False
            self._pause_effective_timer(TIMER_PAUSED_PUSHER,
                                        _monotonic() if at is None else at)
            return True

    def _pause_effective_timer(self, reason: str,
                               at: Optional[float] = None) -> float:
        """暂停有效计时：丢弃待确认区间，保留已确认值。

        房间明确关闭、查询失败/超时（未知）、恢复阻塞都走这里。返回被丢弃的
        待确认秒数。**不**因此下播或杀推流进程——未知不等于已停止推流。
        """
        clock = self._clock()
        anchor = self._segment_monotonic
        dropped = clock.pause(reason, anchor, at)
        self._segment_monotonic = None
        if dropped > 0:
            logger.info("有效计时暂停（%s）：丢弃待确认 %.1f 秒，保留已确认 %.1f 秒",
                        reason, dropped, clock.confirmed)
        return dropped

    def _apply_operator_stop_marker(self):
        # 根据实际 state 文件定位，不能用共享代码目录或另一个实例的全局配置。
        marker = self.state.state_file.parent / '.no_auto_resume'
        if marker.exists():
            self.state.auto_resume = False
            self.state.save()
            # 不在启动时消费标记：即使状态落盘失败，第二次重启也不能自动开播。
            # 只有之后成功的显式任务开播才清除它。
            logger.info("检测到人为停止标记，保留进度但不自动开播")
        checkpoint_marker = self.state.state_file.parent / '.resume_checkpoint_failed'
        if checkpoint_marker.exists():
            try:
                marker_text = checkpoint_marker.read_text(encoding='utf-8').strip()
                marker_data = json.loads(marker_text)
                reason = (str(marker_data.get('reason') or '')
                          if isinstance(marker_data, dict) else marker_text)
            except (OSError, TypeError, ValueError):
                reason = ''
            self.state._checkpoint_failure_active = True
            self.state.auto_resume = False
            if not self.state.resume_blocked_reason:
                self.state.resume_blocked_reason = (
                    reason or '停播时恢复进度未能写入状态文件，已阻断自动续播')
            logger.error("检测到恢复状态写入失败标记，已保留现场并阻断自动续播")

    def _bootstrap(self):
        # 分区是运行时数据（不再随代码分发）：先做一次"有上限"的补齐尝试，
        # 失败也不影响登录/恢复，之后还能从面板「刷新分区」人工补齐。
        # 刻意放在 retry_network_until_cancelled 之外——那个上下文会一直重试到
        # 网络恢复，离线时会把登录永久挡在后面。
        try:
            self._ensure_areas_loaded()
        except Exception:
            logger.exception("启动时的分区加载失败（不影响其它功能）")
        try:
            with self.api.retry_network_until_cancelled(self._startup_cancel):
                if self.api.is_logged_in() and not self._startup_cancel.is_set():
                    self.login()
            if (not self._startup_cancel.is_set() and self.state.is_streaming
                    and self.state.current_zone and self.current_room_id and self.state.auto_resume):
                self._auto_resume()
        except Exception:
            logger.exception("启动恢复异常")

    def _ensure_areas_loaded(self):
        """确保分区数据已加载（文件不存在或为空时自动从 API 拉取）"""
        if not self.area_loader.areas:
            logger.info(" 分区数据为空，正在从 B 站 API 在线拉取...")
            success = self.area_loader.fetch_and_save_areas(self.api)
            if success:
                logger.info(f" 在线拉取成功，共 {len(self.area_loader.areas)} 个分区")
            else:
                logger.warning(" 在线拉取分区失败，部分功能不可用")

    def login(self) -> bool:
        """自动登录（优先 cookies，否则返回需要扫码的状态）"""
        with self._auth_commit_guard():
            login_generation = getattr(self, '_auth_generation', 0)
            candidate = self.api._cookie_snapshot()
        required = ('SESSDATA', 'bili_jct', 'DedeUserID')
        if not all(candidate.get(key) for key in required):
            logger.info(" Cookies 无效或已过期，需要扫码登录")
            return False

        # 启动自动登录的用户/房间查询也使用候选快照，不能读到并发扫码的
        # 另一账号；平台等待始终在认证锁外。
        with self.api.cookie_context(candidate):
            success, user_info = self.api.get_user_info()
            if success and user_info.get('code') == 0:
                data = user_info.get('data', {})
                uid = data.get('mid')
                if uid is not None and str(uid).strip() == str(
                        candidate.get('DedeUserID', '')).strip():
                    ok, room_resp = self.api.get_room_id_by_uid(uid)
                    room_id = (room_resp.get('data', {}).get('room_id')
                               if ok else None)
                    if room_id:
                        with self._auth_commit_guard():
                            if login_generation != getattr(
                                    self, '_auth_generation', 0):
                                logger.info(" 自动登录结果已过期，放弃旧房间身份")
                                return False
                            # 开播线程在尚未解析出房间时会调用 login() 完成冷启动
                            # 认证；这一步仍需允许提交同一持久凭据。已有房间的
                            # 开播/推流意图则占用账号，禁止自动登录改写其身份。
                            if (self.is_streaming
                                    or (self._is_starting
                                        and self.current_room_id is not None)):
                                logger.warning(" 当前直播仍在运行，放弃切换自动登录身份")
                                return False
                            self.current_room_id = room_id
                            self._account_display_name = str(data.get('name') or '').strip()
                            self._auth_generation = getattr(
                                self, '_auth_generation', 0) + 1
                        logger.info(
                            f" 自动登录成功 | {data.get('name', '')} "
                            f"(UID:{uid}, Room:{room_id})")
                        return True
                    logger.warning(" 无法获取真实直播间 ID，拒绝使用 UID 冒充房间")

        logger.info(" Cookies 无效或已过期，需要扫码登录")
        return False

    def get_qrcode_data(self) -> Optional[dict]:
        """获取二维码数据（供前端展示）"""
        with self._auth_commit_guard():
            issuance_generation = getattr(self, '_auth_generation', 0)
        success, resp = self.api.get_qrcode()
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            qrcode_key = data.get('qrcode_key')
            with self._auth_commit_guard():
                # 登出/新登录若在平台签发期间发生，丢弃这张旧 QR；
                # 调用方必须重新 GET 真实二维码，不能靠猜 key 进入 poll。
                if issuance_generation != getattr(self, '_auth_generation', 0):
                    return None
                if not self._register_qr_intent(
                        qrcode_key, issuance_generation):
                    return None
            return {
                'qrcode_url': data.get('url'),
                'qrcode_key': qrcode_key
            }
        return None

    def _auth_session_active(self) -> bool:
        """认证账号是否仍被当前直播/开播意图占用。"""
        if getattr(self, 'is_streaming', False) or getattr(self, '_is_starting', False):
            return True
        state = getattr(self, 'state', None)
        phase = getattr(state, 'phase', None)
        if phase is None:
            return bool(getattr(state, 'is_streaming', False))
        # 任务模式停止后仍可能保留 is_streaming=True + phase=idle 以便续播；
        # 这不是正在占用平台账号的活跃会话，允许用户先停止再退出/切换。
        return phase in {PHASE_STARTING, PHASE_LIVE, PHASE_RECOVERING,
                         PHASE_STOPPING, PHASE_BLOCKED}

    def logout(self, operation_token: str = '') -> dict:
        """撤销 B 站会话，并对同一登出意图提供幂等重放保护。

        认证代际在清理前推进，使所有已经开始的平台轮询失效。清理只包含
        本地内存与凭据文件，不持认证锁等待平台网络；落盘失败明确返回失败，
        让调用方不能把未撤销的账号当成已登出。
        """
        ledger = self._auth_ledger()
        kind, previous = ledger.claim(operation_token)
        if kind == 'replay':
            return previous
        if kind == 'pending':
            return {
                'success': False,
                'status_code': 409,
                'code': 'auth_logout_in_progress',
                'message': '登出请求正在处理中，请稍后重试',
            }
        if kind == 'expired':
            return {
                'success': False,
                'status_code': 409,
                'code': 'auth_logout_token_expired',
                'message': '登出票据已过期，请重新发起登出',
            }
        if kind == 'full':
            return {
                'success': False,
                'status_code': 503,
                'code': 'auth_logout_ledger_full',
                'message': '登出请求保护空间已满，请稍后重试',
            }

        result = None
        try:
            with self._auth_commit_guard():
                if self._auth_session_active():
                    # 账号仍被直播/开播意图占用：不清共享凭据、不改 room，
                    # 并释放 first token 让用户停止后可重试同一退出意图。
                    if kind == 'first':
                        ledger.release(operation_token)
                    return {
                        'success': False,
                        'status_code': 409,
                        'code': 'auth_session_active',
                        'message': '直播进行中，请先停止直播后再登出',
                    }
                # 先把这次登出意图写入凭据文件。进程若在清空凭据后重启，
                # 旧 token 仍会被识别为已处理，不能在新登录后再次清号。
                if kind == 'first' and not self._persist_auth_ledger():
                    ledger.release(operation_token)
                    return {
                        'success': False,
                        'status_code': 503,
                        'code': 'auth_logout_persistence_failed',
                        'message': '登出意图未落盘，未确认登出成功',
                    }
                # 先推进认证代际，再做本地撤销；旧 poll 即使已从平台返回，
                # 也必须在最终提交点被拒绝。
                self._auth_generation = getattr(self, '_auth_generation', 0) + 1
                cleared = self.api.clear_cookies()
                if cleared is False:
                    result = {
                        'success': False,
                        'status_code': 503,
                        'code': 'auth_logout_persistence_failed',
                        'message': '登出凭据撤销未落盘，未确认登出成功',
                    }
                else:
                    # 未在播时清掉房间身份，避免旧账号在状态层残留；正在
                    # 推流时保留 room_id 供既有停止/回收路径继续工作。
                    self._account_display_name = ''
                    if (not getattr(self, 'is_streaming', False)
                            and not getattr(self, '_is_starting', False)):
                        self.current_room_id = None
                    result = {'success': True, 'message': '已登出'}
        except Exception as exc:
            logger.warning(f" 登出撤销失败：{exc}")
            result = {
                'success': False,
                'status_code': 503,
                'code': 'auth_logout_persistence_failed',
                'message': '登出凭据撤销失败，未确认登出成功',
            }
        if kind == 'first':
            # 只有清理动作已经结束后才把 pending 标记改成最终结果；若这次
            # 写回失败，磁盘仍保留 pending，重启后的同票据只能拒绝/确认，
            # 绝不会再次清除后来登录的账号。
            ledger.complete(operation_token, result)
            if not self._persist_auth_ledger():
                result = {
                    'success': False,
                    'status_code': 503,
                    'code': 'auth_logout_persistence_failed',
                    'message': '登出结果未完成持久化确认',
                }
        return result

    def poll_login_status(self, qrcode_key: str) -> dict:
        """轮询扫码登录状态"""
        with self._auth_commit_guard():
            poll_generation = getattr(self, '_auth_generation', 0)
            if self._claim_qr_intent(qrcode_key, poll_generation) is None:
                return {
                    'logged_in': False,
                    'message': '二维码已失效，请重新获取',
                }

        # 平台等待必须在锁外进行；候选凭据只保存在当前线程，不能先写入
        # 共享 self.api.cookies 再做用户/房间查询。
        success, resp = self.api.poll_qrcode(qrcode_key)
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            status = data.get('code', -1)  # 0=扫码中，86101=未扫码，86038=二维码过期
            if 'url' in data and data['url']:
                # 提取 cookies
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(data['url'])
                query_params = parse_qs(parsed.query)
                cookies = {}
                for key in ['SESSDATA', 'bili_jct', 'DedeUserID']:
                    if key in query_params:
                        cookies[key] = query_params[key][0]
                required = ('SESSDATA', 'bili_jct', 'DedeUserID')
                if all(cookies.get(key) for key in required):
                    # 所有平台读取都使用本轮扫码产生的候选账号。
                    with self.api.cookie_context(cookies):
                        suc, user_info = self.api.get_user_info()
                        if suc and user_info.get('code') == 0:
                            info = user_info.get('data', {})
                            uid = info.get('mid')
                            expected_uid = str(cookies.get('DedeUserID', '')).strip()
                            if uid is not None and str(uid).strip() == expected_uid:
                                ok, room_resp = self.api.get_room_id_by_uid(uid)
                                if not ok:
                                    logger.warning(" 无法获取真实直播间 ID，拒绝完成扫码登录")
                                    return {
                                        'logged_in': False,
                                        'message': '无法获取直播间 ID，登录未完成',
                                    }
                                room_id = room_resp.get('data', {}).get('room_id')
                                if room_id:
                                    with self._auth_commit_guard():
                                        if poll_generation != getattr(
                                                self, '_auth_generation', 0):
                                            return {
                                                'logged_in': False,
                                                'message': '登录请求已失效，请重新扫码',
                                            }
                                        if self._auth_session_active():
                                            return {
                                                'logged_in': False,
                                                'code': 'auth_session_active',
                                                'message': '直播进行中，请先停止直播后再切换账号',
                                            }
                                        if not self.api.replace_cookies(cookies):
                                            return {
                                                'logged_in': False,
                                                'message': '登录凭据保存失败',
                                            }
                                        # 凭据持久化与房间身份在同一认证提交点
                                        # 完成，避免旧 poll 只落盘不改 room，或反之。
                                        self.current_room_id = room_id
                                        self._account_display_name = str(info.get('name') or '').strip()
                                        self._auth_generation = getattr(
                                            self, '_auth_generation', 0) + 1
                                    return {
                                        'logged_in': True,
                                        'user_info': {
                                            'uid': uid,
                                            'uname': info.get('name', ''),
                                            'face': info.get('face', ''),
                                            'level': info.get('level', 0)
                                        }
                                    }
                                logger.warning(" 返回的直播间 ID 为空，拒绝完成扫码登录")
                                return {
                                    'logged_in': False,
                                    'message': '无法获取直播间 ID，登录未完成',
                                }
                return {'logged_in': False, 'message': '提取 cookies 失败'}
            elif status == 0:
                return {'logged_in': False, 'scanning': True}
            elif status == 86038:
                return {'logged_in': False, 'expired': True}
            else:
                return {'logged_in': False, 'scanning': False}
        else:
            msg = resp.get('msg', '查询失败')
            if '二维码尚未生成' in msg:
                return {'logged_in': False, 'scanning': False}
            return {'logged_in': False, 'message': msg}
    def get_session_snapshot(self) -> dict:
        """统一的会话快照：每个字段只有一个含义，供状态接口与前端消费。

        - ``phase`` 区分"平台已受理/FFmpeg 启动中/直播中/恢复中/停止中/被阻塞"，
          不再让 ``is_streaming`` 兼任"画面健康"；
        - ``duration_known=False`` 表示目标时长未知，**不等于 0（不限时）**；
        - ``elapsed_seconds`` 由单调时钟推进，不受系统时间回拨影响。
        """
        snapshot_fn = getattr(self.state, 'snapshot', None)
        if callable(snapshot_fn):
            snap = snapshot_fn()
        else:
            # 测试替身/极简 state：退化成旧字段，不因缺能力而 500。
            snap = {
                'run_id': getattr(self.state, 'run_id', ''),
                'service_instance': getattr(self.state, 'service_instance', ''),
                'session_version': getattr(self.state, 'session_version', 0),
                'source_mode': getattr(self.state, 'source_mode', SOURCE_NEW),
                'phase': getattr(self.state, 'phase', PHASE_IDLE),
                'task_id': getattr(self.state, 'task_id', None),
                'execution_date': getattr(self.state, 'execution_date', None),
                'current_zone': getattr(self.state, 'current_zone', '') or '',
                'elapsed_seconds': getattr(self.state, 'elapsed_seconds', 0),
                'duration_seconds': getattr(self.state, 'duration_seconds', 0),
                'duration_known': bool(getattr(self.state, 'duration_known',
                                               False)),
                'room_id': getattr(self.state, 'room_id', 0),
                'resumable': False,
                'resume_blocked_reason': '',
                'auto_resume': getattr(self.state, 'auto_resume', True),
                'state_corrupt': bool(getattr(self.state, 'corrupt', False)),
                'last_save_error': '',
            }
        elapsed = self._confirmed_elapsed()
        duration = 0
        duration_known = False
        if self.current_instruction:
            duration = int(self.current_instruction.duration_seconds or 0)
            duration_known = True
        elif bool(getattr(self.state, 'duration_known', False)):
            duration = int(getattr(self.state, 'duration_seconds', 0) or 0)
            duration_known = True
        if self._is_starting:
            phase = PHASE_STOPPING if self._start_cancel.is_set() else PHASE_STARTING
        elif self.is_streaming:
            phase = PHASE_RECOVERING if (self._recovery_blocked
                                         or self.reconnect_attempts) else PHASE_LIVE
        elif self._recovery_blocked:
            phase = PHASE_BLOCKED
        else:
            phase = self.state.phase if self.state.phase != PHASE_LIVE else PHASE_IDLE
        resumable, resume_blocked_reason = self.get_resume_status()
        if self._is_starting or self.is_streaming:
            resumable = False
        # 在途阶段**不写** resume_blocked_reason：它是"这份存档为什么不能继续"
        # 的**已定结论**，而"正在开播/正在停止"是过程，由 is_starting 表达。
        # 旧实现在这里塞一句"新的开播请求正在准备中"，任何把该字段当结论渲染的
        # 消费者都会把"正在开播"读成"存档废了"（2026-09-26 现场）。
        # 运行中（含"已受理、尚未真正开播"）：当前会话以**内存指令**为准。
        # 持久化的 state 是"可恢复的存档"——手动模式根本不写它，若直接取存档
        # 字段，状态接口就会把上一个任务的 zone/run/进度报成当前会话（R03）。
        if (self.is_streaming or self._is_starting) and self.current_instruction:
            snap.update({
                'current_zone': self.current_instruction.zone_name,
                'task_id': self.current_instruction.task_id,
                'execution_date': (self.current_instruction.execution_date
                                   or snap.get('execution_date')),
                'run_id': (self._current_run_id or self._pending_run_id
                           or snap.get('run_id', '')),
                'source_mode': (self._current_source or self._pending_source
                                or snap.get('source_mode', SOURCE_NEW)),
            })
        # 状态接口绝不因判定失败而 500：判不出来就明确说"暂不提供入口"，
        # 面板据此禁用按钮，而不是让用户按下去才发现服务端拒绝。
        try:
            can_start_new_task, new_task_blocked_reason = self.new_task_verdict(
                apply=False)
        except Exception as exc:
            logger.error('计算"能否开始新任务"失败：%s', exc)
            can_start_new_task = False
            new_task_blocked_reason = '无法确认旧存档状态，暂不提供新任务入口'
        snap.update({
            'phase': phase,
            # elapsed_seconds 是**权威有效时长**（平台确认过的累计值），不含待确认
            # 区间；pending_* 只服务于页面有界外推。同一个字段不会有时代表 confirmed、
            # 有时代表墙钟总时长。
            'elapsed_seconds': elapsed,
            'duration_seconds': duration,
            'duration_known': duration_known,
            'remaining_seconds': max(0, duration - elapsed) if duration_known and duration > 0 else None,
            'resumable': resumable and not self.is_streaming,
            'resume_blocked_reason': resume_blocked_reason,
            # "能不能开始新任务"是**服务端的结论**，面板只负责照它禁用按钮并
            # 显示原因。旧实现让面板自己用 resumable 反推，推出来的可用性
            # 与开播预检可以不一致，用户按到的就是一颗必然失败的按钮。
            'can_start_new_task': can_start_new_task,
            'new_task_blocked_reason': new_task_blocked_reason,
            'recovery_blocked': self._recovery_blocked,
            'is_anomaly': bool(self._recovery_blocked),
        })
        snap.update(self._effective_timer_view())
        return snap

    def _effective_timer_view(self) -> dict:
        """有效计时给页面的有界外推视图（不暴露区间/补偿明细）。"""
        clock = self._clock()
        anchor = self._segment_monotonic if self.is_streaming else None
        now = _monotonic()
        view = clock.snapshot(anchor, now)
        # pending_seconds 是**可推算上限**：超过 credit_limit 的部分一律不给页面，
        # 否则页面会显示一段服务端自己都不认的时长。窗口用尽时它会等于上限、
        # 且 pending_valid_seconds 为 0，页面自然冻结在这个值上。
        pending = min(view['pending'], view['pending_limit'])
        return {
            # 待确认区间：页面可以在这个范围内推算显示，但不是结算依据
            'pending_seconds': int(pending),
            # 还能继续外推多久（秒）；为 0 表示页面必须冻结在当前值上
            'pending_valid_seconds': round(view['pending_remaining'], 3),
            # 单段待确认区间的上限（页面外推的硬边界）
            'pending_limit_seconds': round(view['pending_limit'], 3),
            'pending_extrapolatable': bool(view['extrapolatable']),
            # 'running' | 'paused_closed' | 'paused_unknown' | 'paused_blocked'
            'timer_state': view['state'],
        }

    def get_live_status_api(self) -> dict:
        """获取当前直播状态（供 API 响应）"""
        payload = self.state.get_status()
        payload['is_streaming'] = self.is_streaming
        payload['is_starting'] = self._is_starting
        payload['is_cancelling'] = self._is_starting and self._start_cancel.is_set()
        payload['recovery_blocked'] = self._recovery_blocked
        if self._recovery_blocked:
            payload['is_anomaly'] = True
        # 状态查询失败只表示未知，不触发任何自动处置；计数用于面板观测。
        payload['status_query_failures'] = self._status_query_failures
        payload['status_query_failures_total'] = getattr(
            self, '_status_query_failures_total', 0)
        payload['ffmpeg_unrecycled'] = self._ffmpeg_unrecycled
        payload['stream_mode'] = self._stream_mode or ''
        # 人脸验证待处理（自动切任务触发时前端轮询感知）
        payload['pending_face_verify'] = self._pending_face_verify
        payload['face_verify_url'] = self._face_verify_url or ''
        payload['face_verify_run_id'] = (
            str(self._face_verify_run_id or '') if self._pending_face_verify else '')
        payload['face_verify_epoch'] = (
            self._face_verify_epoch if self._pending_face_verify else None)
        # 面板据此决定是否给出"我已验证"，以及不可确认时给出原因。
        # 服务端算一次、面板照抄，避免两边各判一份而分叉出空转按钮。
        payload['face_verify_confirmable'] = bool(self.face_verify_confirm_owner())
        payload['face_verify_block_reason'] = (
            self.face_verify_block_reason() if self._pending_face_verify else '')
        # FFmpeg 推流状态
        ffmpeg_active = (self.video_process is not None and self.video_process.poll() is None)
        payload['ffmpeg_active'] = ffmpeg_active
        payload['ffmpeg_current_video'] = self.ffmpeg_current_video or ''
        # 后端事件日志（最近50条）
        payload['backend_events'] = self._backend_events[-50:] if hasattr(self, '_backend_events') else []
        # 服务进程身份：前端据此识别"服务重启后迟到的旧响应"，
        # 与请求序号一起构成"拒绝状态回退"的判据。
        payload['boot_id'] = self._boot_id

        # 会话快照（单一来源：前端只消费这一份，不再各处拼字段）
        snap = self.get_session_snapshot()
        payload.update(snap)
        # 兼容旧客户端：保留原有的几个顶层字段名
        payload['duration_seconds'] = snap['duration_seconds']
        payload['elapsed_seconds'] = snap['elapsed_seconds']
        payload['remaining_seconds'] = snap['remaining_seconds'] or 0
        payload['current_zone'] = snap['current_zone']
        return payload

    def _pre_start_cleanup(self, epoch: int = None):
        """开播前清理；只有旧进程与旧房间均已确认清理才可启动。"""
        if epoch is None:
            epoch = self._control_epoch
        room_at_entry = self.current_room_id

        def still_owner() -> bool:
            return (self._is_epoch_current(epoch)
                    and self.current_room_id == room_at_entry)

        if not still_owner():
            return False
        # 1. 清理所有残留 FFmpeg 进程（异常退出时可能未清理）
        if self.video_process or self._ffmpeg_loop_thread:
            logger.info(" 检测到残留 FFmpeg 状态，清理中...")
        self._ffmpeg_stop_event.set()
        self._kill_ffmpeg()
        if not still_owner():
            return False
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            # 旧推流线程仍未退出：再等一轮，避免两代进程同时写同一房间。
            self._ffmpeg_loop_thread.join(timeout=5.0)
        self._kill_ffmpeg()
        if not still_owner():
            return False
        if self._ffmpeg_unrecycled:
            # 旧进程未确认回收：保留引用与告警，禁止本次启动新的本机推流。
            logger.warning(" 上一代 FFmpeg 未确认回收，本次不启动新的本机推流")
            self._push_backend_event(
                '推流', 'warning', '上一代推流进程未确认退出，暂不启动新的本机推流')
            return False
        else:
            self.video_process = None
        self.ffmpeg_current_video = ''

        # 2. 检查 B站直播间是否已在播（异常退出时可能未下播）
        if room_at_entry:
            try:
                ok, resp = self.api.get_live_status(room_at_entry)
                if not still_owner():
                    return False
                data = resp.get('data') if isinstance(resp, dict) else None
                live_status = data.get('live_status') if isinstance(data, dict) else None
                if not ok or live_status not in (0, 1):
                    raise RuntimeError('直播间状态未得到明确确认')
                if live_status == 1:
                    logger.info(" 检测到直播间仍在播，先调用下播 API...")
                    # 与停止路径共用同一下播实现：凭据解析、结果归一化、
                    # 失败留痕只有一份。所有权复核放在 guard 里，语义不变。
                    stopped, stop_reason = self._platform_stop_live(
                        room_at_entry, guard=still_owner)
                    if stop_reason == PLATFORM_STOP_STALE:
                        return False
                    if not still_owner():
                        return False
                    if not stopped:
                        raise RuntimeError(
                            f'旧直播间下播未确认（{stop_reason}）')
                    logger.info(" 旧直播间下播已确认")
            except Exception as e:
                logger.warning("开播前清理未确认，拒绝启动新场：%s", e)
                self._push_backend_event(
                    '开播', 'warning', '旧直播间状态或下播结果未确认，暂不启动新任务')
                return False

        # 3. 重置内部状态
        if not still_owner():
            return False
        self.is_streaming = False
        self.stream_start_time = None
        self.stop_monitor.clear()
        logger.info(" 开播前清理完成")
        return True

    # ---------------- 控制代际（停止让此前的操作失效） ----------------

    def issue_operation(self) -> str:
        """为一次新发起的控制操作签发票据：`<boot>:<代际>:<序号>`。

        客户端在重放请求时回传该票据，服务端只比较前缀（boot + 代际）就能
        区分“停止前挂起的旧操作”与“停止完成后用户新发的请求”——判定
        **不依赖登记表**，因此不受任何缓存淘汰影响，也不受服务重启影响
        （boot 变了就是别的进程的票据）。
        """
        with self._start_mutex():
            self._operation_seq += 1
            return f'{self._boot_id}:{self._control_epoch}:{self._operation_seq}'

    def begin_control_operation(self, token: str = '') -> Optional[int]:
        """在控制入口**一次**完成票据校验与控制代际快照。

        返回本次操作归属的代际；票据过期（停止前发出、停止后重放）时返回
        ``None``。所有入口（/start、/run-next、/resume、/stop）都必须经由
        这里取代际，并在最后提交点把同一个代际交给控制器核对，否则“选任务 /
        读取状态 / 准备 / 后台化”之间的停止就会被漏掉。

        快照与停止推进代际共用同一把锁，因此不存在“先读代际、随后停止”的窗口。
        """
        with self._start_mutex():
            epoch = self._control_epoch
            ok, reason = self._tokens().accept(token, epoch)
        if not ok:
            logger.info("拒绝一次过期/重放的控制操作：%s", reason)
            diagnostics.get_diagnostics().record(
                'operation_replay_rejected', reason=reason, current=epoch)
            return None
        return epoch

    def claim_stop_operation(self, token: str = '') -> bool:
        """停止入口专用：登记票据并判断这个停止是否还需要**执行**。

        返回 ``False`` 表示该停止意图已经在它自己的代际里处理完成——典型场景
        是停止的响应丢失、客户端重试，而期间用户又明确开启了新直播。此时
        重放只应确认既有结果（接口仍返回成功），绝不能再次执行下播，否则
        旧停止会停掉新直播。
        """
        if not isinstance(token, str) or not token:
            return True
        with self._start_mutex():
            execute, reason = self._tokens().register_stop(token, self._control_epoch)
        if not execute:
            logger.info("旧停止请求重放（%s）：只确认既有结果，不再执行下播", reason)
            diagnostics.get_diagnostics().record(
                'stop_replay_confirmed', reason=reason,
                current=self._control_epoch)
        return execute

    def note_control_token(self, token: str = '') -> None:
        """兼容入口：只登记票据、不做过期拒绝。

        正式停止协议见 :meth:`claim_stop_operation`；保留此方法用于没有
        该能力的替身与旧调用方，语义是“登记但放行”。
        """
        if not isinstance(token, str) or not token:
            return
        with self._start_mutex():
            self._tokens().accept(token, self._control_epoch, allow_stale=True)

    def is_operation_stale(self, operation_id: str) -> bool:
        """票据是否属于已经过去的代际或别的服务进程。

        自定义票据（UUID 等）无法从形状推断历史，返回 ``False`` 并交由
        登记表在入口判定——不能假称能从一个陌生 UUID 推断其意图。
        """
        if not operation_id:
            return False
        boot, epoch, _seq = _parse_issued_ticket(str(operation_id))
        if boot is None:
            return False
        if boot != self._boot_id:
            return True
        return epoch != self._control_epoch

    def _is_epoch_current(self, epoch: int) -> bool:
        return epoch == self._control_epoch

    def accept_operation_token(self, token: str) -> bool:
        """兼容入口：仅校验票据是否仍然有效，不返回代际。"""
        with self._start_mutex():
            ok, _ = self._tokens().accept(token, self._control_epoch)
        return ok

    @staticmethod
    def _new_run_id() -> str:
        """新的会话身份（与 LiveState.begin_session 生成的格式一致）。"""
        return f"{int(time.time()):x}-{uuid.uuid4().hex[:8]}"

    def _clear_settled_saved_state_for_new_task(self, apply: bool = True
                                                ) -> Tuple[bool, str]:
        """新任务开始前安全处理仍保存的任务进度。

        同日/跨日和时长是否达到目标都不能证明旧任务已经结算。只有 task_id、
        分区和执行日均匹配，且 DB 的 last_done_date 明确等于该执行日时才可按
        已结算清理。另按既有跨日规则，旧日已知时长且 confirmed 未达目标的部分
        进度可在身份/DB 标记一致时清理并从新执行日重排。

        ``apply=False`` 只取判定结论、不做任何破坏性写入也不发事件，供状态
        载荷回答"现在能不能开始新任务"；判定与清理**共用同一段代码**，面板
        按钮的可用性与真正开播时的结论因此不可能分叉。
        """
        state = self.state
        blocker = str(getattr(state, 'resume_blocked_reason', '') or '')
        zone = str(getattr(state, 'current_zone', '') or '')
        marker = state.state_file.parent / '.resume_checkpoint_failed'
        if marker.exists():
            try:
                marker_text = marker.read_text(encoding='utf-8').strip()
            except OSError:
                marker_text = ''
            try:
                marker_data = json.loads(marker_text)
            except (TypeError, ValueError):
                marker_data = None
            if isinstance(marker_data, dict):
                marker_reason = str(marker_data.get('reason') or '')
            elif marker_text and not marker_text.startswith(('{', '[')):
                # Older markers stored a plain reason. Never surface a malformed
                # serialized snapshot as user-facing diagnostic text.
                marker_reason = marker_text[:240]
            else:
                marker_reason = ''
            return False, (marker_reason or blocker or
                           '恢复状态保存失败标记仍在，不能自动清理或启动新任务')
        if not zone:
            return True, ''
        task_id = getattr(state, 'task_id', None)
        execution_day = str(getattr(state, 'execution_date', '') or '')
        manager = getattr(self, 'task_manager', None)
        if task_id is None or not zone or not execution_day or manager is None:
            return False, (
                ((blocker + '；') if blocker else '') +
                '保存会话缺少可核对的稳定任务身份或任务库，不能覆盖旧进度')
        try:
            row = manager.db.get_task_by_id(int(task_id))
        except Exception as exc:
            logger.error('跨日旧档核对任务库失败：%s', exc)
            return False, (
                ((blocker + '；') if blocker else '') +
                '任务库读取失败，旧进度已保留')
        if not row or row.get('zone_name') != zone:
            return False, (
                ((blocker + '；') if blocker else '') +
                '任务身份已删除或变化，旧进度已保留')
        last_done = str(row.get('last_done_date') or '')
        today_done = self._parse_today_done(row.get('today_done'))
        if today_done is None:
            return False, (
                ((blocker + '；') if blocker else '') +
                '任务库今日完成标记不是可识别的 0/1，旧进度已保留')
        try:
            saved_date = date.fromisoformat(execution_day)
        except ValueError:
            return False, (
                ((blocker + '；') if blocker else '') +
                '保存的执行日格式无效，旧进度已保留')
        today = date.today()
        if saved_date > today:
            return False, (
                ((blocker + '；') if blocker else '') +
                '保存的执行日在未来，旧进度已保留')
        if saved_date == today and (not today_done or last_done != execution_day):
            return False, (
                ((blocker + '；') if blocker else '') +
                '今日完成标记与存档执行日不一致，旧进度已保留')
        if saved_date != today and today_done:
            return False, (
                ((blocker + '；') if blocker else '') +
                '今日完成标记与旧存档执行日冲突，旧进度已保留')
        settled = last_done == execution_day
        old_partial = False
        if saved_date != today and not settled:
            duration_known = bool(getattr(state, 'duration_known', False))
            duration = _safe_int(getattr(state, 'duration_seconds', 0), 0)
            elapsed = _safe_int(getattr(state, 'elapsed_seconds', 0), 0)
            last_done_before_execution = False
            if last_done:
                try:
                    last_done_before_execution = date.fromisoformat(last_done) < saved_date
                except ValueError:
                    last_done_before_execution = False
            old_partial = (not today_done and duration_known and duration > 0
                           and elapsed < duration and int(row.get('category') or 0) != 0
                           and (not last_done or last_done_before_execution))
        if not settled and not old_partial:
            return False, (
                ((blocker + '；') if blocker else '') +
                'DB 未证明旧执行日已结算，或旧进度已达到/超过目标，旧进度已保留；'
                '请核查后再启动新任务')
        if not apply:
            # 只取结论（状态载荷问"现在能不能开新任务"）：不写盘、不发事件。
            return True, ''
        if not state.stop_streaming(preserve=False):
            detail = ('旧日未完成部分进度的归档清理保存失败，旧进度已保留'
                      if old_partial else
                      'DB 已确认结算，但清理旧存档写入失败')
            state.resume_blocked_reason = (
                ((blocker + '；') if blocker else '') +
                detail)
            return False, state.resume_blocked_reason
        logger.info('按 task_id/zone/execution_date 与 DB 结算事实清除跨日旧存档：%s/%s/%s',
                    zone, task_id, execution_day)
        if old_partial:
            self._push_backend_event(
                '跨日', 'warning',
                f'旧任务「{zone}」执行日未完成且未达时长，已清除旧日部分进度；'
                '今日任务将从零开始，不计入旧日完成')
        return True, ''

    def reset_start_verdict(self) -> None:
        """开播操作入口处清空上一次的拒绝原因。

        响应文案与它同源（见 `_reject_start`）；不清就会把上一次的结论算到
        这一次请求头上——例如上一次被旧存档挡住、这一次败在更早的准备阶段，
        用户看到的却是"先继续原任务"。
        """
        self._last_start_reason = ''

    def _reject_start(self, message: str) -> bool:
        """登记一次开播拒绝并返回 False，供 `return self._reject_start(...)`。

        日志、面板事件与响应文案**共用同一句 message**：它本来就是写给人看的
        中文结论，任何一处另写一句都会漂移。
        """
        self._last_start_reason = message
        logger.error(message)
        self._push_backend_event('恢复', 'danger', message)
        return False

    def new_task_verdict(self, apply: bool = True) -> Tuple[bool, str]:
        """现在能不能开始一个**新任务**（旧存档是否已可安全清理）。

        这是"开始下一任务"的唯一判定：`start_streaming` 的开播前预检与状态
        载荷的 ``can_start_new_task`` 都只调用它。分成两处实现必然漂移——
        2026-09-26 现场：面板按钮可用、服务端按同一条规则拒绝，用户只看到
        "开播失败，请查看日志"，而真正的原因（旧存档要嘛继续、要嘛清空）
        只进了日志。

        ``apply`` 只决定"判定通过时是否真的执行清理"：开播路径要清（这是它
        原本就有的副作用），状态载荷只问结论、**绝不能**顺手把存档清掉。
        """
        if self._is_starting or self.is_streaming:
            return False, '当前会话仍在进行中，请先停止直播再开始新任务'
        state_file = getattr(self.state, 'state_file', None)
        if state_file is None:
            return False, '任务状态存储不可用，拒绝启动新任务以免覆盖恢复进度'
        pending_marker = Path(state_file).parent / '.resume_checkpoint_failed'
        if not (self.state.current_zone or pending_marker.exists()):
            return True, ''
        cleared, reason = self._clear_settled_saved_state_for_new_task(apply=apply)
        if cleared:
            return True, ''
        return False, reason + '；先继续原任务或显式清理旧存档，再启动新任务'

    # ---------------- 2026-09-28 工作包 A：受理意图的显示基线 ----------------

    def build_accepted_intent(self, epoch: int = 0,
                              stream_mode: str = '') -> dict:
        """受理成功那一刻冻结的"本次意图"，**只服务于显示**。

        界面拿到它可以立刻显示乐观推进的秒数，不必等第一次平台观察回到面板。
        它和权威计时是两件事：

        - 全部取受理时刻已确定的值（会话 run_id、任务身份、固定目标、继承的已确认
          秒数），不含任何推测；
        - 不写进 ``LiveState``、任务库、结算、邮件已计时，也不参与跨日判定。

        因此这里不做"等区域平台回报再填"之类的延后，也不从别的会话借推算值：
        那会让"乐观显示"反过来污染权威数据。
        """
        ins = self.current_instruction
        mode = stream_mode
        if not mode:
            mode = ('task' if ins is not None
                    and getattr(ins, 'task_id', None) is not None else 'manual')
        return {
            'run_id': str(getattr(self, '_pending_run_id', '') or ''),
            'task_id': getattr(ins, 'task_id', None) if ins is not None else None,
            'zone_name': ins.zone_name if ins is not None else '',
            'duration_seconds': int(ins.duration_seconds or 0)
                                if ins is not None else 0,
            'duration_known': ins is not None,
            # 恢复场景这里是**继承的已确认秒数**；新开播为 0。
            'confirmed_elapsed_seconds': max(
                0, int(getattr(self, '_pending_inherit', 0) or 0)),
            'stream_mode': mode,
            'source': str(getattr(self, '_pending_source', SOURCE_NEW)
                          or SOURCE_NEW),
            'epoch': int(epoch or 0),
            'accepted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    # ---------------- 2026-09-28 工作包 B：旧进度的受控交接 ----------------
    #
    # 语义（用户已确认的产品决定）：
    #   * 目标未确认开播之前，旧进度仍可恢复；
    #   * 目标确认开播之后，旧的"本次进度"才放弃；历史完成天数永不清零。
    #
    # 因此这里**不在受理时清理任何东西**（既不是 clear-state，也不是"先清后开"）：
    # 存档一直原地保留，只有当新会话真正把存档换成自己时（``begin_session``）
    # 旧进度才自然被替换。交接记录只负责把这个决定写成可复核的事实，并让同一张
    # 票的重放无法重复触发。

    def _handover_file(self) -> Path:
        return Path(self.state.state_file).parent / '.progress_handover.json'

    def saved_progress_conflict(self) -> Optional[dict]:
        """旧存档仍持有一份未证明已结算的进度时，给出可辨识的冲突资料。

        纯只读（``apply=False``）：这里是给前端生成确认文案用的**事实**，绝不
        顺手清理。没有冲突（没有存档 / 已结算 / 旧日的部分进度可安全归档）
        时返回 ``None``，调用方按正常流程开始即可。
        """
        if self._is_starting or self.is_streaming:
            return None
        state = self.state
        marker = Path(state.state_file).parent / '.resume_checkpoint_failed'
        zone = str(getattr(state, 'current_zone', '') or '')
        if not zone and not marker.exists():
            return None
        cleared, reason = self._clear_settled_saved_state_for_new_task(apply=False)
        if cleared:
            return None
        resumable, resume_reason = self.get_resume_status()
        return {
            'run_id': str(getattr(state, 'run_id', '') or ''),
            'task_id': getattr(state, 'task_id', None),
            'zone_name': zone,
            'execution_date': str(getattr(state, 'execution_date', '') or ''),
            'elapsed_seconds': max(
                0, _safe_int(getattr(state, 'elapsed_seconds', 0), 0)),
            'duration_seconds': _safe_int(
                getattr(state, 'duration_seconds', 0), 0),
            'duration_known': bool(getattr(state, 'duration_known', False)),
            'resumable': bool(resumable),
            'reason': resume_reason or reason,
        }

    def verify_saved_abandon_claim(self, claim) -> Optional[str]:
        """核对"受控放弃旧进度"的确认凭证；``None`` = 仍然成立。

        每一项都是用户确认那一刻冻结的身份。期间发生下列任一情形都必须让这次
        确认作废，由前端重新拿事实再问一次（**不能**自动重放一个新决定）：

        - 旧存档身份变了（另一个标签页停止/恢复/清理、后台结算推进了它）；
        - **已播秒数变了**（合法的 `/state/update`、后台补记都可能把确认时的
          600 秒变成 900 秒——用户确认的是 600，就不能替他放弃 900）；
        - 控制代际变了（确认之后又发生过停止）；
        - 存档已损坏或落盘失败标记仍在（此时任何换任务都不安全）。
        """
        state = self.state
        marker = Path(state.state_file).parent / '.resume_checkpoint_failed'
        if marker.exists():
            return 'abandon_state_untrusted'
        if str(getattr(claim, 'old_run_id', '') or '') \
                != str(getattr(state, 'run_id', '') or ''):
            return 'abandon_stale_run'
        if getattr(claim, 'old_task_id', None) is not None \
                and getattr(state, 'task_id', None) is not None \
                and int(claim.old_task_id) != int(state.task_id):
            return 'abandon_stale_task'
        if str(getattr(claim, 'old_zone_name', '') or '') \
                != str(getattr(state, 'current_zone', '') or ''):
            return 'abandon_stale_zone'
        if str(getattr(claim, 'old_execution_date', '') or '') \
                != str(getattr(state, 'execution_date', '') or ''):
            return 'abandon_stale_execution_day'
        # 已播秒数是确认的主体本身：确认凭证必须带，且必须与存档此刻的值
        # 完全一致。缺了/对不上都不再是"用户确认的那一份"。
        claim_elapsed = getattr(claim, 'old_elapsed_seconds', None)
        if claim_elapsed is None:
            return 'abandon_missing_elapsed'
        state_elapsed = max(0, _safe_int(
            getattr(state, 'elapsed_seconds', 0), 0))
        try:
            if int(claim_elapsed) != state_elapsed:
                return 'abandon_stale_elapsed'
        except (TypeError, ValueError):
            return 'abandon_invalid_elapsed'
        try:
            if int(getattr(claim, 'stop_epoch', 0)) != int(self._control_epoch):
                return 'abandon_stale_epoch'
        except (TypeError, ValueError):
            return 'abandon_invalid_epoch'
        return None

    def handover_pending(self) -> bool:
        """是否存在一份还没有结论的交接（目标尚未确认开播）。"""
        with self._handover_lock:
            try:
                record = json.loads(
                    self._handover_file().read_text(encoding='utf-8'))
            except (OSError, TypeError, ValueError):
                return False
            return isinstance(record, dict) and record.get('outcome') == 'pending'

    def _atomic_write_json(self, path: Path, payload: dict) -> bool:
        """临时文件 + fsync + 读回 + os.replace：不留半成品、不破坏旧值。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix='.handover-', suffix='.tmp',
                                            dir=str(path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as fp:
                    json.dump(payload, fp, ensure_ascii=False, indent=2)
                    fp.flush()
                    os.fsync(fp.fileno())
                written = json.loads(Path(tmp_name).read_text(encoding='utf-8'))
                if written.get('handover_id') != payload.get('handover_id'):
                    raise OSError('交接记录写回校验失败')
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            return True
        except Exception as exc:
            logger.error('写入旧进度交接记录失败：%s', exc)
            return False

    def record_saved_abandon_handover(self, claim, instruction,
                                      epoch: int) -> Tuple[bool, str]:
        """把一次受控交接写成持久记录；写不进去就**拒绝启动**，原状保留。

        同一张票（同一个 confirm_id）的第二次请求会被认出来：线上一次确认只
        对应一次交接，重放不会碰第二份进度。

        校验与落盘是**同一个原子区间**：第一次核对（API 入口）之后、写记录
        之前，状态仍可能被合法的 `/state/update` 或后台推进改写。这里在状态
        I/O 锁内**重核一遍全部冻结身份再落盘**——任何一项漂移（包括已播秒数
        600→900）都让这次确认作废（409），绝不把"确认时的进度"悄悄换成
        "此刻的进度"。
        """
        path = self._handover_file()
        confirm_id = str(getattr(claim, 'confirm_id', '') or '')
        if not confirm_id:
            return False, '缺少一次性确认标识，拒绝受控放弃旧进度'
        payload = {
            'handover_id': f'{int(time.time()):x}-{secrets.token_hex(4)}',
            'confirm_id': confirm_id,
            'outcome': 'pending',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'epoch': int(epoch or 0),
            'old': {
                'run_id': str(getattr(claim, 'old_run_id', '') or ''),
                'task_id': getattr(claim, 'old_task_id', None),
                'zone_name': str(getattr(claim, 'old_zone_name', '') or ''),
                'execution_date': str(getattr(claim, 'old_execution_date', '')
                                      or ''),
                'elapsed_seconds': max(
                    0, int(getattr(claim, 'old_elapsed_seconds', 0) or 0)),
            },
            'target': {
                'task_id': getattr(instruction, 'task_id', None)
                           if instruction is not None else None,
                'zone_name': instruction.zone_name
                             if instruction is not None else '',
                'duration_seconds': int(instruction.duration_seconds or 0)
                                    if instruction is not None else 0,
                # 目标 run 此时还不存在（意图登记在 start_streaming 里）：
                # 留空，由 _bind_handover_target_run 在意图冻结后补记。
                'run_id': '',
            },
        }
        with self.state._state_io_lock:
            # 原子区间：重核（读 state）与落盘（写 handover）之间，状态写
            # （/state/update、update_progress 落盘）全部被挡在外面。
            reject = self.verify_saved_abandon_claim(claim)
            if reject:
                return False, reject
            # 交接状态机写边界：duplicate/superseded 判断与落盘必须在同一
            # 互斥区间——入口处的读在这里已可能过期（另一确认刚落盘）。
            with self._handover_lock:
                try:
                    existing = json.loads(
                        path.read_text(encoding='utf-8'))
                except (OSError, TypeError, ValueError):
                    existing = None
                if isinstance(existing, dict) \
                        and str(existing.get('confirm_id') or '') == confirm_id \
                        and existing.get('outcome') == 'pending':
                    return False, 'duplicate_handover'
                if isinstance(existing, dict) \
                        and existing.get('outcome') == 'pending':
                    # 上一次交接还没有结果：先把它的结果定为"未执行"，避免
                    # 两份交接互相覆盖。旧进度本身没被动过，所以"未执行"等于
                    # 什么都没发生。这是**有意**的无身份覆盖：新确认明示替代
                    # 旧确认（RLock 重入，同一线程内完成）。
                    self._finalize_handover('superseded')
                if not self._atomic_write_json(path, payload):
                    return False, 'persist_failed'
        self._last_handover_id = payload['handover_id']
        self._push_backend_event(
            '开播', 'info',
            f'已受理"放弃未结算旧进度"的确认（{payload["old"]["zone_name"]}'
            f' → {payload["target"]["zone_name"]}）；'
            f'目标确认开播前旧进度仍可恢复')
        return True, ''

    def _finalize_handover(self, outcome: str,
                           handover_id: Optional[str] = None,
                           target_run_id: Optional[str] = None,
                           epoch: Optional[int] = None) -> bool:
        """给交接记录一个结论——**只结算属于自己那一份**。

        - ``abandoned``：目标已确认接管，旧会话的本次进度到此为止（历史天数
          不受影响）；
        - ``void``：目标没有确认接管（失败/待人脸/被取消/服务重启），旧进度
          始终没被动过——真实情况就是"什么都没发生"；
        - ``superseded``：被更新的交接覆盖。

        归属核对（给了才核，全部通过才落笔）：
        - ``handover_id``：创建该记录的同一请求才拿得到；
        - ``target_run_id``：交接记录里冻结的目标会话身份——旧开播线程的
          finally、迟到的平台响应、另一次新开播，run 不同就**不得**改写；
        - ``epoch``：发起交接的控制代际。

        返回值语义（调用方必须区分对待）：
        - ``handover_id`` 未给（尽力而为的收尾路径）：``True`` = 结论已持久化
          **或**本就没有过交接记录（普通开播的常态，无可结论对象——不许把
          这种情况误判成失败）；``False`` = 没有落笔（待结论交接持久写失败、
          文件损坏、身份不符，或记录已有结论——幂等：不再改写）。
        - ``handover_id`` 给定（**接管必要条件**，``_complete_session_takeover``
          的 ``abandoned`` 结论）：文件缺失、损坏、已有结论、身份不符或写
          失败**一律 False**——要求的这份记录没有按身份成功落笔，接管方
          必须中止，绝不能在结论未落盘时覆盖旧存档或宣告成功。收尾路径（void）忽略返回值：幂等，写不进就留待下一次。

        读—核—写在同一把 ``_handover_lock`` 内完成：旧线程在"校验已通过、
        写入未发生"的窗口里被并发的 record/bind 换掉记录时，锁内**重读**
        拿到的是新记录，归属核对不再通过，过期写入自然消失。
        """
        path = self._handover_file()
        with self._handover_lock:
            if not path.exists():
                if handover_id is not None:
                    # 要求的这份交接记录不存在（被删/从未落成）：必要条件
                    # 不成立，保守按失败处理。
                    return False
                # 从没有过交接记录：普通开播的常态，无可结论对象。
                return True
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, TypeError, ValueError):
                # 文件存在但读不出来：无法证明"没有待结论的交接"，也无法
                # 在其上落笔——保守按失败处理（接管调用方会中止）。
                return False
            if not isinstance(record, dict):
                return False
            if record.get('outcome') != 'pending':
                # 已有结论（void/abandoned/superseded）：不再改写。
                return False
            if handover_id is not None \
                    and str(record.get('handover_id') or '') != str(handover_id):
                return False
            target = record.get('target') or {}
            if target_run_id is not None \
                    and str(target.get('run_id') or '') != str(target_run_id):
                return False
            if epoch is not None:
                try:
                    if int(record.get('epoch') or 0) != int(epoch):
                        return False
                except (TypeError, ValueError):
                    return False
            record['outcome'] = outcome
            record['resolved_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return self._atomic_write_json(path, record)

    def _bind_handover_target_run(self, epoch: int, run_id: str,
                                  expected_handover_id: Optional[str] = None
                                  ) -> Optional[str]:
        """把交接记录的目标 run 补记为本次意图冻结的 run_id。

        交接记录在 API 落盘时目标 run 还不存在（``_pending_run_id`` 在
        ``start_streaming`` 的意图登记里才生成），target.run_id 只能先留空。
        意图登记后立即补记，此后接管/收尾的归属核对才有完整的
        (handover_id, target run, epoch) 三元组。只改**属于本 epoch** 的
        pending 记录；别的交接不碰。

        ``expected_handover_id``：验证后重试沿用**原确认**的必要条件——
        当前文件里的记录必须是这一个 handover_id（被并发换掉/已结论/损坏
        一律不绑）。绝不允许"同一代际的另一份交接"冒充原确认。

        返回绑定成功的 ``handover_id``（受理方把它冻结进当前开播意图，
        一路带到最终结论）；``None`` = 绑定失败（调用方**必须**检查）：
        pending 不存在/不属于本代际/身份不符，或持久写失败。绑定是"已确认
        放弃旧进度"的必要条件——绑定不上就不得受理这次开播（旧进度保持
        可恢复），见 ``start_streaming``。
        """
        path = self._handover_file()
        with self._handover_lock:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, TypeError, ValueError):
                return None
            if not isinstance(record, dict) or record.get('outcome') != 'pending':
                return None
            if expected_handover_id is not None \
                    and str(record.get('handover_id') or '') \
                        != str(expected_handover_id):
                # 当前记录已经不是用户当初确认的那一份（被替换/另起一份）：
                # 沿用原确认的前提不复存在，绝不冒充。
                return None
            try:
                if int(record.get('epoch') or 0) != int(epoch or 0):
                    return None
            except (TypeError, ValueError):
                return None
            target = record.get('target')
            if not isinstance(target, dict):
                target = {}
                record['target'] = target
            target['run_id'] = str(run_id or '')
            if not self._atomic_write_json(path, record):
                return None
            return str(record.get('handover_id') or '') or None

    def _complete_session_takeover(self, epoch: int, run_id: str,
                                   instruction: LiveInstruction,
                                   saved_elapsed: int, source: str,
                                   is_task_mode: bool,
                                   dur_label: str,
                                   handover_id: Optional[str] = None) -> bool:
        """把一场已在平台确认的直播**接管为当前会话**（恰好执行一次）。

        这是"旧进度真正让位"的唯一时刻，包含四件必须同属一个所有权边界的事：

        1. 预留身份复核（防御性：预留本应挡住破坏性写，这里兜底；失败 =
           不能接管，调用方收尾关房）；
        2. 交接结论 ``abandoned``（**先于** begin_session 落盘：崩溃窗口内
           "记录已结论、存档还是旧会话"是安全方向——旧进度仍可恢复；反过来
           会造成存档已切走而记录还 pending 的矛盾）；
        3. ``begin_session`` 落盘新会话；
        4. ``stream_actual_start`` 诊断与**成功通知**——成功邮件只属于
           "接管完成"，不属于"平台开房"。

        ``handover_id`` 是**本次开播意图**在受理时冻结的交接要求身份：
        - 非 None：本次接管带着"用户确认放弃旧进度"的要求，``abandoned``
          必须按 (handover_id, target run, epoch) 三元组持久写成功，否则
          中止接管；
        - None：本次开播没有交接要求（普通开播/恢复/重连）——磁盘上的
          历史终态记录（void/abandoned/superseded）是无关历史，**不得**
          阻断接管，也不在此处被改写。

        手动/OBS 模式的可证明事实就是"直播间已开启"（不冒称本地媒体），在
        平台开房后立即调用；FFmpeg 模式推迟到本地媒体首次确认推进
        （见 ``_deferred_takeover``）。返回 False = 不能接管。
        """
        task_id = getattr(instruction, 'task_id', None)
        if is_task_mode and task_id is not None:
            if not self._register_active_task(
                    instruction, run_id=run_id,
                    validate=self._reserved_identity_validator(instruction)):
                self._push_backend_event(
                    '开播', 'warning',
                    f'任务「{instruction.zone_name}」在开播等待期间被改动，'
                    f'本次开播已取消；旧进度未受影响')
                return False
        # 交接结论先落笔（见 docstring 的崩溃窗口说明）。**结论未持久化就
        # 中止接管**：``abandoned`` 是"旧进度正式让位"的必要事实——写失败
        # 时覆盖 live_state（begin_session）、结算或发成功通知，都会在重启
        # 后制造"记录 pending、旧进度却被换走"的矛盾，甚至丢掉用户确认要
        # 放弃的那份进度。返回 False 时旧存档一个字节都没动，保持可恢复；
        # 已开的平台房间由调用方按既有收尾路径关停（不双推、不误停新会话）。
        # 本次意图**没有**交接要求（handover_id=None）时不落笔：历史终态
        # 记录是无关历史，普通开播/恢复不被上一次交接阻断（R3）。
        if handover_id is not None and not self._finalize_handover(
                'abandoned', handover_id=handover_id,
                target_run_id=str(run_id or ''),
                epoch=int(epoch or 0)):
            logger.warning(
                '旧进度交接结论未能持久化，中止本次接管（zone=%s run=%s）',
                instruction.zone_name, run_id)
            self._push_backend_event(
                '开播', 'warning',
                f'旧进度交接结论未能保存，本次接管中止；'
                f'旧进度保留，可稍后恢复。请检查存储后重试')
            diagnostics.get_diagnostics().record(
                'takeover_aborted_handover_persist',
                zone=instruction.zone_name, run_id=str(run_id or ''))
            return False
        self.state.begin_session(
            instruction.zone_name, self.current_room_id,
            instruction.duration_seconds, duration_known=True,
            source=source, task_id=task_id,
            execution_date=instruction.execution_date,
            initial_elapsed=int(saved_elapsed or 0),
            service_instance=self._boot_id,
            run_id=str(run_id or ''))
        self.state.mark_phase(PHASE_LIVE, persist=True)
        # **接管成功的宣告永远晚于持久化的确认**：begin_session 或阶段落盘
        # 失败时（内存已切、磁盘仍是旧会话），绝不发 [开播]/[轮转] 成功邮件
        # ——重启后磁盘恢复的是旧会话，宣称成功会变成一句兑现不了的承诺。
        # 交接记录此刻已是 abandoned（先落笔的），磁盘旧进度仍随文件保留、
        # 重启后可恢复；自动结算与轮转由 recovery_blocked 阻断。
        if not getattr(self.state, 'last_session_persist_ok', True) \
                or getattr(self.state, '_checkpoint_failure_active', False):
            self._recovery_blocked = (
                self.state.resume_blocked_reason
                or '直播会话存档失败，已阻断自动结算与轮转')
            self._push_backend_event(
                '恢复', 'danger',
                f'{self._recovery_blocked}；直播仍在运行，请检查存储后再处理')
            diagnostics.get_diagnostics().record(
                'takeover_persist_degraded', zone=instruction.zone_name,
                run_id=str(run_id or ''),
                begin_persist=bool(getattr(
                    self.state, 'last_session_persist_ok', True)))
        diagnostics.get_diagnostics().record(
            'stream_actual_start', source=source,
            zone=instruction.zone_name, room_id=self.current_room_id,
            duration_seconds=instruction.duration_seconds,
            inherit_elapsed=int(saved_elapsed or 0))
        mode_label = '任务模式' if is_task_mode else '手动模式'
        source_label = {'new': '新开播', 'resume': '恢复续播',
                        'reconnect': '自动重连', 'manual': '手动开播'}.get(
                            source, '开播')
        saved_label = (f'（已播{int(saved_elapsed or 0) // 60}分钟）'
                       if int(saved_elapsed or 0) > 0 else '')
        session_persist_confirmed = (
            getattr(self.state, 'last_session_persist_ok', True)
            and not getattr(self.state, '_checkpoint_failure_active', False))
        if not (is_task_mode and not session_persist_confirmed):
            self._push_backend_event(
                '开播', 'success',
                f'{mode_label}{source_label}{saved_label}'
                f' - {instruction.zone_name}，时长{dur_label}')
        # 轮转合并信的 B 结果沿用同一事实门槛：接管此刻才真正完成，B 的
        # started 结论在这里收口（_finish_task_rotation_context 幂等：
        # 上下文不属于本场或已被收口时是无害 no-op）。
        self._finish_task_rotation_context(
            getattr(self, '_task_rotation_context', None), 'started')
        return True

    def _abort_open_after_failed_takeover(self, epoch: int, run_id: str,
                                          is_task_mode: bool) -> None:
        """接管失败后的收尾：关掉刚开的平台房间、停掉本地推流。

        旧存档一个字节都不动（deferred 窗口内 begin_session 从未执行）；
        交接记录由各方按身份写 void。在独立线程里跑：调用点可能持有
        控制锁，不能在里面再做带等待的停播清理。
        """
        try:
            self.stop_monitor.set()
            self._clear_face_verify_for_stopped_run(run_id, epoch)
            self._stop_live_process(preserve_state=is_task_mode)
            self.current_instruction = None
        except Exception:
            logger.exception('接管失败后的停播清理异常')
        finally:
            # 接管已中止：属于本次目标的交接按身份结论为"未执行"（旧进度
            # 从未被动过）。写失败也无妨——pending 原样保留，旧进度仍可
            # 恢复，重启清理或下一次确认会把它推到终态。
            try:
                self._finalize_handover('void',
                                        target_run_id=str(run_id or ''))
            except Exception:
                logger.debug('接管失败收尾的交接结论写入异常', exc_info=True)
            with self._start_mutex():
                if not self.is_streaming:
                    self._release_active_task(run_id)
                self._is_starting = False

    def _discard_deferred_takeover(self, epoch, run_id) -> bool:
        """清掉属于 (epoch, run_id) 的推迟接管记录；不属于则不动。

        ``epoch=None`` 表示只按 run 匹配（停止收尾：控制代际已被停止推进，
        记录里的旧代际本来就是它自己的身份）。
        """
        pending = getattr(self, '_deferred_takeover', None)
        if pending is None:
            return False
        run_match = str(pending.get('run_id') or '') == str(run_id or '')
        try:
            epoch_match = (epoch is None
                           or int(pending.get('epoch') or 0) == int(epoch or 0))
        except (TypeError, ValueError):
            epoch_match = False
        if run_match and epoch_match:
            self._deferred_takeover = None
            return True
        return False

    def _complete_deferred_takeover(self, pending: dict) -> bool:
        """本地媒体首次确认推进：完成被推迟的会话接管（幂等，只执行一次）。

        返回 False = 未能接管（任务被改动/写盘失败）：调用方必须暂停计时并
        返回 paused_pusher；收尾线程会关房停流——此时绝不入账、绝不发成功信。
        """
        epoch = int(pending.get('epoch') or 0)
        run_id = str(pending.get('run_id') or '')
        # 所有权核对必须**先于**清除记录：_owns_current_run 对推迟窗口的
        # 认可正来自这条记录本身。
        if not self._owns_current_run(epoch, run_id):
            self._deferred_takeover = None
            return False
        self._deferred_takeover = None
        instruction = pending.get('instruction')
        try:
            taken = self._complete_session_takeover(
                epoch=epoch, run_id=run_id, instruction=instruction,
                saved_elapsed=int(pending.get('saved_elapsed') or 0),
                source=str(pending.get('source') or SOURCE_NEW),
                is_task_mode=True,
                dur_label=str(pending.get('dur_label') or ''),
                handover_id=(str(pending.get('handover_id') or '') or None))
        except Exception:
            logger.exception('推迟接管执行失败')
            taken = False
        if not taken:
            # 媒体在推、但会话不能接管：不能让"无身份的推流"继续占用房间，
            # 也不能留下一份既没接管又没下播的半场。收尾在独立线程执行
            #（调用点持有控制锁）。
            threading.Thread(target=self._abort_open_after_failed_takeover,
                             args=(epoch, run_id, True),
                             name='TakeoverAbort', daemon=True).start()
        return taken

    def start_streaming(self, instruction: LiveInstruction, video_path: str,
                        is_task_mode: bool = True, epoch: int = None,
                        source: str = SOURCE_NEW,
                        inherit_elapsed: Optional[int] = None,
                        expected_face_run_id: Optional[str] = None,
                        expected_face_room_id: Optional[int] = None,
                        handover_confirmed: bool = False,
                        expected_handover_id: Optional[str] = None) -> bool:
        """接受开播请求，使用独立后台线程持续等待网络恢复。

        epoch=None 表示这是停止之后（或从未停止时）新发起的请求，取当前代际；
        epoch 为旧代际说明该请求在停止之前挂起，现在必须作废。

        source 区分 new / resume / reconnect：
        - resume 必须对应**原会话身份**（分区 + task_id + 执行日）才能继承进度，
          否则不得静默从零续播，也不得把别的任务进度继承过来；
        - 恢复/重连都不重新抽取时长（时长来自原会话或原指令）；
        - inherit_elapsed 由调用方在**意图登记时**算好并冻结，避免等待期间
          状态被改写后继承到错误的进度。
        """
        with self._start_lock:
            if epoch is None:
                epoch = self._control_epoch
            elif not self._is_epoch_current(epoch):
                logger.info("放弃一次已经过期的开播请求（停止已在其发起后生效）")
                self._push_backend_event(
                    '停止', 'info', '已放弃停止之前发起的开播请求，未重新开播')
                diagnostics.get_diagnostics().record(
                    'start_dropped_stale_epoch', epoch=epoch,
                    current=self._control_epoch,
                    zone=instruction.zone_name if instruction else '')
                return False
            # 不得为新任务清理一场仍在播/正在准备的会话，即使 DB 恰好已经有
            # 结算标记；先用同一把意图锁拦住副作用。
            if self._is_starting or self.is_streaming:
                return False
            if expected_face_run_id is not None:
                current = self.current_instruction
                if (str(self._pending_run_id or '') != str(expected_face_run_id)
                        or current is None
                        or current.zone_name != instruction.zone_name
                        or current.task_id != instruction.task_id
                        or current.execution_date != instruction.execution_date
                        or current.duration_seconds != instruction.duration_seconds):
                    logger.info("面板/邮件确认的冻结开播意图已被接管，拒绝重试")
                    return False
            if expected_face_room_id is not None:
                try:
                    room_matches = (int(expected_face_room_id)
                                    == int(self.current_room_id or 0))
                except (TypeError, ValueError):
                    room_matches = False
                if not room_matches:
                    logger.info("面板/邮件确认的直播间身份已变化，拒绝重试")
                    return False
            # ``handover_confirmed``：用户已经在干净的确认里看过旧进度的影响，
            # 且冻结身份全部核对通过 + 交接记录已落盘。此时旧存档不再充当闸门
            # ——但它照样留在原地，直到本会话真正被平台确认开播才由
            # ``begin_session`` 顶替（产品决定：目标未确认开播前旧进度仍可恢复）。
            # 别的安全门（在播/正在开播/FFmpeg 未回收/旧房间未确认下播）全部照旧。
            if is_task_mode and source == SOURCE_NEW and not handover_confirmed:
                startable, why = self.new_task_verdict()
                if not startable:
                    return self._reject_start(why)
            if source == SOURCE_RESUME and inherit_elapsed is None:
                # 调用方未显式给出继承量：按身份核对结果在入口处冻结一次。
                inherit_elapsed = self._resolve_resume_inheritance(instruction)
                if inherit_elapsed is None:
                    return False
            # 会话身份在**意图登记时**冻结：整个等待/恢复过程不再变动。
            # 手动模式不写 live_state，因此必须由控制器自己持有这个身份。
            if source == SOURCE_RESUME:
                self._pending_run_id = self.state.run_id or self._new_run_id()
            elif source == SOURCE_RECONNECT:
                self._pending_run_id = self.state.run_id or self._new_run_id()
            else:
                self._pending_run_id = self._new_run_id()
            if expected_face_run_id is None:
                # 全新意图：本次开播默认**没有**交接要求（R3）。历史终态
                # 交接记录对它是无关历史；要求只在下面 handover_confirmed
                # 分支里按绑定结果重新冻结。
                self._pending_handover_id = None
            if handover_confirmed:
                # 交接记录在 API 落盘时目标 run 还未登记（意图此刻才冻结）：
                # 立即补记为本次意图的 run，此后接管/收尾的归属核对才有
                # 完整的 (handover_id, target run, epoch) 三元组。
                # ``expected_handover_id``（R4，人脸验证重试）：沿用**原确认**
                # 的前提是当前文件里仍是这一份记录——被并发换掉/已结论/损坏
                # 一律绑定失败。绝不允许裸 handover_confirmed 绑一份陌生记录。
                # **绑定失败 = 受理失败**：绑定是"已确认放弃旧进度"的必要
                # 条件（持久写失败 / pending 已被并发换掉）。此时平台还没被
                # 调用、`_is_starting` 还没置位，直接拒绝——旧存档一个字节
                # 都没动，交接保持 pending（无目标 run），旧进度可恢复，
                # 用户可重试确认或改选恢复。
                bound = self._bind_handover_target_run(
                    epoch, self._pending_run_id,
                    expected_handover_id=expected_handover_id)
                if bound is None:
                    self._push_backend_event(
                        '开播', 'warning',
                        f'旧进度交接记录未能确认（落盘失败或已被更新），'
                        f'本次开播未受理；旧进度保留，可重试或先恢复')
                    diagnostics.get_diagnostics().record(
                        'start_rejected_handover_bind',
                        zone=instruction.zone_name,
                        task_id=getattr(instruction, 'task_id', None))
                    self._last_start_reason = (
                        '旧进度交接记录未能确认（落盘失败或已被更新），'
                        '本次开播未受理；旧进度保留，可重试或先恢复')
                    return False
                # 交接要求身份随本次意图冻结：worker → deferred takeover →
                # 最终结论全程携带，接管时只结论这一份。人脸验证重试沿用
                # 原确认时也走本分支（handover_confirmed=True + 冻结的
                # expected_handover_id）；不再存在"只凭共享字段就重绑一份
                # 陌生记录"的旁路——R4：重试必须按原 handover_id 重绑定并
                # 持久写成功后才能继续。
                self._pending_handover_id = bound
            # 任务模式的"在播预留"：与破坏性写共用同一把锁，受理即预留，
            # 保证"受理 → 平台返回"期间删除/修改/覆盖同一个任务会被拒绝。
            if instruction is not None and getattr(instruction, 'task_id', None) is not None:
                # 恢复类意图把"目标仍然成立"的判定一起放进预留的临界区：
                # 只在入口查一次会留下"查完 → 被同名替换 → 预留"的窗口。
                # 指定任务选择同理：select 与 reserve 之间的 CRUD 并发（改名/
                # 结算/删除）由同一临界区内的冻结前置条件复核拦下。
                owner_check = None
                if source == SOURCE_RESUME:
                    owner_check = self._resume_owner_validator(instruction)
                elif getattr(instruction, 'source_revision', None) is not None:
                    owner_check = self._named_selection_validator(instruction)
                if not self._register_active_task(instruction,
                                                  run_id=self._pending_run_id,
                                                  validate=owner_check):
                    if owner_check is None:
                        self._push_backend_event(
                            '开播', 'warning',
                            f'任务「{instruction.zone_name}」已不存在（可能刚被删除或重建），'
                            f'本次开播已取消')
                    else:
                        self._push_backend_event(
                            '开播', 'warning',
                            f'任务「{instruction.zone_name}」在确认之后已被改动'
                            f'（改名/结算/删除/编辑），本次开播已取消；'
                            f'请刷新任务列表后重新选择')
                    diagnostics.get_diagnostics().record(
                        'start_rejected_task_missing',
                        zone=instruction.zone_name,
                        task_id=instruction.task_id)
                    return False
            cancel = threading.Event()
            self._start_cancel = cancel
            self._is_starting = True
            # 新意图接管：此前的自动重连即便平台响应迟到，也不得回滚本场房间。
            self._start_intent_id += 1
            # 新意图接管后，上一次停止的"清理已完成"不再适用于本会话：
            # 之后到来的停止必须真正执行一次下播。
            self._stop_cleanup_done = False
            self._platform_stop_done = False
            self._recovery_blocked = ''
            self._pending_face_verify = False
            self._face_verify_url = ''
            self._face_verify_run_id = ''
            self._face_verify_epoch = None
            self._face_verify_intent = None
            self.current_instruction = instruction
            self._pending_source = source
            self._pending_inherit = int(inherit_elapsed or 0)
            self._start_thread = threading.Thread(
                target=self._start_in_background,
                args=(instruction, video_path, is_task_mode, cancel, epoch,
                      self._pending_run_id),
                name="LiveStart", daemon=True)
            self._start_thread.start()
            # 任务选择时尚无 run_id；在意图身份冻结后记录，供事后把
            # 开播、FFmpeg 退出、恢复与结算归到同一场。已受理不等于已开播。
            logger.info(
                "开播意图已受理：run_id=%s task_id=%s source=%s zone=%s",
                self._pending_run_id, getattr(instruction, 'task_id', None),
                source, instruction.zone_name)
            # 已接收 ≠ 已完成：诊断只记录“已接收”。
            diagnostics.get_diagnostics().record(
                'start_accepted', source=source,
                epoch=epoch, zone=instruction.zone_name,
                run_id=self._pending_run_id,
                task_id=getattr(instruction, 'task_id', None),
                duration_seconds=instruction.duration_seconds,
                inherit_elapsed=int(inherit_elapsed or 0))
        return True

    def _resolve_resume_inheritance(self, instruction: LiveInstruction) -> Optional[int]:
        """恢复意图的入口判定：能不能恢复、能继承多少秒。

        返回 None = 拒绝（可恢复性、身份或时长任一不成立）。这里**不**退回
        "继承 0 秒继续播"——那正是"进度莫名归零"的来源。
        """
        state = self.state
        resumable, reason = self.get_resume_status()
        if not resumable:
            logger.warning(f"拒绝恢复：{reason}")
            self._push_backend_event('恢复', 'warning', f'无法恢复：{reason}')
            return None
        if not state.matches_identity(
                instruction.zone_name, instruction.task_id,
                instruction.execution_date):
            logger.warning(
                f"拒绝恢复：身份不匹配（目标 {instruction.zone_name}/"
                f"task_id={instruction.task_id}，已保存 {state.current_zone}/"
                f"task_id={state.task_id}）")
            self._push_backend_event(
                '恢复', 'warning', '无法恢复：保存的进度属于另一个任务/执行日')
            return None
        # 存档自比只能证明"这份指令出自这份存档"。业务校验必须看**当前任务库**：
        # 任务可能已完成、被同名替换、执行日已过期（旧实现缺这一步）。
        tm = getattr(self, 'task_manager', None)
        task_id = getattr(instruction, 'task_id', None)
        if tm is not None and task_id is not None:
            try:
                row = tm.db.get_task_by_id(task_id)
            except Exception as exc:
                logger.warning('恢复校验读取任务库失败：%s', exc)
                return None
            reject = self._current_owner_rejection(
                row, instruction.zone_name, instruction.execution_date)
            if reject:
                logger.warning(f"拒绝恢复：{reject}")
                self._push_backend_event('恢复', 'warning', f'无法恢复：{reject}')
                return None
        if not state.duration_known:
            logger.warning("拒绝恢复：目标时长未知，无法在不重新抽时长的前提下续播")
            self._push_backend_event(
                '恢复', 'warning', '无法恢复：目标时长未知，请手动开播或清空状态后重试')
            return None
        return max(0, int(state.elapsed_seconds))

    def get_resume_status(self) -> Tuple[bool, str]:
        """用状态存档和当前任务库给所有恢复入口同一结论。"""
        state = self.state
        if getattr(state, 'resume_blocked_reason', ''):
            return False, str(state.resume_blocked_reason)
        reason_fn = getattr(state, 'resume_reason', None)
        reason = reason_fn() if callable(reason_fn) else ''
        if reason:
            return False, reason
        resumable_fn = getattr(state, 'resumable', None)
        resumable = (bool(resumable_fn()) if callable(resumable_fn)
                     else bool(getattr(state, 'current_zone', '')
                               and not getattr(state, 'corrupt', False)))
        if not resumable:
            return False, '没有可恢复的会话'
        duration_known = bool(getattr(state, 'duration_known', False))
        duration_seconds = _safe_int(getattr(state, 'duration_seconds', 0), 0)
        if not duration_known and duration_seconds <= 0:
            return False, '目标时长未知，请手动开播或清空状态后重试'

        zone = getattr(state, 'current_zone', '') or ''
        if not zone:
            return False, '没有可恢复的会话（未保存分区）'
        today = date.today().isoformat()
        tm = getattr(self, 'task_manager', None)
        if tm is None:
            if getattr(state, 'source_mode', SOURCE_NEW) != SOURCE_MANUAL:
                return False, '当前任务库不可用，无法核对保存任务的结算状态'
            execution_date = getattr(state, 'execution_date', None)
            if execution_date and execution_date != today:
                return False, (f'保存的执行日 {execution_date} 已过期'
                               f'（今天是 {today}），不能把旧进度接到新的一天')
            return True, ''

        try:
            task_id = getattr(state, 'task_id', None)
            row = (tm.db.get_task_by_id(task_id) if task_id is not None
                   else tm.db.get_task_by_zone(zone))
        except Exception as exc:
            logger.warning('无法核对恢复任务的当前状态：%s', exc)
            return False, '无法读取当前任务记录，暂不可恢复；请检查任务库后重试'
        reason = self._current_owner_rejection(
            row, zone, getattr(state, 'execution_date', None))
        return (False, reason) if reason else (True, '')

    def resolve_resume_target(self) -> Tuple[Optional[LiveInstruction], str]:
        """恢复入口：用**当前任务库**核对保存的身份，返回 (instruction, 原因)。

        旧实现把 ``state.task_id`` / ``state.execution_date`` 原样塞进 instruction，
        再交给 ``state.matches_identity`` 比较——等于"存档与自己比较"。于是任务
        被删除后同名重建、被改名、已完成、执行日已过期都会照旧受理，接手另一个
        任务的进度。这里改为查当前 DB 的稳定身份。

        旧版状态文件没有 task_id：按分区名在当前库解析**一次**并写回（明确的
        兼容迁移），之后一律按 id 核对，不再靠名字。
        """
        state = self.state
        resumable, reason = self.get_resume_status()
        if not resumable:
            return None, reason
        saved_duration = _safe_int(state.duration_seconds, 0)
        if not state.duration_known and saved_duration > 0:
            # 标记缺失但时长本身有值（旧状态文件 / 手工改写）：那仍然是"已知的
            # 时长"，按它继续；留一条日志，不静默。
            logger.warning('状态缺少"时长已知"标记，按已保存时长 %s 秒继续',
                           saved_duration)
        if not state.duration_known and saved_duration <= 0:
            # 既没有标记、也没有正值：这才是真正的"时长未知"（0 会被当成不限时，
            # 等于把原计划悄悄改掉），拒绝并说明原因。
            logger.warning('拒绝恢复：目标时长未知，无法在不重新抽时长的前提下续播')
            self._push_backend_event(
                '恢复', 'warning',
                '无法恢复：目标时长未知，请手动开播或清空状态后重试')
            return None, '目标时长未知，请手动开播或清空状态后重试'
        zone = state.current_zone or ''
        if not zone:
            return None, '没有可恢复的会话（未保存分区）'
        tm = getattr(self, 'task_manager', None)
        saved_task_id = state.task_id
        if saved_task_id is None and tm is not None:
            try:
                row = tm.db.get_task_by_zone(zone)
            except Exception as exc:
                logger.warning('恢复入口按分区读取任务库失败：%s', exc)
                return None, '无法读取当前任务记录，暂不可恢复；请检查任务库后重试'
            if row is None:
                return None, f'保存的任务「{zone}」已不在任务库中，无法恢复'
            saved_task_id = row['id']
            state.task_id = saved_task_id
            state.save()
            logger.info('旧状态文件缺少 task_id，已迁移为 id=%s（分区 %s）',
                        saved_task_id, zone)
        exec_day = state.execution_date
        today = date.today().isoformat()
        if tm is not None:
            try:
                row = tm.db.get_task_by_id(saved_task_id)
            except Exception as exc:
                logger.warning('恢复入口读取任务库失败：%s', exc)
                return None, '无法读取当前任务记录，暂不可恢复；请检查任务库后重试'
            reason = self._current_owner_rejection(row, zone, exec_day)
            if reason:
                return None, reason
        elif exec_day and exec_day != today:
            return None, (f'保存的执行日 {exec_day} 已过期（今天是 {today}），'
                          f'不能把旧进度接到新的一天')
        instruction = LiveInstruction(
            zone_name=zone, duration_seconds=int(state.duration_seconds or 0),
            task_id=saved_task_id, execution_date=exec_day or today)
        return instruction, ''

    @staticmethod
    def _parse_today_done(value) -> Optional[bool]:
        """只接受任务库约定的 NULL/0/1，坏值不能被 truthy 转成已完成。"""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip() in ('0', '1'):
            return value.strip() == '1'
        return None

    def _current_owner_rejection(self, row, zone: str,
                                 execution_date: Optional[str]) -> str:
        """当前任务库里"这条恢复目标"是否仍然成立；返回拒绝原因（'' = 通过）。

        恢复类入口**唯一**的业务校验：存档与存档相比只能证明"这份指令出自这份
        存档"，证明不了任务还在、没被同名替换、没被标完成、执行日没过期。
        手动 `/resume`、启动自动恢复、验证后重试，以及"检查 → 在播预留"的
        临界区都走这里，不允许各自实现一套。
        """
        if row is None:
            return '保存的任务已被删除，无法恢复（请手动开播或清空状态）'
        if row.get('zone_name') != zone:
            return '保存的任务已被改名，无法按原身份恢复'
        today = date.today().isoformat()
        last_done = str(row.get('last_done_date') or '')
        done_today = self._parse_today_done(row.get('today_done'))
        if done_today is None:
            return '任务完成标记不可识别，需核查后才能恢复'
        done_on_saved_day = bool(execution_date and last_done
                                 and last_done == execution_date)
        if ((done_today and last_done and last_done != today)
                or (not done_today and last_done == today)):
            return '任务完成标记与执行日不一致，需核查后才能恢复'
        if int(row.get('category') or 0) == 0:
            return '保存的任务已完成，无法恢复'
        if done_today:
            return '保存的任务已标记今日完成，不能恢复已结算进度'
        if done_on_saved_day:
            return f'保存的任务已在执行日 {execution_date} 结算，不能恢复'
        if last_done == today:
            return '保存的任务今日已结算，不能恢复已结算进度'
        if execution_date and execution_date != today:
            return (f'保存的执行日 {execution_date} 已过期（今天是 {today}），'
                    f'不能把旧进度接到新的一天')
        return ''

    def _state_matches_session(self, instruction, run_id: str) -> bool:
        """只在状态存档确属同一已提交会话时，才允许停止/结算改写它。"""
        if not instruction or not run_id:
            return False
        if str(getattr(self.state, 'run_id', '') or '') != str(run_id):
            return False
        return self.state.matches_identity(
            instruction.zone_name, getattr(instruction, 'task_id', None),
            getattr(instruction, 'execution_date', None))

    def _clear_settled_session_state(self, instruction, run_id: str,
                                     epoch: int) -> bool:
        """结算成功后按冻结身份清存档；迟到的 A 结果不得清除新会话。"""
        with self._start_mutex():
            if epoch != self._control_epoch or self.is_streaming or self._is_starting:
                return False
            if self._current_run_id and self._current_run_id != run_id:
                return False
            current = self.current_instruction
            if current and (current.task_id != instruction.task_id
                            or current.zone_name != instruction.zone_name
                            or current.execution_date != instruction.execution_date):
                return False
            if not self._state_matches_session(instruction, run_id):
                return False
            # 不要先在内存里清掉唯一进度再发现磁盘写失败。save() 会显式返回
            # bool 而不抛异常，所以失败时恢复内存快照并保留可见阻断原因。
            if not self.state.stop_streaming(preserve=False):
                self.state.resume_blocked_reason = (
                    f'任务已结算，但保存状态清理失败'
                    f'（{self.state.resume_blocked_reason or self.state._last_save_error}）；'
                    '轮转已暂停，请检查状态文件后再继续')
                logger.error(self.state.resume_blocked_reason)
                return False
            logger.info('已结算会话存档已按 task/run/day 清除：%s/%s/%s',
                        instruction.zone_name, instruction.task_id, run_id)
            try:
                (self.state.state_file.parent / '.resume_checkpoint_failed').unlink(
                    missing_ok=True)
            except OSError as exc:
                logger.warning("旧结算存档已清理，但恢复失败标记未删除：%s", exc)
            return True

    def _active_handover_requirement_locked(self, face_run: str) -> Optional[str]:
        """锁内判定：当前意图是否仍带着一份**未结论**的交接要求（R4）。

        返回 ``handover_id``：``_pending_handover_id`` 属于本次意图、文件里
        那份记录仍 pending、且目标 run 与本意图的 run 一致。否则 ``None``——
        没有要求、要求已被接管满足（abandoned）、已作废（void/superseded）
        或记录被换。重试把返回值作为 ``expected_handover_id`` 传给
        ``start_streaming``；``None`` = 按普通新开播走旧进度闸门，绝不为它
        跳过 ``new_task_verdict``。

        调用方必须已持有 ``_start_mutex``（本助手只在 ``_handover_lock`` 内
        读文件，锁序与交接状态机一致）；face_run 取自冻结意图，不能传共享
        字段冒充。
        """
        hid = getattr(self, '_pending_handover_id', None)
        if not hid or not face_run:
            return None
        try:
            record = json.loads(
                self._handover_file().read_text(encoding='utf-8'))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        if (str(record.get('handover_id') or '') == str(hid)
                and record.get('outcome') == 'pending'
                and str((record.get('target') or {}).get('run_id') or '')
                    == str(face_run)):
            return hid
        return None

    def _set_pending_face_verify(self, verify_url: str, run_id: str = '',
                                 epoch: Optional[int] = None) -> bool:
        """登记验证弹窗及其唯一意图所有者。"""
        with self._start_mutex():
            if epoch is None:
                # status-query 等路径没有独立参数时，只允许当前仍在启动/直播的
                # 意图登记验证；停止后的迟到结果不得借“此刻的新 epoch”复活弹窗。
                if (not (self.is_streaming or self._is_starting)
                        or self._start_cancel.is_set()
                        or self.stop_monitor.is_set()):
                    return False
                owner_epoch = self._control_epoch
            else:
                owner_epoch = int(epoch)
                if owner_epoch != self._control_epoch:
                    return False
            owner = (run_id or self._pending_run_id or self._current_run_id
                     or getattr(self.state, 'run_id', '') or '')
            instruction = self.current_instruction
            instruction_data = (instruction.dict() if instruction is not None
                                and hasattr(instruction, 'dict') else None)
            pending_owned = bool(owner and owner == self._pending_run_id)
            source = (getattr(self, '_pending_source', SOURCE_NEW)
                      if pending_owned else
                      (getattr(self, '_current_source', '')
                       or getattr(self, '_pending_source', SOURCE_NEW)))
            mode = ('manual' if (self._stream_mode == 'manual'
                                 or self._verify_retry_mode == 'manual') else 'task')
            inherit_elapsed = (getattr(self, '_pending_inherit', 0)
                               if pending_owned else
                               getattr(self.state, 'elapsed_seconds', 0))
            self._pending_face_verify = True
            self._face_verify_url = verify_url or ''
            self._face_verify_run_id = str(owner)
            self._face_verify_epoch = owner_epoch
            self._face_verify_intent = ({
                'source': source or SOURCE_NEW,
                'mode': mode,
                'inherit_elapsed': int(inherit_elapsed or 0),
                'instruction': instruction_data,
                # R4：把"用户已确认放弃哪一份旧进度"冻结进验证意图。登记
                # 时刻记录必然 pending（接管是唯一把 abandoned 写进去的
                # 路径，而接管还没发生）；重试时按这个 id 精确重绑定。
                'handover_id': self._active_handover_requirement_locked(
                    str(owner)),
            } if instruction_data else None)
            # 登记成功后立刻把"能不能从面板确认"做成**可见事实**。
            # owner 为空时仍然登记（用户还需要看验证 URL），但绝不能静默成功：
            # 旧实现无论可不可确认都返回 True 且不留痕，面板于是弹出一个
            # "我已验证"永远无效的窗，用户只能叉掉改点开播
            # （2026-09-25 instance-8000：POST confirm-face-verify 调用次数 = 0）。
            block = self.face_verify_block_reason()
            if block:
                logger.warning(
                    '人脸验证已登记但面板确认不可用：%s（zone=%s, epoch=%s）；'
                    '请完成 B 站验证后直接在面板点开播',
                    block, getattr(instruction, 'zone_name', '') or '', owner_epoch)
            return True

    def face_verify_confirm_owner(self):
        """返回面板确认所需的 ``(run_id, room_id, epoch)``；不可确认时返回 None。

        这是**服务端唯一的可确认性判定**，与 `/api/live/confirm-face-verify`
        端点的入参要求一一对应。面板据此决定是否给出"我已验证"，不再自己
        拿三个字段重拼一份 hasOwner——旧实现两边各判一次，服务端判"能确认"、
        前端判"不能"，于是出现点了没反应的按钮。
        """
        # 必须先判"是否挂起"：block_reason 对"没有待确认的验证"同样返回空串，
        # 不能拿它当作"三元组齐全"的证明（否则非挂起状态会去 int(None)）。
        if not self._pending_face_verify or self.face_verify_block_reason():
            return None
        try:
            return (str(self._face_verify_run_id),
                    int(self.state.room_id), int(self._face_verify_epoch))
        except (TypeError, ValueError):
            return None

    def face_verify_block_reason(self) -> str:
        """面板确认为什么不可用；空串 = 可确认。

        **派生值，不落状态**：若把"不可确认"存成字段，就得在每个清理点
        （停止、换场、重启加载…）各同步一次，漏一处就是幽灵按钮。这里按当前
        字段现算，永不与真实状态脱节。
        """
        if not self._pending_face_verify:
            return ''
        if not str(self._face_verify_run_id or ''):
            return '缺少会话标识（run_id），无法校验确认归属'
        try:
            epoch = int(self._face_verify_epoch)
        except (TypeError, ValueError):
            return '缺少控制代际（epoch），无法校验确认归属'
        try:
            room_id = int(self.state.room_id or 0)
        except (TypeError, ValueError):
            return '直播间号非法（room_id），无法校验确认归属'
        if epoch < 0:
            return '控制代际非法（epoch），无法校验确认归属'
        if room_id <= 0:
            return '直播间未确认（room_id 未知），无法校验确认归属'
        return ''

    def _clear_face_verify_for_stopped_run(self, run_id: str = '',
                                           epoch: Optional[int] = None) -> bool:
        """停止时清除对应的待验证弹窗；不得清另一代新会话的验证状态。"""
        with self._start_mutex():
            # 仅验证专属的冻结 owner 有权清除此状态。current/state 中保留的
            # 旧 A 不能授权旧异步收尾清掉随后 B 的 face intent。
            face_run = str(getattr(self, '_face_verify_run_id', '') or '')
            face_epoch = getattr(self, '_face_verify_epoch', None)
            if not self._pending_face_verify:
                return False
            if run_id and face_run != str(run_id):
                return False
            if epoch is not None and face_epoch != int(epoch):
                return False
            self._pending_face_verify = False
            self._face_verify_url = ''
            self._face_verify_run_id = ''
            self._face_verify_epoch = None
            self._face_verify_intent = None
            self._recovery_blocked = ''
            return True

    def _resume_owner_validator(self, instruction: LiveInstruction):
        """把上一条判定下沉到"与在播预留同一个临界区"里执行。

        校验与预留之间只要还隔着一步，"校验通过 → 记录被同名替换 → 预留"
        就会把旧进度接到另一条记录上。返回的判定函数只在任务库事务内运行，
        不做网络 I/O、不等待、不重复登记。
        """
        zone = instruction.zone_name
        exec_day = getattr(instruction, 'execution_date', None)
        task_id = getattr(instruction, 'task_id', None)

        def _validate(row: dict) -> str:
            reason = self._current_owner_rejection(row, zone, exec_day)
            if reason:
                logger.warning('拒绝恢复：%s（task_id=%s）', reason, task_id)
                self._push_backend_event('恢复', 'warning', f'无法恢复：{reason}')
                diagnostics.get_diagnostics().record(
                    'resume_rejected_current_owner', zone=zone,
                    task_id=task_id, reason=reason)
            return reason

        return _validate

    def _named_selection_validator(self, instruction: LiveInstruction):
        """"点哪条开哪条"的冻结复核：与在播预留**同一个** _mutation_lock 临界区。

        ``select_task_instruction`` 与 ``reserve_active_task`` 之间允许任何任务
        CRUD 并发：改名/结算/删除/编辑都可能插进来（监督反例 RENAME 就落在
        这个窗口）。这里把"用户确认那一刻"的版本与业务日，连同 ID/名称/资格
        再核一遍——任何一项不成立都让这次选择作废（受理即拒绝），而不是启动
        一条用户没有确认过的旧分区。返回的判定函数只在任务库锁内运行，不做
        网络 I/O、不等待。
        """
        task_id = getattr(instruction, 'task_id', None)
        zone = instruction.zone_name
        revision = getattr(instruction, 'source_revision', None)
        business_date = getattr(instruction, 'source_business_date', None)

        def _validate(row: dict) -> str:
            tm = getattr(self, 'task_manager', None)
            validate = getattr(tm, 'validate_named_selection', None) \
                if tm is not None else None
            if not callable(validate):
                # 旧任务管理器没有该能力：退回"只确认记录仍存在"（预留语义）。
                return ''
            reason = validate(task_id, zone, revision, business_date, row)
            if reason:
                logger.warning('拒绝已变化的选择：%s（task_id=%s, zone=%s）',
                               reason, task_id, zone)
                diagnostics.get_diagnostics().record(
                    'start_rejected_selection_changed', zone=zone,
                    task_id=task_id, reason=reason)
            return reason

        return _validate

    def _reserved_identity_validator(self, instruction: LiveInstruction):
        """接管时刻的身份复核（防御性）。

        意图登记时的预留已把破坏性写挡在外面，但从预留到接管之间还隔着平台
        等待；这里只复核**预留所保护的那份身份**仍然成立（行在、名称对应、
        仍待执行）。**不**复核全局版本：别的任务被合法编辑推进版本，不该把
        一场已经开播的直播掐掉。失败由调用方收尾关房。
        """
        zone = instruction.zone_name
        task_id = getattr(instruction, 'task_id', None)

        def _validate(row: dict) -> str:
            if not row:
                return 'start_task_missing'
            if str(row.get('zone_name') or '') != str(zone or ''):
                return 'start_task_changed'
            tm = getattr(self, 'task_manager', None)
            if tm is not None and task_id is not None:
                try:
                    task = next(
                        (t for t in tm.tasks
                         if t.id is not None and int(t.id) == int(task_id)),
                        None)
                except (TypeError, ValueError):
                    return 'start_task_missing'
                if task is not None and not task.needs_execution():
                    return 'start_task_not_executable'
            return ''

        return _validate

    def _claim_task_rotation_context(self, instruction, epoch, run_id):
        """仅把自然完成 A 的上下文绑定到同一个 B 指令对象和控制代际。"""
        lock = getattr(self, '_task_rotation_lock', None)
        if lock is None:
            return None
        with lock:
            pending = getattr(self, '_task_rotation_pending', None)
            if (not pending or pending.get('instruction') is not instruction
                    or pending.get('epoch') != epoch):
                return None
            self._task_rotation_pending = None
            pending['b_run_id'] = str(run_id or '')
            pending['timeout_notified'] = False
            pending['result_notified'] = False
            self._task_rotation_context = pending
            return pending

    def _send_task_rotation_result(self, context, result,
                                   late_update=False):
        """把冻结上下文交给有界邮件队列；不做 SMTP/网络 I/O。"""
        # 面板主动停止已向操作者即时确认；旧轮转线程晚到时不得再寄
        # “下一任务已取消”或“开播后停止”邮件。只抑制同一代的取消结果。
        if (result in ('cancelled', 'started_then_stopped')
                and context.get('epoch') == getattr(
                    self, '_last_user_stop_source_epoch', None)):
            return
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender or not hasattr(sender, 'notify_task_rotation'):
                return
            instruction = context.get('instruction')
            next_task = ({
                'zone': getattr(instruction, 'zone_name', ''),
                'duration_seconds': getattr(
                    instruction, 'duration_seconds', None),
            } if instruction is not None else None)
            sender.notify_task_rotation(
                completion=(None if late_update else
                            dict(context.get('completion') or {})),
                next_task=next_task,
                next_result=result,
                verify_url=context.get('verify_url', ''),
                confirm_url=context.get('confirm_url', ''),
                reason=context.get('reason', ''),
                late_update=late_update,
                identity=f"{context.get('identity', '')}:{context.get('b_run_id', '')}")
        except Exception as exc:
            logger.debug('任务轮转邮件通知异常：%s', exc)

    def _notify_task_rotation_paused(self, completion, epoch, reason):
        """A 已新结算、但 B 因所有权或存档闸门未能启动时保留完成结果。"""
        completion = dict(completion or {})
        self._send_task_rotation_result({
            'completion': completion,
            'instruction': None,
            'epoch': epoch,
            'reason': reason,
            'identity': f"{completion.get('task_id', '')}:"
                        f"{completion.get('execution_date', '')}",
        }, 'paused')

    def _task_rotation_timeout(self, context):
        """B 等待视频/网络时有界报一次准备中，不持控制锁或阻塞开播。"""
        lock = getattr(self, '_task_rotation_lock', None)
        if lock is None:
            return
        with lock:
            if (getattr(self, '_task_rotation_context', None) is not context
                    or context.get('result_notified')
                    or context.get('timeout_notified')
                    or not self._is_epoch_current(context.get('epoch'))
                    or context.get('cancel') is None
                    or context['cancel'].is_set()
                    or self._pending_run_id != context.get('b_run_id')):
                return
            context['timeout_notified'] = True
            # Both provisional and final results must enter the same queue in
            # state order. Releasing this lock before enqueue allowed a delayed
            # provisional mail to arrive *after* the final success mail.
            self._send_task_rotation_result(context, 'preparing')

    def _finish_task_rotation_context(self, context, result):
        """收口 B 真实结果；超时后只发 B 更新，A 不会被重复带入。"""
        if not context or not result:
            return
        lock = getattr(self, '_task_rotation_lock', None)
        if lock is None:
            return
        with lock:
            if (getattr(self, '_task_rotation_context', None) is not context
                    or context.get('result_notified')):
                return
            context['result_notified'] = True
            late_update = bool(context.get('timeout_notified'))
            timer = context.get('timer')
            self._task_rotation_context = None
            self._send_task_rotation_result(
                context, result, late_update=late_update)
        if timer is not None:
            timer.cancel()

    def _start_in_background(self, instruction, video_path, is_task_mode,
                              cancel, epoch, start_run_id=None):
        # Freeze this worker's reservation identity at entry. Shared pending fields
        # may belong to a later request by the time this finally block runs.
        start_run_id = str(start_run_id or self._pending_run_id or '')
        start_intent_id = self._start_intent_id
        rotation_context = self._claim_task_rotation_context(
            instruction, epoch, start_run_id)
        rotation_result = None
        rotation_timer = None
        if rotation_context:
            rotation_context['cancel'] = cancel
            rotation_timer = threading.Timer(
                max(0.0, float(getattr(
                    self, '_task_rotation_notice_timeout_seconds', 120))),
                self._task_rotation_timeout, args=(rotation_context,))
            rotation_timer.daemon = True
            rotation_context['timer'] = rotation_timer
            rotation_timer.start()
        try:
            with self.api.retry_network_until_cancelled(cancel):
                mode, auto_open = self._get_stream_settings()
                missing_reported = False
                while not cancel.is_set():
                    if not self._is_epoch_current(epoch):
                        # 停止已经生效：不再等待视频、不再开播。
                        logger.info("后台开播等待期间停止生效，放弃本次请求")
                        break
                    if not video_path and (mode == 'ffmpeg' or auto_open):
                        video_path = self.video_finder.find_video(instruction.zone_name) or ''
                    if mode == 'ffmpeg' and not video_path:
                        if not missing_reported:
                            self._push_backend_event('推流', 'warning', '暂未找到视频，后台每 30 秒检查并继续开播')
                            missing_reported = True
                        cancel.wait(30)
                        continue
                    ok = self._start_streaming_once(
                        instruction, video_path, is_task_mode, epoch)
                    if ok:
                        pending = getattr(self, '_deferred_takeover', None)
                        if (pending is not None
                                and str(pending.get('run_id') or '')
                                    == str(start_run_id)):
                            # FFmpeg 推迟接管：B 尚无"媒体就绪"的可证明事实，
                            # 合并邮件沿用同一门槛——这里不定结论；接管完成时
                            # 由接管例程按 started 收口（幂等），媒体永远未就绪
                            # 又被停止时由 finally 按 cancelled 收口。
                            pass
                        else:
                            rotation_result = 'started'
                    elif self._pending_face_verify:
                        rotation_result = 'verification'
                    elif not cancel.is_set():
                        rotation_result = 'failed'
                    if not ok and not cancel.is_set() and not self._pending_face_verify and not self._recovery_blocked:
                        self._recovery_blocked = '开播未完成，请检查登录状态、分区和日志后重试'
                        self._push_backend_event(
                            '错误', 'danger', self._recovery_blocked,
                            notify=getattr(self, '_start_failure_event_identity', None)
                            != (epoch, start_intent_id))
                    break
        except Exception:
            logger.exception("后台开播异常")
            self._push_backend_event('错误', 'danger', '后台开播异常，请检查日志')
            if not cancel.is_set():
                rotation_result = 'failed'
        finally:
            # Stop and worker completion share this decision point. If Stop lands
            # after the worker's cleanup check, it must see _is_starting=False and
            # launch its own cleanup worker; otherwise it could be accepted in the
            # gap, see _is_starting=True, return early, and leave a live stream on.
            with self._start_mutex():
                cleanup_needed = cancel.is_set() or not self._is_epoch_current(epoch)
                if not cleanup_needed:
                    if not self.is_streaming:
                        self._release_active_task(start_run_id)
                    self._is_starting = False

            if cleanup_needed:
                checkpoint_saved = True
                try:
                    # An in-flight startLive may already have reached the platform:
                    # stop after leaving the retry context, while B remains barred
                    # from taking the start slot.
                    self.stop_monitor.set()
                    self._clear_face_verify_for_stopped_run(
                        start_run_id, epoch)
                    checkpoint_saved = self._stop_live_process(
                        preserve_state=is_task_mode) is not False
                    self.current_instruction = None
                except Exception:
                    logger.exception("取消后台开播时的停播清理失败")
                    checkpoint_saved = False
                finally:
                    with self._start_mutex():
                        if not self.is_streaming:
                            self._release_active_task(start_run_id)
                        self._is_starting = False
                # 面板主动停止的结果留在面板，包含清理未确认。
                # 同一开播 worker 负责收尾时不会再派 LiveStop 线程。
                if (getattr(self, '_last_user_stop_source_epoch', None)
                        == epoch):
                    try:
                        clean = self._cleanup_confirmed()
                        stage = ('pending_recycle' if not clean else
                                 'checkpoint_failed' if not checkpoint_saved
                                 else '')
                        if stage:
                            zone = getattr(instruction, 'zone_name', '') or '未知分区'
                            self._push_backend_event(
                                '停播', 'warning',
                                (f'{zone}进度保存失败，请在面板核对任务进度'
                                 if stage == 'checkpoint_failed' else
                                 f'{zone}停止未确认，请在面板核对状态'),
                                notify=False)
                    except Exception:
                        logger.debug('取消开播后的清理告警异常', exc_info=True)
            # 目标没有确认开播（失败/取消/被停止/重启）：旧进度一直没被动过，
            # 交接结论只能是"未执行"。人脸验证等待中的意图不算终结——用户
            # 确认之后它仍可能开播，届时由接管例程标记为已放弃。
            # 归属核对：结论只写给**本线程自己的那一份**交接（目标 run/epoch
            # 相符）；期间被新确认 supersede 的交接不归这里管。
            # 本线程的推迟接管同理：只有会话没有以本 run 存活（停止/取消/
            # 代际推进）才清记录；正常交给监控等媒体就绪时必须保留。
            session_survived = (self.is_streaming
                                and str(self._current_run_id or '')
                                == start_run_id)
            if not session_survived:
                try:
                    self._discard_deferred_takeover(epoch, start_run_id)
                except Exception:
                    logger.debug('推迟接管记录清理异常', exc_info=True)
                try:
                    if self.handover_pending():
                        awaiting_verify = (bool(self._pending_face_verify)
                                           and str(self._face_verify_run_id
                                                   or '') == start_run_id)
                        if not awaiting_verify:
                            self._finalize_handover(
                                'void', target_run_id=str(start_run_id or ''),
                                epoch=int(epoch or 0))
                except Exception:
                    logger.debug('交接结论写入异常（不影响本次开播结果）',
                                 exc_info=True)
            if rotation_timer is not None:
                rotation_timer.cancel()
            if rotation_context:
                if cleanup_needed or cancel.is_set() or not self._is_epoch_current(epoch):
                    if rotation_result is None:
                        rotation_result = 'cancelled'
                    elif rotation_result == 'started':
                        rotation_result = 'started_then_stopped'
                self._finish_task_rotation_context(
                    rotation_context, rotation_result)
            self._handoff_queued_face_verify_retry(
                epoch, start_run_id=start_run_id)

    def _start_streaming_once(self, instruction: LiveInstruction, video_path: str,
                              is_task_mode: bool = True, epoch: int = None,
                              source: Optional[str] = None,
                              inherit_elapsed: Optional[int] = None) -> bool:
        """开始直播（核心逻辑）
        is_task_mode=True: 任务模式，保存 live_state
        is_task_mode=False: 手动模式，不触碰 live_state、已播时长始终=0、时长=0表示不限时
        epoch: 发起操作时登记的代际；停止生效后该请求必须作废。
        source/inherit_elapsed: 由调用方在意图登记时冻结的会话来源与继承进度。
        """
        if epoch is None:
            epoch = self._control_epoch
        # 来源与继承量优先取调用方显式传入的值；未传入时读**意图登记时**冻结的
        # 实例字段（start_streaming 在入口处写入），保证两步之间不会被改写。
        if source is None:
            source = getattr(self, '_pending_source', SOURCE_NEW)
        if inherit_elapsed is None:
            inherit_elapsed = getattr(self, '_pending_inherit', 0)
        # 交接要求身份同样取**意图登记时**冻结的值（R3）：确认放弃旧进度的
        # 受理把 handover_id 绑定进意图；普通开播/恢复/重连为 None。
        intent_handover_id = getattr(self, '_pending_handover_id', None)
        if not self._is_epoch_current(epoch):
            logger.info("开播前准备阶段停止生效，放弃本次请求")
            return False
        self._stream_mode = 'task' if is_task_mode else 'manual'
        dur_label = '不限时' if (not is_task_mode and instruction.duration_seconds == 0) else f"{instruction.duration_seconds // 60}分钟"
        logger.info("=" * 70)
        logger.info(f"【开播】模式：{self._stream_mode} | 分区：{instruction.zone_name} | 时长：{dur_label}")
        logger.info(f" 视频：{Path(video_path).name}")
        logger.info("=" * 70)

        if source == SOURCE_RESUME and not inherit_elapsed:
            # 直接调用（自动恢复/验证后重试）时也要按身份核对继承量，
            # 不能默认继承 0 秒把用户进度抹掉。
            resolved = self._resolve_resume_inheritance(instruction)
            if resolved is None:
                return False
            inherit_elapsed = resolved

        if not self.current_room_id and self.api.is_logged_in():
            self.login()
        if not self.current_room_id:
            logger.error(" 未登录，请先调用 login()")
            return False

        # 旧监控收到停止后必须退出，才能清除信号启动下一场。
        self.stop_monitor.set()
        while self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=0.2)
            if self._start_cancel.is_set() or not self._is_epoch_current(epoch):
                return False

        # 等待期间可能已经发生停止：每个可等待点之后都必须重新核对代际。
        if not self._is_epoch_current(epoch):
            logger.info("等待旧监控退出期间停止生效，放弃本次开播")
            return False

        # 开播前清理：检测残留 FFmpeg 和直播间状态，先下播再开始
        if not self._pre_start_cleanup(epoch):
            return False
        if not self._is_epoch_current(epoch):
            logger.info("开播前清理之后停止生效，放弃本次开播")
            return False

        if not self.switch_partition(instruction.zone_name):
            logger.error(" 切换分区失败，终止开播")
            return False
        if not self._is_epoch_current(epoch):
            logger.info("切换分区（访问平台）之后停止生效，放弃本次开播")
            return False

        logger.info("开始直播...")
        csrf = self.api.get_csrf()
        if not csrf:
            logger.error(" 未找到 csrf token")
            return False

        area_id = self.area_loader.get_area_id(instruction.zone_name, auto_update=False)
        # 提前保存指令，确保人脸验证失败时 _retry_after_face_verify 可用
        self.current_instruction = instruction

        success, resp = self.api.start_live(self.current_room_id, area_id, csrf)
        if self._start_cancel.is_set() or not self._is_epoch_current(epoch):
            # 旧请求的失败/未知响应同样不能给新意图设阻塞或发错误邮件。
            # 成功响应的补偿下播仍由后台开播收尾负责。
            logger.info("开播响应到达时停止已生效，放弃本次结果")
            return False
        if not success and isinstance(resp, dict) and resp.get('code') in (60024, 60043):
            if self._start_cancel.is_set() or not self._is_epoch_current(epoch):
                # 丢弃停止/接管期间迟到的人脸验证结果，不能复活旧弹窗。
                return False
            # 人脸验证：返回固定 URL 给前端弹窗
            code = resp.get('code')
            logger.warning(f"️ 检测到需要人脸验证 (code={code})")
            uid = self.api.cookies.get('DedeUserID', '')
            verify_url = f"https://www.bilibili.com/blackboard/live/face-auth-middle.html?source_event=400&mid={uid}"
            if not self._set_pending_face_verify(
                    verify_url, run_id=self._pending_run_id, epoch=epoch):
                return False
            logger.info(f"人脸验证 URL：{verify_url}")
            # 事件与通知分工：事件给面板，通知走 send_face_verify（带确认链接）。
            # 旧实现只 push 事件，通知侧把"验证"映射成通用 action_required——
            # **首次开播**要验证时用户根本收不到可点的确认链接。
            self._push_backend_event('验证', 'warning',
                                     f'开播需要人脸验证 (code={code})',
                                     notify=False)
            face_stage = ('resume_blocked' if source == SOURCE_RESUME
                          else 'start_blocked')
            self._notify_verification_required(
                f'开播需要人脸验证 (code={code})', stage=face_stage)
            return False  # API 层检查 _pending_face_verify 返回给前端

        if not success:
            if isinstance(resp, dict) and resp.get('cancelled'):
                return False
            self._handle_platform_failure(resp, context='start')
            response = resp if isinstance(resp, dict) else {}
            logger.error(f" 开播失败：{response.get('msg', '未知错误')}")
            self._start_failure_event_identity = (epoch, self._start_intent_id)
            self._push_backend_event('开播失败', 'danger',
                                     f"开播失败：{response.get('msg', '未知错误')}")
            self.current_instruction = None  # 非人脸验证失败，清除指令
            return False

        if self._start_cancel.is_set() or not self._is_epoch_current(epoch):
            # 平台可能已经接受开播：此处放弃后由 _start_in_background 的收尾补下播。
            logger.info("开播请求在网络返回期间被停止，放弃本次结果")
            return False

        logger.info(" 直播已开始")

        # 从 start_live 返回中提取并本地缓存推流码（同账号推流码恒定不变）
        self._extract_and_cache_rtmp(resp)

        # 推流方式在这里读一次：FFmpeg 模式的"接管"要推迟到本地媒体首次确认
        # 推进（平台开房 ≠ 本地推流就绪）；手动/OBS/视频模式按平台开房接管。
        pusher_mode, auto_open = self._get_stream_settings()
        defer_takeover = bool(pusher_mode == 'ffmpeg' and is_task_mode)
        takeover_failed = False

        # 成功开播重新允许恢复与接受人为停止必须使用同一把锁；
        # 否则晚到的开播结果可把已落盘的 auto_resume=False 覆盖回 True。
        with self._start_mutex():
            if self._start_cancel.is_set() or not self._is_epoch_current(epoch):
                return False
            self.is_streaming = True
            # 进度继承只认"意图登记时冻结的量"：不在这里再读一次共享状态，
            # 避免等待期间被改写后继承到别的任务的进度或莫名归零。
            saved_elapsed = int(inherit_elapsed or 0) if is_task_mode else 0
            self.stream_start_time = datetime.now() - timedelta(seconds=saved_elapsed)
            # 有效计时：confirmed 只继承**已经确认过**的进度；锚点在此刻建立——
            # `start_live` 返回成功本身就是一次可信的"平台在播"观察，所以既不需要
            # 额外探针，也不会补算启动前/离线等待的时间。
            self._segment_monotonic = _monotonic()
            self._local_pusher_required = False
            self._stop_progress_accumulated = saved_elapsed
            clock = self._clock()
            clock.reset(confirmed=saved_elapsed,
                        origin=('resume' if saved_elapsed > 0 else 'live'),
                        credit_limit=effective_credit_limit(
                            getattr(self, '_monitor_interval_seconds',
                                    MONITOR_INTERVAL)))
            self.stop_monitor.clear()
            self._current_source = source
            self._current_run_id = self._pending_run_id or self._new_run_id()
            # "用户新意图优先"（INTENT-01b）：平台房间已开 = 用户"开播"的
            # 新意图已经胜过上一次的停止意图，`.no_auto_resume` 在此清除。
            # 这属于**意图**语义，不属于会话接管——FFmpeg 推迟的只是
            # begin_session/交接/成功通知，不是用户意图的先后。
            try:
                (self.state.state_file.parent / '.no_auto_resume').unlink(
                    missing_ok=True)
            except OSError as exc:
                logger.warning("无法清除人为停止标记，下次重启仍可能需要手动继续：%s",
                               exc)
            if is_task_mode and defer_takeover:
                # 推迟接管窗口：live_state 仍是旧会话（身份/进度原样保留，
                # 旧进度可恢复），但"用户新意图优先"（INTENT-01b）要求停止
                # 意图在此刻让位——auto_resume 跟随最新意图置回 True 并落盘。
                # 这是**意图落盘**，不是会话接管。用户若在窗口内停止，停止
                # 路径会重新写回 False 与 `.no_auto_resume` 标记。
                try:
                    self.state.auto_resume = True
                    self.state.save()
                except Exception as exc:
                    logger.warning('推迟接管窗口内无法更新停止意图：%s', exc)
                # FFmpeg 任务模式：**推迟接管**。平台房间已开，但本地媒体还没
                # 有任何可证明的就绪事实——此刻改写 live_state、结论交接、发
                # 成功邮件，都会在"FFmpeg 根本没推起来"时制造一场假直播：
                # 旧进度被 abandoned、用户收到 [开播]。把接管连同它的全部
                # 后果（交接结论/落盘/成功通知）推迟到媒体首次确认推进；
                # 在那之前旧存档原样保留、成功邮件为零。
                self._deferred_takeover = {
                    'epoch': int(epoch), 'run_id': str(self._current_run_id),
                    'instruction': instruction,
                    'saved_elapsed': int(saved_elapsed or 0),
                    'source': str(source), 'dur_label': str(dur_label),
                    # 本次意图冻结的交接要求身份：接管完成时只结论这一份。
                    'handover_id': intent_handover_id,
                }
            elif is_task_mode:
                if not self._complete_session_takeover(
                        epoch=epoch, run_id=str(self._current_run_id),
                        instruction=instruction, saved_elapsed=saved_elapsed,
                        source=source, is_task_mode=is_task_mode,
                        dur_label=str(dur_label),
                        handover_id=intent_handover_id):
                    takeover_failed = True
        if takeover_failed:
            # 房间已开但会话不能接管（预留复核失败）：立即收尾关房，按失败
            # 返回。交接记录仍未结论，由收尾路径按身份写 void。
            logger.warning('会话接管被拒绝（任务身份已变化），关闭本次开播')
            self._abort_open_after_failed_takeover(
                epoch, str(self._current_run_id or ''), is_task_mode)
            return False

        # 启动视频/FFmpeg（在 start_live 之后，因为 FFmpeg 需要推流码）
        if pusher_mode == 'ffmpeg':
            # 选择本地推流模式时立即关掉仅凭房间在播建立的锚点。
            # 获取推流地址/启动循环都可能失败；若只在循环创建成功后置位，
            # 监控会把“根本没有本地媒体”的房间在播时间照常结算。
            with self._start_mutex():
                if self.is_streaming and self._owns_current_run(
                        epoch, self._current_run_id):
                    self._local_pusher_required = True
                    self._pause_effective_timer(TIMER_PAUSED_PUSHER,
                                                _monotonic())
            if not self._start_ffmpeg_stream(epoch):
                logger.warning("FFmpeg 暂未启动，监控线程将继续恢复")
        elif auto_open and video_path:
            if not self.play_video(video_path):
                logger.warning(" 视频播放失败，但直播已开始")

        # 启动监控线程
        self.monitor_thread = threading.Thread(
            target=self._monitor_streaming,
            name="StreamMonitor",
            daemon=True
        )
        self.monitor_thread.start()

        logger.info(f"  直播时长：{dur_label}")
        return True

    def _get_stream_settings(self) -> Tuple[str, bool]:
        """读取推流设置，返回 (stream_mode, auto_open_video)"""
        try:
            from app.api.settings import load_settings
            s = load_settings()
            return s.stream_mode, s.auto_open_video
        except Exception:
            return "manual", True

    def switch_partition(self, zone_name: str) -> bool:
        """切换直播分区（调用 B站 API）"""
        area_id = self.area_loader.get_area_id(zone_name, auto_update=True)
        if not area_id:
            logger.error(f" 无法获取分区 '{zone_name}' 的 ID")
            return False
        csrf = self.api.get_csrf()
        if not csrf:
            logger.error(" 未找到 csrf token")
            return False
        logger.info(f" 切换分区：{zone_name} (area_id={area_id})")
        success, resp = self.api.update_area(self.current_room_id, area_id, csrf)
        if not success:
            # 启动前的换区失败属于本次开播意图；不能当作在播中恢复失败，
            # 更不能声称已经请求停止本地推流。
            starting = bool(getattr(self, '_is_starting', False)
                            and not self.is_streaming)
            self._handle_platform_failure(
                resp, context='start' if starting else 'control')
            logger.error(f" 切换分区失败：{resp.get('msg', '未知错误')}")
            return False
        logger.info(f" 分区切换成功：{zone_name}")
        return True

    def play_video(self, video_path: str) -> bool:
        """播放视频（服务器版：无图形环境时跳过）"""
        if sys.platform == 'linux' and not os.environ.get('DISPLAY'):
            logger.info("无图形环境，跳过视频播放")
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(video_path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', video_path])
            else:
                subprocess.Popen(['xdg-open', video_path])
            logger.info(f"已用默认程序打开视频：{Path(video_path).name}")
            return True
        except Exception as e:
            logger.error(f"打开视频失败：{e}")
            return False

    def _extract_and_cache_rtmp(self, resp: dict):
        """从 start_live 响应中提取推流码并本地缓存（同账号推流码恒定不变）"""
        try:
            data = resp.get('data', {})
            rtmp_data = data.get('rtmp', {})
            addr = rtmp_data.get('addr', '')
            code = rtmp_data.get('code', '')
            if addr and code:
                # 读取现有缓存
                cache = self._load_rtmp_cache()
                cache[str(self.current_room_id)] = {'rtmp_addr': addr, 'rtmp_code': code}
                self._save_rtmp_cache(cache)
                logger.info(f" 推流码已缓存：{addr[:30]}...")
            else:
                logger.warning(" start_live 返回中未找到 rtmp 数据")
        except Exception as e:
            logger.debug(f" 缓存推流码失败：{e}")

    def _load_rtmp_cache(self) -> dict:
        """加载本地推流码缓存"""
        cache_file = get_data_path("rtmp_cache.json")
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_rtmp_cache(self, cache: dict):
        """保存推流码缓存到本地"""
        try:
            with open(str(get_data_path("rtmp_cache.json")), 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f" 保存推流码缓存失败：{e}")

    def _get_cached_push_url(self) -> Optional[str]:
        """从本地缓存获取推流地址，归一化 URL（消除 /? 等异常格式）"""
        if not self.current_room_id:
            return None
        cache = self._load_rtmp_cache()
        entry = cache.get(str(self.current_room_id))
        if entry and entry.get('rtmp_addr') and entry.get('rtmp_code'):
            addr = entry['rtmp_addr'].rstrip('/')
            code = entry['rtmp_code']
            # 必须在 app 与 playpath 之间保留 '/'，否则原生 FFmpeg 会把整个
            # "live-bvc?streamname=...&key=..." 当成 app，B站握手拒绝 → Input/output error。
            # 统一拼成 rtmp://host/live-bvc/?streamname=...（与 OBS / get_push_url 一致）。
            return f"{addr}/{code}"          # ?开头→ live-bvc/?streamname=...；否则→ live-bvc/key

    def _start_ffmpeg_stream(self, epoch: int = None) -> bool:
        """启动 FFmpeg 推流循环线程（使用缓存的推流码，播放完一个视频自动换下一个）

        epoch：本次启动归属的控制代际（None 表示在入口处取当前代际）。

        本函数内部有多个等待/阻塞边界（等待旧循环 join、网络获取推流地址、
        读取设置等）。**每个边界返回后都必须复核同一个代际**，而不是只检查
        可被下一次开播清除的 ``stop_monitor``：

        - 等待期间受理停止 → 保留旧循环引用与停止信号，既不清理进程也不启动；
        - 旧循环仍未退出 → 保留引用，交由正常恢复处理，不得先覆盖引用再另建；
        - 只有仍持有本代所有权时，才允许清除停止信号、登记所有权并建新循环。

        全程不持控制锁做网络等待（停止入口不会被慢网络卡住）。
        """
        if epoch is None:
            epoch = self._control_epoch

        def _stale() -> bool:
            """本请求是否已失去所有权；代际是主判据（停止信号会被新代清除）。"""
            return (not self._is_epoch_current(epoch)
                    or self.stop_monitor.is_set()
                    or bool(self._recovery_blocked))

        if _stale():
            logger.info("停止或控制代际已推进：不启动 FFmpeg 推流")
            return False
        # 先停止旧循环（防止双线程同时运行，导致 poll() 竞态和文件冲突）
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            logger.info(" 等待旧的 FFmpeg 循环线程退出...")
            self._ffmpeg_stop_event.set()
            self._ffmpeg_loop_thread.join(timeout=5.0)
            if _stale():
                logger.info(" 等待旧循环期间停止已生效：保留引用与停止信号，不清理、不启动")
                return False
            if self._ffmpeg_loop_thread.is_alive():
                logger.warning("旧推流线程仍在退出，保留引用与停止信号，下一次监控再恢复")
                return False
        if _stale():
            return False
        # 清理对象必须是此刻看到的旧进程。等待回收时新代可能已接管，
        # 不得用无身份的 kill 或无条件赋 None 处理新代引用。
        with self._pusher_mutex():
            previous_process = self.video_process
        if previous_process is not None:
            self._kill_ffmpeg(expected_process=previous_process)
        if _stale():
            return False
        with self._pusher_mutex():
            current_process = self.video_process
        if self._process_alive(current_process):
            if _stale():
                return False
            # 旧进程仍存活：不得再起一路同房间推流。
            with self._pusher_mutex():
                if self.video_process is current_process:
                    self._ffmpeg_unrecycled = True
            logger.warning(" 旧 FFmpeg 仍存活，本次不重复启动推流")
            return False
        if _stale():
            return False
        # 从缓存获取推流地址
        push_url = self._get_cached_push_url()
        if not push_url:
            # 缓存没有则尝试 API 获取（网络等待：返回后必须复核代际）
            success, data = self.api.get_push_url(self.current_room_id) if self.current_room_id else (False, {})
            if success and data.get('push_url'):
                push_url = data['push_url']
                logger.info(f" 从 API 获取推流地址：{data.get('rtmp_addr', '')[:30]}...")
            else:
                logger.error(" 无法获取推流地址：缓存和 API 均无数据")
                return False
        else:
            logger.info(f" 使用缓存推流地址：{push_url[:50]}...")
        if _stale():
            logger.info(" 推流地址准备期间停止已生效：不创建新的推流循环")
            return False

        # 读取 ffmpeg 路径
        ffmpeg_exe = 'ffmpeg'
        try:
            from app.api.settings import load_settings
            s = load_settings()
            if s.ffmpeg_path:
                ffmpeg_exe = s.ffmpeg_path
        except Exception:
            pass

        zone_name = self.current_instruction.zone_name if self.current_instruction else ''

        # 诊断开关只在新推流进程启动前生效，不会因改配置而重启当前流。
        diagnostics.get_diagnostics().refresh_from_settings()

        # 最终副作用（清事件之后建循环并登记所有权）前最后一次复核
        if _stale() or not self.is_streaming:
            return False
        with self._start_mutex():
            if _stale() or not self.is_streaming:
                return False
            with self._pusher_mutex():
                if self.video_process is not None:
                    logger.warning("推流进程引用仍由另一代持有，不建立第二个 FFmpeg 循环")
                    return False
            # 停止信号只有在最后一次代际/进程所有权复核后才能清除。
            self._ffmpeg_stop_event.clear()
            self._local_pusher_required = True
            self._pause_effective_timer(TIMER_PAUSED_PUSHER, _monotonic())
            owner_run_id = (getattr(self, '_current_run_id', '')
                            or getattr(self.state, 'run_id', '') or '')
            # 最终所有权复核和线程登记在同一临界区：停止若已受理，
            # 旧意图就不能在锁释放后迟到地创建新的本地推流循环。
            self._ffmpeg_loop_thread = threading.Thread(
                target=self._ffmpeg_loop,
                args=(zone_name, push_url, ffmpeg_exe, epoch, owner_run_id),
                name="FFmpegLoop",
                daemon=True
            )
            self._ffmpeg_loop_thread.start()
        logger.info(f" FFmpeg 推流循环已启动")
        return True

    def _check_concat_compatible(self, video_list: list) -> bool:
        """检查视频列表是否能用 -c copy 拼接（编码参数必须完全一致）。
        返回 True 表示兼容，False 表示存在不一致。（规则见 media_format）"""
        return media_format.check_concat_compatible(video_list)

    def _filter_compatible_videos(self, video_list: list) -> list:
        """过滤出与第一个视频编码兼容的视频列表（规则见 media_format）"""
        return media_format.filter_compatible_videos(video_list)

    @staticmethod
    def _record_recent_ffmpeg_failure(failure_times, now=None,
                                      window_seconds=600):
        """记录异常退出的滚动窗口；慢 EPIPE 也能达到严重提醒门槛。"""
        now = time.monotonic() if now is None else float(now)
        failure_times.append(now)
        cutoff = now - max(0.0, float(window_seconds))
        while failure_times and failure_times[0] < cutoff:
            failure_times.popleft()
        return len(failure_times)

    def _ffmpeg_loop(self, zone_name: str, push_url: str, ffmpeg_exe: str,
                     owner_epoch: Optional[int] = None,
                     owner_run_id: Optional[str] = None):
        """FFmpeg 单进程推流：用 concat 播放列表串联所有视频。
        - 每轮 concat 结束后从 API 刷新推流 URL（B站 stream key 一次性）
        - 用时间戳文件名避免多线程/残留进程文件冲突
        - 用 _ffmpeg_stop_event 可被外部中断"""
        logger.info(f" FFmpeg 单进程推流开始 | 分区：{zone_name}")
        if owner_epoch is None:
            owner_epoch = self._control_epoch
        if owner_run_id is None:
            owner_run_id = (getattr(self, '_current_run_id', '')
                            or getattr(self.state, 'run_id', '') or '')
        rapid_fails = 0
        local_failure_notified = False
        severe_failure_notified = False
        recent_failure_times = collections.deque()
        backoff = 5
        attempt_id = 0
        current_push_url = push_url
        # 本循环拥有的推流代；每个新进程都会登记新的一代。
        generation = self._next_pusher_generation()
        last_fingerprint = None

        def _note_abnormal_exit():
            nonlocal severe_failure_notified
            count = self._record_recent_ffmpeg_failure(recent_failure_times)
            if count >= 3 and not severe_failure_notified:
                severe_failure_notified = True
                self._push_backend_event(
                    '推流', 'danger',
                    f'FFmpeg 在 10 分钟内异常退出 {count} 次，系统仍会继续重试；'
                    '直播间当前状态请以面板为准')

        # 时间戳唯一文件名，写入数据目录（多实例 CWD 相同也不会冲突）
        import uuid as _uuid
        concat_file = get_data_path(f"_ffmpeg_concat_{_uuid.uuid4().hex[:8]}.txt")

        def _refresh_push_url():
            """尝试从缓存/API 刷新推流地址，失败返回 None"""
            url = self._get_cached_push_url()
            if url:
                return url
            if self.current_room_id:
                ok, data = self.api.get_push_url(self.current_room_id)
                if ok and data.get('push_url'):
                    return data['push_url']
            return None

        try:
            while not self._ffmpeg_stop_event.is_set() and not self.stop_monitor.is_set() and self.is_streaming:
                video_list = self._list_zone_videos(zone_name)
                if not video_list:
                    logger.warning(f" FFmpeg 找不到视频文件，{backoff}秒后重试...")
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        break
                    continue

                random.shuffle(video_list)

                # 读取重编码设置
                reencode_enabled = True
                try:
                    from app.api.settings import load_settings
                    reencode_enabled = load_settings().ffmpeg_reencode
                except Exception:
                    pass

                # 检查编码兼容性
                use_reencode = False
                if not self._check_concat_compatible(video_list):
                    if reencode_enabled:
                        logger.info(" 视频编码不一致，使用重编码模式")
                        use_reencode = True
                    else:
                        logger.info(" 视频编码不一致，过滤只保留兼容视频")
                        video_list = self._filter_compatible_videos(video_list)
                        if not video_list:
                            logger.warning(" 过滤后无兼容视频，跳过本轮")
                            if self._ffmpeg_stop_event.wait(timeout=10):
                                break
                            continue

                try:
                    with open(concat_file, 'w', encoding='utf-8') as f:
                        for v in video_list:
                            f.write(f"file '{v}'\n")
                except Exception as e:
                    logger.error(f" 写入 concat 文件失败：{e}")
                    time.sleep(2)
                    continue

                # 播放列表指纹/清单属于旁路采集：诊断关闭时一次都不计算。
                if self._identity_probe_enabled():
                    try:
                        playlist_fingerprint = hashlib.md5(
                            '|'.join(video_list).encode('utf-8')).hexdigest()[:16]
                    except Exception:
                        playlist_fingerprint = 'unknown'
                    playlist_reused = (playlist_fingerprint == last_fingerprint)
                    last_fingerprint = playlist_fingerprint
                else:
                    playlist_fingerprint = ''
                    playlist_reused = False

                # 沿用生产修复分支的媒体参数，不重新引入缓冲/时间戳实验。
                # FLV 输出选项必须在输出 URL 前；否则会被当作 trailing option 忽略。
                # 默认保留 stderr 的 time/speed 进度，关闭诊断也能排查推流。

                if use_reencode:
                    cmd = f'{ffmpeg_exe} -hide_banner -re -f concat -safe 0 -i "{concat_file}" -c:v libx264 -preset veryfast -b:v 6000k -maxrate 8000k -bufsize 12000k -pix_fmt yuv420p -c:a aac -b:a 128k -flvflags +no_duration_filesize -f flv "{current_push_url}"'
                    logger.info(f" 生成 concat 列表：{len(video_list)} 个视频（重编码）")
                else:
                    cmd = f'{ffmpeg_exe} -hide_banner -re -f concat -safe 0 -i "{concat_file}" -c copy -flvflags +no_duration_filesize -f flv "{current_push_url}"'
                    logger.info(f" 生成 concat 列表：{len(video_list)} 个视频")

                ffmpeg_log = get_data_path("ffmpeg.log")
                log_fp = open(str(ffmpeg_log), 'a', encoding='utf-8', errors='replace')

                attempt_id += 1
                generation = self._next_pusher_generation()
                progress_channel = None
                pusher_identity = None
                try:
                    _limit_ffmpeg_log(log_fp)
                    log_fp.write(f"\n=== {datetime.now().isoformat()} | concat {len(video_list)} files ===\n")
                    log_fp.flush()
                    next_log_check = time.monotonic() + 10
                    t_start = time.time()

                    # 硬保护：本代仍持有存活进程时绝不新开第二个。
                    # 异常重试路径若绕过它，就会出现两个同房间推流。
                    if self._process_alive(self.video_process):
                        logger.warning("上一路推流进程仍存活，先回收，不启动第二个进程")
                        self._kill_ffmpeg()
                        if self._process_alive(self.video_process):
                            self._ffmpeg_unrecycled = True
                            logger.error("推流进程未能回收，停止本循环以避免并发推流")
                            log_fp.close()
                            return

                    # Capture the byte boundary before this attempt starts.
                    # Older time= lines from a previous concat must never
                    # authorize new task time. Failure keeps FFmpeg running,
                    # but this attempt cannot earn time without local output
                    # evidence until a trustworthy reader is available.
                    try:
                        media_reader = FFmpegLogProgressReader(
                            ffmpeg_log,
                            capture_ffmpeg_log_baseline(ffmpeg_log))
                    except Exception as exc:
                        media_reader = None
                        logger.warning('无法建立本轮 FFmpeg 媒体进度基线：%s', exc)

                    # 诊断默认关闭：关闭时不添加任何进度参数，stdout 保持原有重定向。
                    progress_args, _ = diagnostics.get_diagnostics().progress_args()
                    if progress_args:
                        # 全局诊断选项放在输入前，保证最后一个参数始终是媒体输出。
                        cmd = f'{ffmpeg_exe} {" ".join(progress_args)}{cmd[len(ffmpeg_exe):]}'
                        proc = subprocess.Popen(
                            cmd, shell=True,
                            stdout=subprocess.PIPE, stderr=log_fp,
                            start_new_session=True,  # 创建独立进程组，便于精确清理
                        )
                    else:
                        proc = subprocess.Popen(
                            cmd, shell=True,
                            stdout=log_fp, stderr=subprocess.STDOUT,
                            start_new_session=True,  # 创建独立进程组，便于精确清理
                        )
                    # ==== 所有权从这一行开始：直到确认退出，本代拥有该进程 ====
                    # 其后的任何旁路采集失败都不允许逃出这段所有权。
                    self._claim_pusher(
                        generation, proc, progress_reader=media_reader,
                        require_progress=True)
                    self.ffmpeg_current_video = f"concat({len(video_list)}个)"
                    logger.info(f" FFmpeg concat 进程已启动 (PID={proc.pid})")

                    # 1) 先建立管道消费者（关键路径），再考虑任何旁路采集。
                    if progress_args:
                        try:
                            progress_channel = diagnostics.get_diagnostics().attach_progress(
                                proc, zone=zone_name, attempt=attempt_id,
                                push_url=current_push_url)
                        except Exception as exc:
                            logger.debug(f" 附加进度读取器失败（不影响推流）：{exc}")
                            progress_channel = None
                        progress_channel = self._ensure_progress_consumer(
                            proc, progress_channel, generation,
                            zone=zone_name, attempt=attempt_id)

                    # 2) 旁路身份采集/事件记录：全部隔离，失败只记账。
                    if self._identity_probe_enabled():
                        try:
                            pusher_identity = diagnostics.resolve_pusher_identity(proc.pid)
                            diagnostics.get_diagnostics().note_pusher_start(
                                zone=zone_name, attempt=attempt_id, generation=generation,
                                launcher_pid=proc.pid,
                                ffmpeg_pid=pusher_identity.get('ffmpeg_pid', 'unknown'),
                                pid_resolution=pusher_identity.get('resolution', 'unknown'),
                                playlist_size=len(video_list),
                                playlist_fingerprint=playlist_fingerprint,
                                playlist_reused=playlist_reused,
                                reencode=use_reencode, push_url=current_push_url)
                            # S7：指纹只能证明“列表变过”，清单才能还原顺序。
                            diagnostics.get_diagnostics().note_playlist(
                                playlist_fingerprint, video_list,
                                generation=generation, attempt=attempt_id)
                        except Exception as exc:
                            # 旁路诊断失败绝不进入外层重试分支：
                            # 否则会 Popen 第二个进程并覆盖仍在运行的引用。
                            self._record_bypass_failure('pusher_identity', exc)

                    # 输入时长探测已彻底删除（收口复核）：监控循环只负责
                    # 短观察、退出检测与必要控制；任何诊断数据采集都不得
                    # 进入推流控制路径。

                    # 监控进程，使用本地变量 proc 避免与 _kill_ffmpeg 竞态
                    while proc.poll() is None:
                        if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set() or not self.is_streaming:
                            self._kill_ffmpeg()
                            log_fp.close()
                            logger.info(" FFmpeg 被中断")
                            return
                        # 存活子进程的管道必须始终有人消费：读取器退出/创建失败
                        # 时把排空责任交接出去（进程已退出则不重复接管）。
                        if progress_args:
                            progress_channel = self._ensure_progress_consumer(
                                proc, progress_channel, generation,
                                zone=zone_name, attempt=attempt_id)
                        if time.monotonic() >= next_log_check:
                            try:
                                _limit_ffmpeg_log(log_fp)
                            except OSError as e:
                                logger.warning(f"FFmpeg 日志限额维护失败：{e}")
                            next_log_check = time.monotonic() + 10
                        time.sleep(1)

                    exit_code = proc.returncode
                    elapsed = time.time() - t_start
                    self._pause_for_local_pusher_gap(owner_epoch, owner_run_id)
                    self._release_pusher(generation)
                    diagnostics.get_diagnostics().record(
                        'ffmpeg_exit', zone=zone_name, attempt=attempt_id,
                        generation=generation, exit_code=exit_code,
                        elapsed_seconds=round(elapsed, 3),
                        launcher_pid=proc.pid,
                        ffmpeg_pid=(pusher_identity or {}).get('ffmpeg_pid', 'unknown'),
                        push_url=current_push_url)

                    # 如果是被外部主动终止的（任务完成/停播），静默退出
                    if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set() or not self.is_streaming:
                        logger.info("FFmpeg 已随直播停止而终止")
                        return

                    # 长会话（> 30 秒）：非零退出码说明是被掐断，不能算正常完成
                    if elapsed > 30:
                        if exit_code == 0:
                            logger.info(f" concat 播放完毕 ({elapsed:.0f}s)，重新洗牌...")
                            rapid_fails = 0
                            # 播放列表正常结束只作为下一轮本地故障的合并边界；
                            # 不据此宣称平台直播间已恢复或画面正常。
                            local_failure_notified = False
                            severe_failure_notified = False
                            recent_failure_times.clear()
                            backoff = 5
                            continue
                        else:
                            # 长会话但异常退出（如 B站 RTMP 超时掐断 / 编码切换失败）
                            logger.warning(f" concat 异常退出 ({elapsed:.0f}s, exit={exit_code})，可能是编码不一致或服务端掐断")
                            _note_abnormal_exit()
                            self._push_backend_event(
                                '推流', 'warning',
                                f'concat 异常退出 ({elapsed:.0f}s, exit={exit_code})，重试...',
                                notify=not local_failure_notified)
                            local_failure_notified = True
                            # 不重置 rapid_fails=0，但不退出；下次循环会重新洗牌+检查编码
                            rapid_fails = max(0, rapid_fails - 1)
                            backoff = 5
                            if self._ffmpeg_stop_event.wait(timeout=backoff):
                                return
                            continue

                    # 快速失败（≤ 30 秒）
                    rapid_fails += 1
                    _note_abnormal_exit()
                    # Windows: 判断是否 WSAECONNABORTED（-10053 = 4294957243）
                    is_aborted = False
                    if sys.platform == 'win32':
                        is_aborted = (exit_code == 4294957243 or
                                      (exit_code > 2**31 and exit_code - 2**32 == -10053))
                    if is_aborted:
                        logger.warning(f"FFmpeg 连接被拒 (WSAECONNABORTED)，退避重试")
                        rapid_fails = max(0, rapid_fails - 1)
                        backoff = min(10 * (2 ** rapid_fails), 120) + random.uniform(0, 5)
                        logger.info(f"退避 {backoff:.1f} 秒后重试...")
                        if self._ffmpeg_stop_event.wait(timeout=backoff):
                            return
                        continue

                    logger.warning(f" FFmpeg 快速退出 ({elapsed:.1f}s, exit={exit_code})，第 {rapid_fails} 次")
                    self._push_backend_event(
                        '推流', 'warning',
                        f'FFmpeg 快速退出 (exit={exit_code})，第{rapid_fails}次重试',
                        notify=not local_failure_notified)
                    local_failure_notified = True
                    if rapid_fails >= 5:
                        # 不退出了，改为长间隔重试（可能是 B站临时抽风）
                        logger.warning(f" FFmpeg 连续 {rapid_fails} 次快速失败，切换长间隔重试模式（每5分钟一次）")
                        send_severe_notice = not severe_failure_notified
                        self._push_backend_event(
                            '推流', 'danger' if send_severe_notice else 'warning',
                            f'FFmpeg 连续{rapid_fails}次失败，切换长间隔重试（每5分钟）',
                            notify=send_severe_notice)
                        severe_failure_notified = True
                        backoff = 300 + random.uniform(0, 30)  # 5分钟 + 随机抖动
                    else:
                        backoff = min(5 * (2 ** (rapid_fails - 1)), 60) + random.uniform(0, 3)
                    logger.info(f" 退避 {backoff:.1f} 秒后重试...")
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        return

                except FileNotFoundError:
                    self._pause_for_local_pusher_gap(owner_epoch, owner_run_id)
                    log_fp.close()
                    logger.error(f" 未找到 ffmpeg：{ffmpeg_exe}")
                    return
                except Exception as e:
                    self._pause_for_local_pusher_gap(owner_epoch, owner_run_id)
                    log_fp.close()
                    logger.error(f" FFmpeg 异常：{e}")
                    if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set():
                        return
                    # 本代进程仍存活：必须先回收，否则下一轮 Popen 会产生
                    # 第二个同房间的存活进程（引用被覆盖后旧进程无人拥有）。
                    if self._process_alive(self.video_process):
                        self._kill_ffmpeg()
                        if self._process_alive(self.video_process):
                            self._ffmpeg_unrecycled = True
                            logger.error("推流进程未能回收，停止本循环以避免并发推流")
                            return
                    rapid_fails += 1
                    _note_abnormal_exit()
                    backoff = min(5 * (2 ** min(rapid_fails, 6)), 300)
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        return
                finally:
                    # 只在子进程结束后收尾读取器；读取器故障不影响本循环。
                    try:
                        diagnostics.get_diagnostics().detach_progress(progress_channel)
                    except Exception as exc:
                        logger.debug(f" 关闭进度读取器异常：{exc}")
                    for drainer in list(self._local_drainer_list()):
                        if drainer.generation == generation and drainer.exited.is_set():
                            self._local_drainer_list().remove(drainer)
                    # 每次尝试都要释放日志句柄。正常播放完毕/断线重试走上面的
                    # continue，不能只依赖异常或主动停止分支中的 close()。
                    try:
                        log_fp.close()
                    except OSError as exc:
                        logger.debug(f" 关闭 FFmpeg 日志异常：{exc}")
        finally:
            # 只释放仍归属本代的进程引用；未确认退出的进程保留引用，
            # 避免“循环 finally 清空引用”绕过 _kill_ffmpeg 的保护。
            self._release_pusher(generation)
            try:
                if concat_file.exists():
                    concat_file.unlink(missing_ok=True)
            except (PermissionError, OSError):
                try:
                    time.sleep(1)
                    concat_file.unlink(missing_ok=True)
                except Exception:
                    pass

        logger.info(" FFmpeg 循环线程退出")

    def _identity_probe_enabled(self) -> bool:
        """旁路身份采集是否开启。默认关闭时任何探测/指纹都不计算。"""
        try:
            return bool(diagnostics.get_diagnostics().identity_probe_enabled())
        except Exception:
            return False

    def _record_bypass_failure(self, stage: str, exc: BaseException) -> None:
        """旁路诊断自身失败：只记账，绝不传播到推流控制流。"""
        logger.debug("旁路诊断失败（%s，不影响推流）：%s", stage, exc)
        try:
            diagnostics.get_diagnostics().record(
                'diagnostic_bypass_failure', stage=stage,
                error_type=type(exc).__name__)
        except Exception:
            pass

    def _local_drainer_list(self) -> list:
        drainers = self.__dict__.get('_local_drainers')
        if drainers is None:
            drainers = []
            self._local_drainers = drainers
        return drainers

    # 同一代的交接尝试最小间隔：限制的是**重试频率**与并发消费者数量，
    # 不是恢复次数——管道恢复可读后总能重新接上，不会永久锁死。
    HANDOVER_MIN_INTERVAL = 0.25
    _DRAIN_ATTEMPT_HISTORY = 8

    @staticmethod
    def _consumer_state(channel) -> str:
        """消费者所有权状态：consuming / eof / failed / unrecoverable / none。"""
        if channel is None:
            return 'none'
        try:
            if channel.is_alive() and not channel.exited.is_set():
                return 'consuming'
            if getattr(channel, 'unrecoverable', False):
                return 'unrecoverable'
            if getattr(channel, 'read_errors', 0):
                return 'failed'
            return 'eof'
        except Exception:
            return 'none'

    def _retire_drain_owner(self) -> None:
        """旧消费者让位：通知停止并从本地列表摘除，避免双读者。"""
        owner = self.__dict__.get('_drain_owner')
        for candidate in (owner,):
            stop = getattr(candidate, 'stop', None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        drainers = self._local_drainer_list()
        if owner is not None and owner in drainers:
            drainers.remove(owner)

    def _ensure_progress_consumer(self, proc, channel, generation: int, **identity):
        """保证存活子进程的进度管道始终有消费者（读取器故障后的责任交接）。

        判定依据是“**存活子进程的管道有没有有效消费者**”，而不是“线程是否还在”：

        - 消费者状态为 consuming → 复用，不动作；
        - 状态为 eof → 子进程大概率也在退出，交给进程监控，不重复接管；
        - 状态为 failed / unrecoverable / none（创建失败）→ 限频重新交接；
          交接失败（诊断不可用 + 本地创建失败）也不登记“已完成”，下个周期再试。

        限频只约束尝试节奏（避免忙循环），**不限总次数**：兜底消费者退出后
        只要管道恢复可读，就必须有人接手。
        """
        if not self._process_alive(proc):
            return channel
        state = self._consumer_state(channel)
        if state == 'consuming':
            return channel
        if state == 'eof':
            # 消费者正常读到 EOF：管道写端已关闭，没有数据会再来；
            # 不反复重建空转的消费者，子进程退出由进程监控负责。
            return channel
        stream = getattr(proc, 'stdout', None)
        if stream is None:
            return channel

        # 限频：同一代的交接尝试至少间隔 HANDOVER_MIN_INTERVAL 秒。
        now = time.monotonic()
        history = self.__dict__.setdefault('_drain_attempt_at', {})
        last_attempt = history.get(generation)
        if (last_attempt is not None
                and now - last_attempt < self.HANDOVER_MIN_INTERVAL):
            # 窗口内不重复重建；返回当前消费者（可能是已死的），调用方下轮再试。
            return channel
        history[generation] = now
        if len(history) > self._DRAIN_ATTEMPT_HISTORY:
            for stale_gen in sorted(history)[:-self._DRAIN_ATTEMPT_HISTORY]:
                history.pop(stale_gen, None)

        reason = {
            'none': 'reader_creation_failed',
            'failed': 'reader_exited',
            'unrecoverable': 'reader_unrecoverable',
        }.get(state, 'reader_exited')

        # 旧消费者让位（若它还卡在退避里，stop 会让它尽快退出，避免双读）。
        self._retire_drain_owner()
        # 回收已确认退出的旧通道：反复接管时 _channels 不能无界积累（U2）。
        # 仍未退出的保留在活跃集合，由后续回收或 shutdown 处理。
        try:
            diagnostics.get_diagnostics().reap_exited_channels()
        except Exception:
            pass
        # 本地兜底排空线程同样只保留存活的，退出即摘除。
        drainers = self._local_drainer_list()
        self.__dict__['_local_drainers'] = [d for d in drainers if d.is_alive()]

        drained = None
        used_local = False
        try:
            drained = diagnostics.get_diagnostics().attach_drain_only(proc, **identity)
        except Exception as exc:
            self._record_bypass_failure('attach_drain_only', exc)
        if drained is None:
            # 诊断模块不可用：控制器自己承担排空责任，绝不留下无人消费的管道。
            try:
                drained = _PipeDrainer(stream, generation)
                drained.start()
                self._local_drainer_list().append(drained)
                used_local = True
            except Exception as exc:
                self._record_bypass_failure('local_pipe_drainer', exc)
                drained = None
                # 两条兜底都失败：不登记所有权，下个周期继续尝试。
                return channel
        self._drain_owner_generation = generation
        self._drain_owner = drained
        logger.warning("进度管道消费者交接（原因：%s，实现：%s）",
                       reason, 'controller' if used_local else 'diagnostics')
        try:
            diagnostics.get_diagnostics().record(
                'progress_consumer_handover', generation=generation,
                reason=reason, state_before=state,
                owner=('controller' if used_local else 'diagnostics'))
        except Exception:
            pass
        return drained

    def _list_zone_videos(self, zone_name: str) -> List[str]:
        """列出分区文件夹下所有视频文件的绝对路径（规则见 media_format）。"""
        return media_format.list_zone_videos(
            VIDEO_BASE_PATH, DEFAULT_VIDEO_FOLDER, zone_name)

    @staticmethod
    def _process_alive(process) -> bool:
        try:
            return process is not None and process.poll() is None
        except Exception:
            return False

    @staticmethod
    def _wait_for_exit(process, timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return not LiveController._process_alive(process)

    def _terminate_ffmpeg(self, process):
        """尽力终止进程组；平台相关的实现差异收敛在这里。"""
        pid = process.pid
        if sys.platform == 'win32':
            subprocess.run(
                f'taskkill /F /T /PID {pid}',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10
            )
        else:
            # 使用进程组 kill：start_new_session=True 保证 PGID = PID
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass  # 进程已退出

    def _next_pusher_generation(self) -> int:
        """登记新一代推流进程：旧循环不得动用新进程的引用。"""
        with self._pusher_mutex():
            self._pusher_generation += 1
            return self._pusher_generation

    def _claim_pusher(self, generation: int, process,
                      progress_reader=None, require_progress: bool = False) -> None:
        """登记本代拥有的推流进程（Popen 成功后的所有权起点）。

        代际与进程对象成对记录：`_kill_ffmpeg` 只有在“引用仍是同一个对象、
        且代际与登记时一致”时才允许清除，旧清理不会覆盖新代的引用。
        """
        with self._pusher_mutex():
            self._pusher_generation = int(generation)
            self.video_process = process
            self._video_process_generation = int(generation)
            self._pusher_progress_required = bool(require_progress)
            self._pusher_media_reader = progress_reader
            self._pusher_media_generation = int(generation)
            self._pusher_media_seconds = None

    def _pusher_owner_generation(self) -> int:
        return self.__dict__.get('_video_process_generation', -1)

    def _release_pusher(self, generation: int):
        """只在本代进程仍归属自己且已确认退出时才清除引用。

        终止失败或退出延迟时保留引用并标记 _ffmpeg_unrecycled，
        避免“以为清理成功”后另起一路同房间推流。
        """
        with self._pusher_mutex():
            if generation != self._pusher_generation:
                # 已有更新的推流代：旧循环不能清除它的引用。
                return
            if self._process_alive(self.video_process):
                self._ffmpeg_unrecycled = True
                logger.warning("推流进程仍未确认退出：保留引用，不清除")
                return
            self.video_process = None
            self._video_process_generation = -1
            self._pusher_progress_required = False
            self._pusher_media_reader = None
            self._pusher_media_generation = -1
            self._pusher_media_seconds = None
            self.ffmpeg_current_video = ''
            self._ffmpeg_unrecycled = False

    def _kill_ffmpeg(self, timeout: float = 5.0, expected_process=None):
        """强制终止当前 FFmpeg 进程（使用进程组精确清理，不影响其他实例）。

        只有确认进程已经退出、且引用仍属于同一个进程对象（代际一致）才清除引用。
        终止失败或退出延迟时保留引用并标记 _ffmpeg_unrecycled，
        避免“以为清理成功”后另起一路同房间推流。
        """
        # 迟到的恢复拒绝只能回收受理时拥有的进程，不能误杀新会话。
        with self._pusher_mutex():
            process = self.video_process
            if expected_process is not None and process is not expected_process:
                return
            generation_at_entry = self.__dict__.get(
                '_video_process_generation', -1)
        if process is None:
            return
        deadline = time.monotonic() + max(0.0, float(timeout))

        def remaining_wait() -> float:
            return max(0.0, deadline - time.monotonic())

        def _clear_owned() -> bool:
            """仅当引用未被新代替换时才清除。"""
            with self._pusher_mutex():
                if self.video_process is not process:
                    logger.warning("推流引用已被新的一代替换，旧清理不覆盖")
                    return False
                if self.__dict__.get('_video_process_generation', -1) != generation_at_entry:
                    logger.warning("推流代际已推进，旧清理不覆盖新代引用")
                    return False
                self.video_process = None
                self._video_process_generation = -1
                self._pusher_progress_required = False
                self._pusher_media_reader = None
                self._pusher_media_generation = -1
                self._pusher_media_seconds = None
                self._ffmpeg_unrecycled = False
                return True

        try:
            self._terminate_ffmpeg(process)
            if self._wait_for_exit(process, remaining_wait()):
                if _clear_owned():
                    logger.info("FFmpeg 进程已终止")
                return
            # 仍未退出：升级为强制终止并再确认一次
            logger.warning("FFmpeg 未在超时内退出，升级强制终止")
            if sys.platform != 'win32':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if self._wait_for_exit(process, remaining_wait()):
                if _clear_owned():
                    logger.info("FFmpeg 进程已强制终止")
                return
            self._ffmpeg_unrecycled = True
            logger.warning("FFmpeg 进程仍存活：保留引用，不会另起同房间推流")
        except Exception as e:
            logger.debug(f"终止 FFmpeg 异常：{e}")
            if self._wait_for_exit(process, remaining_wait()):
                _clear_owned()
            else:
                self._ffmpeg_unrecycled = True
                logger.warning("终止 FFmpeg 失败且进程仍存活：保留引用")

    def _verification_owner_mismatch(self, run_id=None, room_id=None,
                                     epoch=None) -> str:
        """确认链接绑定的身份是否仍属当前会话；返回不匹配原因（'' = 匹配）。

        "这个 token 还有效"与"这个 token 仍然属于当前这场"是两件事：
        token 消费成功之后、清除验证阻塞之前，可能已经发生停止 + 新开播。
        """
        if epoch is not None:
            try:
                if int(epoch) != int(self._control_epoch):
                    return (f'控制代际已变化（确认链接={epoch}，'
                            f'当前={self._control_epoch}）')
            except (TypeError, ValueError):
                return '控制代际不可比对'
        if room_id is not None and self.current_room_id is not None:
            try:
                if int(room_id) != int(self.current_room_id):
                    return (f'房间号已变化（确认链接={room_id}，'
                            f'当前={self.current_room_id}）')
            except (TypeError, ValueError):
                return '房间号不可比对'
        if run_id:
            pending_face = bool(getattr(self, '_pending_face_verify', False))
            current = (getattr(self, '_face_verify_run_id', '')
                       if pending_face else '')
            current = (current or self._pending_run_id or self._current_run_id
                       or getattr(self.state, 'run_id', '') or '')
            if current and current != run_id:
                return f'会话身份已变化（确认链接属于 {run_id}，当前为 {current}）'
        if bool(getattr(self, '_pending_face_verify', False)):
            face_run = str(getattr(self, '_face_verify_run_id', '') or '')
            face_epoch = getattr(self, '_face_verify_epoch', None)
            # 兼容未持有专属 owner 字段的旧 controller/test doubles：旧冻结身份
            # 仍须匹配 pending/current run 上下文，不能绕过验证会话隔离。
            if not face_run:
                face_run = str(getattr(self, '_pending_run_id', '')
                                or getattr(self, '_current_run_id', '')
                                or getattr(self.state, 'run_id', '') or '')
            if run_id and face_run != str(run_id):
                return '人脸验证意图已由另一场直播接管'
            if epoch is not None and face_epoch is not None \
                    and int(epoch) != int(face_epoch):
                return '人脸验证意图代际已变化'
        return ''

    def _consume_face_verify_intent_locked(self, run_id=None, room_id=None,
                                           epoch=None) -> Optional[dict]:
        """锁内核验并冻结完整启动意图，再消费当前 face pending。"""
        if not bool(getattr(self, '_pending_face_verify', False)):
            return None
        mismatch = self._verification_owner_mismatch(run_id, room_id, epoch)
        if mismatch:
            logger.warning(f"忽略过期的人脸验证确认：{mismatch}")
            diagnostics.get_diagnostics().record(
                'face_verify_confirm_stale', reason=mismatch,
                run_id=run_id or '', epoch=epoch)
            return None
        if self.stop_monitor.is_set() or self._start_cancel.is_set():
            logger.info("已请求停止，忽略人脸验证确认")
            return None
        try:
            frozen_room = int(self.current_room_id)
        except (TypeError, ValueError):
            frozen_room = 0
        if frozen_room <= 0:
            logger.warning("人脸验证确认缺少有效直播间身份，拒绝消费")
            return None
        instruction = self.current_instruction
        if instruction is None:
            logger.warning("人脸验证意图没有冻结的开播指令，拒绝确认")
            return None
        face_run = (str(getattr(self, '_face_verify_run_id', '') or '')
                    or str(self._pending_run_id or self._current_run_id or ''))
        face_epoch = getattr(self, '_face_verify_epoch', None)
        if face_epoch is None:
            face_epoch = self._control_epoch
        frozen = getattr(self, '_face_verify_intent', None) or {}
        instruction_data = frozen.get('instruction')
        if not instruction_data:
            instruction_data = (instruction.dict() if hasattr(instruction, 'dict')
                                else dict(instruction))
        if (instruction_data.get('zone_name') != instruction.zone_name
                or instruction_data.get('task_id') != instruction.task_id
                or instruction_data.get('execution_date') != instruction.execution_date
                or instruction_data.get('duration_seconds') != instruction.duration_seconds):
            logger.warning("人脸验证等待期间原始开播指令已变化，拒绝确认")
            return None
        source = frozen.get('source') or getattr(self, '_pending_source', '') \
            or getattr(self, '_current_source', '') or SOURCE_NEW
        mode = frozen.get('mode') or (
            'manual' if (self._stream_mode == 'manual'
                         or self._verify_retry_mode == 'manual') else 'task')
        intent = {
            'run_id': face_run,
            'room_id': frozen_room,
            'epoch': int(face_epoch),
            'mode': mode,
            'source': source,
            'inherit_elapsed': int(frozen.get(
                'inherit_elapsed', getattr(self, '_pending_inherit', 0)) or 0),
            'instruction': instruction_data,
            # R4：交接确认身份随冻结意图走。登记时已核"记录仍 pending 且
            # 目标 run 与本意图一致"；重试时还要按这个 id 精确重绑定并
            # 持久写成功（start_streaming 的 expected_handover_id）。
            'handover_id': (frozen.get('handover_id')
                            if frozen else
                            self._active_handover_requirement_locked(face_run)),
        }
        self._pending_face_verify = False
        self._face_verify_url = ''
        self._face_verify_run_id = ''
        self._face_verify_epoch = None
        self._face_verify_intent = None
        self._recovery_blocked = ''
        logger.info(" 人脸验证状态已核验并消费；启动意图已冻结")
        return intent

    def consume_face_verify_intent(self, run_id=None, room_id=None,
                                   epoch=None) -> Optional[dict]:
        """以 face owner/run/room/epoch 为条件原子消费，并返回冻结意图。"""
        with self._start_mutex():
            return self._consume_face_verify_intent_locked(
                run_id=run_id, room_id=room_id, epoch=epoch)

    def _face_verify_retry_current_locked(self, intent: dict) -> bool:
        try:
            epoch = int(intent.get('epoch'))
        except (TypeError, ValueError):
            return False
        face_run = str(intent.get('run_id') or '')
        try:
            room_matches = (face_run and int(intent.get('room_id') or 0) > 0
                            and int(intent.get('room_id'))
                            == int(self.current_room_id or 0))
        except (TypeError, ValueError):
            room_matches = False
        return bool(epoch == self._control_epoch and face_run
                    and self._pending_run_id == face_run and room_matches
                    and not self.stop_monitor.is_set()
                    and not self._start_cancel.is_set())

    def schedule_face_verify_retry(self, intent: dict) -> bool:
        """调度冻结重试；原 start 尚在通知/收尾时由其 finally 接手。"""
        frozen = dict(intent or {})
        frozen.setdefault('queued_at_monotonic', time.monotonic())
        with self._start_mutex():
            if not self._face_verify_retry_current_locked(frozen):
                return False
            if self.is_streaming:
                # 状态查询型验证可能发生在直播已经恢复后；确认有效，但不重复开播。
                return True
            if self._is_starting:
                self._queued_face_verify_retry_intent = frozen
                return True
        self._start_face_verify_retry_worker(frozen)
        return True

    def _start_face_verify_retry_worker(self, intent: dict) -> None:
        frozen = dict(intent or {})
        threading.Thread(target=self._retry_frozen_face_verify_intent,
                         args=(frozen,), name='FaceVerifyRetry',
                         daemon=True).start()

    def _handoff_queued_face_verify_retry(self, ended_epoch: int,
                                          start_run_id: str = '') -> None:
        """只由同一 run 的 start worker 消费其排队确认，避免串场丢失。"""
        intent = None
        with self._start_mutex():
            queued = self._queued_face_verify_retry_intent
            if not queued:
                return
            queued_run = str(queued.get('run_id') or '')
            worker_run = str(start_run_id or '')
            # A 的 finally 可能与 B 的 start/确认交错。epoch 有时相同，
            # 因此必须再核 run；不属于本 worker 的队列原样留给 B finally。
            if not worker_run or queued_run != worker_run:
                return
            try:
                queued_epoch = int(queued.get('epoch'))
            except (TypeError, ValueError):
                queued_epoch = -1
            if queued_epoch != int(ended_epoch):
                return
            # 不得在另一个 start 仍占 slot 时取走唯一确认；该 start 的 finally
            # 会用自己的 run/epoch 再次尝试交接。
            if self._is_starting:
                return
            self._queued_face_verify_retry_intent = None
            if (not self.is_streaming
                    and self._face_verify_retry_current_locked(queued)):
                intent = queued
        if intent:
            self._start_face_verify_retry_worker(intent)

    def confirm_face_verify_and_retry(self, run_id=None, room_id=None,
                                      epoch=None) -> bool:
        """面板单次受理：锁内确认/冻结，随后只按原 epoch 派发一次重试。"""
        intent = self.consume_face_verify_intent(
            run_id=run_id, room_id=room_id, epoch=epoch)
        if not intent:
            return False
        # 停止可能恰好落在 consume 与 schedule 之间；该情况下按停止结果拒绝，
        # 不能把“pending 已清”误报成已排队重试。
        return bool(self.schedule_face_verify_retry(intent))

    def _retry_frozen_face_verify_intent(self, intent: dict) -> None:
        """按确认时的指令/来源/进度异步重试，并复核原控制代际。"""
        intent = dict(intent or {})
        delay = max(0.0, float(intent.get('delay', 0) or 0))
        if delay:
            time.sleep(delay)
        epoch = intent.get('epoch')
        face_run = str(intent.get('run_id') or '')
        face_room = intent.get('room_id')
        instruction_data = intent.get('instruction')
        try:
            instruction = LiveInstruction(**instruction_data)
        except Exception:
            logger.warning("冻结的人脸验证指令无效，拒绝重试")
            return
        # 调用方可能是旧邮件 worker/测试直接调用；遇到原 start 尚未收尾时，
        # 转交 queued slot。原 _start_in_background finally 会在相同 epoch 交接。
        with self._start_mutex():
            if not self._face_verify_retry_current_locked(intent):
                logger.info("人脸确认意图已停止或被接管，丢弃本次重试")
                return
            if self._is_starting:
                self._queued_face_verify_retry_intent = intent
                return
            if self.is_streaming:
                logger.info("直播仍在播，确认仅解除验证提示，不重复开播")
                return
            try:
                room_matches = (face_room is not None and int(face_room) > 0
                                and int(face_room) == int(self.current_room_id or 0))
            except (TypeError, ValueError):
                room_matches = False
            if not room_matches:
                logger.info("人脸确认意图的直播间已变化，丢弃本次重试")
                return
            current = self.current_instruction
            if (not current or current.zone_name != instruction.zone_name
                    or current.task_id != instruction.task_id
                    or current.execution_date != instruction.execution_date
                    or current.duration_seconds != instruction.duration_seconds):
                logger.info("人脸确认原始指令已变化，丢弃本次重试")
                return
        is_task_mode = intent.get('mode') != 'manual'
        # R4：确认放弃旧进度的意图在登记/消费时冻结了 handover_id。重试
        # 沿用**原确认**必须走同一所有权判定：handover_confirmed=True 跳过
        # 普通新开播的旧进度闸门，同时 expected_handover_id 要求当前文件
        # 里仍是这一份记录并按原 id 重绑定新目标 run、持久写成功。任何一
        # 环不成立都拒绝——旧进度保持可恢复，绝不冒充、不复活旧请求。
        # 没有交接要求的意图（handover_id=None）绝不携带确认语义：重试按
        # 普通新开播过闸门，旧进度保护照常生效。
        retry_handover_id = intent.get('handover_id') or None
        self.start_streaming(
            instruction, '', is_task_mode=is_task_mode,
            epoch=int(epoch), source=intent.get('source') or SOURCE_NEW,
            inherit_elapsed=intent.get('inherit_elapsed'),
            expected_face_run_id=face_run,
            expected_face_room_id=face_room,
            handover_confirmed=bool(retry_handover_id),
            expected_handover_id=retry_handover_id)

    def confirm_face_verify(self, run_id=None, room_id=None,
                            epoch=None) -> bool:
        """兼容确认入口；调用方仍需自行派发被消费的冻结意图。"""
        return bool(self.consume_face_verify_intent(
            run_id=run_id, room_id=room_id, epoch=epoch))

    def _push_backend_event(self, tag: str, event_type: str, message: str,
                            notify: bool = True):
        """向后端事件日志推送一条记录（供前端轮询展示），可选触发通知。

        事件带稳定身份 ``id = boot_id:seq``：前端按 id 去重，而不是比较秒级
        时间——同秒发生的"完成 → 切换 → 开播"旧实现会整条跳过。

        ``notify=False`` 用于"该事件已经有更精确的通知路径"（例如验证链接、
        完成结算、停止结果），避免同一次事实发两封措辞不同的邮件。
        """
        if self.__dict__.get('_backend_events') is None:
            self._backend_events = []
        with self._event_lock:
            self._event_seq += 1
            seq = self._event_seq
        self._backend_events.append({
            'tag': tag,
            'type': event_type,  # success|danger|warning|info
            'message': message,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'seq': seq,
            'boot_id': self._boot_id,
            'id': f'{self._boot_id}:{seq}',
        })
        if len(self._backend_events) > 100:
            self._backend_events = self._backend_events[-50:]
        # 触发邮件通知
        if notify:
            self._try_email_notify(tag, event_type, message)

    #: 后端事件 tag → 结构化通知事件。**不再**解析中文 message 决定行为：
    #: 模板措辞一改就让重连/完成通知静默失效，是历史缺陷的根源。
    _NOTIFY_BY_TAG = {
        '重连': 'recovering',
        '验证': 'action_required',
        '错误': 'action_required',
        '异常': 'action_required',
    }

    def _try_email_notify(self, tag: str, event_type: str, message: str):
        """把后端事件转成结构化通知；只携带**事件发生时**的事实快照。"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            zone = (self.current_instruction.zone_name
                    if self.current_instruction else self.state.current_zone)
            # 通知里的"已播"只报**已确认的有效时长**：待确认区间还没经平台确认，
            # 不适合写进对用户可见的进度播报。
            elapsed = self._confirmed_elapsed() if self.is_streaming else 0
            elapsed_label = f"{elapsed // 60} 分钟"
            duration = (self.current_instruction.duration_seconds
                        if self.current_instruction else self.state.duration_seconds)
            duration_label = f"{duration // 60} 分钟" if duration > 0 else '不限时'
            remaining = max(0, duration - elapsed) if duration > 0 else 0
            run_id = self.state.run_id

            # 自然完成轮转中的 B 由 _start_in_background 收口邮件。开播成功、
            # 明确拒绝与验证事件仍写进面板，但不再各自抢发第二封。
            rotation = getattr(self, '_task_rotation_context', None)
            if (rotation and tag in ('开播', '开播失败', '错误', '异常', '验证')
                    and rotation.get('epoch') == self._control_epoch
                    and rotation.get('instruction') is self.current_instruction
                    and rotation.get('b_run_id') == str(
                        self._pending_run_id or rotation.get('b_run_id', ''))):
                return

            if tag == '开播':
                # 正常开播默认**一封**有用结果：受理阶段的事实只进操作日志，
                # 不再在同一回调里连发"受理 + 运行"两封（2026-09-21 B：
                # 同一次成功回调排两封属于可简化的冗余，不是两个独立阶段观察）。
                sender.notify_stream_running(
                    zone, self._current_source, elapsed_label, duration_label,
                    f"{remaining // 60} 分钟"
                    if remaining else '未知',
                    room_id=self.current_room_id, run_id=run_id,
                    elapsed_seconds=elapsed, duration_seconds=duration,
                    duration_known=duration > 0)
            elif tag == '开播失败':
                sender.notify_start_failed(
                    zone, identity=f'{self._pending_run_id}:{self._control_epoch}')
            elif tag == '完成':
                # 由 _settle_natural_completion 在结算**提交之后**单独发送，
                # 这里不再抢在 DB 确认之前报成功。
                return
            elif tag == '停播':
                sender.notify_stopped(zone, 'stopped', elapsed_label,
                                     elapsed_seconds=elapsed)
            elif tag in self._NOTIFY_BY_TAG:
                kind = self._NOTIFY_BY_TAG[tag]
                if kind == 'recovering':
                    sender.notify_recovering(
                        zone, self.reconnect_attempts, message, run_id=run_id)
                else:
                    # 仍在推流时如实说明；具体处置事实由各专用通道冻结，
                    # 这里不给"未替你停推"这类可能与后续处置矛盾的断言。
                    sender.notify_action_required(
                        f'{tag}', message, zone=zone,
                        still_streaming=bool(self.is_streaming),
                        action='按提示处理后在面板重试；如需中断请先点停止',
                        identity=f'{run_id}:{self._control_epoch}')
            elif tag == '推流':
                # 本地 FFmpeg 失败与平台直播间状态分开通知。快速重试是否发信
                # 由专用设置控制；danger 只用于 rapid_fails 首次达到 5 的需处理提醒。
                if event_type == 'danger':
                    sender.notify_local_stream_action_required(zone, run_id=run_id)
                else:
                    sender.notify_local_stream_fail(zone, message, run_id=run_id)
        except Exception as e:
            logger.debug(f" 邮件通知异常：{e}")

    def _notify_verification_required(self, message: str,
                                      stage: str = 'start_blocked',
                                      local_stop_requested: bool = False) -> None:
        """人脸验证：token 绑定**冻结的启动意图**，确认后按原样重试。

        随 token 一起冻结：run/room/epoch/模式/来源/继承进度/分区/task_id/
        执行日，以及触发阶段（首次开播被拦/恢复被拦/查询提示）与本路径的
        真实处置——邮件据此渲染，不再把控制器意图断言成媒体实际状态。
        """
        self._verify_retry_mode = 'manual' if self._stream_mode == 'manual' else 'task'
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender or not self._face_verify_url:
                return
            instruction = self.current_instruction
            face_epoch = getattr(self, '_face_verify_epoch', None)
            verification_run_id = (
                getattr(self, '_face_verify_run_id', '')
                or getattr(self, '_pending_run_id', '')
                or getattr(self, '_current_run_id', '')
                or self.state.run_id)
            rotation = getattr(self, '_task_rotation_context', None)
            defer_notification = bool(
                rotation
                and rotation.get('epoch') == (
                    face_epoch if face_epoch is not None else self._control_epoch)
                and rotation.get('instruction') is instruction
                and rotation.get('b_run_id') == str(verification_run_id or ''))
            verify_details = sender.send_face_verify(
                self._face_verify_url,
                run_id=verification_run_id,
                room_id=self.current_room_id,
                epoch=(face_epoch if face_epoch is not None
                       else self._control_epoch),
                still_streaming=bool(self.is_streaming),
                stage=stage,
                local_stop_requested=local_stop_requested,
                defer_notification=defer_notification,
                mode=self._verify_retry_mode,
                source=(self._pending_source if getattr(
                        self, '_is_starting', False)
                        and not self.is_streaming else
                        self._current_source or self._pending_source),
                inherit_elapsed=int(self._pending_inherit or 0),
                zone=(instruction.zone_name if instruction
                      else self.state.current_zone),
                task_id=(instruction.task_id if instruction else None),
                execution_date=(instruction.execution_date if instruction else None),
                reason=message)
            if defer_notification and isinstance(verify_details, dict):
                with self._task_rotation_lock:
                    if self._task_rotation_context is rotation:
                        rotation['verify_url'] = verify_details.get('verify_url', '')
                        rotation['confirm_url'] = verify_details.get('confirm_url', '')
        except Exception as e:
            logger.debug(f" 验证通知异常：{e}")

    def _check_network_ok(self) -> bool:
        """快速检查自身网络是否正常（ping 百度）"""
        try:
            requests.get("https://www.baidu.com", timeout=5)
            return True
        except Exception:
            return False

    @staticmethod
    def classify_failure(resp: dict) -> str:
        """把一次平台/网络失败归到一类，供调用方决定"继续"还是"停下"。

        分类必须是结构化判断，不能反过来用中文 message 里有没有某个词来猜：
        - ``transient``：暂时错误（网络/超时，``retryable=True``）——沿既定退避
          继续，绝不设置恢复阻塞；
        - ``cancelled``：用户/系统主动取消——不重试，也不报警；
        - ``verification``：平台要求身份验证——需要人工处理；
        - ``rejected``：平台明确拒绝（业务错误码）——暂停自动恢复；
        - ``unknown``：无法确定——按"未证明成功"处理，不静默当成功。
        """
        if not isinstance(resp, dict):
            return 'unknown'
        if resp.get('cancelled'):
            return 'cancelled'
        if resp.get('retryable'):
            return 'transient'
        code = resp.get('code', -1)
        if code in (60024, 60043):
            return 'verification'
        if isinstance(code, int) and code < 0:
            # 传输层/网络层才用负码；既是"没拿到结果"也不是平台拒绝。
            return 'transient'
        return 'rejected'

    def _handle_platform_failure(self, resp, context: str = 'control',
                                 owner_epoch: Optional[int] = None,
                                 owner_run_id: str = '', owner_room_id=None):
        """只把平台明确返回的拒绝交给人工处理，不把断网认作封禁。

        context='status_query'：调用方只是查询直播间状态。查询失败只说明
        “状态未知”，不能据此停止仍然健康的推流，也不得设置恢复阻塞；
        未知错误码更不能当成封禁。只有平台显式要求身份验证时才提示用户。
        """
        kind = self.classify_failure(resp)
        if kind in ('transient', 'cancelled', 'unknown'):
            # 短暂网络失败不是永久恢复阻塞：保留退避继续，不写 recovery_blocked。
            if kind == 'transient':
                diagnostics.get_diagnostics().record(
                    'platform_transient', context=context,
                    code=(resp or {}).get('code', -1),
                    action='continue_backoff')
                logger.debug("暂时性平台失败，按退避继续恢复（不设阻塞）：%s",
                             (resp or {}).get('code', -1))
            return
        code = resp.get('code', -1)
        message = resp.get('message') or resp.get('msg') or '平台拒绝请求'

        if context == 'status_query':
            self._status_query_failures = getattr(self, '_status_query_failures', 0) + 1
            # 累计值不会因为“下一次查询成功”而归零，用于证明持续恢复期间
            # 控制器确实处理过多少次未知查询失败。
            self._status_query_failures_total = getattr(
                self, '_status_query_failures_total', 0) + 1
            if code in (60024, 60043):
                # **只有**平台显式要求身份验证才这样提示；未知错误码不能冒充。
                uid = self.api.cookies.get('DedeUserID', '')
                verify_url = (
                    "https://www.bilibili.com/blackboard/live/face-auth-middle.html"
                    f"?source_event=400&mid={uid}")
                if not _register_face_verify_intent(self, verify_url):
                    return
                self._push_backend_event(
                    '验证', 'warning',
                    '直播间状态查询提示需要人脸验证，请完成验证后继续', notify=False)
                # 查询提示验证 ≠ 已停播：通知必须说明"仍可能在推流"，
                # 也**不**自动重开（避免错误地宣称验证成功或不必要的重开）。
                self._notify_verification_required(
                    '直播间状态查询提示需要人脸验证（推流可能仍在继续）',
                    stage='status_query')
                return
            # 其余错误码只是"状态未知"：不冒充人脸验证，也不触发任何自动处置。
            # 旧实现的缩进让这一段永远不可达，于是任何查询失败都被写成
            # "需要人脸验证"（哪怕平台返回的是 12345 之类与验证无关的错误）。
            if self._status_query_failures == 1 or self._status_query_failures % 20 == 0:
                self._push_backend_event(
                    '查询', 'warning',
                    f'直播间状态查询失败（第 {self._status_query_failures} 次），推流保持不变')
            diagnostics.get_diagnostics().record(
                'status_query_unknown', code=code,
                failures=self._status_query_failures,
                message=message, action='none')
            logger.warning(
                f"直播间状态查询失败（第 {self._status_query_failures} 次，"
                f"code={code}）：{message}")
            return

        episode = None
        owned_process = None
        if context == 'recovery' and owner_epoch is not None:
            # 停止与新开播也持这把锁；迟到结果不能给新场设阻塞或消费通知。
            with self._start_mutex():
                current_run = (getattr(self, '_current_run_id', '')
                               or getattr(self.state, 'run_id', '') or '')
                if (owner_epoch != self._control_epoch
                        or owner_run_id != current_run
                        or owner_room_id != self.current_room_id):
                    logger.info('旧会话的恢复拒绝迟到，丢弃结果')
                    return
                self._recovery_blocked = f'{message} (code={code})'
                self._ffmpeg_stop_event.set()
                owned_process = self.video_process
                current = getattr(self, '_platform_outage', None)
                if (current and current['epoch'] == owner_epoch
                        and current['run_id'] == owner_run_id
                        and current['room_id'] == owner_room_id):
                    episode = dict(current)
                    self._platform_outage = None
        else:
            self._recovery_blocked = f'{message} (code={code})'
        if code in (60024, 60043):
            uid = self.api.cookies.get('DedeUserID', '')
            verify_url = f"https://www.bilibili.com/blackboard/live/face-auth-middle.html?source_event=400&mid={uid}"
            if not _register_face_verify_intent(self, verify_url):
                if context == 'recovery' and owner_epoch is not None:
                    with self._start_mutex():
                        if self._control_epoch == owner_epoch:
                            self._recovery_blocked = ''
                else:
                    self._recovery_blocked = ''
                return
            # 一个验证事实一封：事件给面板（notify=False），邮件走
            # send_face_verify 专用通道并**冻结本路径的真实处置**。
            # 旧实现这里先发一封"未替你停推"的通用邮件，紧接着又停推、
            # 再发一封验证邮件——两封互相矛盾（2026-09-21 N2）。
            if context == 'recovery' and owner_epoch is not None:
                if owned_process is not None:
                    self._kill_ffmpeg(expected_process=owned_process)
            elif context != 'start':
                self._ffmpeg_stop_event.set()
                self._kill_ffmpeg()
            face_stage = ('resume_blocked' if context != 'start' or
                          getattr(self, '_pending_source', SOURCE_NEW)
                          == SOURCE_RESUME else 'start_blocked')
            face_label = ('恢复直播' if face_stage == 'resume_blocked'
                          else '开播')
            self._push_backend_event('验证', 'warning',
                                     f'{face_label}需要人脸验证，请完成验证后继续',
                                     notify=False)
            self._notify_verification_required(
                f'{face_label}需要人脸验证', stage=face_stage,
                local_stop_requested=context != 'start')
        else:
            self._push_backend_event(
                '错误', 'danger', f'自动恢复暂停：{self._recovery_blocked}',
                notify=episode is None and context != 'start')
            if context == 'recovery' and owner_epoch is not None:
                if owned_process is not None:
                    self._kill_ffmpeg(expected_process=owned_process)
            else:
                self._ffmpeg_stop_event.set()
                self._kill_ffmpeg()
            self._notify_platform_outage_result(episode, 'failed')

    def _begin_platform_outage(self, epoch: int) -> Optional[dict]:
        """一次已确认断播归属于同一场直播；重复观察不新建邮件事件。"""
        instruction = self.current_instruction
        run_id = (getattr(self, '_current_run_id', '')
                  or getattr(self.state, 'run_id', '') or '')
        if not instruction or not run_id:
            return None
        with self._start_mutex():
            if epoch != self._control_epoch or not self.is_streaming:
                return None
            current = getattr(self, '_platform_outage', None)
            if (current and current['epoch'] == epoch
                    and current['run_id'] == run_id
                    and current['room_id'] == self.current_room_id):
                return current
            self._platform_outage_seq = getattr(
                self, '_platform_outage_seq', 0) + 1
            current = {
                'epoch': epoch, 'run_id': run_id,
                'room_id': self.current_room_id,
                'zone': instruction.zone_name,
                'seq': self._platform_outage_seq,
                'started_at': _monotonic(), 'pending_alerted': False,
            }
            self._platform_outage = current
            return current

    def _platform_outage_pending_alert(self, epoch: int) -> Optional[dict]:
        """无限重试不会有“最终失败”；超时仅提醒仍未恢复一次。"""
        with self._start_mutex():
            current = getattr(self, '_platform_outage', None)
            if (not current or current['epoch'] != epoch
                    or epoch != self._control_epoch
                    or current['run_id'] != (
                        getattr(self, '_current_run_id', '')
                        or getattr(self.state, 'run_id', '') or '')
                    or current['room_id'] != self.current_room_id
                    or current['pending_alerted']
                    or _monotonic() - current['started_at']
                    < PLATFORM_OUTAGE_ALERT_SECONDS):
                return None
            current['pending_alerted'] = True
            return dict(current)

    def _take_platform_outage(self, epoch: int, run_id: str,
                              room_id=None) -> Optional[dict]:
        """只有本场同一房间的终态可消费这次故障；旧响应不能报新场结果。"""
        with self._start_mutex():
            current = getattr(self, '_platform_outage', None)
            if (not current or current['epoch'] != epoch
                    or current['run_id'] != run_id
                    or current['room_id'] != (room_id or self.current_room_id)
                    or epoch != self._control_epoch
                    or (getattr(self, '_current_run_id', '')
                        and self._current_run_id != run_id)):
                return None
            self._platform_outage = None
            return dict(current)

    def _notify_platform_outage_result(self, episode: Optional[dict],
                                       result: str) -> None:
        if not episode:
            return
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if sender:
                sender.notify_platform_outage_result(
                    episode['zone'], result, episode['run_id'], episode['seq'])
        except Exception:
            logger.debug('直播间断播结果通知失败（不影响恢复）', exc_info=True)

    def _handle_live_anomaly_retry(self, epoch: int = None):
        """网络或直播间中断持续恢复，退避最多 30 秒，不耗尽重试次数。"""
        if epoch is not None and not self._is_epoch_current(epoch):
            # 停止已生效：旧监控不得再累计重连计数或推进退避。
            return
        if self._recovery_blocked or self.stop_monitor.is_set():
            return
        if epoch is None:
            epoch = self._control_epoch
        self._begin_platform_outage(epoch)
        pending = self._platform_outage_pending_alert(epoch)
        if pending:
            self._notify_platform_outage_result(pending, 'still_retrying')
        self.reconnect_attempts += 1
        if self.reconnect_attempts == 1 or self.reconnect_attempts % 20 == 0:
            self._push_backend_event(
                '重连', 'warning',
                f'直播中断，后台持续恢复（第 {self.reconnect_attempts} 次）',
                notify=False)
        if not self.stop_monitor.wait(min(2 ** min(self.reconnect_attempts, 5), 30)):
            self._retry_start_live(epoch)

    def _retry_start_live(self, epoch: int = None):
        """内部重试开播：重开 B站 房间 + FFmpeg 模式则重启推流

        这是监控线程的“持续恢复”意图，不是用户的一次控制操作；它沿用本场直播
        启动时登记的代际，因此用户停止（代际 +1）之后不会再被拉起。

        代际必须在**每个等待点之后**复核，且不能只看 ``stop_monitor``——那是
        可被下一次开播清除的复用事件。平台响应返回后、缓存/准备之后、以及
        最终启动本地推流之前都重新核对：旧请求无权触碰新一代。

        全程不持控制锁做网络等待，否则停止入口会被慢网络卡住。
        """
        if epoch is None:
            epoch = self._control_epoch
        if not self._is_epoch_current(epoch):
            logger.info("重连期间停止已生效，放弃本次自动重连")
            return
        if (self.stop_monitor.is_set() or self._recovery_blocked
                or not self.current_room_id or not self.current_instruction):
            return
        csrf = self.api.get_csrf()
        if not csrf:
            return
        room_id_at_entry = self.current_room_id
        run_id_at_entry = (getattr(self, '_current_run_id', '')
                           or getattr(self.state, 'run_id', '') or '')
        intent_at_entry = self._start_intent_id
        area_id = self.area_loader.get_area_id(self.current_instruction.zone_name, auto_update=False)
        request_start = _monotonic()
        success, resp = self.api.start_live(room_id_at_entry, area_id, csrf)
        returned = _monotonic()
        if not self._is_epoch_current(epoch) or self.stop_monitor.is_set():
            # 平台响应期间发生了停止。停止请求的下播可能先于本次 start_live
            # 到达平台，于是刚重开的房间成为流浪房间——补做一次下播。
            # 但只有“本次重连仍是这个房间的所有者”时才补偿：若期间用户已
            # 接受新的开播意图，房间归新意图的清理流程，绝不代替它下播。
            if (success and room_id_at_entry == self.current_room_id
                    and self._start_intent_id == intent_at_entry):
                try:
                    self.api.stop_live(room_id_at_entry, csrf)
                    logger.info("重连迟到响应：撤销刚重开的直播间")
                except Exception:
                    logger.debug("重连补偿下播失败（忽略）", exc_info=True)
            logger.info("重连响应到达时停止已生效，放弃本次自动重连")
            return
        if success:
            logger.info(" 重连成功，直播间已重新开启")
            # 平台确认房间重新在播 = 一次可信观察：若此前因"房间关闭/查询未知"
            # 暂停过，这里只**重立锚点**（不补算暂停期间）；若平台一直正常而只是
            # 本地 FFmpeg 重连，则把这段连续区间正常入账，不会误判为暂停。
            outcome = self._credit_effective_observation(
                request_start, returned, expected_epoch=epoch,
                expected_run_id=run_id_at_entry)
            if outcome == 'dropped_owner':
                logger.info('重连确认计时前会话已停止/接管，忽略迟到结果')
                return
            if not self._is_epoch_current(epoch) or self.stop_monitor.is_set():
                logger.info('重连确认后停止已生效，不发送恢复成功通知')
                return
            # 只对之前已确认关闭的同一场直播发一次恢复结案。
            self._push_backend_event(
                '重连', 'success', '直播间已重新开启，正在恢复本地推流',
                notify=False)
            episode = self._take_platform_outage(
                epoch, run_id_at_entry, room_id_at_entry)
            self._notify_platform_outage_result(episode, 'recovered')
            self._extract_and_cache_rtmp(resp)  # 更新推流码缓存
            # 缓存/准备期间同样可能发生停止：迟到的成功响应不得恢复本地推流，
            # 也不得覆盖新代的重连计数。
            if not self._is_epoch_current(epoch) or self.stop_monitor.is_set():
                logger.info("重连缓存准备期间停止生效，不恢复本地推流")
                return
            self.reconnect_attempts = 0
            self._retry_cooldown_until = None
            # “任务来源（手动指定分区/任务调度）”与“推流来源（本机 FFmpeg/OBS）”
            # 是两个维度。是否恢复本机推流只看设置的推流方式，避免手动选分区
            # 使用本机 FFmpeg 时被当成外部推流而漏掉恢复。OBS 模式永不启动本地进程。
            stream_mode, _ = self._get_stream_settings()
            if stream_mode == 'ffmpeg':
                logger.info(" 重启 FFmpeg 推流循环...")
                # 最终副作用（进程所有权操作）前把本代传给推流启动，由其再做一次复核。
                self._start_ffmpeg_stream(epoch)
        else:
            self._handle_platform_failure(
                resp, context='recovery', owner_epoch=epoch,
                owner_run_id=run_id_at_entry, owner_room_id=room_id_at_entry)

    def _monitor_streaming(self):
        """持续恢复断网和推流中断；平台明确拒绝时暂停并通知。"""
        is_manual = (self._stream_mode == 'manual')
        logger.info(f" 启动直播监控线程（模式：{self._stream_mode}）")
        monitor_epoch = self._control_epoch
        # 本监控所属的场次身份：退出段用它核对"这一场还是不是当前那一场"。
        # 只比较 task_id 不够——同一任务也可能被"停止 → 重新开播"成新的一代。
        # 推迟接管窗口（FFmpeg 未就绪）里 live_state 仍是旧会话，因此身份
        # 优先取控制器当前意图的 run，而不是 state 里可能过期的 run。
        monitor_run_id = (getattr(self, '_current_run_id', '')
                          or getattr(self.state, 'run_id', '') or '')
        # 监控退出清理可能等待平台 API；入口处冻结身份，不能在等待后把新意图
        # 当成自己所属的会话。
        self._begin_stop_cleanup_context()
        # 间隔闸门也走单调时钟：系统时间被回拨/前跳都不会让监控要么空转要么
        # 一次性补发查询；同时让"可注入时钟"能把多小时场景压进有限测试时间。
        last_check_time = _monotonic()
        last_cross_day_check = _monotonic()
        should_stop = False
        cross_day_detected = False

        # 读取重试设置
        monitor_interval = MONITOR_INTERVAL
        try:
            from app.api.settings import load_settings
            s = load_settings()
            if s.scan_interval_seconds and s.scan_interval_seconds >= 5:
                monitor_interval = s.scan_interval_seconds
        except Exception:
            pass
        # 有效计时的入账上限由本次实际使用的监控周期推导（见
        # effective_credit_limit 的公式与代价说明），不新增任何探针。
        self._monitor_interval_seconds = monitor_interval
        self._clock().credit_limit = effective_credit_limit(monitor_interval)
        checkpoint_save_failures = 0
        checkpoint_abort = False
        missing_room_warned = False

        while not self.stop_monitor.is_set() and self.is_streaming and not should_stop:
            if self._recovery_blocked:
                # 恢复阻塞（验证/平台拒绝）：保持暂停，不持续累计、也不会自动到时完成。
                self._pause_effective_timer(TIMER_PAUSED_BLOCKED)
                if getattr(self.state, '_checkpoint_failure_active', False):
                    if self.state._save_checkpoint('恢复状态自动重试'):
                        checkpoint_save_failures = 0
                        self._recovery_blocked = ''
                        # 暂停期间未观察平台状态；清除旧锚点，下一次新鲜在播观察
                        # 只立锚点，不把存储故障期间的墙钟时间补进有效时长。
                        self._segment_monotonic = None
                        self._push_backend_event(
                            '恢复', 'info',
                            '直播状态存档已恢复；等待下一次平台确认后继续计时')
                        continue
                    checkpoint_save_failures += 1
                    if checkpoint_save_failures >= 5:
                        checkpoint_abort = True
                        logger.error('恢复状态连续 5 次保存失败，受控停止直播并保留现场')
                        break
                self.stop_monitor.wait(1)
                continue
            now = _monotonic()

            # 跨日检测（仅任务模式，手动模式不受干扰）
            if not is_manual and now - last_cross_day_check >= CROSS_DAY_CHECK_INTERVAL:
                last_cross_day_check = now
                if self.state.is_cross_day():
                    logger.warning(" 检测到跨日！中断当前直播任务..")
                    self._push_backend_event('跨日', 'warning', '检测到跨日，中断当前任务，等待每日重置后执行新任务')
                    cross_day_detected = True
                    should_stop = True
                    break

            if now - last_check_time >= monitor_interval:
                last_check_time = now

                # === 1) 先做平台观察，再判定是否到时 ===
                # 没有房间身份就无法查询平台：保留本场进度并暂停，而不能读未
                # 赋值的查询结果使监控线程崩溃，随后让任务永久失去恢复/结算。
                if not self.current_room_id:
                    self._pause_effective_timer(TIMER_PAUSED_UNKNOWN, now)
                    if not missing_room_warned:
                        logger.warning("直播间身份暂不可用，暂停有效计时并等待重新确认")
                        missing_room_warned = True
                    self.stop_monitor.wait(1)
                    continue
                missing_room_warned = False
                # 旧顺序是"先按墙钟到时、后查平台"，于是最后一段墙钟时间（可能
                # 平台已经关闭）也会被拿来结算。现在只有经本规则入账的**已确认**
                # 有效时长才能触发完成，最后一段待确认区间不允许提前结算。
                request_start = _monotonic()
                success, status_resp = self.api.get_live_status(self.current_room_id)
                returned = _monotonic()
                # 观察返回后**先复核所有权**：这条查询在途期间可能已经发生了
                # 停止并被新会话接管。迟到的观察无权给新会话加时、暂停或
                # 触发结算——只比较"我还在循环里"是不够的。
                if not self._owns_current_run(monitor_epoch, monitor_run_id):
                    logger.info(
                        "状态观察返回时本场已被接管（epoch/run 已变），"
                        "丢弃该观察结果：%s", monitor_run_id)
                    diagnostics.get_diagnostics().record(
                        'effective_time_stale_observation_dropped',
                        run_id=monitor_run_id)
                    break
                status_data = (status_resp.get('data')
                               if isinstance(status_resp, dict) else None)
                live_status = (status_data.get('live_status')
                               if isinstance(status_data, dict) else None)
                live_ok = success and live_status == 1

                if live_ok:
                    # 直播状态正常：一次性把上一段连续区间并入已确认时长。
                    self.reconnect_attempts = 0
                    self._status_query_failures = 0
                    self._retry_cooldown_until = None
                    self._retry_window_start = None
                    outcome = self._credit_effective_observation(
                        request_start, returned,
                        expected_epoch=monitor_epoch,
                        expected_run_id=monitor_run_id)
                    if outcome == 'dropped_owner':
                        logger.info('状态确认在计时提交前已失去会话所有权，丢弃')
                        break
                    # 也可能是平台自行恢复，未经过本机 start_live；仍按本场
                    # 已确认断播事件结案一次，不能把后续查询重复算成新故障。
                    episode = self._take_platform_outage(
                        monitor_epoch, monitor_run_id, self.current_room_id)
                    self._notify_platform_outage_result(episode, 'recovered')
                    mode, _ = self._get_stream_settings()
                    if mode == 'ffmpeg' and not (self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive()):
                        self._start_ffmpeg_stream(monitor_epoch)
                elif success and live_status == 0:
                    # 只有明确的 live_status=0 才证明平台关闭；缺字段、
                    # 非法 data 或其他状态都不能触发下播/重连。
                    logger.warning(" 直播间已被平台关闭 (live_status=0)")
                    self._pause_effective_timer(TIMER_PAUSED_CLOSED, returned)
                    self._handle_live_anomaly_retry(monitor_epoch)
                else:
                    # 查询失败并不证明已下播：暂停新增有效时长，但保持推流，
                    # 下个周期继续查询（未知 != 停止，禁止据此杀健康进程）。
                    self._pause_effective_timer(TIMER_PAUSED_UNKNOWN, returned)
                    self._handle_platform_failure(status_resp, context='status_query')

                # === 2) 完成门槛：只认已确认的有效时长 ===
                if self.stream_start_time and self.current_instruction:
                    confirmed = self._confirmed_elapsed()
                    dur = self.current_instruction.duration_seconds
                    if dur > 0 and confirmed >= dur:
                        logger.info(f" 直播有效时长已到 ({confirmed / 60:.1f}分钟)")
                        should_stop = True
                        break

            self.stop_monitor.wait(1)

        # 退出处理
        if checkpoint_abort:
            if self._owns_current_run(monitor_epoch, monitor_run_id):
                self.stop_monitor.set()
                checkpoint_saved = self._stop_live_process(preserve_state=True)
                if self._stop_cleanup_is_stale():
                    return
                self._release_active_task(monitor_run_id)
                cleanup_confirmed = self._cleanup_confirmed()
                reason = (self.state.resume_blocked_reason
                          or '直播状态保存连续失败；自动结算与轮转已阻断')
                self._recovery_blocked = reason
                if cleanup_confirmed:
                    stop_status = '直播已确认停止'
                else:
                    stop_status = (
                        '已发起停止清理，但平台下播或本地进程回收尚未确认；'
                        '请再次点击停止重试')
                checkpoint_status = (
                    '恢复检查点已确认写入' if checkpoint_saved else
                    '恢复检查点写入未确认，自动结算与轮转已阻断')
                self._push_backend_event(
                    '恢复', 'danger',
                    f'{stop_status}；{checkpoint_status}：{reason}')
            return
        if should_stop:
            zone_name_done = self.current_instruction.zone_name if self.current_instruction else '未知'
            # 所有权复核：任何耗时等待（下播/回收/重置）之前先确认本场仍是当前场。
            # 旧实现先 _stop_live_process 再进 DB 条件更新，于是旧代可能下播新场、
            # 清掉新场的进程/状态/在播引用，然后才做一次注定被 task_id 挡住的结算。
            if not self._owns_current_run(monitor_epoch, monitor_run_id):
                logger.warning(
                    "监控退出时本场已被接管（epoch/run 已变），放弃下播与轮转：%s",
                    monitor_run_id)
                return
            if cross_day_detected:
                logger.info(" 跨日中断：冻结旧日确认进度并核对结算后再选择新任务")
                # 先checkpoint会话身份与已确认进度；不得用 preserve=False 清空
                # 唯一旧日证据，再尝试判断它是否已结算。
                checkpoint_saved = self._stop_live_process(preserve_state=True)
                if self._stop_cleanup_is_stale():
                    return
                checkpoint_marker = (
                    self.state.state_file.parent / '.resume_checkpoint_failed')
                state_saved = True
                if self.state.current_zone:
                    self.state.is_streaming = False
                    self.state.auto_resume = False
                    self.state.phase = PHASE_IDLE
                    self.state.resume_blocked_reason = (
                        CROSS_DAY_PENDING_PREFIX +
                        f'旧任务「{self.state.current_zone}」执行日 '
                        f'{self.state.execution_date or "未知"} 待核对')
                    try:
                        state_saved = self.state.save() is not False
                    except Exception:
                        state_saved = False
                    if not checkpoint_saved or not state_saved:
                        reason = (self.state.resume_blocked_reason +
                                  '；保存旧日核对状态失败，已保留原进度')
                        try:
                            checkpoint_marker.write_text(reason, encoding='utf-8')
                        except OSError as exc:
                            logger.critical('无法写入跨日旧档保护标记：%s', exc)
                        self.state.resume_blocked_reason = reason
                if not checkpoint_saved or not state_saved or checkpoint_marker.exists():
                    reason = (self.state.resume_blocked_reason or
                              '停播checkpoint失败标记仍在，旧日进度不能自动清理')
                    logger.warning("跨日checkpoint未确认，暂停自动轮转：%s", reason)
                    self._push_backend_event(
                        '跨日', 'danger',
                        f'跨日旧任务进度已保留，自动轮转暂停：{reason}')
                    self.current_instruction = None
                    return
                # 先等业务日切换完整发布，再读取 today_done。午夜附近旧库的
                # yesterday today_done=1 是上一业务日的正确状态，不能误报冲突。
                if self.task_manager and not self._ensure_day_ready(monitor_epoch):
                    logger.warning("跨日等待未就绪：暂停核查旧档与自动轮转")
                    self._push_backend_event(
                        '重置', 'warning',
                        '每日重置未在预算内就绪（或期间已停止），旧日进度已保留；'
                        '请稍后再核查并继续')
                    self.current_instruction = None
                    return
                if not self._owns_current_run(monitor_epoch, monitor_run_id):
                    logger.warning("跨日核查前本场已被接管，放弃旧档清理")
                    return
                cleared, reason = self._clear_settled_saved_state_for_new_task()
                if not cleared:
                    logger.warning("跨日旧档不能安全清理，暂停自动轮转：%s", reason)
                    self._push_backend_event(
                        '跨日', 'danger',
                        f'跨日旧任务进度已保留，自动轮转暂停：{reason}')
                    self.current_instruction = None
                    return
                self._push_backend_event(
                    '跨日', 'warning',
                    '旧执行日任务已安全清理；跨日未完成部分不会计入今日完成')
                self.current_instruction = None
                # 业务日屏障和旧存档核查均通过后，才允许自动选择新日任务。
                if self.task_manager:
                    if not self._owns_current_run(monitor_epoch, monitor_run_id):
                        logger.warning("跨日等待期间本场已被接管，放弃推进轮转")
                        return
                    logger.info(" 每日重置已就绪，执行下一个任务...")
                    self.run_next_task(epoch=monitor_epoch)
            elif is_manual:
                logger.info("手动模式直播时长已到，自动下播")
                # 面板事件先给（即时可见），邮件在**清理结果明确后**按真实
                # 阶段发送——旧实现清理前就发"已停止"（2026-09-21 N3）。
                self._push_backend_event(
                    '停播', 'info',
                    f'手动模式时长已到，自动下播（{zone_name_done}）',
                    notify=False)
                self._finish_manual_expiry(
                    zone_name_done,
                    self._confirmed_elapsed() if self.is_streaming else 0)
            else:
                # 结算身份在**下播之前**捕获：下播会清掉 current_instruction，
                # 旧实现事后才按（可能被替换的）指令结算，可能记到另一条记录上。
                instruction = self.current_instruction
                settle_task_id = instruction.task_id if instruction else None
                settle_day = self.state.execution_date
                settle_zone = instruction.zone_name if instruction else zone_name_done
                rotation_completion = {
                    'zone': settle_zone,
                    'execution_date': settle_day or '',
                    'effective_seconds': max(0, int(
                        self._confirmed_elapsed() if self.is_streaming else 0)),
                    'days_done': None,
                }
                logger.info("任务完成，写入数据库...")
                self._stop_live_process(preserve_state=False)
                if self._stop_cleanup_is_stale():
                    return
                self._release_active_task(monitor_run_id)
                task_done = False
                if instruction and self.task_manager:
                    outcome = self._settle_natural_completion(
                        settle_zone, settle_task_id, settle_day,
                        completion_context=rotation_completion)
                    task_done = outcome in ('settled', 'already')
                # 任务模式下自动执行下一个任务
                if task_done:
                    if not self._cleanup_confirmed():
                        reason = '旧直播间下播或本地推流回收尚未确认'
                        logger.warning('任务已结算，但暂停自动轮转：%s', reason)
                        self._push_backend_event(
                            '切换', 'warning',
                            f'任务「{settle_zone}」已完成；{reason}，请核对并重试停止',
                            notify=False)
                        if outcome == 'settled':
                            self._notify_task_rotation_paused(
                                rotation_completion, monitor_epoch, reason)
                        return
                    if not self._owns_current_run(monitor_epoch, monitor_run_id):
                        logger.warning("完成结算后本场已被接管，放弃推进轮转")
                        if outcome == 'settled':
                            self._notify_task_rotation_paused(
                                rotation_completion, monitor_epoch,
                                '结算后控制所有权已变化')
                        return
                    if instruction and (self.state.current_zone or self.state.corrupt):
                        if not self._state_matches_session(instruction, monitor_run_id):
                            logger.warning("结算成功但无法证明存档归属本场，暂停轮转避免串接旧进度")
                            if outcome == 'settled':
                                self._notify_task_rotation_paused(
                                    rotation_completion, monitor_epoch,
                                    '无法核实旧存档归属')
                            return
                        if not self._clear_settled_session_state(
                                instruction, monitor_run_id, monitor_epoch):
                            logger.warning("结算成功但存档未能按所有权清除，暂停轮转避免串接旧进度")
                            if outcome == 'settled':
                                self._notify_task_rotation_paused(
                                    rotation_completion, monitor_epoch,
                                    '旧存档未能按所有权安全清除')
                            return
                    logger.info(" 自动查找并执行下一个任务...")
                    self._push_backend_event('切换', 'info', f'任务「{zone_name_done}」完成，自动执行下一个任务')
                    # already 只是一条面板历史事实，不应把旧 A 重新附在新 B
                    # 邮件上；新 B 仍按普通开播结果独立通知。
                    rotation_notice = (rotation_completion
                                       if outcome == 'settled' else None)
                    self.run_next_task(epoch=monitor_epoch,
                                       completion_context=rotation_notice)
                    if self._pending_face_verify:
                        logger.warning(" 自动切换任务遇到人脸验证，等待前端确认")
                else:
                    # 未结算成功：必须可见、可重试，不能静静卡住轮转
                    logger.warning(f" 任务未结算（{settle_zone}），保留待办并继续轮转")
        logger.info(" 直播监控线程已退出")

    def _finish_manual_expiry(self, zone: str, elapsed: int) -> None:
        """手动模式时长到期的统一收尾：先清理，后按**真实结果**通知。

        未确认回收不得提前写"已停止"（与用户停止路径同一套阶段语义）。
        通知失败只记日志，不影响停止本身。
        """
        try:
            self._stop_live_process(preserve_state=False)
            if getattr(self, '_stop_cleanup_is_stale', lambda: False)():
                return
            try:
                confirmed = self._cleanup_confirmed()
            except Exception:
                confirmed = False
            stage = 'stopped' if confirmed else 'pending_recycle'
            reason = ('' if confirmed
                      else '仍有资源未确认回收，可再次点击停止重试')
            # 清理未确认时可能仍持有活的推流进程，不能写"已停止"（R3）。
            panel_line = (
                f'直播已停止（{zone or "未知分区"}）' if confirmed
                else f'直播停止处理中（{zone or "未知分区"}）；清理结果待确认'
                     + (f'，{reason}' if reason else ''))
            self._push_backend_event('停播', 'info', panel_line, notify=False)
            self._notify_stopped(zone or '未知分区', stage, elapsed, reason)
        except Exception:
            logger.debug("手动到期收尾通知异常（不影响停止结果）", exc_info=True)

    def _settle_natural_completion(self, zone_name: str,
                                   task_id: Optional[int],
                                   execution_day: Optional[str],
                                   completion_context: Optional[dict] = None) -> str:
        """自然到时的结算：区分结果，不把失败说成成功。

        返回 settle_task_done 的状态；同时把每种结果写成明确的事件：
        - settled：已提交一天；
        - already：今天/本次执行已经结算过，不重复加天；
        - replaced/not_found：任务被替换或删除，旧结算**不**落到新记录；
        - failed：DB 失败且已回滚——先做一次有界重试，仍失败则显式告警。
        """
        checkpoint_marker = (self.state.state_file.parent
                             / '.resume_checkpoint_failed')
        if (getattr(self.state, '_checkpoint_failure_active', False)
                or checkpoint_marker.exists()):
            reason = (getattr(self.state, 'resume_blocked_reason', '')
                      or '直播进度保存失败，无法证明结算使用的是完整时长')
            self._recovery_blocked = reason
            self._push_backend_event(
                '完成', 'danger', f'结算与自动轮转已阻断：{reason}')
            logger.error('恢复checkpoint不可信，保留旧任务现场并暂停结算/轮转')
            return 'blocked'
        exec_date = None
        if execution_day:
            try:
                exec_date = datetime.strptime(execution_day, '%Y-%m-%d').date()
            except ValueError:
                exec_date = None
        outcome = self.task_manager.settle_task_done(
            zone_name, execution_date=exec_date, task_id=task_id)
        status = outcome['status']
        def freeze_completion_facts(result):
            if completion_context is None:
                return
            completion_context.update({
                'zone': zone_name,
                'execution_date': str(result.get('execution_date')
                                      or execution_day or ''),
                'days_done': result.get('days_done'),
                'settlement_result': result.get('status', status),
                'scope': ('all' if result.get('all_done') else 'today'),
                'task_id': task_id,
            })

        if status in ('settled', 'already'):
            # 事件给面板；通知在**提交之后**单独发（旧实现只在事件侧 return，
            # 另一处又只 push 事件，于是"结算提交成功"从来没有完成通知）。
            self._push_backend_event(
                '完成', 'success' if status == 'settled' else 'info',
                (f'任务完成：{zone_name}（执行日 {outcome["execution_date"]}）'
                 if status == 'settled'
                else f'任务今日已完成，未重复计天：{zone_name}'),
                notify=False)
            freeze_completion_facts(outcome)
            # 已经结算过的执行日只保留面板记录，不把历史完成重新发成邮件；
            # 自动轮转时由同一封 A→B 结果邮件承载本次冻结事实。
            if status == 'settled' and completion_context is None:
                self._notify_task_complete(zone_name, outcome)
        elif status in ('replaced', 'not_found'):
            self._push_backend_event(
                '完成', 'warning',
                f'任务已被替换或删除，未结算到新记录：{zone_name}')
            logger.warning("任务自然结束但身份已变化，跳过结算：%s", zone_name)
        else:
            # 有界重试一次：瞬态写失败不应直接放弃用户的一天
            logger.warning("任务结算失败，重试一次：%s", zone_name)
            time.sleep(0.5)
            retry = self.task_manager.settle_task_done(
                zone_name, execution_date=exec_date, task_id=task_id)
            status = retry['status']
            if status in ('settled', 'already'):
                self._push_backend_event(
                    '完成', 'success',
                    f'任务完成（重试后提交）：{zone_name}', notify=False)
                freeze_completion_facts(retry)
                if status == 'settled' and completion_context is None:
                    self._notify_task_complete(zone_name, retry)
            else:
                self._push_backend_event(
                    '完成', 'danger',
                    f'任务结算失败，需人工处理：{zone_name}（数据库写入未完成，'
                    f'可稍后在任务页手动标记完成）')
                logger.error("任务自然结束但结算未提交（已回滚）：%s → %s",
                             zone_name, status)
                # 失败**不得**发成功通知：明确带上 result=failed。
                self._notify_task_complete(zone_name, retry, result='failed')
        return status

    def _notify_task_complete(self, zone_name: str, outcome: dict,
                              result: Optional[str] = None) -> None:
        """结算提交之后的完成通知（事实取自 outcome，不重新读当前场状态）。"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            status = result or outcome.get('status', 'failed')
            if status not in ('settled', 'already', 'failed'):
                status = 'failed'
            scope = 'all' if outcome.get('all_done') else 'today'
            days = outcome.get('days_done')
            progress = f'已完成 {days} 天' if days is not None else ''
            sender.notify_task_complete(
                zone_name, str(outcome.get('execution_date') or ''),
                scope=scope, result=status, progress=progress)
        except Exception as e:
            logger.debug(f" 完成通知异常：{e}")

    @staticmethod
    def _notify_outcome_ok(status) -> bool:
        """通知返回状态里哪些算"已交给渠道"。其余一律要留痕。"""
        # 'suppressed' 是明确「这一阶段不发邮件」的结论（受理/恢复过程等），
        # 不是投递失败，不能当成「没发出去」去告警。
        return status in ('sent', 'queued', 'unconfirmed',
                          'suppressed', None)

    def _notify_stopped(self, zone_name: str, stage: str = 'stopped',
                        elapsed: int = 0, reason: str = '') -> None:
        """停止结果通知（真实结果，不在事件侧再发一次）。"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                logger.warning('停止通知未送出：通知发送器未初始化')
                return
            label = f'{int(elapsed) // 60} 分钟' if elapsed else ''
            status = sender.notify_stopped(zone_name, stage, label, reason,
                                          elapsed_seconds=elapsed)
            # 返回值过去被直接丢弃：disabled / coalesced / dropped / failed
            # 全都不可见（只有 DEBUG 记录）。停止结果是最需要留痕的一类通知，
            # 这里必须让"没发出去"变成可见事实。
            if not self._notify_outcome_ok(status):
                logger.warning(
                    '停止通知未送出：状态=%s（分区=%s，阶段=%s）',
                    status, zone_name, stage)
        except Exception as e:
            logger.warning(f"停止通知异常：{e}")

    def _notify_recovered(self, zone_name: str, scope: str = 'platform',
                          evidence: str = '', run_id: str = '') -> None:
        """恢复成功通知（生产成功点此前从未接上）。"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            sender.notify_recovered(zone_name, scope=scope, evidence=evidence,
                                    run_id=run_id)
        except Exception as e:
            logger.debug(f" 恢复通知异常：{e}")

    @staticmethod
    def _platform_stop_ok(result) -> bool:
        """平台下播结果归一化：容错 (ok, payload) 元组与裸布尔。

        None 表示实现没有返回值——请求未抛异常，视为已下发（与旧行为一致，
        不把"没有返回值"当成失败而反复重试平台下播）。
        """
        if isinstance(result, tuple):
            return bool(result[0]) if result else False
        if result is None:
            return True
        return bool(result)

    def _resolve_csrf(self) -> tuple:
        """解析平台写操作所需的 csrf，返回 ``(csrf, reason)``。

        `get_csrf()` 只读内存快照（`self.cookies`）。落盘已经更新、内存尚未
        刷新时它会一直返回空——而旧代码在停止路径上把它当成"没什么可做的"
        直接跳过。这里补一次**重读凭据文件**的机会，并把原因如实带回去。
        """
        try:
            csrf = self.api.get_csrf()
        except Exception as exc:
            return '', f'read_error:{exc}'
        if csrf:
            return str(csrf), 'ok'
        reload_fn = getattr(self.api, '_load_cookies', None)
        if callable(reload_fn):
            try:
                reload_fn()
            except Exception as exc:
                return '', f'reload_error:{exc}'
            try:
                csrf = self.api.get_csrf()
            except Exception as exc:
                return '', f'reread_error:{exc}'
            if csrf:
                return str(csrf), 'reloaded'
        return '', 'missing'

    def _platform_stop_live(self, room_id, guard=None) -> tuple:
        """平台下播的**唯一实现**：解析凭据 → 下发 → 归一化 → 必定留痕。

        返回 ``(ok, reason)``；``reason`` ∈ ``ok`` / ``no_room`` /
        :data:`PLATFORM_STOP_STALE` / ``csrf_*`` / ``error:*`` / ``rejected``。
        ``guard`` 在**凭据解析之后、真正下发之前**执行，返回 False 表示本次
        下发已不属于当前会话（调用方据此走 stale 分支）。

        为什么要收成一处（2026-09-25 instance-8000 实测）：旧实现在停止路径
        与开播前清理各写一份下播逻辑，停止那一份是

            csrf = self.api.get_csrf()
            if csrf:            # ← 没有 else
                ...

        凭据取不到就**静默跳过**、一行日志都不写。当天 20:10、20:12、21:39
        三次"停止未确认"因此完全无法归因（既没有 API 失败日志，也没有
        "下播已确认"），其中 21:39 那次让自动轮转暂停了 89 分 24 秒。
        现在只有一条路径，且每个出口都写日志与诊断。
        """
        if not room_id:
            return True, 'no_room'
        csrf, csrf_reason = self._resolve_csrf()
        if not csrf:
            logger.warning(
                '平台下播未下发：凭据不可用（%s，room=%s）。本地推流已停止，'
                '但平台侧下播未确认，保留待办供重试。', csrf_reason, room_id)
            diagnostics.get_diagnostics().record(
                'platform_stop_skipped', room_id=room_id, reason=csrf_reason)
            return False, f'csrf_{csrf_reason}'
        if guard is not None and not guard():
            diagnostics.get_diagnostics().record(
                'platform_stop_abandoned', room_id=room_id,
                reason=PLATFORM_STOP_STALE)
            return False, PLATFORM_STOP_STALE
        try:
            # 退出预算传播：`retry_network_until_cancelled` 是**线程本地**
            # 上下文，shutdown 在别的线程里包一层并不会传给执行到这里的
            # LiveStop 线程。必须在这里显式用同一个取消信号包住平台下播，
            # 退出时在途重试才会立即结束，而不是把多次退避叠到退出预算上。
            #
            # 这个信号必须是 _shutdown_cancel（只在服务退出时置位），**不能**
            # 是 _startup_cancel：用户点停止时 stop_streaming 会先置位
            # _startup_cancel，用它包自己的下播请求会让这次下播在发出之前
            # 就被自己取消（_req 返回 {"cancelled": True}），平台**从未收到**，
            # 房间继续挂"在播"，下一次开播又被挡回 —— 重启前死锁。
            if getattr(self, '_shutdown_in_progress', False):
                # Test adapters and alternate platform clients may override
                # stop_live but inherit BilibiliApi's helper. In that case
                # their own stop_live remains the correct boundary.
                bounded_stop = (getattr(self.api, 'stop_live_for_shutdown', None)
                                if type(self.api) is BilibiliApi else None)
                result = (bounded_stop(room_id, csrf) if callable(bounded_stop)
                          else self.api.stop_live(room_id, csrf))
            else:
                result = self._with_shutdown_cancel(
                    lambda: self.api.stop_live(room_id, csrf))
        except Exception as exc:
            logger.warning('平台下播请求异常（本地进程仍会停止）：%s', exc)
            diagnostics.get_diagnostics().record(
                'platform_stop_error', room_id=room_id, reason=str(exc))
            return False, f'error:{exc}'
        if not self._platform_stop_ok(result):
            logger.warning('平台下播未确认：接口返回失败（room=%s, result=%r）',
                           room_id, result)
            diagnostics.get_diagnostics().record(
                'platform_stop_rejected', room_id=room_id,
                reason=str(result)[:200])
            return False, 'rejected'
        logger.info('平台下播已确认下发（room=%s）', room_id)
        return True, 'ok'

    def _cleanup_confirmed(self) -> bool:
        """本次停止是否**确认**完成：owned 进程已回收、平台下播已下发。

        必须基于实际状态判定，不能因为清理函数"返回了"（或执行到了 finally）
        就当作完成：回收失败时引用仍在且 _ffmpeg_unrecycled 为真，此时若标记
        完成，重复停止会被"已完成"分支短路，连一次重试机会都没有。
        """
        if self._process_alive(self.video_process):
            return False
        if self._ffmpeg_unrecycled:
            return False
        return bool(self._platform_stop_done)

    def _stop_cleanup_context(self):
        """当前线程的停止所有者快照；网络等待期间禁止换成此刻的新会话。"""
        context = self.__dict__.get('_stop_cleanup_local')
        if context is None:
            context = threading.local()
            self._stop_cleanup_local = context
        return context

    def _begin_stop_cleanup_context(self, owner: dict = None):
        context = self._stop_cleanup_context()
        if owner is None:
            with self._start_mutex():
                owner = self._capture_stop_owner_locked()
        context.owner = owner
        context.stale = False
        context.last_stale = False
        return context

    def _capture_stop_owner_locked(self) -> dict:
        """调用方持有 ``_start_mutex`` 时冻结会话身份。"""
        return {
            'start_intent_id': getattr(self, '_start_intent_id', None),
            'pending_run_id': getattr(self, '_pending_run_id', ''),
            'current_run_id': getattr(self, '_current_run_id', ''),
            'state_run_id': getattr(self.state, 'run_id', ''),
            'room_id': getattr(self, 'current_room_id', None),
            'instruction': getattr(self, 'current_instruction', None),
            'stream_mode': getattr(self, '_stream_mode', None),
            'platform_stop_done': bool(getattr(self, '_platform_stop_done', False)),
            'video_process': getattr(self, 'video_process', None),
        }

    def _stop_owner_matches_locked(self, owner: dict) -> bool:
        """当前状态仍归 stop 调用入口冻结的会话所有。"""
        return (
            getattr(self, '_start_intent_id', None) == owner['start_intent_id']
            and getattr(self, '_pending_run_id', '') == owner['pending_run_id']
            and getattr(self, '_current_run_id', '') == owner['current_run_id']
            and getattr(self.state, 'run_id', '') == owner['state_run_id']
            and getattr(self, 'current_room_id', None) == owner['room_id']
            and getattr(self, 'current_instruction', None) is owner['instruction']
            and getattr(self, '_stream_mode', None) == owner['stream_mode'])

    def _stop_owner_for_current_thread(self) -> dict:
        context = self._stop_cleanup_context()
        owner = getattr(context, 'owner', None)
        if owner is None:
            with self._start_mutex():
                owner = self._capture_stop_owner_locked()
            context.owner = owner
            context.stale = False
        return owner

    def _stop_cleanup_is_stale(self) -> bool:
        return bool(getattr(self._stop_cleanup_context(), 'last_stale', False))

    def _stop_live_process(self, preserve_state: bool = False):
        """停止直播进程（不 join 线程）
        preserve_state=True: 保留 live_state 以便恢复（任务模式手动停播时使用）
        """
        context = self._stop_cleanup_context()
        owner = self._stop_owner_for_current_thread()
        context.stale = False
        context.last_stale = False
        try:
            return self._stop_live_process_owned(preserve_state, owner, context)
        finally:
            context.last_stale = bool(context.stale)
            context.owner = None
            context.stale = False

    def _stop_live_process_owned(self, preserve_state: bool,
                                 owner: dict, context):
        # 监控退出入口与 stop worker 都可能已经等待了一段时间。先核对冻结的
        # 会话身份，避免迟到的旧清理从入口就开始触碰新会话。
        with self._start_mutex():
            if not self._stop_owner_matches_locked(owner):
                context.stale = True
                return False

        # 通知 FFmpeg 循环线程退出
        self._ffmpeg_stop_event.set()
        # 终止 FFmpeg/视频进程
        if owner['video_process'] is not None:
            self._kill_ffmpeg(expected_process=owner['video_process'])
        with self._start_mutex():
            if not self._stop_owner_matches_locked(owner):
                context.stale = True
                return False
            self.ffmpeg_current_video = ''
        # 等待循环线程退出
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)

        # 平台下播与本地进程回收分开记录：已确认下发的房间不再重复下播
        # （重试"本地回收"时不得顺手把已被新意图接管的房间再下一次播）；
        # 未下发/失败则保留待办，允许后续显式停止重试。
        room_at_stop = owner['room_id']
        platform_stop_done = owner['platform_stop_done']
        if not room_at_stop:
            platform_stop_done = True
        elif not platform_stop_done:
            def _still_owner_for_stop() -> bool:
                # get_csrf / 网络等待期间可能已有新意图接管：那条路径下
                # 不再提交旧房间的下播（原实现同样顺序，这里只把它交给
                # 统一下播入口的 guard 参数执行）。
                with self._start_mutex():
                    return self._stop_owner_matches_locked(owner)

            platform_stop_done, _stop_reason = self._platform_stop_live(
                room_at_stop, guard=_still_owner_for_stop)
            if _stop_reason == PLATFORM_STOP_STALE:
                context.stale = True
                return False

        # 平台请求返回之后，在同一把开播意图锁内复核身份并完成所有共享状态
        # 清理。旧 A 的迟到响应不能覆盖 B 新设的 _platform_stop_done，也不能
        # 清进度计时、current run 或恢复检查点。
        with self._start_mutex():
            if not self._stop_owner_matches_locked(owner):
                context.stale = True
                return False
            self._platform_stop_done = platform_stop_done
            run_at_stop = owner['current_run_id']
            instruction_at_stop = owner['instruction']
            state_matches = self._state_matches_session(
                instruction_at_stop, run_at_stop)
            # 保留实际已播时长（在清除 stream_start_time 之前计算）。
            # 只取**已确认**的有效时长：待确认区间不能落盘（那等于把没观察过的时间
            # 当成已完成进度，重启后会被拿来结算）。数量级仍是单调时钟累计量，
            # 系统时间前跳不会把它写大。
            actual_elapsed = 0
            if (state_matches and self.stream_start_time
                    and instruction_at_stop):
                actual_elapsed = max(int(self._stop_progress_accumulated or 0),
                                     self._confirmed_elapsed(),
                                     # 存档里已有的已确认进度同样不得倒扣
                                     int(float(getattr(self.state, 'effective_seconds',
                                                      0) or 0)))
            self.is_streaming = False
            self.stream_start_time = None
            self._segment_monotonic = None
            clock = self._clock()
            checkpoint_saved = True
            checkpoint_state = state_matches and (
                preserve_state or owner['stream_mode'] == 'task')
            if not checkpoint_state and (
                    not owner['stream_mode'] or owner['stream_mode'] == 'manual'):
                clock.reset(0.0, 'none')
            self._current_run_id = ''

        if checkpoint_state:
            state_lock = getattr(self.state, '_state_io_lock', None)
            with state_lock if state_lock is not None else nullcontext():
                # 状态 I/O 不持有控制锁，避免慢磁盘阻塞新的停止意图。
                # state_io_lock 与冻结 run/task/day 身份一起防止写到新检查点。
                if not self._state_matches_session(
                        instruction_at_stop, run_at_stop):
                    checkpoint_saved = False
                else:
                    self.state.effective_seconds = max(
                        float(self.state.effective_seconds or 0), float(actual_elapsed))
                    self.state.accumulated_seconds = self.state.elapsed_seconds
                    # The live-state streaming flag also owns bootstrap recovery intent:
                    # a task stop/shutdown that preserves progress must remain resumable.
                    self.state.is_streaming = bool(preserve_state)
                    self.state.phase = PHASE_IDLE
                    if preserve_state:
                        self.state.source_mode = SOURCE_RESUME
                    checkpoint_saved = self.state._save_checkpoint('停播恢复状态')
                    if checkpoint_saved:
                        clock.reset(confirmed=self.state.effective_seconds,
                                    origin='resume')
                        logger.info(
                            f" 已保留直播状态以便恢复（已确认有效时长={self.state.elapsed_seconds}秒）")
                    else:
                        error = (getattr(self.state, '_last_save_error', '')
                                 or '状态文件原子写入未确认成功')
                        self.state._record_checkpoint_failure(
                            '停播恢复状态', error)
                        clock.reset(confirmed=self.state.effective_seconds,
                                    origin='resume')
                        logger.error(self.state.resume_blocked_reason)
        # 解除"运行中任务"保护：按**冻结的 run 身份**清理，绝不清掉期间新登记的
        # 那一场（旧实现读"此刻的 _active_run_id"，会把新会话的保护一起清掉）。
        self._release_active_task(run_at_stop)
        return checkpoint_saved

    def _with_shutdown_cancel(self, fn):
        """在**退出**取消上下文内执行 fn；没有该能力的替身直接执行。

        传的是 :attr:`_shutdown_cancel`（只在服务退出时置位），**不是**
        `_startup_cancel`。用户的普通停止也会置位 `_startup_cancel`，
        拿它包自己的下播调用等于让这次下播自我取消——`_req` 会在发请求
        之前就返回 ``{"code": -1, "msg": "操作已取消", "cancelled": True}``，
        平台根本收不到下播。方法名说的本来就是 shutdown，这里让它名副其实。
        """
        factory = getattr(self.api, 'retry_network_until_cancelled', None)
        if not callable(factory):
            return fn()
        with factory(self._shutdown_cancel):
            return fn()

    def stop_streaming(self, user_initiated: bool = True) -> bool:
        """立即接受停止请求，后台取消等待并清理，不让下播网络请求阻塞面板。"""
        logger.info("  停止直播...")
        with self._start_lock:
            if user_initiated:
                self._last_user_stop_source_epoch = self._control_epoch
                # 在任何网络清理/线程等待之前持久化意图，即使有开播请求尚未返回。
                self.state.auto_resume = False
                try:
                    (self.state.state_file.parent / '.no_auto_resume').write_text(
                        'user stop\n', encoding='utf-8')
                except (OSError, AttributeError) as exc:
                    logger.warning("无法写入人为停止标记：%s", exc)
                try:
                    self.state.save()
                except Exception as exc:
                    # 状态存储异常也不能阻止本次实际停止；标记可独立保护重启。
                    logger.error("无法保存人为停止意图，继续停止当前直播：%s", exc)
            # 让此前发起、尚未完成的所有控制操作（含正在选任务的请求）失效。
            self._control_epoch += 1
            # 停止接受后立即撤销当前待验证意图；迟到的旧线程还会按冻结的
            # face run/epoch 再核对一次，不能误清后续新意图。
            if self._pending_face_verify:
                self._pending_face_verify = False
                self._face_verify_url = ''
                self._face_verify_run_id = ''
                self._face_verify_epoch = None
                self._face_verify_intent = None
                self._recovery_blocked = ''
            self._queued_face_verify_retry_intent = None
            self._startup_cancel.set()
            self._start_cancel.set()
            self.stop_monitor.set()
            self._ffmpeg_stop_event.set()
            diagnostics.get_diagnostics().record(
                'stop_accepted', source=('user' if user_initiated else 'shutdown'), epoch=self._control_epoch,
                zone=(self.current_instruction.zone_name if self.current_instruction else ''))
            if self._is_starting:
                # 已有清理或开播在进行：停止是幂等的，重复请求不再派发清理。
                return True
            if not self.is_streaming and self._stop_cleanup_done:
                # 该停止意图的清理已经完成，且此后没有新的在播会话：
                # 重复的停止只确认既有结果，不再提交一次平台下播。
                logger.info("重复的停止请求：清理已完成且无新的在播会话，只确认结果")
                return True
            self._is_starting = True
            self._stop_cleanup_done = False
            stop_owner = self._capture_stop_owner_locked()
            self._start_thread = threading.Thread(
                target=self._stop_in_background,
                args=(user_initiated, stop_owner),
                name="LiveStop", daemon=True)
            self._start_thread.start()
        return True

    def _stop_in_background(self, user_initiated: bool = False,
                            stop_owner: dict = None):
        stop_context = self._begin_stop_cleanup_context(stop_owner)
        worker_thread = threading.current_thread()
        slot_was_unassigned = getattr(self, '_start_thread', None) is None

        def owns_stop_slot():
            current = getattr(self, '_start_thread', None)
            return (current is worker_thread
                    or (slot_was_unassigned and current is None))

        cleanup_confirmed = False
        state_checkpointed = True
        same_session_already_cleaned = False
        # 停止结果的事实（分区/已播/会话身份）必须在清理**之前**捕获：清理会
        # 清掉 stream_start_time 与 current_instruction。
        zone_at_stop = (self.current_instruction.zone_name
                        if self.current_instruction
                        else getattr(self.state, 'current_zone', '') or '')
        elapsed_at_stop = self._confirmed_elapsed() if self.is_streaming else 0
        # 被停那场的会话 run：正常在播是 _current_run_id；**人脸验证挂起**的
        # 意图还没接管（run 仍在 _pending_run_id），停止时交接也要按它收口，
        # 否则那份已确认的 pending 会永远留在盘上（R4）。
        run_at_stop = str(getattr(self, '_current_run_id', '')
                          or getattr(self, '_pending_run_id', '') or '')
        try:
            owned_process = stop_context.owner['video_process']
            if owned_process is not None:
                self._kill_ffmpeg(expected_process=owned_process)
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=3.0)
            state_checkpointed = self._stop_live_process(
                preserve_state=self._stream_mode == 'task') is not False
            with self._start_mutex():
                if not owns_stop_slot():
                    return
                if self._stop_cleanup_is_stale():
                    same_session_already_cleaned = (
                        self._start_intent_id
                        == stop_context.owner['start_intent_id']
                        and self._pending_run_id
                        == stop_context.owner['pending_run_id']
                        and self._current_run_id == ''
                        and self.current_instruction
                        is stop_context.owner['instruction']
                        and self.current_room_id
                        == stop_context.owner['room_id'])
                    if not same_session_already_cleaned:
                        return
                    # 监控线程已先完成同一场的下播。只收尾这个 stop worker 的
                    # 占用标志，不再重做状态清理，也不影响后续新开播意图。
                    cleanup_confirmed = self._cleanup_confirmed()
                    # 本 worker 的重复清理因所有权已释放而返回 False；这不等于
                    # 监控线程保存检查点失败。只依据状态对象的明确失败标记报告。
                    state_checkpointed = not bool(getattr(
                        self.state, '_checkpoint_failure_active', False))
                else:
                    self.current_instruction = None
            logger.info("直播已完全停止")
            if not same_session_already_cleaned:
                cleanup_confirmed = self._cleanup_confirmed()
            # 停止收尾：属于被停那场的交接写成"未执行"（幂等，终态）。
            # 此刻已无在途开播线程能让它的目标接管；按目标 run 匹配即可——
            # 控制代际已被停止推进，记录里的旧代际本来就是它自己的身份。
            # 推迟接管记录（FFmpeg 未就绪窗口）一并清掉。
            try:
                if run_at_stop:
                    self._finalize_handover('void',
                                            target_run_id=str(run_at_stop))
                    self._discard_deferred_takeover(None, run_at_stop)
            except Exception:
                logger.debug('停止收尾的交接结论写入异常', exc_info=True)
        except Exception:
            # 异常路径不是"回收成功"：保留失败状态，后续显式停止仍可重试。
            logger.exception("后台停止直播异常")
            cleanup_confirmed = False
        finally:
            # 只有确认回收（owned 进程已退出、平台下播已下发）才记"清理完成"。
            # 未确认回收/异常时保留失败状态：重复停止必须还能再试一次，不得被
            # "已完成"分支短路（否则用户第二次点停止连回收都不会执行）。
            # 先写标记再复位 _is_starting：避免重复停止在这两步之间看到
            # "既没有清理在进行、又没有清理完成"而重复派发。
            worker_still_owns_slot = False
            try:
                with self._start_mutex():
                    worker_still_owns_slot = (
                        owns_stop_slot()
                        and (not self._stop_cleanup_is_stale()
                             or same_session_already_cleaned))
                    if worker_still_owns_slot:
                        self._stop_cleanup_done = (
                            cleanup_confirmed and self._cleanup_confirmed())
                        self._is_starting = False
            except Exception:
                if worker_still_owns_slot:
                    self._stop_cleanup_done = False
            # 面板主动停止的结果只留在面板，包括清理和进度保存异常；
            # 后台发起的停止仍沿用原有通知语义。
            try:
                if worker_still_owns_slot:
                    stage = ('pending_recycle' if not self._stop_cleanup_done
                             else 'checkpoint_failed' if not state_checkpointed
                             else 'stopped')
                    reasons = []
                    if not self._stop_cleanup_done:
                        reasons.append('仍有资源未确认回收，可再次点击停止重试')
                    if not state_checkpointed:
                        reasons.append(getattr(
                            self.state, 'resume_blocked_reason', '')
                            or '恢复进度未能写入状态文件，已阻断自动续播')
                    reason = '；'.join(reasons)
                    outcome = ('停止未确认' if not self._stop_cleanup_done
                               else '直播已停止')
                    self._push_backend_event(
                        '停播', 'warning' if stage != 'stopped' else 'info',
                        f'{outcome}（{zone_at_stop or "未知分区"}）' + (
                            f'；{reason}' if reason else ''), notify=False)
                    # 只有**已确认的停止**才允许安静，且仅限这两种：
                    #   * 计划内关停 + 自动恢复 = 一次维护过渡，不必打扰；
                    #   * 面板主动停止且已确认 = 用户刚按的，面板已即时确认。
                    # 其余一律通知。尤其 pending_recycle（清理未确认）与
                    # checkpoint_failed 是**需要人工处理**的状态——面板上写着
                    # "可再次点击停止重试"，若压在面板里，只看邮件的人会以为
                    # 已经干净停止（2026-09-25 instance-8000：20:10 与 20:12
                    # 两次面板停止都属未确认，却一封邮件都没有）。
                    # 旧条件 `not user_initiated and (...)` 把面板停止的**所有**
                    # 结果一并压掉，与本文件既有的
                    # `test_user_stop_emits_stopped_event_and_notification`
                    # （"停止必须发出结果通知"）本就矛盾。
                    quiet = stage == 'stopped' and (
                        user_initiated
                        or getattr(self, '_shutdown_in_progress', False))
                    if not quiet:
                        self._notify_stopped(zone_at_stop or '未知分区', stage,
                                             elapsed_at_stop, reason)
            except Exception:
                logger.debug("停止事件/通知异常（不影响停止结果）", exc_info=True)

    def run_next_task(self, epoch: int = None,
                      completion_context: Optional[dict] = None) -> bool:
        """执行下一个直播任务（供 API 调用）

        epoch 为 None 时取当前代际；调用方（例如 API）可以在选任务这类可能
        长时间等待的操作开始前登记代际，停止生效后旧结果必须作废。
        """
        logger.info("=" * 70)
        logger.info(" 开始执行新任务")
        logger.info("=" * 70)

        if epoch is None:
            epoch = self._control_epoch

        # 跨日屏障：**所有**选任务入口都要先确认目标业务日已发布。旧实现只在
        # is_resetting() 为真时才等一次，而且等待失败还继续往下走——调度线程
        # 尚未开始时该标志本来就是 False，于是直接拿着上一日的 today_done 与
        # 排序选任务。等待有界、可取消；失败即暂停，不用旧日快照继续。
        if not self._ensure_day_ready(epoch):
            logger.warning("跨日屏障未通过：暂停本次选任务，不以旧日快照继续")
            self._push_backend_event(
                '重置', 'warning',
                '每日重置未在预算内就绪（或期间已停止），已暂停自动选任务；'
                '请稍后在面板手动继续')
            if completion_context:
                stale = (not self._is_epoch_current(epoch)
                         or self._start_cancel.is_set())
                self._send_task_rotation_result({
                    'completion': dict(completion_context),
                    'instruction': None,
                    'epoch': epoch,
                    'reason': ('停止已生效，尚未选出下一任务' if stale else
                               '每日重置屏障未就绪，尚未选出下一任务'),
                    'identity': f"{completion_context.get('task_id', '')}:"
                                f"{completion_context.get('execution_date', '')}",
                }, 'cancelled' if stale else 'failed')
            return False
        if not self._is_epoch_current(epoch):
            logger.info("等待每日重置期间停止生效，放弃本次选任务结果")
            self._push_backend_event('停止', 'info', '停止已生效，放弃之前发起的选任务请求')
            return False

        instruction = self.task_manager.select_next_instruction() if self.task_manager else None
        # 选择调用可能等待跨日/数据库屏障；先复核代际，再判断空结果是不是
        # 真的“没有待办”，避免把停止期间的迟到空响应写成任务清空。
        if not self._is_epoch_current(epoch):
            logger.info("选任务返回时停止已生效，放弃本次结果，不重新开播")
            self._push_backend_event('停止', 'info', '停止已生效，放弃之前发起的选任务请求')
            diagnostics.get_diagnostics().record(
                'run_next_dropped_stale_epoch', epoch=epoch,
                current=self._control_epoch,
                zone=getattr(instruction, 'zone_name', ''))
            if completion_context:
                self._send_task_rotation_result({
                    'completion': dict(completion_context),
                    'instruction': None,
                    'epoch': epoch,
                    'reason': '停止已生效，尚未选出下一任务',
                    'identity': f"{completion_context.get('task_id', '')}:"
                                f"{completion_context.get('execution_date', '')}",
                }, 'cancelled')
            return False
        if not instruction:
            logger.warning(" 无待执行任务")
            if completion_context:
                self._send_task_rotation_result({
                    'completion': dict(completion_context),
                    'instruction': None,
                    'epoch': epoch,
                    'identity': f"{completion_context.get('task_id', '')}:"
                                f"{completion_context.get('execution_date', '')}",
                }, 'none')
            return False

        if not completion_context:
            return self.start_streaming(instruction, '', epoch=epoch,
                                        source=SOURCE_NEW)

        rotation_context = {
            'completion': dict(completion_context),
            'instruction': instruction,
            'epoch': epoch,
            'identity': f"{completion_context.get('task_id', '')}:"
                        f"{completion_context.get('execution_date', '')}",
        }
        with self._task_rotation_lock:
            self._task_rotation_pending = rotation_context
        accepted = self.start_streaming(instruction, '', epoch=epoch,
                                        source=SOURCE_NEW)
        if not accepted:
            with self._task_rotation_lock:
                still_pending = self._task_rotation_pending is rotation_context
                if still_pending:
                    self._task_rotation_pending = None
            if still_pending:
                stale = (not self._is_epoch_current(epoch)
                         or self._start_cancel.is_set())
                rotation_context['reason'] = (
                    '停止已生效，开播请求已取消' if stale else
                    '开播请求未被接受')
                self._send_task_rotation_result(
                    rotation_context, 'cancelled' if stale else 'failed')
        return accepted

    def get_qrcode_for_login(self) -> dict:
        """获取登录二维码数据（供 API 使用）"""
        result = self.get_qrcode_data()
        if result:
            return {'success': True, 'data': result}
        return {'success': False, 'message': '获取二维码失败'}

    def _auto_resume(self):
        """启动时自动恢复上次中断的直播（系统更新/重启后）"""
        self._apply_operator_stop_marker()
        if not self.state.auto_resume:
            return
        # 代际在进入等待之前登记：等待期间若发生停止，本次自动恢复必须作废。
        epoch = self._control_epoch
        if self._startup_cancel.wait(3):
            return
        if not self.state.auto_resume or not self._is_epoch_current(epoch):
            return
        try:
            # 与手动 /resume、验证后重试**同一份**判定：旧实现直接拿存档拼指令，
            # 于是"任务已删除后同名重建/已改名/已完成/执行日已过期"都会被受理，
            # 服务重启反而开播了本应拒绝的那条任务。
            instruction, reason = self.resolve_resume_target()
            if instruction is None:
                logger.warning(f"跳过自动恢复：{reason}")
                self._push_backend_event('恢复', 'warning', f'未自动恢复：{reason}')
                return
            zone = instruction.zone_name
            dur = int(instruction.duration_seconds or 0)
            # 继承量取**当前存档**的已播秒数（身份已核对通过），不重新抽时长、
            # 不归零；冻结后交给 start_streaming，等待期间状态被改写也不影响。
            inherit = max(0, int(self.state.elapsed_seconds))
            logger.info(f"检测到未完成直播，自动恢复：{zone}（剩余{dur // 60}分钟）")
            # 检查分区是否有效
            if self.area_loader.get_area_id(zone, auto_update=False) is None:
                logger.warning(f"分区 '{zone}' 已不存在于分区列表，跳过自动恢复")
                return
            ok = self.start_streaming(instruction, '', is_task_mode=True,
                                      epoch=epoch, source=SOURCE_RESUME,
                                      inherit_elapsed=inherit)
            if ok:
                logger.info("自动恢复请求已提交，后台等待开播")
            else:
                logger.warning("自动恢复失败")
        except Exception as e:
            logger.error(f"自动恢复异常：{e}")

    def shutdown(self):
        """受控退出：分阶段记录耗时，并遵守一个**总体** deadline。

        退出链路包含四类可能无限等待的外部动作：在途 HTTP、平台下播、FFmpeg
        退出、线程回收。这里给整体设预算，每步只允许花掉剩余额度：
        外部接口永不返回时仍然受控退出，且保留进度与"人为停止"意图。

        未在预算内回收的资源必须留下证据（``_ffmpeg_unrecycled`` 与诊断记录），
        不填成功、也不据此修改 systemd 的 KillMode / TimeoutStopSec。
        """
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        timings: Dict[str, float] = {}

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def phase(name: str, started: float) -> None:
            timings[name] = round(time.monotonic() - started, 3)

        logger.info(" 正在关闭直播控制模块...（总预算 %.0fs）", SHUTDOWN_BUDGET_SECONDS)

        started = time.monotonic()
        with self._start_mutex():
            self._shutdown_in_progress = True
            # 退出取消信号：与 _startup_cancel 分开置位，这样"在途重试"能被
            # 退出打断，而**普通的用户停止不会**把自己正在发的下播请求取消掉。
            self._shutdown_cancel.set()
            # shutdown 也使尚未进入 streaming 的待启动意图失效，避免 finally
            # 把 B 当成可恢复状态写到仍属于 A 的存档上。
            self._control_epoch += 1
            if self._pending_face_verify:
                self._pending_face_verify = False
                self._face_verify_url = ''
                self._face_verify_run_id = ''
                self._face_verify_epoch = None
                self._face_verify_intent = None
            self._queued_face_verify_retry_intent = None
            self._startup_cancel.set()
            self._start_cancel.set()
            self.stop_monitor.set()
            self._ffmpeg_stop_event.set()
        phase('signals', started)

        # 平台下播/在途请求纳入同一个取消上下文：挂了的外部调用可被打断，
        # 不会把 3 次 × 10 秒的最坏情况叠到退出预算上。
        started = time.monotonic()
        try:
            with self.api.retry_network_until_cancelled(self._startup_cancel):
                if self.is_streaming:
                    self.stop_streaming(user_initiated=False)
                if self._start_thread and self._start_thread.is_alive():
                    self._start_thread.join(timeout=min(15.0, remaining()))
                if self.monitor_thread and self.monitor_thread.is_alive():
                    self.monitor_thread.join(timeout=min(5.0, remaining()))
        except Exception:
            logger.exception("退出阶段停止直播异常（继续回收资源）")
        phase('stop_and_drain', started)

        started = time.monotonic()
        # 回收同样受总预算约束：剩余额度不足时不再等满默认的 5 秒。
        try:
            self._kill_ffmpeg(timeout=max(0.1, min(5.0, remaining())))
        except TypeError:
            # 替身/旧调用方不接受关键字参数：退出链路的可达性优先。
            self._kill_ffmpeg()
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=min(3.0, remaining()))
        phase('ffmpeg_recycle', started)

        started = time.monotonic()
        try:
            diagnostics.get_diagnostics().record(
                'shutdown_timings', budget=SHUTDOWN_BUDGET_SECONDS,
                timings=timings, ffmpeg_unrecycled=bool(self._ffmpeg_unrecycled),
                process_alive=bool(self._process_alive(self.video_process)),
                over_budget=time.monotonic() > deadline)
        except Exception as exc:
            logger.debug(f" 关闭诊断记录异常：{exc}")
        # 诊断收尾在慢磁盘上可能长时间不返回：放到独立线程里按剩余预算等待，
        # 超时就放弃等待（记录告警），绝不把退出拖出预算。
        diag = diagnostics.get_diagnostics()
        finished = threading.Event()

        def _finish_diagnostics():
            try:
                diag.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f" 关闭诊断异常：{exc}")
            finally:
                finished.set()

        threading.Thread(target=_finish_diagnostics, name='DiagShutdown',
                         daemon=True).start()
        if not finished.wait(max(0.0, remaining())):
            logger.warning('诊断收尾超出退出预算，已放弃等待（不影响直播资源回收）')
        phase('diagnostics', started)

        if self._ffmpeg_unrecycled or self._process_alive(self.video_process):
            logger.error(
                "退出时仍有未回收的推流进程（已保留证据，不填成功）："
                "ffmpeg_unrecycled=%s", self._ffmpeg_unrecycled)
        total = round(time.monotonic() - (deadline - SHUTDOWN_BUDGET_SECONDS), 3)
        logger.info(" 直播控制模块已关闭 | 阶段耗时 %s | 实测总耗时 %.3fs（预算 %.0fs）",
                    timings, total, SHUTDOWN_BUDGET_SECONDS)
