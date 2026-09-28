"""2026-09-28 桌面收口批次共享夹具(只读帮助模块,非测试)。

隔离环境 + 生产控制器/任务库/API 路由:
- 平台边界用合成替身(FakeApi),数据目录用 pytest tmp;
- 路由、控制器、任务库、状态持久化全部是生产实现;
- 不联网、不触碰真实账号/SMTP/直播。

线程策略:
- `LiveStart` 后台开播 worker 默认真线程(异步推进,用例用 join_worker 收口);
  worker_mode='held' 时替换为不运行的替身(交错用例手工驱动);
- `StreamMonitor`/`FFmpegLoop`/`LiveBootstrap` 等长生命周期线程持引用不运行;
- 其余(测试框架自身,如 anyio portal)一律真线程,不劫持基础设施。
"""

import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.live_controller import LiveController, LiveState
from app.core.task_manager import TaskManager


class HeldThread:
    """创建即持有、从不运行的线程替身(引用保留,语义与真线程一致)。"""

    def __init__(self, *args, **kwargs):
        self.daemon = True
        self.name = kwargs.get('name', '')
        self._target = kwargs.get('target')
        self._args = kwargs.get('args', ())
        self._started = False

    def start(self):
        self._started = True

    def is_alive(self):
        return self._started

    def join(self, timeout=None):
        return True


class DispatchThread(threading.Thread):
    """按线程名分发的线程工厂(可被第三方类继承,见 anyio WorkerThread)。"""

    worker_mode = 'thread'

    def __new__(cls, *args, **kwargs):
        name = kwargs.get('name', '')
        if name == 'LiveStart' and cls.worker_mode == 'held':
            return HeldThread(*args, **kwargs)
        if name in ('StreamMonitor', 'FFmpegLoop', 'LiveBootstrap'):
            return HeldThread(*args, **kwargs)
        return super().__new__(cls)


def make_thread_factory(worker_mode: str = 'thread'):
    return type('DispatchThread', (DispatchThread,), {'worker_mode': worker_mode})


def build_task_manager(data_dir: Path, zones=('学习区', '游戏区'),
                       category: int = 2, total_days: int = 3) -> TaskManager:
    """真实 TaskManager,库/Excel 均落在隔离目录;重复构建容忍已存在任务。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    with patch.object(TaskManager, '_start_reset_scheduler', lambda self: None), \
            patch.object(TaskManager, '_seed_sample_data', lambda self: None):
        manager = TaskManager(str(data_dir / 'tasks.db'),
                              str(data_dir / 'missing.xlsx'))
    deadline = (date.today() + timedelta(days=40)).isoformat()
    existing = {t.zone_name for t in manager.tasks}
    for zone in zones:
        if zone in existing:
            continue
        assert manager.create_task({
            'zone_name': zone, 'category': category,
            'total_days': total_days, 'days_done': 0,
            'deadline_raw': deadline,
        })
    return manager


class _NullRetryCtx:
    """retry_network_until_cancelled 的空转上下文(不做任何网络重试)。"""

    def __init__(self, cancel):
        self.cancel = cancel

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeApi:
    """平台 API 替身:记录调用、可编程返回;无任何真实网络。"""

    def __init__(self, room_id: int = 42):
        self.room_id = room_id
        self.cookies = {'DedeUserID': str(room_id)}
        self.start_live_calls = []
        self.stop_live_calls = []
        self.status_calls = []
        self.start_live_result = (True, {'code': 0, 'data': {
            'rtmp': {'addr': 'rtmp://fake.push', 'code': '?streamkey=synthetic'}}})
        self.live_status_result = (True, {'code': 0, 'data': {'live_status': 1}})
        self._lock = threading.Lock()

    def retry_network_until_cancelled(self, cancel):
        return _NullRetryCtx(cancel)

    def get_csrf(self):
        return 'synthetic-csrf'

    def start_live(self, room_id, area_id, csrf):
        with self._lock:
            self.start_live_calls.append({'room_id': room_id, 'area_id': area_id})
        return self.start_live_result

    def stop_live(self, room_id, csrf):
        with self._lock:
            self.stop_live_calls.append({'room_id': room_id})
        return True, {'code': 0}

    def get_live_status(self, room_id):
        with self._lock:
            self.status_calls.append(room_id)
        return self.live_status_result

    def get_push_url(self, room_id):
        return True, {'push_url': 'rtmp://fake.push?streamkey=synthetic'}

    def update_area(self, room_id, area_id, csrf):
        return True, {'code': 0}


class FakeEmailSender:
    """邮件替身:按 notify_* 入口记录,不发队列、不联网。"""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def _record(self, kind, **kwargs):
        with self._lock:
            self.events.append({'kind': kind, **kwargs})
        return 'queued'

    def notify_stream_running(self, zone_name=None, source=None, elapsed_label='',
                              duration_label='', remaining='', room_id=None,
                              run_id=None, **kw):
        return self._record('stream_running', zone_name=zone_name, source=source,
                            run_id=run_id, elapsed_label=elapsed_label)

    def notify_task_complete(self, zone_name=None, **kw):
        return self._record('task_complete', zone_name=zone_name)

    def notify_task_rotation(self, **kw):
        return self._record('task_rotation', **kw)

    def notify_stopped(self, zone_name=None, stage='', elapsed_label='',
                       reason='', **kw):
        return self._record('stopped', zone_name=zone_name, stage=stage)

    def notify_recovering(self, zone_name=None, **kw):
        return self._record('recovering', zone_name=zone_name)

    def notify_action_required(self, title=None, message='', **kw):
        return self._record('action_required', title=title, message=message)

    def notify_local_stream_fail(self, zone_name=None, **kw):
        return self._record('local_stream_fail', zone_name=zone_name)

    def send_face_verify(self, url, **kw):
        return self._record('face_verify', url=url)

    def send_daily_summary(self, **kw):
        return self._record('daily_summary')

    def notify_event(self, *a, **kw):
        return self._record('event')

    def count(self, kind):
        with self._lock:
            return sum(1 for e in self.events if e['kind'] == kind)


class CloseoutEnv:
    """一个用例独占的隔离环境(目录/任务库/控制器/替身)。"""

    def __init__(self, root: Path, zones=('学习区', '游戏区'),
                 settings: dict = None, worker_mode: str = 'thread'):
        self.root = root
        self.settings = {'stream_mode': 'ffmpeg', 'auto_open_video': False}
        if settings:
            self.settings.update(settings)
        self.email = FakeEmailSender()
        self.task_manager = build_task_manager(root / 'data', zones=zones)
        self.area_file = root / 'areas.json'
        self.area_file.write_text('[]', encoding='utf-8')
        self.state_file = root / 'live_state.json'
        self._thread_patch = patch('app.core.live_controller.threading.Thread',
                                   make_thread_factory(worker_mode))
        self._thread_patch.start()
        try:
            with patch.object(LiveController, '_bootstrap', lambda self: None):
                self.controller = LiveController(
                    self.task_manager, str(self.state_file), str(self.area_file))
        except Exception:
            self._thread_patch.stop()
            raise
        self.controller.api = FakeApi()
        self.controller.current_room_id = 42
        self._patchers = [
            patch('app.api.settings.load_settings',
                  lambda: SimpleNamespace(**self.settings)),
            patch('app.dependencies.get_email_sender', lambda: self.email),
        ]
        for p in self._patchers:
            p.start()

    def join_worker(self, timeout: float = 10.0):
        """等待当前开播 worker 收口(真线程模式下保证后续断言确定性)。"""
        thread = getattr(self.controller, '_start_thread', None)
        if thread is not None:
            thread.join(timeout=timeout)

    def close(self):
        for p in reversed(self._patchers):
            try:
                p.stop()
            except Exception:
                pass
        try:
            self.controller.shutdown()
        except Exception:
            pass
        try:
            self.task_manager.shutdown()
        except Exception:
            pass
        self._thread_patch.stop()

    # ---------- 常用构造 ----------

    def task_id(self, zone):
        return self.task_manager.db.get_task_by_zone(zone)['id']

    def revision(self):
        rev, _rows = self.task_manager.get_snapshot()
        return rev

    def read_state(self):
        if not self.state_file.exists():
            return None
        return json.loads(self.state_file.read_text(encoding='utf-8'))

    def read_handover(self):
        path = self.controller._handover_file()
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def seed_saved_session(self, zone='学习区', seconds=600, run_id='run-old'):
        """铺一份"已停止但未结算"的旧存档(默认 600 秒,任务模式)。"""
        task_id = self.task_id(zone)
        state = self.controller.state
        state.begin_session(zone, 42, 3600, duration_known=True,
                            source='new', task_id=task_id,
                            execution_date=date.today().isoformat(),
                            initial_elapsed=seconds,
                            service_instance=self.controller._boot_id,
                            run_id=run_id)
        state.save()
        return task_id

    def make_claim(self, zone='学习区', seconds=600, run_id='run-old',
                   confirm_id='confirm-1', **overrides):
        task_id = self.task_id(zone)
        claim = {
            'old_run_id': run_id,
            'old_task_id': task_id,
            'old_zone_name': zone,
            'old_execution_date': date.today().isoformat(),
            'old_elapsed_seconds': seconds,
            'confirm_id': confirm_id,
        }
        claim.update(overrides)
        return claim


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate)
