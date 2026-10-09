"""桌面数据目录与旧安装目录迁移的守卫（从退役模块中**拆出并保留**）。

为什么单独成文件：`test_a7_e3_data_intent.py` 整体断言的是旧 ``LiveController`` 的
私有方法，已按"被测对象不存在"退役；但**同一文件里**这三条守卫不依赖旧控制器，
对应的还是桌面**现在仍在用**的能力（``run.py`` 的数据目录初始化与旧安装目录迁移、
防覆盖），退役它们等于把产品守卫一起删掉（复核"测试退役范围"一节）。

这里的三条按当前 ``app.core.config`` 接口重写（旧文件读的模块级 ``DATA_DIR`` 已不
存在），语义逐条不变。后面两条是**新链上的替代守卫**：旧文件断言的 ffmpeg argv 与
日志轮转，在新链里分别由 ``media_playlist.LocalCommandBuilder`` 与
``media_port.OwnedMediaWatcher`` 承担——登记"由新链承担"不等于有可运行的用例。

原始文件与断言保留在 ``test_a7_e3_data_intent.py`` / ``test_a6_ffmpeg_options.py`` /
``test_d6_log_rotation.py``（未删除，仍在 ``conftest.RETIRED_MODULES`` 里登记不收集），
便于对照。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.config import init_data_dir
from app.core.data_migration import migrate_legacy_data


# ==================== 数据目录初始化与旧安装目录迁移 ====================

def test_data_dir_init_and_write_probe(tmp_path):
    """数据目录初始化：创建 + 可写探测 + 幂等（测试后还原全局状态）。"""
    from app.core import config

    saved_dir, saved_flag = config._DATA_DIR, config._data_dir_initialized
    try:
        config._data_dir_initialized = False
        d = tmp_path / "iso-data"
        p1 = config.init_data_dir(str(d))
        assert p1 == d.resolve() and p1.exists()
        # 探测文件写入并可读回（保留不删除：兼容环境的删除保护）
        probe = p1 / ".write_probe"
        assert probe.exists() and probe.read_text(encoding="utf-8") != ""
        p2 = config.init_data_dir()  # 幂等（已初始化时直接返回）
        assert p2 == p1
    finally:
        config._DATA_DIR = saved_dir
        config._data_dir_initialized = saved_flag


def test_migration_copies_with_backup_and_never_overwrites(tmp_path):
    """E3：旧数据迁移——带备份；目标已存在的新数据绝不被旧数据覆盖。"""
    legacy = tmp_path / "legacy_root"
    data = tmp_path / "data"
    legacy.mkdir()
    data.mkdir()
    (legacy / "live_state.json").write_text('{"elapsed_seconds": 555}', encoding='utf-8')
    (legacy / "settings.json").write_text('{"video_path": "old"}', encoding='utf-8')
    # 新数据已存在 → 必须保留
    (data / "settings.json").write_text('{"video_path": "new"}', encoding='utf-8')

    actions = dict(migrate_legacy_data(data, legacy))
    assert actions["live_state.json"] == "migrated"
    assert actions["settings.json"] == "kept_new"
    assert json.loads((data / "live_state.json").read_text(encoding='utf-8'))["elapsed_seconds"] == 555
    assert json.loads((data / "settings.json").read_text(encoding='utf-8'))["video_path"] == "new"
    # 备份存在
    backups = list((data / "migration_backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "live_state.json").exists()


def test_migration_noop_when_same_dir(tmp_path):
    data = tmp_path / "same"
    data.mkdir()
    assert migrate_legacy_data(data, data) == []


# ==================== 新链上的替代守卫：ffmpeg argv ====================

def _builder(tmp_path, *, realtime=True, output='', reencode=None):
    from app.core.session_runtime.media_playlist import LocalCommandBuilder

    media_dir = tmp_path / 'media'
    media_dir.mkdir(exist_ok=True)
    return LocalCommandBuilder(
        media_dir=media_dir, video_base=tmp_path / 'videos',
        default_folder=tmp_path / 'videos' / 'default', output=output,
        ffmpeg_path='ffmpeg-x', reencode=reencode,
        settings_reader=lambda: None, realtime=realtime, shuffle=False)


def test_media_command_argv_is_argv_list_without_shell(tmp_path):
    builder = _builder(tmp_path)
    argv = builder._argv(tmp_path / '_c.txt', 'rtmp://local/out.flv', reencode=False)

    # 逐项字符串、可直接交给不经过 shell 的启动器（不存在拼接式的命令字符串）。
    assert not isinstance(argv, str)
    assert all(isinstance(item, str) for item in argv)
    assert argv[0] == 'ffmpeg-x'
    assert '-c' in argv and argv[argv.index('-c') + 1] == 'copy'


def test_media_command_flvflags_only_for_real_flv_output(tmp_path):
    builder = _builder(tmp_path)
    output = 'rtmp://local/out.flv'
    argv = builder._argv(tmp_path / '_c.txt', output, reencode=False)
    # flvflags 出现在输出 URL **之前**，且输出是最后一项。
    assert '-flvflags' in argv
    assert argv.index('-flvflags') < argv.index(output)
    assert argv[-1] == output

    from app.core.session_runtime.media_playlist import NULL_OUTPUT
    null_argv = builder._argv(tmp_path / '_c.txt', NULL_OUTPUT, reencode=False)
    assert '-flvflags' not in null_argv
    assert 'null' in null_argv


def test_media_command_reencode_mode_declares_encoder_and_rate(tmp_path):
    builder = _builder(tmp_path)
    argv = builder._argv(tmp_path / '_c.txt', 'rtmp://local/out.flv', reencode=True)
    assert 'libx264' in argv
    assert '-c' not in argv        # 重编码模式不再走 stream copy
    assert argv[0] == 'ffmpeg-x'


# ==================== 新链上的替代守卫：媒体日志轮转 ====================

def _watcher(log_path: Path, *, log_max_bytes: int):
    from app.core.session_runtime.media_port import OwnedMedia, OwnedMediaWatcher

    owned = OwnedMedia(instance_id='i', physical_id='p', pid=os.getpid(),
                       creation_time=0, run_id='r', log_path=log_path)
    # ``_open_log`` / ``_rotate_if_needed`` 不触碰端口与 OS 适配层；这里只测这一小段。
    return OwnedMediaWatcher(None, owned, interval=0.01, stall_window=1.0,
                             log_max_bytes=log_max_bytes, os_ops=None, name='t-log')


def test_media_log_rotates_while_writer_is_alive(tmp_path):
    """写入者**活着**时也要轮转：旧段留成 .1，句柄重开后还能继续写。"""
    log_path = tmp_path / 'ffmpeg.log'
    watcher = _watcher(log_path, log_max_bytes=64)
    watcher._open_log()
    try:
        watcher._handle.write(b'x' * 200)
        watcher._rotate_if_needed()

        rotated = log_path.with_suffix(log_path.suffix + '.1')
        assert rotated.is_file()
        assert rotated.read_bytes() == b'x' * 200
        assert watcher._handle is not None

        # 重开之后的句柄仍然可用（轮转不是"把写入者一起关掉"）。
        watcher._handle.write(b'y' * 10)
        assert log_path.read_bytes() == b'y' * 10
    finally:
        watcher._close_log()


def test_media_log_rotation_failure_does_not_kill_writer(tmp_path, monkeypatch):
    """轮转失败（改名被拒）不能让观察线程死掉：句柄必须重开、继续可写。"""
    log_path = tmp_path / 'ffmpeg.log'
    watcher = _watcher(log_path, log_max_bytes=64)
    watcher._open_log()
    try:
        watcher._handle.write(b'x' * 200)

        def boom(*_args, **_kwargs):
            raise OSError('rotate refused')

        monkeypatch.setattr(
            'app.core.session_runtime.media_port.os.replace', boom)
        watcher._rotate_if_needed()          # 不抛
        assert watcher.last_error == ''

        watcher._handle.write(b'z' * 5)
        assert log_path.read_bytes().endswith(b'z' * 5)
    finally:
        watcher._close_log()
        monkeypatch.undo()
