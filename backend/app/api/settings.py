import json, logging
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()
SETTINGS_FILE = Path('settings.json')

class AppSettings(BaseModel):
    video_path: str = 'F:/videosforlive'
    excel_path: str = 'live_tasks.xlsx'
    db_path: str = 'live_tasks.db'
    scan_interval_seconds: int = 30
    max_reconnect: int = 3
    live_retry_cooldown_minutes: int = 60
    stream_mode: str = 'manual'
    auto_open_video: bool = True
    ffmpeg_path: str = 'ffmpeg'
    ffmpeg_reencode: bool = True
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
    duration_distribution: str = 'beta'
    duration_multiplier_min: float = 1.05
    duration_multiplier_max: float = 1.25

def load_settings():
    if not SETTINGS_FILE.exists():
        return AppSettings()
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return AppSettings(**data)
    except Exception as e:
        logger.warning(f'加载设置失败：{e}，使用默认值')
        return AppSettings()

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings.model_dump(), f, ensure_ascii=False, indent=2)
        logger.info('设置已保存')
    except Exception as e:
        logger.error(f'保存设置失败：{e}')
        raise

@router.get('', summary='获取设置')
async def get_settings():
    return load_settings().model_dump()

@router.put('', summary='更新设置')
async def update_settings(settings: AppSettings):
    current = load_settings()
    update_data = settings.model_dump(exclude_unset=True)
    updated = current.model_copy(update=update_data)
    save_settings(updated)
    return updated.model_dump()

@router.get('/check-videos', summary='检查视频文件夹是否有视频文件')
async def check_videos():
    s = load_settings()
    video_path = Path(s.video_path)
    has_videos = False
    if video_path.exists() and video_path.is_dir():
        video_exts = ('*.mp4', '*.mkv', '*.flv', '*.avi', '*.mov', '*.wmv')
        for sub in video_path.iterdir():
            if sub.is_dir():
                for ext in video_exts:
                    if list(sub.glob(ext)):
                        has_videos = True
                        break
                if has_videos:
                    break
        if not has_videos:
            for ext in video_exts:
                if list(video_path.glob(ext)):
                    has_videos = True
                    break
    return {'has_videos': has_videos, 'video_path': str(video_path)}

@router.get('/rtmp-code', summary='获取缓存的推流码')
async def get_rtmp_code():
    cache_file = Path('rtmp_cache.json')
    result = {'rtmp_addr': '', 'rtmp_code': '', 'full_url': '', 'room_id': ''}
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            from app.main import get_live_controller
            lc = get_live_controller()
            room_key = str(lc.current_room_id) if lc and lc.current_room_id else ''
            entry = None
            if room_key and room_key in cache:
                entry = cache[room_key]
            elif cache:
                room_key = next(iter(cache.keys()))
                entry = cache[room_key]
            if entry:
                addr = entry.get('rtmp_addr', '').rstrip('/')
                code = entry.get('rtmp_code', '')
                if code.startswith('?'):
                    full_url = f'{addr}{code}'
                else:
                    full_url = f'{addr}/{code}'
                result = {'rtmp_addr': addr, 'rtmp_code': code, 'full_url': full_url, 'room_id': room_key}
        except Exception as e:
            logger.warning(f'读取推流码缓存失败：{e}')
    return result
