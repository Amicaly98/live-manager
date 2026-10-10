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


# ==================== 已退役用例的集中登记（桌面移植阶段 1） ====================
#
# 桌面默认入口已由旧 ``LiveController`` 换成共享运行链
# （``ApplicationOwner → ControlService → RuntimeInstance → Core``，见
# ``app/core/session/`` 与 ``app/core/session_runtime/``）。下列模块断言的是**旧
# 控制器**的私有方法、旧票据协议（``/api/live/operation-ticket`` 已按设计返回
# 410）或旧控制器私有的构建/截断函数，因此不再适用于默认链路。
#
# 这里**不删除**它们（保留原始材料，便于对照），也**不**为让旧断言变绿而保留第二
# 套控制器；只在收集阶段跳过，并在下表登记每条业务守卫现在由谁承担。
#
# **登记"由新链承担"不等于有可运行的用例**（复核"测试退役范围"一节）。因此按
# 单个模块停收集之前先分两类处理：
#
# 1. 与旧控制器私有方法无关、且对应的能力**产品现在仍在用**的守卫，已拆出到
#    ``test_desktop_data_dir_legacy.py`` 并可在当前链上运行：
#    ``test_a7_e3_data_intent.py`` 的数据目录初始化 / 旧安装目录迁移 / 防覆盖三条，
#    以及旧 ``test_a6_ffmpeg_options.py`` / ``test_d6_log_rotation.py`` 对应的
#    ffmpeg argv 与媒体日志轮转（新链里分别由 ``media_playlist.LocalCommandBuilder``
#    与 ``media_port.OwnedMediaWatcher`` 承担）；
# 2. 其余断言的**被测对象**（旧控制器的私有方法 / 旧票据 / 旧监控线程）确实已不在
#    默认链路上，按下表登记为不适用。
#
# 真正落在桌面产品边界上的守卫（启动/退出/托盘/文件路径/单实例/区域缓存）继续跑，
# 见同目录未列出的模块、``test_desktop_data_dir_legacy.py`` 与
# ``test_desktop_port_phase1*.py``。
RETIRED_MODULES = {
    'test_api_live_protocol.py':
        '旧票据/取票协议；新协议见 app/api/live.py（令牌由客户端生成）',
    'test_a2_control_epoch.py':
        '旧控制代际票据；由 ControlService 操作令牌 + Core Goal revision 承担',
    'test_a4_monitor_semantics.py':
        '旧监控线程的观察语义；由 Core 的观察/校准路径承担',
    'test_a5_ownership.py':
        '旧控制器的 ffmpeg 所有权；新链由 MediaPort 的实例归属与回收门承担，'
        '但**桌面树内没有**对应的可运行用例（该套件在服务端仓库的媒体端口测试里）',
    'test_a6_ffmpeg_options.py':
        '旧 build_ffmpeg_command（已不在本仓库）；新 argv 见 '
        'test_desktop_data_dir_legacy.py 的 LocalCommandBuilder 用例',
    'test_a7_e3_data_intent.py':
        '旧控制器的停止意图与旧票据（桌面改为 stop_intent.json → 候选里的 '
        '.no_auto_resume，由 test_desktop_port_phase1*.py 承担）；其中不依赖旧控制器'
        '的数据目录/旧安装目录迁移三条守卫已拆出到 test_desktop_data_dir_legacy.py',
    'test_d6_log_rotation.py':
        '旧 _truncate_ffmpeg_log_inplace（已不在本仓库）；新链路见 '
        'test_desktop_data_dir_legacy.py 的 OwnedMediaWatcher 轮转用例',
    'test_closeout_handover_identity_20260928.py':
        '旧控制器交接身份；由 Core 的 Run/Goal 身份核对承担',
    'test_closeout_stop_boundaries_20260928.py':
        '旧控制器停止边界/受理冻结基线；由 ControlService 回执语义承担',
    'test_closeout_named_task_20260928.py':
        '旧控制器指定任务协议；由 ControlService 冻结三件套承担',
    'test_closeout_deferred_takeover_20260928.py':
        '旧控制器推迟接管窗口；由 Core 的 close/resume 步骤承担',
    'test_pending_session_stop.py':
        '旧控制器在途会话停止；由 Runtime actor 的唯一在途槽承担',
    'test_desktop_core_port_111.py':
        '旧控制器桌面收口语义；等价行为由共享链 + test_desktop_port_phase1 承担',
    'test_desktop_sync_matrix.py':
        '双仓同步矩阵的旧控制器列；迁移后同一矩阵应在共享链上重建（**未重建**）',
    # 2026-10-10 第二阶段：桌面通知措辞的替代守卫已在新链上重建
    # （真实 EmailSender 模板 + 队列演练，只换物理投递出口），恢复收集。
    'test_cross_repo_contracts.py':
        '共同契约仍走 stability-contracts/scenarios/adapter_desktop.py（旧控制器）；'
        '适配器需按共享链重接后才能恢复',
}


def pytest_ignore_collect(collection_path, config):   # noqa: ARG001
    return collection_path.name in RETIRED_MODULES


@pytest.fixture()
def fresh_state():
    """每个测试前清空 live_state.json（跨日检查等依赖它）。"""
    _clear_state_file()
    yield
    _clear_state_file()


@pytest.fixture()
def controller(fresh_state):
    """合成环境下的 LiveController（后台引导不发真实请求）。

    路径一律从 ``config`` 取**数据目录内**的绝对路径：旧夹具用相对默认值时会把
    分区缓存与停止标记写到进程工作目录（本仓库的 ``backend/``），既污染工作区，
    也不符合"运行数据只落数据目录"的口径。
    """
    from app.core.live_controller import LiveController
    lc = LiveController(task_manager=None,
                        state_file=str(state_file_path()),
                        area_file=str(area_file_path()))
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
