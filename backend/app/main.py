"""
main.py - FastAPI 后端入口

将所有业务逻辑包装为 RESTful API，供 Vue3 前端调用。
使用 app/dependencies.py 作为全局单例容器，避免循环导入。
"""

import sys
import time
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 确保 backend 目录在 sys.path 中
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.FileHandler('backend.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from app.core.config import API_HOST, API_PORT
from app.dependencies import init_globals, shutdown_globals, get_task_manager, get_live_controller


# ==================== 应用生命周期 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时初始化和清理"""
    logger.info("=" * 70)
    logger.info("直播控制系统 - 后端服务启动")
    logger.info("=" * 70)

    # 延迟导入以避免循环
    from app.core.task_manager import TaskManager
    from app.core.live_controller import LiveController

    task_manager_instance = None
    live_controller_instance = None

    try:
        task_manager_instance = TaskManager(
            db_path="live_tasks.db",
            excel_path="live_tasks.xlsx"
        )
        task_manager_instance.print_summary()
        logger.info("任务管理器初始化成功")
    except Exception as e:
        logger.error(f"任务管理器初始化失败：{e}", exc_info=True)
        task_manager_instance = None

    try:
        live_controller_instance = LiveController(
            task_manager_instance,
            state_file="live_state.json",
            area_file="bili_areas_full.json"
        )
        logger.info("直播控制器初始化成功")
    except Exception as e:
        logger.error(f"直播控制器初始化失败：{e}", exc_info=True)
        live_controller_instance = None

    # 注入到全局依赖容器（解决循环导入）
    from app.core.email_sender import EmailSender
    email_sender_instance = EmailSender()
    email_sender_instance.start_face_verify_server()
    init_globals(task_manager_instance, live_controller_instance, email_sender_instance)

    logger.info("=" * 70)
    logger.info("后端服务就绪")
    logger.info("=" * 70)

    yield  # 应用运行中

    # 关闭时的清理
    logger.info("正在关闭后端服务...")
    shutdown_globals()
    logger.info("后端服务已安全关闭")


# ==================== 创建应用 ====================
app = FastAPI(
    title="直播控制系统 API",
    description="面向主播的一站式自动化直播管理工具后端",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 配置（允许前端开发服务器和 Electron 访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "file://",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由（API 导入放在 lifespan 之后，确保不会触发循环）
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
    """健康检查接口"""
    tm = get_task_manager()
    lc = get_live_controller()
    return {
        "status": "ok",
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "task_manager": tm is not None,
        "live_controller": lc is not None
    }


@app.post("/api/shutdown", tags=["系统"])
async def shutdown():
    """安全关闭（保存运行日期和状态）"""
    logger.info("收到关闭请求，正在保存状态...")
    tm = get_task_manager()
    lc = get_live_controller()
    if lc:
        lc.shutdown()
    if tm:
        tm.shutdown()
    return {"status": "ok", "message": "状态已保存"}
