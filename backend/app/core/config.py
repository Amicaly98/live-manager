"""
config.py - 后端配置（桌面版）

本文件 = 服务端 ``app/core/config.py`` 的路径模型 + 桌面版既有的路径访问口。
两套口径合一是**刻意**的：新增的 session / session_runtime 运行链读的是
``get_data_path`` / ``area_file_path`` / ``VIDEO_BASE_PATH`` / ``ACCOUNT_DATA_ROOT``，
而桌面既有的迁移、日志、旧测试读的是 ``state_file_path()`` / ``backend_log_path()``
等函数。它们必须指向**同一个**数据目录，否则迁移会搬到 A、运行链读的是 B。

数据目录（桌面既有约定，E3/A8）：
- 所有可写运行时文件（状态、数据库、Excel、cookies、日志、缓存）统一落在
  一个"数据目录"内，默认由 Electron 通过 ``--data-dir`` 传入（userData/data），
  或环境变量 BILIBILI_DATA_DIR；都没有时回退到仓库根 data/ 目录。
- 路径必须在后端导入任何业务模块之前初始化（run.py 解析参数后立即调用
  ``init`` / ``init_data_dir``）。

账号根（桌面是单实例、单账号客户端，但仍按 UID 归属任务/进度/历史）：
- 显式 ``--account-root`` / ``ACCOUNT_DATA_ROOT`` 优先；缺省由**启动入口**
  run.py 传入 ``data_dir`` 父目录下的 ``account-data``（即 userData/account-data）。
- ``init`` 本身的语义与服务端逐字相同：``account_root`` 为 None 且无环境变量时
  **不启用**账号模式。桌面默认值留在 run.py，便于隔离夹具与旧测试沿用兼容模式。
"""

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ==================== 运行时初始化 ====================
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent  # 回到项目根目录
_DATA_DIR: Path = None
_data_dir_initialized = False

#: **同机共享的账号数据根目录**（M0A）。它为 None 时保持旧行为（实例目录里的单份
#: 任务库）；一旦配置，任务/进度/历史/账本/版本/日切都按 ``<root>/accounts/<uid>/``
#: 分库。桌面默认单账号，但归属仍按 UID 记，因此 run.py 默认就会传它。
ACCOUNT_DATA_ROOT: Path = None

#: 未显式配置账号根时使用的默认子目录名（与桌面启动入口的默认一致）。
DEFAULT_ACCOUNT_ROOT_NAME = 'account-data'

#: 视频文件根目录。**真正的权威是设置页的 ``video_path``**（见 api.settings）；
#: 这里只是"设置不可信 / 未初始化"时的兜底，且 init() 之后即可用。
VIDEO_BASE_PATH: Path = None
DEFAULT_VIDEO_FOLDER: Path = None


def resolve_account_root(data_dir, account_root: str = None) -> Path:
    """把一个账号根配置解释成绝对路径（**唯一**的推导规则）。

    显式值优先于环境变量；直接调用本辅助函数且两者都缺时，才推导数据目录
    父目录下的默认根。默认启动的 ``init()`` 会先检查是否显式配置，未配置时
    不调用本函数，也不启用账号模式。
    """
    raw = account_root or os.getenv("ACCOUNT_DATA_ROOT")
    if raw:
        return Path(raw).expanduser().resolve()
    base = Path(data_dir) if data_dir else Path(os.getenv("DATA_DIR", "./data"))
    return (base.resolve().parent / DEFAULT_ACCOUNT_ROOT_NAME).resolve()


def init(data_dir: Optional[str] = None, account_root: Optional[str] = None) -> Path:
    """初始化数据目录（必须在导入业务模块之前调用，幂等）。

    ``account_root`` 给出（或设置了 ``ACCOUNT_DATA_ROOT``）时启用账号级数据路径；
    省略且没有环境变量时 ``ACCOUNT_DATA_ROOT`` 保持 None，旧调用方行为不变。
    """
    global _DATA_DIR, _data_dir_initialized
    global VIDEO_BASE_PATH, DEFAULT_VIDEO_FOLDER, ACCOUNT_DATA_ROOT
    if _data_dir_initialized:
        return _DATA_DIR
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
    _DATA_DIR = resolved

    raw_root = account_root or os.getenv("ACCOUNT_DATA_ROOT")
    ACCOUNT_DATA_ROOT = (resolve_account_root(_DATA_DIR, account_root)
                         if raw_root else None)

    # 视频根：桌面既有的 BILIBILI_VIDEO_PATH 与服务端的 VIDEO_BASE_PATH 都认，
    # 顺序按"更具体的前缀名优先"。真正生效的目录仍是设置页的 video_path，
    # 默认组装会用已保存设置覆盖它（见 session_runtime.production）。
    vbp = os.getenv("VIDEO_BASE_PATH") or os.getenv("BILIBILI_VIDEO_PATH")
    VIDEO_BASE_PATH = Path(vbp).expanduser() if vbp else Path.home() / "videosforlive"
    DEFAULT_VIDEO_FOLDER = VIDEO_BASE_PATH / "default"

    _data_dir_initialized = True
    logger.info(f"数据目录已初始化：{_DATA_DIR}")
    return _DATA_DIR


def init_data_dir(data_dir: Optional[str] = None) -> Path:
    """桌面启动入口使用的别名（同一份实现，不许出现两个数据根）。"""
    return init(data_dir)


def account_root() -> Path:
    """当前配置的共享账号根；未启用账号模式时为 None。"""
    return ACCOUNT_DATA_ROOT


def account_mode_enabled() -> bool:
    """是否启用账号级数据路径（默认组装据此决定任务数据绑定方式）。"""
    return ACCOUNT_DATA_ROOT is not None


def console_enabled() -> bool:
    """桌面产品**没有**跨实例操作台：恒为 False。

    服务端按 ``CONSOLE_ROLE``/端口决定管理角色；桌面既不注册那些管理入口，
    也不启动控制台连接服务，因此这里给出明确的产品事实，而不是让调用方各自
    去猜端口。保留同名函数是为了让共用代码不必分叉。
    """
    return False


def console_role() -> str:
    """桌面恒为普通实例（见 :func:`console_enabled`）。"""
    return "normal"


def get_data_path(filename: str = ""):
    """获取数据目录下的文件路径。

    与桌面既有实现保持一致：**没有**"未初始化就偷偷 init 一个默认根"的隐式行为，
    调用方必须显式初始化（run.py 已经保证）。未初始化时按默认数据目录解析，
    绝不落到进程工作目录。
    """
    base = _DATA_DIR if _data_dir_initialized else BASE_DIR / "data"
    return base / filename if filename else base


def get_project_root() -> Path:
    """获取桌面项目根目录（bilibili-live-manager/）。"""
    return BASE_DIR


# ==================== 动态分区数据 ====================
# B 站分区列表是频繁变化的外部业务数据，不是代码资产：运行时缓存落在数据目录
# （每实例一份），仓库根/backend 下的同名历史文件只作一次性兼容导入来源。
# 读写/校验/迁移规则见 app.core.area_data。
AREA_CACHE_FILENAME = "bili_areas_full.json"


def _p(name: str) -> Path:
    """数据目录内文件路径（目录未初始化时按默认数据目录解析）。"""
    return (_DATA_DIR if _data_dir_initialized else BASE_DIR / "data") / name


def area_file_path() -> Path:
    """运行时分区缓存路径（数据目录内）。"""
    return _p(AREA_CACHE_FILENAME)


# ==================== 运行时数据文件（全部在数据目录内） ====================
def state_file_path() -> Path:
    return _p("live_state.json")

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
# 桌面是单机客户端：默认只监听回环地址。
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ==================== 发布标识 ====================
#: **唯一的发布构建标识来源**：状态读取与控制受理都以它判断"是否同一发布版本"。
RELEASE_ID = "20261009-desktop-port-1"

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
