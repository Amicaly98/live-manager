"""自动选场的分区解析与目录可用性必须来自同一索引快照。"""

from __future__ import annotations

import unittest
from datetime import date

from port_support.backend_environment import build_task_manager, isolated_environment

from app.core.session.effects import SelectTask
from app.core.session.model import MODE_SINGLE, MODE_TASK, SELECT_AUTO, SELECT_SINGLE
from app.core.session_runtime.runtime import Job
from app.core.session_runtime.tasks import TaskDataPort


MISSING_ZONE = '缓存恢复中的分区'
VALID_ZONE = '稳定分区'


def _row(zone_name):
    return {'zone_name': zone_name, 'category': 1, 'total_days': 2,
            'days_done': 0, 'deadline_raw': '=DATE(2027,12,31)',
            'today_done': None}


class AreaSelectionSnapshotTests(unittest.TestCase):
    def test_unavailable_snapshot_cannot_be_overridden_by_a_second_read(self):
        env = isolated_environment()
        tmp = env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        manager = build_task_manager(
            tmp / 'data', [_row(MISSING_ZONE), _row(VALID_ZONE)])
        self.addCleanup(manager.shutdown)

        reads = 0

        def area_index():
            nonlocal reads
            reads += 1
            if reads == 1:
                # 这次选取读到的是未知/未加载状态。
                return {}
            # 刷新恰在两次旧式读取之间完成，且确认目标分区仍然存在。
            return {MISSING_ZONE: 1001, VALID_ZONE: 2002}

        port = TaskDataPort(
            manager, None, None, None,
            area_index=area_index,
            area_index_ready=lambda: bool(area_index()))
        effect = SelectTask(
            op_id='snapshot-probe', revision=0, mode=MODE_TASK,
            selection={'kind': SELECT_AUTO},
            business_day=date.today().isoformat())

        result = port.select(Job(effect=effect, deadline_mono=0.0))
        task = next(t for t in manager.tasks
                    if t.zone_name == MISSING_ZONE)

        self.assertFalse(result.found,
                         '未知快照应等待重试，不能跳过后继续下一任务')
        self.assertEqual(result.reason, 'area_index_unavailable')
        self.assertEqual(reads, 1,
                         '一次自动选场判定不得重读索引并混合不同状态')
        self.assertEqual(int(task.category), 1,
                         '目录未知时任务必须保持待完成')
        self.assertEqual(int(task.days_done), 0)
        history = [event for event in manager.task_history_rows(task.id)
                   if event['event'] == 'moved_out']
        self.assertEqual(history, [], '未知快照不得写入移出历史')

    def test_area_lookup_only_provider_keeps_its_authoritative_semantics(self):
        env = isolated_environment()
        tmp = env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        manager = build_task_manager(
            tmp / 'data', [_row(MISSING_ZONE), _row(VALID_ZONE)])
        self.addCleanup(manager.shutdown)
        port = TaskDataPort(
            manager, None, None, None,
            area_lookup=lambda zone: 2002 if zone == VALID_ZONE else None)
        effect = SelectTask(
            op_id='lookup-probe', revision=0, mode=MODE_TASK,
            selection={'kind': SELECT_AUTO},
            business_day=date.today().isoformat())

        result = port.select(Job(effect=effect, deadline_mono=0.0))
        missing = next(t for t in manager.tasks
                       if t.zone_name == MISSING_ZONE)

        self.assertTrue(result.found, result.reason)
        self.assertEqual(result.zone_name, VALID_ZONE)
        self.assertEqual(int(missing.category), 0,
                         '仅注入 area_lookup 时仍以该解析器作为唯一权威')

    def test_explicit_readiness_false_vetoes_a_nonempty_partial_index(self):
        env = isolated_environment()
        tmp = env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        manager = build_task_manager(
            tmp / 'data', [_row(MISSING_ZONE), _row(VALID_ZONE)])
        self.addCleanup(manager.shutdown)
        port = TaskDataPort(
            manager, None, None, None,
            area_index=lambda: {VALID_ZONE: 2002},
            area_index_ready=lambda: False)
        effect = SelectTask(
            op_id='readiness-veto', revision=0, mode=MODE_TASK,
            selection={'kind': SELECT_AUTO},
            business_day=date.today().isoformat())

        result = port.select(Job(effect=effect, deadline_mono=0.0))
        missing = next(t for t in manager.tasks
                       if t.zone_name == MISSING_ZONE)

        self.assertFalse(result.found,
                         '显式 readiness=False 必须否决移出')
        self.assertEqual(result.reason, 'area_index_unavailable')
        self.assertEqual(int(missing.category), 1)
        self.assertEqual(int(missing.days_done), 0)
        history = [event for event in manager.task_history_rows(missing.id)
                   if event['event'] == 'moved_out']
        self.assertEqual(history, [])

    def test_manual_preflight_uses_one_snapshot_and_rejects_empty_index(self):
        env = isolated_environment()
        tmp = env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        manager = build_task_manager(tmp / 'data', [_row(VALID_ZONE)])
        self.addCleanup(manager.shutdown)

        reads = 0

        def changing_index():
            nonlocal reads
            reads += 1
            return ({VALID_ZONE: 2002} if reads == 1 else {})

        port = TaskDataPort(
            manager, None, None, None, area_index=changing_index)
        reason = port.preflight_start(
            MODE_SINGLE, {'kind': SELECT_SINGLE, 'zone_name': VALID_ZONE})
        self.assertEqual(reason, '',
                         '有效首快照中的分区不能被后续空读误报下架')
        self.assertEqual(reads, 1, '手动预检一次判定只能读取一次索引')

        empty_reads = 0

        def empty_index():
            nonlocal empty_reads
            empty_reads += 1
            return {}

        empty_port = TaskDataPort(
            manager, None, None, None, area_index=empty_index)
        unavailable = empty_port.preflight_start(
            MODE_SINGLE, {'kind': SELECT_SINGLE, 'zone_name': VALID_ZONE})
        self.assertIn('分区列表不可用', unavailable)
        self.assertEqual(empty_reads, 1,
                         '空索引明确表示未加载，不能继续另读一次补授权')


if __name__ == '__main__':
    unittest.main()
