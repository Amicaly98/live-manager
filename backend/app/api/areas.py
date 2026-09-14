"""
areas.py - 分区信息 API

分区列表是**运行时数据**（不入库）：缓存位于数据目录（userData/data 或
--data-dir）；没有缓存时接口仍正常返回，只是 available=False 并给出提示，
面板与其它功能不受影响。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi import APIRouter, Query
from app.dependencies import get_live_controller

logger = logging.getLogger(__name__)
router = APIRouter()

AREAS_UNAVAILABLE_HINT = '分区数据暂不可用（本实例暂无缓存），请点击「刷新分区」联网获取'

# 刷新走线程池：网络往返不占事件循环，不会卡住状态/停止等控制请求
_REFRESH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='areas-refresh')


@router.get("", summary="获取所有分区列表")
async def get_all_areas():
    """获取完整分区列表（含层次结构）+ 可用性状态"""
    controller = get_live_controller()
    if not controller:
        return {'areas': [], 'available': False,
                'status': 'controller_unavailable',
                'message': '直播控制器未初始化'}
    loader = controller.area_loader
    if not loader.areas:
        return {'areas': [], 'available': False, 'status': loader.status,
                'message': AREAS_UNAVAILABLE_HINT}
    return {'areas': loader.areas, 'available': True,
            'status': loader.status, 'message': ''}


@router.get("/search", summary="模糊搜索分区")
async def search_areas(keyword: str = Query("", description="搜索关键词")):
    """根据关键词模糊搜索分区"""
    controller = get_live_controller()
    if not controller:
        return {'results': [], 'total': 0, 'available': False,
                'message': '直播控制器未初始化'}
    results = controller.area_loader.fuzzy_search(keyword)
    available = bool(controller.area_loader.areas)
    return {'results': results, 'total': len(results), 'available': available,
            'message': '' if available else AREAS_UNAVAILABLE_HINT}


@router.post("/refresh", summary="刷新分区数据")
async def refresh_areas():
    """从 B 站 API 获取最新分区并写入本实例缓存。

    网络中转放到线程池执行（不占事件循环）；失败保留旧数据并返回明确失败状态，
    不返回假成功。
    """
    import asyncio
    controller = get_live_controller()
    if not controller:
        return {'success': False, 'status': 'controller_unavailable',
                'message': '直播控制器未初始化'}
    loader = controller.area_loader
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        _REFRESH_POOL, partial(loader.fetch_and_save_areas, controller.api))
    if success:
        total_sub = sum(
            len(a.get('children', [])) for a in controller.area_loader.areas
        )
        return {
            'success': True,
            'status': loader.status,
            'message': f'更新成功，共 {len(controller.area_loader.areas)} 个大类、{total_sub} 个子分区'
        }
    return {
        'success': False,
        'status': loader.status,
        'message': f'刷新分区失败（{loader.status}），已保留当前可用数据',
    }
