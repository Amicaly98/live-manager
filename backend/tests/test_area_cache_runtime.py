"""分区缓存落位/原子写/打包种子 行为测试（桌面版）。

与服务器版同一套行为约定，本仓侧额外覆盖"打包种子"：
- 种子是**构建输入**（仓库外注入），只在缓存缺失时复制一次，绝不覆盖用户数据；
- 没有种子时面板照常启动，分区暂不可用有明确状态。

全部使用 tmp 数据目录 + 注入的平台响应，不联网、不触碰真实账号数据。
"""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

from app.core.area_data import (
    AREA_CACHE_FILENAME, copy_seed_if_missing, read_cache, validate_areas,
    write_cache)
from app.core.live_controller import AreaLoader


SEED_AREAS = [
    {"id": 1, "name": "网游", "parent_id": 0, "parent_name": "网游",
     "children": [{"id": 11, "name": "王者荣耀", "parent_id": 1,
                   "parent_name": "网游", "children": []}]},
]

API_PAYLOAD = [
    {"id": 6, "name": "购物", "list": [{"id": 601, "name": "带货"}]},
    {"id": 9, "name": "虚拟主播", "list": [{"id": 371, "name": "聊天歌回"}]},
]


class InjectableAreas:
    """只提供 get_areas 的可注入替身（不联网）。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def get_areas(self):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return True, {'code': 0, 'data': []}


@pytest.fixture()
def workspace():
    """临时数据目录 + 该目录内的缓存路径。"""
    tmp = Path(tempfile.mkdtemp(prefix='desktop-areacache-'))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


# ---------------- 1) 已有缓存 + 离线能加载；种子不覆盖 ----------------

def test_existing_cache_loads_offline(workspace):
    cache = _write(workspace / AREA_CACHE_FILENAME, SEED_AREAS)
    loader = AreaLoader(area_file=str(cache))
    assert loader.available is True
    assert loader.status == 'loaded'
    assert [a['name'] for a in loader.areas] == ['网游']


def test_seed_does_not_overwrite_existing_cache(workspace):
    cache = _write(workspace / AREA_CACHE_FILENAME, SEED_AREAS)
    before = cache.read_bytes()
    seed = _write(workspace / 'seed.json', API_PAYLOAD)
    assert copy_seed_if_missing(cache, seed) == 'kept_new'
    assert cache.read_bytes() == before, '用户已有分区数据绝不能被种子覆盖'


# ---------------- 2) 无缓存 + 无网络：状态明确、不阻塞、不写文件 ----------------

def test_missing_cache_reports_status_without_writing(workspace):
    target = workspace / AREA_CACHE_FILENAME
    loader = AreaLoader(area_file=str(target))
    assert loader.areas == []
    assert loader.available is False
    assert loader.status == 'cache_missing'
    assert target.exists() is False, '只加载不应凭空生成缓存文件'


def test_corrupt_cache_is_reported_not_raised(workspace):
    target = workspace / AREA_CACHE_FILENAME
    target.write_text('{broken', encoding='utf-8')
    loader = AreaLoader(area_file=str(target))
    assert loader.areas == []
    assert loader.status.startswith('cache_corrupt')


def test_offline_refresh_returns_false(workspace):
    cache = workspace / AREA_CACHE_FILENAME
    loader = AreaLoader(area_file=str(cache))
    api = InjectableAreas([(False, {'code': -1})])
    assert loader.fetch_and_save_areas(api) is False
    assert loader.status == 'request_failed'
    assert cache.exists() is False


# ---------------- 3) 有效刷新落盘 / 重启可读 / 搜索更新 ----------------

def test_refresh_persists_restart_readable_and_search_updates(workspace):
    cache = workspace / AREA_CACHE_FILENAME
    _write(cache, SEED_AREAS)
    loader = AreaLoader(area_file=str(cache))
    assert loader.fuzzy_search('王者荣耀'), '前置：旧数据可搜到'

    api = InjectableAreas([(True, {'code': 0, 'data': API_PAYLOAD})])
    assert loader.fetch_and_save_areas(api) is True
    assert cache.exists()

    restarted = AreaLoader(area_file=str(cache))
    assert {a['name'] for a in restarted.areas} == {'购物', '虚拟主播'}
    assert loader.fuzzy_search('王者荣耀') == [], '刷新后搜索缓存必须失效'
    assert [r['name'] for r in loader.fuzzy_search('带货')] == ['带货']


# ---------------- 4) 失败/空/非法响应保留最后可用数据 ----------------

@pytest.mark.parametrize('payload', [
    {'code': 0, 'data': []},
    {'code': -1},
    {'code': 0, 'data': [{'id': 7}]},          # 缺 name
    {'code': 0, 'data': [{'name': 'x', 'id': 'abc'}]},
])
def test_bad_responses_keep_last_good_data(workspace, payload):
    cache = _write(workspace / AREA_CACHE_FILENAME, SEED_AREAS)
    before = cache.read_bytes()
    loader = AreaLoader(area_file=str(cache))
    api = InjectableAreas([(True, payload)])
    assert loader.fetch_and_save_areas(api) is False
    assert loader.status in ('empty_response', 'invalid_payload', 'request_failed')
    assert cache.read_bytes() == before, '不得落盘坏数据'
    assert [a['name'] for a in loader.areas] == ['网游'], '内存数据保持最后可用值'


# ---------------- 5) 写入失败不半截；并发不产出坏文件 ----------------

def test_write_failure_reports_and_leaves_nothing(workspace):
    blocker = workspace / 'blocker'
    blocker.write_text('not a dir', encoding='utf-8')
    ok, err = write_cache(blocker / AREA_CACHE_FILENAME, SEED_AREAS)
    assert ok is False and err.startswith('write_failed')
    assert (blocker / AREA_CACHE_FILENAME).exists() is False


def test_refresh_reports_failure_when_persist_fails(workspace):
    blocker = workspace / 'blocker'
    blocker.write_text('not a dir', encoding='utf-8')
    loader = AreaLoader(area_file=str(blocker / AREA_CACHE_FILENAME))
    api = InjectableAreas([(True, {'code': 0, 'data': API_PAYLOAD})])
    assert loader.fetch_and_save_areas(api) is False
    assert loader.status == 'persist_failed'
    assert loader.areas == [], '落盘失败不得切换内存数据'


def test_concurrent_writes_never_produce_half_json(workspace):
    cache = _write(workspace / AREA_CACHE_FILENAME, SEED_AREAS)
    bad = [{'id': 'x', 'name': ''}]
    barrier = threading.Barrier(8)

    def worker(payload):
        barrier.wait()
        for _ in range(25):
            write_cache(cache, payload)

    threads = [threading.Thread(target=worker, args=(SEED_AREAS,)) for _ in range(6)]
    threads += [threading.Thread(target=worker, args=(bad,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), '并发写超时'

    areas, status = read_cache(cache)
    assert status == 'loaded', '并发结束后缓存必须仍可读'
    assert len(areas) == len(SEED_AREAS)
    assert list(cache.parent.glob(AREA_CACHE_FILENAME + '*.tmp')) == []


# ---------------- 6) 打包种子的三种边界 ----------------

def test_seed_missing_target_gets_seeded(workspace):
    cache = workspace / AREA_CACHE_FILENAME
    seed = _write(workspace / 'seed.json', SEED_AREAS)
    assert copy_seed_if_missing(cache, seed) == 'seeded'
    assert read_cache(cache)[0][0]['name'] == '网游'


def test_missing_seed_file_is_reported_not_faked(workspace):
    cache = workspace / AREA_CACHE_FILENAME
    assert copy_seed_if_missing(cache, workspace / 'nope.json') == 'seed_missing'
    assert cache.exists() is False, '没有种子不得伪造/创建假的分区数据'


def test_corrupt_seed_is_rejected(workspace):
    cache = workspace / AREA_CACHE_FILENAME
    seed = workspace / 'seed.json'
    seed.write_text('["broken", ,]', encoding='utf-8')
    action = copy_seed_if_missing(cache, seed)
    assert action.startswith('seed_rejected')
    assert cache.exists() is False


def test_second_run_keeps_user_cache(workspace):
    """第二次启动保留用户缓存（哪怕用户后来改过内容）。"""
    cache = workspace / AREA_CACHE_FILENAME
    seed = _write(workspace / 'seed.json', SEED_AREAS)
    assert copy_seed_if_missing(cache, seed) == 'seeded'
    _write(cache, API_PAYLOAD)  # 用户刷新/手工改过
    assert copy_seed_if_missing(cache, seed) == 'kept_new'
    assert read_cache(cache)[0][0]['name'] == '购物'


# ---------------- 7) run.spec 的种子输入规则持久化校验 ----------------

_SPEC = Path(__file__).resolve().parent.parent / 'run.spec'


def _spec_seed_resolver(env):
    import os
    src = _SPEC.read_text(encoding='utf-8')
    head = src.split('a = Analysis(')[0]
    ns = {'SPECPATH': str(_SPEC.parent), '__file__': str(_SPEC)}
    saved = dict(os.environ)
    try:
        for k in ('BILIBILI_AREA_SEED_FILE', 'BILIBILI_REQUIRE_AREA_SEED',
                  'BILIBILI_AREA_SEED_PROVENANCE'):
            os.environ.pop(k, None)
        os.environ.update(env)
        exec(compile(head, 'run.spec', 'exec'), ns)
        return ns['_resolve_seed']()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_spec_requires_explicit_seed_and_fails_loud(workspace, tmp_path):
    good = _write(workspace / 'seed.json', SEED_AREAS)
    # 显式注入 → 打包数据里带上该文件
    assert _spec_seed_resolver({'BILIBILI_AREA_SEED_FILE': str(good)}) != []
    # 路径不存在 → 立即失败（不静默兜底）
    for bad in [str(workspace / 'nope.json'), str(workspace)]:
        with pytest.raises(SystemExit):
            _spec_seed_resolver({'BILIBILI_AREA_SEED_FILE': bad})
    # 未设置 → 无种子但不报错（源码运行不需要）
    assert _spec_seed_resolver({}) == []
    # 未设置但发布要求必须带 → 硬失败
    with pytest.raises(SystemExit):
        _spec_seed_resolver({'BILIBILI_REQUIRE_AREA_SEED': '1'})
    # 设置了但结构非法 → 失败
    with pytest.raises(SystemExit):
        _spec_seed_resolver({'BILIBILI_AREA_SEED_FILE': str(_write(
            workspace / 'bad.json', {'not': 'a list'}))})


def test_spec_records_provenance(workspace):
    seed = _write(workspace / 'seed.json', SEED_AREAS)
    pv = workspace / 'provenance.txt'
    _spec_seed_resolver({'BILIBILI_AREA_SEED_FILE': str(seed),
                         'BILIBILI_AREA_SEED_PROVENANCE': str(pv)})
    text = pv.read_text(encoding='utf-8')
    assert 'source=' in text and 'sha256=' in text
    import hashlib
    assert hashlib.sha256(seed.read_bytes()).hexdigest() in text


def test_module_level_sanity():
    """结构校验入口的基本形状（与服务器版同一约定）。"""
    assert validate_areas([])[0] is False
    assert validate_areas({'areas': []})[0] is False
    assert validate_areas([{'id': 1}])[0] is False
    assert validate_areas(SEED_AREAS) == (True, '')
