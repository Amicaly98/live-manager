"""
email.py - 邮箱推送相关 API（测试发送 + 人脸验证确认）
"""

import asyncio
import concurrent.futures
import logging
import threading
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.dependencies import get_email_sender, get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()

# Waiting for a real channel result must not block FastAPI's event loop.  A
# single dedicated lane also bounds concurrent manual test deliveries.
_TEST_DELIVERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix='email-test')

_HTML_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>验证成功</title>
<style>body{display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;
font-family:system-ui;background:#f0f9e8;color:#2d6a1e}
.box{text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1)}
h1{font-size:48px;margin:0 0 16px}p{color:#606266;margin:8px 0 0}</style></head>
<body><div class="box"><h1>✅</h1><h2>人脸验证已确认</h2>
<p>正在重试开播，可以关闭此页面。</p></div></body></html>"""

_HTML_CONFIRM_TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>确认验证完成</title>
<style>body{display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;
font-family:system-ui;background:#f0f9e8;color:#2d6a1e}
.box{text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1)}
h1{font-size:48px;margin:0 0 16px}p{color:#606266;margin:8px 0 0}
button{margin-top:20px;padding:12px 28px;font-size:16px;border:0;border-radius:8px;
background:#2d6a1e;color:#fff;cursor:pointer}</style></head>
<body><div class="box"><h1>🔐</h1><h2>确认人脸验证已完成</h2>
<p>此链接当前有效。点击下方按钮才会真正执行确认并重试开播。</p>
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
<p>请重新从邮件中点击链接，或在面板中手动重试。</p></div></body></html>"""


def _current_identity():
    """取当前会话身份，用于校验确认链接是否仍然属于这一场。

    身份优先取**冻结的意图身份**（_current_run_id/_pending_run_id）：首次开播
    在验证发生时还没有建立会话存档，若只看 state.run_id 会拿到上一场的旧 id，
    把刚刚发出的有效链接误判为"属于另一个会话"。
    """
    lc = get_live_controller()
    if not lc:
        return None, '', None, None
    run_id = (getattr(lc, '_current_run_id', '') or getattr(lc, '_pending_run_id', '')
              or getattr(getattr(lc, 'state', None), 'run_id', '') or '')
    return lc, run_id, lc.current_room_id, getattr(lc, '_control_epoch', None)


def _frozen_intent(lc, info):
    """把"确认那一刻"的启动意图冻结成一份不可变事实，交给重试线程。

    通知发生时的事实（模式/来源/继承进度/分区/task_id/执行日）随 token 保存，
    重试时原样使用；不能由晚到的 worker 重新读当前状态去拼——那正是"恢复被
    归零"和"旧确认复活新一场"的来源。
    """
    info = dict(info or {})
    instruction = getattr(lc, 'current_instruction', None)
    fallback_run = (getattr(lc, '_current_run_id', '')
                    or getattr(lc, '_pending_run_id', '') or '')
    intent = {
        'run_id': info.get('run_id') or fallback_run,
        'room_id': info.get('room_id', getattr(lc, 'current_room_id', None)),
        'epoch': info.get('epoch'),
        'mode': info.get('mode') or getattr(lc, '_verify_retry_mode', None),
        'source': info.get('source'),
        'inherit_elapsed': info.get('inherit_elapsed'),
    }
    if 'zone' in info or instruction is not None:
        intent['zone'] = info.get('zone') or getattr(instruction, 'zone_name', '')
    if 'task_id' in info or instruction is not None:
        intent['task_id'] = info.get('task_id',
                                     getattr(instruction, 'task_id', None))
    if 'execution_date' in info or instruction is not None:
        intent['execution_date'] = info.get(
            'execution_date', getattr(instruction, 'execution_date', None))
    return intent


async def _read_confirmation_token(request: Request) -> str:
    """把确认 token 从**三种**载体里读出来：查询串 / 表单 / JSON。

    旧的 POST 路由把 token 声明成普通查询参数，而邮件里生成的却是
    `<form method=\"post\">` + 隐藏 input：按网页按钮提交时后端拿到的是空串
    （表单体根本不会被解析），确认永远失败。
    """
    token = (request.query_params.get('token') or '').strip()
    if token:
        return token
    content_type = (request.headers.get('content-type') or '').lower()
    if 'multipart/form-data' in content_type:
        try:
            form = await request.form()
            return str(form.get('token') or '').strip()
        except Exception as exc:            # 缺 python-multipart 等：明确失败，不静默成空串
            logger.warning('解析 multipart 表单失败：%s', exc)
            return ''
    if 'application/x-www-form-urlencoded' in content_type:
        raw = (await request.body()).decode('utf-8', 'replace')
        values = parse_qs(raw, keep_blank_values=True).get('token') or ['']
        return values[0].strip()
    if 'application/json' in content_type:
        try:
            payload = await request.json()
        except Exception:
            return ''
        if isinstance(payload, dict):
            return str(payload.get('token') or '').strip()
    return ''


def _do_confirm(token: str):
    """执行确认逻辑，返回 (success, message)

    只有**显式 POST** 才走到这里。token 与会话身份（run_id/房间/控制代际）
    绑定并带有效期：旧邮件链接不得清掉新会话的验证/恢复阻塞状态。
    """
    sender = get_email_sender()
    lc = get_live_controller()
    if not sender or not lc:
        return False, '服务未初始化'
    lc, run_id, room_id, epoch = _current_identity()
    info = None
    consumer_ex = getattr(sender, 'consume_face_verify_token_ex', None)
    consumer = getattr(sender, 'consume_face_verify_token', None)
    if callable(consumer_ex):
        ok, reason, info = consumer_ex(token, run_id=run_id, room_id=room_id,
                                       epoch=epoch)
    elif callable(consumer):
        ok, reason = consumer(token, run_id=run_id, room_id=room_id, epoch=epoch)
    else:
        # 旧通知器没有 token 能力：退回最小行为（存在即消费）
        ok = bool(token) and token in getattr(sender, '_face_verify_tokens', {})
        reason = 'ok' if ok else '无效的确认链接'
    if not ok:
        return False, reason
    # 用户已请求停止时不得借确认复活开播
    if getattr(lc, 'stop_monitor', None) is not None and lc.stop_monitor.is_set():
        return False, '已请求停止，未重试开播'
    # "token 还能消费"不等于"这次确认仍属于当前会话"：从消费成功到清除验证
    # 阻塞之间可能已停止旧会话并开播新的一场。身份核对与清除必须在控制器的
    # **同一把意图锁**内完成；不匹配时既不清新会话状态，也不回报已恢复、
    # 也不派发后台重试线程。
    try:
        confirmed = lc.confirm_face_verify(run_id=run_id, room_id=room_id,
                                           epoch=epoch)
    except TypeError as exc:
        # A real signature/type failure must fail closed; never retry without
        # identity arguments and accidentally confirm another session.
        logger.warning('确认人脸验证身份校验失败：%s', exc)
        return False, '确认链接身份校验不可用'
    if not confirmed:
        return False, '确认链接已过期：该会话已结束或被新的会话取代'
    intent = _frozen_intent(lc, info)
    retry = getattr(sender, '_retry_after_face_verify', None)
    if callable(retry):
        threading.Thread(target=retry, kwargs={'intent': intent},
                         daemon=True).start()
    return True, '人脸验证已确认，正在重试开播'


@router.get("/confirm-face-verify", summary="人脸验证确认页（仅预览，不执行）")
async def confirm_face_verify_get(request: Request, token: str = ""):
    """邮件中的链接通过浏览器 GET 访问：只展示状态，**不消费、不执行**。

    邮件客户端/安全扫描的预取不应造成任何控制动作（旧实现 GET 就会消费
    token 并触发重新开播）。真正的确认必须点页面上的按钮发 POST。
    预览同样校验**有效期与当前身份**，不只是"字典里有没有"。
    """
    token = token or (request.query_params.get('token') or '')
    sender = get_email_sender()
    lc, run_id, room_id, epoch = _current_identity()
    valid = False
    if sender:
        peek = getattr(sender, 'peek_face_verify_token', None)
        if callable(peek):
            try:
                valid = peek(token, run_id=run_id, room_id=room_id,
                             epoch=epoch) is not None
            except TypeError as exc:
                logger.warning('确认链接身份校验接口不兼容：%s', exc)
                valid = False
        else:
            valid = bool(token) and token in getattr(sender, '_face_verify_tokens', {})
    if not valid:
        return HTMLResponse(content=_HTML_FAIL_TPL % '确认链接无效或已过期',
                            status_code=400)
    return HTMLResponse(content=_HTML_CONFIRM_TPL % token)


@router.post("/confirm-face-verify", summary="远程确认人脸验证（表单/查询/JSON）")
async def confirm_face_verify_post(request: Request):
    """确认入口：**接受邮件页面表单提交**，也接受查询串与 JSON。

    旧实现把 token 声明成查询参数，而邮件正文生成的是 POST 表单——真实按钮
    提交时后端收到的是空串（`_do_confirm('')` 必然失败）。这里统一从一个
    入口读取 token，三种载体都可达。至多生效一次（token 消费是原子的）。
    """
    token = await _read_confirmation_token(request)
    ok, msg = _do_confirm(token)
    return {'success': ok, 'message': msg}


@router.post("/test", summary="发送测试推送")
async def send_test_email():
    """发一条测试通知，返回**真实**投递结果。

    旧实现无论禁用、无收件人、限频跳过还是后台发送失败都先返回"已发送"。
    这里等待投递结束，把 disabled/queued/sent/failed 如实返回。
    """
    sender = get_email_sender()
    if not sender:
        return {'success': False, 'status': 'disabled',
                'message': '推送发送器未初始化'}
    try:
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(
            _TEST_DELIVERY_EXECUTOR, sender.send_test)
    except Exception as e:
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
