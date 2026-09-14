"""A6/A8：FFmpeg 参数顺序与命令构建（纯函数，无平台依赖）。"""

from app.core.live_controller import build_ffmpeg_command, _limit_ffmpeg_log_size


def test_copy_mode_flvflags_before_output_url():
    cmd = build_ffmpeg_command('copy', 'c.txt', 'rtmp://push/url', 'ffmpeg')
    assert '-flvflags' in cmd
    i = cmd.index('-flvflags')
    assert cmd[i + 1] == 'no_duration_filesize'
    # 输出 URL 必须是 flvflags 之后的最后一个参数
    assert cmd[-1] == 'rtmp://push/url'
    assert cmd.index('-flvflags') < cmd.index('rtmp://push/url')


def test_reencode_mode_flvflags_before_output_url():
    cmd = build_ffmpeg_command('reencode', 'c.txt', 'rtmp://push/url', 'ffmpeg')
    assert cmd.index('-flvflags') < cmd.index('rtmp://push/url')
    assert cmd[-1] == 'rtmp://push/url'


def test_no_extra_options_introduced():
    """不得顺带引入 rtmp_buffer/genpts；编码器/码率参数保持不变。"""
    for mode in ('copy', 'reencode'):
        cmd = build_ffmpeg_command(mode, 'c.txt', 'rtmp://u', 'ffmpeg')
        joined = ' '.join(cmd)
        assert 'rtmp_buffer' not in joined
        assert 'genpts' not in joined
    copy_cmd = build_ffmpeg_command('copy', 'c.txt', 'rtmp://u', 'ffmpeg')
    assert '-c' in copy_cmd and 'copy' in copy_cmd
    re_cmd = build_ffmpeg_command('reencode', 'c.txt', 'rtmp://u', 'ffmpeg')
    assert 'libx264' in re_cmd
    assert '6000k' in re_cmd


def test_command_is_list_argv_without_shell():
    """A5 配套：命令必须是 list argv（不经 shell），PID 即 ffmpeg 本体。"""
    cmd = build_ffmpeg_command('copy', 'c.txt', 'rtmp://u', 'ffmpeg')
    assert isinstance(cmd, list)
    assert all(isinstance(a, str) for a in cmd)
    assert cmd[0] == 'ffmpeg'


def test_ffmpeg_log_rotation(tmp_path, monkeypatch):
    """A8：ffmpeg.log 超过容量上限时轮转为 .old，不影响写入路径。"""
    from app.core.config import FFMPEG_LOG_MAX_BYTES
    log = tmp_path / "ffmpeg.log"
    log.write_text("x" * 20, encoding='utf-8')
    monkeypatch.setattr('app.core.live_controller.FFMPEG_LOG_MAX_BYTES', 10)
    _limit_ffmpeg_log_size(log, max_bytes=10)
    assert not log.exists() or log.stat().st_size <= 10
    old = log.with_suffix(log.suffix + '.old')
    assert old.exists() and old.stat().st_size == 20
    # 轮转失败不抛异常
    _limit_ffmpeg_log_size(tmp_path / "nonexist.log", max_bytes=10)
