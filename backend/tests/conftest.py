"""conftest.py - 桌面版隔离测试环境

- 所有测试使用合成数据目录（BILIBILI_DATA_DIR 指向 pytest tmp），不触碰
  真实账号状态、不连接真实直播平台；
- 分区文件预置合成数据：LiveController 的后台引导线程看到非空分区即跳过
  在线拉取（不发任何真实平台请求）；
- Cookie 文件不存在 → is_logged_in=False → 不触发登录。
"""

import os
import sys
import json
from pathlib import Path

import pytest

# 必须在导入 app.core.config 之前设置数据目录（conftest 最先加载）
_TEST_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("BILIBILI_DATA_DIR", str(_TEST_ROOT / "_test_data"))

BACKEND_DIR = Path(__file__).resolve().parent.parent  # backend/tests → backend
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 数据目录在收集阶段就初始化（路径函数依赖它）
from app.core.config import init_data_dir, area_file_path, state_file_path  # noqa: E402

init_data_dir()


def _seed_area_file() -> None:
    """预置合成分区数据（避免任何真实平台请求）。"""
    f = area_file_path()
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps([
            {
                "id": 1, "name": "测试大区", "parent_id": 0,
                "parent_name": "测试大区",
                "children": [
                    {"id": 101, "name": "王者荣耀", "parent_id": 1,
                     "parent_name": "测试大区", "children": []},
                    {"id": 102, "name": "英雄联盟", "parent_id": 1,
                     "parent_name": "测试大区", "children": []},
                ],
            }
        ], ensure_ascii=False), encoding="utf-8")


def _clear_state_file() -> None:
    f = state_file_path()
    if f.exists():
        f.unlink()


_seed_area_file()


@pytest.fixture()
def fresh_state():
    """每个测试前清空 live_state.json（跨日检查等依赖它）。"""
    _clear_state_file()
    yield
    _clear_state_file()


@pytest.fixture()
def controller(fresh_state):
    """合成环境下的 LiveController（后台引导不发真实请求）。"""
    from app.core.live_controller import LiveController
    lc = LiveController(task_manager=None)
    # 等待后台引导线程跑完（无网络路径，应当立即结束）
    import time
    for _ in range(20):
        if not any(t.name == "LiveBootstrap" and t.is_alive()
                   for t in __import__("threading").enumerate()):
            break
        time.sleep(0.1)
    yield lc


class FakeResp:
    """requests.Response 的最小合成替身。"""

    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {"code": 0}
        self.status_code = status_code
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass


@pytest.fixture()
def fake_api(controller):
    """替换 controller.api 的网络方法为可控替身。

    retry_network_until_cancelled 转发给真实 BilibiliApi（上下文管理器
    逻辑属于 A1，替身不重复实现）。
    """
    real_api = controller.api

    class FakeApi:
        def __init__(self):
            self.calls = []
            self.start_live_result = (True, {"code": 0, "data": {
                "rtmp": {"addr": "rtmp://fake.push", "code": "?streamkey=test"}}})
            self.live_status_result = (True, {"code": 0, "data": {"live_status": 1}})
            self.stop_calls = []

        def retry_network_until_cancelled(self, cancel):
            return real_api.retry_network_until_cancelled(cancel)

        def get_csrf(self):
            return "fake-csrf"

        def start_live(self, room_id, area_id, csrf):
            self.calls.append(("start_live", room_id, area_id))
            return self.start_live_result

        def stop_live(self, room_id, csrf):
            self.stop_calls.append((room_id, csrf))
            return True, {"code": 0}

        def get_live_status(self, room_id):
            self.calls.append(("get_live_status", room_id))
            return self.live_status_result

        def get_push_url(self, room_id):
            return True, {"push_url": "rtmp://fake.push?streamkey=test"}

    fake = FakeApi()
    controller.api = fake
    return fake
