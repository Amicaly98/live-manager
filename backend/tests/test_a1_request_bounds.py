"""A1：请求的有界性/可取消性 + 健康可达（慢请求不占死服务）。"""

import json
import threading
import time
from pathlib import Path

import pytest
import requests as requests_lib

from app.core.live_controller import BilibiliApi
from conftest import FakeResp

_COOKIE = str(Path("nonexist-cookies.json"))


def test_req_bounded_without_cancel(monkeypatch):
    """无取消上下文（面板查询语义）：网络错误最多 3 次有界重试后返回失败。"""
    api = BilibiliApi(cookie_file=_COOKIE)
    attempts = {"n": 0}

    def fake_get(url, **kwargs):
        attempts["n"] += 1
        raise requests_lib.exceptions.ConnectionError("simulated network down")

    monkeypatch.setattr(requests_lib, "get", fake_get)
    # 把退避 sleep 缩短，避免测试变慢
    monkeypatch.setattr(time, "sleep", lambda s: None)

    ok, resp = api._req("GET", "https://fake.bilibili.com/x")
    assert ok is False
    assert attempts["n"] == 3, f"应有界重试 3 次，实际 {attempts['n']}"
    assert resp.get("retryable") is True


def test_req_cancellable_in_background(monkeypatch):
    """有取消上下文（后台开播语义）：无限等待恢复，停止事件立即打断。"""
    api = BilibiliApi(cookie_file=_COOKIE)
    cancel = threading.Event()
    attempts = {"n": 0}

    def fake_get(url, **kwargs):
        attempts["n"] += 1
        raise requests_lib.exceptions.ConnectionError("simulated network down")

    monkeypatch.setattr(requests_lib, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    result = {}

    def run():
        with api.retry_network_until_cancelled(cancel):
            result["resp"] = api._req("GET", "https://fake.bilibili.com/x")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    assert attempts["n"] >= 1, "取消前应至少重试过一次（无限重试语义）"
    cancel.set()
    t.join(timeout=5)
    assert not t.is_alive(), "停止事件应立即打断重试等待"
    ok, resp = result["resp"]
    assert ok is False
    assert resp.get("cancelled") is True


def test_req_cancellation_checked_before_send(monkeypatch):
    """停止先于请求发出：_req 在发送前检查取消，根本不发请求。"""
    api = BilibiliApi(cookie_file=_COOKIE)
    cancel = threading.Event()
    cancel.set()
    sent = {"n": 0}

    def fake_get(url, **kwargs):
        sent["n"] += 1
        return FakeResp({"code": 0})

    monkeypatch.setattr(requests_lib, "get", fake_get)
    with api.retry_network_until_cancelled(cancel):
        ok, resp = api._req("GET", "https://fake.bilibili.com/x")
    assert sent["n"] == 0, "已取消的操作不应发出网络请求"
    assert resp.get("cancelled") is True


def test_http_status_classification(monkeypatch):
    """HTTP 408/429/5xx 视为可重试上游故障；4xx 视为平台拒绝（不重试）。"""
    api = BilibiliApi(cookie_file=_COOKIE)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    # 5xx → 重试后放弃
    n = {"v": 0}
    def fake_get_5xx(url, **kwargs):
        n["v"] += 1
        return FakeResp({"code": -1}, status_code=502)
    monkeypatch.setattr(requests_lib, "get", fake_get_5xx)
    ok, resp = api._req("GET", "https://fake.bilibili.com/x")
    assert n["v"] == 3 and ok is False

    # 4xx → 不重试，直接平台拒绝
    n2 = {"v": 0}
    def fake_get_4xx(url, **kwargs):
        n2["v"] += 1
        return FakeResp({"code": -1}, status_code=412)
    monkeypatch.setattr(requests_lib, "get", fake_get_4xx)
    ok2, resp2 = api._req("GET", "https://fake.bilibili.com/x")
    assert n2["v"] == 1 and ok2 is False
    assert "平台拒绝" in resp2.get("msg", "")
