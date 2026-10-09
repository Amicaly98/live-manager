"""本地媒体命令构建：分区视频清单、兼容选择、concat 播放列表与 FFmpeg argv。

构建器**只**回答"这一路本地推流怎么跑"，不读 Goal/Run、不决定轮转、不写业务状态。
执行参数（视频根目录、输出目标、ffmpeg 路径、是否重编码）由构造时的**实例上下文**
固定，端口不拿可变 Goal 来重拼请求（见 ``PHASE3B-PLAN.md`` §3）。

输出目标可以是本地文件、Windows 空设备或 ``-f null -``；测试可用显式本地输出，
**不**使用真实 RTMP 地址、不打印推流密钥。
"""

from __future__ import annotations

import logging
import os
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from ..media_format import (
    check_concat_compatible,
    filter_compatible_videos,
    list_zone_videos,
    write_concat_playlist,
)

logger = logging.getLogger(__name__)

#: 输出到"什么都不写"的 ffmpeg muxer（本地健康验证用，不是真实推流）。
NULL_OUTPUT = 'null:'


class NoVideosAvailable(RuntimeError):
    """分区与默认目录都没有可用视频：本轮不创建进程。"""


class NoCompatibleVideos(RuntimeError):
    """重编码关闭且过滤后无兼容视频：本轮不创建进程。"""


@dataclass(frozen=True)
class MediaCommand:
    """一路本地推流的完整启动事实。

    ``argv == ()`` 表示**本轮媒体预算已耗尽**：没有可启动命令，端口据此不创建对象
    （不是"命令构建失败"，也不是"没有视频"）。
    """

    argv: Tuple[str, ...]
    output: str
    reencode: bool
    concat_path: Optional[str] = None
    cleanup_paths: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def runnable(self) -> bool:
        return bool(self.argv)


def _default_settings_reader():
    try:
        from app.api.settings import load_settings
        return load_settings()
    except Exception:                                 # noqa: BLE001
        return None


class LocalCommandBuilder:
    """把 (分区名, 本轮绝对截止) 变成一条**本地** FFmpeg 命令。

    - 视频清单与兼容规则复用 ``app.core.media_format``（与旧控制器同一份规则）；
    - ``ffmpeg_path`` / ``reencode`` 来自构造时固化的设置读取器，端口不回读 Goal；
    - ffprobe 与文件准备收进本轮剩余预算，不每段各发一份。
    """

    def __init__(self, *, media_dir, video_base, default_folder,
                 output: str = '',
                 ffmpeg_path: str = 'ffmpeg',
                 reencode: Optional[bool] = None,
                 settings_reader: Optional[Callable[[], object]] = None,
                 realtime: bool = True,
                 shuffle: bool = True,
                 seed: Optional[int] = None,
                 concat_prefix: str = '_ffmpeg_concat_') -> None:
        self.media_dir = Path(media_dir)
        self.video_base = Path(video_base)
        self.default_folder = Path(default_folder)
        self._output = output
        self._ffmpeg_path = ffmpeg_path
        self._reencode = reencode
        self._settings_reader = settings_reader or _default_settings_reader
        self.realtime = bool(realtime)
        self.shuffle = bool(shuffle)
        self._random = random.Random(seed)
        self._concat_prefix = concat_prefix
        self.builds = 0

    # ---------------------------------------------------------------- 参数

    def output_for(self, zone_name: str) -> str:
        if self._output:
            return self._output
        self.media_dir.mkdir(parents=True, exist_ok=True)
        return str(self.media_dir / 'local_out.flv')

    def _settings(self):
        try:
            return self._settings_reader()
        except Exception:                             # noqa: BLE001
            return None

    def _ffmpeg(self) -> str:
        settings = self._settings()
        value = getattr(settings, 'ffmpeg_path', None) if settings else None
        return str(value) if value else self._ffmpeg_path

    def _reencode_enabled(self) -> bool:
        if self._reencode is not None:
            return bool(self._reencode)
        settings = self._settings()
        if settings is None:
            return True
        return bool(getattr(settings, 'ffmpeg_reencode', True))

    # ---------------------------------------------------------------- 构建

    def build(self, zone_name: str, deadline: float, clock=None) -> MediaCommand:
        """构建一路命令。``deadline`` 是本轮单调绝对截止；``clock`` 提供 ``mono()``。

        每一段耗时动作（ffprobe 兼容探测、文件准备）都只消费**当前剩余**时间。截止
        耗尽时返回 ``argv == ()`` 的空命令：**不产出可启动命令**，端口据此不创建对象。
        """
        videos = list_zone_videos(self.video_base, self.default_folder, zone_name)
        if not videos:
            raise NoVideosAvailable(f'分区与默认目录都没有视频：{zone_name!r}')
        videos = list(videos)
        if self.shuffle:
            self._random.shuffle(videos)

        use_reencode = False
        if not check_concat_compatible(videos, deadline=deadline, clock=clock):
            if self._reencode_enabled():
                use_reencode = True
            else:
                videos = filter_compatible_videos(videos, deadline=deadline,
                                                  clock=clock)
                if not videos:
                    raise NoCompatibleVideos('过滤后无兼容视频')

        if self._remaining(deadline, clock) <= 0:
            # 截止已被探测/过滤吃光：不再准备文件、不再产出可启动命令。
            logger.warning('媒体预算已耗尽：本轮不产出可启动命令')
            return MediaCommand(argv=(), output=self.output_for(zone_name),
                                reencode=use_reencode, concat_path=None,
                                cleanup_paths=())

        self.media_dir.mkdir(parents=True, exist_ok=True)
        concat_path = self.media_dir / (
            f'{self._concat_prefix}{uuid.uuid4().hex[:8]}.txt')
        write_concat_playlist(videos, concat_path)

        if self._remaining(deadline, clock) <= 0:
            # **准备件写完之后再核同一份绝对截止**：文件准备本身可能吃掉整份预算。
            # 已耗尽就撤回本轮准备件，明确"本轮不产出可启动命令"（不是构建失败，
            # 也不是没有视频）—— 端口据此不创建对象。
            try:
                Path(concat_path).unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning('媒体预算在文件准备后耗尽：撤回临时件，本轮不产出可启动命令')
            return MediaCommand(argv=(), output=self.output_for(zone_name),
                                reencode=use_reencode, concat_path=None,
                                cleanup_paths=())

        argv = self._argv(concat_path, self.output_for(zone_name), use_reencode)
        self.builds += 1
        return MediaCommand(argv=tuple(argv), output=self.output_for(zone_name),
                            reencode=use_reencode, concat_path=str(concat_path),
                            cleanup_paths=(str(concat_path),))

    def _argv(self, concat_path, output: str, reencode: bool) -> Sequence[str]:
        # 与既有生产命令同形：显式本地输出、不引入新的码率/缓冲实验。
        argv = [self._ffmpeg(), '-hide_banner']
        if self.realtime:
            argv.append('-re')
        argv += ['-f', 'concat', '-safe', '0', '-i', str(concat_path)]
        if reencode:
            argv += ['-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '6000k',
                     '-maxrate', '8000k', '-bufsize', '12000k',
                     '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k']
        else:
            argv += ['-c', 'copy']
        if output == NULL_OUTPUT:
            # 空 muxer 不认 flv 专用选项：只在真实 flv 输出时带上。
            argv += ['-f', 'null', '-']
        else:
            argv += ['-flvflags', '+no_duration_filesize', '-f', 'flv', str(output)]
        return argv

    @staticmethod
    def _remaining(deadline: float, clock) -> float:
        if clock is None:
            import time
            return max(0.0, float(deadline) - time.monotonic())
        return max(0.0, float(deadline) - float(clock.mono()))


def null_output_path() -> str:
    """平台无关的空设备路径（仅用于把 stdout/额外写盘降到零的场景）。"""
    return os.devnull
