"""
live_controller.py - 直播控制器（重构版）

从原 live_controller.py 提取核心类 LiveController、BilibiliApi、VideoPathFinder、AreaLoader 等。
为 API 层提供简洁的接口。
"""

import os
import sys
import json
import time
import random
import logging
import threading
import subprocess
import hashlib
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple, List, Dict, TYPE_CHECKING
from datetime import datetime, date, timedelta

import requests

from app.core.config import (
    VIDEO_BASE_PATH, DEFAULT_VIDEO_FOLDER,
    BILIBILI_APP_KEY, BILIBILI_APP_SEC,
    HEADERS, DEFAULT_LIVE_DURATION_BASE,
    MAX_RECONNECT_ATTEMPTS, MONITOR_INTERVAL,
    CROSS_DAY_CHECK_INTERVAL
)
from app.models.schemas import LiveInstruction, Task

if TYPE_CHECKING:
    from app.core.task_manager import TaskManager

logger = logging.getLogger(__name__)


# ==================== VideoPathFinder（原样保留） ====================
class VideoPathFinder:
    """视频路径查找器（优先分区名文件夹，其次 default）"""

    @staticmethod
    def find_video(zone_name: str) -> Optional[str]:
        zone_names = [
            zone_name,
            zone_name.replace("区", ""),
            zone_name.lower()
        ]
        for name in zone_names:
            zone_folder = VIDEO_BASE_PATH / name
            if zone_folder.exists() and zone_folder.is_dir():
                video_files = (list(zone_folder.glob("*.mp4")) +
                              list(zone_folder.glob("*.mkv")) +
                              list(zone_folder.glob("*.flv")))
                if video_files:
                    video_path = random.choice(video_files)
                    logger.info(f" 找到分区视频：{video_path}")
                    return str(video_path)
        if DEFAULT_VIDEO_FOLDER.exists() and DEFAULT_VIDEO_FOLDER.is_dir():
            video_files = (list(DEFAULT_VIDEO_FOLDER.glob("*.mp4")) +
                          list(DEFAULT_VIDEO_FOLDER.glob("*.mkv")) +
                          list(DEFAULT_VIDEO_FOLDER.glob("*.flv")))
            if video_files:
                video_path = random.choice(video_files)
                logger.info(f" 使用 default 视频：{video_path}")
                return str(video_path)
        logger.error(f" 未找到视频文件（分区：{zone_name}）")
        return None

    @staticmethod
    def ensure_folders():
        VIDEO_BASE_PATH.mkdir(parents=True, exist_ok=True)
        DEFAULT_VIDEO_FOLDER.mkdir(parents=True, exist_ok=True)
        logger.info(f" 视频文件夹已准备：{VIDEO_BASE_PATH}")


# ==================== BilibiliApi（核心 API 封装） ====================
class BilibiliApi:
    """B 站直播 API 封装（支持 cookies 持久化）"""

    def __init__(self, cookie_file: str = "bili_cookies.json"):
        self.cookies = {}
        self.headers = HEADERS.copy()
        self.cookie_file = Path(cookie_file)
        self._load_cookies()

    def _load_cookies(self):
        if not self.cookie_file.exists():
            return
        try:
            with open(self.cookie_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.cookies = data.get('cookies', {})
            if self.cookies:
                logger.debug(f" 已加载 {len(self.cookies)} 个 cookies")
        except Exception as e:
            logger.debug(f" 加载 cookies 失败：{e}")

    def _save_cookies(self):
        if not self.cookies:
            return
        try:
            with open(self.cookie_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'cookies': self.cookies,
                    'last_update': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f" 保存 cookies 失败：{e}")

    def update_cookies(self, cookies: dict):
        self.cookies.update(cookies)
        self._save_cookies()
        logger.info(f" Cookies 已更新（共{len(self.cookies)}项）")

    def is_logged_in(self) -> bool:
        required_keys = ['SESSDATA', 'bili_jct', 'DedeUserID']
        return all(key in self.cookies for key in required_keys)

    def validate_login(self) -> bool:
        if not self.is_logged_in():
            return False
        success, user_info = self.get_user_info()
        if success and user_info.get('code') == 0:
            uid = user_info.get('data', {}).get('mid')
            if uid:
                logger.debug(f" 登录验证成功 | UID: {uid}")
                return True
        logger.warning(" 登录验证失败，cookies 可能已过期")
        return False

    def _appsign(self, params: dict) -> dict:
        params = params.copy()
        params['appkey'] = BILIBILI_APP_KEY
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        sign = hashlib.md5((query + BILIBILI_APP_SEC).encode()).hexdigest()
        params['sign'] = sign
        return params

    def _get_buvid3_simple(self) -> str:
        """生成简单的 buvid3"""
        return hashlib.md5(
            f"{time.time()}_{random.randint(1000, 9999)}".encode()
        ).hexdigest()[:16]

    def _req(self, method: str, url: str, params: dict = None, data: dict = None) -> Tuple[bool, dict]:
        """发送 API 请求，网络层无限重试（指数退避，上限30s）"""
        attempt = 0
        while True:
            attempt += 1
            try:
                url = url.strip()
                req_cookies = self.cookies.copy()
                if 'buvid3' not in req_cookies:
                    buvid3 = self._get_buvid3_simple()
                    if buvid3:
                        req_cookies['buvid3'] = buvid3
                timeout = 10
                if method == "GET":
                    resp = requests.get(url, params=params, cookies=req_cookies, headers=self.headers, timeout=timeout)
                else:
                    resp = requests.post(url, params=params, data=data, cookies=req_cookies, headers=self.headers, timeout=timeout)
                try:
                    json_data = resp.json()
                    code = json_data.get("code", -1)
                    msg = json_data.get("message") or json_data.get("msg", "")
                    if code == 0:
                        logger.debug(f" API 成功：{msg}")
                    else:
                        logger.warning(f" API 失败：code={code}, msg={msg}")
                    return code == 0, json_data
                except ValueError:
                    logger.error(f" JSON 解析失败：{resp.status_code} {resp.text[:100]}")
                    return False, {"code": -1, "msg": "JSON 解析失败"}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException) as e:
                err_name = type(e).__name__
                logger.warning(f" 网络错误 {err_name} (第{attempt}次)：{url}")
            # 指数退避：1s, 2s, 4s, 8s, 16s, 30s, 30s...
            wait = min(2 ** (attempt - 1), 30)
            time.sleep(wait)

    # ==================== 公开 API 方法 ====================
    def get_qrcode(self) -> Tuple[bool, dict]:
        """获取登录二维码"""
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        params = {"source": "main-fe-header"}
        return self._req("GET", url, params=params)

    def poll_qrcode(self, qrcode_key: str) -> Tuple[bool, dict]:
        """轮询二维码扫码状态"""
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        params = {"qrcode_key": qrcode_key}
        return self._req("GET", url, params=params)

    def save_qrcode_image(self, qr_url: str) -> Optional[str]:
        """下载并保存二维码图片到本地，返回文件路径"""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            import io as _io
            resp = requests.get(qr_url, timeout=10)
            if resp.status_code == 200:
                save_path = Path("_temp_qrcode.png")
                with open(save_path, 'wb') as f:
                    f.write(resp.content)
                logger.info(f" 二维码已保存：{save_path}")
                return str(save_path)
        except Exception as e:
            logger.error(f" 保存二维码失败：{e}")
        return None

    def get_user_info(self) -> Tuple[bool, dict]:
        """获取用户信息"""
        url = "https://api.bilibili.com/x/space/myinfo"
        return self._req("GET", url)

    def get_live_status(self, room_id: int) -> Tuple[bool, dict]:
        """获取直播间状态"""
        return self._req("GET", f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}")

    def create_room(self) -> Tuple[bool, dict]:
        """创建直播间"""
        url = "https://api.live.bilibili.com/room/v1/Room/create"
        data = {}
        return self._req("POST", url, data=data)

    def start_live(self, room_id: int, area_id: int, csrf: str) -> Tuple[bool, dict]:
        """开始直播（带 ts/build/version 签名，模拟直播姬）"""
        # 获取服务端时间戳
        success, ts_resp = self._req("GET", "https://api.bilibili.com/x/report/click/now")
        if not success:
            return False, ts_resp
        ts = ts_resp.get("data", {}).get("now", int(time.time()))

        # 获取直播姬版本信息
        v_params = self._appsign({"system_version": 2, "ts": ts})
        success, v_resp = self._req(
            "GET",
            "https://api.live.bilibili.com/xlive/app-blink/v1/liveVersionInfo/getHomePageLiveVersion",
            params=v_params
        )
        if not success:
            return False, v_resp

        v_data = v_resp.get("data", {})
        data = {
            "room_id": room_id,
            "platform": "pc_link",
            "area_v2": area_id,
            "backup_stream": "0",
            "csrf_token": csrf,
            "csrf": csrf,
            "build": v_data.get("build", "0"),
            "version": v_data.get("curr_version", "0.0.0"),
            "ts": ts,
        }
        url = "https://api.live.bilibili.com/room/v1/Room/startLive"
        return self._req("POST", url, data=self._appsign(data))

    def stop_live(self, room_id: int, csrf: str) -> Tuple[bool, dict]:
        """停止直播"""
        url = "https://api.live.bilibili.com/room/v1/Room/stopLive"
        data = {
            "room_id": room_id,
            "platform": "pc_link",
            "csrf_token": csrf,
            "csrf": csrf,
        }
        return self._req("POST", url, data=data)

    def update_area(self, room_id: int, area_id: int, csrf: str) -> Tuple[bool, dict]:
        """切换直播分区"""
        url = "https://api.live.bilibili.com/room/v1/Room/update"
        data = {
            "room_id": room_id,
            "area_id": area_id,
            "platform": "pc_link",
            "csrf_token": csrf,
            "csrf": csrf,
        }
        return self._req("POST", url, data=data)

    def get_areas(self) -> Tuple[bool, dict]:
        """获取直播分区列表"""
        url = "https://api.live.bilibili.com/room/v1/Area/getList"
        return self._req("GET", url)

    def get_csrf(self) -> Optional[str]:
        """从 cookies 中获取 csrf token"""
        return self.cookies.get('bili_jct')

    def get_room_id_by_uid(self, uid: int) -> Tuple[bool, dict]:
        """通过 UID 获取直播间 ID（多接口容错）"""
        import time as _time
        # 方法1
        try:
            url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                room_id = resp['data'].get('room_id') or resp['data'].get('roomid')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        _time.sleep(1.5)
        # 方法2
        try:
            url = f"https://api.bilibili.com/x/space/acc/info?mid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                live_room = resp['data'].get('live_room', {})
                room_id = live_room.get('roomid')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        _time.sleep(1.5)
        # 方法3
        try:
            url = f"https://api.live.bilibili.com/room/v2/Room/room_id_by_uid?uid={uid}"
            success, resp = self._req("GET", url)
            if success and resp.get('code') == 0 and 'data' in resp:
                room_id = resp['data'].get('room_id')
                if room_id:
                    return True, {'data': {'room_id': room_id}}
        except:
            pass
        return False, {'code': -1, 'msg': '无法获取直播间 ID'}

    def clear_cookies(self):
        """清除 cookies"""
        self.cookies = {}
        self._save_cookies()
        logger.info(" Cookies 已清除")

    def get_push_url(self, room_id: int) -> Tuple[bool, dict]:
        """获取推流地址（RTMP 地址 + 推流码）"""
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
        success, resp = self._req("GET", url)
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            rtmp = data.get('rtmp', {})
            addr = rtmp.get('addr', '')
            code = rtmp.get('code', '')
            if addr and code:
                push_url = f"{addr}{code}" if not addr.endswith('/') else f"{addr}{code}"
                logger.info(f" 获取推流地址成功：{addr}...")
                return True, {'push_url': push_url, 'rtmp_addr': addr, 'rtmp_code': code}
            # 兼容其他字段名
            addr2 = data.get('rtmp_addr', '')
            code2 = data.get('rtmp_code', '')
            if addr2 and code2:
                push_url = f"{addr2}{code2}" if not addr2.endswith('/') else f"{addr2}{code2}"
                return True, {'push_url': push_url, 'rtmp_addr': addr2, 'rtmp_code': code2}
            logger.warning(f" 获取推流地址失败：响应中无 rtmp 数据")
        return False, {'msg': resp.get('msg', '获取推流地址失败')}


# ==================== AreaLoader（分区加载器） ====================
class AreaLoader:
    """直播分区动态加载器"""

    def __init__(self, area_file: str = "bili_areas_full.json"):
        self.area_file = Path(area_file)
        self.areas: List[dict] = []
        self._search_cache: dict = {}
        self._load_areas()

    def _load_areas(self):
        if self.area_file.exists():
            try:
                with open(self.area_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.areas = data if isinstance(data, list) else []
                if self.areas:
                    logger.info(f" 加载分区数据：{len(self.areas)} 项")
                else:
                    logger.warning(f" 分区文件为空，将尝试在线获取")
            except Exception as e:
                logger.warning(f" 加载分区文件失败：{e}")
                self.areas = []
        else:
            logger.info(" 分区文件不存在，将在启动时从 API 获取")
            self.areas = []

    def fuzzy_search(self, keyword: str) -> List[dict]:
        """模糊搜索分区 — 子分区优先，支持拼音首字母检索"""
        if not keyword or not self.areas:
            return []
        keyword = keyword.lower().strip()
        if keyword in self._search_cache:
            return self._search_cache[keyword]

        # 扁平化所有分区（含 children）
        def flat_all(areas: list, parent_name: str = '') -> list:
            result = []
            for area in areas:
                pname = parent_name or area.get('parent_name', '')
                result.append({**area, 'parent_name': pname})
                for child in area.get('children', []):
                    result.extend(flat_all([child], area.get('name', '')))
            return result

        all_areas = flat_all(self.areas)

        # 搜索 + 子分区优先排序
        results = []
        for area in all_areas:
            name = str(area.get('name', '')).lower()
            parent = str(area.get('parent_name', '')).lower()
            pinyin = _to_pinyin_initials(name)
            matched = (
                keyword in name or
                keyword in parent or
                keyword in pinyin
            )
            if matched:
                score = 100 if area.get('parent_id', 0) != 0 else 0
                if keyword == name:
                    score += 50
                elif name.startswith(keyword):
                    score += 30
                if pinyin and keyword in pinyin:
                    score += 20  # 拼音匹配加分
                results.append((score, area))

        results.sort(key=lambda x: x[0], reverse=True)
        results = [area for _, area in results][:20]
        self._search_cache[keyword] = results
        return results

    def get_area_id(self, zone_name: str, auto_update: bool = False) -> Optional[int]:
        """根据分区名获取 area_id"""
        results = self.fuzzy_search(zone_name)
        if results:
            return results[0].get('id')
        logger.error(f" 未找到分区：{zone_name}")
        return None

    def fetch_and_save_areas(self, api: BilibiliApi) -> bool:
        """从 B 站 API 获取最新分区并保存"""
        success, resp = api.get_areas()
        if success and resp.get('code') == 0:
            data = resp.get('data', [])
            if data:
                def flatten(areas, parent_id=0, parent_name=''):
                    result = []
                    for area in areas:
                        item = {
                            "id": area.get("id"),
                            "name": area.get("name"),
                            "parent_id": parent_id,
                            "parent_name": parent_name if parent_name else area.get("name", ""),
                            "children": []
                        }
                        children = area.get("list", [])
                        if children:
                            item["children"] = flatten(children, area.get("id"), area.get("name"))
                        result.append(item)
                    return result
                self.areas = flatten(data)
                self._search_cache.clear()  # 清空旧缓存
                with open(self.area_file, 'w', encoding='utf-8') as f:
                    json.dump(self.areas, f, ensure_ascii=False, indent=2)
                logger.info(f" 成功获取并保存 {len(self.areas)} 个分区")
                return True
        logger.error(" 获取分区列表失败")
        return False


# ==================== 拼音首字母工具 ====================
try:
    from pypinyin import lazy_pinyin, Style
    def _to_pinyin_initials(text: str) -> str:
        """将中文文本转换为拼音首字母（如 '王者荣耀' → 'wzry'）"""
        return ''.join(lazy_pinyin(text, style=Style.FIRST_LETTER))
except ImportError:
    def _to_pinyin_initials(text: str) -> str:
        return ''

# ==================== LiveState（直播状态持久化） ====================
class LiveState:
    """直播状态持久化管理器"""

    def __init__(self, state_file: str = "live_state.json"):
        self.state_file = Path(state_file)
        self.is_streaming = False
        self.current_zone = ""
        self.elapsed_seconds = 0
        self.room_id = 0
        self.start_time: Optional[str] = None  # ISO 格式
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 检查是否跨日，如果是则丢弃旧状态
                last_update = data.get('last_update', '')
                if last_update:
                    try:
                        last_date = datetime.fromisoformat(last_update).date()
                        if last_date != date.today():
                            logger.info(f"状态文件来自 {last_date}（非今天），已弃置")
                            self.save()
                            return
                    except ValueError:
                        pass
                self.is_streaming = data.get('is_streaming', False)
                self.current_zone = data.get('current_zone', '')
                self.elapsed_seconds = data.get('elapsed_seconds', 0)
                self.room_id = data.get('room_id', 0)
                self.duration_seconds = data.get('duration_seconds', 0)
                self.start_time = data.get('start_time')
                if self.is_streaming:
                    logger.debug(f"恢复直播状态：分区={self.current_zone}, 已播={self.elapsed_seconds // 60}分钟")
            except Exception as e:
                logger.debug(f" 加载状态文件失败：{e}")

    def save(self):
        data = {
            'is_streaming': self.is_streaming,
            'current_zone': self.current_zone or '',
            'elapsed_seconds': self.elapsed_seconds,
            'room_id': self.room_id,
            'duration_seconds': getattr(self, 'duration_seconds', 0),
            'start_time': self.start_time or datetime.now().isoformat(),
            'last_update': datetime.now().isoformat()
        }
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f" 保存状态失败：{e}")

    def start_streaming(self, zone_name: str, room_id: int):
        self.is_streaming = True
        self.current_zone = zone_name
        self.elapsed_seconds = 0
        self.room_id = room_id
        self.start_time = datetime.now().isoformat()
        self.save()
        logger.info(f" 直播状态已保存：开始 {zone_name}")

    def start_streaming_with_duration(self, zone_name: str, room_id: int, duration_seconds: int, initial_elapsed: int = 0):
        """带时长信息的开始直播"""
        self.is_streaming = True
        self.current_zone = zone_name
        self.elapsed_seconds = initial_elapsed
        self.room_id = room_id
        self.start_time = datetime.now().isoformat()
        self.duration_seconds = duration_seconds
        self.save()

    def update_progress(self, elapsed: int):
        self.elapsed_seconds = elapsed
        self.save()

    def stop_streaming(self):
        self.is_streaming = False
        self.current_zone = ''
        self.elapsed_seconds = 0
        self.start_time = None
        self.save()
        logger.info(" 直播状态已重置")

    def reload(self):
        """重新从文件加载状态（方便手动编辑 live_state.json 后刷新）"""
        # 先保存当前状态再加载（保留未保存的更改）
        self._load()
        logger.debug(f"已重新加载状态文件：is_streaming={self.is_streaming}, zone={self.current_zone}")

    def update_state(self, **kwargs):
        """更新状态字段并保存（方便修改待恢复任务的参数）"""
        allowed = {'is_streaming', 'current_zone', 'elapsed_seconds', 'room_id', 'duration_seconds'}
        changed = False
        for key, val in kwargs.items():
            if key in allowed and hasattr(self, key):
                current = getattr(self, key)
                if current != val:
                    setattr(self, key, val)
                    changed = True
                    logger.info(f" 状态更新：{key} = {val}")
        if changed:
            self.save()
        return changed

    def complete_task(self):
        """完成任务时的状态重置"""
        self.stop_streaming()

    def is_cross_day(self) -> bool:
        if not self.start_time:
            return False
        try:
            start = datetime.fromisoformat(self.start_time)
            return start.date() != date.today()
        except:
            return False

    def get_status(self) -> dict:
        return {
            'is_streaming': self.is_streaming,
            'current_zone': self.current_zone,
            'elapsed_seconds': self.elapsed_seconds,
            'room_id': self.room_id,
            'start_time': self.start_time
        }


# ==================== LiveController（核心控制器重构） ====================
class LiveController:
    """直播控制器（重构版，去除 GUI 依赖，面向 API）"""

    def __init__(
        self,
        task_manager: 'TaskManager' = None,
        state_file: str = "live_state.json",
        area_file: str = "bili_areas_full.json"
    ):
        self.task_manager = task_manager
        self.api = BilibiliApi()
        self.area_loader = AreaLoader(area_file)
        self.video_finder = VideoPathFinder()
        self.state = LiveState(state_file)
        self._ensure_areas_loaded()  # 启动时自动拉取分区

        self.current_room_id: Optional[int] = None
        self.is_streaming = False
        self.stream_start_time: Optional[datetime] = None
        self.current_instruction: Optional[LiveInstruction] = None
        self.stop_monitor = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None

        # 视频进程
        self.video_process: Optional[subprocess.Popen] = None

        # 重连相关
        self.reconnect_attempts = 0
        self.max_reconnect = MAX_RECONNECT_ATTEMPTS
        self._retry_cooldown_until: Optional[datetime] = None  # 冷却结束时间
        self._retry_window_start: Optional[datetime] = None    # 当前重试窗口起点

        # 流模式：'task'（任务模式）| 'manual'（手动模式）| None
        self._stream_mode: Optional[str] = None

        # FFmpeg 推流状态
        self._ffmpeg_loop_thread: Optional[threading.Thread] = None
        self._ffmpeg_stop_event = threading.Event()  # 独立信号：停止 FFmpeg 循环
        self.ffmpeg_current_video: str = ''   # 当前正在推流的视频文件名

        # 人脸验证状态
        self._pending_face_verify: bool = False
        self._face_verify_url: str = ''

        # 后端事件日志（供前端轮询展示）
        self._backend_events: list = []

        # 启动时自动恢复登录态（从持久化 cookies）
        if self.api.is_logged_in():
            self.login()

        # 注意：不自动恢复直播状态，仅保留 state 数据供前端显示恢复提示

    def _ensure_areas_loaded(self):
        """确保分区数据已加载（文件不存在或为空时自动从 API 拉取）"""
        if not self.area_loader.areas:
            logger.info(" 分区数据为空，正在从 B 站 API 在线拉取...")
            success = self.area_loader.fetch_and_save_areas(self.api)
            if success:
                logger.info(f" 在线拉取成功，共 {len(self.area_loader.areas)} 个分区")
            else:
                logger.warning(" 在线拉取分区失败，部分功能不可用")

    def login(self) -> bool:
        """自动登录（优先 cookies，否则返回需要扫码的状态）"""
        if self.api.validate_login():
            success, user_info = self.api.get_user_info()
            if success and user_info.get('code') == 0:
                data = user_info.get('data', {})
                uid = data.get('mid')
                if uid:
                    # 通过 UID 获取真实 room_id
                    ok, room_resp = self.api.get_room_id_by_uid(uid)
                    if ok:
                        self.current_room_id = room_resp['data']['room_id']
                        logger.info(f" 自动登录成功 | {data.get('name', '')} (UID:{uid}, Room:{self.current_room_id})")
                        return True
                    else:
                        # 降级：使用 UID 作为 room_id
                        self.current_room_id = uid
                        logger.warning(f" 无法获取直播间 ID，使用 UID 替代：{uid}")
                        return True

        logger.info(" Cookies 无效或已过期，需要扫码登录")
        return False

    def get_qrcode_data(self) -> Optional[dict]:
        """获取二维码数据（供前端展示）"""
        success, resp = self.api.get_qrcode()
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            return {
                'qrcode_url': data.get('url'),
                'qrcode_key': data.get('qrcode_key')
            }
        return None

    def poll_login_status(self, qrcode_key: str) -> dict:
        """轮询扫码登录状态"""
        success, resp = self.api.poll_qrcode(qrcode_key)
        if success and resp.get('code') == 0:
            data = resp.get('data', {})
            status = data.get('code', -1)  # 0=扫码中，86101=未扫码，86038=二维码过期
            if 'url' in data and data['url']:
                # 提取 cookies
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(data['url'])
                query_params = parse_qs(parsed.query)
                cookies = {}
                for key in ['SESSDATA', 'bili_jct', 'DedeUserID']:
                    if key in query_params:
                        cookies[key] = query_params[key][0]
                if cookies:
                    self.api.update_cookies(cookies)
                    # 获取用户信息
                    suc, user_info = self.api.get_user_info()
                    if suc and user_info.get('code') == 0:
                        info = user_info.get('data', {})
                        uid = info.get('mid')
                        if uid:
                            # 获取真实 room_id
                            ok, room_resp = self.api.get_room_id_by_uid(uid)
                            if ok:
                                self.current_room_id = room_resp['data']['room_id']
                            else:
                                self.current_room_id = uid
                            return {
                                'logged_in': True,
                                'user_info': {
                                    'uid': uid,
                                    'uname': info.get('name', ''),
                                    'face': info.get('face', ''),
                                    'level': info.get('level', 0)
                                }
                            }
                return {'logged_in': False, 'message': '提取 cookies 失败'}
            elif status == 0:
                return {'logged_in': False, 'scanning': True}
            elif status == 86038:
                return {'logged_in': False, 'expired': True}
            else:
                return {'logged_in': False, 'scanning': False}
        else:
            msg = resp.get('msg', '查询失败')
            if '二维码尚未生成' in msg:
                return {'logged_in': False, 'scanning': False}
            return {'logged_in': False, 'message': msg}

    def get_live_status_api(self) -> dict:
        """获取当前直播状态（供 API 响应）"""
        payload = self.state.get_status()
        payload['is_streaming'] = self.is_streaming
        payload['duration_seconds'] = getattr(self.state, 'duration_seconds', 0)
        payload['stream_mode'] = self._stream_mode or ''
        # 人脸验证待处理（自动切任务触发时前端轮询感知）
        payload['pending_face_verify'] = self._pending_face_verify
        payload['face_verify_url'] = self._face_verify_url or ''
        # FFmpeg 推流状态
        ffmpeg_active = (self.video_process is not None and self.video_process.poll() is None)
        payload['ffmpeg_active'] = ffmpeg_active
        payload['ffmpeg_current_video'] = self.ffmpeg_current_video or ''
        # 后端事件日志（最近50条）
        payload['backend_events'] = self._backend_events[-50:] if hasattr(self, '_backend_events') else []
        if self.stream_start_time and self.current_instruction:
            elapsed = (datetime.now() - self.stream_start_time).total_seconds()
            payload['elapsed_seconds'] = int(elapsed)
            payload['remaining_seconds'] = max(0, self.current_instruction.duration_seconds - int(elapsed))
            payload['current_zone'] = self.current_instruction.zone_name
            # 优先使用指令中的时长
            payload['duration_seconds'] = self.current_instruction.duration_seconds
        return payload

    def _pre_start_cleanup(self):
        """开播前清理：检测 FFmpeg 残留 + 直播间是否已在播，先下播再继续"""
        # 1. 清理所有残留 FFmpeg 进程（异常退出时可能未清理）
        if self.video_process or self._ffmpeg_loop_thread:
            logger.info(" 检测到残留 FFmpeg 状态，清理中...")
        self._ffmpeg_stop_event.set()
        self._kill_ffmpeg()
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)
        self._ffmpeg_stop_event.clear()
        self._kill_all_ffmpeg()
        self.video_process = None
        self.ffmpeg_current_video = ''

        # 2. 检查 B站直播间是否已在播（异常退出时可能未下播）
        if self.current_room_id:
            try:
                ok, resp = self.api.get_live_status(self.current_room_id)
                if ok:
                    live_status = resp.get('data', {}).get('live_status', 0)
                    if live_status == 1:
                        logger.info(" 检测到直播间仍在播，先调用下播 API...")
                        csrf = self.api.get_csrf()
                        if csrf:
                            self.api.stop_live(self.current_room_id, csrf)
                            logger.info(" 已调用下播 API")
            except Exception as e:
                logger.debug(f" 检查直播间状态异常（忽略）：{e}")

        # 3. 重置内部状态
        self.is_streaming = False
        self.stream_start_time = None
        self.stop_monitor.clear()
        logger.info(" 开播前清理完成")

    def start_streaming(self, instruction: LiveInstruction, video_path: str, is_task_mode: bool = True) -> bool:
        """开始直播（核心逻辑）
        is_task_mode=True: 任务模式，保存 live_state、可继承已播时长
        is_task_mode=False: 手动模式，不触碰 live_state、已播时长始终=0、时长=0表示不限时
        """
        self._stream_mode = 'task' if is_task_mode else 'manual'
        dur_label = '不限时' if (not is_task_mode and instruction.duration_seconds == 0) else f"{instruction.duration_seconds // 60}分钟"
        logger.info("=" * 70)
        logger.info(f"【开播】模式：{self._stream_mode} | 分区：{instruction.zone_name} | 时长：{dur_label}")
        logger.info(f" 视频：{Path(video_path).name}")
        logger.info("=" * 70)

        if not self.current_room_id:
            logger.error(" 未登录，请先调用 login()")
            return False

        # 开播前清理：检测残留 FFmpeg 和直播间状态，先下播再开始
        self._pre_start_cleanup()

        if not self.switch_partition(instruction.zone_name):
            logger.error(" 切换分区失败，终止开播")
            return False

        logger.info("开始直播...")
        csrf = self.api.get_csrf()
        if not csrf:
            logger.error(" 未找到 csrf token")
            return False

        area_id = self.area_loader.get_area_id(instruction.zone_name, auto_update=False)
        # 提前保存指令，确保人脸验证失败时 _retry_after_face_verify 可用
        self.current_instruction = instruction

        success, resp = self.api.start_live(self.current_room_id, area_id, csrf)
        if not success and resp.get('code') in (60024, 60043):
            # 人脸验证：返回固定 URL 给前端弹窗
            code = resp.get('code')
            logger.warning(f"️ 检测到需要人脸验证 (code={code})")
            uid = self.api.cookies.get('DedeUserID', '')
            verify_url = f"https://www.bilibili.com/blackboard/live/face-auth-middle.html?source_event=400&mid={uid}"
            self._pending_face_verify = True
            self._face_verify_url = verify_url
            logger.info(f"人脸验证 URL：{verify_url}")
            self._push_backend_event('验证', 'warning', f'开播需要人脸验证 (code={code})')
            return False  # API 层检查 _pending_face_verify 返回给前端

        if not success:
            logger.error(f" 开播失败：{resp.get('msg', '未知错误')}")
            self._push_backend_event('错误', 'danger', f"开播失败：{resp.get('msg', '未知错误')}")
            self.current_instruction = None  # 非人脸验证失败，清除指令
            return False

        logger.info(" 直播已开始")

        # 从 start_live 返回中提取并本地缓存推流码（同账号推流码恒定不变）
        self._extract_and_cache_rtmp(resp)

        self.is_streaming = True
        # 已播时长：任务模式可继承，手动模式始终为 0
        if is_task_mode:
            saved_elapsed = self.state.elapsed_seconds if self.state.is_streaming else 0
        else:
            saved_elapsed = 0
        self.stream_start_time = datetime.now() - timedelta(seconds=saved_elapsed)
        self.stop_monitor.clear()
        # 任务模式保存 state，手动模式不触碰
        if is_task_mode:
            self.state.start_streaming_with_duration(
                instruction.zone_name, self.current_room_id, instruction.duration_seconds,
                initial_elapsed=saved_elapsed)

        # 推送开播事件（触发邮件/Server酱通知）
        mode_label = '任务模式' if is_task_mode else '手动模式'
        saved_label = f'（恢复，已播{saved_elapsed // 60}分钟）' if saved_elapsed > 0 else ''
        self._push_backend_event('开播', 'success', f'{mode_label}开播{saved_label} - {instruction.zone_name}，时长{dur_label}')

        # 启动视频/FFmpeg（在 start_live 之后，因为 FFmpeg 需要推流码）
        stream_mode, auto_open = self._get_stream_settings()
        if stream_mode == 'ffmpeg':
            if not self._start_ffmpeg_stream():
                logger.error(" FFmpeg 推流启动失败")
                return False
        elif auto_open and video_path:
            if not self.play_video(video_path):
                logger.warning(" 视频播放失败，但直播已开始")

        # 启动监控线程
        self.monitor_thread = threading.Thread(
            target=self._monitor_streaming,
            name="StreamMonitor",
            daemon=True
        )
        self.monitor_thread.start()

        logger.info(f"  直播时长：{dur_label}")
        return True

    def _get_stream_settings(self) -> Tuple[str, bool]:
        """读取推流设置，返回 (stream_mode, auto_open_video)"""
        try:
            from app.api.settings import load_settings
            s = load_settings()
            return s.stream_mode, s.auto_open_video
        except Exception:
            return "manual", True

    def switch_partition(self, zone_name: str) -> bool:
        """切换直播分区（调用 B站 API）"""
        area_id = self.area_loader.get_area_id(zone_name, auto_update=True)
        if not area_id:
            logger.error(f" 无法获取分区 '{zone_name}' 的 ID")
            return False
        csrf = self.api.get_csrf()
        if not csrf:
            logger.error(" 未找到 csrf token")
            return False
        logger.info(f" 切换分区：{zone_name} (area_id={area_id})")
        success, resp = self.api.update_area(self.current_room_id, area_id, csrf)
        if not success:
            logger.error(f" 切换分区失败：{resp.get('msg', '未知错误')}")
            return False
        logger.info(f" 分区切换成功：{zone_name}")
        return True

    def play_video(self, video_path: str) -> bool:
        """播放视频（使用系统默认软件打开）"""
        try:
            if sys.platform == 'win32':
                os.startfile(video_path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', video_path])
            else:
                subprocess.Popen(['xdg-open', video_path])
            logger.info(f"▶️ 已用默认程序打开视频：{Path(video_path).name}")
            return True
        except Exception as e:
            logger.error(f" 打开视频失败：{e}")
            return False

    def _extract_and_cache_rtmp(self, resp: dict):
        """从 start_live 响应中提取推流码并本地缓存（同账号推流码恒定不变）"""
        try:
            data = resp.get('data', {})
            rtmp_data = data.get('rtmp', {})
            addr = rtmp_data.get('addr', '')
            code = rtmp_data.get('code', '')
            if addr and code:
                # 读取现有缓存
                cache = self._load_rtmp_cache()
                cache[str(self.current_room_id)] = {'rtmp_addr': addr, 'rtmp_code': code}
                self._save_rtmp_cache(cache)
                logger.info(f" 推流码已缓存：{addr[:30]}...")
            else:
                logger.warning(" start_live 返回中未找到 rtmp 数据")
        except Exception as e:
            logger.debug(f" 缓存推流码失败：{e}")

    def _load_rtmp_cache(self) -> dict:
        """加载本地推流码缓存"""
        cache_file = Path("rtmp_cache.json")
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_rtmp_cache(self, cache: dict):
        """保存推流码缓存到本地"""
        try:
            with open("rtmp_cache.json", 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f" 保存推流码缓存失败：{e}")

    def _get_cached_push_url(self) -> Optional[str]:
        """从本地缓存获取推流地址，归一化 URL（消除 /? 等异常格式）"""
        if not self.current_room_id:
            return None
        cache = self._load_rtmp_cache()
        entry = cache.get(str(self.current_room_id))
        if entry and entry.get('rtmp_addr') and entry.get('rtmp_code'):
            addr = entry['rtmp_addr'].rstrip('/')
            code = entry['rtmp_code']
            # 归一化：确保 / 和 ? 只有一个出现
            if code.startswith('?'):
                return f"{addr}{code}"       # rtmp://host/app?key=...
            else:
                return f"{addr}/{code}"      # rtmp://host/app/key

    def _start_ffmpeg_stream(self) -> bool:
        """启动 FFmpeg 推流循环线程（使用缓存的推流码，播放完一个视频自动换下一个）"""
        # 先停止旧循环（防止双线程同时运行，导致 poll() 竞态和文件冲突）
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            logger.info(" 等待旧的 FFmpeg 循环线程退出...")
            self._ffmpeg_stop_event.set()
            self._ffmpeg_loop_thread.join(timeout=5.0)
            self._ffmpeg_stop_event.clear()
        # 杀掉可能残留的 ffmpeg 进程（上次异常退出遗留）
        self._kill_all_ffmpeg()
        self.video_process = None
        # 从缓存获取推流地址
        push_url = self._get_cached_push_url()
        if not push_url:
            # 缓存没有则尝试 API 获取
            success, data = self.api.get_push_url(self.current_room_id) if self.current_room_id else (False, {})
            if success and data.get('push_url'):
                push_url = data['push_url']
                logger.info(f" 从 API 获取推流地址：{data.get('rtmp_addr', '')[:30]}...")
            else:
                logger.error(" 无法获取推流地址：缓存和 API 均无数据")
                return False
        else:
            logger.info(f" 使用缓存推流地址：{push_url[:50]}...")

        # 读取 ffmpeg 路径
        ffmpeg_exe = 'ffmpeg'
        try:
            from app.api.settings import load_settings
            s = load_settings()
            if s.ffmpeg_path:
                ffmpeg_exe = s.ffmpeg_path
        except Exception:
            pass

        zone_name = self.current_instruction.zone_name if self.current_instruction else ''

        self._ffmpeg_loop_thread = threading.Thread(
            target=self._ffmpeg_loop,
            args=(zone_name, push_url, ffmpeg_exe),
            name="FFmpegLoop",
            daemon=True
        )
        self._ffmpeg_loop_thread.start()
        logger.info(f" FFmpeg 推流循环已启动")
        return True

    def _check_concat_compatible(self, video_list: list) -> bool:
        """检查视频列表是否能用 -c copy 拼接（编码参数必须完全一致）。
        返回 True 表示兼容，False 表示存在不一致。"""
        if len(video_list) <= 1:
            return True
        import json as _json
        ref_key = None
        for v in video_list[:8]:  # 检查前8个足够了
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                     '-show_streams', '-select_streams', 'v:0', v],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    logger.debug(f" ffprobe 失败 ({v})：{r.stderr[:100]}")
                    continue
                info = _json.loads(r.stdout)
                streams = info.get('streams', [])
                if not streams:
                    continue
                s = streams[0]
                key = (s.get('codec_name'), s.get('width'), s.get('height'),
                       s.get('pix_fmt'), s.get('profile'))  # level 不影响 -c copy
                if ref_key is None:
                    ref_key = key
                elif key != ref_key:
                    logger.info(f" 编码不一致：{ref_key} ≠ {key} → {Path(v).name}")
                    return False
            except Exception as e:
                logger.debug(f" ffprobe 异常 ({v})：{e}")
                continue
        return True

    def _filter_compatible_videos(self, video_list: list) -> list:
        """过滤出与第一个视频编码兼容的视频列表"""
        if len(video_list) <= 1:
            return list(video_list)
        import json as _json
        ref_key = None
        compatible = []
        for v in video_list:
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                     '-show_streams', '-select_streams', 'v:0', v],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    compatible.append(v)  # 无法判断，保留
                    continue
                info = _json.loads(r.stdout)
                streams = info.get('streams', [])
                if not streams:
                    compatible.append(v)
                    continue
                s = streams[0]
                key = (s.get('codec_name'), s.get('width'), s.get('height'),
                       s.get('pix_fmt'), s.get('profile'))  # level 不影响 -c copy
                if ref_key is None:
                    ref_key = key
                    compatible.append(v)
                elif key == ref_key:
                    compatible.append(v)
                else:
                    logger.info(f" 跳过不兼容视频：{Path(v).name} ({s.get('width')}x{s.get('height')} {s.get('codec_name')})")
            except Exception:
                compatible.append(v)  # 无法判断，保留
        return compatible

    def _ffmpeg_loop(self, zone_name: str, push_url: str, ffmpeg_exe: str):
        """FFmpeg 单进程推流：用 concat 播放列表串联所有视频。
        - 每轮 concat 结束后从 API 刷新推流 URL（B站 stream key 一次性）
        - 用时间戳文件名避免多线程/残留进程文件冲突
        - 用 _ffmpeg_stop_event 可被外部中断"""
        logger.info(f" FFmpeg 单进程推流开始 | 分区：{zone_name}")
        rapid_fails = 0
        backoff = 5
        current_push_url = push_url
        # 时间戳唯一文件名，防止多线程 / 残留进程抢同一文件
        import uuid as _uuid
        concat_file = Path(f"_ffmpeg_concat_{_uuid.uuid4().hex[:8]}.txt")

        def _refresh_push_url():
            """尝试从缓存/API 刷新推流地址，失败返回 None"""
            url = self._get_cached_push_url()
            if url:
                return url
            if self.current_room_id:
                ok, data = self.api.get_push_url(self.current_room_id)
                if ok and data.get('push_url'):
                    return data['push_url']
            return None

        try:
            while not self._ffmpeg_stop_event.is_set() and not self.stop_monitor.is_set() and self.is_streaming:
                video_list = self._list_zone_videos(zone_name)
                if not video_list:
                    logger.warning(f" FFmpeg 找不到视频文件，{backoff}秒后重试...")
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        break
                    continue

                random.shuffle(video_list)

                # 读取重编码设置
                reencode_enabled = True
                try:
                    from app.api.settings import load_settings
                    reencode_enabled = load_settings().ffmpeg_reencode
                except Exception:
                    pass

                # 检查编码兼容性
                use_reencode = False
                if not self._check_concat_compatible(video_list):
                    if reencode_enabled:
                        logger.info(" 视频编码不一致，使用重编码模式")
                        use_reencode = True
                    else:
                        logger.info(" 视频编码不一致，过滤只保留兼容视频")
                        video_list = self._filter_compatible_videos(video_list)
                        if not video_list:
                            logger.warning(" 过滤后无兼容视频，跳过本轮")
                            if self._ffmpeg_stop_event.wait(timeout=10):
                                break
                            continue

                try:
                    with open(concat_file, 'w', encoding='utf-8') as f:
                        for v in video_list:
                            f.write(f"file '{v}'\n")
                except Exception as e:
                    logger.error(f" 写入 concat 文件失败：{e}")
                    time.sleep(2)
                    continue

                if use_reencode:
                    cmd = f'{ffmpeg_exe} -re -f concat -safe 0 -i "{concat_file}" -c:v libx264 -preset veryfast -b:v 6000k -maxrate 8000k -bufsize 12000k -pix_fmt yuv420p -c:a aac -b:a 128k -f flv "{current_push_url}" -flvflags no_duration_filesize'
                    logger.info(f" 生成 concat 列表：{len(video_list)} 个视频（重编码）")
                else:
                    cmd = f'{ffmpeg_exe} -re -f concat -safe 0 -i "{concat_file}" -c copy -f flv "{current_push_url}" -flvflags no_duration_filesize'
                    logger.info(f" 生成 concat 列表：{len(video_list)} 个视频")

                ffmpeg_log = Path("ffmpeg.log")
                log_fp = open(str(ffmpeg_log), 'a', encoding='utf-8', errors='replace')
                log_fp.write(f"\n=== {datetime.now().isoformat()} | concat {len(video_list)} files ===\n")
                log_fp.flush()

                try:
                    t_start = time.time()
                    proc = subprocess.Popen(
                        cmd, shell=True,
                        stdout=log_fp, stderr=subprocess.STDOUT,
                    )
                    self.video_process = proc
                    self.ffmpeg_current_video = f"concat({len(video_list)}个)"
                    logger.info(f" FFmpeg concat 进程已启动 (PID={proc.pid})")

                    # 监控进程，使用本地变量 proc 避免与 _kill_ffmpeg 竞态
                    while proc.poll() is None:
                        if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set() or not self.is_streaming:
                            self._kill_ffmpeg()
                            log_fp.close()
                            logger.info(" FFmpeg 被中断")
                            return
                        time.sleep(1)

                    exit_code = proc.returncode
                    elapsed = time.time() - t_start
                    self.video_process = None
                    log_fp.close()

                    # 如果是被外部主动终止的（任务完成/停播），静默退出
                    if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set() or not self.is_streaming:
                        logger.info("FFmpeg 已随直播停止而终止")
                        return

                    # 长会话（> 30 秒）：非零退出码说明是被掐断，不能算正常完成
                    if elapsed > 30:
                        if exit_code == 0:
                            logger.info(f" concat 播放完毕 ({elapsed:.0f}s)，重新洗牌...")
                            rapid_fails = 0
                            backoff = 5
                            continue
                        else:
                            # 长会话但异常退出（如 B站 RTMP 超时掐断 / 编码切换失败）
                            logger.warning(f" concat 异常退出 ({elapsed:.0f}s, exit={exit_code})，可能是编码不一致或服务端掐断")
                            self._push_backend_event('推流', 'warning', f'concat 异常退出 ({elapsed:.0f}s, exit={exit_code})，重试...')
                            # 不重置 rapid_fails=0，但不退出；下次循环会重新洗牌+检查编码
                            rapid_fails = max(0, rapid_fails - 1)
                            backoff = 5
                            if self._ffmpeg_stop_event.wait(timeout=backoff):
                                return
                            continue

                    # 快速失败（≤ 30 秒）→ 判断是否 WSAECONNABORTED（-10053 = 4294957243）
                    rapid_fails += 1
                    is_aborted = (exit_code == 4294957243 or
                                  (exit_code > 2**31 and exit_code - 2**32 == -10053))
                    if is_aborted:
                        logger.warning(f" FFmpeg 连接被拒 (WSAECONNABORTED)，退避重试")
                        # 连接被拒通常是 B站服务端临时问题，退避后重试即可
                        # 不再无谓刷新 URL（推流码持久绑定，非一次性）
                        rapid_fails = max(0, rapid_fails - 1)  # 不因此快速累计
                        backoff = min(10 * (2 ** rapid_fails), 120) + random.uniform(0, 5)
                        logger.info(f" 退避 {backoff:.1f} 秒后重试...")
                        if self._ffmpeg_stop_event.wait(timeout=backoff):
                            return
                        continue

                    logger.warning(f" FFmpeg 快速退出 ({elapsed:.1f}s, exit={exit_code})，第 {rapid_fails} 次")
                    self._push_backend_event('推流', 'warning', f'FFmpeg 快速退出 (exit={exit_code})，第{rapid_fails}次重试')
                    if rapid_fails >= 5:
                        # 不退出了，改为长间隔重试（可能是 B站临时抽风）
                        logger.warning(f" FFmpeg 连续 {rapid_fails} 次快速失败，切换长间隔重试模式（每5分钟一次）")
                        self._push_backend_event('推流', 'warning', f'FFmpeg 连续{rapid_fails}次失败，切换长间隔重试（每5分钟）')
                        backoff = 300 + random.uniform(0, 30)  # 5分钟 + 随机抖动
                    else:
                        backoff = min(5 * (2 ** (rapid_fails - 1)), 60) + random.uniform(0, 3)
                    logger.info(f" 退避 {backoff:.1f} 秒后重试...")
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        return

                except FileNotFoundError:
                    log_fp.close()
                    logger.error(f" 未找到 ffmpeg：{ffmpeg_exe}")
                    return
                except Exception as e:
                    log_fp.close()
                    logger.error(f" FFmpeg 异常：{e}")
                    if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set():
                        return
                    rapid_fails += 1
                    if rapid_fails >= 5:
                        return
                    if self._ffmpeg_stop_event.wait(timeout=backoff):
                        return
        finally:
            self.video_process = None
            self.ffmpeg_current_video = ''
            try:
                if concat_file.exists():
                    concat_file.unlink(missing_ok=True)
            except (PermissionError, OSError):
                try:
                    time.sleep(1)
                    concat_file.unlink(missing_ok=True)
                except Exception:
                    pass

        logger.info(" FFmpeg 循环线程退出")

    def _list_zone_videos(self, zone_name: str) -> List[str]:
        """列出分区文件夹下所有视频文件的绝对路径"""
        zone_names = [zone_name, zone_name.replace("区", ""), zone_name.lower()]
        for name in zone_names:
            zone_folder = VIDEO_BASE_PATH / name
            if zone_folder.exists() and zone_folder.is_dir():
                files = []
                for ext in ('*.mp4', '*.mkv', '*.flv', '*.avi', '*.mov', '*.wmv'):
                    for f in zone_folder.glob(ext):
                        files.append(str(f.resolve()))
                if files:
                    return files
        # fallback: default 文件夹
        if DEFAULT_VIDEO_FOLDER.exists():
            files = []
            for ext in ('*.mp4', '*.mkv', '*.flv', '*.avi', '*.mov', '*.wmv'):
                for f in DEFAULT_VIDEO_FOLDER.glob(ext):
                    files.append(str(f.resolve()))
            return files
        return []

    def _kill_ffmpeg(self):
        """强制终止当前 FFmpeg 进程"""
        if not self.video_process:
            return
        try:
            pid = self.video_process.pid
            if pid and sys.platform == 'win32':
                subprocess.run(
                    f'taskkill /F /T /PID {pid}',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10
                )
            else:
                self.video_process.terminate()
                try:
                    self.video_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.video_process.kill()
            self.video_process = None
            logger.info(" FFmpeg 进程已终止")
        except Exception as e:
            logger.debug(f" 终止 FFmpeg 异常：{e}")
            self.video_process = None

    def _kill_all_ffmpeg(self):
        """杀掉所有残留的 ffmpeg 进程（用于开播前清理）"""
        self._kill_ffmpeg()
        try:
            if sys.platform == 'win32':
                subprocess.run(
                    'taskkill /F /IM ffmpeg.exe 2>nul',
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10
                )
                logger.info(" 已清理所有残留 ffmpeg 进程")
        except Exception as e:
            logger.debug(f" 清理残留 ffmpeg 异常：{e}")

    def confirm_face_verify(self) -> bool:
        """清除人脸验证待处理状态，供前端确认后重试开播"""
        self._pending_face_verify = False
        self._face_verify_url = ''
        logger.info(" 人脸验证状态已清除，可重试开播")
        return True

    def _push_backend_event(self, tag: str, event_type: str, message: str):
        """向后端事件日志推送一条记录（供前端轮询展示），同时触发邮件"""
        self._backend_events.append({
            'tag': tag,
            'type': event_type,  # success|danger|warning|info
            'message': message,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        if len(self._backend_events) > 100:
            self._backend_events = self._backend_events[-50:]
        # 触发邮件通知
        self._try_email_notify(tag, event_type, message)

    def _try_email_notify(self, tag: str, event_type: str, message: str):
        """根据事件类型决定是否发送邮件"""
        try:
            from app.dependencies import get_email_sender
            sender = get_email_sender()
            if not sender:
                return
            zone = self.current_instruction.zone_name if self.current_instruction else ''
            elapsed = int((datetime.now() - self.stream_start_time).total_seconds()) if self.stream_start_time else 0
            elapsed_label = f"{elapsed // 60}分钟" if elapsed > 0 else ""

            if tag in ('开播',):
                dur = self.current_instruction.duration_seconds if self.current_instruction else 0
                dur_label = f"{dur // 60}分钟" if dur > 0 else '不限时'
                sender.send_task_start(zone, dur_label)
            elif tag in ('完成',) and '任务完成' in message:
                sender.send_task_complete(zone, elapsed_label)
            elif tag in ('重连',) and '重试次数耗尽' in message:
                try:
                    from app.api.settings import load_settings
                    s = load_settings()
                    sender.send_reconnect_exhausted(s.live_retry_cooldown_minutes)
                except Exception:
                    sender.send_reconnect_exhausted(60)
            elif tag in ('重连',) and '尝试重连' in message:
                import re
                m = re.search(r'(\d+)/(\d+)', message)
                if m:
                    sender.send_reconnect_start(int(m.group(1)), int(m.group(2)))
            elif tag in ('验证',) and '人脸验证' in message:
                verify_url = self._face_verify_url or ''
                if verify_url:
                    sender.send_face_verify(verify_url)
                else:
                    sender.send_error('人脸验证', message)
            elif tag in ('错误', '异常') and event_type == 'danger':
                sender.send_error(tag, message)
        except Exception as e:
            logger.debug(f" 邮件通知异常：{e}")

    def _check_network_ok(self) -> bool:
        """快速检查自身网络是否正常（ping 百度）"""
        try:
            requests.get("https://www.baidu.com", timeout=5)
            return True
        except Exception:
            return False

    def _handle_live_anomaly_retry(self, max_retries: int, cooldown_minutes: int):
        """直播间状态异常的分层限次重试"""
        now = datetime.now()

        # 检查是否在冷却期
        if self._retry_cooldown_until and now < self._retry_cooldown_until:
            remaining = int((self._retry_cooldown_until - now).total_seconds() / 60)
            logger.debug(f" 重试冷却中，剩余 {remaining} 分钟")
            return

        # 新窗口：重置计数
        if self._retry_window_start is None or (now - self._retry_window_start).total_seconds() > 3600:
            self._retry_window_start = now
            self.reconnect_attempts = 0
            self._retry_cooldown_until = None
            logger.info(" 新重试窗口开始（1h）")

        if self.reconnect_attempts < max_retries:
            self.reconnect_attempts += 1
            logger.info(f" 直播间异常，尝试重连 ({self.reconnect_attempts}/{max_retries})...")
            self._push_backend_event('重连', 'warning', f'直播间异常，尝试重连 ({self.reconnect_attempts}/{max_retries})')
            time.sleep(min(2 ** self.reconnect_attempts, 30))
            self._retry_start_live()
        else:
            self._retry_cooldown_until = now + timedelta(minutes=cooldown_minutes)
            logger.warning(f" 重试次数耗尽，冷却 {cooldown_minutes} 分钟至 {self._retry_cooldown_until.strftime('%H:%M:%S')}")
            self._push_backend_event('重连', 'danger', f'重试次数耗尽，冷却 {cooldown_minutes} 分钟')

    def _retry_start_live(self):
        """内部重试开播：重开 B站 房间 + FFmpeg 模式则重启推流"""
        if not self.current_room_id or not self.current_instruction:
            return
        csrf = self.api.get_csrf()
        if not csrf:
            return
        area_id = self.area_loader.get_area_id(self.current_instruction.zone_name, auto_update=False)
        success, resp = self.api.start_live(self.current_room_id, area_id, csrf)
        if success:
            logger.info(" 重连成功，直播间已重新开启")
            self._extract_and_cache_rtmp(resp)  # 更新推流码缓存
            self.reconnect_attempts = 0
            self._retry_cooldown_until = None
            # FFmpeg 模式：重新启动推流循环
            if self._stream_mode != 'manual':
                stream_mode, _ = self._get_stream_settings()
                if stream_mode == 'ffmpeg':
                    logger.info(" 重启 FFmpeg 推流循环...")
                    self._start_ffmpeg_stream()
        else:
            code = resp.get('code', -1)
            if code in (60024, 60043):
                logger.warning(" 重连遇到人脸验证，停止重试等待人工确认")

    def _monitor_streaming(self):
        """监控直播状态 — 分层重试策略：
        - 自身网络不通 → 无限重试（等网络恢复）
        - 直播间状态异常 → 1h 内最多 max_reconnect 次，耗尽后冷却 live_retry_cooldown_minutes
        - 人脸验证(60024/60043) → 不重试，等人工确认
        """
        is_manual = (self._stream_mode == 'manual')
        logger.info(f" 启动直播监控线程（模式：{self._stream_mode}）")
        last_check_time = time.time()
        last_cross_day_check = time.time()
        should_stop = False
        cross_day_detected = False

        # 读取重试设置
        max_retries = self.max_reconnect
        cooldown_minutes = 60
        monitor_interval = MONITOR_INTERVAL
        try:
            from app.api.settings import load_settings
            s = load_settings()
            max_retries = s.max_reconnect
            cooldown_minutes = s.live_retry_cooldown_minutes
            if s.scan_interval_seconds and s.scan_interval_seconds >= 5:
                monitor_interval = s.scan_interval_seconds
        except Exception:
            pass

        while not self.stop_monitor.is_set() and self.is_streaming and not should_stop:
            now = time.time()

            # 跨日检测（仅任务模式，手动模式不受干扰）
            if not is_manual and now - last_cross_day_check >= CROSS_DAY_CHECK_INTERVAL:
                last_cross_day_check = now
                if self.state.is_cross_day():
                    logger.warning(" 检测到跨日！中断当前直播任务..")
                    self._push_backend_event('跨日', 'warning', '检测到跨日，中断当前任务，等待每日重置后执行新任务')
                    cross_day_detected = True
                    should_stop = True
                    break

            if now - last_check_time >= monitor_interval:
                last_check_time = now
                if self.stream_start_time and self.current_instruction:
                    elapsed = (datetime.now() - self.stream_start_time).total_seconds()
                    if not is_manual:
                        self.state.update_progress(int(elapsed))
                    dur = self.current_instruction.duration_seconds
                    if dur > 0 and elapsed >= dur:
                        logger.info(f" 直播时长已到 ({elapsed / 60:.1f}分钟)")
                        should_stop = True
                        break

                # === 分层重试逻辑 ===
                if self.current_room_id:
                    success, status_resp = self.api.get_live_status(self.current_room_id)
                    live_ok = success and status_resp.get('data', {}).get('live_status') == 1

                    if live_ok:
                        # 直播状态正常，重置重试计数
                        self.reconnect_attempts = 0
                        self._retry_cooldown_until = None
                        self._retry_window_start = None
                    elif success:
                        # API 成功但 live_status != 1（平台掐断）
                        logger.warning(" 直播间已被平台关闭 (live_status=0)")
                        if not self._check_network_ok():
                            logger.warning(" 自身网络不通，等待恢复...")
                            time.sleep(10)
                            continue
                        # 网络正常 → 尝试重开直播间
                        self._handle_live_anomaly_retry(max_retries, cooldown_minutes)
                    else:
                        # API 调用本身失败（网络错误等）
                        code = status_resp.get('code', -1)
                        # 人脸验证 → 不重试
                        if code in (60024, 60043):
                            logger.warning(" 直播状态异常（人脸验证），等待人工确认")
                            continue

                        # 自身网络不通 → 无限重试
                        if not self._check_network_ok():
                            logger.warning(" 自身网络不通，等待网络恢复后重试...")
                            time.sleep(10)
                            if self._check_network_ok():
                                self._retry_start_live()
                            continue

                        # 直播间状态异常 → 限次重试
                        self._handle_live_anomaly_retry(max_retries, cooldown_minutes)

            time.sleep(1)

        # 退出处理（不变）
        if should_stop:
            zone_name_done = self.current_instruction.zone_name if self.current_instruction else '未知'
            if cross_day_detected:
                logger.info(" 跨日中断：任务被跳过，等待重置后选取新任务")
                self._stop_live_process(preserve_state=False)
                self.state.stop_streaming()
                self.current_instruction = None
                # 等待 TaskManager 每日重置，然后自动执行下一个任务
                if self.task_manager:
                    logger.info(" 等待 TaskManager 每日重置...")
                    self._push_backend_event('重置', 'info', '每日重置中，等待完成后执行新任务')
                    self.task_manager.wait_for_reset_complete(timeout=120.0)
                    logger.info(" 重置完成，执行下一个任务...")
                    self._push_backend_event('重置', 'success', '每日重置完成')
                    self.run_next_task()
            elif is_manual:
                logger.info("手动模式直播时长已到，自动下播")
                self._push_backend_event('停播', 'info', f'手动模式时长已到，自动下播（{zone_name_done}）')
                self._stop_live_process(preserve_state=False)
            else:
                logger.info("任务完成，更新 Excel...")
                self._push_backend_event('完成', 'success', f'任务完成：{zone_name_done}')
                self._stop_live_process(preserve_state=False)
                task_done = False
                if self.current_instruction and self.task_manager:
                    if self.task_manager.mark_task_done(self.current_instruction.zone_name):
                        logger.info(f" 任务 '{self.current_instruction.zone_name}' 已完成并写入 Excel")
                        self.current_instruction = None
                        self.state.stop_streaming()
                        task_done = True
                # 任务模式下自动执行下一个任务
                if task_done:
                    logger.info(" 自动查找并执行下一个任务...")
                    self._push_backend_event('切换', 'info', f'任务「{zone_name_done}」完成，自动执行下一个任务')
                    self.run_next_task()
                    if self._pending_face_verify:
                        logger.warning(" 自动切换任务遇到人脸验证，等待前端确认")
        logger.info(" 直播监控线程已退出")

    def _stop_live_process(self, preserve_state: bool = False):
        """停止直播进程（不 join 线程）
        preserve_state=True: 保留 live_state 以便恢复（任务模式手动停播时使用）
        """
        # 通知 FFmpeg 循环线程退出
        self._ffmpeg_stop_event.set()
        # 终止 FFmpeg/视频进程
        self._kill_ffmpeg()
        self.ffmpeg_current_video = ''
        # 等待循环线程退出
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)
        self._ffmpeg_stop_event.clear()
        if self.current_room_id:
            csrf = self.api.get_csrf()
            if csrf:
                self.api.stop_live(self.current_room_id, csrf)
        # 保留实际已播时长（在清除 stream_start_time 之前计算）
        actual_elapsed = 0
        if preserve_state and self.stream_start_time and self.current_instruction:
            actual_elapsed = int((datetime.now() - self.stream_start_time).total_seconds())
        self.is_streaming = False
        self.stream_start_time = None
        if preserve_state:
            # 保留状态以供恢复（任务模式下手动停播后显示恢复提示）
            self.state.elapsed_seconds = max(self.state.elapsed_seconds, actual_elapsed)
            self.state.is_streaming = True
            self.state.save()
            logger.info(f" 已保留直播状态以便恢复（已播={self.state.elapsed_seconds}秒）")
        else:
            self.state.is_streaming = False

    def stop_streaming(self) -> bool:
        """停止直播（对外接口）。任务模式保留状态以便恢复，手动模式彻底清除。"""
        logger.info("  停止直播...")
        self.stop_monitor.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            try:
                self.monitor_thread.join(timeout=3.0)
            except:
                pass
        preserve = (self._stream_mode == 'task')
        self._stop_live_process(preserve_state=preserve)
        self.current_instruction = None
        logger.info(" 直播已完全停止")
        return True

    def run_next_task(self) -> bool:
        """执行下一个直播任务（供 API 调用）"""
        logger.info("=" * 70)
        logger.info(" 开始执行新任务")
        logger.info("=" * 70)

        if self.task_manager and self.task_manager.is_resetting():
            logger.info(" 检测到每日重置进行中，等待完成...")
            if not self.task_manager.wait_for_reset_complete(timeout=120.0):
                logger.warning("️  等待重置超时，继续执行")

        instruction = self.task_manager.select_next_instruction() if self.task_manager else None
        if not instruction:
            logger.warning(" 无待执行任务")
            return False

        video_path = self.video_finder.find_video(instruction.zone_name)
        if not video_path:
            logger.error(f" 未找到视频文件，跳过任务：{instruction.zone_name}")
            return False

        return self.start_streaming(instruction, video_path)

    def get_qrcode_for_login(self) -> dict:
        """获取登录二维码数据（供 API 使用）"""
        result = self.get_qrcode_data()
        if result:
            return {'success': True, 'data': result}
        return {'success': False, 'message': '获取二维码失败'}

    def shutdown(self):
        logger.info(" 正在关闭直播控制模块...")
        self._ffmpeg_stop_event.set()
        if self.is_streaming:
            self.stop_streaming()
        self._kill_all_ffmpeg()
        logger.info(" 直播控制模块已关闭")