"""
tasks.py - 任务管理相关 API（SQLite 版）

提供任务 CRUD、统计、标记完成、导入导出功能。
"""

import logging
from typing import Optional, List
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse

from app.dependencies import get_task_manager, get_live_controller
from app.models.schemas import (
    TaskListResponse, Task, TaskCreate, TaskUpdate, TaskDetail,
    ImportResult, ExportResult,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== 查询 ====================

@router.get('', summary='获取任务列表')
async def get_tasks():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    tasks = tm.get_tasks_as_list()
    stats = tm.get_stats()
    return {'tasks': tasks, **stats}


@router.get('/detail', summary='获取任务详情列表')
async def get_tasks_detail():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    tasks = tm.get_tasks_detail()
    stats = tm.get_stats()
    return {'tasks': tasks, **stats}


@router.get('/next', summary='获取下一个待执行任务')
async def get_next_task():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    instruction = tm.select_next_instruction()
    if instruction:
        return {
            'has_next': True,
            'zone_name': instruction.zone_name,
            'duration_seconds': instruction.duration_seconds
        }
    return {'has_next': False}


@router.get('/stats', summary='获取任务统计')
async def get_task_stats():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    return tm.get_stats()


# ==================== CRUD ====================

@router.post('', summary='创建任务')
async def create_task(task: TaskCreate, overwrite: bool = Query(False)):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.create_task(task.model_dump(), overwrite=overwrite)
    if success:
        verb = '已覆盖' if overwrite else '已创建'
        return {'success': True, 'message': f'任务 {task.zone_name} {verb}'}
    raise HTTPException(status_code=400, detail='创建任务失败（可能已存在同名任务）')


@router.put('/{zone_name}', summary='更新任务')
async def update_task(zone_name: str, task: TaskUpdate):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    updates = task.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail='未提供任何更新字段')
    new_zone = updates.pop('zone_name', None)
    target = new_zone or zone_name
    success = tm.update_task_fields(target, updates)
    if success:
        return {'success': True, 'message': f'任务 {target} 已更新'}
    raise HTTPException(status_code=400, detail=f'更新失败：{target}')


@router.delete('/{zone_name}', summary='删除任务')
async def delete_task(zone_name: str):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.delete_task_by_zone(zone_name)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已删除'}
    raise HTTPException(status_code=400, detail=f'删除失败：{zone_name}')


# ==================== 标记操作 ====================

@router.post('/mark-done/{zone_name}', summary='手动标记任务完成')
async def mark_task_done(zone_name: str):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.mark_task_done(zone_name)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已标记完成'}
    raise HTTPException(status_code=400, detail=f'标记失败：{zone_name}（可能不存在或已完成）')


@router.post('/mark-all-done/{zone_name}', summary='标记任务全部完成')
async def mark_task_all_done(zone_name: str):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.mark_task_all_done(zone_name)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已全部完成'}
    raise HTTPException(status_code=400, detail=f'标记失败：{zone_name}')


@router.post('/reload', summary='重新加载任务')
async def reload_tasks():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.load_tasks()
    if success:
        return {'success': True, 'message': f'已加载 {len(tm.tasks)} 个任务'}
    raise HTTPException(status_code=500, detail='重新加载任务失败')


# ==================== 导入/导出 ====================

@router.post('/import', summary='从 Excel 导入任务')
async def import_tasks(
    file: UploadFile = File(...),
    force: bool = Query(False),
    mode: str = Query('merge', description='导入模式: merge=合并, replace=全覆盖'),
):
    if mode not in ('merge', 'replace'):
        raise HTTPException(status_code=400, detail='mode 必须为 merge 或 replace')

    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')

    temp_path = Path('_temp_import.xlsx')
    try:
        content = await file.read()
        with open(temp_path, 'wb') as f:
            f.write(content)

        from app.core.db import TaskDB
        zone_names = TaskDB.read_zone_names_from_excel(str(temp_path))

        invalid_zones: List[str] = []
        lc = get_live_controller()
        if lc and lc.area_loader:
            all_area_names: set = set()

            def _collect_names(areas: list):
                for a in areas:
                    all_area_names.add(a.get('name', ''))
                    for child in a.get('children', []):
                        all_area_names.add(child.get('name', ''))

            _collect_names(lc.area_loader.areas)

            for z in zone_names:
                if z not in all_area_names:
                    invalid_zones.append(z)

        if invalid_zones and not force:
            return ImportResult(
                success=False,
                needs_confirmation=True,
                invalid_zones=invalid_zones,
                message=f'以下 {len(invalid_zones)} 个分区不存在或已下架，请检查拼写或符号错误',
            )

        if mode == 'replace':
            tm.db.delete_all_tasks()
            logger.info('全覆盖模式：已清空所有现有任务')

        result = tm.import_from_excel(
            str(temp_path),
            skip_zones=invalid_zones if force else None
        )
        return ImportResult(**result)
    except Exception as e:
        logger.error(f'导入失败：{e}', exc_info=True)
        raise HTTPException(status_code=500, detail=f'导入失败：{e}')
    finally:
        if temp_path.exists():
            temp_path.unlink()


@router.get('/export', summary='导出任务到 Excel')
async def export_tasks():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    export_path = Path('live_tasks.xlsx')
    try:
        stats = tm.get_stats()
        tm.db.export_to_excel(str(export_path), stats=stats)
        return FileResponse(
            path=str(export_path),
            filename='live_tasks.xlsx',
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        logger.error(f'导出失败：{e}', exc_info=True)
        raise HTTPException(status_code=500, detail=f'导出失败：{e}')
