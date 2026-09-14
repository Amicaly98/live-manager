"""桌面端适配器：把共同场景接到桌面本仓的真实入口。

只做"构造真实控制器 / 调用真实入口 / 观察"。平台边界用合成替身替换（不联网）；
数据目录必须由调用方在导入 app.core.config 之前通过 BILIBILI_DATA_DIR 指定，
因此本适配器在 import 时才绑定路径（由 run_contracts.py 保证顺序）。

有意差异（见 contracts.json 的 intentional_differences）：
- 桌面 stop_streaming 是**同步**入口（受理+清理一次完成）。共同场景需要
  "只受理、不清理"的停止时，本适配器临时以记录替身替换 _stop_live_process，
  把该产品的单体停止收窄到受理阶段（不重写被测逻辑）。
- 桌面 _retry_start_live / _start_ffmpeg_stream 不接收显式代际参数，代际在
  重连入口内部捕获（契约只约束行为，不约束内部接口）。
- 桌面重启后只提示继续，不自动恢复（auto_resume 策略不同）。
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

from contract_core import Spy, ZONE, ZONE_ALT  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / 'backend'
for _p in (str(BACKEND),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PRODUCT = 'desktop'
CAPS = {
    'stop_is_async': False,
    'reconnect_takes_epoch': False,
    'auto_resume': False,
}

_AREA_SEED = [
    {"id": 1, "name": "测试大区", "parent_id": 0, "parent_name": "测试大区",
     "children": [
         {"id": 101, "name": ZONE, "parent_id": 1, "parent_name": "测试大区", "children": []},
         {"id": 102, "name": ZONE_ALT, "parent_id": 1, "parent_name": "测试大区", "children": []},
     ]},
]


class _StubProcess:
    """替身进程：pid=None 避免任何真实 taskkill；poll 恒为存活；wait 超时。"""

    def __init__(self, alive=True):
        self.pid = None
        self.returncode = None if alive else 0
        self._alive = alive

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd='stub', timeout=timeout)
        return 0


class _FakePlatform:
    """合成平台替身（不联网）。

    `retry_network_until_cancelled` 属于控制器的可取消重试语义，不是平台边界，
    因此转发给真实 BilibiliApi 实现（与本仓测试替身一致），不在替身里重写。
    """

    def __init__(self, real_api=None):
        self.calls = []
        self.stop_calls = []
        self._real = real_api
        # 平台侧"房间是否在播"：与服务器端测试底座一致地随 start/stop 变化，
        # 否则开播前的残留清理会误认"房间仍在播"而多发一次下播。
        self.live_on = False

    def retry_network_until_cancelled(self, cancel):
        if self._real is not None:
            return self._real.retry_network_until_cancelled(cancel)
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield
        return _cm()

    def get_csrf(self):
        return 'contract-csrf'

    def start_live(self, room_id, area_id, csrf):
        self.calls.append(('start_live', room_id, area_id))
        self.live_on = True
        return True, {'code': 0, 'data': {'rtmp': {
            'addr': 'rtmp://127.0.0.1:1935/live-bvc', 'code': '?k=1'}}}

    def stop_live(self, room_id, csrf):
        self.stop_calls.append((room_id, csrf))
        self.calls.append(('stop_live', room_id))
        self.live_on = False
        return True, {'code': 0}

    def get_live_status(self, room_id):
        self.calls.append(('get_live_status', room_id))
        return True, {'code': 0, 'data': {
            'live_status': 1 if self.live_on else 0}}

    def get_push_url(self, room_id):
        self.calls.append(('get_push_url', room_id))
        return True, {'code': 0, 'push_url': 'rtmp://127.0.0.1/test',
                      'rtmp_addr': 'rtmp://127.0.0.1:1935/live-bvc'}

    def update_area(self, room_id, area_id, csrf):
        self.calls.append(('update_area', area_id))
        return True, {'code': 0}

    def names(self, name):
        return [x for x in self.calls if x[0] == name]


class DesktopAdapter:
    PRODUCT = PRODUCT
    CAPS = CAPS

    def __init__(self):
        import json
        from app.core import config
        from app.core.config import (area_file_path, state_file_path,
                                     stop_intent_file_path)
        # 路径函数依赖已初始化的数据目录：必须先 init 再取路径
        config.init_data_dir()
        self._config = config
        self._area_file = area_file_path()
        self._state_file = state_file_path()
        self._stop_intent_file = stop_intent_file_path
        self._ensure_contract_zones()

    def _ensure_contract_zones(self):
        """确保契约所需合成分区存在；**合并**而不是覆盖既有分区文件。

        pytest 下数据目录由本仓 conftest 先占位（分区名不同），直接覆盖会
        影响同会话的其它测试；这里只补齐缺失的分区。
        """
        import json
        data = []
        if self._area_file.exists():
            try:
                data = json.loads(self._area_file.read_text(encoding='utf-8'))
            except Exception:
                data = []
        if not isinstance(data, list):
            data = []
        names = {c.get('name') for g in data if isinstance(g, dict)
                 for c in (g.get('children') or [])}
        if ZONE in names and ZONE_ALT in names:
            return
        group = next((g for g in data if isinstance(g, dict)
                      and g.get('name') == '测试大区'), None)
        if group is None:
            group = {'id': 1, 'name': '测试大区', 'parent_id': 0,
                     'parent_name': '测试大区', 'children': []}
            data.append(group)
        children = group.setdefault('children', [])
        existing = {c.get('name') for c in children if isinstance(c, dict)}
        for zone, area_id in ((ZONE, 101), (ZONE_ALT, 102)):
            if zone not in existing:
                children.append({'id': area_id, 'name': zone, 'parent_id': 1,
                                 'parent_name': '测试大区', 'children': []})
        self._area_file.parent.mkdir(parents=True, exist_ok=True)
        self._area_file.write_text(json.dumps(data, ensure_ascii=False),
                                   encoding='utf-8')

    # ---------- 构造/销毁 ----------
    def make(self, mode='ffmpeg'):
        from app.core.live_controller import LiveController
        self._state_file.unlink(missing_ok=True)
        self._stop_intent_file().unlink(missing_ok=True)
        c = LiveController(task_manager=None)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(t.name == 'LiveBootstrap' and t.is_alive()
                       for t in threading.enumerate()):
                break
            time.sleep(0.05)
        c.api = _FakePlatform(real_api=c.api)
        c.current_room_id = 10001
        c._stream_mode = 'task'
        c._get_stream_settings = lambda: (mode, False)
        c.is_streaming = True
        return c

    def dispose(self, c):
        try:
            c._startup_cancel.set()
            c.stop_monitor.set()
            c._start_cancel.set()
            for th in (getattr(c, '_start_thread', None), getattr(c, 'monitor_thread', None),
                       getattr(c, '_ffmpeg_loop_thread', None)):
                if th is not None:
                    th.join(timeout=5)
        except Exception:
            pass
        try:
            self._stop_intent_file().unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- 观察点 ----------
    def spy(self, c, name, impl=None):
        spy = Spy(impl)
        setattr(c, name, spy)
        return spy

    def stub_cached_push_url(self, c, url):
        c._get_cached_push_url = lambda: url

    def set_push_url(self, c, fn):
        """替换平台侧"获取推流地址"这一网络边界（用于在等待期间插入停止）。"""
        c.api.get_push_url = fn

    def set_old_loop(self, c, loop):
        """换入受控旧循环替身（不创建真实线程）。"""
        c._ffmpeg_loop_thread = loop

    def pusher_loop(self, c):
        return c._ffmpeg_loop_thread

    def stop_signal_set(self, c):
        """内部的"停止推流循环"信号是否置位（停止后不得被旧请求清除）。"""
        return bool(c._ffmpeg_stop_event.is_set())

    def set_start_live(self, c, fn):
        c.api.start_live = fn

    def platform_call_count(self, c, name):
        return len(c.api.names(name))

    # ---------- 控制状态 ----------
    def epoch(self, c):
        return c._control_epoch

    def intent_id(self, c):
        return c._start_intent_id

    def is_streaming(self, c):
        return bool(c.is_streaming)

    def set_streaming(self, c, value):
        c.is_streaming = bool(value)

    def current_zone(self, c):
        return c.current_instruction.zone_name if c.current_instruction else ''

    def install_zone(self, c, zone):
        from app.models.schemas import LiveInstruction
        c.current_instruction = LiveInstruction(zone_name=zone, duration_seconds=3600)

    def wait_idle(self, c, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not getattr(c, '_is_starting', False):
                return True
            time.sleep(0.01)
        return not getattr(c, '_is_starting', False)

    # ---------- 真实入口 ----------
    def issue_ticket(self, c):
        return c.issue_operation()

    def begin_operation(self, c, token):
        return c.begin_control_operation(token)

    def claim_stop(self, c, token):
        # 桌面返回 None=只确认；int=登记时刻的意图快照（需要执行）
        return 'confirm' if c.claim_stop_operation(token) is None else 'execute'

    def stop_is_acceptance_only(self, c):
        """把随后的同步停止收窄到"受理阶段"：只替换清理调用，逻辑不重写。"""
        c._stop_live_process = Spy()

    def stop_user(self, c):
        return bool(c.stop_streaming())

    def start(self, c, zone, is_task_mode=True):
        from app.models.schemas import LiveInstruction
        video = c.video_finder.find_video(zone) or ''
        ins = LiveInstruction(zone_name=zone, duration_seconds=3600)
        accepted = bool(c.start_streaming(ins, video, is_task_mode))
        th = getattr(c, '_start_thread', None)
        if th is not None:
            th.join(timeout=15)
        return accepted

    def initial_start_once(self, c, zone, epoch):
        from app.models.schemas import LiveInstruction
        video = c.video_finder.find_video(zone) or ''
        ins = LiveInstruction(zone_name=zone, duration_seconds=3600)
        c.start_streaming(ins, video, True, epoch)
        th = getattr(c, '_start_thread', None)
        if th is not None:
            th.join(timeout=15)
        return bool(c.is_streaming)

    def reconnect(self, c, epoch=None):
        # 桌面重连入口不接受代际参数：它在入口内部捕获当前代际。
        return c._retry_start_live()

    def model_taken_over_generation(self, c):
        c.stop_monitor.clear()
        c._start_cancel.clear()
        c.is_streaming = True
        if c.current_instruction is None:
            self.install_zone(c, ZONE)

    # ---------- 进度与停止意图 ----------
    def mark_progress(self, c, seconds):
        c.state.elapsed_seconds = int(seconds)
        c.state.is_streaming = True
        c.state.save()

    def resumable_elapsed(self, c):
        return int(getattr(c.state, 'elapsed_seconds', 0) or 0)

    def stop_intent_present(self, c):
        return bool(self._stop_intent_file().exists())

    # ---------- 推流进程所有权 ----------
    def make_unkillable_process(self, c):
        return _StubProcess(alive=True)

    def make_finished_process(self):
        return _StubProcess(alive=False)

    def new_pusher_generation(self, c):
        return c._new_pusher_generation()

    def claim_pusher(self, c, generation, process):
        c._claim_pusher(generation, process)

    def release_pusher(self, c, generation):
        c._release_pusher(generation)

    def reclaim(self, c, timeout=1.0):
        c._kill_ffmpeg(timeout=timeout)

    def video_process(self, c):
        return c.video_process

    def unrecycled(self, c):
        return bool(c._ffmpeg_unrecycled)

    def start_pusher(self, c, epoch=None):
        # 桌面 1.1.0 起 _start_ffmpeg_stream 也接收显式代际（入口捕获后不再重取）
        return bool(c._start_ffmpeg_stream(epoch))

    # ---------- 停止清理的"完成"判定（失败分支） ----------
    def stub_failed_reclaim(self, c):
        """把回收边界替换为"失败且保留 owner"的替身，返回可读调用计数。

        只替换**进程边界**（本机不能真的创建一个杀不掉的同房间推流子进程）；
        停止入口、所有权登记、清理判定与被测状态机全部走真实实现。
        """
        def cannot_reclaim(*_args, **_kwargs):
            c._ffmpeg_unrecycled = True
            return False

        spy = Spy(impl=cannot_reclaim)
        c._kill_ffmpeg = spy
        return spy

    def stub_successful_reclaim(self, c):
        """把回收边界替换为"本代进程确实退出"，随后走**真实所有权清理**路径。

        只让替身进程"退出"（真实进程由 OS 确认），引用清理仍由产品自己的
        _release_pusher 完成，不在适配器里重写所有权逻辑。
        """
        def reclaim(*_args, **_kwargs):
            proc = c.video_process
            if proc is not None:
                proc._alive = False      # 只有替身进程能这样"退出"
            c._release_pusher(c._pusher_owner_generation())
            return True

        spy = Spy(impl=reclaim)
        c._kill_ffmpeg = spy
        return spy

    def cleanup_done(self, c):
        """上一次停止是否被判定为"确认完成"（重复停止的短路判据）。"""
        return bool(c._stop_cleanup_done)
