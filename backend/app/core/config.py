"""
config.py - 后端配置（统一管理路径、API 地址等）

从原 live_controller.py / task_manager.py 中提取的配置项。
仍保留对原有文件的兼容（可动态读取 bili_areas_full.json 等）。
"""

import os
from pathlib import Path
from typing import Optional

# ==================== 路径配置 ====================
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent  # 回到项目根目录

# 视频文件根目录（与原相同）
VIDEO_BASE_PATH = Path("F:/videosforlive")
DEFAULT_VIDEO_FOLDER = VIDEO_BASE_PATH / "default"

# 数据文件默认存放目录（可在运行时覆盖）
DATA_DIR = BASE_DIR / "data"

# ==================== Excel/状态文件 ====================
DEFAULT_EXCEL_PATH = str(Path("live_tasks.xlsx").resolve())
DEFAULT_STATE_FILE = str(Path("live_state.json").resolve())
DEFAULT_AREA_FILE = str(Path("bili_areas_full.json").resolve())

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