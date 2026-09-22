"""
auth.py - 认证相关 API（登录、登出、状态查询）
"""

import logging
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse


from app.dependencies import get_task_manager, get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/qrcode", summary="获取登录二维码")
def get_qrcode():
    """获取扫码登录二维码"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    result = controller.get_qrcode_for_login()
    if result.get('success'):
        return result['data']
    raise HTTPException(status_code=500, detail=result.get('message', '获取二维码失败'))


@router.get("/status", summary="查询登录状态")
def get_login_status():
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
def poll_qrcode(qrcode_key: str):
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
def logout(x_operation_token: str = Header(default=None,
                                            alias='X-Operation-Token')):
    """清除登录状态"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    logout_fn = getattr(controller, 'logout', None)
    if callable(logout_fn):
        result = logout_fn(x_operation_token or '')
        if not result.get('success'):
            # ``detail`` 保持字符串，兼容现有前端错误提示；稳定 code 供
            # 客户端区分“直播占用账号”与普通网络/落盘失败。
            return JSONResponse(
                status_code=int(result.get('status_code', 503)),
                content={'detail': result.get('message', '登出失败'),
                         'code': result.get('code', 'auth_logout_failed')},
            )
        return result
    # 兼容最小测试替身；生产 LiveController 始终走上面的认证协议。
    if controller.api.clear_cookies() is False:
        return JSONResponse(
            status_code=503,
            content={'detail': '登出凭据撤销未落盘，未确认登出成功',
                     'code': 'auth_logout_persistence_failed'},
        )
    return {'success': True, 'message': '已登出'}
