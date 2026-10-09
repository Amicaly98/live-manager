"""纯媒体格式工具：分区视频查找、ffprobe 兼容键与 concat 播放列表。

本模块**不含任何全局状态、不导入 LiveController**：所有输入（视频根目录、默认回落
目录、文件列表）都由调用方显式传入。它是阶段 3B 从 ``live_controller.py`` **窄提取**
出来的纯媒体规则，唯一目的是让"真实媒体端口"与旧控制器复用**同一份**规则，而不是
各自维护一份（见 ``PHASE3B-DESIGN.md`` §7）。

提取清单（先在设计列名，默认输入输出不变）：

- :data:`VIDEO_EXTENSIONS` / :func:`zone_dir_candidates` / :func:`list_zone_videos`
  —— 对应 ``LiveController._list_zone_videos``；
- :data:`VIDEO_KEY_FIELDS` / :func:`video_stream_key` / :func:`probe_video_key`
  —— 对应两处 ``key = (codec_name, width, height, pix_fmt, profile)``；
- :func:`check_concat_compatible` —— 对应 ``LiveController._check_concat_compatible``；
- :func:`filter_compatible_videos` —— 对应 ``LiveController._filter_compatible_videos``。

兼容键刻意**不含 level**（5.0 与 5.1 视为兼容），与既有行为逐字一致。
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 分区目录下被当作"可播视频"的扩展名（顺序与旧实现一致）。
VIDEO_EXTENSIONS: Tuple[str, ...] = (
    '*.mp4', '*.mkv', '*.flv', '*.avi', '*.mov', '*.wmv')

#: 参与 ``-c copy`` 兼容比较的字段。**level 不参与**。
VIDEO_KEY_FIELDS: Tuple[str, ...] = (
    'codec_name', 'width', 'height', 'pix_fmt', 'profile')

#: 单次 ffprobe 的墙钟上限（与旧实现一致）。
PROBE_TIMEOUT_SECONDS = 15.0


def zone_dir_candidates(zone_name: str) -> Tuple[str, ...]:
    """分区名的目录候选顺序：原名 → 去掉"区" → 小写（与旧实现一致）。"""
    return (zone_name, zone_name.replace('区', ''), zone_name.lower())


def _files_in(folder: Path) -> List[str]:
    files: List[str] = []
    for ext in VIDEO_EXTENSIONS:
        for path in folder.glob(ext):
            files.append(str(path.resolve()))
    return files


def list_zone_videos(video_base, default_folder, zone_name: str) -> List[str]:
    """列出分区目录下的视频；分区目录没有命中时回落到默认目录。

    与 ``LiveController._list_zone_videos`` 逐字同构：第一个**非空**的分区候选目录胜出；
    分区全空时才考虑默认目录（默认目录存在就返回它的列表，即使为空）。
    """
    video_base = Path(video_base)
    default_folder = Path(default_folder)
    for name in zone_dir_candidates(zone_name):
        zone_folder = video_base / name
        if zone_folder.exists() and zone_folder.is_dir():
            files = _files_in(zone_folder)
            if files:
                return files
    if default_folder.exists():
        return _files_in(default_folder)
    return []


def video_stream_key(stream: dict) -> Tuple:
    """从一路 ffprobe 视频流里取兼容键（**不含 level**）。"""
    return tuple(stream.get(field) for field in VIDEO_KEY_FIELDS)


def _remaining(deadline, clock) -> float:
    """本轮绝对截止还剩多少秒（没有截止时是无穷）。"""
    if deadline is None:
        return float('inf')
    now = float(clock.mono()) if clock is not None else time.monotonic()
    return max(0.0, float(deadline) - now)


def probe_video_key(path, timeout: float = PROBE_TIMEOUT_SECONDS,
                    deadline=None, clock=None) -> Optional[Tuple]:
    """对单个文件跑一次 ffprobe，返回兼容键；任何失败都返回 ``None``。

    与旧实现同样的调用形状（``v:0``、json、15 秒上限、静默）。失败**不**抛异常：
    调用方按各自规则决定"未知"是放行还是保留。

    ``deadline`` 给定时，这次探测只消费**当前剩余**时间；耗尽则直接返回 ``None``
    （不发新动作）。旧调用方不传 ``deadline``，行为与以前逐字一致。
    """
    if deadline is not None:
        remaining = _remaining(deadline, clock)
        if remaining <= 0:
            return None
        timeout = min(float(timeout), remaining)
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-select_streams', 'v:0', str(path)],
            capture_output=True, text=True, timeout=timeout)
    except Exception as exc:                          # noqa: BLE001 - 与旧实现一致
        logger.debug(' ffprobe 异常 (%s)：%s', path, exc)
        return None
    if result.returncode != 0:
        logger.debug(' ffprobe 失败 (%s)：%s', path, (result.stderr or '')[:100])
        return None
    try:
        info = json.loads(result.stdout)
    except Exception as exc:                          # noqa: BLE001
        logger.debug(' ffprobe 输出无法解析 (%s)：%s', path, exc)
        return None
    streams = info.get('streams', []) if isinstance(info, dict) else []
    if not streams:
        return None
    stream = streams[0]
    if not isinstance(stream, dict):
        return None
    return video_stream_key(stream)


def check_concat_compatible(video_list: Sequence[str], sample: int = 8,
                            timeout: float = PROBE_TIMEOUT_SECONDS,
                            deadline=None, clock=None) -> bool:
    """视频列表是否能用 ``-c copy`` 拼接（编码参数完全一致）。

    与 ``LiveController._check_concat_compatible`` 同构：只抽前 ``sample`` 个、只看 ``v:0``、
    探测失败**放行**（不把"未知"当"不兼容"）。``timeout`` 让调用方把 ffprobe 收进本轮
    媒体绝对预算；``deadline`` 给定时**每次探测只消费当前剩余**，耗尽即停止探测
    （放行由调用方按同一截止再判定）。旧调用方用默认值，行为不变。
    """
    if len(video_list) <= 1:
        return True
    ref_key: Optional[Tuple] = None
    for path in list(video_list)[:max(0, int(sample))]:
        if _remaining(deadline, clock) <= 0:
            break                                     # 预算耗尽：不再发新探测
        key = probe_video_key(path, timeout=timeout, deadline=deadline,
                              clock=clock)
        if key is None:
            continue
        if ref_key is None:
            ref_key = key
        elif key != ref_key:
            logger.info(' 编码不一致：%s ≠ %s → %s', ref_key, key, Path(path).name)
            return False
    return True


def filter_compatible_videos(video_list: Sequence[str],
                             timeout: float = PROBE_TIMEOUT_SECONDS,
                             deadline=None, clock=None) -> List[str]:
    """过滤出与第一个视频编码兼容的视频列表。

    与 ``LiveController._filter_compatible_videos`` 同构：探测失败/无视频流**保留**该文件；
    第一个成功探测的文件确定参照键。``deadline`` 给定时每次探测只消费当前剩余。
    """
    if len(video_list) <= 1:
        return list(video_list)
    ref_key: Optional[Tuple] = None
    compatible: List[str] = []
    for path in video_list:
        if _remaining(deadline, clock) <= 0:
            break                                     # 预算耗尽：不再发新探测
        key = probe_video_key(path, timeout=timeout, deadline=deadline,
                              clock=clock)
        if key is None:
            compatible.append(path)               # 无法判断，保留
            continue
        if ref_key is None:
            ref_key = key
            compatible.append(path)
        elif key == ref_key:
            compatible.append(path)
        else:
            logger.info(' 跳过不兼容视频：%s (%sx%s %s)', Path(path).name,
                        key[1], key[2], key[0])
    return compatible


def write_concat_playlist(paths: Iterable[str], destination) -> None:
    """写 concat demuxer 播放列表：每行 ``file '<绝对路径>'``。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('w', encoding='utf-8') as handle:
        for path in paths:
            handle.write(f"file '{path}'\n")
