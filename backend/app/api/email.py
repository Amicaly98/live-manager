"""email.py - 邮箱推送相关 API（测试发送 ＋ 人脸验证确认）

阶段 4B 起，人脸确认与面板走**同一个**唯一入口：``ControlService.confirm``。
令牌在事实形成时冻结 ``goal_revision`` / ``uid`` / ``room_id``；旧停止、退出或
新目标之后，冻结值不再匹配当前事实，确认自然失效——由唯一 Core 的
``UserConfirmation`` 身份核对裁定，不是本文件另设一道版本门。

邮件链接：**GET 只预览，不消费、不执行**；**POST 一次消费**。
"""

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.session_runtime.control import ControlServiceError
from app.dependencies import get_application_owner, get_control_service, get_email_sender

logger = logging.getLogger(__name__)
router = APIRouter()

_PRIORITY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='email-confirm')

_HTML_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>验证成功</title>
<style>body{display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;
font-family:system-ui;background:#f0f9e8;color:#2d6a1e}
.box{text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1)}
h1{font-size:48px;margin:0 0 16px}p{color:#606266;margin:8px 0 0}</style></head>
<body><div class="box"><h1>✅</h1><h2>人脸验证已确认</h2>
<p>已按原启动意图继续处理。如果直播已在播，不会重复开播；可以关闭此页面。</p>
</div></body></html>"""

_HTML_CONFIRM_TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>确认验证完成</title>
<style>body{display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;
font-family:system-ui;background:#f0f9e8;color:#2d6a1e}
.box{text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1)}
h1{font-size:48px;margin:0 0 16px}p{color:#606266;margin:8px 0 0}
button{margin-top:20px;padding:12px 28px;font-size:16px;border:0;border-radius:8px;
background:#2d6a1e;color:#fff;cursor:pointer}</style></head>
<body><div class="box"><h1>🔐</h1><h2>确认人脸验证已完成</h2>
<p>此链接当前有效。点击下方按钮才会真正执行确认并继续原启动意图。</p>
<form method="post" action="/api/email/confirm-face-verify">
<input type="hidden" name="token" value="%s">
<button type="submit">我已完成验证</button>
</form></div></body></html>"""

_HTML_FAIL_TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>验证失败</title>
<style>body{display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;
font-family:system-ui;background:#fef0f0;color:#c45656}
.box{text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1)}
h1{font-size:48px;margin:0 0 16px}p{color:#909399;margin:8px 0 0}</style></head>
<body><div class="box"><h1>❌</h1><h2>%s</h2>
<p>请重新从邮件中点击链接，或在面板中重试。</p></div></body></html>"""


def _current_goal_revision() -> int:
    control = get_control_service()
    if control is None:
        return -1
    snapshot = control.read() or {}
    goal = snapshot.get('goal') or {}
    try:
        return int(goal.get('revision'))
    except (TypeError, ValueError):
        return -1


async def _read_confirmation_token(request: Request) -> str:
    """从三种载体读 token：查询串 / 表单 / JSON（邮件页面用表单提交）。"""
    token = (request.query_params.get('token') or '').strip()
    if token:
        return token
    content_type = (request.headers.get('content-type') or '').lower()
    if 'multipart/form-data' in content_type:
        try:
            form = await request.form()
            return str(form.get('token') or '').strip()
        except Exception as exc:                      # noqa: BLE001
            logger.warning('解析 multipart 表单失败：%s', exc)
            return ''
    if 'application/x-www-form-urlencoded' in content_type:
        from urllib.parse import parse_qs
        raw = (await request.body()).decode('utf-8', 'replace')
        values = parse_qs(raw, keep_blank_values=True).get('token') or ['']
        return values[0].strip()
    if 'application/json' in content_type:
        try:
            payload = await request.json()
        except Exception:                             # noqa: BLE001
            return ''
        if isinstance(payload, dict):
            return str(payload.get('token') or '').strip()
    return ''


def _do_confirm(token: str):
    """POST 才走到这里：消费令牌 → 唯一控制面确认。返回 (success, message)。"""
    sender = get_email_sender()
    control = get_control_service()
    if not sender or control is None:
        return False, '服务未初始化'
    consumer = getattr(sender, 'consume_face_verify_token_ex', None)
    if not callable(consumer):
        return False, '确认通道不可用'
    ok, reason, info = consumer(token)
    if not ok:
        return False, reason
    info = info or {}
    goal_revision = info.get('goal_revision')
    uid = info.get('uid')
    room_id = info.get('room_id')
    if goal_revision is None or uid is None or room_id is None:
        # 记录里缺必需身份：不猜当前值补齐，明确拒绝。
        return False, '该确认链接缺少必需身份信息，已失效'
    op_token = f'email-face:{token}'
    try:
        receipt = control.confirm(op_token, goal_revision=int(goal_revision),
                                  uid=int(uid), room_id=int(room_id))
    except ControlServiceError as exc:
        return False, str(exc)
    if receipt.accepted:
        return True, '人脸验证已确认，已按原启动意图继续处理'
    if receipt.status == 'ignored':
        return False, '确认链接已过期：该会话已结束或被新的会话取代'
    return False, (receipt.reason or '确认未被受理')


@router.get("/confirm-face-verify", summary="人脸验证确认页（仅预览，不执行）")
async def confirm_face_verify_get(request: Request, token: str = ""):
    """预览：**不消费、不执行**，但同样校验有效期与身份。"""
    token = token or (request.query_params.get('token') or '')
    sender = get_email_sender()
    info = None
    if sender is not None:
        peek = getattr(sender, 'peek_face_verify_token', None)
        if callable(peek):
            info = peek(token)
    if not info:
        return HTMLResponse(content=_HTML_FAIL_TPL % '确认链接无效或已过期',
                            status_code=400)
    if info.get('goal_revision') is not None \
            and int(info['goal_revision']) != _current_goal_revision():
        # 旧目标的确认不再有效：预览也说清楚，不给一个点了必然失败的按钮。
        return HTMLResponse(content=_HTML_FAIL_TPL % '该确认已不属于当前目标',
                            status_code=409)
    return HTMLResponse(content=_HTML_CONFIRM_TPL % token)


@router.post("/confirm-face-verify", summary="远程确认人脸验证（表单/查询/JSON）")
async def confirm_face_verify_post(request: Request):
    token = await _read_confirmation_token(request)
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(_PRIORITY_EXECUTOR,
                                         functools.partial(_do_confirm, token))
    content_type = (request.headers.get('content-type') or '').lower()
    accept = (request.headers.get('accept') or '').lower()
    browser_form = (('application/x-www-form-urlencoded' in content_type
                     or 'multipart/form-data' in content_type)
                    and 'text/html' in accept)
    if browser_form:
        if ok:
            return HTMLResponse(content=_HTML_OK)
        return HTMLResponse(content=_HTML_FAIL_TPL % escape(msg),
                            status_code=400)
    # 非浏览器提交也给出稳定可识别的失败码：旧链接/已消费 = 400。
    return JSONResponse(status_code=200 if ok else 400,
                        content={'success': ok, 'message': msg})


@router.post("/test", summary="发送测试推送")
async def send_test_email():
    """发一条测试通知，返回**真实**投递结果。"""
    sender = get_email_sender()
    if not sender:
        return {'success': False, 'status': 'disabled',
                'message': '推送发送器未初始化'}
    try:
        status = sender.send_test()
    except Exception as e:                            # noqa: BLE001
        return {'success': False, 'status': 'failed', 'message': f'发送失败：{e}'}
    messages = {
        'sent': '测试推送已送达，请检查收件箱/微信',
        'queued': '测试推送已排队，尚未确认送达',
        'unconfirmed': '等待投递超时，结果待确认：稍后可能仍会送达',
        'disabled': '推送未发送：总开关或事件开关已关闭，或没有可用渠道',
        'coalesced': '测试推送被限频合并，请稍后重试',
        'dropped': '测试推送被丢弃：通知队列已满',
        'failed': '测试推送发送失败，请检查渠道配置',
    }
    return {'success': status == 'sent', 'status': status,
            'message': messages.get(status, status)}
