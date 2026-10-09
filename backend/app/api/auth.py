"""auth.py - 认证相关 API（阶段 4B：纯能力 QR ＋ 唯一控制面的安装/移交）

两条独立职责保持分离：

1. **面板访问鉴权**（``panel_auth_middleware``）在 ``main.py``，与本文件无关；
2. **B 站账号登录**在这里：只复用 ``BilibiliApi`` 的纯能力（QR/cookie/user/room），
   **不构造旧 ``LiveController``**。

扫码有三条硬约束（见 ``PHASE4B-DESIGN.md`` §4）：

- 每个 ``qrcode_key`` 绑定一个**扫码代次**；代次被后来的扫码取代后，旧回包一律
  丢弃（零 cookies 写入、零实例安装）；
- 候选凭据只在**内存**里核验 uid/room，登录安装成功之后才持久化；
- 安装/移交只走 ``ControlService.login`` / ``logout``，路由不自己写 cookies、
  不自己关房。
"""

import asyncio
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.core.session_runtime.area_capability import new_bilibili_api
from app.core.session_runtime.bounded_transport import BoundedHttpSender
from app.core.session_runtime.control import ControlServiceError
from app.core.session_runtime.platform import RemainingBudget
from app.dependencies import get_application_owner, get_control_service

from app.models.control_dto import LoginRequest, LogoutRequest

logger = logging.getLogger(__name__)
router = APIRouter()

_AUTH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='auth')

#: 一次认证往返的预算（秒）：与其它平台调用共用同一份"有界单轮"接缝。
AUTH_BUDGET_SECONDS = 10.0


# ==================== 扫码代次 ====================

class QrSessions:
    """进程内的扫码代次表：只保证"旧回包不生效"，不是业务状态、不落盘。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._current_key = ''
        self._current_generation = 0

    def register(self, qrcode_key: str) -> int:
        with self._lock:
            self._seq += 1
            self._current_key = str(qrcode_key)
            self._current_generation = self._seq
            return self._current_generation

    def current(self) -> Tuple[str, int]:
        with self._lock:
            return self._current_key, self._current_generation

    def is_current(self, qrcode_key: str) -> bool:
        with self._lock:
            return bool(self._current_key) and str(qrcode_key) == self._current_key


_qr_sessions = QrSessions()


def reset_qr_sessions() -> None:
    """仅供测试夹具在同一进程内复跑。"""
    global _qr_sessions
    _qr_sessions = QrSessions()


# ==================== 有界平台调用 ====================

def _boundaries_and_clock():
    owner = get_application_owner()
    if owner is not None:
        return owner.boundaries, owner.clock
    from app.core.session_runtime.production import current_boundaries
    from app.core.session.clock import SystemClock
    return current_boundaries(), SystemClock()


@contextmanager
def _bounded_platform(api, seconds: float = AUTH_BUDGET_SECONDS):
    """把一次认证往返放进**同一份**有界单轮接缝（测试端点由边界注入决定）。"""
    boundaries, clock = _boundaries_and_clock()
    budget = RemainingBudget(float(clock.mono()) + float(seconds), clock)
    with api.bounded_attempt(budget, boundaries.endpoint_base, BoundedHttpSender()):
        yield api


def _new_api():
    return new_bilibili_api()


# ==================== 端点 ====================

@router.get("/qrcode", summary="获取登录二维码")
async def get_qrcode():
    """签发一张新二维码并**替换**当前扫码代次（旧回包从此无效）。

    取得二维码是一次**同步有界**的平台往返：它必须离开 ASGI 事件循环，否则闸门
    保持时连 Stop/health 都排不到（复核 R4）。
    """
    loop = asyncio.get_running_loop()
    try:
        ok, payload = await loop.run_in_executor(_AUTH_EXECUTOR, _fetch_qrcode)
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'获取二维码失败：{type(exc).__name__}')
    if not ok or not isinstance(payload, dict) or payload.get('code') != 0:
        raise HTTPException(status_code=502, detail='获取二维码失败')
    data = payload.get('data') or {}
    key = str(data.get('qrcode_key') or '')
    if not key:
        raise HTTPException(status_code=502, detail='二维码回包缺少 qrcode_key')
    generation = _qr_sessions.register(key)
    return {'qrcode_key': key, 'qr_url': str(data.get('url') or ''),
            'generation': generation}


def _fetch_qrcode():
    api = _new_api()
    with _bounded_platform(api):
        return api.get_qrcode()


@router.get("/status", summary="查询登录状态")
async def get_login_status():
    """**只读**：只报告内存里已核验的账号事实，不 Query / Stop 任何直播间。"""
    owner = get_application_owner()
    control = get_control_service()
    instance = control.instance if control is not None else None
    if instance is None:
        return {'logged_in': False, 'user_info': None, 'source': 'memory'}
    account = instance.account
    return {
        'logged_in': True,
        'source': 'cached',
        'user_info': {
            'uid': int(getattr(account, 'uid', 0)),
            'uname': (owner.account_display_name if owner is not None else '')
                     or str(getattr(account, 'display_name', '') or ''),
            'room_id': int(getattr(account, 'room_id', 0)),
            # 头像只在已核验账号下回报；未取得（旧凭据且尚未补齐）时为空串，
            # 前端按无头像显示，不伪造。
            'face': (str(getattr(owner, 'account_face', '') or '')
                     if owner is not None else ''),
        },
    }


@router.post("/poll/{qrcode_key}", summary="轮询二维码状态")
async def poll_qrcode(qrcode_key: str,
                      x_operation_token: str = Header(
                          default=None, alias='X-Operation-Token')):
    """轮询扫码状态；只有**当前代次**的成功回包才会去核验并安装账号。

    代次判定在**发出任何平台请求之前**：已经被更新的二维码取代的 key 不再去
    平台轮询，也就没有任何"旧回包"可以写凭据或安装实例。
    """
    if not _qr_sessions.is_current(qrcode_key):
        return {'logged_in': False, 'status': 'stale_generation',
                'message': '这次扫码已被更新的二维码取代，未询问平台、未安装账号'}
    loop = asyncio.get_running_loop()
    api = _new_api()
    try:
        ok, payload = await loop.run_in_executor(
            _AUTH_EXECUTOR, functools.partial(_poll_platform, api, qrcode_key))
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'轮询二维码失败：{type(exc).__name__}')
    if not ok or not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail='轮询二维码失败')
    data = payload.get('data') or {}
    code = payload.get('code')
    status = data.get('code')
    if code != 0:
        return {'logged_in': False, 'status': 'poll_failed',
                'message': str(payload.get('message') or '轮询失败')}
    if status != 0:
        # 86101 未扫码 / 86090 已扫码未确认 / 86038 已过期
        return {'logged_in': False, 'status': str(status),
                'message': str(data.get('message') or '')}

    cookies = _extract_cookies(payload, api)
    if not cookies.get('SESSDATA'):
        return {'logged_in': False, 'status': 'no_session',
                'message': '扫码结果没有携带会话凭据，未安装账号'}

    return await loop.run_in_executor(
        _AUTH_EXECUTOR,
        functools.partial(_install_candidate, cookies, x_operation_token or '',
                          qrcode_key))


def _poll_platform(api, qrcode_key: str):
    with _bounded_platform(api):
        return api.poll_qrcode(qrcode_key)


def _extract_cookies(payload: Dict[str, Any], api) -> Dict[str, str]:
    """从回包里取候选凭据：优先 URL 查询串里的 cookie，再退回 api 的候选快照。"""
    candidates: Dict[str, str] = {}
    url = str((payload.get('data') or {}).get('url') or '')
    if url:
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(url).query)
        for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
            values = query.get(name) or []
            if values:
                candidates[name] = values[0]
    for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
        if name not in candidates:
            value = (getattr(api, 'cookies', {}) or {}).get(name)
            if value:
                candidates[name] = str(value)
    return candidates


def _install_candidate(cookies: Dict[str, str], operation_token: str,
                       qrcode_key: Optional[str] = None):
    """在**内存候选**里核验 uid/room，再经唯一控制面安装；随后才持久化凭据。

    ``qrcode_key`` 给出时，本次安装属于**那一次**扫码代次：同一份代次判定会被交给
    控制面，在**实际安装提交点**（实例已建好、即将占用账号插槽）再问一次。用户在
    候选人脸/房间核验在途、乃至实例构造与激活在途时换了二维码，旧回包都不会安装、
    不会写凭据，已建好的实例按既有 close 门释放（复核 R3/R1）。
    """
    from app.core.session.core import AccountContext

    control = get_control_service()
    if control is None:
        raise HTTPException(status_code=503, detail='控制服务尚未就绪')

    api = _new_api()
    try:
        with _bounded_platform(api), api.cookie_context(dict(cookies)):
            ok_user, user_payload = api.get_user_info()
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(status_code=502,
                            detail=f'候选凭据核验失败：{type(exc).__name__}')
    uid = _payload_uid(ok_user, user_payload)
    if uid is None:
        return {'logged_in': False, 'status': 'candidate_rejected',
                'message': '候选凭据无法核验账号身份，未安装账号'}

    display_name = ''
    face = ''
    if ok_user and isinstance(user_payload, dict):
        data = user_payload.get('data')
        if isinstance(data, dict):
            display_name = str(data.get('name') or '')
            face = str(data.get('face') or '')

    api2 = _new_api()
    room_id = None
    try:
        with _bounded_platform(api2), api2.cookie_context(dict(cookies)):
            ok_room, room_payload = api2.get_room_id_by_uid(uid)
        room_id = _payload_room(ok_room, room_payload)
    except Exception:                                 # noqa: BLE001
        room_id = None
    if room_id is None:
        return {'logged_in': False, 'status': 'candidate_rejected',
                'message': '候选凭据无法解析直播间，未安装账号'}

    # 最终安装边界：身份与房间都已核验，此刻再判一次扫码代次。它必须覆盖"poll
    # 在途 / 用户核验在途 / 房间核验在途"三种迟到——判定点越靠后越安全。
    if qrcode_key is not None and not _qr_sessions.is_current(qrcode_key):
        return {'logged_in': False, 'status': 'stale_generation',
                'message': '这次扫码已被更新的二维码取代，未安装账号、未写入凭据'}

    account = AccountContext(uid=int(uid), room_id=int(room_id),
                             display_name=display_name)
    token = operation_token or f'login-{uid}'
    # 同一份代次判定**再次**交给控制面，让它在真正的安装提交点（慢构造之后、
    # 占用账号插槽之前）核对：核验之后到安装之间还有数据接管、组装、actor 启动与
    # Bootstrap，用户完全可能在这段时间里换码（复核 R1）。没有二维码的显式登录
    # 不带这份判定。
    still_valid = (None if qrcode_key is None
                   else functools.partial(_qr_sessions.is_current, qrcode_key))
    try:
        receipt = control.login(token, account, dict(cookies),
                                still_valid=still_valid)
    except ControlServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not receipt.accepted:
        return {'logged_in': False, 'status': receipt.status or 'rejected',
                'message': receipt.reason or '账号安装被拒绝',
                'operation_token': receipt.operation_token}
    # 安装成功之后才持久化凭据：崩溃在中间时留的是"未登录"，不是"半装上的账号"。
    # 已核验的 room_id 一并写入——重启读取的函数依赖它，否则下次启动会退回未登录
    # （复核 R2）。已核验的头像地址同样一并写入：面板左下角读它。
    _persist_cookies(cookies, display_name, int(room_id), face)
    owner = get_application_owner()
    if owner is not None:
        owner.account_display_name = display_name
        owner.account_face = face
    return {'logged_in': True, 'status': 'installed', 'uid': int(uid),
            'room_id': int(room_id), 'uname': display_name, 'face': face,
            'operation_token': receipt.operation_token,
            'projection': receipt.projection}


def _payload_uid(ok: bool, payload: Any) -> Optional[int]:
    data = _payload_data(ok, payload)
    if data is None:
        return None
    return _positive_int(data.get('mid') or data.get('uid'))


def _payload_room(ok: bool, payload: Any) -> Optional[int]:
    data = _payload_data(ok, payload)
    if data is None:
        return None
    return _positive_int(data.get('room_id') or data.get('roomid'))


def _payload_data(ok: bool, payload: Any) -> Optional[dict]:
    """取回包里的 ``data``；缺失 ``code`` 视为 0（``get_room_id_by_uid`` 的
    便捷方法只回 ``{'data': {...}}``，不带业务码）。"""
    if not ok or not isinstance(payload, dict):
        return None
    if 'code' in payload and payload.get('code') != 0:
        return None
    data = payload.get('data')
    return data if isinstance(data, dict) else None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _persist_cookies(cookies: Dict[str, str], display_name: str,
                     room_id: int = 0, face: str = '') -> None:
    """把已安装账号的凭据写入本实例凭据文件（原子）；失败只记日志、不回滚实例。

    ``room_id`` 是**已核验**的上下文，必须一起落盘：生产重启的读取函数按它判断
    "这台机器控制的是哪个房间"，只有三项旧凭据的目录会被判成未登录（复核 R2）。
    ``face`` 是同一份核验回包里的头像地址，随凭据缓存，避免每次启动再取。
    """
    try:
        api = _new_api()
        payload = dict(cookies)
        if display_name:
            payload['uname'] = display_name
        if face:
            payload['face'] = face
        if int(room_id or 0) > 0:
            payload['room_id'] = str(int(room_id))
        if not api.replace_cookies(payload):
            logger.warning('账号已安装，但凭据未能持久化（重启后需重新扫码）')
    except Exception as exc:                          # noqa: BLE001
        logger.warning('账号已安装，但凭据持久化异常：%s', exc)


# ==================== 退出 ====================

@router.post("/logout", summary="登出")
async def logout(request: Optional[LogoutRequest] = None,
                 x_operation_token: str = Header(default=None,
                                                 alias='X-Operation-Token')):
    """退出：唯一移交协议在 ``ControlService.logout``（先收尾旧账号，再换指针）。

    **不**先删 cookies 再关旧房：只有移交被受理之后才撤销本地凭据。
    """
    control = get_control_service()
    if control is None:
        raise HTTPException(status_code=503, detail='控制服务尚未就绪')
    body = request or LogoutRequest()
    next_account = None
    credentials = None
    if body.next_account is not None:
        from app.core.session.core import AccountContext
        next_account = AccountContext(
            uid=int(body.next_account.uid),
            room_id=int(body.next_account.room_id),
            display_name=body.next_account.display_name)
        credentials = body.next_account.credentials()
    account_context = (body.account_context.model_dump()
                       if body.account_context else None)

    loop = asyncio.get_running_loop()
    try:
        receipt = await loop.run_in_executor(
            _AUTH_EXECUTOR,
            functools.partial(control.logout, x_operation_token or '',
                              next_account=next_account, credentials=credentials,
                              timeout=float(body.timeout or 15.0),
                              account_context=account_context))
    except ControlServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not receipt.accepted:
        # 未 clean / 暂拒：保留原 owner 与凭据，如实说明。
        raise HTTPException(status_code=409,
                            detail=receipt.reason or '退出未被受理，凭据保持不变')
    _revoke_local_cookies()
    owner = get_application_owner()
    if owner is not None:
        owner.account_display_name = (body.next_account.display_name
                                      if body.next_account is not None else '')
        # 头像与显示名同一条身份事实：退出/换号后不得留着旧账号的头像。
        owner.account_face = ''
    return {'success': True, 'message': '已退出账号',
            'operation_token': receipt.operation_token,
            'projection': receipt.projection}


def _revoke_local_cookies() -> None:
    """移交**已受理**之后才撤销本地凭据；失败如实记录（不谎称已登出）。"""
    try:
        api = _new_api()
        if not api.replace_cookies({}):
            logger.warning('退出已受理，但本地凭据撤销未确认落盘')
    except Exception as exc:                          # noqa: BLE001
        logger.warning('退出已受理，但本地凭据撤销异常：%s', exc)


# ==================== 显式登录（无扫码的等价入口） ====================

@router.post("/login", summary="以显式凭据安装账号")
async def login(request: LoginRequest,
                x_operation_token: str = Header(default=None,
                                                alias='X-Operation-Token')):
    """凭据仍必须能在**内存候选**里核验成功；安装成功后才持久化。"""
    loop = asyncio.get_running_loop()
    cookies = request.credentials()
    result = await loop.run_in_executor(
        _AUTH_EXECUTOR,
        functools.partial(_install_candidate, cookies, x_operation_token or ''))
    if not result.get('logged_in'):
        return JSONResponse(status_code=409, content=result)
    return result
