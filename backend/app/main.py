"""
main.py - FastAPI 后端入口（桌面版）

来源：bilibili-live-server 提交 8bd2fb67 的 ``app/main.py``（新控制核心链路）。
桌面适配点集中在这里，业务规则一律不复制：

- 生命周期构造**桌面 owner**（``session_runtime.desktop.build_desktop_owner``）：
  与应用 owner 同一份实现，另加两条桌面产品规则（重启只提示恢复；首次领取账号
  目录前先接管旧资料）。**不再构造旧 ``LiveController``**，也不并跑旧监控/日切。
- **不注册**跨实例操作台、面板鉴权中间件及其路由：桌面既不提供远端控制入口，
  也不启动控制台连接服务。
- 保留桌面既有的 CORS（``file://`` 的 ``"null"`` origin）、健康检查、
  ``/api/shutdown?stop_live=`` 语义与前端静态托管。
"""

import asyncio
import functools
import logging
import logging.handlers
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 确保 backend 目录在 sys.path 中
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# ---- 数据目录（必须在导入业务模块之前；run.py 已通过参数/环境变量传入）----
from app.core.config import (
    init, get_data_path, get_project_root, backend_log_path,
    BACKEND_LOG_MAX_BYTES, BACKEND_LOG_BACKUPS,
)
try:
    _data_dir = init()          # 幂等：run.py 已初始化则直接返回
except Exception as _exc:       # noqa: BLE001
    # 数据目录不可用：API 仍要起来（health 可达），业务模块会给出明确错误。
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger(__name__).error("数据目录初始化失败：%s", _exc)
    _data_dir = None


def _build_log_handlers():
    """A8：有容量控制的轮转日志；日志文件不可写不阻塞启动。"""
    handlers = [logging.StreamHandler()]
    if _data_dir is not None:
        try:
            backend_log_path().parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                backend_log_path(), encoding='utf-8',
                maxBytes=BACKEND_LOG_MAX_BYTES,
                backupCount=BACKEND_LOG_BACKUPS))
        except Exception as e:                              # noqa: BLE001
            logging.getLogger(__name__).warning(
                "日志文件不可用，仅输出到控制台：%s", e)
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=_build_log_handlers(),
)
logger = logging.getLogger(__name__)

from app.dependencies import (
    clear_owner, get_application_owner, get_control_service, get_task_manager,
    publish_owner,
)

#: 启动/关闭都用**事件循环之外**的有界执行器：同步慢活（数据接管、账号实例组装、
#: 资源关闭）不得占住事件循环。
_LIFECYCLE_EXECUTOR = ThreadPoolExecutor(max_workers=2,
                                         thread_name_prefix='app-lifecycle')

#: 保留最后一次关闭报告：关闭本身可能是"真事实"（未收回的资源），不能删掉。
LAST_CLOSE_REPORT = None

#: 「停止并退出」给资源收尾的有界预算（秒）。Electron 的关闭请求等 25 秒，这里留出
#: 余量，让平台关房与本地媒体回收能在一次请求内真实收齐，而不是必然报未收齐。
STOP_EXIT_CLOSE_BUDGET_SECONDS = 20.0

#: 「停止并退出」里等唯一 Core 把停止**做完**的上限（秒）。它是上面那份总预算的
#: 一部分，不是另一份预算：超时后仍按剩余时间走同一条关闭门，并如实报告"未确认"。
STOP_EXIT_SETTLE_SECONDS = 12.0


def _stop_settled(snapshot) -> bool:
    """停止是否已经在唯一 Core 里落地：Goal=stopped 且不再承担任何房间收尾。"""
    from app.core.session.model import WANT_STOPPED

    if not isinstance(snapshot, dict):
        return False
    goal = snapshot.get('goal') or {}
    return goal.get('wanted') == WANT_STOPPED and snapshot.get('step') == 'idle'


def _prepare_owner(owner) -> None:
    """启动期的**同步慢活**（在事件循环外执行）：接管数据、读凭据、起账号实例。"""
    from app.core.session_runtime.production import (
        DataTakeover, resolve_account_context, take_over_state,
    )

    data_dir = owner.data_dir
    boundaries = owner.boundaries
    # 日期预约的补做间隔沿用本实例**在播核对**的节拍：默认构造时它就是设置页保存的
    # scan_interval_seconds（见 production._apply_saved_scan_interval）。它与媒体端口
    # 的观察周期是两个不同职责，不共用同一字段。
    day_prep_interval = float(getattr(boundaries, 'watch_interval_seconds', 30.0) or 30.0)
    account, credentials, reason = resolve_account_context(
        data_dir=data_dir, clock=owner.clock, boundaries=boundaries)
    if account is None and reason not in ('no_credentials', 'no_session'):
        logger.warning("凭据存在但账号上下文不完整（%s）：控制保持未登录", reason)

    settings_snapshot = getattr(owner, 'settings_snapshot', None) or {}
    if owner.account_mode:
        # 账号级数据路径：**未登录不构造匿名任务库**，也不解释实例目录里的旧资料
        # （归属不明，等显式接管）。有已核验账号时才领取该 UID 的原生占用、在
        # **账号目录**上做数据接管（桌面还会先经 account_preparer 搬旧资料）。
        if account is None:
            owner.takeover = DataTakeover(
                status='account_required', reason='not_logged_in',
                detail='未登录：账号模式下不构造任务库，登录后按账号接管那一份资料')
            owner.instance_errors.append(
                f'未登录（{reason}）：账号模式下不构造匿名任务库')
        else:
            owner.takeover = DataTakeover(
                status='account_required', reason='pending',
                detail='账号数据接管尚未执行')
            owner.prepare_data_and_account(account=account, credentials=credentials)
            # 已核验账号的显示名/头像：面板左下角与通知署名都读它。旧凭据只缓存了
            # 登录 cookies 时先取缓存值，缺失则按需**有界**补齐（不阻塞启动）。
            owner.account_display_name = (
                account.display_name
                or str((credentials or {}).get('uname') or ''))
            owner.account_face = str((credentials or {}).get('face') or '')
            if not owner.account_display_name:
                owner.enrich_account_identity()
        owner.start_day_prep(interval=day_prep_interval)
        return

    takeover = take_over_state(data_dir=data_dir, account=account,
                               settings=settings_snapshot)
    owner.takeover = takeover
    logger.info("启动数据接管：%s（%s）", takeover.status, takeover.detail)
    if not takeover.control_available:
        owner.instance_errors.append(
            f'状态数据不可用于控制：{takeover.reason or takeover.detail}')
        owner.start_day_prep(interval=day_prep_interval)
        return
    owner.account_display_name = (account.display_name if account is not None else '')
    if account is not None:
        owner.prepare_data_and_account(account=account, credentials=credentials)
    owner.start_day_prep(interval=day_prep_interval)


# ==================== 应用生命周期 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时初始化和清理。

    启动顺序固定：``config.init``（run.py 已完成）→ 桌面 owner → 数据接管/凭据/
    账号实例（事件循环外）→ 发布依赖 → 运行 → 统一关闭门。
    """
    global LAST_CLOSE_REPORT
    from app.core.session_runtime.desktop import build_desktop_owner

    logger.info("=" * 70)
    logger.info("直播控制系统 - 后端服务启动（桌面版 / 新控制核心）")
    logger.info("=" * 70)

    owner = None
    loop = asyncio.get_running_loop()
    try:
        data_dir = Path(get_data_path('')).resolve()
        owner = build_desktop_owner(data_dir=data_dir)
        await loop.run_in_executor(_LIFECYCLE_EXECUTOR,
                                   functools.partial(_prepare_owner, owner))
        publish_owner(owner)
        logger.info("启动入口：协议=http 端口=%s 监听=%s",
                    os.environ.get('API_PORT', 'unknown'),
                    os.environ.get('API_HOST', 'unknown'))
        for note in list(owner.instance_errors):
            logger.warning("启动注意：%s", note)
        logger.info("后端服务就绪")
    except Exception:
        logger.error("后端启动失败，正在释放已取得资源", exc_info=True)
        owner_report = None
        owner_error = None
        if owner is not None:
            try:
                owner_report = await loop.run_in_executor(
                    _LIFECYCLE_EXECUTOR, owner.close)
            except Exception as exc:                        # noqa: BLE001
                owner_error = exc
                logger.error("启动失败后的资源释放同样失败", exc_info=True)
        report = dict(owner_report) if isinstance(owner_report, dict) else {}
        errors = list(report.get('errors') or [])
        if owner_error is not None:
            errors.append(f'应用 owner 关闭异常：{owner_error}')
        report['errors'] = errors
        report['ok'] = bool(report.get('ok', True)) and owner_error is None
        LAST_CLOSE_REPORT = report
        clear_owner()
        raise

    yield  # 应用运行中

    logger.info("正在关闭后端服务...")
    owner_report = None
    owner_error = None
    if owner is not None:
        try:
            owner_report = await loop.run_in_executor(
                _LIFECYCLE_EXECUTOR, owner.close)
        except Exception as exc:                            # noqa: BLE001
            owner_error = exc
            logger.error("关闭资源时出现异常", exc_info=True)
    report = dict(owner_report) if isinstance(owner_report, dict) else {}
    if owner_error is not None:
        report.setdefault('errors', []).append(f'应用 owner 关闭异常：{owner_error}')
        report['ok'] = False
    LAST_CLOSE_REPORT = report
    clear_owner()
    if report and not report.get('ok', True):
        logger.warning("关闭过程存在未收齐资源，不宣称安全关闭：%s",
                       report.get('errors'))
    else:
        logger.info("后端服务已安全关闭")


# ==================== 创建应用 ====================
app = FastAPI(
    title="直播控制系统 API",
    description="面向主播的一站式自动化直播管理工具后端（桌面版）",
    version="1.2.0",
    lifespan=lifespan
)

# CORS 配置（允许前端开发服务器和 Electron 访问）
# file:// 协议下浏览器 Origin 为字符串 "null"（Electron 打包后请求需要被放行）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-Operation-Token", "X-Settings-Revision"],
)

# 注册路由（API 导入放在 lifespan 之后，确保不会触发循环）。
# 桌面**不**注册 /api/panel 与 /api/instance*：没有面板远程登录，也没有跨实例
# 操作台、远端控制或同步批量创建入口。
from app.api.auth import router as auth_router
from app.api.tasks import router as tasks_router
from app.api.live import router as live_router
from app.api.areas import router as areas_router
from app.api.settings import router as settings_router
from app.api.email import router as email_router

app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(tasks_router, prefix="/api/tasks", tags=["任务管理"])
app.include_router(live_router, prefix="/api/live", tags=["直播控制"])
app.include_router(areas_router, prefix="/api/areas", tags=["分区管理"])
app.include_router(settings_router, prefix="/api/settings", tags=["设置管理"])
app.include_router(email_router, prefix="/api/email", tags=["邮件推送"])


# ==================== 健康检查 ====================
@app.get("/api/health", tags=["系统"])
async def health_check():
    """健康检查接口（Electron 的 BackendLifecycle 轮询它等待就绪）。

    ``未登录`` 是**合法的健康状态**；它不等于"控制器故障"。旧控制器字段保留
    但恒为 ``None``（默认入口不再构造它），另给出唯一 owner 的真实事实。
    """
    tm = get_task_manager()
    control = get_control_service()
    owner = get_application_owner()
    instance = control.instance if control is not None else None
    override = {}
    if owner is not None:
        try:
            override = owner.health()
        except Exception:                                   # noqa: BLE001
            override = {}
    return {
        "status": "ok",
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "task_manager": tm is not None,
        "live_controller": None,          # 默认入口不再构造旧控制器
        "control_service": control is not None,
        "logged_in": instance is not None,
        "account": (None if instance is None else {
            "uid": int(getattr(instance.account, 'uid', 0)),
            "room_id": int(getattr(instance.account, 'room_id', 0)),
        }),
        **override,
    }


# ==================== 关闭入口 ====================

def _request_platform_stop() -> dict:
    """「停止并退出」的第一步：经**同一** ``ControlService`` 发一次停止意图。

    受理只表示 Goal 已落盘（``accepted``），**不等于**平台已经关房；关房由本进程的
    Runtime 继续推进。这里在**有界**窗口内等唯一 Core 把这次停止做完（关房核对 →
    必要时下播 → 收尾），并把是否等到的真实结果一并回报：等不到就照实说没确认，
    绝不把"已受理"写成"平台已关房"。
    """
    import uuid

    control = get_control_service()
    if control is None:
        return {'status': 'skipped', 'reason': '控制服务尚未就绪'}
    instance = control.instance
    if instance is None:
        return {'status': 'skipped', 'reason': '当前没有已登录账号'}
    try:
        receipt = control.stop(uuid.uuid4().hex)
    except Exception as exc:                                # noqa: BLE001
        logger.error("停止意图未能受理：%s", exc, exc_info=True)
        return {'status': 'error', 'reason': f'{type(exc).__name__}: {exc}'}
    report = receipt.to_dict()
    if not receipt.accepted:
        return report
    waiter = getattr(instance, 'wait_for_snapshot', None)
    if waiter is None:
        report['settled'] = None       # 没有等待接口：不假装已确认收尾
        return report
    report['settled'] = waiter(_stop_settled, STOP_EXIT_SETTLE_SECONDS) is not None
    return report


def _platform_stop_confirmed(report) -> bool:
    """「停止并退出」的前置判据：**这一次平台停止是否确证收尾**。

    本地资源收齐**不足以**证明"停止并退出"完成（复核 R3）：房间还在播时销毁 owner
    会让调用方失去继续关房/重试所需的后端。三种结论分别处理：

    - ``skipped``：没有控制服务 / 当前没有已登录账号 → 本程序没有要关的门，判据不
      适用，**不**阻断正常退出；
    - 回执未被受理 → 未确证；
    - 已受理 → 以 ``settled``（唯一 Core 是否真的走到"Goal=stopped 且 step=idle"）
      为准。受理只是把意图落盘，不是关房事实。
    """
    if not isinstance(report, dict):
        return False
    if report.get('status') == 'skipped':
        return True
    if not report.get('accepted'):
        return False
    return bool(report.get('settled'))


@app.post("/api/shutdown", tags=["系统"])
async def shutdown(stop_live: bool = True):
    """桌面受控关闭入口。

    - ``stop_live=true``（停止并退出）：先经 ``ControlService`` 发停止意图，在有界
      窗口内等**唯一 Core** 收尾（关房核对 → 必要时下播 → 收尾）；**确证收尾**之后
      才走与 lifespan 同一道幂等关闭门回收自有资源。
    - ``stop_live=false``（不停止并退出）：**不**请求平台下播，只回收自有本地媒体与
      后端；运行资料与恢复依据保留。

    未确证或资源未收齐时返回 503（``SHUTDOWN_NOT_CONFIRMED``）并**不**放行进程退出、
    **不**释放账号占用、**不**销毁运行实例：由调用方自行决定重试或强杀。这里不伪报
    成功、不误杀外部进程、不把"已受理"写成"平台已关房"。
    """
    owner = get_application_owner()
    if owner is None:
        return {"status": "ok", "message": "服务未启动或已关闭", "steps": []}
    loop = asyncio.get_running_loop()
    stop_report = None
    budget = None
    if stop_live:
        started = time.monotonic()
        stop_report = await loop.run_in_executor(_LIFECYCLE_EXECUTOR,
                                                 _request_platform_stop)
        # 停止本身用掉的等待从**同一份**预算里扣：收尾不会再获得一整份新预算。
        budget = max(0.0, STOP_EXIT_CLOSE_BUDGET_SECONDS
                     - (time.monotonic() - started))
        if not _platform_stop_confirmed(stop_report):
            # 平台停止未确证：本地资源收齐也不能算"停止并退出"完成。保留运行实例
            # （继续关房/重试所需）、账号占用与后端，明确回报未收齐事实。
            logger.error("停止并退出未确证，保留后端继续核验：%s", stop_report)
            return JSONResponse(status_code=503, content={
                "status": "error",
                "message": "平台停止未确证，保留后端继续核验，未退出",
                "code": "SHUTDOWN_NOT_CONFIRMED",
                "stop_live": True,
                "stop": stop_report,
                "ok": False,
                "steps": [],
                "errors": ["平台停止未确证（受理 ≠ 已关房）：未销毁运行实例、未释放账号占用"],
            })
    if budget is None:
        report = await loop.run_in_executor(_LIFECYCLE_EXECUTOR, owner.close)
    else:
        report = await loop.run_in_executor(_LIFECYCLE_EXECUTOR,
                                            functools.partial(owner.close, budget))
    clean = bool(report.get('ok', True))
    payload = {
        "status": "ok" if clean else "error",
        "message": "资源已按统一关闭门释放" if clean else "本地资源未确认收齐，未退出",
        "stop_live": bool(stop_live),
        "stop": stop_report,
        "ok": clean,
        "steps": report.get('steps', []),
        "errors": report.get('errors', []),
    }
    if not clean:
        payload["code"] = "SHUTDOWN_NOT_CONFIRMED"
        return JSONResponse(status_code=503, content=payload)
    server = getattr(app.state, 'uvicorn_server', None)
    if server is not None:
        server.should_exit = True
    else:
        logger.warning("未找到 uvicorn server 句柄：进程将由调用方强杀兜底")
    return payload


# ==================== 前端静态文件托管 ====================
def _get_frontend_dist() -> Path:
    """获取前端构建目录"""
    custom = os.environ.get("FRONTEND_DIR")
    if custom:
        return Path(custom)
    return get_project_root() / "frontend" / "dist"


frontend_dist = _get_frontend_dist()

if frontend_dist.exists():
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    # 静态资源（JS/CSS/图片等）
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="static-assets")

    # SPA fallback：所有非 API 路由返回 index.html
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # 跳过 API 路由（已被上面的 include_router 注册）
        file_path = frontend_dist / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(frontend_dist / "index.html"))
else:
    logger.warning("前端构建目录不存在：%s，静态文件托管已禁用", frontend_dist)
