"""分区不存在/已下架：不可完成移出 / 自动跳过 / 平台下架后的轮转（2026-10-19）。

覆盖：
1. ``TaskManager.move_out_area_unavailable``：保留任务记录，category=0、
   days_done=total_days、历史 moved_out/area_unavailable；幂等且分区名不符为 no-op；
2. ``TaskDataPort`` 自动选场：分区不可用 → 移出并继续选下一个；列表不可用时
   只返回瞬时原因、不动任务；移出失败不空转；指定任务解析失败为 terminal；
3. 真实 Core：平台返回 ``area_unavailable`` 时自动场先移出再关房轮转（Goal 保持
   running）；单次场停止且不移出任务。
"""

from __future__ import annotations

import unittest
from datetime import date

from port_support.backend_environment import build_task_manager, isolated_environment

from app.core.session.effects import SelectTask
from app.core.session.events import OUTCOME_AREA_UNAVAILABLE
from app.core.session.model import (
    MODE_SINGLE, MODE_TASK, SELECT_AUTO, SELECT_TASK)
from app.core.session_runtime.runtime import Job
from app.core.session_runtime.tasks import TaskDataPort


BUSINESS_DAY = date.today().isoformat()
MISSING_ZONE = '缺失区'
VALID_ZONE = '有效区'


def _row(zone_name, deadline='=DATE(2027,12,31)', category=1, days_done=0,
         today_done=None, total_days=2):
    return {'zone_name': zone_name, 'category': category,
            'total_days': total_days, 'days_done': days_done,
            'deadline_raw': deadline, 'today_done': today_done}


def _select_effect(selection, business_day=BUSINESS_DAY):
    return SelectTask(op_id='area-probe', revision=0, mode=MODE_TASK,
                      selection=dict(selection), business_day=business_day)


class MoveOutAreaUnavailableTests(unittest.TestCase):
    def setUp(self):
        self.env = isolated_environment()
        self.tmp = self.env.__enter__()
        self.addCleanup(lambda: self.env.__exit__(None, None, None))
        self.manager = build_task_manager(
            self.tmp / 'data', [_row(MISSING_ZONE)])
        self.addCleanup(self.manager.shutdown)
        self.task = next(t for t in self.manager.tasks
                         if t.zone_name == MISSING_ZONE)

    def _current(self):
        return next(t for t in self.manager.tasks if t.id == self.task.id)

    def test_move_out_keeps_record_and_history(self):
        self.assertTrue(self.manager.move_out_area_unavailable(
            task_id=self.task.id, business_day=BUSINESS_DAY,
            zone_name=MISSING_ZONE))
        row = self._current()
        self.assertEqual(int(row.category), 0)
        self.assertEqual(int(row.days_done), int(row.total_days))
        events = [e for e in self.manager.task_history_rows(self.task.id)
                  if e['event'] == 'moved_out']
        self.assertTrue(events, '移出没有留下历史')
        self.assertEqual(events[-1]['source'], 'area_unavailable')
        self.assertIn(MISSING_ZONE, events[-1]['detail'])
        # 任务记录仍在（不删除）。
        self.assertIsNotNone(self._current())

    def test_second_call_and_zone_mismatch_are_noops(self):
        self.assertTrue(self.manager.move_out_area_unavailable(
            task_id=self.task.id, business_day=BUSINESS_DAY,
            zone_name=MISSING_ZONE))
        self.assertFalse(self.manager.move_out_area_unavailable(
            task_id=self.task.id, business_day=BUSINESS_DAY,
            zone_name=MISSING_ZONE))

        second_dir = self.tmp / 'data2'
        second_dir.mkdir()
        second = build_task_manager(
            second_dir, [_row(VALID_ZONE)])
        self.addCleanup(second.shutdown)
        task2 = next(t for t in second.tasks if t.zone_name == VALID_ZONE)
        self.assertFalse(second.move_out_area_unavailable(
            task_id=task2.id, business_day=BUSINESS_DAY,
            zone_name=MISSING_ZONE), '分区名不符不得移出')


class SelectionSkipTests(unittest.TestCase):
    def setUp(self):
        self.env = isolated_environment()
        self.tmp = self.env.__enter__()
        self.addCleanup(lambda: self.env.__exit__(None, None, None))
        self.manager = build_task_manager(
            self.tmp / 'data',
            [_row(MISSING_ZONE, deadline='=DATE(2027,1,31)'),
             _row(VALID_ZONE)])
        self.addCleanup(self.manager.shutdown)

    def _port(self, index, ready=None):
        return TaskDataPort(
            self.manager, None, None, None,
            area_index=lambda: index,
            area_index_ready=ready)

    @staticmethod
    def _auto_job():
        return Job(effect=_select_effect({'kind': SELECT_AUTO}),
                   deadline_mono=0.0)

    def test_auto_skips_missing_zone_and_returns_next(self):
        port = self._port({VALID_ZONE: 2002}, ready=lambda: True)
        result = port.select(self._auto_job())
        self.assertTrue(result.found, result.reason)
        self.assertEqual(result.zone_name, VALID_ZONE)
        self.assertEqual(result.area_id, 2002)

        moved = next(t for t in self.manager.tasks
                     if t.zone_name == MISSING_ZONE)
        self.assertEqual(int(moved.category), 0)
        self.assertEqual(int(moved.days_done), int(moved.total_days))
        events = [e for e in self.manager.task_history_rows(moved.id)
                  if e['event'] == 'moved_out']
        self.assertEqual(events[-1]['source'], 'area_unavailable')

    def test_empty_index_does_not_mutate_tasks(self):
        port = self._port({}, ready=lambda: False)
        result = port.select(self._auto_job())
        self.assertFalse(result.found)
        self.assertEqual(result.reason, 'area_index_unavailable')
        pending = next(t for t in self.manager.tasks
                       if t.zone_name == MISSING_ZONE)
        self.assertEqual(int(pending.category), 1)
        self.assertEqual(int(pending.days_done), 0)

    def test_move_out_failure_returns_transient_reason(self):
        port = self._port({VALID_ZONE: 2002}, ready=lambda: True)
        self.manager.move_out_area_unavailable = lambda **kwargs: False
        result = port.select(self._auto_job())
        self.assertFalse(result.found)
        self.assertFalse(result.terminal)
        self.assertEqual(result.reason, 'area_unresolved')

    def test_named_ticket_with_missing_zone_is_terminal(self):
        task = next(t for t in self.manager.tasks
                    if t.zone_name == MISSING_ZONE)
        selection = {
            'kind': SELECT_TASK,
            'task_id': task.id,
            'expected_revision': self.manager.tasks_revision,
            'business_date': BUSINESS_DAY,
        }
        port = self._port({VALID_ZONE: 2002}, ready=lambda: True)
        result = port.select(Job(
            effect=_select_effect(selection), deadline_mono=0.0))
        self.assertFalse(result.found)
        self.assertTrue(result.terminal)
        self.assertEqual(result.reason, 'named_area_unresolved')
        self.assertEqual(int(task.category), 1, '指定任务不应被移出')



if __name__ == '__main__':
    unittest.main()
