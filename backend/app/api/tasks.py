"""
tasks.py - 任务管理相关 API（SQLite 版）

提供任务 CRUD、统计、标记完成、导入导出功能。
"""

import asyncio
import functools
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Optional, List
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Request, Header
from fastapi.responses import FileResponse

from app.core import config
from app.dependencies import get_task_manager, get_live_controller
from app.models.schemas import (
    TaskListResponse, Task, TaskCreate, TaskUpdate, TaskDetail,
    ImportResult, ExportResult,
)
from app.core.task_manager import TaskMutationRejected

logger = logging.getLogger(__name__)
router = APIRouter()

# 导入/导出上限（2c2g 环境）：超限明确拒绝，不在内存里堆整张表。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024        # 8 MB
MAX_IMPORT_ROWS = 20000                   # 与 db.plan_import_from_excel 一致
# 同步 sqlite/openpyxl/文件写入必须离开事件循环，且不能占用无界线程：
# 阻塞慢导入/慢磁盘时，健康检查和停止入口仍要能被事件循环受理。
_FILE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tasks-io')
#: 任务写请求（CRUD/结算/重载）也走线程池：它们在 async handler 里同步抢
#: _mutation_lock 并写 SQLite，长事务期间会把事件循环整个堵住，连健康检查与
#: 停止入口都收不到请求。
_TASK_WRITES = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tasks-write')
#: 排队上限：ThreadPoolExecutor 只限线程数、**不限排队数**，慢 DB 时请求会
#: 无限堆积；显式给一个上界，过载时明确拒绝而不是把内存吃满。
_MAX_WRITE_PENDING = 16
_WRITE_SLOTS = threading.BoundedSemaphore(2 + _MAX_WRITE_PENDING)


async def _run_write(func, *args, **kwargs):
    """把同步写操作移出事件循环；排队已满时明确 503。"""
    if not _WRITE_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail='服务繁忙：任务写请求排队已满，请稍后重试')
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _TASK_WRITES, functools.partial(func, *args, **kwargs))
    finally:
        _WRITE_SLOTS.release()
# 临时文件所在目录：各实例自己的数据目录，绝不使用进程工作目录（多实例共享
# 同一 WorkingDirectory 时会互相覆盖 `_temp_import.xlsx` / `live_tasks.xlsx`）。
_TEMP_SUBDIR = 'tmp'


def _temp_dir() -> Path:
    """本实例专属的临时目录（在数据目录内，跨实例不冲突）。"""
    try:
        base = Path(config.get_data_path(_TEMP_SUBDIR))
    except Exception:
        base = Path(tempfile.gettempdir()) / 'bilibili-live-tmp'
    base.mkdir(parents=True, exist_ok=True)
    return base


def _reject_mutation(exc: TaskMutationRejected) -> HTTPException:
    """把"明确拒绝"翻译成 409（可操作），而不是 500 或未落盘的假成功。"""
    return HTTPException(status_code=409,
                         detail=f"{exc.reason}（{exc.code}）")


# ==================== 查询 ====================

@router.get('', summary='获取任务列表')
async def get_tasks():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    revision, _rows = tm.get_snapshot()
    tasks = tm.get_tasks_as_list()
    stats = tm.get_stats()
    # business_date：客户端把它连同 task_id 一起回传，"标记今日完成"才带上
    # 渲染时的业务日——否则响应丢失后的重放会在次日把新的一天记成完成。
    return {'tasks': tasks, 'revision': revision,
            'business_date': _business_date(), **stats}


def _business_date() -> str:
    """当前业务日（服务端口径）。"""
    return datetime.now().date().isoformat()


@router.get('/detail', summary='获取任务详情列表')
async def get_tasks_detail():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    revision, _rows = tm.get_snapshot()
    tasks = tm.get_tasks_detail()
    stats = tm.get_stats()
    return {'tasks': tasks, 'revision': revision, **stats}


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
    """轻量统计 + 快照版本。

    前端用它做"版本驱动刷新"：只有 revision 前进才重取完整列表，
    不必把整个任务列表按秒轮询（也不把慢查询压在 live 状态上）。
    """
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    revision, _rows = tm.get_snapshot()
    return {**tm.get_stats(), 'revision': revision,
            'business_date': _business_date()}


# ==================== CRUD ====================

@router.post('', summary='创建任务')
async def create_task(task: TaskCreate, overwrite: bool = Query(False)):
    return await _run_write(_create_task_sync, task, overwrite)


def _create_task_sync(task: TaskCreate, overwrite: bool):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    try:
        success = tm.create_task(task.model_dump(), overwrite=overwrite)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if success:
        verb = '已覆盖' if overwrite else '已创建'
        return {'success': True, 'message': f'任务 {task.zone_name} {verb}',
                'revision': tm.tasks_revision}
    raise HTTPException(status_code=400, detail='创建任务失败（可能已存在同名任务）')


@router.put('/{zone_name}', summary='更新任务')
async def update_task(zone_name: str, task: TaskUpdate):
    return await _run_write(_update_task_sync, zone_name, task)


def _update_task_sync(zone_name: str, task: TaskUpdate):
    """更新任务。

    body 里的 ``zone_name`` 是**新的分区名**（重命名），不是"要更新哪条记录"；
    记录由路径参数或 body 的 ``id`` 定位。旧实现把 body 的 zone_name 当成更新
    目标，于是"把 A 改名为 B"会去改已经存在的 B，属于串写。
    """
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    payload = task.model_dump(exclude_none=True)
    task_id = payload.pop('id', None)
    new_zone = payload.pop('zone_name', None)
    if not payload and new_zone is None:
        raise HTTPException(status_code=400, detail='未提供任何更新字段')
    if task_id is None and not zone_name:
        raise HTTPException(status_code=400, detail='缺少任务身份（id 或分区名）')
    # 重命名与其它字段必须在**同一个事务**里提交：旧实现先 rename 再 update，
    # 第二步失败会留下"名字改了、字段没改"的半次用户操作。
    try:
        ok = tm.update_task_fields(
            zone_name, payload, task_id=task_id,
            rename_to=new_zone if task_id is not None else None)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if not ok:
        raise HTTPException(status_code=400, detail=f'更新失败：{zone_name}')
    label = new_zone or zone_name
    return {'success': True, 'message': f'任务 {label} 已更新',
            'revision': tm.tasks_revision}


@router.delete('/{zone_name}', summary='删除任务')
async def delete_task(zone_name: str, task_id: Optional[int] = Query(None)):
    return await _run_write(_delete_task_sync, zone_name, task_id)


def _delete_task_sync(zone_name: str, task_id: Optional[int]):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    try:
        success = tm.delete_task_by_zone(zone_name, task_id=task_id)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已删除',
                'revision': tm.tasks_revision}
    raise HTTPException(status_code=400, detail=f'删除失败：{zone_name}')


# ==================== 标记操作 ====================

@router.post('/mark-done/{zone_name}', summary='手动标记任务完成')
async def mark_task_done(zone_name: str,
                         task_id: Optional[int] = Query(None),
                         execution_date: Optional[str] = Query(
                             None, description='业务执行日 YYYY-MM-DD，缺省为今天')):
    """人工"今日完成"。

    与"自然到时"共用同一结算入口：按 task_id + 执行日做条件写入，同一天重复
    提交（含网络重放）至多结算一次，不会重复加天。同步写库移出事件循环，
    慢 DB 不会堵住健康检查与停止入口。
    """
    return await _run_write(_mark_task_done_sync, zone_name, task_id, execution_date)


def _mark_task_done_sync(zone_name: str, task_id, execution_date: str):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    exec_date = None
    if execution_date:
        try:
            exec_date = datetime.strptime(execution_date, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(status_code=400, detail='execution_date 需为 YYYY-MM-DD')
    outcome = tm.settle_task_done(zone_name, execution_date=exec_date,
                                  task_id=task_id)
    status = outcome['status']
    if status in ('settled', 'already'):
        note = '已标记完成' if status == 'settled' else '今日已完成（未重复计天）'
        return {'success': True, 'message': f'任务 {zone_name} {note}',
                'status': status, 'revision': tm.tasks_revision}
    if status == 'replaced':
        raise HTTPException(status_code=409,
                            detail=f'任务 {zone_name} 已被替换/重建，未结算到新记录')
    if status == 'not_found':
        raise HTTPException(status_code=404, detail=f'未找到任务：{zone_name}')
    raise HTTPException(status_code=500, detail=f'结算失败（已回滚）：{zone_name}')


@router.post('/mark-all-done/{zone_name}', summary='标记任务全部完成')
async def mark_task_all_done(zone_name: str, task_id: Optional[int] = Query(None)):
    return await _run_write(_mark_task_all_done_sync, zone_name, task_id)


def _mark_task_all_done_sync(zone_name: str, task_id: Optional[int]):
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    try:
        success = tm.mark_task_all_done(zone_name, task_id=task_id)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已全部完成',
                'revision': tm.tasks_revision}
    raise HTTPException(status_code=400, detail=f'标记失败：{zone_name}')


@router.post('/reload', summary='重新加载任务')
async def reload_tasks():
    return await _run_write(_reload_tasks_sync)


def _reload_tasks_sync():
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    success = tm.load_tasks()
    if success:
        return {'success': True, 'message': f'已加载 {len(tm.tasks)} 个任务',
                'revision': tm.tasks_revision}
    raise HTTPException(status_code=500, detail='重新加载任务失败')


# ==================== 导入/导出 ====================

@router.post('/import', summary='从 Excel 导入任务')
async def import_tasks(
    file: UploadFile = File(...),
    force: bool = Query(False),
    mode: str = Query('merge', description='导入模式: merge=合并, replace=全覆盖'),
    x_operation_token: Optional[str] = Header(
        default=None, alias='X-Operation-Token'),
):
    """导入任务。

    - 临时文件落在**本实例数据目录内的唯一文件**，不再用进程工作目录下的
      `_temp_import.xlsx`：多个实例共享同一发布目录时会互相覆盖/删除输入；
    - 先完整解析校验，再在单事务内落盘；replace 模式不再"先 DELETE 再解析"，
      失败不会留下空库；
    - 没有有效行时拒绝 replace，不把"文件全是非法行"解释成"想清空任务库"；
    - 同步 openpyxl/sqlite 工作放到有界执行器，不堵事件循环。
    """
    if mode not in ('merge', 'replace'):
        raise HTTPException(status_code=400, detail='mode 必须为 merge 或 replace')

    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')

    temp_path = _temp_dir() / f'import-{uuid.uuid4().hex}.xlsx'
    try:
        # 分块落盘：旧实现 `await file.read()` 先把整个文件读进内存再判上限，
        # 8MiB 不是内存硬上界；这里边读边写，超限立刻中止并拒绝。
        written = 0
        with open(temp_path, 'wb') as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f'文件过大（超过 {MAX_UPLOAD_BYTES} 字节上限），已拒绝导入')
                f.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail='上传内容为空')

        from app.core.db import TaskDB
        zone_names = await asyncio.get_running_loop().run_in_executor(
            _FILE_EXECUTOR, TaskDB.read_zone_names_from_excel, str(temp_path))
        if len(zone_names) > MAX_IMPORT_ROWS:
            raise HTTPException(status_code=413,
                                detail=f'行数超过上限 {MAX_IMPORT_ROWS}')

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
            return {
                'success': False,
                'imported_count': 0,
                'needs_confirmation': True,
                'invalid_zones': invalid_zones,
                'rejected': True,
                'message': f'以下 {len(invalid_zones)} 个分区不存在或已下架，请检查拼写或符号错误',
                'revision': tm.tasks_revision,
            }

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                _FILE_EXECUTOR,
                lambda: tm.import_from_excel(
                    str(temp_path),
                    skip_zones=invalid_zones if force else None,
                    mode=mode,
                    operation_token=x_operation_token))
        except TaskMutationRejected as exc:
            raise _reject_mutation(exc)
        payload = dict(result)
        # Keep the committed revision on a replay response.  A later task write
        # may have advanced the live revision, but replacing it here would make
        # the response look like the replay itself had committed again.
        if payload.get('revision') is None:
            payload['revision'] = tm.tasks_revision
        payload['success'] = not bool(payload.get('rejected'))
        payload['imported_count'] = (
            int(payload.get('imported') or 0)
            + int(payload.get('updated') or 0))
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'导入失败：{e}', exc_info=True)
        raise HTTPException(status_code=500, detail=f'导入失败：{e}')
    finally:
        # 只清理本请求自己的临时文件，不做目录级无差别清理。
        try:
            if temp_path.exists():
                os.unlink(temp_path)
        except OSError as exc:
            logger.warning('清理导入临时文件失败：%s', exc)


@router.get('/export', summary='导出任务到 Excel')
async def export_tasks():
    """导出任务。

    产物写到本实例数据目录内的唯一文件，并在响应完成后删除**本请求自己的**文件；
    不再使用工作目录下的共享 `live_tasks.xlsx`（既会被别的实例覆盖，也可能被
    当成导入源）。响应体来自同一版本快照，导出期间的任务修改不会混进这张表。
    """
    tm = get_task_manager()
    if not tm:
        raise HTTPException(status_code=500, detail='任务管理器未初始化')
    export_path = _temp_dir() / f'export-{uuid.uuid4().hex}.xlsx'
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            _FILE_EXECUTOR, lambda: tm.export_to_excel(str(export_path)))
        if not export_path.exists() or export_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail='导出产物为空，未生成文件')
        return _ExportFileResponse(path=str(export_path), filename='live_tasks.xlsx')
    except HTTPException:
        _safe_unlink(export_path)
        raise
    except Exception as e:
        _safe_unlink(export_path)
        logger.error(f'导出失败：{e}', exc_info=True)
        raise HTTPException(status_code=500, detail=f'导出失败：{e}')


class _ExportFileResponse(FileResponse):
    """响应发完后删除本请求自己的临时文件（后台任务，不延迟响应）。"""

    def __init__(self, path: str, filename: str):
        super().__init__(
            path=path,
            filename=filename,
            media_type='application/vnd.openxmlformats-'
                       'officedocument.spreadsheetml.sheet')
        self._cleanup_path = path
        self.background = self._cleanup

    async def _cleanup(self):
        _safe_unlink(Path(self._cleanup_path))


def _safe_unlink(path) -> None:
    try:
        p = Path(path)
        if p.exists():
            os.unlink(p)
    except OSError as exc:
        logger.warning('清理临时文件失败：%s', exc)
