"""API 层：HTTP 票据协议、409 过期重放、健康可达（FastAPI TestClient）。

不进入 lifespan（不初始化真实 TaskManager/EmailSender），全局依赖直接
注入合成 controller —— 测试完全离线。
"""

import time

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def api_client(controller, fake_api):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import dependencies

    dependencies.init_globals(None, controller, None)
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        dependencies.init_globals(None, None, None)


def test_operation_ticket_issued(api_client, controller):
    r = api_client.get("/api/live/operation-ticket")
    assert r.status_code == 200
    data = r.json()
    assert data["ticket"].startswith(f"{controller._boot_id}:")
    assert data["epoch"] == controller._control_epoch


def test_start_with_stale_ticket_returns_409(api_client, controller):
    ticket = controller.issue_operation()
    controller._advance_control_epoch()  # 模拟停止
    r = api_client.post("/api/live/start", json={"zone_name": "王者荣耀"},
                        headers={"X-Operation-Token": ticket})
    assert r.status_code == 409
    assert "响应已丢失" in r.json()["detail"]


def test_start_with_valid_ticket_accepted(api_client, controller, fake_api):
    controller.switch_partition = lambda zone: True
    r = api_client.get("/api/live/operation-ticket")
    ticket = r.json()["ticket"]
    r2 = api_client.post("/api/live/start",
                         json={"zone_name": "王者荣耀", "duration_seconds": 60},
                         headers={"X-Operation-Token": ticket})
    assert r2.status_code == 200
    assert r2.json()["success"] is True
    # 后台开播线程被接受
    deadline = time.time() + 5
    while time.time() < deadline and not (controller._is_starting or controller.is_streaming):
        time.sleep(0.05)
    assert controller._is_starting or controller.is_streaming


def test_stop_replay_only_confirms(api_client, controller):
    r1 = api_client.post("/api/live/stop", headers={"X-Operation-Token": "stop-tk-1"})
    assert r1.status_code == 200 and r1.json()["success"] is True
    # 同一票据在代际推进后重放：只确认，不再执行下播
    r2 = api_client.post("/api/live/stop", headers={"X-Operation-Token": "stop-tk-1"})
    assert r2.status_code == 200
    assert "重放确认" in r2.json()["message"]


def test_start_without_token_still_works_compat(api_client, controller, fake_api):
    """无票据（旧客户端）→ 空票据不拦截（协议向后兼容）。"""
    controller.switch_partition = lambda zone: True
    r = api_client.post("/api/live/start", json={"zone_name": "英雄联盟"})
    assert r.status_code == 200


def test_health_reachable(api_client):
    r = api_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_exposes_is_starting(api_client, controller):
    r = api_client.get("/api/live/status")
    assert r.status_code == 200
    assert "is_starting" in r.json()
