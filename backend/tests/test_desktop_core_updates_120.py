"""Current shared Core + real desktop SQLite; only platform replies are synthetic."""

from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.session import effects as fx
from app.core.session.clock import VirtualClock
from app.core.session.core import AccountContext, Core, Tuning, face_verify_reason
from app.core.session.events import (
    OUTCOME_AREA_UNAVAILABLE, OUTCOME_OK, OUTCOME_UNKNOWN, STATUS_CLOSED,
    STATUS_LIVE, STATUS_ROUND, PlatformResult, UserGoal,
)
from app.core.session.model import (
    MODE_SINGLE, MODE_TASK, PUSH_OBS, SELECT_AUTO, SELECT_SINGLE, Selection,
)
from app.core.session.store import SessionStore
from app.core.session_runtime.runtime import Job
from app.core.session_runtime.tasks import TaskDataPort
from port_support.backend_environment import build_task_manager, isolated_environment


class CoreHarness:
    """Execute emitted effects without adding requests or advancing past due events."""

    def __init__(self, root):
        self.clock = VirtualClock(datetime.combine(date.today(), datetime.min.time())
                                  + timedelta(hours=10))
        self.store = SessionStore(root / 'data' / 'live_state.json')
        self.account = AccountContext(uid=99001, room_id=55001)
        self.core = Core(self.store, self.clock, self.account, Tuning(
            watch_interval_seconds=20, retry_delay_seconds=20))
        self.manager = build_task_manager(root / 'data', [
            {'zone_name': zone, 'category': 1, 'total_days': 2, 'days_done': 0,
             'deadline_raw': (date.today() + timedelta(days=days)).isoformat(),
             'today_done': None}
            for zone, days in [('first', 2), ('second', 20)]])
        self.port = TaskDataPort(
            self.manager, self.store, self.clock, self.account,
            area_index=lambda: {'first': 101, 'second': 102})
        self.pending = deque()
        self.plans = defaultdict(deque)
        self.held = []
        self.actions = []
        self.facts = []
        self.room_live = False
        self.area = 101

    def send(self, event):
        result, effects = self.core.ingest(event)
        self.pending.extend(effects)
        return result

    def plan(self, kind, *, outcome=OUTCOME_OK, status=None, hold=False):
        self.plans[kind].append(SimpleNamespace(
            outcome=outcome, status=status, hold=hold))

    def drain(self):
        for _ in range(100):
            due = self.core.take_due_events(self.clock.mono())
            for event in due:
                self.send(event)
            if not self.pending and not due:
                return
            while self.pending:
                effect = self.pending.popleft()
                if isinstance(effect, fx.ConfirmedFact):
                    self.facts.append(effect)
                elif isinstance(effect, fx.PublishState):
                    pass
                elif isinstance(effect, fx.SelectTask):
                    self.send(self.port.select(Job(effect, self.clock.mono() + 5)))
                elif isinstance(effect, fx.MarkTaskAreaUnavailable):
                    self.actions.append('mark_area')
                    self.send(self.port.mark_area_unavailable(effect))
                elif isinstance(effect, (fx.StartPlatform, fx.StopPlatform,
                                         fx.QueryPlatform)):
                    kind = ('start' if isinstance(effect, fx.StartPlatform) else
                            'stop' if isinstance(effect, fx.StopPlatform) else 'query')
                    self.actions.append(kind)
                    plan = (self.plans[kind].popleft() if self.plans[kind] else
                            SimpleNamespace(outcome=OUTCOME_OK, status=None, hold=False))
                    if plan.outcome == OUTCOME_OK:
                        if kind == 'start':
                            self.room_live, self.area = True, effect.area_id
                        elif kind == 'stop':
                            self.room_live = False
                    status = plan.status or (STATUS_LIVE if self.room_live else STATUS_CLOSED)
                    reply = PlatformResult(
                        operation_id=effect.op_id, kind=kind, run_id=effect.run_id,
                        room_id=effect.room_id, outcome=plan.outcome,
                        status=status, area_id=self.area if status == STATUS_LIVE else None)
                    if plan.hold:
                        self.held.append(reply)
                    else:
                        self.send(reply)
                else:
                    raise AssertionError(f'unexpected effect: {effect!r}')
        raise AssertionError('Core emitted an unbounded immediate loop')

    def advance(self, seconds):
        end = self.clock.mono() + seconds
        while self.clock.mono() < end:
            due = self.core.next_due_mono()
            self.clock.advance(max(0, min(end, due if due is not None else end)
                                   - self.clock.mono()))
            self.drain()

    def start(self, mode=MODE_TASK):
        selection = (Selection(kind=SELECT_SINGLE, zone_name='first', area_id=101,
                               target_seconds=3000, push_source=PUSH_OBS)
                     if mode == MODE_SINGLE else
                     Selection(kind=SELECT_AUTO, push_source=PUSH_OBS))
        self.send(UserGoal(wanted='running', mode=mode, selection=selection))
        self.drain()

    def stop(self):
        goal = self.core.snapshot.goal
        self.send(UserGoal(wanted='stopped', mode=goal.mode,
                           selection=goal.selection, stop_reason='user'))
        self.drain()


@pytest.fixture()
def world(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError('Core regression attempted network access')

    monkeypatch.setattr('socket.socket.connect', deny_network)
    monkeypatch.setattr('socket.create_connection', deny_network)
    with isolated_environment() as root:
        instance = CoreHarness(root)
        try:
            yield instance
        finally:
            instance.manager.shutdown()


def test_initial_round_opens_without_stopping_platform_round(world):
    world.plan('query', status=STATUS_ROUND)
    world.start()
    assert world.core.snapshot.step == 'live'
    assert world.actions.count('stop') == 0
    assert world.actions.count('start') == 1


def test_round_reopens_same_run_and_confirmed_checkpoint(world):
    world.start()
    world.advance(20)
    original = world.core.snapshot.run
    run_id, target = original.id, original.target_seconds
    assert original.clock.checkpoint_seconds == 20
    world.plan('query', status=STATUS_ROUND)
    world.advance(20)
    run = world.core.snapshot.run
    assert (run.id, run.target_seconds) == (run_id, target)
    assert run.clock.business_elapsed(world.clock.mono()) == 20
    assert world.store.load().snapshot.run.clock.checkpoint_seconds == 20
    assert world.actions.count('stop') == 0
    assert world.actions.count('start') == 2


def test_unknown_tail_then_round_never_credits_unconfirmed_seconds(world):
    world.start()
    world.advance(20)
    run_id = world.core.snapshot.run.id
    world.plan('query', outcome=OUTCOME_UNKNOWN)
    world.advance(20)
    world.plan('query', status=STATUS_ROUND)
    world.advance(20)
    assert world.core.snapshot.run.id == run_id
    assert world.core.snapshot.run.clock.business_elapsed(world.clock.mono()) == 20
    world.advance(20)
    assert world.core.snapshot.run.clock.business_elapsed(world.clock.mono()) == 40
    assert world.actions.count('stop') == 0
    assert not any(f.kind == 'run_completed' for f in world.facts)


def test_stop_during_round_observation_never_reopens(world):
    world.start()
    world.advance(20)
    world.plan('query', status=STATUS_ROUND, hold=True)
    world.advance(20)
    assert len(world.held) == 1
    world.stop()
    world.plan('query', status=STATUS_ROUND)
    world.send(world.held.pop())
    world.drain()
    assert world.core.snapshot.goal.wanted == 'stopped'
    assert world.core.snapshot.step == 'idle'
    assert world.actions.count('start') == 1
    assert world.core.snapshot.run.clock.checkpoint_seconds == 20


def test_auto_delisted_area_is_recorded_before_next_real_task_selection(world):
    world.plan('start', outcome=OUTCOME_AREA_UNAVAILABLE)
    world.start()
    assert world.core.snapshot.goal.wanted == 'running'
    assert world.core.snapshot.step == 'live'
    assert world.core.snapshot.run.zone_name == 'second'
    first = world.manager.db.get_task_by_zone('first')
    assert first['category'] == 0 and first['days_done'] == first['total_days']
    history = world.manager.task_history_rows(first['id'])
    assert any(e['event'] == 'moved_out' and e['source'] == 'area_unavailable'
               for e in history)
    assert world.actions.index('mark_area') < len(world.actions) - 1
    assert not any(f.kind == 'session_open' and f.result == 'rejected'
                   for f in world.facts)


def test_single_delisted_area_stops_without_changing_task_progress(world):
    world.plan('start', outcome=OUTCOME_AREA_UNAVAILABLE)
    world.start(MODE_SINGLE)
    assert world.core.snapshot.goal.wanted == 'stopped'
    assert world.core.snapshot.goal.stop_reason == 'area_unavailable'
    assert world.core.snapshot.step == 'idle'
    assert world.actions.count('start') == 1
    assert world.manager.db.get_task_by_zone('first')['category'] == 1


@pytest.mark.parametrize('detail', ['需要验证', '', 'javascript:alert(1)',
                                    'https://name:secret@example.invalid/'])
def test_face_reason_has_usable_platform_url(detail):
    assert face_verify_reason(detail, 99001).endswith('source_event=400&mid=99001')


def test_platform_face_url_is_preserved():
    url = 'https://www.bilibili.com/synthetic-face-check'
    assert face_verify_reason(url, 99001) == url
