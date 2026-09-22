"""Desktop settings persistence with atomic writes and a last-good fallback."""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.config import settings_file_path, rtmp_cache_file_path, VIDEO_BASE_PATH

logger = logging.getLogger(__name__)
router = APIRouter()
SETTINGS_FILE = settings_file_path()
REVISION_KEY = '_revision'
_SAVE_LOCK = threading.RLock()
_TMP_LOCK = threading.Lock()
_TMP_SEQ = 0


class SettingsConflict(RuntimeError):
    """The settings document advanced after a client read it."""


class AppSettings(BaseModel):
    video_path: str = str(VIDEO_BASE_PATH)
    excel_path: str = 'live_tasks.xlsx'
    db_path: str = 'live_tasks.db'
    scan_interval_seconds: int = 30
    max_reconnect: int = 3
    live_retry_cooldown_minutes: int = 60
    stream_mode: str = 'manual'
    auto_open_video: bool = True
    ffmpeg_path: str = 'ffmpeg'
    ffmpeg_reencode: bool = True
    notification_enabled: bool = True
    notification_channel: str = 'email'
    email_enabled: bool = False
    email_smtp_host: str = 'smtp.qq.com'
    email_smtp_port: int = 587
    email_smtp_user: str = ''
    email_smtp_pass: str = ''
    email_recipients: str = ''
    email_notify_start: bool = True
    email_notify_stop: bool = True
    email_notify_error: bool = True
    email_notify_complete: bool = True
    email_daily_summary: bool = True
    email_face_verify_port: int = 19080
    serverchan_sendkey: str = ''
    # Explicit address for a local confirmation endpoint; empty selects loopback.
    server_host: str = ''
    duration_distribution: str = 'beta'
    duration_multiplier_min: float = 1.05
    duration_multiplier_max: float = 1.25


def _settings_file() -> Path:
    return settings_file_path()


def _last_good_file() -> Path:
    return _settings_file().with_name('settings.last-good.json')


def _initialized_marker() -> Path:
    return _settings_file().with_name('settings.initialized')


def _unique_tmp(path: Path) -> Path:
    global _TMP_SEQ
    with _TMP_LOCK:
        _TMP_SEQ += 1
        seq = _TMP_SEQ
    return path.with_name(
        f'{path.name}.{os.getpid()}.{threading.get_ident()}.{seq}.tmp')


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        check = json.loads(tmp.read_text(encoding='utf-8'))
        if not isinstance(check, dict):
            raise IOError('配置读回校验失败：内容不是对象')
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _read_document(path: Path) -> Tuple[AppSettings, int]:
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError('配置内容不是对象')
    try:
        revision = int(raw.get(REVISION_KEY) or 0)
    except (TypeError, ValueError):
        revision = 0
    values = {key: value for key, value in raw.items() if key != REVISION_KEY}
    return AppSettings(**values), revision


def settings_document() -> Tuple[AppSettings, int, str]:
    """Return settings, revision, and trust (ok/fallback/missing/untrusted)."""
    primary = _settings_file()
    if primary.exists():
        try:
            settings, revision = _read_document(primary)
            _touch_initialized()
            return settings, revision, 'ok'
        except Exception as exc:
            logger.error('配置文件无法读取（保留现场）：%s', exc)
    fallback = _last_good_file()
    if fallback.exists():
        try:
            settings, revision = _read_document(fallback)
            logger.warning('主配置不可用，使用最后可用副本：%s', fallback)
            return settings, revision, 'fallback'
        except Exception as exc:
            logger.error('最后可用配置同样无法读取：%s', exc)
    if primary.exists() or _initialized_marker().exists():
        return AppSettings(), 0, 'untrusted'
    return AppSettings(), 0, 'missing'


def _touch_initialized() -> None:
    marker = _initialized_marker()
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('initialized\n', encoding='utf-8')
    except OSError as exc:
        logger.debug('写入配置初始化标记失败：%s', exc)


def load_settings() -> AppSettings:
    settings, _revision, _trust = settings_document()
    return settings


def _write_document(settings: AppSettings,
                    expected_revision: Optional[int] = None) -> int:
    primary = _settings_file()
    with _SAVE_LOCK:
        current_revision = 0
        for candidate in (primary, _last_good_file()):
            if candidate.exists():
                try:
                    _old, current_revision = _read_document(candidate)
                    break
                except Exception:
                    continue
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise SettingsConflict(
                f'配置版本冲突：磁盘 {current_revision}，请求 {expected_revision}')
        new_revision = current_revision + 1
        payload = settings.model_dump()
        payload[REVISION_KEY] = new_revision
        _write_atomic(primary, payload)
        try:
            _write_atomic(_last_good_file(), payload)
        except Exception as exc:
            logger.warning('更新最后可用配置副本失败（主配置已保存）：%s', exc)
        _touch_initialized()
        return new_revision


def save_settings(settings: AppSettings,
                  expected_revision: Optional[int] = None) -> int:
    return _write_document(settings, expected_revision=expected_revision)


@router.get('', summary='获取设置')
async def get_settings():
    settings, revision, trust = settings_document()
    data = settings.model_dump()
    data[REVISION_KEY] = revision
    data['_trust'] = trust
    return data


@router.put('', summary='更新设置')
async def update_settings(
        settings: AppSettings,
        x_settings_revision: Optional[str] = Header(
            default=None, alias='X-Settings-Revision')):
    expected = None
    if x_settings_revision not in (None, ''):
        try:
            expected = int(x_settings_revision)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail='X-Settings-Revision 必须是整数')
    values = settings.model_dump(exclude_unset=True)
    with _SAVE_LOCK:
        current, revision, _trust = settings_document()
        if expected is not None and expected != revision:
            raise HTTPException(status_code=409, detail='配置已被其他请求修改，请刷新后重试')
        updated = current.model_copy(update=values)
        try:
            new_revision = _write_document(updated, expected_revision=revision)
        except SettingsConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f'保存设置失败：{exc}')
    data = updated.model_dump()
    data[REVISION_KEY] = new_revision
    data['_trust'] = 'ok'
    return data


@router.get('/check-videos', summary='检查视频文件夹是否有视频文件')
def check_videos():
    settings = load_settings()
    video_path = Path(settings.video_path)
    exts = ('*.mp4', '*.mkv', '*.flv', '*.avi', '*.mov', '*.wmv')
    files = []
    if video_path.exists() and video_path.is_dir():
        for item in video_path.iterdir():
            roots = [item] if item.is_dir() else [video_path]
            for root in roots:
                for ext in exts:
                    files.extend(root.glob(ext))
    return {'has_videos': bool(files), 'video_path': str(video_path)}


@router.get('/rtmp-code', summary='获取缓存的推流码')
async def get_rtmp_code():
    cache_file = rtmp_cache_file_path()
    result = {'rtmp_addr': '', 'rtmp_code': '', 'full_url': '', 'room_id': ''}
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            from app.main import get_live_controller
            controller = get_live_controller()
            room_key = str(controller.current_room_id) if controller and controller.current_room_id else ''
            entry = cache.get(room_key) if room_key else None
            if entry is None and cache:
                room_key, entry = next(iter(cache.items()))
            if entry:
                addr = entry.get('rtmp_addr', '').rstrip('/')
                code = entry.get('rtmp_code', '')
                full_url = f'{addr}{code}' if code.startswith('?') else f'{addr}/{code}'
                result = {'rtmp_addr': addr, 'rtmp_code': code,
                          'full_url': full_url, 'room_id': room_key}
        except Exception as exc:
            logger.warning('读取推流码缓存失败：%s', exc)
    return result
