"""日初容错指标：真实 SQLite、完成账本、计划修订及读写交错。"""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.task_manager import TaskManager


class ScheduleMarginIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-margin-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.today = date.today()
        self.deadline = self.today + timedelta(days=9)
        # Windows 事件循环内部先建立本机 socketpair，再禁止测试业务代码联网。
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        for target in ('socket.socket.connect', 'socket.create_connection'):
            guard = patch(target, side_effect=AssertionError('测试不得访问网络'))
            guard.start()
            self.addCleanup(guard.stop)
        self.tm = self.manager()
        self.addCleanup(self.tm.shutdown)

    def manager(self):
        return TaskManager(
            db_path=str(self.root / 'tasks.db'),
            excel_path=str(self.root / 'unused.xlsx'), reset_scheduler=False,
            seed_sample_data=False, import_from_excel=False,
            last_run_date_file=str(self.root / 'last-run.json'))

    def create(self, zone='A', total=3, hours=2, deadline=None, replace=False):
        preview = self.tm.preview_creation(zone)
        command = {'zone_name': zone, 'category': hours, 'total_days': total,
                   'deadline_raw': (deadline or self.deadline).isoformat()}
        if replace:
            command.update(confirmed=True, preview={
                'task_id': preview['existing_task']['id'],
                'revision': preview['tasks_revision'],
                'business_day': preview['business_day'],
                'existing_is_unfinished': preview['existing_is_unfinished']})
        result = self.tm.create_new_task(command)
        self.assertEqual(result['status'], 'committed', result)
        return result['task_id']

    def margin(self, day=None, manager=None):
        return (manager or self.tm).get_stats(as_of=day or self.today)['schedule_margin']

    def natural(self, task_id, day=None, **extra):
        result = self.tm.settle_completion(
            task_id=task_id, business_day=(day or self.today).isoformat(),
            run_id='isolated-run', settlement_guard=self.tm.progress_guard(task_id),
            effective_seconds=7200, target_seconds=7200, today=self.today, **extra)
        self.assertEqual(result['status'], self.tm.COMPLETION_CREDITED, result)
        return result

    def assert_hours(self, hours, day=None, manager=None):
        result = self.margin(day, manager)
        self.assertIsNone(result['reason'], result)
        self.assertEqual(result['hours'], hours, result)
        return result

    def test_natural_completion_keeps_day_start_margin_and_old_stats_move(self):
        task_id = self.create()
        before = self.tm.get_stats()
        self.natural(task_id, completed_at='1999-01-01T00:00:00')
        after = self.tm.get_stats()
        self.assertEqual(after['schedule_margin'], before['schedule_margin'])
        self.assertEqual(before['remaining_time'], 6)
        self.assertEqual(after['remaining_time'], 4)
        self.assertNotEqual(after['urgency'], before['urgency'])

    def test_manual_today_completion_keeps_margin(self):
        task_id = self.create()
        before = self.margin()
        result = self.tm.settle_task_done('A', task_id=task_id,
                                          execution_date=self.today)
        self.assertEqual(result['status'], 'settled', result)
        self.assertEqual(self.margin(), before)

    def test_final_day_category_zero_is_restored_for_both_completion_sources(self):
        for source in ('auto', 'manual'):
            with self.subTest(source=source):
                task_id = self.create(zone=source, total=1)
                before = self.margin()
                if source == 'auto':
                    self.natural(task_id)
                else:
                    result = self.tm.settle_task_done(source, task_id=task_id,
                                                      execution_date=self.today)
                    self.assertEqual(result['status'], 'settled')
                self.assertEqual(self.tm.db.get_task_by_zone(source)['category'], 0)
                self.assertEqual(self.margin(), before)

    def test_today_all_done_restores_prior_progress_and_expires_next_day(self):
        self.create(total=4)
        self.assertTrue(self.tm.update_task_fields('A', {'days_done': 1}))
        before = self.margin()
        self.assertTrue(self.tm.mark_task_all_done('A'))
        self.assertEqual(self.margin(), before)
        self.assertEqual(self.margin(self.today + timedelta(days=1))['reason'],
                         'no_pending_tasks')

    def test_plan_edits_after_completion_keep_latest_hours_total_and_deadline(self):
        task_id = self.create(total=3)
        self.natural(task_id)
        deadline = self.today + timedelta(days=6)
        self.assertTrue(self.tm.update_task_fields(
            'A', {'total_days': 4, 'category': 3,
                  'deadline_raw': deadline.isoformat()}))
        result = self.assert_hours(7 * 22 - 4 * 3)
        self.assertEqual(result['bottleneck_date'], deadline.isoformat())

    def test_final_completion_then_zero_category_parameter_edits_preserve_requirement(self):
        task_id = self.create(total=1, hours=3)
        self.natural(task_id)
        self.assertTrue(self.tm.update_task_fields('A', {'total_days': 2}))
        # 原静态公式将已完成行 days_done 补到新 total；不能把该补正当人工进度。
        self.assertEqual(self.tm.db.get_task_by_zone('A')['days_done'], 2)
        deadline = self.today + timedelta(days=5)
        self.assertTrue(self.tm.update_task_fields('A', {'deadline_raw': deadline.isoformat()}))
        self.assert_hours(6 * 22 - 2 * 3)

    def test_explicit_manual_progress_replaces_earlier_completion_effect(self):
        task_id = self.create(total=5)
        self.natural(task_id)
        self.assertTrue(self.tm.update_task_fields('A', {'days_done': 3}))
        self.assert_hours(10 * 22 - 2 * 2)

    def test_restart_same_day_retains_margin_without_persistent_cache(self):
        task_id = self.create(total=1)
        before = self.margin()
        self.natural(task_id)
        self.tm.shutdown()
        restarted = self.manager()
        self.addCleanup(restarted.shutdown)
        self.assertEqual(self.margin(manager=restarted), before)

    def test_next_business_day_uses_reduced_remaining_and_shorter_capacity(self):
        task_id = self.create()
        self.natural(task_id)
        self.assert_hours(10 * 22 - 3 * 2)
        self.assert_hours(9 * 22 - 2 * 2, self.today + timedelta(days=1))

    def test_late_previous_business_day_uses_ledger_day_instead_of_record_timestamps(self):
        task_id = self.create()
        self.natural(task_id, self.today - timedelta(days=1),
                     completed_at=self.today.isoformat() + 'T00:00:02')
        history = self.tm.task_history_rows(task_id)[-1]
        self.assertTrue(history['at'].startswith(self.today.isoformat()))
        self.assert_hours(10 * 22 - 2 * 2)

    def test_old_day_completion_after_today_completion_keeps_only_old_day_progress(self):
        task_id = self.create(total=2)
        self.natural(task_id)
        self.natural(task_id, self.today - timedelta(days=1))
        self.assertEqual(self.tm.db.get_task_by_zone('A')['category'], 0)
        self.assert_hours(10 * 22 - 1 * 2)

    def test_legacy_ledger_without_old_history_does_not_block_today_recovery(self):
        task_id = self.create(total=4)
        self.natural(task_id, self.today - timedelta(days=1))
        with self.tm.db.transaction() as conn:
            conn.execute('DELETE FROM task_history')
        self.tm.ensure_history_baseline()
        before = self.margin()
        self.natural(task_id)
        self.assertEqual(self.margin(), before,
                         '旧账本缺旧逐日历史不影响今天完整before/after证据')

    def test_add_delete_replace_and_move_out_immediately_change_current_plan(self):
        first = self.create(total=1)
        self.natural(first)
        self.assert_hours(218)
        second = self.create(zone='B', total=2, hours=3)
        self.assert_hours(212)
        self.assertTrue(self.tm.delete_task_by_zone('B', task_id=second))
        self.assert_hours(218)
        replacement = self.create(total=2, hours=4, replace=True)
        self.assertNotEqual(replacement, first)
        self.assert_hours(212)
        self.assertTrue(self.tm.move_out_area_unavailable(replacement, self.today.isoformat()))
        self.assertEqual(self.margin()['reason'], 'no_pending_tasks')

    def test_get_stats_is_read_only_for_tasks_history_ledger_and_metadata(self):
        task_id = self.create()
        self.natural(task_id)

        def tables():
            with closing(sqlite3.connect(self.root / 'tasks.db')) as conn:
                return {name: conn.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                        for name in ('tasks', 'task_history',
                                     'task_completion_ledger', 'tasks_meta')}

        before = tables()
        for _ in range(3):
            self.assert_hours(214)
        self.assertEqual(tables(), before)

    def test_missing_recovery_history_returns_unknown_but_retains_other_stats(self):
        task_id = self.create(total=1)
        self.natural(task_id)
        with self.tm.db.transaction() as conn:
            conn.execute("DELETE FROM task_history WHERE event='completed'")
        result = self.tm.get_stats()
        self.assertEqual(result['schedule_margin']['reason'], 'baseline_unavailable')
        self.assertIsNone(result['schedule_margin']['hours'])
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['remaining_time'], 0)

    def test_legacy_today_flag_without_ledger_does_not_guess_a_day(self):
        self.create()
        with self.tm.db.transaction() as conn:
            conn.execute('UPDATE tasks SET days_done=1, today_done=1, last_done_date=?',
                         (self.today.isoformat(),))
        self.assertTrue(self.tm.load_tasks())
        self.assertEqual(self.margin()['reason'], 'baseline_unavailable')

    def test_read_failure_only_disables_new_metric(self):
        self.create()
        with patch.object(self.tm.db, 'coherent_read', side_effect=sqlite3.OperationalError('offline')):
            result = self.tm.get_stats()
        self.assertEqual(result['schedule_margin']['reason'], 'baseline_unavailable')
        self.assertEqual(result['remaining_time'], 6)

    def test_stale_published_rows_are_not_joined_to_new_completion_history(self):
        task_id = self.create()
        stale_rows = self.tm.get_snapshot()[1]
        self.natural(task_id)
        result = self.tm._compute_stats_from_rows(stale_rows, self.today)
        self.assertEqual(result['remaining_time'], 6)
        self.assertEqual(result['schedule_margin']['reason'], 'baseline_unavailable')

    def test_external_commit_between_selects_cannot_mix_rows_and_history(self):
        task_id = self.create(total=1)
        expected = self.margin()
        reached = threading.Event()
        committed = threading.Event()
        original_read = self.tm.db.coherent_read
        writer_errors = []

        class ConnectionGate:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if sql.startswith('SELECT h.*'):
                    reached.set()
                    if not committed.wait(5):
                        raise AssertionError('外部写者未提交')
                return self.conn.execute(sql, *args)

        @contextmanager
        def gated_read():
            with original_read() as conn:
                yield ConnectionGate(conn)

        def writer():
            try:
                if not reached.wait(5):
                    raise AssertionError('读者未进入历史读取')
                # 第二个真实管理器/连接不共享本管理器的线程锁。
                other = self.manager()
                try:
                    outcome = other.settle_completion(
                        task_id=task_id, business_day=self.today.isoformat(),
                        run_id='external', settlement_guard=other.progress_guard(task_id),
                        effective_seconds=7200, target_seconds=7200, today=self.today)
                    if outcome['status'] != other.COMPLETION_CREDITED:
                        raise AssertionError(outcome)
                finally:
                    other.shutdown()
            except Exception as exc:
                writer_errors.append(exc)
            finally:
                committed.set()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(self.tm.db, 'coherent_read', gated_read):
                actual = self.margin()
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertFalse(writer_errors, writer_errors)
        self.assertEqual(actual, expected)
        self.assertEqual(self.tm.db.get_task_by_zone('A')['category'], 0,
                         '外部事务确实在读者两条SELECT之间提交')

    def test_api_three_query_routes_capture_application_business_day_once(self):
        from app.api import tasks as api

        self.create()
        business_day = self.today + timedelta(days=1)
        owner = SimpleNamespace(clock=SimpleNamespace(wall=lambda: SimpleNamespace(date=lambda: business_day)))
        for route in (api.get_tasks, api.get_tasks_detail, api.get_task_stats):
            with self.subTest(route=route.__name__), \
                 patch.object(api, 'get_task_manager', return_value=self.tm), \
                 patch.object(api, 'get_application_owner', return_value=owner), \
                 patch.object(api, '_business_day', wraps=api._business_day) as clock:
                result = self.loop.run_until_complete(route())
                self.assertEqual(clock.call_count, 1)
                self.assertEqual(result['business_date'], business_day.isoformat())
                self.assertEqual(result['schedule_margin']['business_date'], business_day.isoformat())
                self.assertEqual(result['schedule_margin']['hours'], 9 * 22 - 6)


if __name__ == '__main__':
    unittest.main()
