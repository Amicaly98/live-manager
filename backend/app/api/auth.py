"""
auth.py - 认证相关 API（登录、登出、状态查询）
"""

import logging
from fastapi import APIRouter, HTTPException


from app.dependencies import get_task_manager, get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/qrcode", summary="获取登录二维码")
async def get_qrcode():
    """获取扫码登录二维码"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    result = controller.get_qrcode_for_login()
    if result.get('success'):
        return result['data']
    raise HTTPException(status_code=500, detail=result.get('message', '获取二维码失败'))


@router.get("/status", summary="查询登录状态")
async def get_login_status():
    """查询当前登录状态"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    logged_in = controller.api.is_logged_in()
    user_info = None
    if logged_in:
        success, resp = controller.api.get_user_info()
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            user_info = {
                'uid': data.get('mid'),
                'uname': data.get('name', ''),
                'face': data.get('face', ''),
                'level': data.get('level', 0)
            }
    return {
        'logged_in': logged_in,
        'user_info': user_info
    }


@router.post("/poll/{qrcode_key}", summary="轮询二维码状态")
async def poll_qrcode(qrcode_key: str):
    """轮询查看二维码是否已被扫码"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    result = controller.poll_login_status(qrcode_key)

    # 如果登录成功，确保更新 room_id
    if result.get('logged_in'):
        logger.info(f"✅ 用户登录成功：{result.get('user_info', {}).get('uname', '')}")
        # 此处 controller 已保存 cookies 和 room_id

    return result


@router.post("/logout", summary="登出")
async def logout():
    """清除登录状态"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    controller.api.clear_cookies()
    return {'success': True, 'message': '已登出'}