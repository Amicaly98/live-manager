"""
email_sender.py - 推送通知模块

支持：SMTP 邮件、Server酱（微信推送）、每日简报。
人脸验证确认链接走 /api/email/confirm-face-verify（当前服务端口，无需额外放行端口）。

设计要点（2026-09-16 收口）：

- **事件分类**：调用方传结构化事件 ID 与不可变业务快照，通知器不再解析中文
  message 决定行为（历史缺陷：模板措辞一变，重连/完成通知就静默失效）。
- **开关**：总开关 → 事件开关 → 渠道开关，三级都生效；UI 上"推送通知"关掉后
  所有渠道都不再发。
- **有界资源**：固定 worker + 有界队列，失败只影响通知；同一故障的持续恢复
  事件按事件身份合并，不每轮新建线程。
- **真实结果**：测试通知返回 disabled/queued/sent/failed，不把排队当投递成功。
- **确认链接**：token 绑定会话身份（run/room/epoch）、用途与有效期；GET 只预览，
  显式 POST 才消费；停止或会话更替后旧链接失效。
"""

import html as _html
import ipaddress
import json
import math
import os
import queue
import re
import smtplib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import requests as _requests

logger = logging.getLogger(__name__)


def _normalize_public_host(value: str) -> str:
    """Accept a domain or IP only; never turn a URL/path into a confirmation link."""
    host = str(value or '').strip()
    if not host or re.search(r'[\s/?#@\\]', host) or '://' in host:
        return ''
    try:
        address = ipaddress.ip_address(host.strip('[]'))
        # A confirmation endpoint must be reachable from the email recipient's
        # network. Reject private, reserved, documentation and other non-global
        # literals; hostnames are accepted without synchronous DNS resolution.
        if not address.is_global:
            return ''
        return f'[{address.compressed}]' if address.version == 6 else address.compressed
    except ValueError:
        pass
    # A colon in a non-IP value is a port or malformed IPv6 literal. Ports belong
    # to API_PORT so the link always targets the actual instance listener.
    if ':' in host or host.startswith('[') or host.endswith(']'):
        return ''
    try:
        ascii_host = host.encode('idna').decode('ascii').lower().rstrip('.')
    except UnicodeError:
        return ''
    if len(ascii_host) > 253:
        return ''
    labels = ascii_host.split('.')
    if ascii_host == 'localhost' or ascii_host.endswith('.localhost'):
        return ''
    if not labels or any(
            len(label) > 63
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label)
            for label in labels):
        return ''
    return ascii_host


# ==================== 事件分类 ====================

EVENT_START_ACCEPTED = 'start_accepted'      # 控制请求受理，未证明媒体输出
EVENT_STREAM_RUNNING = 'stream_running'      # 已进入运行阶段（区分新开/恢复/手动）
EVENT_RECOVERING = 'recovering'              # 持续恢复中（同一故障合并）
EVENT_RECOVERED = 'recovered'                # 本轮恢复条件确实达成
EVENT_ACTION_REQUIRED = 'action_required'    # 需人工处理：验证/登录失效/明确拒绝
EVENT_STOPPED = 'stopped'                    # 真实停止结果
EVENT_TASK_COMPLETE = 'task_complete'        # 结算已提交
EVENT_TASK_ROTATION = 'task_rotation'        # 自然完成后与下一任务结果合并
EVENT_DAILY_SUMMARY = 'daily_summary'        # 指定统计日的一致快照
EVENT_TEST = 'test'                          # 仅渠道测试
EVENT_LOCAL_FAIL = 'local_stream_fail'       # 本地推流（FFmpeg）持续故障

#: 事件 → 生效的事件开关（``_load_config`` 返回的键名）；None = 不受事件开关限制。
EVENT_SWITCH = {
    EVENT_START_ACCEPTED: 'notify_start',
    EVENT_STREAM_RUNNING: 'notify_start',
    EVENT_RECOVERING: 'notify_error',
    EVENT_RECOVERED: 'notify_error',
    EVENT_ACTION_REQUIRED: 'notify_error',
    EVENT_STOPPED: 'notify_stop',
    EVENT_TASK_COMPLETE: 'notify_complete',
    EVENT_TASK_ROTATION: 'notify_complete',
    EVENT_DAILY_SUMMARY: 'daily_summary',
    EVENT_TEST: None,
    EVENT_LOCAL_FAIL: 'notify_error',
}

#: 事件 → 限频秒数（按事件+去重键分别计算，不同错误互不压制）。
EVENT_COALESCE_SECONDS = {
    EVENT_START_ACCEPTED: 60,
    EVENT_STREAM_RUNNING: 60,
    EVENT_RECOVERING: 300,
    EVENT_RECOVERED: 60,
    EVENT_ACTION_REQUIRED: 300,
    EVENT_STOPPED: 30,
    EVENT_TASK_COMPLETE: 60,
    EVENT_TASK_ROTATION: 300,
    EVENT_DAILY_SUMMARY: 3600,
    EVENT_TEST: 0,
    EVENT_LOCAL_FAIL: 300,
}

#: 测试通知等待投递的最长时间（秒）：超时返回 failed，不伪称已发送。
TEST_DELIVERY_TIMEOUT = 20.0


def escape_html(value) -> str:
    """模板里插入的任务名/房间/实例都可能是用户可控内容。"""
    return _html.escape(str(value if value is not None else ''), quote=True)


# ==================== 通知任务 ====================

@dataclass
class _NotificationJob:
    event_id: str
    subject: str
    body: str
    html: bool
    dedup_key: str
    created_at: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event)
    results: Dict[str, str] = field(default_factory=dict)
    ordering_key: str = ''
    predecessor: Optional[threading.Event] = None


# ==================== EmailSender ====================

class EmailSender:
    """推送通知器：SMTP 邮件 + Server酱微信推送 + 分类限频 + 有界异步"""

    MAX_QUEUE = 200
    WORKERS = 2
    #: 限频表上界（按 event+dedup_key 计）：防止按 run_id 的历史字典无限增长。
    MAX_RATE_ENTRIES = 2048

    def __init__(self):
        self._last_send: Dict[Tuple[str, str], float] = {}
        self._rate_lock = threading.Lock()
        self._face_verify_tokens: Dict[str, dict] = {}
        self._token_lock = threading.Lock()
        self._queue: 'queue.Queue[_NotificationJob]' = queue.Queue(maxsize=self.MAX_QUEUE)
        self._enqueue_lock = threading.Lock()
        self._ordering_lock = threading.Lock()
        self._ordered_tails: Dict[str, threading.Event] = {}
        self._accepting = True
        self._workers: list = []
        self._closing = threading.Event()
        self._counters = {
            'queued': 0, 'sent': 0, 'failed': 0, 'dropped': 0, 'coalesced': 0,
            'unconfirmed': 0,
        }
        self._channel_counters = {
            'email': {'sent': 0, 'failed': 0},
            'serverchan': {'sent': 0, 'failed': 0},
        }
        self._counter_lock = threading.Lock()
        # 主机地址显式配置优先；元数据探测只在后台做一次，不阻塞主直播路径。
        self._host_cache: Optional[str] = None
        self._host_probe_started = False
        self._host_resolution_error = ''
        self._start_workers()

    # ---------- 配置读取 ----------

    def _get_server_host(self) -> str:
        """服务器公网地址（settings → 环境变量 → 缓存的元数据 → localhost）。

        云元数据探测**不在调用线程**上做：主机事件可能是监控/控制线程，
        一次 2 秒的探测会连带拖住控制器（历史缺陷）。
        """
        try:
            from app.api.settings import load_settings
            configured = str(getattr(load_settings(), 'server_host', '') or '').strip()
            if configured:
                # 显式配置错误时 fail closed；不能悄悄拼出假 URL 或绕过配置。
                host = _normalize_public_host(configured)
                self._host_resolution_error = '' if host else 'invalid_config'
                return host
        except Exception:
            pass
        env_value = os.environ.get('SERVER_PUBLIC_HOST', '').strip()
        if env_value:
            host = _normalize_public_host(env_value)
            self._host_resolution_error = '' if host else 'invalid_config'
            return host
        if self._host_cache:
            host = _normalize_public_host(self._host_cache)
            self._host_resolution_error = '' if host else 'invalid_config'
            return host
        self._schedule_host_probe()
        self._host_resolution_error = 'missing'
        return 'localhost'

    def warmup_host(self) -> None:
        """启动时预热公网地址解析（消除"首封验证邮件没有确认链接"的竞态）。

        确认链接由 :meth:`_confirm_url` 依赖 :meth:`_get_server_host` 的结果。
        缓存为空时该方法**不阻塞**：它把探测丢到后台线程后立刻返回 localhost，
        于是"正文已拼好、探测才刚成功"的窗口必然存在。

        2026-09-25 现场（instance-8000，重启后第一次需要验证链接）：
        19:27:43,714 拿到验证 URL → 正文在 43,714–43,727 之间构建（缓存此刻为空）
        → 19:27:43,727 探测才完成 → 邮件里没有确认链接。该日志每进程只出现一次，
        即**重启后的第一封验证邮件必然缺链接**。

        启动时调用一次，把竞态前移到进程初始化阶段；探测仍在后台线程，
        不拖慢启动。失败不影响启动——后续调用仍会重新调度探测。
        """
        try:
            self._get_server_host()
        except Exception as exc:            # 预热失败不得影响服务启动
            logger.debug('公网地址预热失败（不影响启动）：%s', exc)

    def disable_cloud_host_probe(self, reason: str = '') -> None:
        """**桌面装配用**：明确关掉云元数据探测（默认不调用 = 服务端语义不变）。

        服务端跑在云服务器上，``100.100.100.200`` 的元数据端点是那台机器合法地
        拿到自己公网地址的方式；桌面客户端跑在用户自己的电脑上，那个地址既不存在
        也不该被访问——为一个**根本不会存在的**远程确认链接去连它，只是无谓的
        外网试探。

        这里**不**改变共享语义：探测仍然是"未禁用时的既有行为"，禁用只是让
        :meth:`_schedule_host_probe` 立刻返回；没有可用地址时 :meth:`_get_server_host`
        照旧走"配置 → 环境变量 → 缓存 → 不可用"，确认链接也照旧按
        ``host_usable`` 判断（不可用就只在正文里写"请在面板确认"，不拼 localhost
        假链接）。

        ``_host_probe_started = True`` 的语义是"已经判过、不需要再探"，不是
        "已经探到"。禁用原因只用于日志（本方法内不打日志的调用方可见），不依赖
        ``_host_resolution_error``——那是"最近一次地址解析"的结论字段，
        :meth:`_get_server_host` 每次都会重写它，拿它当禁用标记会被冲掉。
        """
        self._host_probe_started = True
        self._host_resolution_error = reason or 'cloud_probe_disabled'

    def _schedule_host_probe(self) -> None:
        if self._host_probe_started or self._closing.is_set():
            return
        self._host_probe_started = True
        threading.Thread(target=self._probe_host, daemon=True,
                         name='HostProbe').start()

    def _probe_host(self) -> None:
        for path in ('eipv4', 'public-ipv4'):
            try:
                resp = _requests.get(
                    f'http://100.100.100.200/latest/meta-data/{path}', timeout=2)
                if resp.ok and resp.text.strip() and '404' not in resp.text:
                    host = _normalize_public_host(resp.text.strip())
                    if host:
                        self._host_cache = host
                        logger.info('已缓存服务器公网地址（后台探测）')
                        return
            except Exception:
                continue
        logger.debug('后台探测公网地址无结果，继续按配置/localhost 生成链接')

    def _load_config(self) -> dict:
        """读取推送配置"""
        try:
            from app.api.settings import load_settings
            s = load_settings()
            return {
                # 总开关：关闭后所有渠道都不再发送（历史缺陷：UI 总开关只管邮件）
                'master_enabled': bool(getattr(s, 'notification_enabled', True)),
                # 推送渠道：'email' | 'serverchan' | 'both'
                'channel': getattr(s, 'notification_channel', 'email'),
                # 邮箱
                'email_enabled': getattr(s, 'email_enabled', False),
                'smtp_host': getattr(s, 'email_smtp_host', 'smtp.qq.com'),
                'smtp_port': getattr(s, 'email_smtp_port', 587),
                'smtp_user': getattr(s, 'email_smtp_user', ''),
                'smtp_pass': getattr(s, 'email_smtp_pass', ''),
                'recipients': getattr(s, 'email_recipients', ''),
                # Server酱
                'serverchan_sendkey': getattr(s, 'serverchan_sendkey', ''),
                # 通知事件开关
                'notify_start': getattr(s, 'email_notify_start', True),
                'notify_stop': getattr(s, 'email_notify_stop', True),
                'notify_error': getattr(s, 'email_notify_error', True),
                'notify_complete': getattr(s, 'email_notify_complete', True),
                'daily_summary': getattr(s, 'email_daily_summary', True),
                'notify_ffmpeg_retry': getattr(
                    s, 'email_notify_ffmpeg_retry', False),
                'face_verify_port': getattr(s, 'email_face_verify_port', 19080),
            }
        except Exception:
            return {'channel': 'email', 'email_enabled': False,
                    'master_enabled': True,
                    'notify_ffmpeg_retry': False}

    def _use_email(self, config: dict) -> bool:
        if not config.get('master_enabled', True):
            return False
        ch = config.get('channel', 'email')
        return ch in ('email', 'both') and config.get('email_enabled') and config.get('recipients')

    def _use_serverchan(self, config: dict) -> bool:
        if not config.get('master_enabled', True):
            return False
        ch = config.get('channel', 'serverchan')
        return ch in ('serverchan', 'both') and bool(config.get('serverchan_sendkey'))

    def _instance_label(self) -> str:
        """稳定的实例标识：多实例共用收件人时能区分来源。

        旧实现取数据目录的 **parent** 名，而多实例的数据目录都叫 `.../data`，
        于是所有实例在通知里都显示成 `data`，等于没有区分度。
        优先级：显式 INSTANCE_LABEL → API_PORT（`instance-8001`）→ 数据目录自身的名字。
        """
        explicit = os.environ.get('INSTANCE_LABEL', '').strip()
        if explicit:
            return explicit
        port = os.environ.get('API_PORT', '').strip()
        if port:
            return f'instance-{port}'
        try:
            from app.core import config as _config
            data_dir = Path(_config.get_data_path('')).resolve()
            return data_dir.name or data_dir.parent.name or 'default'
        except Exception:
            return 'default'

    # ---------- 事件入口 ----------

    def notify_event(self, event_id: str, subject: str, body: str,
                     html: bool = False, dedup_key: str = '',
                     wait: bool = False,
                     timeout: float = TEST_DELIVERY_TIMEOUT,
                     _ordering_key: str = '') -> str:
        """按事件分类发送一条通知，返回真实状态。

        返回值：``disabled`` / ``coalesced`` / ``dropped`` / ``queued`` /
        ``sent`` / ``failed``。``wait=True`` 时等到投递结束，测试通知用它拿到
        真实结果（排队不等于已投递）。
        """
        config = self._load_config()
        if not config.get('master_enabled', True):
            logger.debug('推送总开关已关闭，跳过 [%s]', event_id)
            return 'disabled'
        switch = EVENT_SWITCH.get(event_id)
        if switch and not config.get(switch, True):
            logger.debug('事件开关关闭，跳过 [%s]（%s=%s）',
                         event_id, switch, config.get(switch))
            return 'disabled'
        if (event_id == EVENT_LOCAL_FAIL
                and not config.get('notify_ffmpeg_retry', False)):
            logger.debug('FFmpeg 短断流通知开关关闭，跳过 [%s]', event_id)
            return 'disabled'
        if not self._use_email(config) and not self._use_serverchan(config):
            logger.debug('没有可用渠道，跳过 [%s]', event_id)
            return 'disabled'

        key = (event_id, dedup_key or '')
        window = EVENT_COALESCE_SECONDS.get(event_id, 0)
        job = _NotificationJob(event_id=event_id, subject=subject, body=body,
                               html=html, dedup_key=dedup_key,
                               ordering_key=_ordering_key)
        if window > 0:
            # Check, enqueue and reserve as one operation. If admission fails,
            # no rate slot is left behind; concurrent same-key callers cannot
            # pass the check while this job is being admitted.
            with self._rate_lock:
                now = time.time()
                self._prune_rate_table(now)
                last = self._last_send.get(key, 0.0)
                if now - last < window:
                    self._bump('coalesced')
                    logger.debug('限频合并 [%s/%s]（距上次 %.0fs）',
                                 event_id, dedup_key, now - last)
                    return 'coalesced'
                if not self._try_enqueue(job):
                    return 'dropped'
                self._last_send[key] = time.time()
        elif not self._try_enqueue(job):
            return 'dropped'
        self._bump('queued')
        if not wait:
            return 'queued'
        if not job.done.wait(timeout):
            # 等待超时**不等于**失败：worker 稍后仍可能把它发出去。
            # 旧实现直接返回 failed，把"还没送到"说成"发送失败"。
            self._bump('unconfirmed')
            return 'unconfirmed'
        if any(v == 'sent' for v in job.results.values()):
            return 'sent'
        return 'failed'

    def _prune_rate_table(self, now: float) -> None:
        """限频表必须有界：按 event+dedup_key 的字典会随 run_id 无限增长。

        超过最大窗口的条目已经不可能再压制任何事件，直接淘汰即可。
        """
        max_window = max(EVENT_COALESCE_SECONDS.values() or [0])
        if len(self._last_send) <= self.MAX_RATE_ENTRIES:
            return
        stale = [k for k, ts in self._last_send.items()
                 if now - ts >= max_window]
        for key in stale:
            self._last_send.pop(key, None)
        if len(self._last_send) > self.MAX_RATE_ENTRIES:
            # 仍然超限（短窗口内大量不同 run）：按时间淘汰最旧的一批。
            ordered = sorted(self._last_send.items(), key=lambda kv: kv[1])
            for key, _ts in ordered[:len(self._last_send) - self.MAX_RATE_ENTRIES]:
                self._last_send.pop(key, None)

    def _bump(self, name: str) -> None:
        with self._counter_lock:
            self._counters[name] = self._counters.get(name, 0) + 1

    def _try_enqueue(self, job: _NotificationJob) -> bool:
        """Queue a job without blocking; return False when admission is closed/full."""
        if job.ordering_key:
            with self._ordering_lock:
                job.predecessor = self._ordered_tails.get(job.ordering_key)
                if not self._put_job_nowait(job):
                    job.predecessor = None
                    return False
                self._ordered_tails[job.ordering_key] = job.done
            return True
        return self._put_job_nowait(job)

    def _put_job_nowait(self, job: _NotificationJob) -> bool:
        with self._enqueue_lock:
            if not self._accepting:
                self._bump('dropped')
                return False
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._bump('dropped')
                logger.warning('通知队列已满，丢弃一条 [%s]', job.event_id)
                return False
        return True

    def _start_workers(self) -> None:
        for index in range(self.WORKERS):
            thread = threading.Thread(target=self._worker_loop, daemon=True,
                                      name=f'NotifyWorker-{index}')
            thread.start()
            self._workers.append(thread)

    def _worker_loop(self) -> None:
        while not self._closing.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self._wait_for_predecessor(job):
                    self._deliver(job)
                else:
                    job.results['shutdown'] = 'dropped'
                    self._bump('dropped')
            except Exception as exc:
                logger.error('通知投递异常：%s', exc)
                job.results['error'] = 'failed'
            finally:
                job.done.set()
                self._retire_ordered_job(job)
                self._queue.task_done()

    def _wait_for_predecessor(self, job: _NotificationJob) -> bool:
        """Wait off the caller thread, and cancel a wait when shutdown begins."""
        predecessor = job.predecessor
        if predecessor is None:
            return True
        while not self._closing.is_set():
            if predecessor.wait(0.1):
                return True
        return False

    def _retire_ordered_job(self, job: _NotificationJob) -> None:
        if not job.ordering_key:
            return
        with self._ordering_lock:
            if self._ordered_tails.get(job.ordering_key) is job.done:
                self._ordered_tails.pop(job.ordering_key, None)

    def _deliver(self, job: _NotificationJob) -> None:
        config = self._load_config()
        sent = False
        if self._use_email(config):
            ok = self._do_send_email(config, job.subject, job.body, job.html)
            job.results['email'] = 'sent' if ok else 'failed'
            with self._counter_lock:
                self._channel_counters['email']['sent' if ok else 'failed'] += 1
            sent = sent or ok
            if not ok:
                # 发送失败不得压制后续：把这次限频时间回退，允许较快重试。
                self._release_rate_slot(job)
                self._bump('failed')
        if self._use_serverchan(config):
            plain = job.body
            if job.html:
                import re as _re
                plain = _re.sub(r'<[^>]+>', '', job.body)
                plain = _re.sub(r'\n{3,}', '\n\n', plain)
            ok = self._send_serverchan(job.subject, plain)
            job.results['serverchan'] = 'sent' if ok else 'failed'
            with self._counter_lock:
                self._channel_counters['serverchan']['sent' if ok else 'failed'] += 1
            sent = sent or ok
            if not ok:
                # Server酱失败同样要释放限频槽：否则一次失败会让后续
                # 同事件在窗口内被静默合并掉（邮件路径已释放，这里曾漏掉）。
                self._release_rate_slot(job)
                self._bump('failed')
        if sent:
            self._bump('sent')

    def _release_rate_slot(self, job: _NotificationJob) -> None:
        with self._rate_lock:
            self._last_send.pop((job.event_id, job.dedup_key or ''), None)

    def stats(self) -> dict:
        with self._counter_lock:
            return {
                'counters': dict(self._counters),
                'channels': {k: dict(v) for k, v in self._channel_counters.items()},
                'queue_size': self._queue.qsize(),
                'workers': len(self._workers),
            }

    def send(self, subject: str, body: str, event_type: str = "info",
             html: bool = False) -> str:
        """兼容旧签名：把历史 event_type 映射到新的事件分类。"""
        mapping = {
            'start': EVENT_STREAM_RUNNING,
            'task_start': EVENT_STREAM_RUNNING,
            'stop': EVENT_STOPPED,
            'task_stop': EVENT_STOPPED,
            'error': EVENT_ACTION_REQUIRED,
            'face_verify': EVENT_ACTION_REQUIRED,
            'complete': EVENT_TASK_COMPLETE,
            'task_done': EVENT_TASK_COMPLETE,
            'daily_summary': EVENT_DAILY_SUMMARY,
            'test': EVENT_TEST,
        }
        event_id = mapping.get(event_type, EVENT_ACTION_REQUIRED)
        return self.notify_event(event_id, subject, body, html=html)

    # ---------- Server酱 ----------

    def _send_serverchan(self, title: str, content: str) -> bool:
        """Server酱（Turbo版）推送；先保留操作指引再截断，避免丢掉关键动作。"""
        config = self._load_config()
        sendkey = config.get('serverchan_sendkey', '')
        if not sendkey:
            return False
        body = self._shorten_for_serverchan(content)
        try:
            resp = _requests.post(
                f"https://sctapi.ftqq.com/{sendkey}.send",
                # 主题不再叠加旧前缀：分类前缀（[开播]/[异常]/…）已在 title 里。
                data={'title': title, 'desp': body},
                timeout=10)
            result = resp.json()
            if result.get('code') == 0:
                logger.info(f"Server酱推送成功：{title}")
                return True
            logger.warning(f"Server酱推送失败：{result.get('message', '未知')}")
            return False
        except Exception as e:
            logger.error(f"Server酱推送异常：{e}")
            return False

    @staticmethod
    def _shorten_for_serverchan(content: str, limit: int = 500) -> str:
        """截断前先保留"要做什么"：Server酱 500 字会把动作说明切掉。"""
        if len(content) <= limit:
            return content
        lines = [line for line in content.splitlines() if line.strip()]
        action = [line for line in lines
                  if any(mark in line for mark in ('请', '点击', '链接', '操作'))]
        head = '\n'.join(action[:3])
        if head:
            return (head + '\n…\n' + content[:max(0, limit - len(head) - 8)])[:limit]
        return content[:limit]

    # ---------- 邮件 ----------

    def _do_send_email(self, config: dict, subject: str, body: str,
                       html: bool = False) -> bool:
        """实际 SMTP 发送"""
        try:
            msg = MIMEMultipart("alternative")
            # 主题的分类前缀由事件方法自己给出（见 SUBJECT_*），这里不再叠加。
            msg["Subject"] = subject
            msg["From"] = config['smtp_user']
            msg["To"] = config['recipients']
            msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

            with smtplib.SMTP(config['smtp_host'], config['smtp_port'], timeout=15) as smtp:
                smtp.starttls()
                smtp.login(config['smtp_user'], config['smtp_pass'])
                smtp.sendmail(
                    config['smtp_user'],
                    [r.strip() for r in config['recipients'].split(',') if r.strip()],
                    msg.as_string()
                )
            logger.info(f"邮件已发送 | 主题：{subject}")
            return True
        except Exception as e:
            logger.error(f"邮件发送失败：{e}")
            return False

    # ---------- 便捷发送方法（结构化事件） ----------
    #
    # 2026-09-28 文案收口：**一个事件只发送一个最终有用的结果**。
    #   - 主题前缀即类别：`[开播]` `[轮转]` `[停止]` `[恢复]` `[异常]`
    #     `[简报]` `[人脸]`，另有 `[测试]` 只用于人工渠道测试；不再叠加旧的
    #     `[直播控制]`，也不再出现"请以直播间为准""系统仍会继续尝试"这类把
    #     未验证事实写给用户的过程话术。
    #   - 除日报外，正文首行给**事件发生**的本地时间（精确到秒），不是队列送达
    #     时间；轮转里 A/B 各冻结自己的时间。
    #   - 凡是时长就写明人读格式**并**附总秒数，例如 `2小时6分35秒（7595秒）`；
    #     未知不写成 0，0 目标只写"不限时"。

    SUBJECT_START = '[开播]'
    SUBJECT_ROTATION = '[轮转]'
    SUBJECT_STOP = '[停止]'
    SUBJECT_RECOVERED = '[恢复]'
    SUBJECT_ABNORMAL = '[异常]'
    SUBJECT_SUMMARY = '[简报]'
    SUBJECT_FACE = '[人脸]'
    SUBJECT_TEST = '[测试]'

    @staticmethod
    def _duration_label(seconds) -> str:
        """统一时长口径：人读格式 + 总秒数。未知写成"未知"，绝不冒充 0。"""
        try:
            total = max(0, int(seconds))
        except (TypeError, ValueError, OverflowError):
            return '未知'
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            readable = f'{hours}小时{minutes}分{secs}秒'
        elif minutes:
            readable = f'{minutes}分{secs}秒'
        else:
            readable = f'{secs}秒'
        return f'{readable}（{total}秒）'

    @staticmethod
    def _event_time_line(when=None) -> str:
        """事件发生时间（调用方此刻 = 事实成立时刻），精确到秒。"""
        stamp = when if when is not None else datetime.now()
        return f'时间：{stamp.strftime("%Y-%m-%d %H:%M:%S")}'

    def _summary_account_name(self, frozen: Optional[str] = None) -> str:
        """署名：优先用**事实里冻结**的账号显示名，其次已验证登录时缓存的昵称。

        阶段 4B 起，已确认事实自带 ``account_name``（形成点冻结）。晚投递（用户
        已退出/换账号）必须仍然署名**原账号**，因此这里绝不回读当前登录状态——
        ``frozen`` 给了就只用它。没有事实上下文时（旧调用方/日报）才退回实例标识。
        """
        name = frozen
        if not name:
            try:
                from app.dependencies import get_account_display_name
                name = get_account_display_name()
            except Exception:
                name = ''
        # 邮件主题不能包含换行或其他控制字符；未知账号保留简短实例标识，
        # 避免多个实例汇入同一收件箱时再被误认成重复发送。
        name = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(name or '')).strip()[:32]
        return name or self._instance_label()

    def _mail_subject(self, prefix: str, zone: str = '',
                      account: Optional[str] = None) -> str:
        """`[类别] 用户名 · 分区`（轮转另传 `A → B` 作为 zone）。"""
        parts = [prefix, self._summary_account_name(account)]
        if zone:
            parts.append(zone)
        return ' · '.join(parts)

    def notify_start_accepted(self, zone: str, duration_label: str = '',
                              mode: str = '', room_id=None) -> str:
        """受理阶段**不发邮件**：结果还没有任何事实可写。

        这条统一: 受理/准备/一次失败后马上重试只留在面板事件；短 FFmpeg 断流
        由单独开关控制。这里保留方法只是为了让旧调用方不必改签名——它明确返回
        `suppressed`，绝不静默排队。
        """
        logger.debug('受理阶段不发送邮件（等待真实结果）：%s', zone)
        return 'suppressed'

    def notify_stream_running(self, zone: str, source: str, elapsed_label: str = '',
                              duration_label: str = '', remaining_label: str = '',
                              room_id=None, run_id: str = '',
                              when=None, elapsed_seconds=None,
                              duration_seconds=None,
                              duration_known: bool = True,
                              account_name: Optional[str] = None) -> str:
        """平台房间已开启且推流方式已进入运行态的一次确认结果。

        ``source`` 决定类别与首句：
        - ``resume``/``reconnect`` → `[恢复]`（同一会话继续，不是新的一场）；
        - 其余 → `[开播]`。

        已计时只报**已确认的有效秒数**；目标时长未知时如实写"未知"，不冒充 0。
        """
        if source in ('resume', 'reconnect'):
            prefix = self.SUBJECT_RECOVERED
            if source == 'resume':
                lead = f'{zone}已从原进度继续，直播间已开启，任务继续计时。'
            else:
                lead = f'{zone}的直播已恢复，任务继续计时，无需操作。'
        else:
            prefix = self.SUBJECT_START
            lead = f'{zone}已开播。'
        lines = [self._event_time_line(when), lead]
        target = (self._duration_label(duration_seconds) if duration_seconds is not None
                  else ('不限时' if duration_label in ('', None) else str(duration_label)))
        if not duration_known:
            target = '不限时'
        lines.append(f'目标时长：{target}。')
        counted = (self._duration_label(elapsed_seconds) if elapsed_seconds is not None
                   else (str(elapsed_label) if elapsed_label else '0秒（0秒）'))
        lines.append(f'已计时：{counted}。')
        return self.notify_event(EVENT_STREAM_RUNNING,
                                 self._mail_subject(prefix, zone, account_name),
                                 '\n'.join(lines), dedup_key=f'{zone}:{run_id}')

    def notify_recovering(self, zone: str, attempt: int = 0,
                          reason: str = '', run_id: str = '') -> str:
        """持续恢复的**过程**不发邮件。

        恢复尝试期间没有任何"最终结果"可言；真正的结论只有两种——恢复成功
        （`[恢复]`）或达到人工处理条件（`[异常]`），分别由
        :meth:`notify_recovered` 与 :meth:`notify_platform_outage_result` 发出。
        """
        logger.debug('恢复过程不发送邮件（等待结果）：zone=%s attempt=%s',
                     zone, attempt)
        return 'suppressed'

    def notify_local_stream_fail(self, zone: str, reason: str = '',
                                 run_id: str = '') -> str:
        """本地推流（FFmpeg）故障的有界合并提醒——与平台房间故障**分范围**。

        默认由 `notify_ffmpeg_retry` 关闭（本轮保持默认关闭）。文案只写已确认的
        事实，不做"会立即恢复"的承诺。
        """
        body = '\n'.join([
            self._event_time_line(),
            f'{zone}的推流遇到问题，尚未确认恢复。',
        ])
        return self.notify_event(EVENT_LOCAL_FAIL,
                                 self._mail_subject(self.SUBJECT_ABNORMAL, zone),
                                 body, dedup_key=f'{zone}:{run_id}:local-fail')

    def notify_local_stream_action_required(self, zone: str,
                                            run_id: str = '') -> str:
        """短窗口内多次异常退出时的一次提醒；不宣称平台直播间停播，也不写承诺话术。"""
        body = '\n'.join([
            self._event_time_line(),
            f'{zone}的推流已连续多次中断，请打开面板查看推流日志并处理。',
        ])
        return self.notify_event(
            EVENT_ACTION_REQUIRED,
            self._mail_subject(self.SUBJECT_ABNORMAL, zone),
            body,
            dedup_key=f'{zone}:{run_id}:ffmpeg-rapid-fail')

    def notify_recovered(self, zone: str, scope: str = 'platform',
                         evidence: str = '', run_id: str = '',
                         when=None) -> str:
        """已确认的一次恢复结果（`[恢复]`），不是"正在恢复"的过程信。"""
        lead = (f'{zone}的直播间已重新开启，任务继续计时。' if scope == 'platform'
                else f'{zone}的推流程序已重新启动。')
        return self.notify_event(
            EVENT_RECOVERED, self._mail_subject(self.SUBJECT_RECOVERED, zone),
            '\n'.join([self._event_time_line(when), lead]),
            dedup_key=f'{zone}:{run_id}:{scope}')

    def notify_platform_outage_result(self, zone: str, result: str,
                                      run_id: str, episode_id: int) -> str:
        """仅对平台**已确认**断播的一次故障给出结论。

        - ``recovered``：`[恢复]`，一次而已（同一故障身份去重）；
        - ``failed``：`[异常]`，明确到了需要人工处理的程度；
        - ``still_retrying``：`[异常]`，但只写**已确认的事实**——"持续无法确认
          直播状态"，不写"已断播/已恢复"，也不写"仍会继续尝试"。
        """
        key = f'{run_id}:{episode_id}:platform-outage'
        if result == 'recovered':
            return self.notify_event(
                EVENT_RECOVERED,
                self._mail_subject(self.SUBJECT_RECOVERED, zone),
                '\n'.join([self._event_time_line(),
                            f'{zone}的直播间已重新开启，任务继续计时，无需操作。']),
                dedup_key=key)
        if result == 'still_retrying':
            return self.notify_event(
                EVENT_ACTION_REQUIRED,
                self._mail_subject(self.SUBJECT_ABNORMAL, zone),
                '\n'.join([self._event_time_line(),
                            f'持续无法确认{zone}的直播状态，请在面板查看并处理。']),
                dedup_key=f'{key}:pending')
        if result == 'failed':
            return self.notify_event(
                EVENT_ACTION_REQUIRED,
                self._mail_subject(self.SUBJECT_ABNORMAL, zone),
                '\n'.join([self._event_time_line(),
                            f'{zone}的直播未能恢复，请在面板查看并处理。']),
                dedup_key=key)
        raise ValueError(f'unsupported platform outage result: {result}')

    def notify_action_required(self, title: str, reason: str,
                               still_streaming: bool = False,
                               action: str = '', identity: str = '',
                               zone: str = '', when=None) -> str:
        """需人工处理：`[异常]` + 处置方向；技术细节留在面板事件与日志。"""
        lead = ('自动恢复未成功。' if title.startswith('自动恢复')
                else '直播需要处理。')
        body = '\n'.join([self._event_time_line(when), lead,
                           '请打开面板查看原因并处理。'])
        return self.notify_event(EVENT_ACTION_REQUIRED,
                                 self._mail_subject(self.SUBJECT_ABNORMAL, zone),
                                 body, dedup_key=f'{title}:{identity}')

    def notify_start_failed(self, zone: str, identity: str = '',
                            when=None, account_name: Optional[str] = None) -> str:
        """首次开播的单一失败结果；平台错误详情留在面板。"""
        return self.notify_event(
            EVENT_ACTION_REQUIRED,
            self._mail_subject(self.SUBJECT_ABNORMAL, zone, account_name),
            '\n'.join([self._event_time_line(when),
                        f'{zone}尚未确认直播间开启。',
                        '请先在面板核对状态，再决定是否重试。']),
            dedup_key=f'start-failed:{identity}')

    def notify_stopped(self, zone: str, stage: str = 'stopped',
                       elapsed_label: str = '', reason: str = '',
                       elapsed_seconds=None, when=None,
                       account_name: Optional[str] = None) -> str:
        """真实停止结果：停止中/已停止/结果待确认分别表述。

        ``pending_recycle``（清理未确认）与 ``checkpoint_failed``（进度落盘失败）
        是独立的已确认问题，按 `[异常]` 发送——不能让正文既说"已停止"又说
        "结果未确认"。
        """
        if stage == 'stopped':
            subject = self._mail_subject(self.SUBJECT_STOP, zone, account_name)
            lines = [self._event_time_line(when), f'{zone}的直播已停止。']
            counted = (self._duration_label(elapsed_seconds)
                       if elapsed_seconds is not None
                       else (str(elapsed_label) if elapsed_label else '0秒（0秒）'))
            lines.append(f'本次已计时：{counted}。')
            body = '\n'.join(lines)
        elif stage == 'pending_recycle':
            subject = self._mail_subject(self.SUBJECT_ABNORMAL, zone, account_name)
            body = '\n'.join([
                self._event_time_line(when),
                f'{zone}的停止请求已受理，但结果待确认。',
                '请在面板核对直播状态；若仍在播，可再次点击停止。'])
        elif stage == 'checkpoint_failed':
            subject = self._mail_subject(self.SUBJECT_ABNORMAL, zone, account_name)
            body = '\n'.join([
                self._event_time_line(when),
                f'{zone}的直播已停止，但本次进度未能确认保存。',
                '请在面板核对任务进度。'])
        else:
            subject = self._mail_subject(self.SUBJECT_STOP, zone, account_name)
            body = '\n'.join([self._event_time_line(when),
                               f'{zone}正在停止。'])
        event_id = (EVENT_ACTION_REQUIRED if stage in (
            'pending_recycle', 'checkpoint_failed') else EVENT_STOPPED)
        return self.notify_event(event_id, subject, body,
                                 dedup_key=f'{zone}:{stage}')

    def notify_task_complete(self, zone: str, business_date: str,
                             scope: str = 'today', result: str = 'settled',
                             progress: str = '', when=None,
                             effective_seconds=None,
                             account_name: Optional[str] = None) -> str:
        """结算**已提交**之后才发；结果决定类别与标题。

        - ``failed``：结算失败是需要人工确认的异常 → `[异常]`，服从异常开关；
        - ``already``：此前已结算，不冒充新完成；
        - 其余按 `[轮转]`："今天的当天完成/全部完成/已到期" 都属于一次任务推进。
        """
        if result == 'failed':
            return self.notify_event(
                EVENT_ACTION_REQUIRED,
                self._mail_subject(self.SUBJECT_ABNORMAL, zone, account_name),
                '\n'.join([self._event_time_line(when),
                            f'{zone}本次直播的有效天数尚未确认计入。',
                            '请到面板核对进度。']),
                dedup_key=f'{zone}:{business_date}:{scope}:failed')
        lines = [self._event_time_line(when)]
        if result == 'already':
            body_tail = (f'{zone}在 {business_date} 的完成记录此前已计入，'
                         '本次未重复计天。')
        elif scope == 'all':
            body_tail = f'{zone}需要的直播天数已全部完成。'
        elif scope == 'expired':
            body_tail = f'{zone}的任务截止日已到，本次直播已结束。'
        else:
            body_tail = f'{business_date} 的直播已计入{zone}的任务进度。'
        lines.append(body_tail)
        if effective_seconds is not None:
            lines.append(f'本次有效时长：{self._duration_label(effective_seconds)}。')
        if progress and result == 'settled':
            lines.append(f'{progress}。')
        return self.notify_event(
            EVENT_TASK_COMPLETE,
            self._mail_subject(self.SUBJECT_ROTATION, zone, account_name),
            '\n'.join(lines), dedup_key=f'{zone}:{business_date}:{scope}')

    @staticmethod
    def _task_duration_label(duration_seconds) -> str:
        try:
            seconds = int(duration_seconds or 0)
        except (TypeError, ValueError, OverflowError):
            return '未知'
        if seconds <= 0:
            return '不限时'
        return EmailSender._duration_label(seconds)

    @staticmethod
    def _effective_duration_label(effective_seconds) -> str:
        try:
            seconds = max(0, int(effective_seconds))
        except (TypeError, ValueError, OverflowError):
            return '未知'
        return EmailSender._duration_label(seconds)

    def notify_task_rotation(self, completion: Optional[dict],
                             next_task: Optional[dict],
                             next_result: str,
                             verify_url: str = '',
                             confirm_url: str = '',
                             reason: str = '',
                             late_update: bool = False,
                             identity: str = '',
                             when=None,
                             account_name: Optional[str] = None) -> str:
        """一封冻结 A 完成事实与 B 实际开播结果的 `[轮转]` 邮件。

        ``next_result`` 只接受由开播线程确认的 ``started``、``failed``、
        ``verification``、``preparing`` 或无待办 ``none``。B 尚在等待而结果未知
        时不发"准备中"邮件；B 需人脸验证时发 `[人脸]`，明确失败时发 `[异常]`，
        两种情况都如实带上 A 已确认的完成事实。
        """
        completion = completion or {}
        next_task = next_task or {}
        zone_a = str(completion.get('zone') or '上一任务')
        zone_b = str(next_task.get('zone') or '')
        # 署名取**冻结**的账号显示名：晚投递（已退出/换账号）仍署名原账号。
        account = self._summary_account_name(account_name)
        time_line = self._event_time_line(when)

        if late_update:
            event_id = (EVENT_STREAM_RUNNING if next_result == 'started'
                        else EVENT_ACTION_REQUIRED)
            if next_result == 'started':
                subject = self._mail_subject(self.SUBJECT_ROTATION,
                                             zone_b or '下一任务', account_name)
                body = '\n'.join([time_line,
                                   f'{zone_b or "下一任务"}已开播。'])
            elif next_result == 'verification':
                subject = self._mail_subject(self.SUBJECT_FACE,
                                             zone_b or '下一任务', account_name)
                body = self._face_lines(time_line, zone_b or '下一任务',
                                        verify_url, confirm_url)
            elif next_result == 'preparing':
                # B 的结果还未知：不替它下结论，也不发送过程邮件。
                logger.debug('轮转 B 仍在准备，暂不发送结果邮件：%s', zone_b)
                return 'suppressed'
            elif next_result == 'cancelled':
                subject = self._mail_subject(self.SUBJECT_ROTATION, zone_a, account_name)
                body = '\n'.join([time_line,
                                   f'{zone_b or "下一任务"}的开播等待已取消；'
                                   f'当前未确认直播间开启。'])
                event_id = EVENT_TASK_ROTATION
            elif next_result == 'started_then_stopped':
                subject = self._mail_subject(self.SUBJECT_STOP,
                                             zone_b or '下一任务', account_name)
                body = '\n'.join([time_line,
                                   f'{zone_b or "下一任务"}已确认开启，随后收到停止请求。'])
                event_id = EVENT_STOPPED
            else:
                subject = self._mail_subject(self.SUBJECT_ABNORMAL,
                                             zone_b or '下一任务', account_name)
                body = '\n'.join([time_line,
                                   f'{zone_b or "下一任务"}开播未成功，未确认直播间开启。',
                                   '请查看面板与日志。'])
            # 晚投递同样署名**冻结**的账号（见本函数开头）：主题与正文一致，
            # 用户已退出/换号之后才投出去的结果不会被误读成新账号的动作。
            body = '\n'.join([body, f'账号：{account}。'])
        else:
            duration_label = self._task_duration_label(
                next_task.get('duration_seconds')) if next_task else '未知'
            lines = []
            if completion:
                lines.append(
                    f'{zone_a}已完成（执行日 {completion.get("execution_date") or "未取得"}）。')
                effective = completion.get('effective_seconds')
                lines.append('有效时长：' + (self._duration_label(effective)
                                            if effective is not None else '未知'))
                days_done = completion.get('days_done')
                lines.append(f'累计完成：{days_done} 天' if days_done is not None
                             else '累计完成天数：未取得')
            if next_result == 'none':
                lines.append('没有下一个待执行任务。')
                event_id = EVENT_TASK_ROTATION
                subject = self._mail_subject(self.SUBJECT_ROTATION, zone_a, account_name)
            elif not next_task:
                if next_result == 'paused':
                    reason_label = f'（{reason}）' if reason else ''
                    lines.append(
                        f'自动轮转已暂停{reason_label}；下一任务尚未启动。')
                    event_id = EVENT_TASK_ROTATION
                    subject = self._mail_subject(self.SUBJECT_ROTATION, zone_a, account_name)
                elif next_result == 'cancelled':
                    lines.append('轮转已停止；下一任务尚未确认开播。')
                    event_id = EVENT_TASK_ROTATION
                    subject = self._mail_subject(self.SUBJECT_ROTATION, zone_a, account_name)
                else:
                    reason_label = f'（{reason}）' if reason else ''
                    lines.append(
                        f'下一任务未能选出或启动{reason_label}；'
                        f'当前没有确认开播结果，请查看面板。')
                    event_id = EVENT_ACTION_REQUIRED
                    subject = self._mail_subject(self.SUBJECT_ABNORMAL, zone_a, account_name)
            else:
                lines.append(f'下一个任务：{zone_b or "未取得"}')
                lines.append(f'目标时长：{duration_label}')
                if next_result == 'started':
                    lines.append(
                        f'结果：{zone_b or "下一任务"}已开播。')
                    # 合并信同时承载 A 完成与 B 开播。若用户关闭了完成邮件，
                    # 但保留开播邮件，仍按开播开关送出这封结果邮件。
                    try:
                        config = self._load_config()
                    except Exception:
                        config = {}
                    event_id = (EVENT_TASK_ROTATION
                                if config.get('notify_complete', True)
                                or not config.get('notify_start', True)
                                else EVENT_STREAM_RUNNING)
                    subject = self._mail_subject(
                        self.SUBJECT_ROTATION,
                        f'{zone_a} → {zone_b or "下一任务"}' if zone_a else zone_b,
                        account_name)
                elif next_result == 'verification':
                    # 不在这里提前 return：下面还有"异常开关关闭但完成通知打开"
                    # 的保护，提前返回会把 A 已结算的事实一起静默吞掉。
                    lines.append(
                        f'结果：{zone_b or "下一任务"}开播需要人脸验证，'
                        f'尚未确认直播间开启。')
                    lines.append(
                        f'请先打开 B 站验证页面：{verify_url or "未取得"}')
                    lines.append(
                        f'验证完成入口：{confirm_url or "请在面板点击“验证完成”"}')
                    event_id = EVENT_ACTION_REQUIRED
                    subject = self._mail_subject(self.SUBJECT_FACE,
                                                 zone_b or '下一任务', account_name)
                elif next_result == 'preparing':
                    # B 的结果未知：不发"准备中"，等结果确定后再报。
                    logger.debug('轮转 B 仍在准备，暂不发送合并结果邮件：%s', zone_b)
                    return 'suppressed'
                elif next_result == 'cancelled':
                    lines.append('结果：轮转已停止；下一任务尚未确认开播。')
                    event_id = EVENT_TASK_ROTATION
                    subject = self._mail_subject(
                        self.SUBJECT_ROTATION,
                        f'{zone_a} → {zone_b or "下一任务"}' if zone_a else zone_b,
                        account_name)
                elif next_result == 'started_then_stopped':
                    lines.append('结果：已确认开启，随后收到停止请求。')
                    event_id = EVENT_TASK_ROTATION
                    subject = self._mail_subject(
                        self.SUBJECT_STOP,
                        f'{zone_a} → {zone_b or "下一任务"}' if zone_a else zone_b,
                        account_name)
                else:
                    lines.append('结果：开播未成功，未确认直播间开启。请查看面板与日志。')
                    event_id = EVENT_ACTION_REQUIRED
                    subject = self._mail_subject(self.SUBJECT_ABNORMAL,
                                                 zone_b or '下一任务', account_name)
            lines.insert(0, time_line)
            lines.append(f'账号：{account}。')
            body = '\n'.join(lines)

        if (not late_update and completion
                and next_result in ('failed', 'verification')):
            # B 的异常邮件默认走异常开关；若异常通知关闭而 A 的完成通知打开，
            # 仍送出这封合并结果，避免 A 已结算的事实被一起静默吞掉。
            try:
                config = self._load_config()
            except Exception:
                config = {}
            if (not config.get('notify_error', True)
                    and config.get('notify_complete', True)):
                event_id = EVENT_TASK_ROTATION

        stage_key = 'late' if late_update else 'initial'
        dedup = f'task-rotation:{identity or zone_a}:{stage_key}:{next_result}'
        return self.notify_event(
            event_id, subject, body, dedup_key=dedup,
            _ordering_key=f'task-rotation:{identity or zone_a}')

    def _face_lines(self, time_line: str, zone: str, verify_url: str,
                    confirm_url: str) -> str:
        """人脸验证的统一正文段（不含年月日之外的夸大承诺）。"""
        lines = [time_line,
                 f'{zone}开播需要人脸验证。',
                 f'请先打开 B 站验证页面：{verify_url or "未取得"}']
        if confirm_url:
            lines.append(f'验证后打开确认链接，点击“验证完成”：{confirm_url}')
        else:
            lines.append('验证后请在面板点击“验证完成”。')
        return '\n'.join(lines)

    def send_daily_summary(self, stats: dict, top5: list,
                           summary_date=None, snapshot_meta: Optional[dict] = None,
                           account_name: Optional[str] = None
                           ) -> str:
        """简短汇报冻结快照；统计日完成数不与当前待办混为一日。"""
        stats = stats or {}
        top5 = top5 or []
        meta = snapshot_meta or {}
        day_label = summary_date.strftime('%Y-%m-%d') if summary_date else \
            datetime.now().strftime('%Y-%m-%d')
        instance = self._instance_label()

        def field(key):
            value = stats.get(key)
            return '未取得' if value is None else escape_html(value)

        # 完成标记的归属窗口（R2）：只有窗口 == 统计日时，完成数才能写成
        # "当日已完成 N"。窗口是别的日子（多日未重置/延迟重置）或未知
        # （字段缺失、与重置边界不一致）→ 如实"未取得"+说明，绝不把
        # 历史完成硬贴成统计日的结果。待办始终是**当前口径**（可信）。
        done_flags = meta.get('done_flags') or {}
        done_window = done_flags.get('window')
        done_confident = bool(done_flags.get('confident'))
        window_matches_day = (done_confident and summary_date is not None
                              and done_window == summary_date)
        done_available = window_matches_day and stats.get('today_done') is not None
        done_value = (f"{field('today_done')} 项" if done_available else '待确认')
        done_note = ('' if done_available else
                     '<p>完成数暂未核实，请以面板记录为准。</p>')

        remaining = stats.get('remaining_time')
        remaining_label = ('未取得' if remaining is None else
                           f'约 {escape_html(remaining)} 小时')
        try:
            urgency = float(stats.get('urgency'))
            urgency_label = ('未取得' if not math.isfinite(urgency) or urgency < 0
                             else f'{urgency * 100:.2f}'.rstrip('0').rstrip('.') + '%')
        except (TypeError, ValueError, OverflowError):
            urgency_label = '未取得'

        priority_rows = ''
        for t in top5[:10]:
            remaining = t.get('remaining_exec_days', t.get('remaining_days'))
            done = t.get('days_done')
            total = t.get('actual_days')
            priority = t.get('priority')
            priority_label = ('未取得' if priority is None else
                              escape_html(priority))
            progress = (f'已完成 {escape_html(done)}/{escape_html(total)} 天'
                        if done is not None and total is not None else '进度待确认')
            if remaining is not None:
                progress += f'，还需 {escape_html(remaining)} 天'
            priority_rows += (f'<li>{escape_html(t.get("zone_name", ""))}：'
                              f'优先度 {priority_label}，{progress}</li>')
        priority_section = (f'<h3>接下来优先</h3><ol>{priority_rows}</ol>'
                            if priority_rows else '')

        body = f"""<html><body>
<h2>{escape_html(day_label)} 直播简报</h2>
<p>当日完成：{done_value}</p>
{done_note}
<p>当前待办：{field('pending_total')} 项<br>
计划剩余直播：{remaining_label}<br>
紧迫率：{urgency_label}</p>
{priority_section}
</body></html>"""
        return self.notify_event(
            EVENT_DAILY_SUMMARY,
            self._mail_subject(self.SUBJECT_SUMMARY, day_label, account_name),
            body, html=True,
            dedup_key=f'{instance}:{day_label}')

    def send_test(self) -> str:
        """渠道测试：等待真实投递结果（排队不等于已发送）。"""
        return self.notify_event(
            EVENT_TEST,
            self._mail_subject(self.SUBJECT_TEST),
            '\n'.join([self._event_time_line(),
                        '这是一封人工触发的渠道测试邮件，'
                        '用于确认通知可以送达此邮箱。']),
            wait=True)

    # ---------- 兼容旧调用名 ----------

    def send_task_start(self, zone_name: str, duration_label: str) -> str:
        return self.notify_stream_running(zone_name, 'new', '0 分钟', duration_label)

    def send_task_complete(self, zone_name: str, elapsed_label: str) -> str:
        return self.notify_task_complete(
            zone_name, datetime.now().strftime('%Y-%m-%d'), 'today', 'settled')

    def send_error(self, error_type: str, detail: str) -> str:
        return self.notify_action_required(str(error_type), str(detail))

    def send_reconnect_start(self, attempt: int, max_retries: int) -> str:
        return self.notify_recovering('', attempt, f'第 {attempt}/{max_retries} 次')

    def send_reconnect_exhausted(self, cooldown_minutes: int) -> str:
        return self.notify_action_required(
            '自动恢复持续失败', f'已连续重试，冷却 {cooldown_minutes} 分钟',
            still_streaming=False, action='检查网络与账号状态后手动重试')

    # ---------- 人脸验证远程确认 ----------

    def issue_face_verify_token(self, run_id: str = '', room_id=None,
                                epoch: int = None, purpose: str = 'face_verify',
                                ttl_seconds: int = 3600, **extra) -> str:
        """签发带身份与有效期的确认 token。

        旧实现只保存 True：没有失效时间、也没绑定会话，于是"发一封新邮件就能
        让上一封失效"，而旧邮件链接又能清掉**新会话**的验证/恢复阻塞状态。
        """
        token = uuid.uuid4().hex[:16]
        now = time.time()
        with self._token_lock:
            # 清理过期 token（不无条件清空：一次发送失败不应废掉上一封有效邮件）
            expired = [k for k, v in self._face_verify_tokens.items()
                       if v.get('expires_at', 0) <= now]
            for k in expired:
                self._face_verify_tokens.pop(k, None)
            record = {
                'run_id': run_id or '',
                'room_id': room_id,
                'epoch': epoch,
                'purpose': purpose,
                'created_at': now,
                'expires_at': now + ttl_seconds,
            }
            # 冻结的启动意图（模式/来源/继承进度/分区/task_id/执行日）：确认后
            # 重试必须沿用**当时**的事实，不能由晚到的 worker 重新拼当前状态。
            for key, value in extra.items():
                if value is not None:
                    record[key] = value
            self._face_verify_tokens[token] = record
        return token

    def _token_mismatch(self, info: dict, run_id: str, room_id,
                        epoch) -> str:
        """返回不匹配的原因（'' 表示匹配）。有效期也在这里统一判定。"""
        if info.get('expires_at', 0) <= time.time():
            return '确认链接已过期'
        if run_id and info.get('run_id') and info['run_id'] != run_id:
            return '该确认链接属于另一个会话，已失效'
        if room_id is not None and info.get('room_id') is not None \
                and info['room_id'] != room_id:
            return '该确认链接属于另一个直播间，已失效'
        if epoch is not None and info.get('epoch') is not None \
                and int(info['epoch']) != int(epoch):
            return '该确认链接的控制代际已过期，已失效'
        return ''

    def peek_face_verify_token(self, token: str, run_id: str = '',
                               room_id=None, epoch: int = None
                               ) -> Optional[dict]:
        """GET 只预览：不消费、不触发控制动作，但**同样校验有效期与身份**。

        旧实现只判断"字典里有没有这个 token"：过期链接、属于另一场会话的链接
        都会渲染出可点的按钮，点下去才失败。
        """
        with self._token_lock:
            info = self._face_verify_tokens.get(token)
            if not info:
                return None
            if self._token_mismatch(dict(info), run_id, room_id, epoch):
                return None
            return dict(info)

    def consume_face_verify_token_ex(self, token: str,
                                     run_id: str = '', room_id=None,
                                     epoch: int = None
                                     ) -> Tuple[bool, str, Optional[dict]]:
        """消费并**连同冻结意图一起返回**（确认后的重试要用它）。"""
        with self._token_lock:
            info = self._face_verify_tokens.get(token)
            if info is None:
                return False, 'token 不存在或已被使用', None
            frozen = dict(info)
            reason = self._token_mismatch(frozen, run_id, room_id, epoch)
            if reason:
                self._face_verify_tokens.pop(token, None)
                return False, reason, None
            # 消费：只在这里删除，保证并发双击也至多生效一次
            self._face_verify_tokens.pop(token, None)
            return True, 'ok', frozen

    def consume_face_verify_token(self, token: str,
                                  run_id: str = '', room_id=None,
                                  epoch: int = None) -> Tuple[bool, str]:
        """POST 才消费：原子取出，且身份/有效期不匹配一律拒绝。

        返回 (ok, reason)。旧链接不得清掉新会话的状态——run_id/room/epoch
        任一不匹配即视为失效。
        """
        now = time.time()
        with self._token_lock:
            info = self._face_verify_tokens.get(token)
            if info is None:
                return False, 'token 不存在或已被使用'
            if info.get('expires_at', 0) <= now:
                self._face_verify_tokens.pop(token, None)
                return False, '确认链接已过期'
            if run_id and info.get('run_id') and info['run_id'] != run_id:
                self._face_verify_tokens.pop(token, None)
                return False, '该确认链接属于另一个会话，已失效'
            if room_id is not None and info.get('room_id') is not None \
                    and info['room_id'] != room_id:
                self._face_verify_tokens.pop(token, None)
                return False, '该确认链接属于另一个直播间，已失效'
            if epoch is not None and info.get('epoch') is not None \
                    and int(info['epoch']) != int(epoch):
                self._face_verify_tokens.pop(token, None)
                return False, '该确认链接的控制代际已过期，已失效'
            # 消费：只在这里删除，保证并发双击也至多生效一次
            self._face_verify_tokens.pop(token, None)
            return True, 'ok'

    #: 验证阶段 → 用户可读的阶段标题。一个验证事实一封、阶段措辞与真实处置
    #: 一致（2026-09-21 N2：旧正文固定写"开播需要人脸验证"，查询提示与恢复
    #: 被拦分不清，还和"未替你停推"的通用邮件同发两封互相矛盾）。
    _FACE_VERIFY_STAGE_LABELS = {
        'start_blocked': '首次开播需要人脸验证',
        'resume_blocked': '恢复直播需要人脸验证',
        'status_query': '状态查询提示需要人脸验证',
    }

    def send_face_verify(self, verify_url: str, run_id: str = '', room_id=None,
                         epoch: int = None, still_streaming: bool = False,
                         stage: str = 'start_blocked',
                         local_stop_requested: bool = False,
                         defer_notification: bool = False,
                         when=None,
                         account_name: Optional[str] = None,
                         **intent) -> Any:
        """发送人脸验证邮件（一封、带阶段事实与确认入口）。

        ``stage`` 冻结触发背景：``start_blocked``（首次开播被拦）/
        ``resume_blocked``（恢复被拦，本路径会停止本地推流）/
        ``status_query``（查询提示验证，本路径**未**停止推流）。
        ``local_stop_requested``：本路径是否处置了本地推流——写进正文的是
        **本路径的真实动作**，不用控制器 is_streaming 断言媒体实际状态，
        未知的保持未知。

        ``**intent`` 是**通知发生时**冻结的启动意图（模式/来源/继承进度/分区/
        task_id/执行日），随 token 一起保存：确认后重试沿用当时的事实。
        """
        token = self.issue_face_verify_token(run_id=run_id, room_id=room_id,
                                             epoch=epoch,
                                             still_streaming=still_streaming,
                                             stage=stage,
                                             **intent)
        stage_label = self._FACE_VERIFY_STAGE_LABELS.get(stage, '需要人脸验证')
        zone = re.sub(r'[\x00-\x1f\x7f]+', ' ',
                      str(intent.get('zone') or '')).strip()[:40]
        account = self._summary_account_name(account_name)
        # 同一封邮件只解析一次 host；避免后台探测刚好在两次读取之间完成，
        # 让可用判断与最终链接使用不同地址。
        server_host = self._get_server_host()
        host_usable = bool(server_host) and server_host != 'localhost'
        # 人脸邮件同样要带事件发生时间到秒（除日报外每封都带）。
        lines = [self._event_time_line(when),
                 f'{zone}：{stage_label}。' if zone else f'{stage_label}。',
                 '', '请先打开 B 站验证页面：', verify_url]
        if host_usable:
            confirm_url = self._confirm_url(token, server_host=server_host)
            lines += ['', '验证后打开以下链接，点击“验证完成”：',
                      confirm_url]
        else:
            # 地址不可用时不暴露配置细节，更不能拼出 localhost 假链接。
            lines += ['', '邮件确认链接暂不可用。',
                      '验证后请在面板点击“验证完成”。']
        if stage == 'status_query':
            lines += ['', '系统未停止推流。']
        elif local_stop_requested:
            lines += ['', '系统已请求停止推流。']
        if defer_notification:
            return {
                'verify_url': verify_url,
                'confirm_url': confirm_url if host_usable else '',
            }
        return self.notify_event(
            EVENT_ACTION_REQUIRED,
            self._mail_subject(self.SUBJECT_FACE, zone, account_name),
            '\n'.join(lines),
            dedup_key=f'face_verify:{stage}:{run_id or room_id or ""}',
        )

    def _confirm_url(self, token: str, server_host: Optional[str] = None) -> str:
        server_host = _normalize_public_host(
            server_host if server_host is not None else self._get_server_host())
        if not server_host:
            return ''
        try:
            port = int(os.environ.get('API_PORT', '8000'))
        except (TypeError, ValueError):
            port = 8000
        if not 1 <= port <= 65535:
            port = 8000
        # Match run.py exactly: only the value set when TLS is configured enables https.
        scheme = 'https' if os.environ.get('API_SSL') == '1' else 'http'
        return f"{scheme}://{server_host}:{port}/api/email/confirm-face-verify?token={token}"

    # ---------- 人脸验证确认后的重试 ----------

    def _retry_after_face_verify(self, mode: Optional[str] = None,
                                 intent: Optional[dict] = None):
        """人脸验证确认后重试开播。

        ``intent`` 是**确认时冻结**的启动意图（run/room/epoch/模式/来源/继承
        进度）。旧实现在 worker 启动时才去读当前 epoch 与 current_instruction，
        然后 sleep 3 秒——期间发生的"停止 + 新开播"完全看不见，旧确认会驱动
        新的一场直播，而且恢复的进度被归零。
        """
        intent = dict(intent or {})
        try:
            from app.dependencies import get_live_controller
            lc = get_live_controller()
        except Exception as e:
            logger.error(f"人脸验证后重试准备失败：{e}")
            return
        if not lc:
            return
        frozen_epoch = intent.get('epoch')
        frozen_run = intent.get('run_id') or ''
        saved_mode = mode or intent.get('mode') \
            or getattr(lc, '_verify_retry_mode', None)
        delay = float(intent.get('delay', 3) or 3)
        time.sleep(max(0.0, delay))
        try:
            if getattr(lc, 'stop_monitor', None) is not None and lc.stop_monitor.is_set():
                logger.info("人脸验证确认到达时已有停止请求，不再重试开播")
                return
            if getattr(lc, '_start_cancel', None) is not None and lc._start_cancel.is_set():
                logger.info("人脸验证确认到达时开播已被取消，不再重试开播")
                return
            if getattr(lc, 'is_streaming', False):
                logger.info("人脸验证确认到达时已经在推流，无需重试开播")
                return
            # 房间也是身份的一部分：同一 run 名在不同房间号下不代表同一场。
            frozen_room = intent.get('room_id')
            try:
                if (frozen_room is None or int(frozen_room) <= 0
                        or int(frozen_room) != int(getattr(lc, 'current_room_id', 0) or 0)):
                    logger.info("人脸验证确认属于另一个或不可核对的房间，不再重试开播")
                    return
            except (TypeError, ValueError):
                logger.info("人脸验证确认的房间号不可比对，不再重试开播")
                return
            # 身份冻结：本场会话必须是确认时那一场（不能重新读"当前"代际复活旧意图）
            current_run = (getattr(lc, '_current_run_id', '')
                           or getattr(lc, '_pending_run_id', '')
                           or getattr(getattr(lc, 'state', None), 'run_id', '') or '')
            if frozen_run and current_run and current_run != frozen_run:
                logger.info("人脸验证确认属于另一场会话（run 已变），不再重试开播")
                return
            checker = getattr(lc, '_is_epoch_current', None)
            if frozen_epoch is not None and callable(checker) \
                    and not checker(frozen_epoch):
                logger.info("人脸验证确认的控制代际已过期，不再重试开播")
                return
            instruction = None
            frozen_instruction = intent.get('instruction')
            if isinstance(frozen_instruction, dict):
                try:
                    from app.models.schemas import LiveInstruction
                    instruction = LiveInstruction(**frozen_instruction)
                except Exception:
                    logger.info("冻结的人脸验证指令无效，不再重试开播")
                    return
                current_instruction = getattr(lc, 'current_instruction', None)
                if (not current_instruction
                        or current_instruction.zone_name != instruction.zone_name
                        or current_instruction.task_id != instruction.task_id
                        or current_instruction.execution_date != instruction.execution_date
                        or current_instruction.duration_seconds != instruction.duration_seconds):
                    logger.info("人脸验证确认时的开播指令已变化，不再重试开播")
                    return
            else:
                instruction = getattr(lc, 'current_instruction', None)
            if not instruction:
                logger.info("人脸验证确认到达时已无待开播指令，不再重试")
                return
            # 手动验证后的恢复：沿用原模式，不强制改成任务模式
            is_task_mode = saved_mode != 'manual'
            source = intent.get('source') or ('new' if is_task_mode else 'manual')
            inherit = intent.get('inherit_elapsed')
            logger.info("人脸验证已确认，重试开播（模式：%s，来源：%s，继承：%s 秒）",
                        '任务' if is_task_mode else '手动', source, inherit)
            lc.start_streaming(instruction, '',
                               is_task_mode=is_task_mode, epoch=frozen_epoch,
                               source=source, inherit_elapsed=inherit,
                               expected_face_run_id=(frozen_run or None),
                               expected_face_room_id=frozen_room)
        except Exception as e:
            logger.error(f"人脸验证后重试失败：{e}")

    def shutdown(self) -> None:
        """停止受理，给已排队通知三秒投递机会，再丢弃未完成部分。

        为什么必须最终清空队列：worker 一旦退出，队列里剩下的任务既不会被
        投递、也不会被回收，却仍然"存在"——任何后续再装/卸 SMTP 替身的顺序
        变化都会让它们打到真实渠道（测试收尾时曾出现对真实 SMTP 的连接尝试）。
        丢弃量计入 dropped 计数，不静默。
        """
        deadline = time.monotonic() + 3.0
        with self._enqueue_lock:
            self._accepting = False
        # Worker must stay alive while the existing queue drains. A service
        # shutdown notice cannot be silently discarded merely because it was
        # queued a moment before lifespan cleanup.
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        self._closing.set()
        dropped = 0
        while True:
            try:
                job = self._queue.get_nowait()
                job.results['shutdown'] = 'dropped'
                job.done.set()
                self._retire_ordered_job(job)
                self._queue.task_done()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            self._bump("dropped")
            logger.info("关闭通知器：丢弃 %d 条排队中的通知", dropped)
        # 全部 worker 共用 3 秒预算；逐个各等 3 秒会把直播控制器的
        # 20 秒退出预算与 systemd 30 秒窗口叠加到 SIGKILL。
        for thread in self._workers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

