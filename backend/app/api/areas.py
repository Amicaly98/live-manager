"""
areas.py - 分区信息 API
"""

import logging
from fastapi import APIRouter, Query
from app.dependencies import get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", summary="获取所有分区列表")
async def get_all_areas():
    """获取完整分区列表（含层次结构）"""
    controller = get_live_controller()
    if not controller:
        return {'areas': [], 'message': '直播控制器未初始化'}
    return {'areas': controller.area_loader.areas}


@router.get("/search", summary="模糊搜索分区")
async def search_areas(keyword: str = Query("", description="搜索关键词")):
    """根据关键词模糊搜索分区"""
    controller = get_live_controller()
    if not controller:
        return {'results': []}
    results = controller.area_loader.fuzzy_search(keyword)
    return {'results': results, 'total': len(results)}


@router.post("/refresh", summary="刷新分区数据")
async def refresh_areas():
    """从 B 站 API 获取并保存最新分区"""
    controller = get_live_controller()
    if not controller:
        return {'success': False, 'message': '直播控制器未初始化'}
    success = controller.area_loader.fetch_and_save_areas(controller.api)
    if success:
        total_sub = sum(
            len(a.get('children', [])) for a in controller.area_loader.areas
        )
        return {
            'success': True,
            'message': f'更新成功，共 {len(controller.area_loader.areas)} 个大类、{total_sub} 个子分区'
        }