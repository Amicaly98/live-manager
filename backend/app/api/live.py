"""
live.py - 直播控制相关 API（开始/停止/状态查询）

A2/A3：控制入口统一接入"票据 + 控制代际"协议：
- 前端每次新控制意图先 GET /api/live/operation-ticket 取票；
- 控制请求带 `X-Operation-Token` 头；
- start/resume/run-next/switch-area/confirm-face-verify 经 begin_control_operation
  校验并快照代际；
- stop 经 claim_stop_operation 登记：旧停止重放只确认既有结果，不停掉新开播。
- 过期/重放票据返回 409（响应体含"响应已丢失"标识，前端据此分类）。

A1：/stop 在**独立的单线程执行器**中同步执行——普通线程池饱和时停止仍可达。
"""

import asyncio
import functools
import logging
import concurrent.futures
from typing import Optional, Tuple
from fastapi import APIRouter, Header, HTTPException

from app.dependencies import get_live_controller, get_task_manager
from app.models.schemas import StartLiveRequest, StartLiveResponse, LiveStatusResponse, LiveInstruction
from app.core.live_controller import SOURCE_RESUME

logger = logging.getLogger(__name__)
router = APIRouter()

# 停止专用执行通道：单线程、独立于默认池（A1）。
# 注意：max_workers=1 只保证串行执行，不等于提交队列有界——队列语义由
# 目标意图绑定保证（见 _stop_live_sync：执行时核对登记时刻的目标意图，
# 期间新接受的开播意图不会被旧队列任务停掉）。
_STOP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="stop-executor")

# 慢控制操作（同步网络往返）统一离开事件循环执行的线程池（D1）
_BLOCKING_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="blocking-control")


# ==================== 票据与代际协议 ====================

def _begin_operation(controller, token: str = '') -> int:
    """校验票据并快照控制代际；无效票据抛 409。"""
    begin = getattr(controller, 'begin_control_operation', None)
    if begin is None:  # 测试替身兼容
        return 0
    epoch = begin(token)
    if epoch is None:
        raise HTTPException(
            status_code=409,
            detail='操作已失效（响应已丢失，旧操作在停止后到达被拒绝）')
    return epoch


def _claim_stop_operation(controller, token: str = '') -> Optional[int]:
    """停止入口专用：登记票据并返回目标意图快照（None=重放，不再执行）。"""
    claim = getattr(controller, 'claim_stop_operation', None)
    if claim is None:
        return 0
    return claim(token)


def _epoch_current(controller, epoch: int) -> bool:
    """代际复核（测试替身兼容）。"""
    check = getattr(controller, '_is_epoch_current', None)
    if check is None:
        return True
    return check(epoch)


async def _run_blocking(func, *args, **kwargs):
    """Run a synchronous control boundary on the bounded blocking pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _BLOCKING_POOL, functools.partial(func, *args, **kwargs))


# ==================== 统一响应构建 ====================

def _build_stream_response(controller, success: bool, success_msg: str = "直播已开始") -> StartLiveResponse:
    """统一构建 start_streaming / run_next_task 后的响应（人脸验证、失败等）"""
    if success:
        return StartLiveResponse(
            success=True,
            room_id=controller.current_room_id,
            message=success_msg
        )
    if controller._pending_face_verify:
        return StartLiveResponse(
            success=False,
            need_face_verification=True,
            qr_data=controller._face_verify_url,
            message="需要人脸验证，请扫描二维码完成验证后重试"
        )
    return StartLiveResponse(success=False, message="开播失败，请查看日志")


def _make_instruction(zone_name: str, duration_seconds: int = 7200) -> LiveInstruction:
    """生成直播指令（时长上限 24h，0=不限时）"""
    if duration_seconds > 86400:
        duration_seconds = 86400
    return LiveInstruction(zone_name=zone_name, duration_seconds=duration_seconds)


def _get_next_task_instruction() -> Tuple[Optional[LiveInstruction], Optional[str], Optional[str]]:
    """获取下一个待执行任务的指令，返回 (instruction, video_path, error_msg)

    D3：OBS（非 ffmpeg 推流设置）模式下无本地视频不阻断任务入口；
    仅 FFmpeg 推流必须有视频文件。
    """
    tm = get_task_manager()
    if not tm:
        return None, None, "任务管理器未初始化"
    task_ins = tm.select_next_instruction()
    if not task_ins:
        return None, None, "没有待执行的任务"
    controller = get_live_controller()
    video_path = controller.video_finder.find_video(task_ins.zone_name) if controller else None
    if not video_path:
        stream_mode, _ = controller._get_stream_settings() if controller else ('ffmpeg', False)
        if stream_mode == 'ffmpeg':
            return None, None, f"未找到分区 {task_ins.zone_name} 的视频文件，FFmpeg 推流不可用"
        return task_ins, '', None  # OBS 外部推流：允许无本地视频
    return task_ins, video_path, None


# ==================== API 端点 ====================

@router.get("/operation-ticket", summary="签发控制操作票据")
async def issue_operation_ticket():
    """前端每次新的控制意图取一张新票据（无跨意图缓存，A3/U1 语义）。"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    issue = getattr(controller, 'issue_operation', None)
    if issue is None:
        raise HTTPException(status_code=404, detail="后端不支持签发票据")
    ticket = issue()
    return {'ticket': ticket, 'epoch': int(ticket.split(':')[1])}


@router.get("/status", summary="获取直播状态")
async def get_live_status():
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    return controller.get_live_status_api()


@router.post("/start", summary="开始直播")
async def start_live(request: StartLiveRequest = None,
                     x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """手动模式(指定分区)或任务模式(自动取下一任务)"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    epoch = _begin_operation(controller, x_operation_token)

    if request and request.zone_name:
        requested_duration = getattr(request, 'duration_seconds', None)
        dur = 7200 if requested_duration is None else requested_duration
        if dur < 0:
            return StartLiveResponse(
                success=False,
                message='duration_seconds 不能为负数')
        instruction = _make_instruction(request.zone_name, dur)
        video_path = controller.video_finder.find_video(request.zone_name)
        # OBS 模式无视频不阻断开播，FFmpeg 模式必须有视频
        if not video_path:
            stream_mode, _ = controller._get_stream_settings()
            if stream_mode == 'ffmpeg':
                return StartLiveResponse(success=False, message=f"未找到分区 {request.zone_name} 的视频文件，FFmpeg 推流不可用")
            # OBS 模式：允许无视频开播
            video_path = ''
        ok = await _run_blocking(
            controller.start_streaming, instruction, video_path,
            is_task_mode=False, epoch=epoch)
    else:
        instruction, video_path, err = await _run_blocking(
            _get_next_task_instruction)
        if err:
            return StartLiveResponse(success=False, message=err)
        ok = await _run_blocking(
            controller.start_streaming, instruction, video_path,
            is_task_mode=True, epoch=epoch)

    return _build_stream_response(controller, ok)


@router.post("/resume", summary="恢复进行中的直播")
async def resume_live(x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """从 live_state.json 恢复直播"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    epoch = _begin_operation(controller, x_operation_token)

    resolver = getattr(controller, 'resolve_resume_target', None)
    if callable(resolver):
        instruction, reason = await _run_blocking(resolver)
        if instruction is None:
            return StartLiveResponse(success=False, message=reason or '没有可恢复的直播任务')
    else:
        # Compatibility for a minimal controller double; production desktop
        # controllers always expose the identity checked resolver above.
        state = controller.state
        if not state.current_zone:
            return StartLiveResponse(success=False, message="没有可恢复的直播任务")
        instruction = _make_instruction(
            state.current_zone, getattr(state, 'duration_seconds', 7200))

    if controller.area_loader.get_area_id(instruction.zone_name, auto_update=False) is None:
        return StartLiveResponse(success=False, message=f"分区 '{instruction.zone_name}' 不存在于分区列表中")

    video_path = controller.video_finder.find_video(instruction.zone_name)
    if not video_path:
        # D3：OBS 外部推流模式无本地视频不阻断恢复
        stream_mode, _ = controller._get_stream_settings()
        if stream_mode == 'ffmpeg':
            return StartLiveResponse(success=False, message=f"未找到分区 {instruction.zone_name} 的视频文件，FFmpeg 推流不可用")
        video_path = ''

    state = controller.state
    inherit_elapsed = int(getattr(state, 'effective_seconds', 0) or
                           getattr(state, 'elapsed_seconds', 0) or 0)
    ok = await _run_blocking(
        controller.start_streaming,
        instruction, video_path, is_task_mode=True, epoch=epoch,
        source=SOURCE_RESUME, inherit_elapsed=inherit_elapsed)
    return _build_stream_response(controller, ok, success_msg="直播已恢复")


@router.post("/confirm-face-verify", summary="确认人脸验证完成")
async def confirm_face_verify(x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """前端用户完成人脸验证后调用，清除待验证状态"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    _begin_operation(controller, x_operation_token)
    run_id = (getattr(controller, '_current_run_id', '')
              or getattr(controller, '_pending_run_id', '')
              or getattr(getattr(controller, 'state', None), 'run_id', '') or '')
    confirmed = controller.confirm_face_verify(
        run_id=run_id,
        room_id=getattr(controller, 'current_room_id', None),
        epoch=getattr(controller, '_control_epoch', None))
    return {
        'success': bool(confirmed),
        'message': '验证状态已确认' if confirmed
        else '确认链接已过期：该会话已结束或被新的会话取代'}


def _stop_live_sync(x_operation_token: str = None, target_intent: Optional[int] = None):
    """在独立执行器线程里执行的同步停止（A1：不受默认池饱和影响）。

    D1：target_intent 是登记时刻的目标开播意图快照。执行时核对当前意图：
    队列等待期间新接受的开播意图（id 已变）不会被旧队列任务停掉；
    target_intent=None 表示无绑定（兼容直调），始终执行。
    """
    controller = get_live_controller()
    if not controller:
        return {'success': False, 'message': '直播控制器未初始化'}
    if target_intent is not None:
        current = getattr(controller, '_start_intent_id', 0)
        if current != target_intent:
            logger.info(
                " 停止执行时目标意图已变更（登记=%s 当前=%s）：不执行下播",
                target_intent, current)
            return {'success': True,
                    'message': '该停止意图的目标已被新开播取代或已结束（未重复执行）'}
    try:
        success = controller.stop_streaming()
        return {
            'success': success,
            'message': '直播已停止' if success else '停止失败'
        }
    except Exception as e:
        logger.error(f" 停止直播异常：{e}", exc_info=True)
        return {'success': False, 'message': f'停止异常：{e}'}


@router.post("/stop", summary="停止直播")
async def stop_live(x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """停止当前直播。

    旧停止重放（票据属于已过去的代际）只确认既有结果——返回成功但不再执行
    下播，绝不停掉后来明确开启的新直播（A2/A7）。
    D1：登记与执行分离——登记时刻快照目标意图，执行器队列中迟到执行时
    仍关联原意图，不停掉期间新接受的开播。
    """
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    target = _claim_stop_operation(controller, x_operation_token)
    if target is None:
        return {'success': True, 'message': '该停止意图已在先前处理完成（重放确认，未重复执行）'}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STOP_EXECUTOR,
        functools.partial(_stop_live_sync, x_operation_token, target))


@router.get("/state", summary="检查进行中的任务状态")
async def get_live_state():
    """检查 live_state.json 是否有进行中的任务（分区名不为空即视为有未完成任务）"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    return {
        'has_active': bool(controller.state.current_zone),
        'current_zone': controller.state.current_zone or '',
        'elapsed_seconds': controller.state.elapsed_seconds,
        'room_id': controller.state.room_id,
    }


@router.post("/clear-state", summary="清空进行中的任务状态")
async def clear_live_state():
    """清空 live_state.json 中的进行中任务"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    def clear():
        lock = getattr(controller, '_start_lock', None)
        if lock is None:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return False
            controller.state.stop_streaming(preserve=False)
            return True
        with lock:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return False
            controller.state.stop_streaming(preserve=False)
            return True
    if not await _run_blocking(clear):
        return {'success': False, 'message': '直播正在运行或启动中，不能清空状态'}
    return {'success': True, 'message': '任务状态已清空'}


@router.get("/state/full", summary="获取完整状态（含时长信息）")
async def get_full_live_state():
    """获取完整的 live_state.json 内容，含 duration_seconds。"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    s = controller.state
    return {
        'has_active': bool(s.current_zone),  # 只要分区名不为空就认为有未完成任务
        'current_zone': s.current_zone or '',
        'elapsed_seconds': s.elapsed_seconds,
        'room_id': s.room_id,
        'duration_seconds': getattr(s, 'duration_seconds', 0),
        'start_time': getattr(s, 'start_time', None),
    }


@router.post("/state/reload", summary="重新加载 live_state.json")
async def reload_live_state():
    """从 live_state.json 重新读取状态（方便手动编辑 JSON 后刷新）"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    def reload_state():
        lock = getattr(controller, '_start_lock', None)
        if lock is None:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return False
            controller.state.reload()
            return True
        with lock:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return False
            controller.state.reload()
            return True
    if not await _run_blocking(reload_state):
        return {'success': False, 'message': '直播正在运行或启动中，不能重新加载状态'}
    s = controller.state
    return {
        'success': True,
        'message': '状态已重新加载',
        'has_active': s.is_streaming,
        'current_zone': s.current_zone or '',
        'elapsed_seconds': s.elapsed_seconds,
        'duration_seconds': getattr(s, 'duration_seconds', 0),
    }


@router.post("/state/update", summary="更新进行中任务状态")
async def update_live_state(data: dict):
    """更新 live_state 中的字段（duration_seconds / current_zone / elapsed_seconds 等）
    可传字段：is_streaming, current_zone, elapsed_seconds, room_id, duration_seconds
    """
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    if data.get('is_streaming') is True:
        return {'success': False, 'message': '不能通过状态编辑伪造在播会话'}

    def update_state():
        lock = getattr(controller, '_start_lock', None)
        if lock is None:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return None
            return controller.state.update_state(**data)
        with lock:
            if controller.is_streaming or getattr(controller, '_is_starting', False):
                return None
            return controller.state.update_state(**data)

    changed = await _run_blocking(update_state)
    if changed is None:
        return {'success': False, 'message': '直播正在运行或启动中，不能编辑恢复状态'}
    s = controller.state
    return {
        'success': True,
        'changed': changed,
        'message': '状态已更新' if changed else '无变更',
        'current_zone': s.current_zone or '',
        'elapsed_seconds': s.elapsed_seconds,
        'duration_seconds': getattr(s, 'duration_seconds', 0),
    }


@router.post("/switch-area", summary="切换直播分区（手动模式）")
async def switch_area(zone_name: str,
                      x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """手动模式下正在直播时切换分区（调用 B 站换区 API，不下播）

    D1：换区是同步网络往返，放到线程池执行——事件循环不被慢换区阻塞
    （停止等控制入口可及时获得执行）；返回结果在提交前复核代际，
    换区期间发生停止时不把结果提交到已停止的会话。
    """
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    epoch = _begin_operation(controller, x_operation_token)
    if controller._stream_mode != 'manual':
        raise HTTPException(status_code=400, detail="仅手动模式支持直播中切换分区")
    if not controller.is_streaming:
        raise HTTPException(status_code=400, detail="当前未在直播")
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        _BLOCKING_POOL, functools.partial(controller.switch_partition, zone_name))
    # D1：换区期间发生停止 → 结果不提交（不修改 current_instruction）
    if not _epoch_current(controller, epoch):
        return {'success': False, 'message': '直播已停止，换区结果未提交'}
    if success:
        # 更新当前指令的分区名
        if controller.current_instruction:
            controller.current_instruction.zone_name = zone_name
        return {'success': True, 'message': f'已切换到分区：{zone_name}'}
    raise HTTPException(status_code=500, detail=f'切换分区失败：{zone_name}')


@router.post("/run-next", summary="执行下一个任务")
async def run_next_task(x_operation_token: str = Header(default=None, alias='X-Operation-Token')):
    """执行下一个待执行直播任务"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    epoch = _begin_operation(controller, x_operation_token)

    if controller.is_streaming or getattr(controller, '_is_starting', False):
        return {
            'success': False,
            'message': '当前已有直播进行中或正在启动'
        }

    success = controller.run_next_task(epoch=epoch)
    if success:
        return {
            'success': True,
            'message': '任务已开始执行',
            'task': {
                'zone_name': controller.current_instruction.zone_name if controller.current_instruction else '',
                'duration': controller.current_instruction.duration_seconds if controller.current_instruction else 0
            }
        }
    elif controller._pending_face_verify:
        return {
            'success': False,
            'need_face_verification': True,
            'qr_data': controller._face_verify_url,
            'message': '需要人脸验证，请扫描二维码完成验证后重试'
        }
    else:
        return {
            'success': False,
            'message': '执行任务失败（可能无可用任务或找不到视频）'
        }


@router.get("/areas/search", summary="模糊搜索分区")
async def search_areas(keyword: str = ""):
    """模糊搜索直播分区"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    results = controller.area_loader.fuzzy_search(keyword)
    return {'results': results}


@router.post("/areas/refresh", summary="刷新分区列表")
async def refresh_areas():
    """从 B 站 API 获取最新分区列表

    D1：网络往返同步调用离开事件循环执行，不阻塞停止等控制入口。
    """
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        _BLOCKING_POOL,
        functools.partial(controller.area_loader.fetch_and_save_areas, controller.api))
    if success:
        return {'success': True, 'message': f'已更新 {len(controller.area_loader.areas)} 个分区'}
    raise HTTPException(status_code=500, detail='获取分区列表失败')
