"""共享分区列表（AREA_CACHE_PATH）行为测试（2026-10-19）。

覆盖：
1. 环境变量优先于显式 data_dir 与 config 数据目录；
2. 未设置时保持每实例一份的历史语义；
3. 旧缓存导入落到共享目标；
4. 文件被原子替换后，运行时索引与面板加载器都无需重启即可见；
5. 缺失/损坏的共享文件只返回空能力，不抛错。

全部使用临时目录与注入的平台响应，不联网、不触碰真实账号数据。
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from port_support.backend_environment import FakePlatform, isolated_environment

import app.core.area_data as area_data
from app.core.config import area_file_path
from app.core.area_data import (
    AREA_CACHE_FILENAME, AREA_CACHE_PATH_ENV, area_cache_path,
    import_legacy_cache, read_cache, write_cache)
from app.core.live_controller import AreaLoader
from app.core.session_runtime.area_capability import (
    get_area_loader, reset_area_loader)
from app.core.session_runtime.area_reader import (
    load_area_index, reset_area_index_cache)


def _areas(zone, area_id, parent='共享类'):
    return [{
        'id': 10, 'name': parent, 'parent_id': 0, 'parent_name': parent,
        'children': [{'id': area_id, 'name': zone, 'parent_id': 10,
                      'parent_name': parent, 'children': []}],
    }]


def _api_payload(zone, area_id, parent='共享类'):
    return [{'id': 10, 'name': parent, 'list': [
        {'id': area_id, 'name': zone}]}]


class _InjectableAreas(FakePlatform):
    """只替换 get_areas；其余仍是禁止联网的测试替身。"""

    def __init__(self, cookie_file, results=None):
        super().__init__(cookie_file=str(cookie_file))
        self._results = list(results or [])
        self.calls = 0

    def get_areas(self):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return True, {'code': 0, 'data': []}


class _SharedCase(unittest.TestCase):
    def setUp(self):
        self._env = isolated_environment()
        self.tmp = self._env.__enter__()
        self.addCleanup(lambda: self._env.__exit__(None, None, None))
        self.data_dir = self.tmp / 'data'
        self.shared = self.tmp / 'shared' / AREA_CACHE_FILENAME
        patcher = mock.patch.dict(os.environ,
                                  {AREA_CACHE_PATH_ENV: str(self.shared)},
                                  clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_area_loader)
        self.addCleanup(reset_area_index_cache)


class PathResolutionTests(_SharedCase):
    def test_env_path_wins_over_explicit_data_dir(self):
        self.assertEqual(area_cache_path(self.data_dir), self.shared.resolve())

    def test_config_area_file_path_honors_env(self):
        self.assertEqual(area_file_path(), self.shared.resolve())

    def test_env_empty_keeps_per_instance_path(self):
        with mock.patch.dict(os.environ, {AREA_CACHE_PATH_ENV: ''}):
            self.assertEqual(area_cache_path(self.data_dir),
                             self.data_dir / AREA_CACHE_FILENAME)
            self.assertEqual(area_file_path(),
                             self.data_dir / AREA_CACHE_FILENAME)

    def test_import_legacy_cache_targets_shared_path(self):
        legacy = self.tmp / 'legacy'
        legacy.mkdir()
        ok, reason = write_cache(legacy / AREA_CACHE_FILENAME,
                                 _areas('旧导入区', 9001))
        self.assertTrue(ok, reason)
        actions = import_legacy_cache(candidates=[legacy])
        self.assertEqual([a for _, a in actions], ['imported'])
        areas, status = read_cache(self.shared)
        self.assertEqual(status, 'loaded')
        self.assertTrue(areas)


class HotReloadTests(_SharedCase):
    def test_load_area_index_reloads_after_atomic_rewrite(self):
        ok, reason = write_cache(self.shared, _areas('共享区A', 1001))
        self.assertTrue(ok, reason)
        self.assertEqual(load_area_index().get('共享区A'), 1001)

        ok, reason = write_cache(self.shared, _areas('共享区B', 1002))
        self.assertTrue(ok, reason)
        # 不重启、不手动清缓存：签名变化后自动重读。
        self.assertEqual(load_area_index().get('共享区B'), 1002)
        self.assertIsNone(load_area_index().get('共享区A'))

    def test_index_reader_does_not_cache_old_read_under_new_signature(self):
        old = _areas('old-zone', 4101)
        new = _areas('new-zone', 4102)
        ok, reason = write_cache(self.shared, old)
        self.assertTrue(ok, reason)
        real_read = area_data.read_cache
        replaced = False

        def read_then_replace(path):
            nonlocal replaced
            result = real_read(path)
            if not replaced:
                replaced = True
                ok, reason = write_cache(path, new)
                self.assertTrue(ok, reason)
            return result

        with mock.patch.object(area_data, 'read_cache',
                               side_effect=read_then_replace):
            first = load_area_index()
        self.assertEqual(first.get('old-zone'), 4101)
        # The old snapshot may serve this in-flight call, but must not be cached
        # under the signature of the file installed while the read was in flight.
        self.assertEqual(load_area_index().get('new-zone'), 4102)

    def test_area_loader_reloads_if_atomic_replace_follows_read(self):
        old = _areas('old-zone', 4201)
        new = _areas('new-zone', 4202)
        ok, reason = write_cache(self.shared, old)
        self.assertTrue(ok, reason)
        real_read = area_data.read_cache
        replaced = False

        def read_then_replace(path):
            nonlocal replaced
            result = real_read(path)
            if not replaced:
                replaced = True
                ok, reason = write_cache(path, new)
                self.assertTrue(ok, reason)
            return result

        with mock.patch.object(area_data, 'read_cache',
                               side_effect=read_then_replace):
            loader = AreaLoader(str(self.shared))
        self.assertTrue(loader.reload_if_changed())
        self.assertEqual(loader.get_area_id('new-zone'), 4202)

    def test_two_loaders_share_one_refreshed_list(self):
        ok, reason = write_cache(self.shared, _areas('刷新前区', 2001))
        self.assertTrue(ok, reason)
        loader_a = AreaLoader()
        loader_b = AreaLoader()
        self.assertTrue(loader_a.available)
        self.assertTrue(loader_b.available)
        self.assertEqual(loader_b.fuzzy_search('刷新后区'), [])

        api = _InjectableAreas(self.tmp / 'cookies.json',
                               [(True, {'code': 0,
                                        'data': _api_payload('刷新后区', 2002)})])
        self.assertTrue(loader_a.fetch_and_save_areas(api))
        # B 没刷新、没重启：查询入口按签名重载后可见 A 写入的共享内容。
        names = [item['name'] for item in loader_b.fuzzy_search('刷新后区')]
        self.assertIn('刷新后区', names)
        self.assertEqual(load_area_index().get('刷新后区'), 2002)

    def test_get_area_loader_sees_external_rewrite(self):
        ok, reason = write_cache(self.shared, _areas('外部改写前', 3001))
        self.assertTrue(ok, reason)
        loader = get_area_loader()
        self.assertIn('外部改写前',
                      [item['name'] for item in loader.fuzzy_search('外部改写前')])

        ok, reason = write_cache(self.shared, _areas('外部改写后', 3002))
        self.assertTrue(ok, reason)
        same = get_area_loader()
        self.assertIs(same, loader)
        self.assertIn('外部改写后',
                      [item['name'] for item in same.fuzzy_search('外部改写后')])

    def test_missing_or_corrupt_shared_file_is_empty_capability(self):
        self.assertEqual(load_area_index(), {})
        self.shared.parent.mkdir(parents=True, exist_ok=True)
        self.shared.write_text('{not json', encoding='utf-8')
        self.assertEqual(load_area_index(), {})
        loader = AreaLoader()
        self.assertFalse(loader.available)
        self.assertTrue(loader.status.startswith('cache_'))
