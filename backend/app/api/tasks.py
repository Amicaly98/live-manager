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
from app.dependencies import (
    account_mode_enabled, capture_account_work, get_application_owner,
    get_control_service, get_task_manager,
)
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

#: 请求自带的**账号归属标识**头。前端在用户点下操作、**第一个确认 UI 之前**把当时
#: 的账号冻结进这一格（见 ``frontend/src/stores/tasks.ts`` 的 ``freezeWriteScope``），
#: 写请求与它的传输层重试都带同一份值。见 :func:`_claimed_write_work`。
ACCOUNT_UID_HEADER = 'X-Account-UID'


def _claimed_write_work(request: Request):
    """在**请求到达服务端这一刻**核验请求自带的账号归属，并冻结那份实际绑定。

    这是任务写口的**唯一**账号身份接缝（创建/覆盖、编辑、删除、人工完成、重载、
    导入共用）：

    - 账号模式下，写请求**必须**自带账号归属标识，且必须与**当前绑定**一致；缺失
      或与当前绑定不同都明确 409 —— 绝不把旧页面/旧请求的操作补成"最新账号"后
      执行，也不给它冻结一份新绑定。这里**没有**按请求形状放行的例外：一条写请求
      是不是"新建"（有没有 ``task_id`` / 版本 / 业务日）**不能**证明它属于此刻
      登录的账号，因此"不带标识"本身就不是受理依据（复核 S1）。
    - 兼容模式（实例目录里的单份任务库）没有第二个账号，任务是实例归属的，
      "这次请求属于哪个 UID"这个问题不存在：旧调用语义原样保留。
      可信内部同步入口（本模块的 ``_xxx_sync``，``frozen=None``）不走这里，同样
      沿用它们一贯的"当前账号"语义（见 :func:`_frozen_or_current`）。

    通过之后返回的 :class:`FrozenAccountWork` 是**这一份**绑定的凭据：排队到执行
    点时 ``enter_valid_work`` 会再核一次，期间换号即 409（交办 §6）。
    """
    from app.core.session_runtime.accounts import AccountContextExpired

    raw = request.headers.get(ACCOUNT_UID_HEADER)
    uid = None
    if raw is not None and str(raw).strip():
        try:
            uid = int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail=f'{ACCOUNT_UID_HEADER} 必须是一个正整数 UID')
        if uid <= 0:
            raise HTTPException(status_code=400,
                                detail=f'{ACCOUNT_UID_HEADER} 必须是一个正整数 UID')
    if not account_mode_enabled():
        return capture_account_work()
    if uid is None:
        raise HTTPException(
            status_code=409,
            detail='本次写请求没有携带账号归属标识（%s）：无法证明它属于当前登录'
                   '账号，已拒绝执行，请刷新页面后按当前账号重新操作'
                   % ACCOUNT_UID_HEADER)
    try:
        return capture_account_work(expected_uid=uid)
    except AccountContextExpired as exc:
        raise HTTPException(status_code=409, detail=exc.detail)


async def _run_under_binding(frozen, func, *args, busy_detail=None, **kwargs):
    """在**冻结的账号绑定**下执行一段同步工作（读或写）。

    ``frozen`` 是**领取那一刻**冻结的账号绑定（见 ``dependencies.capture_account_work``）。
    真正的执行在 ``_invoke_write`` 里先核验绑定仍然有效、再在"工作期间持有资源"的
    保护下调用既有事务入口，因此排队期间换了账号的请求会被**明确拒绝**，而不是在
    执行时随手取到新账号的库。

    M1 的三个只读入口也走同一条接缝：**读到哪份资料**与**报出哪个来源 UID** 必须
    出自同一次绑定。先用 ``get_task_manager()`` 读旧库、再用 ``capture_account_work()``
    贴一个当下的 UID，正是复核 R5 的第二条反例。
    """
    if not _WRITE_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail=busy_detail or '服务繁忙：任务请求排队已满，请稍后重试')
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _TASK_WRITES,
            functools.partial(_invoke_write, frozen, func, args, kwargs))
    finally:
        _WRITE_SLOTS.release()


async def _run_write(frozen, func, *args, **kwargs):
    """任务写请求的排队入口（与只读共用一条绑定接缝）。"""
    return await _run_under_binding(
        frozen, func, *args,
        busy_detail='服务繁忙：任务写请求排队已满，请稍后重试', **kwargs)


def _invoke_write(frozen, func, args, kwargs):
    from app.core.session_runtime.accounts import AccountContextExpired
    kwargs = dict(kwargs)
    kwargs['frozen'] = frozen
    try:
        with frozen.enter_valid_work():
            return func(*args, **kwargs)
    except AccountContextExpired as exc:
        raise HTTPException(status_code=409, detail=exc.detail)


def _frozen_or_current(frozen):
    """同步入口的**兼容**取用口：路由总是把领取时冻结的那一份传进来；省略时按
    "当前账号"现取一次。后者正是这些私有入口在本包之前的语义（单账号/旧测试直接
    调用它们的形态），因此旧调用方不需要跟着改签名。"""
    if frozen is not None:
        return frozen
    return capture_account_work()


def _task_manager_or_409(frozen):
    task_manager = frozen.task_manager
    if not task_manager and frozen.binding is None:
        # **没有账号绑定**（兼容模式 / 隔离夹具）：这些调用方在本包之前用的就是
        # 本模块的 `get_task_manager()`（旧夹具按名替换它），保持一致。
        # 有绑定却拿不到库不是这里的事——那属于账号绑定失效，由上一步拒绝。
        task_manager = get_task_manager()
    if not task_manager:
        raise HTTPException(
            status_code=409,
            detail='当前没有已登录账号的任务库：请先登录该账号后重试')
    return task_manager


def _require_current_task_manager():
    """只读入口的当前账号任务库；未登录是**状态**（409），不是服务故障。

    账号级数据路径下任务数据属于 UID：没有已登录账号就没有匿名任务库，因此这里
    明确回答"未登录"，而不是 500"未初始化"（那会让前端把状态当成服务挂了）。
    """
    task_manager = get_task_manager()
    if not task_manager:
        raise HTTPException(
            status_code=409,
            detail='当前未登录：任务数据属于已登录账号，请先登录后重试')
    return task_manager
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


# ==================== 提交事实与唤醒事实分开 ====================

def _business_day() -> str:
    """当前业务日（与唯一 Core 同一个应用时钟，不用进程的 date.today()）。"""
    owner = get_application_owner()
    if owner is not None:
        try:
            return owner.clock.wall().date().isoformat()
        except Exception:                             # noqa: BLE001
            pass
    return datetime.now().date().isoformat()


def _wake_after_commit(frozen, reason: str) -> dict:
    """**提交成功之后**的窄唤醒。投递失败如实返回，绝不谎称 DB 已回滚。

    唤醒**只发给本次提交所属的那一个账号绑定**：排队期间换过账号（绑定已失效）
    时不唤醒——否则迟到的 A 通知会叫醒 B 的运行时。返回 :class:`dict` 并挂在
    响应里的 ``wakeup`` 字段：调用方（前端）可以据此区分"数据已经写进去了"与
    "唤醒没有送出去"两件事。
    """
    if frozen is not None and not frozen.is_current():
        return {'delivered': False, 'reason': 'account_changed'}
    owner = get_application_owner()
    if owner is None:
        return {'delivered': False, 'reason': 'no_owner'}
    try:
        return owner.notify_tasks_changed(_business_day(), reason, timeout=2.0)
    except Exception as exc:                          # noqa: BLE001
        logger.warning('任务变化唤醒失败（数据库提交仍然有效）：%s', exc)
        return {'delivered': False, 'reason': f'{type(exc).__name__}: {exc}'}


def _reject_mutation(exc: TaskMutationRejected) -> HTTPException:
    """把"明确拒绝"翻译成 409（可操作），而不是 500 或未落盘的假成功。"""
    return HTTPException(status_code=409,
                         detail=f"{exc.reason}（{exc.code}）")


# ==================== 查询 ====================

@router.get('', summary='获取任务列表')
async def get_tasks():
    tm = _require_current_task_manager()
    revision, _rows = tm.get_snapshot()
    tasks = tm.get_tasks_as_list()
    stats = tm.get_stats()
    # business_date：客户端把它连同 task_id 一起回传，"标记今日完成"才带上
    # 渲染时的业务日——否则响应丢失后的重放会在次日把新的一天记成完成。
    return {'tasks': tasks, 'revision': revision,
            'business_date': _business_day(), **stats}


@router.get('/detail', summary='获取任务详情列表')
async def get_tasks_detail():
    tm = _require_current_task_manager()
    revision, _rows = tm.get_snapshot()
    tasks = tm.get_tasks_detail()
    stats = tm.get_stats()
    return {'tasks': tasks, 'revision': revision, **stats}


@router.get('/next', summary='获取下一个待执行任务')
async def get_next_task():
    tm = _require_current_task_manager()
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
    tm = _require_current_task_manager()
    revision, _rows = tm.get_snapshot()
    return {**tm.get_stats(), 'revision': revision,
            'business_date': _business_day()}


# ==================== CRUD ====================

def _instance_key() -> str:
    """本实例的入口标识（展示用，**不是**数据身份）。

    数据归属永远是 ``uid + task_id``；端口只是"这次操作从哪个入口进来"。这里
    如实报服务端自己绑定的地址与端口，不靠浏览器 ``location.port`` 推断（反向
    代理可能让浏览器看不到真实端口）。
    """
    return f'{config.API_HOST}:{config.API_PORT}'


def _frozen_uid(frozen) -> Optional[int]:
    binding = getattr(frozen, 'binding', None)
    if binding is None:
        return None
    try:
        return int(binding.uid)
    except (TypeError, ValueError):
        return None


def _require_create_protocol(task: TaskCreate, overwrite: bool) -> None:
    """默认 HTTP 创建口的**输入合同**：完整原预检 + 非空客户端令牌。

    复核 R1 的洞是"字段可选、服务端自行判定"：原预检齐全但删掉令牌 → 200 并新建
    （没有持久防重回执）；带令牌但不带预检 → 200 并新建。两者都必须改成**明确
    输入拒绝、零写入**。

    这不是"新产品需求"：交办 §4.3/§5.2/§5.3/§6 一直要求冻结预检与同一客户端
    令牌；"不存在/已终结不弹框"说的是**不用弹确认框**，不是可以不预检。服务端
    也**不会**替客户端补最新预检、补当前版本或生成替代令牌——那正好会让一次旧
    确认看起来仍然有效。

    旧 ``overwrite=true`` 形状仍然受理，但只当作**同一份原预检的另一种写法**：
    必须同时带上原来的 id / 版本 / 业务日（不得现查补当前身份），令牌同样不可省。
    """
    problems: List[str] = []
    token = task.operation_token
    if not isinstance(token, str) or not token.strip():
        problems.append('缺少客户端一次性令牌 operation_token')
    legacy = (overwrite and task.id is not None
              and task.expected_revision is not None
              and bool(task.business_date))
    if task.preview is None and not legacy:
        problems.append(
            '缺少提交前冻结的原始预检 preview（预检时的身份/版本/业务日）')
    elif task.preview is not None:
        if task.preview.revision is None:
            problems.append('原预检缺少提交前冻结的任务版本 revision')
        if not task.preview.business_day:
            problems.append('原预检缺少提交前冻结的业务日 business_day')
    if problems:
        raise HTTPException(
            status_code=400,
            detail=('创建请求不完整：' + '；'.join(problems)
                    + '。请重新读取该分区的当前状态后，按页面上的确认流程重新发起'
                      '创建（本次未执行、数据未变）'))


def _build_create_command(task: TaskCreate, overwrite: bool, frozen) -> dict:
    """把 HTTP 请求收敛成一份**不可变创建命令**。

    - ``days_done`` 一律不参与：新建流程统一从 0 开始（历史进度修正走既有编辑
      入口）。载荷里带了非零进度会记一条告警，但不会让它成为新任务的初始进度。
    - ``overwrite=true`` 是**旧调用形状**：用请求里的 id/版本/业务日组装同一份
      预检并视为已确认，走同一条事务（新 id / 0 进度），不再 UPDATE 旧 id。
    """
    preview = task.preview.model_dump() if task.preview is not None else None
    confirmed = bool(task.confirm_replace)
    if preview is None and overwrite and task.id is not None:
        preview = {'task_id': int(task.id),
                   'revision': task.expected_revision,
                   'business_day': task.business_date,
                   'existing_is_unfinished': None}
        confirmed = True
    if int(task.days_done or 0) != 0:
        logger.warning(
            '创建请求带了已完成的 %s 天：新建一律从 0 开始，该值不会成为新任务的'
            '初始进度（历史进度修正请用编辑入口）', task.days_done)
    return {
        'zone_name': task.zone_name,
        'category': task.category,
        'total_days': task.total_days,
        'deadline_raw': task.deadline_raw,
        'token': task.operation_token,
        'confirmed': confirmed,
        'preview': preview,
        'uid': _frozen_uid(frozen),
    }


@router.post('', summary='创建任务')
async def create_task(task: TaskCreate, request: Request,
                      overwrite: bool = Query(False)):
    """新建任务，或把同分区已有的当前任务**替换**成一份新身份。

    与其它写口共用同一道账号门：账号模式下请求必须自带冻结的账号归属（见
    :func:`_claimed_write_work`），无论是新建还是替换。

    输入合同是**强制**的：必须带非空的一次性令牌与提交前冻结的完整原预检
    （见 :func:`_require_create_protocol`），缺失一律明确拒绝且零写入。

    结果一律是**新 task_id、进度 0**；同分区已终结的旧任务直接让位（不弹框），
    未完成的旧任务必须带上针对**那一个 id** 的冻结预检与明确确认。
    """
    frozen = _claimed_write_work(request)
    _require_create_protocol(task, overwrite)
    return await _run_write(frozen, _create_task_sync, task, overwrite)


def _create_task_sync(task: TaskCreate, overwrite: bool, frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    command = _build_create_command(task, overwrite, frozen)
    try:
        outcome = tm.create_new_task(command)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if outcome.get('status') == 'not_committed' or outcome.get('task_id') is None:
        raise HTTPException(status_code=400,
                            detail='创建任务失败（已回滚，数据未变）')
    replayed = bool(outcome.get('replayed'))
    replaced_id = outcome.get('replaced_task_id')
    if replayed:
        message = f'任务 {task.zone_name} 已创建（本次是同一次操作的结果核对）'
    elif replaced_id is not None:
        message = (f'任务 {task.zone_name} 已用新任务替换'
                   f'（旧任务 {replaced_id} 保留在历史）')
    else:
        message = f'任务 {task.zone_name} 已创建'
    # 回执重放没有产生新变更：不重发唤醒（也绝不谎称"刚刚又播起来了"）。
    wakeup = ({'delivered': False, 'reason': 'replayed_no_change'} if replayed
              else _wake_after_commit(frozen, 'create'))
    return {
        'success': True,
        'message': message,
        'task_id': outcome.get('task_id'),
        'replaced_task_id': replaced_id,
        'replayed': replayed,
        'revision': outcome.get('revision'),
        # 数据库提交与唤醒投递是两件事实：投递失败照样返回创建成功。
        'wakeup': wakeup,
    }


def _creation_preview_sync(zone_name: str, frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    preview = tm.preview_creation(zone_name)
    return {**preview,
            # 归属**取自冻结的那一份绑定**，而不是回包时再取一次"当前账号"：
            # 读取期间换号时，资料行与来源 UID 仍必须出自同一次绑定（复核 R5）。
            'uid': _frozen_uid(frozen),
            'instance_key': _instance_key()}


@router.get('/creation-preview', summary='新建任务的一致预检')
async def get_creation_preview(request: Request, zone_name: str = Query(...)):
    """该分区的一致创建预检（只读）。

    行、版本、业务日与占用事实在同一份读快照里取；不拼接多个 API 的不同时间
    回包。回包里的 UID 是**领取这次读取的那份绑定**的 UID，调用方必须核对它仍
    等于自己冻结的范围，否则不得据此弹框或提交。

    账号模式下与写口共用同一道归属门：必须自带请求原 UID，缺失/旧 UID 明确拒绝；
    读取期间持有该绑定，换号即 409，绝不用新账号的资料配一个新 UID 返回。
    """
    frozen = _claimed_write_work(request)
    return await _run_under_binding(
        frozen, _creation_preview_sync, zone_name,
        busy_detail='服务繁忙：任务读取排队已满，请稍后重试')


def _creation_receipt_sync(operation_token: str, frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    try:
        record = tm.creation_receipt(operation_token)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if record is None:
        return {'found': False, 'token': operation_token}
    return {'found': True, 'token': operation_token,
            'result': dict(record.get('result') or {}),
            'uid': _frozen_uid(frozen)}


@router.get('/creation-receipt', summary='核对已提交的创建回执')
async def get_creation_receipt(request: Request, operation_token: str = Query(...)):
    """只读核对：这次创建是否已经提交过。

    **缺记录只表示尚未查到成功**，不证明请求没有执行；本入口不创建任何东西、
    不发 start/stop、不推进版本。

    回执查的是**请求原来那个账号**的库：账号模式下缺失/旧 UID 明确拒绝，不会
    转到"当前账号"的库去核对另一个账号的操作（复核 R5 第一条反例）。
    """
    frozen = _claimed_write_work(request)
    return await _run_under_binding(
        frozen, _creation_receipt_sync, operation_token,
        busy_detail='服务繁忙：任务读取排队已满，请稍后重试')


def _zone_history_sync(zone_name: str, offset: int, limit: int, frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    return {**tm.zone_history(zone_name, offset=offset, limit=limit),
            'uid': _frozen_uid(frozen)}


@router.get('/history', summary='当前账号该分区的历次任务与每日记录')
async def get_zone_history(request: Request, zone_name: str = Query(...),
                           offset: int = Query(0, ge=0),
                           limit: int = Query(20, ge=1, le=200)):
    """只读分区历史：**请求所属账号**的历次任务身份 + 每日完成账本。

    只查请求原来那个账号的库；不接受任意 UID 参数绕过绑定去读别的账号，也不会
    在换号之后把旧标签页的历史读成新账号的历史。读取不构造 TaskManager/Runtime、
    不触发日切、不做历史补写、不发 start/stop。
    """
    frozen = _claimed_write_work(request)
    return await _run_under_binding(
        frozen, _zone_history_sync, zone_name, offset, limit,
        busy_detail='服务繁忙：任务读取排队已满，请稍后重试')


@router.put('/{zone_name}', summary='更新任务')
async def update_task(zone_name: str, task: TaskUpdate, request: Request):
    frozen = _claimed_write_work(request)
    return await _run_write(frozen, _update_task_sync, zone_name, task)


def _update_task_sync(zone_name: str, task: TaskUpdate, frozen=None):
    frozen = _frozen_or_current(frozen)
    """更新任务。

    body 里的 ``zone_name`` 是**新的分区名**（重命名），不是"要更新哪条记录"；
    记录由路径参数或 body 的 ``id`` 定位。旧实现把 body 的 zone_name 当成更新
    目标，于是"把 A 改名为 B"会去改已经存在的 B，属于串写。
    """
    tm = _task_manager_or_409(frozen)
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
            'revision': tm.tasks_revision,
            'wakeup': _wake_after_commit(frozen, 'update')}


@router.delete('/{zone_name}', summary='删除任务')
async def delete_task(zone_name: str, request: Request,
                      task_id: Optional[int] = Query(None)):
    frozen = _claimed_write_work(request)
    return await _run_write(frozen, _delete_task_sync, zone_name, task_id)


def _delete_task_sync(zone_name: str, task_id: Optional[int], frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    try:
        success = tm.delete_task_by_zone(zone_name, task_id=task_id)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已删除',
                'revision': tm.tasks_revision,
                'wakeup': _wake_after_commit(frozen, 'delete')}
    raise HTTPException(status_code=400, detail=f'删除失败：{zone_name}')


# ==================== 标记操作 ====================

@router.post('/mark-done/{zone_name}', summary='手动标记任务完成')
async def mark_task_done(zone_name: str, request: Request,
                         task_id: Optional[int] = Query(None),
                         execution_date: Optional[str] = Query(
                             None, description='业务执行日 YYYY-MM-DD，缺省为今天')):
    """人工"今日完成"。

    与"自然到时"共用同一结算入口：按 task_id + 执行日做条件写入，同一天重复
    提交（含网络重放）至多结算一次，不会重复加天。同步写库移出事件循环，
    慢 DB 不会堵住健康检查与停止入口。
    """
    frozen = _claimed_write_work(request)
    return await _run_write(frozen, _mark_task_done_sync,
                            zone_name, task_id, execution_date)


def _mark_task_done_sync(zone_name: str, task_id, execution_date: str, frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
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
                'status': status, 'revision': tm.tasks_revision,
                'wakeup': _wake_after_commit(frozen, 'mark-done')}
    if status == 'replaced':
        raise HTTPException(status_code=409,
                            detail=f'任务 {zone_name} 已被替换/重建，未结算到新记录')
    if status == 'not_found':
        raise HTTPException(status_code=404, detail=f'未找到任务：{zone_name}')
    raise HTTPException(status_code=500, detail=f'结算失败（已回滚）：{zone_name}')


@router.post('/mark-all-done/{zone_name}', summary='标记任务全部完成')
async def mark_task_all_done(zone_name: str, request: Request,
                             task_id: Optional[int] = Query(None)):
    frozen = _claimed_write_work(request)
    return await _run_write(frozen, _mark_task_all_done_sync, zone_name, task_id)


def _mark_task_all_done_sync(zone_name: str, task_id: Optional[int], frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    try:
        success = tm.mark_task_all_done(zone_name, task_id=task_id)
    except TaskMutationRejected as exc:
        raise _reject_mutation(exc)
    if success:
        return {'success': True, 'message': f'任务 {zone_name} 已全部完成',
                'revision': tm.tasks_revision,
                'wakeup': _wake_after_commit(frozen, 'mark-all-done')}
    raise HTTPException(status_code=400, detail=f'标记失败：{zone_name}')


@router.post('/reload', summary='重新加载任务')
async def reload_tasks(request: Request):
    # "重新加载"与其它写口共用同一道账号门：它同样要能证明属于当前登录账号。
    frozen = _claimed_write_work(request)
    return await _run_write(frozen, _reload_tasks_sync)


def _reload_tasks_sync(frozen=None):
    frozen = _frozen_or_current(frozen)
    tm = _task_manager_or_409(frozen)
    success = tm.load_tasks()
    if success:
        return {'success': True, 'message': f'已加载 {len(tm.tasks)} 个任务',
                'revision': tm.tasks_revision,
                'wakeup': _wake_after_commit(frozen, 'reload')}
    raise HTTPException(status_code=500, detail='重新加载任务失败')


# ==================== 导入/导出 ====================

@router.post('/import', summary='从 Excel 导入任务')
async def import_tasks(
    request: Request,
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

    # 导入是一整批任务写入（replace 还会先清空）：它必须属于**发起它的那个账号**。
    frozen = _claimed_write_work(request)
    tm = _task_manager_or_409(frozen)

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
        # 分区校验是**纯能力**：直接读本实例缓存，不构造旧控制器。
        from app.core.session_runtime.area_capability import get_area_loader
        loader = get_area_loader()
        if loader.areas:
            all_area_names: set = set()

            def _collect_names(areas: list):
                for a in areas:
                    all_area_names.add(a.get('name', ''))
                    for child in a.get('children', []):
                        all_area_names.add(child.get('name', ''))

            _collect_names(loader.areas)

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
        from app.core.session_runtime.accounts import AccountContextExpired

        def _perform_import():
            # 与其它写请求同一条规则：执行点先核验这次导入所属的账号绑定仍然
            # 有效，并在工作期间持有那份账号资源。
            with frozen.enter_valid_work():
                return tm.import_from_excel(
                    str(temp_path),
                    skip_zones=invalid_zones if force else None,
                    mode=mode,
                    operation_token=x_operation_token)

        try:
            result = await loop.run_in_executor(_FILE_EXECUTOR, _perform_import)
        except AccountContextExpired as exc:
            raise HTTPException(status_code=409, detail=exc.detail)
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
        # 只有**真的提交了**才唤醒；被拒绝/无有效行不唤醒（也不谎称回滚）。
        payload['wakeup'] = (_wake_after_commit(frozen, f'import:{mode}')
                             if payload['success'] else
                             {'delivered': False, 'reason': 'not_committed'})
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

    导出是**账号数据工作**：整段读表 + 写文件都在冻结绑定的在途持有里完成，因此
    换账号/退出必须等它收尾才关库放锁（与任务 CRUD、日切走同一份生命周期）。
    """
    frozen = capture_account_work()
    tm = _task_manager_or_409(frozen)
    export_path = _temp_dir() / f'export-{uuid.uuid4().hex}.xlsx'
    loop = asyncio.get_running_loop()
    from app.core.session_runtime.accounts import AccountContextExpired

    def _perform_export():
        with frozen.enter_valid_work():
            return tm.export_to_excel(str(export_path))

    try:
        try:
            await loop.run_in_executor(_FILE_EXECUTOR, _perform_export)
        except AccountContextExpired as exc:
            raise HTTPException(status_code=409, detail=exc.detail)
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
