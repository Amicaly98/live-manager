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
import json
import os
import queue
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


# ==================== 事件分类 ====================

EVENT_START_ACCEPTED = 'start_accepted'      # 控制请求受理，未证明媒体输出
EVENT_STREAM_RUNNING = 'stream_running'      # 已进入运行阶段（区分新开/恢复/手动）
EVENT_RECOVERING = 'recovering'              # 持续恢复中（同一故障合并）
EVENT_RECOVERED = 'recovered'                # 本轮恢复条件确实达成
EVENT_ACTION_REQUIRED = 'action_required'    # 需人工处理：验证/登录失效/明确拒绝
EVENT_STOPPED = 'stopped'                    # 真实停止结果
EVENT_TASK_COMPLETE = 'task_complete'        # 结算已提交
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
        # 桌面版只使用显式配置/本机地址。服务器版的云元数据探测会触发
        # 外网请求，并不适用于 file:// + 本机 FastAPI 的桌面确认页。
        self._host_cache: Optional[str] = None
        self._start_workers()

    # ---------- 配置读取 ----------

    def _get_server_host(self) -> str:
        """Return an explicitly configured host or this desktop's loopback host.

        A desktop confirmation link is useful on the same machine, while a
        public address must be supplied deliberately through settings or the
        environment.  Never probe cloud metadata from a notification thread.
        """
        try:
            from app.api.settings import load_settings
            h = getattr(load_settings(), 'server_host', '')
            if h:
                return h.strip()
        except Exception:
            pass
        env_host = os.environ.get('SERVER_PUBLIC_HOST', '').strip()
        if env_host:
            return env_host
        if self._host_cache:
            return self._host_cache
        return '127.0.0.1'

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
                'face_verify_port': getattr(s, 'email_face_verify_port', 19080),
            }
        except Exception:
            return {'channel': 'email', 'email_enabled': False,
                    'master_enabled': True}

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
                     timeout: float = TEST_DELIVERY_TIMEOUT) -> str:
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
        if not self._use_email(config) and not self._use_serverchan(config):
            logger.debug('没有可用渠道，跳过 [%s]', event_id)
            return 'disabled'

        key = (event_id, dedup_key or '')
        window = EVENT_COALESCE_SECONDS.get(event_id, 0)
        now = time.time()
        if window > 0:
            with self._rate_lock:
                self._prune_rate_table(now)
                last = self._last_send.get(key, 0.0)
                if now - last < window:
                    self._bump('coalesced')
                    logger.debug('限频合并 [%s/%s]（距上次 %.0fs）',
                                 event_id, dedup_key, now - last)
                    return 'coalesced'
                self._last_send[key] = now

        job = _NotificationJob(event_id=event_id, subject=subject, body=body,
                               html=html, dedup_key=dedup_key)
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            # 队列满：丢弃并计数，绝不无限堆积，也绝不阻塞直播线程。
            self._bump('dropped')
            logger.warning('通知队列已满，丢弃一条 [%s]', event_id)
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
                self._deliver(job)
            except Exception as exc:
                logger.error('通知投递异常：%s', exc)
                job.results['error'] = 'failed'
            finally:
                job.done.set()
                self._queue.task_done()

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
                data={'title': f'[直播控制] {title}', 'desp': body},
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
            msg["Subject"] = f"[直播控制] {subject}"
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

    @staticmethod
    def _facts_lines(facts: Dict[str, Any], order=None) -> str:
        keys = order or [k for k in facts if facts.get(k) not in (None, '')]
        return '\n'.join(f"{k}：{facts[k]}" for k in keys if facts.get(k) not in (None, ''))

    def _instance_facts(self) -> Dict[str, Any]:
        return {'实例': self._instance_label()}

    def notify_start_accepted(self, zone: str, duration_label: str = '',
                              mode: str = '', room_id=None) -> str:
        """开播请求已受理——**未**证明媒体输出，措辞不得写成"画面正常"。

        注意：本方法产出**纯文本**正文，分区名等用户可控内容不做 HTML 转义
        （旧实现把 ``&amp;`` 直接写进纯文本/主题，2026-09-21 N4）。转义只在
        HTML 模板（每日简报表格）里做。
        """
        facts = self._instance_facts()
        facts.update({'分区': zone, '模式': mode or '任务模式',
                      '时长': duration_label or '未知'})
        if room_id:
            facts['房间'] = room_id
        body = ('开播请求已受理，正在启动（尚未确认媒体输出）\n\n'
                + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_START_ACCEPTED, f'[开播受理] {zone}',
                                 body, dedup_key=str(zone))

    def notify_stream_running(self, zone: str, source: str, elapsed_label: str = '',
                              duration_label: str = '', remaining_label: str = '',
                              room_id=None, run_id: str = '') -> str:
        """已进入运行阶段：区分新开/恢复/手动，带已播/目标/剩余与实例。"""
        source_label = {'new': '新开播', 'resume': '恢复续播',
                        'reconnect': '自动重连后继续', 'manual': '手动开播'}.get(
                            source, source)
        facts = self._instance_facts()
        facts.update({'分区': zone, '来源': source_label,
                      '已播': elapsed_label or '0 分钟',
                      '目标': duration_label or '未知',
                      '剩余': remaining_label or '未知'})
        if room_id:
            facts['房间'] = room_id
        body = ('直播已进入运行阶段（房间已开启，画面以实际推流为准）\n\n'
                + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_STREAM_RUNNING, f'[{source_label}] {zone}',
                                 body, dedup_key=f'{zone}:{run_id}')

    def notify_recovering(self, zone: str, attempt: int = 0,
                          reason: str = '', run_id: str = '') -> str:
        """持续恢复中：同一故障按身份合并，不是每次重试一封。

        合并键只含**稳定的故障身份**（分区+场次）：旧实现把可变的 reason
        拼进键里，"第 1 次/第 20 次"文案一变就成了新故障（2026-09-21 N3）。
        新一场直播有新的 run_id，不会被前一场压掉。
        """
        facts = self._instance_facts()
        facts.update({'分区': zone, '范围': '平台直播间',
                      '第几次': attempt or '-',
                      '现象': reason or '直播间状态异常'})
        body = ('直播中断，后台持续恢复中（同一故障合并提示，不逐次发送）\n\n'
                + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_RECOVERING, f'[持续恢复中] {zone}',
                                 body, dedup_key=f'{zone}:{run_id}')

    def notify_local_stream_fail(self, zone: str, reason: str = '',
                                 run_id: str = '') -> str:
        """本地推流（FFmpeg）故障的有界合并提醒——与平台房间故障**分范围**。

        复用既有失败信号（``推流`` warning 事件），不新增网络探针、不改重试
        节奏。合并键只含稳定身份。入口没有本地 FFmpeg 循环的结构化阶段，
        因此统一使用中性"正在重试"，不拿平台房间的重连计数推断本地次数
        或长间隔阶段；也**不做无生产触发点的恢复承诺**——本地重建成功
        目前没有配对的通知调用，恢复状态以面板为准。
        """
        stage_line = '本地推流遇到问题，正在重试。'
        stage_fact = '正在重试，恢复状态以面板为准'
        facts = self._instance_facts()
        facts.update({'分区': zone,
                      '范围': '本地推流输出（FFmpeg）；平台房间状态以面板为准',
                      '处置': stage_fact})
        if reason:
            facts['现象'] = reason
        body = (stage_line
                + '平台房间是否正常、是否已恢复，以面板状态为准。\n\n'
                + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_LOCAL_FAIL, f'[本地推流异常] {zone}',
                                 body, dedup_key=f'{zone}:{run_id}:local-fail')

    def notify_recovered(self, zone: str, scope: str = 'platform',
                         evidence: str = '', run_id: str = '') -> str:
        """恢复成功：必须指明恢复的是平台房间还是本地输出，附证据。"""
        scope_label = {'platform': '平台直播间已重新开启',
                       'local': '本地推流已重新启动'}.get(
                           scope, f'恢复范围：{scope}')
        facts = self._instance_facts()
        facts.update({'分区': zone, '恢复内容': scope_label,
                      '依据': evidence or '平台返回成功'})
        body = ('恢复成功（仅表示该项已恢复，不代表画面一定正常）\n\n'
                + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_RECOVERED, f'[已恢复] {zone}',
                                 body, dedup_key=f'{zone}:{run_id}:{scope}')

    def notify_action_required(self, title: str, reason: str,
                               still_streaming: bool = False,
                               action: str = '', identity: str = '') -> str:
        """需人工处理：说明原因、当前是否仍在推流、可操作入口。

        ``identity`` 只参与合并键，**不再**写进用户可见正文（run_id/epoch
        是内部身份，用户不需要也读不懂，2026-09-21 N4）。标题与原因分开：
        标题给类别，正文给一次完整原因，不重复贴两遍。
        """
        facts = self._instance_facts()
        facts.update({'原因': reason,
                      '当前是否仍在推流': '是' if still_streaming else '否'})
        if action:
            facts['建议操作'] = action
        body = (f'{title}\n\n' + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_ACTION_REQUIRED, f'[需处理] {title}',
                                 body, dedup_key=f'{title}:{identity}')

    def notify_stopped(self, zone: str, stage: str = 'stopped',
                       elapsed_label: str = '', reason: str = '') -> str:
        """真实停止结果：停止中/已停止/结果待确认分别表述。

        ``pending_recycle``（清理未确认）**不得**写"已停止"——那个时刻可能
        仍持有活的推流进程（2026-09-21 R3 监督反例：枚举正确但邮件/面板
        都写了"已停止"）。用户能区分：停止请求已受理 ≠ 停止结果已确认。
        """
        stage_label = {'stopping': '停止中（正在清理）',
                       'stopped': '已停止',
                       'pending_recycle': '停止处理中（结果待确认）'}.get(
                           stage, stage)
        lead = ('直播停止结果' if stage == 'stopped'
                else '直播停止进展（结果尚待确认）' if stage == 'pending_recycle'
                else '直播停止结果')
        facts = self._instance_facts()
        facts.update({'分区': zone, '状态': stage_label,
                      '已播': elapsed_label or '-'})
        if reason:
            facts['说明'] = reason
        body = (lead + '\n\n' + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_STOPPED, f'[{stage_label}] {zone}',
                                 body, dedup_key=f'{zone}:{stage}')

    def notify_task_complete(self, zone: str, business_date: str,
                             scope: str = 'today', result: str = 'settled',
                             progress: str = '') -> str:
        """结算已提交之后才发；结果决定**事件类别与标题**。

        - ``failed``：结算失败是"需人工确认"的异常——走 action_required、
          服从**异常开关**，标题写"任务结算失败"。旧实现失败也发
          task_complete、标题"[今日完成]"，用户关掉成功通知连失败提醒
          一起消失（2026-09-21 N1）。
        - ``already``：此前已结算——不冒充新完成，标题明确区分。
        - ``settled``：真正的成功提交，才用"今日完成/全部完成"。
        """
        scope_label = {'today': '今日完成', 'all': '全部完成',
                       'expired': '超期按规则结束'}.get(scope, scope)
        result_label = {'settled': '已提交', 'already': '此前已结算（未重复计天）',
                        'failed': '结算失败，需人工确认'}.get(result, result)
        facts = self._instance_facts()
        facts.update({'分区': zone, '业务日期': business_date,
                      '完成类型': scope_label, '结果': result_label})
        if progress:
            facts['进度'] = progress
        body = ('任务结算\n\n' + self._facts_lines(facts)
                + f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if result == 'failed':
            return self.notify_event(
                EVENT_ACTION_REQUIRED,
                f'[任务结算失败] {zone}',
                body, dedup_key=f'{zone}:{business_date}:{scope}:failed')
        if result == 'already':
            subject = f'[此前已结算] {zone}'
        else:
            subject = f'[{scope_label}] {zone}'
        return self.notify_event(
            EVENT_TASK_COMPLETE, subject,
            body, dedup_key=f'{zone}:{business_date}:{scope}')

    def send_daily_summary(self, stats: dict, top5: list,
                           summary_date=None, snapshot_meta: Optional[dict] = None
                           ) -> str:
        """每日简报：**统计日**与发送时刻分列，昨日结果不与今日计划混表。

        两个区块都来自**同一份冻结快照**（调用方 TaskManager 在重置前一次
        捕获）：
        - "统计日结果"：该日完成了多少、还差多少（today_done/today_pending）；
        - "待办快照"：截至快照时刻的待办与 Top5，剩余天数用权威派生口径
          （remaining_exec_days，与计算列 I 同义；旧列默认 1 从不重算的
          缺陷不再出现在邮件里，2026-09-21 DS1）。

        字段缺失显示"未取得"而不是 0——合法 0 与未知必须区分（DS4）。
        主题带实例身份与统计日：同收件人的多实例各自独立一封，不互相去重、
        也不合并（C4）。
        """
        stats = stats or {}
        top5 = top5 or []
        meta = snapshot_meta or {}
        day_label = summary_date.strftime('%Y-%m-%d') if summary_date else \
            datetime.now().strftime('%Y-%m-%d')
        instance = self._instance_label()

        def field(key, default='未取得'):
            value = stats.get(key)
            if value is None:
                return default
            return escape_html(value)

        as_of = meta.get('as_of')
        as_of_label = as_of.strftime('%Y-%m-%d %H:%M:%S') \
            if isinstance(as_of, datetime) else (str(as_of) if as_of else '未记录')
        revision = meta.get('revision')
        revision_label = revision if revision is not None else '未记录'

        # 完成标记的归属窗口（R2）：只有窗口 == 统计日时，完成数才能写成
        # "当日已完成 N"。窗口是别的日子（多日未重置/延迟重置）或未知
        # （字段缺失、与重置边界不一致）→ 如实"未取得"+说明，绝不把
        # 历史完成硬贴成统计日的结果。待办始终是**当前口径**（可信）。
        done_flags = meta.get('done_flags') or {}
        done_window = done_flags.get('window')
        done_confident = bool(done_flags.get('confident'))
        window_matches_day = (done_confident and summary_date is not None
                              and done_window == summary_date)
        done_value = field('today_done') if window_matches_day else '未取得'
        if window_matches_day:
            day_note = ''
        elif done_confident and done_window is not None:
            if not done_flags.get('done_rows'):
                day_note = (f'自 {escape_html(done_window.strftime("%Y-%m-%d"))} '
                            '的重置以来无完成记录；该口径不按统计日拆分，'
                            '不并入本简报')
            else:
                day_note = (f'最近完成标记日为 '
                            f'{escape_html(done_window.strftime("%Y-%m-%d"))}'
                            '（非统计日），完成结果不并入本简报')
        else:
            day_note = '完成标记缺少可归属的业务日（字段缺失或与重置边界不一致）'

        day_note_row = (f'<tr><td colspan="2" style="color:#909399">'
                        f'{escape_html(day_note)}</td></tr>') if day_note else ''

        rows = ''
        for i, t in enumerate(top5, 1):
            remaining = t.get('remaining_exec_days', t.get('remaining_days'))
            remaining_label = remaining if remaining is not None else '未取得'
            rows += (
                f"<tr><td>{i}</td><td>{escape_html(t.get('zone_name', ''))}</td>"
                f"<td>{escape_html(t.get('priority', ''))}</td>"
                f"<td>{escape_html(t.get('days_done', ''))}/"
                f"{escape_html(t.get('actual_days', ''))}</td>"
                f"<td>{escape_html(remaining_label)}</td></tr>")

        body = f"""<html><body>
<h2>每日直播简报</h2>
<p><b>实例：</b>{escape_html(instance)}</p>
<p><b>统计日期：</b>{escape_html(day_label)}</p>
<p><b>发送时刻：</b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<hr>
<h3>统计日（{escape_html(day_label)}）执行结果</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><td><b>当日已完成</b></td><td>{done_value}</td></tr>
<tr><td><b>待执行任务（当前口径）</b></td><td>{field('today_pending')}</td></tr>
{day_note_row}
</table>
<hr>
<h3>待办快照（{escape_html(as_of_label)} 采集 · 版本 {escape_html(revision_label)}）</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><td><b>待完成任务</b></td><td>{field('pending_total')}</td></tr>
<tr><td><b>剩余时间</b></td><td>{field('remaining_time')} 小时</td></tr>
<tr><td><b>平均剩余</b></td><td>{field('avg_remaining')} 天（截止松弛 + 待执行天数，既有公式）</td></tr>
<tr><td><b>紧迫率</b></td><td>{field('urgency')}（剩余时间 ÷ (平均剩余×20)，仅参考）</td></tr>
</table>
<h4>优先度最高 Top5（"剩余"为还需执行天数，已完成任务为 0）</h4>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><th>#</th><th>分区</th><th>优先度</th><th>进度</th><th>剩余待执行天数</th></tr>
{rows}
</table>
<p style="color:#909399;font-size:12px">直播控制系统 · 自动发送（实际直播时长以有效时长统计为准，本简报不含未经确认的直播小时）</p>
</body></html>"""
        return self.notify_event(
            EVENT_DAILY_SUMMARY,
            f"[每日简报] {instance} {day_label}",
            body, html=True,
            dedup_key=f'{instance}:{day_label}')

    def send_test(self) -> str:
        """渠道测试：等待真实投递结果（排队不等于已发送）。"""
        body = (f"这是一封来自 直播控制系统的测试邮件。\n\n"
                f"实例：{self._instance_label()}\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return self.notify_event(EVENT_TEST, '[测试] 推送测试', body, wait=True)

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
                         **intent) -> str:
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
        server_host = self._get_server_host()
        # Loopback is intentional for desktop: the FastAPI confirmation route
        # is owned by this local backend.  A non-loopback host is only used
        # when the user explicitly configured it.
        host_usable = bool(server_host)
        lines = [stage_label, '']
        if host_usable:
            confirm_url = self._confirm_url(token)
            lines += ['请点击以下链接确认验证完成：',
                      confirm_url,
                      '',
                      '或在系统面板的验证弹窗中点击"验证完成"。']
        else:
            # 公共确认地址缺失时不生成 localhost 假链接（外部点了打不开，
            # 还像钓鱼邮件）：给出明确的面板替代指引（2026-09-21 N4）。
            lines += ['当前未配置公网访问地址，无法生成可点击的确认链接。',
                      '请打开系统面板，在验证弹窗中点击"验证完成"。']
        lines += ['', 'B站验证页面：', verify_url, '']
        if stage == 'status_query':
            lines.append('处置说明：本路径仅提示验证，未停止推流；'
                         '实际是否在播以面板状态为准。')
        elif local_stop_requested:
            lines.append('处置说明：本路径已请求停止本地推流；'
                         '完成验证后可在面板重试开播。')
        else:
            lines.append('处置说明：本路径未停止推流；'
                         '如需中断请先在面板点停止。')
        lines += ['', f'实例：{self._instance_label()}',
                  f'时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}']
        return self.notify_event(
            EVENT_ACTION_REQUIRED,
            f'[需要人脸验证] {stage_label}',
            '\n'.join(lines),
            dedup_key=f'face_verify:{stage}:{run_id or room_id or ""}',
        )

    def _confirm_url(self, token: str) -> str:
        server_host = self._get_server_host()
        port = os.environ.get('API_PORT', '8000')
        scheme = 'https' if os.environ.get('API_SSL') else 'http'
        return f"{scheme}://{server_host}:{port}/api/email/confirm-face-verify?token={token}"

    def start_face_verify_server(self):
        """Compatibility no-op: desktop FastAPI owns the confirmation route.

        The old desktop sender opened a second HTTP listener.  Keeping the
        method lets the existing lifespan call remain harmless while ensuring
        there is exactly one local confirmation service.
        """
        return None

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
            if frozen_room is not None \
                    and getattr(lc, 'current_room_id', None) is not None:
                try:
                    if int(frozen_room) != int(lc.current_room_id):
                        logger.info("人脸验证确认属于另一个房间，不再重试开播")
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
            instruction = getattr(lc, 'current_instruction', None)
            if not instruction:
                logger.info("人脸验证确认到达时已无待开播指令，不再重试")
                return
            # 手动验证后的恢复：沿用原模式，不强制改成任务模式
            is_task_mode = saved_mode != 'manual'
            source = intent.get('source') or ('new' if is_task_mode else 'manual')
            inherit = intent.get('inherit_elapsed')
            # A resume instruction is a saved task identity, not merely a
            # matching run/epoch.  Recheck the current task while the task
            # mutation lock is held before dispatching the platform request;
            # the start path performs the final atomic reservation again after
            # the platform response to close the remaining race.
            if source == 'resume':
                validator = getattr(lc, 'validate_face_verify_retry_intent', None)
                if not callable(validator) or not validator(intent):
                    logger.info("人脸验证恢复目标已删除、完成或被替换，不再重试开播")
                    return
            logger.info("人脸验证已确认，重试开播（模式：%s，来源：%s，继承：%s 秒）",
                        '任务' if is_task_mode else '手动', source, inherit)
            lc.start_streaming(instruction, '',
                               is_task_mode=is_task_mode, epoch=frozen_epoch,
                               source=source, inherit_elapsed=inherit)
        except Exception as e:
            logger.error(f"人脸验证后重试失败：{e}")

    def shutdown(self) -> None:
        """关闭 worker：停止受理、**丢弃排队通知**，再等在途投递结束。

        为什么必须显式清空队列：worker 一旦退出，队列里剩下的任务既不会被
        投递、也不会被回收，却仍然"存在"——任何后续再装/卸 SMTP 替身的顺序
        变化都会让它们打到真实渠道（测试收尾时曾出现对真实 SMTP 的连接尝试）。
        丢弃量计入 dropped 计数，不静默。
        """
        self._closing.set()
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            self._bump("dropped")
            logger.info("关闭通知器：丢弃 %d 条排队中的通知", dropped)
        for thread in self._workers:
            thread.join(timeout=3.0)
