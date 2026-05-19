"""
email_sender.py - 推送通知模块

支持：SMTP 邮件、Server酱（微信推送）、人脸验证远程确认（HTTP 端口）、每日简报。
所有推送异步发送（独立线程），不阻塞主流程。
"""

import json
import time
import smtplib
import logging
import threading
import uuid
import requests as _requests
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)


# ==================== EmailSender（同时支持 Server酱） ====================

class EmailSender:
    """推送通知器：SMTP 邮件 + Server酱微信推送 + 限频 + 异步"""

    def __init__(self):
        self._last_send: dict = {}          # {event_type: timestamp} 限频
        self._face_verify_tokens: dict = {}  # {token: expiry_timestamp}
        self._http_server: Optional[HTTPServer] = None
        self._http_port: int = 0

    # ---------- 配置读取 ----------

    def _load_config(self) -> dict:
        """读取推送配置"""
        try:
            from app.api.settings import load_settings
            s = load_settings()
            return {
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
            return {'channel': 'email', 'email_enabled': False}

    def _use_email(self, config: dict) -> bool:
        return config.get('channel') == 'email' and config.get('email_enabled') and config.get('recipients')

    def _use_serverchan(self, config: dict) -> bool:
        return config.get('channel') == 'serverchan' and bool(config.get('serverchan_sendkey'))

    # ---------- Server酱 发送 ----------

    def _send_serverchan(self, title: str, content: str, event_type: str):
        """Server酱（Turbo版）推送：https://sctapi.ftqq.com/{SENDKEY}.send"""
        config = self._load_config()
        sendkey = config.get('serverchan_sendkey', '')
        if not sendkey:
            return
        try:
            url = f"https://sctapi.ftqq.com/{sendkey}.send"
            # 截断过长内容（Server酱限制）
            short = content[:500] if len(content) > 500 else content
            resp = _requests.post(url, data={
                'title': f'[直播控制] {title}',
                'desp': short,
            }, timeout=10)
            result = resp.json()
            if result.get('code') == 0:
                logger.info(f"Server酱推送成功：{title}")
            else:
                logger.warning(f"Server酱推送失败：{result.get('message', '未知')}")
        except Exception as e:
            logger.error(f"Server酱推送异常：{e}")

    def send(self, subject: str, body: str, event_type: str = "info", html: bool = False):
        """异步发送推送（独立线程）。根据配置分发到邮箱/Server酱/双选。
        限频：同类型事件 3 分钟内最多发一封，防止刷屏。
        """
        config = self._load_config()

        # 检查通知开关
        if event_type in ('start', 'task_start') and not config['notify_start']:
            return
        if event_type in ('stop', 'task_stop') and not config['notify_stop']:
            return
        if event_type in ('error', 'face_verify') and not config['notify_error']:
            return
        if event_type in ('complete', 'task_done') and not config['notify_complete']:
            return

        # 检查至少有一个渠道可用
        if not self._use_email(config) and not self._use_serverchan(config):
            return

        # 限频
        now = time.time()
        last = self._last_send.get(event_type, 0)
        if now - last < 180:
            logger.debug(f"推送限频跳过 [{event_type}]（距上次 {now - last:.0f}s）")
            return
        self._last_send[event_type] = now

        thread = threading.Thread(
            target=self._do_send_all,
            args=(config, subject, body, html, event_type),
            daemon=True,
            name=f"Notify-{event_type}"
        )
        thread.start()

    def _do_send_all(self, config: dict, subject: str, body: str, html: bool, event_type: str):
        """同时发邮箱和 Server酱（按配置）"""
        if self._use_email(config):
            self._do_send_email(config, subject, body, html)
        if self._use_serverchan(config):
            # Server酱用纯文本（或 HTML 去标签后的文本）
            plain_body = body
            if html:
                import re as _re
                plain_body = _re.sub(r'<[^>]+>', '', body)
                plain_body = _re.sub(r'\n{3,}', '\n\n', plain_body)
            self._send_serverchan(subject, plain_body, event_type)

    def _do_send_email(self, config: dict, subject: str, body: str, html: bool = False):
        """实际 SMTP 发送"""
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[直播控制] {subject}"
            msg["From"] = config['smtp_user']
            msg["To"] = config['recipients']
            msg.attach(MIMEText(body, "plain" if not html else "html", "utf-8"))

            with smtplib.SMTP(config['smtp_host'], config['smtp_port'], timeout=15) as smtp:
                smtp.starttls()
                smtp.login(config['smtp_user'], config['smtp_pass'])
                smtp.sendmail(
                    config['smtp_user'],
                    [r.strip() for r in config['recipients'].split(',') if r.strip()],
                    msg.as_string()
                )
            logger.info(f"邮件已发送 -> {config['recipients'][:30]}... | 主题：{subject}")
        except Exception as e:
            logger.error(f"邮件发送失败：{e}")

    # ---------- 便捷发送方法 ----------

    def send_task_start(self, zone_name: str, duration_label: str):
        self.send(
            subject=f"[开播] {zone_name}",
            body=f"直播已开始\n\n分区：{zone_name}\n时长：{duration_label}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            event_type="start"
        )

    def send_task_complete(self, zone_name: str, elapsed_label: str):
        self.send(
            subject=f"[完成] {zone_name}",
            body=f"任务已完成\n\n分区：{zone_name}\n已播：{elapsed_label}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            event_type="complete"
        )

    def send_error(self, error_type: str, detail: str):
        self.send(
            subject=f"[异常] {error_type}",
            body=f"直播异常\n\n类型：{error_type}\n详情：{detail}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            event_type="error"
        )

    def send_face_verify(self, verify_url: str):
        """发送人脸验证邮件，含远程确认链接"""
        # 生成一次性 token
        token = str(uuid.uuid4())[:12]
        port = self._load_config().get('face_verify_port', 18080)
        confirm_url = f"http://localhost:{port}/confirm?token={token}"
        self._face_verify_tokens[token] = time.time() + 1800  # 30分钟有效

        body = f"""需要人脸验证

请点击以下链接确认验证完成（30分钟内有效）：
{confirm_url}

或手动在应用中点击"验证完成"按钮。

B站验证页面：
{verify_url}

时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        self.send(
            subject="[需要人脸验证]",
            body=body,
            event_type="face_verify"
        )

    def send_reconnect_start(self, attempt: int, max_retries: int):
        self.send(
            subject=f"[重连中] 第 {attempt}/{max_retries} 次",
            body=f"直播间异常，正在尝试重连\n\n第 {attempt}/{max_retries} 次\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            event_type="error"
        )

    def send_reconnect_exhausted(self, cooldown_minutes: int):
        self.send(
            subject=f"[重试耗尽] 冷却 {cooldown_minutes} 分钟",
            body=f"重试次数已耗尽\n\n进入冷却期 {cooldown_minutes} 分钟\n后续将不再自动重连\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            event_type="error"
        )

    def send_daily_summary(self, stats: dict, top5: list):
        """发送每日简报（HTML 格式）"""
        rows = ""
        for i, t in enumerate(top5, 1):
            rows += f"<tr><td>{i}</td><td>{t['zone_name']}</td><td>{t['priority']}</td><td>{t['days_done']}/{t['actual_days']}</td><td>{t['remaining_days']}</td></tr>"

        body = f"""<html><body>
<h2>每日直播简报</h2>
<p><b>日期：</b>{datetime.now().strftime('%Y-%m-%d')}</p>
<hr>
<h3>任务统计</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><td><b>待完成</b></td><td>{stats.get('pending_total', 0)}</td></tr>
<tr><td><b>剩余时间</b></td><td>{stats.get('remaining_time', 0)} 小时</td></tr>
<tr><td><b>平均剩余</b></td><td>{stats.get('avg_remaining', 0):.2f}</td></tr>
<tr><td><b>紧迫率</b></td><td>{stats.get('urgency', 0) * 100:.2f}%</td></tr>
<tr><td><b>今日已执行</b></td><td>{stats.get('today_done', 0)}</td></tr>
<tr><td><b>今日待执行</b></td><td>{stats.get('today_pending', 0)}</td></tr>
</table>
<hr>
<h3>优先度最高 Top5</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><th>#</th><th>分区</th><th>优先度</th><th>进度</th><th>剩余天数</th></tr>
{rows}
</table>
<p style="color:#909399;font-size:12px">直播控制系统 · 自动发送</p>
</body></html>"""
        self.send(
            subject=f"[每日简报] {datetime.now().strftime('%m/%d')}",
            body=body,
            event_type="daily_summary",
            html=True
        )

    # ---------- 人脸验证远程确认 HTTP 服务 ----------

    def start_face_verify_server(self):
        """启动人脸验证远程确认 HTTP 服务器"""
        config = self._load_config()
        if not self._use_email(config) and not self._use_serverchan(config):
            return  # 没有任何推送渠道开启，不启动确认服务
        port = config.get('face_verify_port', 18080)
        if self._http_server:
            return  # 已启动

        sender_ref = self  # 闭包引用

        class VerifyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self._handle_get()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    pass  # 客户端已断开，静默忽略

            def _handle_get(self):
                if self.path.startswith('/confirm'):
                    import urllib.parse as up
                    qs = up.urlparse(self.path).query
                    params = up.parse_qs(qs)
                    token = params.get('token', [None])[0]
                    if token and token in sender_ref._face_verify_tokens:
                        expiry = sender_ref._face_verify_tokens[token]
                        if time.time() < expiry:
                            del sender_ref._face_verify_tokens[token]
                            # 触发确认
                            from app.dependencies import get_live_controller
                            lc = get_live_controller()
                            if lc:
                                lc.confirm_face_verify()
                                logger.info("人脸验证已通过远程链接确认，尝试重连...")
                                # 异步重试开播
                                threading.Thread(
                                    target=sender_ref._retry_after_face_verify,
                                    daemon=True
                                ).start()
                            self.send_response(200)
                            self.send_header('Content-type', 'text/html;charset=utf-8')
                            self.end_headers()
                            self.wfile.write('<h2>验证成功</h2><p>人脸验证已确认，正在重试开播。可以关闭此页面。</p>'.encode('utf-8'))
                            return
                    self.send_response(400)
                    self.send_header('Content-type', 'text/html;charset=utf-8')
                    self.end_headers()
                    self.wfile.write('<h2>链接无效或已过期</h2>'.encode('utf-8'))
                else:
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html;charset=utf-8')
                    self.end_headers()
                    self.wfile.write('<h2>B站直播控制 · 人脸验证确认服务</h2><p>请使用邮件中的确认链接。</p>'.encode('utf-8'))

            def log_message(self, format, *args):
                pass  # 静默 HTTP 日志

        try:
            self._http_server = HTTPServer(('0.0.0.0', port), VerifyHandler)
            thread = threading.Thread(
                target=self._http_server.serve_forever,
                daemon=True,
                name="FaceVerifyServer"
            )
            thread.start()
            self._http_port = port
            logger.info(f"人脸验证确认服务已启动 -> http://localhost:{port}/confirm?token=xxx")
        except OSError as e:
            logger.warning(f"人脸验证端口 {port} 被占用：{e}")

    def _retry_after_face_verify(self):
        """人脸验证确认后，等待 3 秒然后用完整流程重试开播"""
        time.sleep(3)
        try:
            from app.dependencies import get_live_controller
            lc = get_live_controller()
            if not lc or not lc.current_instruction:
                return
            # 重新查找视频
            video_path = lc.video_finder.find_video(lc.current_instruction.zone_name) or ''
            stream_mode, _ = lc._get_stream_settings()
            if stream_mode == 'ffmpeg' and not video_path:
                logger.warning("FFmpeg 模式但未找到视频文件，跳过重试")
                return
            logger.info("人脸验证已确认，启动完整开播流程...")
            lc.start_streaming(lc.current_instruction, video_path, is_task_mode=True)
        except Exception as e:
            logger.error(f"人脸验证后重试失败：{e}")

    def shutdown(self):
        """关闭 HTTP 服务"""
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
