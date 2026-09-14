"""
config.py - 后端配置（统一管理路径、API 地址等）

从原 live_controller.py / task_manager.py 中提取的配置项。
仍保留对原有文件的兼容（可动态读取 bili_areas_full.json 等）。

数据目录（A8/E3）：
- 所有可写运行时文件（状态、数据库、Excel、cookies、日志、缓存）统一落在
  一个"数据目录"内，默认由 Electron 通过 `--data-dir` 传入（userData/data），
  或环境变量 BILIBILI_DATA_DIR；都没有时回退到仓库根 data/ 目录。
- 路径必须在后端导入任何业务模块之前初始化（run.py 解析参数后立即调用
  init_data_dir）。
- 旧版本把数据散落在工作目录；首次在新数据目录运行时会带备份迁移，
  已存在的目标文件不会被旧数据覆盖（见 data_migration.py）。
"""

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ==================== 只读资源路径 ====================
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent  # 回到项目根目录

# 视频文件根目录（与原相同，用户可设置）
VIDEO_BASE_PATH = Path(os.getenv("BILIBILI_VIDEO_PATH", "F:/videosforlive"))
DEFAULT_VIDEO_FOLDER = VIDEO_BASE_PATH / "default"

# ==================== 数据目录 ====================
DATA_DIR: Path = BASE_DIR / "data"
_data_dir_initialized = False


def init_data_dir(data_dir: Optional[str] = None) -> Path:
    """初始化数据目录（必须在导入业务模块之前调用，幂等）。

    优先级：显式参数 > 环境变量 BILIBILI_DATA_DIR > 默认（BASE_DIR/data）。
    目录不存在则创建；创建失败抛出异常由调用方决定终止。
    """
    global DATA_DIR, _data_dir_initialized
    if _data_dir_initialized:
        return DATA_DIR
    candidate = data_dir or os.getenv("BILIBILI_DATA_DIR") or str(BASE_DIR / "data")
    resolved = Path(candidate).resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        # 可写性探测：真实写入并读回（文件保留，不删除——部分环境的
        # 文件删除保护会拦截删除操作，且保留探测文件本身无害）
        probe = resolved / ".write_probe"
        probe.write_text(datetime.now().isoformat(), encoding="utf-8")
        if probe.read_text(encoding="utf-8") == "":
            raise RuntimeError("探测文件写入后读回为空")
    except Exception as e:
        raise RuntimeError(f"数据目录不可用：{resolved}（{e}）") from e
    DATA_DIR = resolved
    _data_dir_initialized = True
    logger.info(f"数据目录已初始化：{DATA_DIR}")
    return DATA_DIR


def _p(name: str) -> Path:
    """数据目录内文件路径（目录未初始化时按默认数据目录解析）。"""
    return (DATA_DIR if _data_dir_initialized else BASE_DIR / "data") / name


# ==================== 运行时数据文件（全部在数据目录内） ====================
def state_file_path() -> Path:
    return _p("live_state.json")

def area_file_path() -> Path:
    return _p("bili_areas_full.json")

def cookies_file_path() -> Path:
    return _p("bili_cookies.json")

def rtmp_cache_file_path() -> Path:
    return _p("rtmp_cache.json")

def settings_file_path() -> Path:
    return _p("settings.json")

def db_file_path() -> Path:
    return _p("live_tasks.db")

def excel_file_path() -> Path:
    return _p("live_tasks.xlsx")

def last_run_file_path() -> Path:
    return _p("task_manager_last_run.json")

def backend_log_path() -> Path:
    return _p("logs") / "backend.log"

def ffmpeg_log_path() -> Path:
    return _p("logs") / "ffmpeg.log"

def stop_intent_file_path() -> Path:
    """用户停止意图持久化文件（A7）：停止后写入，重新开播成功后清除。"""
    return _p("stop_intent.json")

def temp_dir_path() -> Path:
    d = _p("temp")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d

# 兼容旧代码的模块级常量（import 时按当前 DATA_DIR 解析；init_data_dir 之后
# 应改用上述函数获取路径）
DEFAULT_EXCEL_PATH = str(_p("live_tasks.xlsx"))
DEFAULT_STATE_FILE = str(_p("live_state.json"))
DEFAULT_AREA_FILE = str(_p("bili_areas_full.json"))

# ==================== API 服务配置 ====================
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ==================== Bilibili API ====================
BILIBILI_APP_KEY = "aae92bc66f3edfab"
BILIBILI_APP_SEC = "af125a0d5279fd576c1b4418a3e8276d"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'zh-CN,zh;q=0.9',
    'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'user-agent': USER_AGENT
}

# ==================== 直播默认参数 ====================
DEFAULT_LIVE_DURATION_BASE = 7200  # 秒
MAX_RECONNECT_ATTEMPTS = 3
MONITOR_INTERVAL = 30  # 秒
CROSS_DAY_CHECK_INTERVAL = 60  # 秒

# ==================== 日志容量（A8） ====================
BACKEND_LOG_MAX_BYTES = 5 * 1024 * 1024    # 单文件 5MB
BACKEND_LOG_BACKUPS = 3
FFMPEG_LOG_MAX_BYTES = 10 * 1024 * 1024    # 单文件 10MB
