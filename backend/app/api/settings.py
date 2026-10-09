import json, logging, os, threading
from pathlib import Path
from typing import Optional, Tuple
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.config import get_data_path, VIDEO_BASE_PATH

logger = logging.getLogger(__name__)
router = APIRouter()

#: 配置里承载"磁盘版本号"的键。它不是业务字段，只在读写时被解析/剥离。
REVISION_KEY = '_revision'

#: 本实例的设置提交串行化：读旧配置 → 合并 → 全量写回必须是一个不可分割的
#: 序列，否则两个并发提交会互相覆盖（客户端队列只能保护自己这一个面板）。
_SAVE_LOCK = threading.RLock()
_TMP_LOCK = threading.Lock()
_TMP_SEQ = 0


class SettingsUnavailable(RuntimeError):
    """配置存在但无法取得**可信**内容（主文件与副本都不可用）。

    与"首次安装还没有配置"是两件事：前者必须让控制接口明确不可用，
    不能退化成"没有面板密码=不启用鉴权"。
    """


class SettingsConflict(RuntimeError):
    """保存被并发修改拒绝：期望版本与磁盘上的版本不一致。"""


def _settings_file() -> Path:
    return get_data_path('settings.json')


def _initialized_marker() -> Path:
    """"配置曾经写好过"的标记。

    用来区分"首次安装、从未有过配置"（可以没有密码）与"配置被删除"
    （不可信，控制接口必须停用）。
    """
    return get_data_path('settings.initialized')


def _unique_tmp(path: Path) -> Path:
    """每次写入独立的临时文件名。

    只用 PID 命名时同一进程内多个线程会共用同一个临时文件，交错写入会
    把两个配置拼在一起。PID + 线程 + 序号保证互不覆盖。
    """
    global _TMP_SEQ
    with _TMP_LOCK:
        _TMP_SEQ += 1
        seq = _TMP_SEQ
    return path.with_name(
        f'{path.name}.{os.getpid()}.{threading.get_ident()}.{seq}.tmp')


class AppSettings(BaseModel):
    video_path: str = str(VIDEO_BASE_PATH) if VIDEO_BASE_PATH else str(Path.home() / "videosforlive")
    excel_path: str = 'live_tasks.xlsx'
    db_path: str = 'live_tasks.db'
    scan_interval_seconds: int = 30
    max_reconnect: int = 3
    live_retry_cooldown_minutes: int = 60
    # 推流方式只由本设置决定（manual=自行推流/OBS，ffmpeg=本机推流）。
    # 首次安装、或可信配置缺少该字段时默认 ffmpeg（产品既有默认：本机推流）；
    # 已有明确保存的值原样保留，升级不批量改写。
    stream_mode: str = 'ffmpeg'
    auto_open_video: bool = False  # 服务器版默认关闭
    ffmpeg_path: str = 'ffmpeg'
    ffmpeg_reencode: bool = True
    # 推送总开关：关闭后**所有**渠道（邮件/Server酱）都不再发送。
    # 旧实现里 UI 的总开关只绑定到 email_enabled，微信推送仍会继续发。
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
    # 本地 FFmpeg 短断流通常会自动重试，默认只记入面板/日志；
    # 连续快速失败达到严重阈值仍走独立的需处理通知。
    email_notify_ffmpeg_retry: bool = False
    email_face_verify_port: int = 19080
    serverchan_sendkey: str = ''
    # 面板/邮件链接中的服务器公网地址（域名或IP），留空则自动检测
    server_host: str = ''
    duration_distribution: str = 'beta'
    duration_multiplier_min: float = 1.05
    duration_multiplier_max: float = 1.25
    # 面板访问密码（pbkdf2 哈希串，空=未启用密码保护）。
    # 仅通过 /api/panel/set-password 修改，GET 时会被置空，不向前端泄露。
    panel_password: str = ''
    # 旁路诊断（默认全部关闭；启用后也只能观察，不参与任何控制决策）。
    # 开关只在新启动的推流进程/采样线程上生效，不会重启当前直播。
    diagnostics_enabled: bool = False
    diagnostics_media_progress: bool = False
    diagnostics_host_sampling: bool = False
    diagnostics_max_file_mb: int = 8
    diagnostics_retain_days: int = 7

def _last_good_file() -> Path:
    """上一个可用的完整配置副本（配置损坏时的回退来源）。

    没有它时，一次半写/损坏会让 `load_settings` 退回默认值，而默认的
    空 `panel_password` 会被鉴权中间件解释成"面板没有设密码"——等于
    静默关闭鉴权。回退副本保证"设置损坏"不会变成"控制接口不再受保护"。
    """
    return _settings_file().with_name('settings.last-good.json')


def _write_atomic(path: Path, payload: dict) -> None:
    """临时文件 + fsync + 读回校验 + os.replace；失败保留原文件并抛错。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        check = json.loads(tmp.read_text(encoding='utf-8'))  # 读回校验
        if not isinstance(check, dict):
            raise IOError('配置读回校验失败：内容不是对象')
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_document(path: Path) -> Tuple[AppSettings, int]:
    """解析一份配置文件，返回 (settings, revision)。失败时抛异常。"""
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError('配置内容不是对象')
    try:
        revision = int(raw.get(REVISION_KEY) or 0)
    except (TypeError, ValueError):
        revision = 0
    data = {k: v for k, v in raw.items() if k != REVISION_KEY}
    return AppSettings(**data), revision


def settings_document() -> Tuple[AppSettings, int, str]:
    """返回 (settings, revision, trust)，trust ∈ ok / fallback / missing / untrusted。

    - ``ok``：主文件可读；
    - ``fallback``：主文件不可读/缺失，但"最后可用副本"可读——鉴权仍受保护；
    - ``missing``：从未有过配置（全新安装）——可以没有面板密码；
    - ``untrusted``：配置**存在过**却拿不到任何可信版本——控制接口必须停用，
      不能解释成"没设密码"而静默放行。
    """
    sf = _settings_file()
    if sf.exists():
        try:
            settings, revision = _read_document(sf)
            _touch_initialized()
            return settings, revision, 'ok'
        except Exception as e:
            # 读取失败**不覆盖现场**，也不解释成"没有设过密码"。
            logger.error(
                f'配置文件无法读取（{sf}）：{e}；已保留现场不覆盖，'
                f'尝试使用上一个可用配置。请修复或删除该文件后重启服务。')
    fallback = _last_good_file()
    if fallback.exists():
        try:
            settings, revision = _read_document(fallback)
            if not sf.exists():
                logger.warning(f'主配置缺失，已使用上一个可用配置：{fallback}')
            return settings, revision, 'fallback'
        except Exception as exc:
            logger.error(f'上一个可用配置同样无法读取：{exc}')
    if sf.exists() or _initialized_marker().exists():
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
        logger.debug(f'写入配置初始化标记失败：{exc}')


def panel_auth_snapshot() -> Tuple[str, str]:
    """鉴权中间件专用：返回 (state, panel_password)。

    state='untrusted' 时调用方必须让控制接口明确不可用（例如 503），
    绝不能当作"没有设置密码"放行；state='missing' 才是真正的全新安装。
    """
    settings, _revision, trust = settings_document()
    if trust == 'untrusted':
        return 'untrusted', ''
    if trust == 'missing':
        return 'missing', ''
    return 'ok', settings.panel_password


def load_settings():
    """业务侧读取（宽松）：拿不到可信配置时返回默认值并已记录错误。

    鉴权路径不得使用本函数的宽松语义，见 :func:`panel_auth_snapshot`。
    """
    settings, _revision, _trust = settings_document()
    return settings


def push_source_from_settings(settings=None) -> str:
    """设置 → 推流来源的**唯一**解释：只由 ``stream_mode`` 决定。

    ``manual``（自行推流/OBS）→ ``obs``；其余（含字段缺失、首次安装）→ ``ffmpeg``。
    与数据接管（``session.migrate``）的同一条规则一致：不把"没有配置"猜成 OBS，
    也不在这里做任何按业务模式反推。读取沿用 :func:`load_settings` 的宽松语义
    （拿不到可信配置时返回默认并已记录错误），调用方不静默改写结论。
    """
    from app.core.session.model import PUSH_FFMPEG, PUSH_OBS

    target = settings if settings is not None else load_settings()
    mode = (target.get('stream_mode') if isinstance(target, dict)
            else getattr(target, 'stream_mode', ''))
    return PUSH_OBS if str(mode or '') == 'manual' else PUSH_FFMPEG


def current_push_source() -> str:
    """本实例**当前**有效推流来源：读设置并解释的**唯一**入口。

    控制服务的受理、以及核心在新场建立时采用的来源，都从这里取，不各自复制一套
    解释规则（解释本身仍只有 :func:`push_source_from_settings` 一处）。
    """
    return push_source_from_settings(load_settings())


def _write_document(settings, expected_revision: Optional[int] = None) -> int:
    """在 _SAVE_LOCK 内写出主配置与副本，返回新的版本号。"""
    sf = _settings_file()
    with _SAVE_LOCK:
        current_revision = 0
        if sf.exists():
            try:
                _settings, current_revision = _read_document(sf)
            except Exception:
                current_revision = 0
        elif _last_good_file().exists():
            try:
                _settings, current_revision = _read_document(_last_good_file())
            except Exception:
                current_revision = 0
        if expected_revision is not None \
                and int(expected_revision) != current_revision:
            raise SettingsConflict(
                f'配置已被其他面板修改（磁盘版本 {current_revision}，'
                f'本次基于版本 {expected_revision}）；请刷新后重试')
        new_revision = current_revision + 1
        payload = dict(settings.model_dump())
        payload[REVISION_KEY] = new_revision
        # 原子替换：写到一半/磁盘满都不会留下半个配置文件
        _write_atomic(sf, payload)
        try:
            _write_atomic(_last_good_file(), payload)
        except Exception as exc:
            logger.warning(f'更新最后可用配置副本失败（主配置已保存）：{exc}')
        _touch_initialized()
        return new_revision


def save_settings(settings, expected_revision: Optional[int] = None) -> int:
    """保存设置（本实例串行化 + 原子替换 + 可选版本冲突检查）。"""
    try:
        revision = _write_document(settings, expected_revision=expected_revision)
        logger.info('设置已保存')
        return revision
    except SettingsConflict:
        logger.warning('保存设置被并发修改拒绝')
        raise
    except Exception as e:
        logger.error(f'保存设置失败（上一个完整配置仍然有效）：{e}')
        raise

@router.get('', summary='获取设置')
async def get_settings():
    current, revision, trust = settings_document()
    data = current.model_dump()
    data['panel_password'] = ''  # 不向前端泄露密码哈希
    # 版本号随响应返回：前端把它作为下一次 PUT 的期望版本，用于发现并发修改。
    data[REVISION_KEY] = revision
    data['_trust'] = trust
    return data


@router.put('', summary='更新设置')
async def update_settings(
        settings: AppSettings,
        x_settings_revision: Optional[str] = Header(
            default=None, alias='X-Settings-Revision')):
    """保存设置。

    整个"读旧配置 → 合并 → 写回"在 _SAVE_LOCK 内完成：并发的两个提交不会
    互相覆盖（客户端自己的队列只能保护这一个面板）。
    前端可带 ``X-Settings-Revision`` 声明本次编辑基于哪个版本，磁盘版本
    已前进时返回 409，由用户决定刷新还是覆盖，而不是静默丢改动。
    """
    expected = None
    if x_settings_revision not in (None, ''):
        try:
            expected = int(x_settings_revision)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail='X-Settings-Revision 必须是整数')
    update_data = settings.model_dump(exclude_unset=True)
    # 面板密码只能通过 /api/panel/set-password 修改：忽略此处的空值，保留原哈希，
    # 避免设置页的全量自动保存把密码清空。
    if not update_data.get('panel_password'):
        update_data.pop('panel_password', None)
    with _SAVE_LOCK:
        current, revision, _trust = settings_document()
        if expected is not None and expected != revision:
            raise HTTPException(
                status_code=409,
                detail=f'配置已被其他面板修改（磁盘版本 {revision}，'
                       f'本次基于版本 {expected}）；请刷新后重试')
        updated = current.model_copy(update=update_data)
        try:
            new_revision = _write_document(updated, expected_revision=revision)
        except SettingsConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f'保存设置失败：{exc}')
    data = updated.model_dump()
    data['panel_password'] = ''
    data[REVISION_KEY] = new_revision
    return data

@router.get('/check-videos', summary='检查视频文件夹是否有视频文件')
def check_videos():
    """检查**生效的**视频根目录（``VIDEO_BASE_PATH``）是否有视频。

    实际选片走 ``VideoPathFinder``（``VIDEO_BASE_PATH/<zone>`` 回退 ``default/``），
    不是设置页里的 ``video_path`` 字段：两者不一致时，按字段判断会把"本机推流
    可用"误报成"不可用"（设置页据此禁用 FFmpeg 选择）。这里只如实检查生效目录，
    不扩大视频目录功能。生效目录不可得时才退回字段值（保持响应形状不变）。
    """
    if VIDEO_BASE_PATH:
        video_path = Path(VIDEO_BASE_PATH)
    else:
        video_path = Path(load_settings().video_path)
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
    cache_file = get_data_path('rtmp_cache.json')
    result = {'rtmp_addr': '', 'rtmp_code': '', 'full_url': '', 'room_id': ''}
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            # 房间来自**已核验账号**的冻结上下文（不再回读旧控制器）。
            from app.dependencies import get_control_service
            control = get_control_service()
            instance = control.instance if control is not None else None
            account = getattr(instance, 'account', None)
            room_key = str(getattr(account, 'room_id', '') or '')
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
