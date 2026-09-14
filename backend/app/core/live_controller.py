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
import secrets
import threading
import subprocess
import hashlib
import collections
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple, List, Dict, TYPE_CHECKING
from datetime import datetime, date, timedelta

import requests

from app.core.config import (
    VIDEO_BASE_PATH, DEFAULT_VIDEO_FOLDER,
    BILIBILI_APP_KEY, BILIBILI_APP_SEC,
    HEADERS, DEFAULT_LIVE_DURATION_BASE,
    MAX_RECONNECT_ATTEMPTS, MONITOR_INTERVAL,
    CROSS_DAY_CHECK_INTERVAL,
    cookies_file_path, area_file_path, state_file_path,
    rtmp_cache_file_path, ffmpeg_log_path, temp_dir_path,
    FFMPEG_LOG_MAX_BYTES, stop_intent_file_path,
)
from app.models.schemas import LiveInstruction, Task

if TYPE_CHECKING:
    from app.core.task_manager import TaskManager

logger = logging.getLogger(__name__)

# 面板查询请求的有限重试上限（无取消上下文时）
_PANEL_QUERY_MAX_ATTEMPTS = 3


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

    def __init__(self, cookie_file: str = None):
        self.cookies = {}
        self.headers = HEADERS.copy()
        self.cookie_file = Path(cookie_file) if cookie_file else cookies_file_path()
        # 网络重试上下文：后台线程可用 retry_network_until_cancelled 注入
        # 取消事件实现"无限重试直到恢复/取消"；面板查询保持有限重试。
        self._retry_context = type('_RetryCtx', (), {'cancel': None})()
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

    @contextmanager
    def retry_network_until_cancelled(self, cancel):
        """仅当前后台线程持续重试网络错误，不改变面板查询的重试策略。

        用法（后台线程内）：
            with api.retry_network_until_cancelled(cancel_event):
                ...  该线程内发起的 _req 遇网络错误将无限重试，
                     每次 await cancel_event（停止时立即打断）
        """
        previous = getattr(self._retry_context, 'cancel', None)
        self._retry_context.cancel = cancel
        try:
            yield
        finally:
            self._retry_context.cancel = previous

    def _req(self, method: str, url: str, params: dict = None, data: dict = None) -> Tuple[bool, dict]:
        """发送 API 请求（A1：有界 + 可取消）。

        - 有取消上下文（后台开播线程注入）：网络错误无限重试等待恢复，
          每次等待用 cancel.wait() 可被停止立即打断；
        - 无取消上下文（面板查询/登录等同步调用）：最多 _PANEL_QUERY_MAX_ATTEMPTS
          次有限重试后返回失败——绝不在事件循环或调用线程里无限等待；
        - HTTP 408/429/5xx 视为上游暂不可用（可重试），4xx 视为平台拒绝（不重试）。
        """
        cancel = getattr(self._retry_context, 'cancel', None)
        attempt = 0
        resp = None
        while True:
            if cancel is not None and cancel.is_set():
                return False, {"code": -1, "msg": "操作已取消", "cancelled": True}
            attempt += 1
            resp = None
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
                if cancel is not None and cancel.is_set():
                    return False, {"code": -1, "msg": "操作已取消", "cancelled": True}
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    raise requests.ConnectionError(f"上游暂不可用 HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    return False, {"code": resp.status_code, "msg": f"平台拒绝请求 HTTP {resp.status_code}"}
                try:
                    json_data = resp.json()
                    if not isinstance(json_data, dict) or 'code' not in json_data:
                        raise ValueError('响应缺少业务状态码')
                    code = json_data.get("code", -1)
                    msg = json_data.get("message") or json_data.get("msg", "")
                    if code == 0:
                        logger.debug(f" API 成功：{msg}")
                    else:
                        logger.warning(f" API 失败：code={code}, msg={msg}")
                    return code == 0, json_data
                except ValueError:
                    logger.error(f" JSON 解析失败：{resp.status_code} {resp.text[:100]}")
                    raise requests.ConnectionError('上游响应格式异常')
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException) as e:
                err_name = type(e).__name__
                logger.warning(f" 网络错误 {err_name} (第{attempt}次)：{url}")
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
            # 无取消上下文 → 面板查询语义：有限重试后放弃
            if cancel is None and attempt >= _PANEL_QUERY_MAX_ATTEMPTS:
                return False, {"code": -1, "msg": "网络请求暂时失败", "retryable": True}
            wait = min(2 ** min(attempt - 1, 5), 30)
            if cancel is not None:
                if cancel.wait(wait):
                    return False, {"code": -1, "msg": "操作已取消", "cancelled": True}
            else:
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
                save_path = temp_dir_path() / "_temp_qrcode.png"
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
    """直播分区动态加载器

    缓存位置：数据目录内 bili_areas_full.json；打包种子只在缓存缺失时复制一次。
    没有缓存时分区功能暂不可用（areas 为空 + status 说明），面板照常启动。
    """

    def __init__(self, area_file: str = None):
        self.area_file = Path(area_file) if area_file else area_file_path()
        self.areas: List[dict] = []
        self._search_cache: dict = {}
        # loaded / cache_missing / cache_corrupt / cache_invalid / empty_response /
        # invalid_payload / persist_failed / request_failed
        self.status: str = "unloaded"
        self.last_error: str = ""
        self._load_areas()

    def _load_areas(self):
        from app.core.area_data import read_cache
        areas, reason = read_cache(self.area_file)
        self.areas = areas if isinstance(areas, list) else []
        self.status = reason
        if self.areas:
            self.last_error = ""
            logger.info(f" 加载分区数据：{len(self.areas)} 项（{self.area_file}）")
        else:
            self.last_error = reason
            logger.info(f" 分区数据暂不可用（{reason}）：{self.area_file}")

    @property
    def available(self) -> bool:
        return bool(self.areas)

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
        """从 B 站 API 获取最新分区并写入本实例缓存。

        顺序：网络请求（不持 IO 锁）→ 结构校验 → 原子落盘 → 落盘成功后才切换
        内存数据并清搜索缓存。失败保留当前可用数据并返回 False。
        """
        from app.core.area_data import validate_areas, write_cache

        success, resp = api.get_areas()
        if not success or resp.get('code') != 0:
            self.status = "request_failed"
            self.last_error = str(resp.get('msg', '')) if isinstance(resp, dict) else ""
            logger.error(" 获取分区列表失败")
            return False
        data = resp.get('data', [])
        if not data:
            self.status = "empty_response"
            self.last_error = "empty_response"
            logger.error(" 获取的分区列表为空，保留当前缓存")
            return False

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

        flat = flatten(data)
        ok, reason = validate_areas(flat)
        if not ok:
            self.status = "invalid_payload"
            self.last_error = reason
            logger.error(f" 获取的分区结构不合法（{reason}），保留当前缓存")
            return False

        written, err = write_cache(self.area_file, flat)
        if not written:
            self.status = "persist_failed"
            self.last_error = err
            logger.error(f" 分区缓存写入失败（{err}），内存数据保持不变")
            return False

        self.areas = flat
        self._search_cache.clear()  # 清空旧缓存
        self.status = "loaded"
        self.last_error = ""
        logger.info(f" 成功获取并保存 {len(self.areas)} 个分区 → {self.area_file}")
        return True


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

    def __init__(self, state_file: str = None):
        self.state_file = Path(state_file) if state_file else state_file_path()
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


# ==================== 控制操作票据（A2/A3：识别"响应丢失后的重放"） ====================

_ISSUED_TICKET = __import__('re').compile(r'^([0-9a-f]+):(\d+):(\d+)$')

DEFAULT_TOKEN_TTL_SECONDS = 6 * 3600.0


def _parse_issued_ticket(token: str):
    """解析服务端签发票据 → (boot, epoch, seq)；自定义票据返回 (None, None, None)。"""
    match = _ISSUED_TICKET.match(token or '')
    if not match:
        return None, None, None
    return match.group(1), int(match.group(2)), int(match.group(3))


class _OperationTokens:
    """控制操作票据登记表。

    三类票据语义（与服务器实现一致，裁剪自 bilibili-live-server 26c170c）：

    - **服务端签发**（`<boot>:<epoch>:<seq>`，LiveController.issue_operation）：
      只比较前缀（boot + epoch），不查登记表，永不受淘汰影响；
    - **自定义票据**（旧客户端 UUID）：首次出现按当时代际登记，TTL 内保留；
      再次出现且代际已变 = "停止之后的重放" → 拒绝；
    - 超过 TTL 的票据被遗忘：明确的有效期边界。
    """

    def __init__(self, capacity: int = 4096, tombstone: int = 1024,
                 ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS,
                 boot_id: str = None):
        # RLock：accept()/register_stop() 持锁期间会调用 _reject()，
        # 普通 Lock 会自锁死。
        self._lock = threading.RLock()
        self.capacity = max(1, int(capacity))
        self._entries: 'collections.OrderedDict[str, tuple]' = collections.OrderedDict()
        self._tombstone: 'collections.deque[str]' = collections.deque(
            maxlen=max(1, int(tombstone)))
        self._tombstone_set = set()
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.boot_id = boot_id
        self.evicted_total = 0
        self.rejected = 0
        self.expired_total = 0
        self.last_reason = ''

    def _purge_expired_unlocked(self) -> None:
        if not self._entries:
            return
        now = time.monotonic()
        expired = [token for token, (_, registered_at) in self._entries.items()
                   if now - registered_at > self.ttl_seconds]
        for token in expired:
            self._entries.pop(token, None)
            self.expired_total += 1

    def _remember_evicted(self, token: str) -> None:
        if self._tombstone.maxlen and len(self._tombstone) == self._tombstone.maxlen:
            self._tombstone_set.discard(self._tombstone[0])
        self._tombstone.append(token)
        self._tombstone_set.add(token)
        self.evicted_total += 1

    def _reject(self, reason: str):
        with self._lock:
            self.rejected += 1
            self.last_reason = reason
        return False, reason

    def _register_unlocked(self, token: str, epoch: int) -> None:
        self._entries[token] = (epoch, time.monotonic())
        self._entries.move_to_end(token)
        while len(self._entries) > self.capacity:
            evicted, _ = self._entries.popitem(last=False)
            self._remember_evicted(evicted)

    def accept(self, token: str, current_epoch: int,
               allow_stale: bool = False) -> Tuple[bool, str]:
        """返回 (是否可执行, 原因)。空票据不登记、不拦截。"""
        if not isinstance(token, str) or not token:
            return True, 'no_token'
        boot, epoch, _seq = _parse_issued_ticket(token)
        if boot is not None:
            if self.boot_id is not None and boot != self.boot_id:
                return self._reject('foreign_boot_ticket')
            if epoch != current_epoch:
                return self._reject('issued_ticket_stale_epoch')
            return True, 'issued_ticket'
        with self._lock:
            self._purge_expired_unlocked()
            if allow_stale:
                self._tombstone_set.discard(token)
                self._register_unlocked(token, current_epoch)
                return True, 'registered_allow_stale'
            if token in self._tombstone_set:
                return self._reject('replay_of_evicted_ticket')
            previous = self._entries.get(token)
            if previous is None:
                self._register_unlocked(token, current_epoch)
                return True, 'registered'
            self._entries.move_to_end(token)
            if previous[0] != current_epoch:
                return self._reject('replay_after_stop')
            return True, 'replay_same_epoch'

    def register_stop(self, token: str, current_epoch: int) -> Tuple[bool, str]:
        """停止专用：返回 (是否需要执行, 原因)。

        旧代际已登记 / 墓碑 / 其它服务进程的签发票据 → 该停止意图已经在
        它自己的代际里处理完成，重放只应确认既有结果，绝不再执行——
        否则响应丢失的旧停止会停掉后来明确开启的新直播。
        """
        if not isinstance(token, str) or not token:
            return True, 'no_token'
        boot, epoch, _seq = _parse_issued_ticket(token)
        if boot is not None:
            if self.boot_id is not None and boot != self.boot_id:
                return False, 'foreign_boot_ticket'
            if epoch != current_epoch:
                return False, 'already_handled_in_prior_epoch'
            return True, 'issued_ticket_current_epoch'
        with self._lock:
            self._purge_expired_unlocked()
            previous = self._entries.get(token)
            if previous is not None:
                self._entries.move_to_end(token)
                if previous[0] == current_epoch:
                    return True, 'retry_same_epoch'
                return False, 'already_handled_in_prior_epoch'
            if token in self._tombstone_set:
                return False, 'replay_of_evicted_ticket'
            self._register_unlocked(token, current_epoch)
            return True, 'first_seen'

    def forget(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                'tracked': len(self._entries),
                'capacity': self.capacity,
                'tombstoned': len(self._tombstone_set),
                'ttl_seconds': self.ttl_seconds,
                'expired_total': self.expired_total,
                'evicted_total': self.evicted_total,
                'rejected_total': self.rejected,
                'last_reject_reason': self.last_reason,
            }


# ==================== FFmpeg 命令构建（A6：可测试的纯函数） ====================

def build_ffmpeg_command(mode: str, concat_file, push_url: str, ffmpeg_exe: str) -> list:
    """构建 FFmpeg 推流命令行（list argv，不带 shell）。

    A6：`-flvflags no_duration_filesize` 必须放在输出 URL **之前**，
    否则 FFmpeg 视其为 trailing option 产生告警且 FLV 头不更新。
    不引入 rtmp_buffer/genpts；不改变编码器/码率参数。
    """
    concat_arg = str(concat_file)
    if mode == 'reencode':
        return [
            ffmpeg_exe, '-re', '-f', 'concat', '-safe', '0', '-i', concat_arg,
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '6000k',
            '-maxrate', '8000k', '-bufsize', '12000k', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-f', 'flv', '-flvflags', 'no_duration_filesize', push_url,
        ]
    return [
        ffmpeg_exe, '-re', '-f', 'concat', '-safe', '0', '-i', concat_arg,
        '-c', 'copy', '-f', 'flv', '-flvflags', 'no_duration_filesize', push_url,
    ]


def _limit_ffmpeg_log_size(log_path: Path, max_bytes: int = FFMPEG_LOG_MAX_BYTES) -> None:
    """ffmpeg.log 简单容量控制：超过上限时整体轮转为 .old（A8）。

    仅在打开日志/启动 FFmpeg **之前**调用（此时无打开句柄，rename 安全）。
    存活期容量控制见 _truncate_ffmpeg_log_inplace（D6）。
    轮转失败不影响推流（写失败不能使直播停止）。
    """
    try:
        if log_path.exists() and log_path.stat().st_size > max_bytes:
            old = log_path.with_suffix(log_path.suffix + '.old')
            if old.exists():
                old.unlink(missing_ok=True)
            log_path.rename(old)
            logger.info(f" ffmpeg.log 已轮转（超过 {max_bytes // (1024*1024)}MB）")
    except Exception as e:
        logger.debug(f" ffmpeg.log 轮转失败（忽略）：{e}")


def _truncate_ffmpeg_log_inplace(log_fp, max_bytes: int = FFMPEG_LOG_MAX_BYTES) -> bool:
    """D6：存活期 ffmpeg.log 原地截断（容量控制贯穿长会话）。

    Windows 下打开中的文件不能 rename，但子进程继承的句柄与父进程
    log_fp **共享同一内核文件指针**——seek(0) 同时复位两者的写位置，
    truncate(0) 把文件裁回零，随后的输出从文件头继续：推流不中断、
    容量受控。截断失败绝不影响推流。返回是否执行了截断。
    """
    try:
        log_fp.flush()
        log_fp.seek(0)
        log_fp.truncate(0)
        log_fp.write(
            f"\n=== {datetime.now().isoformat()} | log truncated in place "
            f"(exceeded {max_bytes} bytes) ===\n")
        log_fp.flush()
        return True
    except Exception as e:
        logger.debug(f" ffmpeg.log 原地截断失败（忽略，不影响推流）：{e}")
        return False


# ==================== LiveController（核心控制器重构） ====================
class LiveController:
    """直播控制器（重构版，去除 GUI 依赖，面向 API）"""

    _CLASS_FALLBACK_LOCK = threading.Lock()

    def __init__(
        self,
        task_manager: 'TaskManager' = None,
        state_file: str = None,
        area_file: str = None
    ):
        self.task_manager = task_manager
        self.api = BilibiliApi()
        self.area_loader = AreaLoader(area_file)
        self.video_finder = VideoPathFinder()
        self.state = LiveState(state_file)

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

        # ---------- A2：控制代际与票据 ----------
        self._start_lock = threading.Lock()
        self._start_thread: Optional[threading.Thread] = None
        self._start_cancel = threading.Event()
        self._startup_cancel = threading.Event()
        self._is_starting = False
        self._recovery_blocked = ''
        self._control_epoch = 0
        # 上一次停止的清理是否已经跑完（且此后没有新的开播意图被受理）。
        # 用于让"清理已完成、无新会话"的重复停止只确认既有结果，而不是再走一次
        # 平台下播与进程回收（重复回收会在新会话刚建立时把它误清理掉）。
        self._stop_cleanup_done = False
        # 平台下播与"本地 owned 进程回收"分开记录：确认下发后不再重复下播，
        # 未下发/失败则保留待办，允许后续显式停止重试（见 _cleanup_confirmed）。
        self._platform_stop_done = False
        # D1：每次被接受的开播意图分配唯一 id；停止在登记时快照目标意图，
        # 执行器队列中迟到的停止不再停掉"之后新接受"的开播意图。
        self._start_intent_id = 0
        self._operation_seq = 0
        self._operation_token_limit = 4096
        self._boot_id = secrets.token_hex(4)
        self._operation_tokens = _OperationTokens(
            self._operation_token_limit, boot_id=self._boot_id)

        # ---------- A5：推流进程所有权（代际） ----------
        self._pusher_lock = threading.Lock()
        self._pusher_generation = 0
        self._video_process_generation = -1
        self._ffmpeg_unrecycled = False

        # ---------- A4：状态查询未知失败计数 ----------
        self._status_query_failures = 0
        self._status_query_failures_total = 0
        self._last_query_failure_notice = 0.0

        # ---------- A7：停止意图持久化 ----------
        self._stop_intent_epoch: Optional[int] = None
        self._load_stop_intent()

        # 后端事件日志（供前端轮询展示）
        self._backend_events: list = []

        # A1：启动时断网也不能阻塞服务就绪——登录/分区拉取放入后台引导线程，
        # 保留登录和状态数据直至网络恢复。注意：桌面版**不自动恢复直播**，
        # 仅保留 state 数据供前端显示恢复提示（A7 桌面语义）。
        threading.Thread(target=self._bootstrap, name="LiveBootstrap", daemon=True).start()

    # ---------------- 后台引导（A1：构造器不再同步刷网络） ----------------

    def _bootstrap(self):
        # 分区是运行时数据（不再随代码分发）：先做一次"有上限"的补齐尝试，
        # 失败也不影响登录/恢复；之后还能从面板「刷新分区」人工补齐。
        # 刻意放在 retry_network_until_cancelled 之外——那个上下文会一直重试到
        # 网络恢复，离线时会把登录永久挡在后面。
        try:
            self._ensure_areas_loaded()
        except Exception:
            logger.exception("启动时的分区加载失败（不影响其它功能）")
        try:
            with self.api.retry_network_until_cancelled(self._startup_cancel):
                if self.api.is_logged_in() and not self._startup_cancel.is_set():
                    self.login()
        except Exception as e:
            logger.warning(f" 后台引导异常（不阻塞服务）：{e}")

    # ---------------- A7：停止意图持久化 ----------------

    def _load_stop_intent(self):
        """读取持久化的停止意图（应用重启后仍保留"用户已停止"语义）。"""
        try:
            f = stop_intent_file_path()
            if f.exists():
                data = json.loads(f.read_text(encoding='utf-8'))
                self._stop_intent_epoch = data.get('epoch')
                logger.info(" 检测到持久化的用户停止意图（重开后仅提示继续，不自动开播）")
        except Exception as e:
            logger.debug(f" 读取停止意图失败：{e}")

    def _persist_stop_intent(self):
        try:
            stop_intent_file_path().write_text(json.dumps({
                'stopped_at': datetime.now().isoformat(),
                'zone': self.current_instruction.zone_name if self.current_instruction else (self.state.current_zone or ''),
                'epoch': self._control_epoch,
            }, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.debug(f" 写入停止意图失败：{e}")

    def _clear_stop_intent(self):
        try:
            f = stop_intent_file_path()
            if f.exists():
                f.unlink(missing_ok=True)
        except Exception:
            pass

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
        payload['is_starting'] = self._is_starting  # 后台开播进行中（A1/A2）
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

    # ---------------- 控制代际（A2：停止让此前的操作失效） ----------------

    def issue_operation(self) -> str:
        """为一次新发起的控制操作签发票据：`<boot>:<代际>:<序号>`。

        客户端在重放请求时回传该票据，服务端只比较前缀（boot + 代际）就能
        区分"停止前挂起的旧操作"与"停止完成后用户新发的请求"。
        """
        with self._start_lock:
            self._operation_seq += 1
            return f'{self._boot_id}:{self._control_epoch}:{self._operation_seq}'

    def begin_control_operation(self, token: str = '') -> Optional[int]:
        """在控制入口一次完成票据校验与控制代际快照。

        返回本次操作归属的代际；票据过期（停止前发出、停止后重放）返回 None。
        快照与停止推进代际共用同一把锁，不存在"先读代际、随后停止"的窗口。
        """
        with self._start_lock:
            epoch = self._control_epoch
            ok, reason = self._operation_tokens.accept(token, epoch)
        if not ok:
            logger.info("拒绝一次过期/重放的控制操作：%s", reason)
            return None
        return epoch

    def claim_stop_operation(self, token: str = '') -> Optional[int]:
        """停止入口专用：登记票据并返回本次停止的目标开播意图。

        返回值（D1）：
        - None：该停止意图已经在它自己的代际里处理完成（重放确认）——
          典型场景是停止的响应丢失、客户端重试，而期间用户又明确开启了
          新直播。此时重放只应确认既有结果，绝不能再次执行下播；
        - int：登记时刻的 `_start_intent_id` 快照。执行器队列中的停止
          执行时用快照核对当前意图——期间新接受的开播意图不会被
          旧队列任务停掉。
        """
        if not isinstance(token, str) or not token:
            return self._start_intent_id
        with self._start_lock:
            execute, reason = self._operation_tokens.register_stop(token, self._control_epoch)
            target = self._start_intent_id
        if not execute:
            logger.info("旧停止请求重放（%s）：只确认既有结果，不再执行下播", reason)
            return None
        return target

    def _is_epoch_current(self, epoch: int) -> bool:
        return epoch == self._control_epoch

    def _advance_control_epoch(self):
        """推进控制代际：让此前所有挂起操作在下一个复核点失效。"""
        with self._start_lock:
            self._control_epoch += 1
            return self._control_epoch

    def _pre_start_cleanup(self):
        """开播前清理（A5：只回收自建进程，不动外部 FFmpeg/OBS）"""
        # 1. 停掉自己上一代的 FFmpeg 循环与进程（所有权精确回收）
        if self.video_process or self._ffmpeg_loop_thread:
            logger.info(" 检测到残留 FFmpeg 状态，清理中...")
        self._ffmpeg_stop_event.set()
        self._kill_ffmpeg()
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)
        self._ffmpeg_stop_event.clear()
        # video_process 引用只能由所有权路径清除（_kill_ffmpeg/_release_pusher），
        # 这里不再无条件置 None（A5：回收失败时保留引用并阻止重复创建）。

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

    def start_streaming(self, instruction: LiveInstruction, video_path: str,
                        is_task_mode: bool = True, epoch: int = None) -> bool:
        """接受开播请求，使用独立后台线程持续等待网络恢复（A1/A2）。

        epoch=None 表示这是停止之后（或从未停止时）新发起的请求，取当前代际；
        epoch 为旧代际说明该请求在停止之前挂起，现在必须作废。
        返回 True 仅表示"已接受并开始后台执行"，具体结果通过 /status 轮询。
        """
        with self._start_lock:
            if epoch is None:
                epoch = self._control_epoch
            elif not self._is_epoch_current(epoch):
                logger.info("放弃一次已经过期的开播请求（停止已在其发起后生效）")
                self._push_backend_event(
                    '停止', 'info', '已放弃停止之前发起的开播请求，未重新开播')
                return False
            if self._is_starting or self.is_streaming:
                return False
            cancel = threading.Event()
            self._start_cancel = cancel
            self._is_starting = True
            self._start_intent_id += 1  # D1：新接受的开播意图（停止执行时核对）
            # 新意图接管后，上一次停止的"清理已完成"不再适用于本会话：
            # 之后到来的停止必须真正执行一次回收与下播。
            self._stop_cleanup_done = False
            self._platform_stop_done = False
            self._recovery_blocked = ''
            self._pending_face_verify = False
            self.current_instruction = instruction
            self._start_thread = threading.Thread(
                target=self._start_in_background,
                args=(instruction, video_path, is_task_mode, cancel, epoch),
                name="LiveStart", daemon=True)
            self._start_thread.start()
        return True

    def _start_in_background(self, instruction: LiveInstruction, video_path: str,
                             is_task_mode: bool, cancel: threading.Event,
                             epoch: int):
        """后台开播线程：网络重试无限等待（可取消），各边界复核代际（A2）。"""
        try:
            with self.api.retry_network_until_cancelled(cancel):
                self._start_streaming_sync(instruction, video_path, is_task_mode,
                                           cancel, epoch)
        except Exception as e:
            logger.error(f" 后台开播异常：{e}", exc_info=True)
            self._push_backend_event('错误', 'danger', f'开播异常：{e}')
        finally:
            with self._start_lock:
                self._is_starting = False

    def _start_streaming_sync(self, instruction: LiveInstruction, video_path: str,
                              is_task_mode: bool, cancel: threading.Event,
                              epoch: int) -> bool:
        """开始直播（核心逻辑，运行在后台线程）
        is_task_mode=True: 任务模式，保存 live_state、可继承已播时长
        is_task_mode=False: 手动模式，不触碰 live_state、已播时长始终=0、时长=0表示不限时
        """
        self._stream_mode = 'task' if is_task_mode else 'manual'
        dur_label = '不限时' if (not is_task_mode and instruction.duration_seconds == 0) else f"{instruction.duration_seconds // 60}分钟"
        logger.info("=" * 70)
        logger.info(f"【开播】模式：{self._stream_mode} | 分区：{instruction.zone_name} | 时长：{dur_label}")
        logger.info(f" 视频：{Path(video_path).name if video_path else '（OBS 外部推流）'}")
        logger.info("=" * 70)

        if not self.current_room_id:
            # 登录在后台引导线程里进行；开播线程内等待登录完成（可取消）
            waited = 0.0
            while not self.current_room_id and waited < 60.0:
                if cancel.is_set() or not self._is_epoch_current(epoch):
                    logger.info(" 开播请求已取消（等待登录期间停止）")
                    return False
                if self._start_login_once():
                    break
                time.sleep(1.0)
                waited += 1.0
            if not self.current_room_id:
                logger.error(" 未登录，无法开播")
                self._push_backend_event('错误', 'danger', '开播失败：未登录（请先扫码登录）')
                return False

        # 等待跨日重置（任务模式，取消可打断）
        if is_task_mode and self.task_manager and self.task_manager.is_resetting():
            logger.info(" 检测到每日重置进行中，等待完成...")
            if not self.task_manager.wait_for_reset_complete(
                    timeout=120.0, cancel=cancel):
                logger.warning(" 等待重置超时或被取消")
                if cancel.is_set() or not self._is_epoch_current(epoch):
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
        if cancel.is_set() or not self._is_epoch_current(epoch):
            # 停止发生在平台请求之后：撤销刚打开的房间，绝不把停止前发起的
            # 开播当作成功（A2/A7：旧结果不能恢复直播）
            logger.info(" 开播请求在平台返回后被停止，撤销刚打开的直播间")
            if success and self.current_room_id:
                try:
                    self.api.stop_live(self.current_room_id, csrf)
                except Exception:
                    pass
            return False

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

        # 从 start_live 返回中提取并本地缓存推流码（同账号推流码恒定不变）。
        # D1：缓存/收尾可能让出执行权，期间到达的停止由下方提交复核拦截。
        self._extract_and_cache_rtmp(resp)

        # D1：最终状态提交与停止的代际推进共用 _start_lock——
        # 停止要么在提交前推进代际（本次提交作废并撤销平台房间），
        # 要么在提交之后执行（由停止自身的回收路径处理已提交状态），
        # 不存在"平台检查已过、提交区间内被停止却仍复活"的窗口。
        with self._start_lock:
            if cancel.is_set() or not self._is_epoch_current(epoch):
                logger.info(" 开播在最终提交前被停止，撤销刚打开的直播间")
                if success and self.current_room_id:
                    try:
                        self.api.stop_live(self.current_room_id, csrf)
                    except Exception:
                        pass
                return False

            self.is_streaming = True
            self._clear_stop_intent()  # A7：成功开播即清除持久化的停止意图
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

            # 启动监控线程
            self.monitor_thread = threading.Thread(
                target=self._monitor_streaming,
                name="StreamMonitor",
                daemon=True
            )
            self.monitor_thread.start()

        # 启动视频/FFmpeg（在 start_live 之后，因为 FFmpeg 需要推流码）。
        # CTRL-01：这里**不持 _start_lock**——推流启动内部有 join 与可能的
        # 网络获取推流地址（持锁做等待会拖住停止入口）。安全性由
        # _start_ffmpeg_stream 在每个等待边界复核本代代际保证：若停止在上方
        # 提交之后到达，代际已推进，本次启动随即作废且不会创建循环。
        stream_mode, auto_open = self._get_stream_settings()
        local_push_started = True
        if stream_mode == 'ffmpeg':
            local_push_started = self._start_ffmpeg_stream(epoch)
            if not local_push_started:
                logger.error(" FFmpeg 推流启动失败")
        elif auto_open and video_path:
            if not self.play_video(video_path):
                logger.warning(" 视频播放失败，但直播已开始")

        if not local_push_started:
            return False
        logger.info(f"  直播时长：{dur_label}")
        return True

    def _start_login_once(self) -> bool:
        """后台开播线程内的单次登录尝试（不阻塞、不重试循环）。"""
        try:
            return self.login()
        except Exception as e:
            logger.debug(f" 登录尝试异常：{e}")
            return False

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
        cache_file = rtmp_cache_file_path()
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
            with open(rtmp_cache_file_path(), 'w', encoding='utf-8') as f:
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

    def _start_ffmpeg_stream(self, epoch: int = None) -> bool:
        """启动 FFmpeg 推流循环线程（使用缓存的推流码，播放完一个视频自动换下一个）

        epoch：本次启动归属的控制代际。调用方（首次开播 / 自动重连）在进入
        可能长时间等待的操作**之前**捕获代际并传入；None 表示在入口处取当前
        代际，取到之后**不再重取**——迟到的请求不能借"当前代际"把自己变成
        新代的所有者。

        CTRL-01：本函数内部有多处等待（等待旧循环 join、网络获取推流地址、
        准备返回），**每个等待之后都必须复核同一代际与停止状态**，而不是只
        看可被下一次开播清除的 stop_monitor：

        - 等待期间受理停止（或新意图接管）→ 保留旧循环引用与停止信号，
          既不清理进程也不启动新循环；
        - 旧循环仍未退出 → 保留其引用，交由正常恢复处理，绝不覆盖引用另建；
        - 只有仍持有本代所有权时，才允许清除停止信号、登记新代并创建循环。
        """
        if epoch is None:
            epoch = self._control_epoch

        def _stale() -> bool:
            """本请求是否已失去所有权；代际是主判据（停止信号会被新代清除）。"""
            return (not self._is_epoch_current(epoch)
                    or self.stop_monitor.is_set()
                    or bool(self._recovery_blocked))

        # A5：上一路推流进程未确认回收时，禁止再创建新的同房间推流
        if self._ffmpeg_unrecycled:
            logger.error(" 上一路推流进程未确认回收，禁止重复创建 FFmpeg 推流")
            self._push_backend_event('推流', 'danger', '上一路推流进程未回收，已阻止重复开播（请重启应用或手动处理后重试）')
            return False
        if _stale():
            logger.info("停止或控制代际已推进：不启动 FFmpeg 推流")
            return False
        # 先停止旧循环（防止双线程同时运行，导致 poll() 竞态和文件冲突）
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            logger.info(" 等待旧的 FFmpeg 循环线程退出...")
            self._ffmpeg_stop_event.set()
            self._ffmpeg_loop_thread.join(timeout=5.0)
            if _stale():
                logger.info(" 等待旧循环期间停止已生效：保留引用与停止信号，不清理、不启动")
                return False
            if self._ffmpeg_loop_thread.is_alive():
                logger.warning(" 旧推流线程仍在退出：保留其引用与停止信号，交由正常恢复处理")
                return False
        if _stale():
            return False
        # 只有确认仍是本代所有者，才允许清除停止信号
        self._ffmpeg_stop_event.clear()
        self._kill_ffmpeg()
        # 从缓存获取推流地址
        push_url = self._get_cached_push_url()
        if not push_url:
            # 缓存没有则尝试 API 获取（网络等待：返回后必须复核代际）
            success, data = self.api.get_push_url(self.current_room_id) if self.current_room_id else (False, {})
            if success and data.get('push_url'):
                push_url = data['push_url']
                logger.info(f" 从 API 获取推流地址：{data.get('rtmp_addr', '')[:30]}...")
            else:
                logger.error(" 无法获取推流地址：缓存和 API 均无数据")
                return False
        else:
            logger.info(f" 使用缓存推流地址：{push_url[:50]}...")
        if _stale():
            logger.info(" 推流地址准备期间停止已生效：不创建新的推流循环")
            return False

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

        # 最终副作用（登记新代 + 创建循环）前最后一次复核
        if _stale() or not self.is_streaming:
            logger.info(" 最终提交前停止已生效：不登记新代、不创建推流循环")
            return False

        # A5：登记新一代推流代际（所有权起点）
        generation = self._new_pusher_generation()

        self._ffmpeg_loop_thread = threading.Thread(
            target=self._ffmpeg_loop,
            args=(zone_name, push_url, ffmpeg_exe, generation),
            name="FFmpegLoop",
            daemon=True
        )
        self._ffmpeg_loop_thread.start()
        logger.info(f" FFmpeg 推流循环已启动（代际 {generation}）")
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

    def _ffmpeg_loop(self, zone_name: str, push_url: str, ffmpeg_exe: str, generation: int = 0):
        """FFmpeg 单进程推流：用 concat 播放列表串联所有视频。
        - 每轮 concat 结束后从 API 刷新推流 URL（B站 stream key 一次性）
        - 用时间戳文件名避免多线程/残留进程文件冲突
        - 用 _ffmpeg_stop_event 可被外部中断
        - A5：进程引用登记在当前代际名下；旧代 finally 不清新代引用
        - A6：-flvflags 放在输出 URL 之前（build_ffmpeg_command）
        """
        logger.info(f" FFmpeg 单进程推流开始 | 分区：{zone_name} | 代际：{generation}")
        rapid_fails = 0
        backoff = 5
        current_push_url = push_url
        # 时间戳唯一文件名，防止多线程 / 残留进程抢同一文件
        import uuid as _uuid
        concat_file = temp_dir_path() / f"_ffmpeg_concat_{_uuid.uuid4().hex[:8]}.txt"

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

                mode = 'reencode' if use_reencode else 'copy'
                cmd = build_ffmpeg_command(mode, concat_file, current_push_url, ffmpeg_exe)
                logger.info(f" 生成 concat 列表：{len(video_list)} 个视频（{mode}）")

                _limit_ffmpeg_log_size(ffmpeg_log_path())
                log_fp = open(str(ffmpeg_log_path()), 'a', encoding='utf-8', errors='replace')
                log_fp.write(f"\n=== {datetime.now().isoformat()} | concat {len(video_list)} files ===\n")
                log_fp.flush()

                try:
                    t_start = time.time()
                    # A5：不经 shell，直接持有 ffmpeg 进程（list argv）；
                    # PID 即 ffmpeg 本体，taskkill /T /PID 才能真实命中进程树
                    proc = subprocess.Popen(
                        cmd,
                        stdout=log_fp, stderr=subprocess.STDOUT,
                    )
                    self._claim_pusher(generation, proc)
                    self.ffmpeg_current_video = f"concat({len(video_list)}个)"
                    logger.info(f" FFmpeg concat 进程已启动 (PID={proc.pid}, 代际={generation})")

                    # 监控进程，使用本地变量 proc 避免与 _kill_ffmpeg 竞态；
                    # D6：存活期日志容量控制——每 10 秒检查一次，跨阈值原地截断
                    _log_check_ticks = 0
                    while proc.poll() is None:
                        if self._ffmpeg_stop_event.is_set() or self.stop_monitor.is_set() or not self.is_streaming:
                            self._kill_ffmpeg()
                            log_fp.close()
                            logger.info(" FFmpeg 被中断")
                            return
                        time.sleep(1)
                        _log_check_ticks += 1
                        if _log_check_ticks >= 10:
                            _log_check_ticks = 0
                            try:
                                if ffmpeg_log_path().stat().st_size > FFMPEG_LOG_MAX_BYTES:
                                    if _truncate_ffmpeg_log_inplace(log_fp):
                                        logger.info(" ffmpeg.log 存活期已原地截断（容量控制）")
                            except Exception as e:
                                logger.debug(f" ffmpeg.log 容量检查失败（忽略）：{e}")

                    exit_code = proc.returncode
                    elapsed = time.time() - t_start
                    log_fp.close()

                    # A5：确认退出后才释放所有权；未退出保留引用并标记
                    self._release_pusher(generation)

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
            # A5：只释放仍归属本代的引用；新代引用（已被 _claim_pusher 覆盖）
            # 不受旧循环退出影响
            self._release_pusher(generation)
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

    # ---------------- 推流进程所有权（A5） ----------------

    def _new_pusher_generation(self) -> int:
        """登记新一代推流进程：旧循环不得动用新进程的引用。"""
        with self._pusher_lock:
            self._pusher_generation += 1
            return self._pusher_generation

    def _claim_pusher(self, generation: int, process) -> None:
        """登记本代拥有的推流进程（Popen 成功后的所有权起点）。

        代际与进程对象成对记录：`_kill_ffmpeg` 只有在"引用仍是同一个对象、
        且代际与登记时一致"时才允许清除，旧清理不会覆盖新代的引用。
        """
        with self._pusher_lock:
            self._pusher_generation = int(generation)
            self.video_process = process
            self._video_process_generation = int(generation)

    def _pusher_owner_generation(self) -> int:
        return self.__dict__.get('_video_process_generation', -1)

    @staticmethod
    def _process_alive(process) -> bool:
        try:
            return process is not None and process.poll() is None
        except Exception:
            return False

    def _release_pusher(self, generation: int):
        """只在本代进程仍归属自己且已确认退出时才清除引用。

        终止失败或退出延迟时保留引用并标记 _ffmpeg_unrecycled，
        避免"以为清理成功"后另起一路同房间推流。
        """
        with self._pusher_lock:
            if generation != self._pusher_generation:
                # 已有更新的推流代：旧循环不能清除它的引用。
                return
            if self._process_alive(self.video_process):
                self._ffmpeg_unrecycled = True
                logger.warning("推流进程仍未确认退出：保留引用，不清除")
                return
            self.video_process = None
            self._video_process_generation = -1
            self.ffmpeg_current_video = ''
            self._ffmpeg_unrecycled = False

    def _kill_ffmpeg(self, timeout: float = 8.0):
        """强制终止当前 FFmpeg 进程（A5：只回收本应用创建的进程树）。

        - 只针对 self.video_process 持有的自建进程（PID 即 ffmpeg 本体，
          因 spawn 不经 shell）；外部 FFmpeg/OBS/未知端口服务完全不受影响；
        - Windows 用 taskkill /F /T /PID 回收真实子进程树；
        - 只有确认进程已经退出、且引用仍属于同一个进程对象（代际一致）
          才清除引用；终止失败或退出延迟时保留引用并标记 _ffmpeg_unrecycled。
        """
        process = self.video_process
        if process is None:
            return
        generation_at_entry = self._pusher_owner_generation()
        pid = None
        try:
            pid = process.pid
        except Exception:
            pass
        if pid:
            logger.info(f" 终止自建推流进程树 (PID={pid})")
            if sys.platform == 'win32':
                try:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=timeout,
                    )
                except Exception as e:
                    logger.warning(f" taskkill 执行异常：{e}")
            else:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
        # 等待确认退出（有界）
        try:
            process.wait(timeout=timeout)
        except Exception:
            pass
        with self._pusher_lock:
            if self.video_process is not process:
                logger.info(" 推流引用已被新的一代替换，旧清理不覆盖")
                return
            if self.__dict__.get('_video_process_generation', -1) != generation_at_entry:
                logger.info(" 推流代际已推进，旧清理不覆盖新代引用")
                return
            if self._process_alive(process):
                self._ffmpeg_unrecycled = True
                logger.warning(f" 推流进程 (PID={pid}) 在超时内未确认退出：保留引用，阻止重复创建")
                return
            self.video_process = None
            self._video_process_generation = -1
            self._ffmpeg_unrecycled = False
            logger.info(" 推流进程已确认退出并回收")

    def confirm_face_verify(self) -> bool:
        """清除人脸验证待处理状态，供前端确认后重试开播。

        A7：确认动作绑定当前控制代际——用户停止后（代际已推进）到达的
        邮件/远程确认不会重试开播。
        """
        self._pending_face_verify = False
        self._face_verify_url = ''
        logger.info(" 人脸验证状态已清除，可重试开播")
        return True

    def retry_after_face_verify_guarded(self, confirm_epoch: int = None) -> bool:
        """邮件确认后的重试守卫（A7）。

        返回 False 表示确认属于已过去的代际（用户已停止或已开启新意图），
        不得据此重试开播。
        """
        if confirm_epoch is not None and not self._is_epoch_current(confirm_epoch):
            logger.info(" 忽略迟到的验证确认（用户已停止或开启新意图）")
            return False
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
            # 可取消等待：停止时立即打断，不再发起迟到的重连
            if self.stop_monitor.wait(min(2 ** self.reconnect_attempts, 30)):
                logger.info(" 重连等待被停止打断")
                return
            self._retry_start_live()
        else:
            self._retry_cooldown_until = now + timedelta(minutes=cooldown_minutes)
            logger.warning(f" 重试次数耗尽，冷却 {cooldown_minutes} 分钟至 {self._retry_cooldown_until.strftime('%H:%M:%S')}")
            self._push_backend_event('重连', 'danger', f'重试次数耗尽，冷却 {cooldown_minutes} 分钟')

    def _retry_start_live(self):
        """内部重试开播：重开 B站 房间 + 按实际推流方式恢复（A4）。

        恢复判断不看任务/手动分区（task/manual），只看推流设置：
        - stream_mode == 'ffmpeg' → 重启 FFmpeg 推流循环；
        - 其他（OBS 外部推流）→ 不创建、不杀掉、不重启用户推流进程。

        D1：进入时捕获控制代际；平台 start_live 返回后复核——重连请求
        在平台响应期间被停止（代际推进/stop_monitor 置位）时，迟到的成功
        响应不能恢复本地推流，还要撤销刚重新打开的平台房间。
        """
        if not self.current_room_id or not self.current_instruction:
            return
        epoch = self._control_epoch  # D1：捕获代际，平台返回后复核
        intent_at_entry = self._start_intent_id  # D1：新意图已存在时不回滚房间
        csrf = self.api.get_csrf()
        if not csrf:
            return
        area_id = self.area_loader.get_area_id(self.current_instruction.zone_name, auto_update=False)
        success, resp = self.api.start_live(self.current_room_id, area_id, csrf)
        # D1：平台响应期间的停止 → 撤销刚重开的房间，绝不恢复推流。
        # 若期间用户已接受新开播意图，房间状态归新意图的清理流程管，
        # 这里不再补发下播（避免误关新任务的房间）。
        if self.stop_monitor.is_set() or not self._is_epoch_current(epoch):
            logger.info(" 重连请求在平台返回后被停止，撤销刚重开的直播间")
            if (success and self.current_room_id
                    and self._start_intent_id == intent_at_entry):
                try:
                    self.api.stop_live(self.current_room_id, csrf)
                except Exception:
                    pass
            return
        if success:
            logger.info(" 重连成功，直播间已重新开启")
            self._extract_and_cache_rtmp(resp)  # 更新推流码缓存
            # CTRL-01：缓存/准备期间同样可能发生停止；stop_monitor 会被下一次
            # 开播清除，不能作为唯一判据——必须复核本代是否仍是当前代，之后才
            # 提交重连计数与本地推流启动。
            if not self._is_epoch_current(epoch) or self.stop_monitor.is_set():
                logger.info(" 重连缓存准备期间停止生效，不恢复本地推流")
                return
            self.reconnect_attempts = 0
            self._retry_cooldown_until = None
            # 按实际推流方式恢复（A4：手动分区 + FFmpeg 同样需要恢复推流）。
            # CTRL-01：把入口捕获的代际传下去，推流启动在每个等待边界复核它。
            stream_mode, _ = self._get_stream_settings()
            if stream_mode == 'ffmpeg':
                logger.info(" 重启 FFmpeg 推流循环...")
                self._start_ffmpeg_stream(epoch)
        else:
            code = resp.get('code', -1)
            if code in (60024, 60043):
                self._pending_face_verify = True
                uid = self.api.cookies.get('DedeUserID', '')
                self._face_verify_url = (
                    "https://www.bilibili.com/blackboard/live/face-auth-middle.html"
                    f"?source_event=400&mid={uid}")
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

                # === 分层重试逻辑（A4：查询失败只记未知，不据此掐流） ===
                if self.current_room_id:
                    success, status_resp = self.api.get_live_status(self.current_room_id)
                    live_ok = success and status_resp.get('data', {}).get('live_status') == 1

                    if live_ok:
                        # 直播状态正常，重置重试计数与未知计数
                        self.reconnect_attempts = 0
                        self._retry_cooldown_until = None
                        self._retry_window_start = None
                        self._status_query_failures = 0
                    elif success:
                        # API 成功但 live_status != 1（确认被平台关闭）
                        self._status_query_failures = 0
                        logger.warning(" 直播间已被平台关闭 (live_status=0)")
                        if not self._check_network_ok():
                            logger.warning(" 自身网络不通，等待恢复...")
                            if self.stop_monitor.wait(10):
                                break
                            continue
                        # 网络正常 → 尝试重开直播间（限次）
                        self._handle_live_anomaly_retry(max_retries, cooldown_minutes)
                    else:
                        # API 调用本身失败：状态未知 ≠ 直播已断。
                        # 本地 FFmpeg 可能仍在正常推进，绝不能据此重开/停播（A4）。
                        code = status_resp.get('code', -1)
                        if code in (60024, 60043):
                            logger.warning(" 直播状态异常（人脸验证），等待人工确认")
                            continue
                        self._status_query_failures += 1
                        self._status_query_failures_total += 1
                        now = time.time()
                        if now - self._last_query_failure_notice >= 60.0:
                            self._last_query_failure_notice = now
                            logger.warning(
                                f" 状态查询失败（未知，第 {self._status_query_failures} 次），"
                                f"保持当前直播不动，仅节流提示")
                            self._push_backend_event(
                                '监控', 'warning',
                                f'直播状态查询失败（状态未知，不影响本地推流），已连续 {self._status_query_failures} 次')

            if self.stop_monitor.wait(1):
                break

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

    @staticmethod
    def _platform_stop_ok(result) -> bool:
        """平台下播结果归一化：容错 (ok, payload) 元组与裸布尔。

        None 表示实现没有返回值——请求未抛异常，视为已下发（与旧行为一致，
        不把"没有返回值"当成失败而反复重试平台下播）。
        """
        if isinstance(result, tuple):
            return bool(result[0]) if result else False
        if result is None:
            return True
        return bool(result)

    def _cleanup_confirmed(self) -> bool:
        """本次停止是否**确认**完成：owned 进程已回收、平台下播已下发。

        必须基于实际状态判定，不能因为清理函数"返回了"就当作完成：回收失败时
        引用仍在且 _ffmpeg_unrecycled 为真，此时若标记完成，重复停止会被
        "已完成"分支短路，连一次回收重试都没有机会。
        """
        if self._process_alive(self.video_process):
            return False
        if self._ffmpeg_unrecycled:
            return False
        return bool(self._platform_stop_done)

    def _stop_live_process(self, preserve_state: bool = False):
        """停止直播进程（不 join 线程）
        preserve_state=True: 保留 live_state 以便恢复（任务模式手动停播时使用）
        """
        # 通知 FFmpeg 循环线程退出
        self._ffmpeg_stop_event.set()
        # 终止 FFmpeg/视频进程（A5：所有权精确回收，失败保留引用）
        self._kill_ffmpeg()
        # 等待循环线程退出
        if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
            self._ffmpeg_loop_thread.join(timeout=3.0)
        self._ffmpeg_stop_event.clear()
        # 平台下播与本地进程回收分开记录：已确认下发的房间不再重复下播
        # （重试"本地回收"时不得顺手把已被新意图接管的房间再下一次播）；
        # 未下发/失败则保留待办，允许后续显式停止重试。
        if not self.current_room_id:
            self._platform_stop_done = True
        elif not self._platform_stop_done:
            csrf = self.api.get_csrf()
            if csrf:
                try:
                    self._platform_stop_done = self._platform_stop_ok(
                        self.api.stop_live(self.current_room_id, csrf))
                except Exception as e:
                    logger.warning(f" 平台下播请求异常（本地进程仍会停止）：{e}")
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

    def stop_streaming(self, token: str = '') -> bool:
        """停止直播（用户对外接口，A7）。

        - 先推进控制代际并取消在途开播：挂起中的 start 在各复核点失效，
          其迟到结果被撤销（包括平台侧刚打开的房间）；
        - 持久化用户停止意图：旧开播/邮件回调不能改回；重开应用只提示继续；
        - 任务模式保留状态以便恢复（保持桌面默认"重开后提示继续，不自动开播"），
          手动模式彻底清除。
        """
        logger.info("  停止直播...")
        # 1) 推进代际 + 取消在途开播（A2/A7：停止优先于一切挂起操作）
        self._advance_control_epoch()
        self._start_cancel.set()
        # 2) 停监控
        self.stop_monitor.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            try:
                self.monitor_thread.join(timeout=3.0)
            except Exception:
                pass
        # 3) 持久化停止意图（在动 state 之前记录原始分区名）
        self._persist_stop_intent()
        # 4) 停进程与平台侧。
        # 重复停止的判据必须明确：清理已经完成、且此后没有新的在播会话时，
        # 重复请求只确认既有结果——不再重复回收进程/重复下播（否则新会话
        # 刚建立时会被旧停止请求误清理）。新会话会重置该标记（见 start_streaming）。
        preserve = (self._stream_mode == 'task')
        if self._stop_cleanup_done and not self.is_streaming:
            logger.info("重复的停止请求：清理已完成且无新的在播会话，只确认结果")
        else:
            self._stop_live_process(preserve_state=preserve)
            # 只有确认回收（owned 进程已退出、平台下播已下发）才记"清理完成"。
            # 回收失败时引用仍在、_ffmpeg_unrecycled 为真 → 保留失败状态，
            # 让用户再次停止时还能真正重试回收，而不是被"已完成"分支短路。
            self._stop_cleanup_done = self._cleanup_confirmed()
        self.current_instruction = None
        logger.info(" 直播已完全停止")
        return True

    def run_next_task(self, epoch: int = None) -> bool:
        """执行下一个直播任务（供 API 调用）。

        epoch 来自控制入口的代际快照：等待重置/选任务期间发生停止时，
        旧代际的请求在 start_streaming 的复核点作废。
        D3：OBS（非 ffmpeg 推流设置）模式下无本地视频不阻断任务执行。
        """
        logger.info("=" * 70)
        logger.info(" 开始执行新任务")
        logger.info("=" * 70)

        instruction = self.task_manager.select_next_instruction() if self.task_manager else None
        if not instruction:
            logger.warning(" 无待执行任务")
            return False

        video_path = self.video_finder.find_video(instruction.zone_name)
        if not video_path:
            stream_mode, _ = self._get_stream_settings()
            if stream_mode == 'ffmpeg':
                logger.error(f" 未找到视频文件，跳过任务：{instruction.zone_name}")
                return False
            video_path = ''  # OBS 外部推流：允许无本地视频

        return self.start_streaming(instruction, video_path, is_task_mode=True,
                                    epoch=epoch)

    def get_qrcode_for_login(self) -> dict:
        """获取登录二维码数据（供 API 使用）"""
        result = self.get_qrcode_data()
        if result:
            return {'success': True, 'data': result}
        return {'success': False, 'message': '获取二维码失败'}

    def shutdown(self, stop_platform: bool = True):
        """关闭直播控制模块。

        D2：stop_platform=False 用于"不停止并退出"——保存进度、回收自建
        本地推流进程（FFmpeg/视频播放），**不调用平台下播 API**、不碰
        外部 OBS 推流；平台侧房间保持原状。
        """
        logger.info(" 正在关闭直播控制模块...")
        # 取消后台开播/引导线程（A1/A2）
        self._startup_cancel.set()
        self._advance_control_epoch()
        self._start_cancel.set()
        self._ffmpeg_stop_event.set()
        self.stop_monitor.set()
        if self.is_streaming or self._is_starting:
            if stop_platform:
                self.stop_streaming()
            else:
                # D2：退出不停播——只回收自建进程 + 保存进度
                self._kill_ffmpeg()
                if self._ffmpeg_loop_thread and self._ffmpeg_loop_thread.is_alive():
                    self._ffmpeg_loop_thread.join(timeout=3.0)
                if self.monitor_thread and self.monitor_thread.is_alive():
                    self.monitor_thread.join(timeout=2.0)
                # 保存进度：任务模式把已播时长写入 live_state（重开后提示继续）
                if self._stream_mode == 'task' and self.stream_start_time:
                    elapsed = int((datetime.now() - self.stream_start_time).total_seconds())
                    self.state.elapsed_seconds = max(self.state.elapsed_seconds, elapsed)
                    self.state.is_streaming = True
                    self.state.save()
                    logger.info(f" 已保存进度（已播={self.state.elapsed_seconds}秒），平台侧未下播")
                self.is_streaming = False  # 本地控制器视角停止；平台/OBS 不受影响
        # A5：只回收自建进程（所有权感知），绝不全局 taskkill
        self._kill_ffmpeg()
        logger.info(" 直播控制模块已关闭")