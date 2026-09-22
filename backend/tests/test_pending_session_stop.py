"""P1: cancelling a pending start must not contaminate a saved session."""

import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.live_controller import LiveController, LiveState
from app.core.task_manager import TaskManager
from app.models.schemas import LiveInstruction


class HeldThread:
    """Accept a start intent without running its worker thread."""

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self):
        return None

    def is_alive(self):
        return self._alive

    def join(self, *args, **kwargs):
        return None


class StopGateLock:
    """Hold the stop critical section while a commit attempt queues behind it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()

    def __enter__(self):
        self._lock.acquire()
        if threading.current_thread().name == 'P1-stop':
            self.stop_entered.set()
            assert self.release_stop.wait(2), 'stop gate was not released'
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


def _task_manager(tmp_path: Path) -> TaskManager:
    with patch.object(TaskManager, '_start_reset_scheduler', lambda self: None), \
            patch.object(TaskManager, '_seed_sample_data', lambda self: None):
        manager = TaskManager(str(tmp_path / 'tasks.db'),
                              str(tmp_path / 'missing.xlsx'))
    deadline = (date.today() + timedelta(days=40)).isoformat()
    for zone in ('P1-A', 'P1-B'):
        assert manager.create_task({
            'zone_name': zone,
            'category': 1,
            'total_days': 10,
            'days_done': 0,
            'deadline_raw': deadline,
        })
    return manager


def _controller(tmp_path: Path):
    manager = _task_manager(tmp_path)
    area_file = tmp_path / 'areas.json'
    area_file.write_text('[]', encoding='utf-8')
    state_file = tmp_path / 'state.json'
    with patch.object(LiveController, '_bootstrap', lambda self: None), \
            patch('app.core.live_controller.threading.Thread', HeldThread):
        controller = LiveController(manager, str(state_file), str(area_file))
    controller.api = SimpleNamespace(
        get_csrf=lambda: 'synthetic-csrf',
        stop_live=lambda *_args: (True, {'code': 0}),
    )
    controller.current_room_id = 42
    return controller, manager, state_file


def _commit_a(controller, manager):
    task_id = manager.db.get_task_by_zone('P1-A')['id']
    day = date.today().isoformat()
    instruction = LiveInstruction(
        zone_name='P1-A', duration_seconds=600, task_id=task_id,
        execution_date=day)
    controller.state.begin_session(
        'P1-A', 42, 600, task_id=task_id, execution_date=day,
        initial_elapsed=480, run_id='RUN-A')
    controller.current_instruction = instruction
    controller._current_run_id = 'RUN-A'
    controller._stream_mode = 'task'
    controller.is_streaming = True
    controller._stop_progress_accumulated = 480
    controller._clock().reset(480)
    return instruction, task_id


def _new_b(manager):
    task_id = manager.db.get_task_by_zone('P1-B')['id']
    return LiveInstruction(
        zone_name='P1-B', duration_seconds=1800, task_id=task_id,
        execution_date=date.today().isoformat())


def test_pending_new_intent_stop_preserves_previous_resume(tmp_path):
    """A pending B and repeated stop leave committed A entirely intact."""
    # This test intentionally does not use the shared controller fixture: the
    # task DB and state file must both be independent of the repository's old
    # test-data directory.
    import tempfile

    with tempfile.TemporaryDirectory(prefix='pending-session-',
                                     dir=str(tmp_path)) as root:
        root_path = Path(root)
        lc, manager, state_file = _controller(root_path)
        try:
            _, task_a = _commit_a(lc, manager)
            lc.stop_streaming()
            before = LiveState(str(state_file)).to_dict()

            task_b = _new_b(manager)
            with patch('app.core.live_controller.threading.Thread', HeldThread):
                assert lc.start_streaming(task_b, '', is_task_mode=True,
                                         source='new') is True
            lc.stop_streaming()
            lc.stop_streaming()  # duplicate stop must remain idempotent

            saved = LiveState(str(state_file))
            assert saved.current_zone == 'P1-A'
            assert saved.task_id == task_a
            assert saved.run_id == 'RUN-A'
            assert saved.elapsed_seconds == before['elapsed_seconds'] == 480
            assert saved.duration_seconds == before['duration_seconds'] == 600
            lc.state = saved
            resumed, reason = lc.resolve_resume_target()
            assert reason == ''
            assert resumed.zone_name == 'P1-A'
            assert resumed.task_id == task_a
            assert resumed.duration_seconds == 600
        finally:
            lc._is_starting = False
            manager.shutdown()


def test_first_pending_intent_stop_leaves_no_resume_state(tmp_path):
    """Cancelling the first uncommitted start cannot create a fake session."""
    lc, manager, state_file = _controller(tmp_path)
    try:
        task_b = _new_b(manager)
        with patch('app.core.live_controller.threading.Thread', HeldThread):
            assert lc.start_streaming(task_b, '', is_task_mode=True,
                                     source='new') is True
        lc.stop_streaming()
        saved = LiveState(str(state_file))
        assert saved.current_zone == ''
        assert saved.task_id is None
        assert saved.resumable() is False
    finally:
        lc._is_starting = False
        manager.shutdown()


def test_committed_new_session_stop_keeps_new_identity_and_target(tmp_path):
    """Once B is committed, stopping it persists B's own target and progress."""
    lc, manager, state_file = _controller(tmp_path)
    try:
        instruction = _new_b(manager)
        lc.state.begin_session(
            instruction.zone_name, 42, instruction.duration_seconds,
            task_id=instruction.task_id,
            execution_date=instruction.execution_date,
            initial_elapsed=120, run_id='RUN-B')
        lc.current_instruction = instruction
        lc._current_run_id = 'RUN-B'
        lc._stream_mode = 'task'
        lc.is_streaming = True
        # Model the commit thread's tiny window before its finally block clears
        # _is_starting: the session is committed, so stop must use B's state.
        lc._is_starting = True
        lc._stop_progress_accumulated = 120
        lc._clock().reset(120)
        lc.stop_streaming()
        saved = LiveState(str(state_file))
        assert saved.current_zone == 'P1-B'
        assert saved.task_id == instruction.task_id
        assert saved.run_id == 'RUN-B'
        assert saved.duration_seconds == 1800
        assert saved.elapsed_seconds == 120
    finally:
        manager.shutdown()


def test_pending_stop_does_not_hide_unrecycled_owned_process(tmp_path):
    """Pending cancellation preserves A and keeps a failed local cleanup visible."""
    lc, manager, state_file = _controller(tmp_path)
    try:
        _commit_a(lc, manager)
        lc.stop_streaming()
        task_b = _new_b(manager)
        with patch('app.core.live_controller.threading.Thread', HeldThread):
            assert lc.start_streaming(task_b, '', is_task_mode=True,
                                     source='new') is True
        lc._ffmpeg_unrecycled = True
        lc._kill_ffmpeg = lambda: None
        lc.stop_streaming()
        saved = LiveState(str(state_file))
        assert saved.current_zone == 'P1-A'
        assert saved.elapsed_seconds == 480
        assert lc._stop_cleanup_done is False
    finally:
        manager.shutdown()


def test_stop_epoch_wins_over_queued_pending_commit(tmp_path):
    """The stop decision and epoch advance are atomic against a late commit."""
    lc, manager, state_file = _controller(tmp_path)
    try:
        _commit_a(lc, manager)
        lc.stop_streaming()
        task_b = _new_b(manager)
        with patch('app.core.live_controller.threading.Thread', HeldThread):
            assert lc.start_streaming(task_b, '', is_task_mode=True,
                                     source='new') is True

        gate = StopGateLock()
        lc._start_lock = gate
        old_epoch = lc._control_epoch
        committed = threading.Event()

        def queued_commit():
            with gate:
                if (not lc._start_cancel.is_set() and
                        lc._is_epoch_current(old_epoch)):
                    lc.is_streaming = True
                    committed.set()

        stop_thread = threading.Thread(target=lc.stop_streaming, name='P1-stop')
        commit_thread = None
        try:
            stop_thread.start()
            assert gate.stop_entered.wait(2)
            commit_thread = threading.Thread(target=queued_commit, name='P1-commit')
            commit_thread.start()
            # The commit cannot inspect the old epoch until stop has advanced it.
            assert not committed.wait(0.05)
        finally:
            gate.release_stop.set()
            stop_thread.join(timeout=3)
            if commit_thread is not None:
                commit_thread.join(timeout=3)
        assert not stop_thread.is_alive()
        assert commit_thread is not None and not commit_thread.is_alive()
        assert not committed.is_set()
        saved = LiveState(str(state_file))
        assert saved.current_zone == 'P1-A'
        assert saved.task_id == manager.db.get_task_by_zone('P1-A')['id']
        assert saved.duration_seconds == 600
    finally:
        lc._is_starting = False
        manager.shutdown()


def test_new_start_rejected_while_stop_reclaims_resources(tmp_path):
    """A worker finishing during stop cannot open a new session mid-cleanup."""
    lc, manager, state_file = _controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    stop_thread = None
    stop_errors = []
    try:
        _commit_a(lc, manager)
        lc._is_starting = True  # old worker's finally will clear this below

        def slow_cleanup(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('cleanup gate was not released')
            lc._platform_stop_done = True
            lc.is_streaming = False

        lc._stop_live_process = slow_cleanup

        def stop_call():
            try:
                lc.stop_streaming()
            except Exception as exc:  # report rather than leak a thread
                stop_errors.append(exc)

        stop_thread = threading.Thread(target=stop_call, name='P1-stop-cleanup')
        stop_thread.start()
        assert entered.wait(2)
        lc._is_starting = False  # emulate the old worker finally block
        task_b = _new_b(manager)
        with patch('app.core.live_controller.threading.Thread', HeldThread):
            assert lc.start_streaming(task_b, '', is_task_mode=True,
                                     source='new') is False
    finally:
        release.set()
        if stop_thread is not None:
            stop_thread.join(timeout=3)
        lc._is_starting = False
        manager.shutdown()
    assert stop_thread is not None and not stop_thread.is_alive()
    assert not stop_errors
    saved = LiveState(str(state_file))
    assert saved.current_zone == 'P1-A'


def test_late_cancelled_start_returns_before_platform_cleanup(tmp_path):
    """An old worker that reaches the core after stop cannot touch the state."""
    lc, manager, state_file = _controller(tmp_path)
    try:
        _, task_a = _commit_a(lc, manager)
        lc.stop_streaming()
        before = LiveState(str(state_file)).to_dict()
        old_epoch = lc._control_epoch - 1
        cancelled = lc._start_cancel
        late = _new_b(manager)
        assert lc._start_streaming_sync(
            late, '', False, cancelled, old_epoch) is False
        assert lc._stream_mode == 'task'
        saved = LiveState(str(state_file))
        assert saved.current_zone == before['current_zone'] == 'P1-A'
        assert saved.task_id == before['task_id'] == task_a
        assert saved.duration_seconds == before['duration_seconds'] == 600
        assert saved.elapsed_seconds == before['elapsed_seconds'] == 480
    finally:
        manager.shutdown()
