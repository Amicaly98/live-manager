"""live.py - 直播控制相关 API（阶段 4B：只解释协议，不推进业务）

设计边界（见 ``docs/plans/live-session-core-reset-20260930/PHASE4B-DESIGN.md``）：

- 每个控制入口只做三件事：**校验冻结字段 → 交给唯一 ControlService → 映射回执**；
- HTTP 成功 ≠ 平台开播成功：``accepted`` 只表示意图已持久保存；
- 路由**不**维护自己的运行授权/长锁/票据缓存（4A 的领取互斥是唯一权威）；
- 旧的状态编辑/清空/重载入口**明确退役**（410），保留恢复/放弃/放弃并新开。
"""

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.core import diagnostics as diagnostics_module
from app.core.session_runtime.area_capability import get_area_loader, new_bilibili_api
from app.core.session_runtime.control import ControlServiceError
from app.dependencies import get_application_owner, get_control_service

from app.models.control_dto import (
    AbandonRequest, ControlRequest, ControlResponse, FaceConfirmRequest,
    RestartAfterAbandonRequest, ResumeRequest, RetiredEndpointResponse,
    StopRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: 普通控制入口的有界执行器：同步等待（平台往返）不占事件循环。
_CONTROL_EXECUTOR = ThreadPoolExecutor(max_workers=4,
                                       thread_name_prefix='live-control')
#: 停止/确认是"取消"语义：独立执行器，慢控制把它们排不到后面去。
_PRIORITY_EXECUTOR = ThreadPoolExecutor(max_workers=2,
                                        thread_name_prefix='live-priority')
_DIAG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='live-diag')


# ==================== 协议 → 服务 ====================

def _require_control():
    control = get_control_service()
    if control is None:
        raise HTTPException(status_code=503,
                            detail='控制服务尚未就绪（服务正在启动或已关闭）')
    return control


def _require_available(control):
    """未登录 / 数据不可信时的**统一明确拒绝**（不是伪造一个空账号）。"""
    owner = get_application_owner()
    if owner is not None and not owner.control_available():
        detail = owner.takeover.detail or '状态数据不可用于控制'
        raise HTTPException(status_code=409,
                            detail=f'控制当前不可用：{detail}')
    return control


def _map_receipt(receipt) -> ControlResponse:
    messages = {
        'accepted': '控制意图已保存，后台按该目标处理',
        'pending': '同一操作的另一个请求仍在处理中',
        'ignored': '该输入与当前事实无关，未改变任何状态',
        'rejected': '该控制未被受理',
        'token_conflict': '同一操作令牌已用于不同请求',
    }
    return ControlResponse(
        operation_token=receipt.operation_token,
        status=receipt.status,
        accepted=bool(receipt.accepted),
        reason=receipt.reason or '',
        projection=receipt.projection,
        success=bool(receipt.accepted),
        message=messages.get(receipt.status, receipt.status),
    )


def _http_status_for(receipt) -> int:
    if receipt.status == 'accepted':
        return 200
    if receipt.status == 'pending':
        return 202
    if receipt.status in ('rejected', 'token_conflict'):
        return 409
    return 200


def _receipt_response(receipt) -> JSONResponse:
    payload = _map_receipt(receipt)
    return JSONResponse(status_code=_http_status_for(receipt),
                        content=payload.model_dump())


async def _offload(func, *args, executor=None, **kwargs):
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(executor or _CONTROL_EXECUTOR, call)


def _run_control(operation, *args, executor=None, **kwargs):
    """统一把服务异常翻译成稳定 HTTP 错误；回执翻译成稳定响应。"""
    try:
        receipt = operation(*args, **kwargs)
    except ControlServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:                     # noqa: BLE001
        logger.error('控制入口异常：%s', exc, exc_info=True)
        raise HTTPException(status_code=500,
                            detail=f'控制处理失败：{type(exc).__name__}')
    return _receipt_response(receipt)


# ==================== 控制端点 ====================

@router.post("/start", summary="开始直播（任务/单次/指定任务）",
             response_model=ControlResponse)
async def start_live(request: ControlRequest = ControlRequest(),
                     x_operation_token: str = Header(default=None,
                                                     alias='X-Operation-Token')):
    """新场：一律先关后开；``mode`` 决定任务/单次；指定任务须带原票据三件套。"""
    control = _require_available(_require_control())
    frozen = request.to_service_request()
    account = request.account_context.model_dump() if request.account_context else None
    return await _offload(_run_control, control.start, frozen,
                          x_operation_token or '', account_context=account)


@router.post("/run-next", summary="执行下一个待执行任务",
             response_model=ControlResponse)
async def run_next_task(request: ControlRequest = ControlRequest(),
                        x_operation_token: str = Header(
                            default=None, alias='X-Operation-Token')):
    """**一次**自动新意图（不是 clear→start 两项操作）。"""
    control = _require_available(_require_control())
    frozen = request.to_service_request()
    frozen['mode'] = 'task'
    frozen.pop('selection', None)
    frozen.pop('task_id', None)
    frozen.pop('expected_revision', None)
    frozen.pop('business_date', None)
    account = request.account_context.model_dump() if request.account_context else None
    return await _offload(_run_control, control.start, frozen,
                          x_operation_token or '', account_context=account)


@router.post("/resume", summary="恢复进行中的直播",
             response_model=ControlResponse)
async def resume_live(request: ResumeRequest,
                      x_operation_token: str = Header(
                          default=None, alias='X-Operation-Token')):
    """原场续播：请求必须携带**用户看到的**原 run_id 与 goal_revision。"""
    control = _require_available(_require_control())
    account = request.account_context.model_dump() if request.account_context else None
    return await _offload(_run_control, control.resume, x_operation_token or '',
                          run_id=request.run_id,
                          goal_revision=request.goal_revision,
                          account_context=account)


@router.post("/stop", summary="停止直播", response_model=ControlResponse)
async def stop_live(request: Optional[StopRequest] = None,
                    x_operation_token: str = Header(default=None,
                                                    alias='X-Operation-Token')):
    """停止：**随时可达**，走独立优先执行器；受理只表示 stopping 意图已落盘。"""
    control = _require_control()
    body = request or StopRequest()
    account = body.account_context.model_dump() if body.account_context else None
    return await _offload(_run_control, control.stop, x_operation_token or '',
                          account_context=account, executor=_PRIORITY_EXECUTOR)


@router.post("/abandon", summary="单独放弃原场", response_model=ControlResponse)
async def abandon(request: AbandonRequest,
                  x_operation_token: str = Header(
                      default=None, alias='X-Operation-Token')):
    """只放弃同日已停止并保留的原场；不直接 unlink/clear 状态文件。"""
    control = _require_available(_require_control())
    return await _offload(_run_control, control.abandon, x_operation_token or '',
                          run_id=request.run_id,
                          goal_revision=request.goal_revision,
                          selection=request.selection)


@router.post("/restart-after-abandon", summary="放弃旧场并一步开新场",
             response_model=ControlResponse)
async def restart_after_abandon(request: RestartAfterAbandonRequest,
                                x_operation_token: str = Header(
                                    default=None, alias='X-Operation-Token')):
    """**一项**请求同时携带原场冻结上下文与新选择：没有 clear→start 的间隙。"""
    control = _require_available(_require_control())
    return await _offload(_run_control, control.restart_after_abandon,
                          request.to_service_request(), x_operation_token or '')


@router.post("/confirm-face-verify", summary="面板确认人脸验证完成",
             response_model=ControlResponse)
async def confirm_face_verify(request: FaceConfirmRequest,
                              x_operation_token: str = Header(
                                  default=None, alias='X-Operation-Token')):
    """面板与邮件共用**同一个**确认入口：都只调 ``ControlService.confirm``。"""
    control = _require_available(_require_control())
    return await _offload(_run_control, control.confirm, x_operation_token or '',
                          goal_revision=request.goal_revision,
                          uid=request.uid, room_id=request.room_id,
                          executor=_PRIORITY_EXECUTOR)


# ==================== 只读投影 ====================

def read_own_projection(wait_reason: str = '') -> Dict[str, Any]:
    """本实例的**一次读取**只读投影（M2 窄提取：原 `_read_projection` 公开名）。

    实例状态入口与这里共用同一份读取：账号必须与投影来自同一次 `control.read()`，
    不用多份回包拼"原子快照"。函数体与原实现逐字节同源，行为不变。
    """
    owner = get_application_owner()
    control = get_control_service()
    if control is None:
        raise HTTPException(status_code=503, detail='控制服务尚未就绪')
    from app.core.session_runtime.projection import build_projection
    instance = control.instance
    return build_projection(
        control.read(),
        clock=(owner.clock if owner is not None else None),
        account=(instance.account if instance is not None else None),
        wait_reason=wait_reason,
        # 只读展示出口：最近**已确认**的后台事实（有界内存缓冲，读不发通知）。
        notifications=(owner.notifications if owner is not None else None))


#: 既有内部调用保持原名（行为不变）。
_read_projection = read_own_projection


@router.get("/status", summary="获取直播状态")
async def get_live_status():
    return _read_projection()


@router.get("/state", summary="检查进行中的任务状态")
async def get_live_state():
    """只读投影；GET **不**修改任何 JSON。"""
    return _read_projection()


@router.get("/state/full", summary="获取完整状态（含时长信息）")
async def get_full_live_state():
    return _read_projection()


# ==================== 已退役入口（稳定拒绝，零写入） ====================

def _retired(reason: str, replacement) -> JSONResponse:
    payload = RetiredEndpointResponse(reason=reason, replacement=list(replacement))
    return JSONResponse(status_code=410, content=payload.model_dump())


@router.post("/state/update", summary="（已退役）更新进行中任务状态")
async def update_live_state(data: dict = None, force: bool = Query(False)):
    """后端不再接受运行参数编辑：进度只由唯一 Core 的确认事实推进。"""
    return _retired('后端恢复参数编辑能力已退役，未执行任何写入',
                    ['/api/live/abandon', '/api/live/restart-after-abandon'])


@router.post("/state/reload", summary="（已退役）重新加载 live_state.json")
async def reload_live_state(force: bool = Query(False)):
    """磁盘旧档不得替换 actor 的当前快照。"""
    return _retired('重载已退役：运行快照由唯一执行器持有，不接受磁盘旧档替换',
                    ['/api/live/state/full'])


@router.post("/clear-state", summary="（已退役）清空进行中的任务状态")
async def clear_live_state(force: bool = Query(False)):
    """清空等价于绕过 Core 直接改业务 JSON；改用明确的放弃/重新开播。"""
    return _retired('直接清空状态已退役：请使用放弃原场或放弃并新开',
                    ['/api/live/abandon', '/api/live/restart-after-abandon'])


@router.post("/switch-area", summary="（已退役）直播中切换分区")
async def switch_area(zone_name: str = ''):
    """新控制核心没有"运行中换区"这一步：目标分区属于本场固定目标。"""
    return _retired('运行中切换分区已退役：分区是本场固定目标的一部分',
                    ['/api/live/restart-after-abandon'])


@router.get("/operation-ticket", summary="（已退役）签发控制操作票据")
async def issue_operation_ticket():
    """票据由调用方在冻结请求时生成并随请求携带（``X-Operation-Token``）。"""
    return _retired('服务端票据签发已退役：操作令牌由客户端生成并随请求携带',
                    ['/api/live/start', '/api/live/stop'])


# ==================== 诊断（旁路，只读） ====================

@router.get("/diagnostics", summary="旁路诊断状态（默认关闭，只读）")
async def get_diagnostics_status():
    """只读内存计数：不获取预算锁、不做磁盘扫描、不参与控制决策。"""
    return diagnostics_module.get_diagnostics().stats_fast()


@router.post("/diagnostics/export", summary="导出脱敏诊断摘要")
async def export_diagnostics(limit: int = 200):
    if limit < 1 or limit > 2000:
        raise HTTPException(status_code=400, detail="limit 需在 1..2000 之间")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DIAG_EXECUTOR,
        lambda: diagnostics_module.get_diagnostics().export_bundle(limit=limit))


# ==================== 分区（纯能力） ====================

@router.get("/areas/search", summary="模糊搜索分区")
async def search_areas(keyword: str = ""):
    loader = get_area_loader()
    return {'results': loader.fuzzy_search(keyword),
            'available': bool(loader.areas)}


@router.post("/areas/refresh", summary="刷新分区列表")
async def refresh_areas():
    """联网刷新分区缓存；**不**启动任何旧控制器线程。"""
    loader = get_area_loader()
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        _DIAG_EXECUTOR,
        functools.partial(loader.fetch_and_save_areas, new_bilibili_api()))
    if success:
        return {'success': True, 'message': f'已更新 {len(loader.areas)} 个分区'}
    raise HTTPException(status_code=502, detail=f'获取分区列表失败（{loader.status}）')
