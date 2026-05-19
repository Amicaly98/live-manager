"""
live.py - 直播控制相关 API（开始/停止/状态查询）
"""

import logging
from typing import Optional, Tuple
from fastapi import APIRouter, HTTPException

from app.dependencies import get_live_controller, get_task_manager
from app.models.schemas import StartLiveRequest, StartLiveResponse, LiveStatusResponse, LiveInstruction

logger = logging.getLogger(__name__)
router = APIRouter()


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
    """获取下一个待执行任务的指令，返回 (instruction, video_path, error_msg)"""
    tm = get_task_manager()
    if not tm:
        return None, None, "任务管理器未初始化"
    task_ins = tm.select_next_instruction()
    if not task_ins:
        return None, None, "没有待执行的任务"
    controller = get_live_controller()
    video_path = controller.video_finder.find_video(task_ins.zone_name) if controller else None
    if not video_path:
        return None, None, f"未找到分区 {task_ins.zone_name} 的视频文件"
    return task_ins, video_path, None


# ==================== API 端点 ====================

@router.get("/status", summary="获取直播状态")
async def get_live_status():
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    return controller.get_live_status_api()


@router.post("/start", summary="开始直播")
async def start_live(request: StartLiveRequest = None):
    """手动模式(指定分区)或任务模式(自动取下一任务)"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    if controller.is_streaming:
        return StartLiveResponse(success=False, message="当前已有直播进行中，请先停止")

    if request and request.zone_name:
        dur = getattr(request, 'duration_seconds', None) or 7200
        instruction = _make_instruction(request.zone_name, dur)
        video_path = controller.video_finder.find_video(request.zone_name)
        # OBS 模式无视频不阻断开播，FFmpeg 模式必须有视频
        if not video_path:
            stream_mode, _ = controller._get_stream_settings()
            if stream_mode == 'ffmpeg':
                return StartLiveResponse(success=False, message=f"未找到分区 {request.zone_name} 的视频文件，FFmpeg 推流不可用")
            # OBS 模式：允许无视频开播
            video_path = ''
        ok = controller.start_streaming(instruction, video_path, is_task_mode=False)
    else:
        instruction, video_path, err = _get_next_task_instruction()
        if err:
            return StartLiveResponse(success=False, message=err)
        ok = controller.start_streaming(instruction, video_path, is_task_mode=True)

    return _build_stream_response(controller, ok)


@router.post("/resume", summary="恢复进行中的直播")
async def resume_live():
    """从 live_state.json 恢复直播"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    if controller.is_streaming:
        return StartLiveResponse(success=False, message="当前已有直播进行中")

    state = controller.state
    if not state.current_zone:
        return StartLiveResponse(success=False, message="没有可恢复的直播任务")

    if controller.area_loader.get_area_id(state.current_zone, auto_update=False) is None:
        return StartLiveResponse(success=False, message=f"分区 '{state.current_zone}' 不存在于分区列表中")

    dur = getattr(state, 'duration_seconds', 7200)
    instruction = _make_instruction(state.current_zone, dur)
    video_path = controller.video_finder.find_video(state.current_zone)
    if not video_path:
        return StartLiveResponse(success=False, message=f"未找到分区 {state.current_zone} 的视频文件")

    ok = controller.start_streaming(instruction, video_path, is_task_mode=True)
    return _build_stream_response(controller, ok, success_msg="直播已恢复")


@router.post("/confirm-face-verify", summary="确认人脸验证完成")
async def confirm_face_verify():
    """前端用户完成人脸验证后调用，清除待验证状态"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    controller.confirm_face_verify()
    return {'success': True, 'message': '验证状态已确认'}


@router.post("/stop", summary="停止直播")
async def stop_live():
    """停止当前直播"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")

    success = controller.stop_streaming()
    return {
        'success': success,
        'message': '直播已停止' if success else '停止失败'
    }


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
    controller.state.is_streaming = False
    controller.state.current_zone = ''
    controller.state.elapsed_seconds = 0
    controller.state.duration_seconds = 0
    controller.state.save()
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
    controller.state.reload()
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
    changed = controller.state.update_state(**data)
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
async def switch_area(zone_name: str):
    """手动模式下正在直播时切换分区（调用 B 站换区 API，不下播）"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    if controller._stream_mode != 'manual':
        raise HTTPException(status_code=400, detail="仅手动模式支持直播中切换分区")
    if not controller.is_streaming:
        raise HTTPException(status_code=400, detail="当前未在直播")
    success = controller.switch_partition(zone_name)
    if success:
        # 更新当前指令的分区名
        if controller.current_instruction:
            controller.current_instruction.zone_name = zone_name
        return {'success': True, 'message': f'已切换到分区：{zone_name}'}
    raise HTTPException(status_code=500, detail=f'切换分区失败：{zone_name}')


@router.post("/run-next", summary="执行下一个任务")
async def run_next_task():
    """执行下一个待执行直播任务"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")

    if controller.is_streaming:
        return {
            'success': False,
            'message': '当前已有直播进行中'
        }

    success = controller.run_next_task()
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
    """从 B 站 API 获取最新分区列表"""
    controller = get_live_controller()
    if not controller:
        raise HTTPException(status_code=500, detail="直播控制器未初始化")
    success = controller.area_loader.fetch_and_save_areas(controller.api)
    if success:
        return {'success': True, 'message': f'已更新 {len(controller.area_loader.areas)} 个分区'}
    raise HTTPException(status_code=500, detail='获取分区列表失败')