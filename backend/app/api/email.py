"""
email.py - 邮箱推送相关 API（测试发送 + 人脸验证确认）
"""

import logging
from fastapi import APIRouter

from app.dependencies import get_email_sender, get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/test", summary="发送测试邮件")
async def send_test_email():
    """发送一封测试邮件到配置的收件人，用于验证 SMTP 配置"""
    sender = get_email_sender()
    if not sender:
        return {'success': False, 'message': '邮件发送器未初始化'}
    try:
        from datetime import datetime
        sender.send(
            subject="[测试] 推送测试",
            body=f"这是一封来自 直播控制系统的测试邮件。\n\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n如果收到此邮件，说明邮箱配置正确。",
            event_type="test"
        )
        return {'success': True, 'message': '测试邮件已发送，请检查收件箱'}
    except Exception as e:
        return {'success': False, 'message': f'发送失败：{e}'}


@router.post("/confirm-face-verify", summary="远程确认人脸验证（供邮件链接回调）")
async def confirm_face_verify_remote(token: str = ""):
    """通过邮件中的 token 远程确认人脸验证完成并重试开播"""
    sender = get_email_sender()
    lc = get_live_controller()
    if not sender or not lc:
        return {'success': False, 'message': '服务未初始化'}

    import time
    if token and token in sender._face_verify_tokens:
        expiry = sender._face_verify_tokens[token]
        if time.time() < expiry:
            del sender._face_verify_tokens[token]
            lc.confirm_face_verify()
            # 重试开播
            import threading
            threading.Thread(target=sender._retry_after_face_verify, daemon=True).start()
            return {'success': True, 'message': '人脸验证已确认，正在重试开播'}
        else:
            return {'success': False, 'message': '链接已过期（30分钟有效）'}
    return {'success': False, 'message': '无效的确认链接'}
