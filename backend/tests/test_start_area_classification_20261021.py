"""A changed numeric mapping is not evidence that the task's zone was delisted."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.session.effects import StartPlatform
from app.core.session_runtime.platform import PlatformHttpPort
from app.core.session_runtime.runtime import Job



class StartAreaClassificationTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(mono=lambda: 1000.0)
        self.effect = StartPlatform(op_id='start-1', run_id='run-1',
                                    room_id=5001, area_id=17,
                                    task_id=1, zone_name='有效区')

    def _execute(self, index, refresh=None, raw=None):
        api = SimpleNamespace(
            cookie_context=lambda _: nullcontext(),
            start_live_once_with_budget=Mock(return_value=(False, raw or {
                'code': -400, 'message': '请求错误'})))
        port = PlatformHttpPort(api, self.clock, sender=object(),
                                area_index=lambda: dict(index),
                                area_refresh=refresh)
        result = port.execute(Job(effect=self.effect,
                                   deadline_mono=self.clock.mono() + 5))
        self.assertEqual(api.start_live_once_with_budget.call_count, 1)
        self.assertEqual(self.effect.area_id, 17, 'the frozen target must not be rewritten')
        return result

    def test_existing_zone_with_different_id_is_not_delisted(self):
        result = self._execute({'有效区': 29})
        self.assertEqual(result.outcome, 'rejected')

    def test_refresh_changing_only_id_is_not_delisted(self):
        index = {'有效区': 17}
        refresh = Mock(side_effect=lambda *_: index.update({'有效区': 29}))
        result = self._execute(index, refresh)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(result.outcome, 'rejected')

    def test_first_refresh_is_allowed_at_low_monotonic_uptime(self):
        self.clock.mono = lambda: 10.0
        refresh = Mock(return_value=True)
        self.assertEqual(self._execute({'有效区': 17}, refresh).outcome, 'rejected')
        self.assertEqual(refresh.call_count, 1)

    def test_absent_zone_in_usable_directory_still_maps_to_delisted(self):
        result = self._execute({'其他区': 29})
        self.assertEqual(result.outcome, 'area_unavailable')

    def test_explicit_platform_delisting_still_maps_to_delisted(self):
        result = self._execute({'有效区': 17}, raw={
            'code': -400, 'message': '该分区已下架'})
        self.assertEqual(result.outcome, 'area_unavailable')

    def test_unknown_and_face_verification_do_not_trigger_delisting(self):
        for raw, outcome in [({'code': -1}, 'unknown'),
                             ({'code': 60043}, 'face_verify')]:
            with self.subTest(outcome=outcome):
                refresh = Mock()
                self.assertEqual(self._execute({'其他区': 29}, refresh, raw).outcome,
                                 outcome)
                refresh.assert_not_called()
