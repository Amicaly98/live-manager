"""“改为待完成”的分区校验（2026-10-19）。

``TaskManager.update_task_fields`` 只在“改为待完成”转移（category 0→>0 或
today_done 1→0）时调用 ``area_state(zone)``：
- 分区不存在/已下架 → ``TaskMutationRejected(code='area_unavailable')``；
- 分区列表不可用 → ``code='area_index_unavailable'``；
- 普通编辑不触发校验；事务内抛出保证零写入。API 把该异常映射为 409。
"""

import unittest

from port_support.backend_environment import build_task_manager, isolated_environment

from app.api.tasks import _reject_mutation
from app.core.task_manager import TaskMutationRejected

MISSING = '已下架区'
VALID = '有效区'
DONE_TODAY = '今日已办区'


def _row(zone_name, *, category=1, total_days=2, days_done=0,
         today_done=None):
    return {'zone_name': zone_name, 'category': category,
            'total_days': total_days, 'days_done': days_done,
            'deadline_raw': '=DATE(2027,12,31)', 'today_done': today_done}


class AreaEditGuardTests(unittest.TestCase):
    def setUp(self):
        self.env = isolated_environment()
        self.tmp = self.env.__enter__()
        self.addCleanup(lambda: self.env.__exit__(None, None, None))
        self.manager = build_task_manager(self.tmp / 'data', [
            _row(MISSING, category=0, total_days=3, days_done=3),
            _row(VALID),
            _row(DONE_TODAY, today_done=1),
        ])
        self.addCleanup(self.manager.shutdown)

    def _task(self, zone_name):
        return next(t for t in self.manager.tasks if t.zone_name == zone_name)

    def _current(self, task_id):
        return next(t for t in self.manager.tasks if t.id == task_id)

    def _edits(self, task_id):
        return [e for e in self.manager.task_history_rows(task_id)
                if e['event'] in ('manual_progress', 'updated')]

    def test_reactivating_missing_zone_is_rejected_without_write(self):
        task = self._task(MISSING)
        with self.assertRaises(TaskMutationRejected) as ctx:
            self.manager.update_task_fields(
                MISSING, {'category': 2}, task_id=task.id,
                area_state=lambda zone: False)
        self.assertEqual(ctx.exception.code, 'area_unavailable')
        self.assertIn('已不存在或已下架', ctx.exception.reason)
        self.assertEqual(int(self._current(task.id).category), 0)
        self.assertEqual(self._edits(task.id), [], '拒绝路径不得写入历史')

    def test_reactivating_valid_zone_commits(self):
        task = self._task(MISSING)
        self.assertTrue(self.manager.update_task_fields(
            MISSING, {'category': 2}, task_id=task.id,
            area_state=lambda zone: True))
        self.assertEqual(int(self._current(task.id).category), 2)

    def test_today_done_reset_with_missing_zone_is_rejected(self):
        task = self._task(DONE_TODAY)
        with self.assertRaises(TaskMutationRejected) as ctx:
            self.manager.update_task_fields(
                DONE_TODAY, {'today_done': 0}, task_id=task.id,
                area_state=lambda zone: False)
        self.assertEqual(ctx.exception.code, 'area_unavailable')
        self.assertEqual(int(self._current(task.id).today_done or 0), 1)

    def test_empty_index_is_reported_as_unavailable(self):
        task = self._task(MISSING)
        with self.assertRaises(TaskMutationRejected) as ctx:
            self.manager.update_task_fields(
                MISSING, {'category': 2}, task_id=task.id,
                area_state=lambda zone: None)
        self.assertEqual(ctx.exception.code, 'area_index_unavailable')
        self.assertEqual(int(self._current(task.id).category), 0)

    def test_unrelated_edit_does_not_consult_area_state(self):
        task = self._task(MISSING)

        def boom(zone):
            raise AssertionError('普通编辑不应触发分区校验')

        self.assertTrue(self.manager.update_task_fields(
            MISSING, {'deadline_raw': '=DATE(2028,1,31)'}, task_id=task.id,
            area_state=boom))

    def test_rename_while_reactivating_checks_resulting_zone(self):
        task = self._task(MISSING)
        with self.assertRaises(TaskMutationRejected) as ctx:
            self.manager.update_task_fields(
                MISSING, {'category': 2}, task_id=task.id,
                rename_to='已下架区-改名', area_state=lambda zone: False)
        self.assertEqual(ctx.exception.code, 'area_unavailable')
        self.assertEqual(self._current(task.id).zone_name, MISSING)

        self.assertTrue(self.manager.update_task_fields(
            MISSING, {'category': 2}, task_id=task.id,
            rename_to='有效区-改名', area_state=lambda zone: zone == '有效区-改名'))
        updated = self._current(task.id)
        self.assertEqual(updated.zone_name, '有效区-改名')
        self.assertEqual(int(updated.category), 2)

    def test_api_maps_rejection_to_409(self):
        error = TaskMutationRejected(
            '该分区「X」已不存在或已下架，不能改为待完成',
            code='area_unavailable')
        http = _reject_mutation(error)
        self.assertEqual(http.status_code, 409)
        self.assertIn('分区', http.detail)


if __name__ == '__main__':
    unittest.main()
