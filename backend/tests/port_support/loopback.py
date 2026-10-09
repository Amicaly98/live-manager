"""阶段 3A 的**本地真实 HTTP 服务**：真实收请求、独立保持房间状态、可丢回包。

它是验收用的"平台"，只跑在 ``127.0.0.1`` 上，不连任何真实账号/端点：

- **物理状态与响应投递分开**：请求受理后先施加真实副作用（开房/关房），再决定
  回包头/体是否延迟、是否整条丢掉 —— 因此"远端动作已经发生、答复丢失"可以被
  如实表达，而不是靠本地"回滚"。
- **逐项可编程**：每个端点可以排若干 ``Plan``（业务码、HTTP 状态、延迟、丢弃、
  非 JSON 原文）。
- **并发可数**：记录 ``max_inflight``，用来直接证明"同一瞬间至多一项物理调用"。
- 请求轨迹只记方法/路径/房间号/时刻，**不记请求头与 cookie**。
"""

from __future__ import annotations

import json
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit


@dataclass
class Plan:
    """一个端点的下一次行为。字段为 None 时用该端点的默认行为。"""

    code: Optional[int] = None
    body: Optional[dict] = None
    raw_body: Optional[str] = None      # 非 JSON 原文（"无法解析响应"用例）
    status: int = 200
    delay: float = 0.0                  # 写出响应头之前先等（连接建立、无响应头）
    body_delay: float = 0.0             # 头已写出、体再等（延迟响应体）
    drop: bool = False                  # 受理后直接断开，不回任何字节
    side: Optional[str] = None          # 'open' | 'close' | 'none'；None = 默认
    area_id: Optional[int] = None       # 开房时实际使用的分区（默认跟请求走）


@dataclass
class Request:
    seq: int
    method: str
    path: str
    query: str
    endpoint: str
    at_mono: float
    room_id: Optional[int] = None
    requested_area: Optional[int] = None
    room_open_before: bool = False
    reply_code: Optional[int] = None
    dropped: bool = False
    elapsed: float = 0.0


class _QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        # 跳过 HTTPServer.server_bind 里的 socket.getfqdn（反向 DNS 查询）：
        # 验收期间不需要它，也不能让一次名字解析拖慢或漏到网络出口上。
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address) -> None:
        """客户端在慢响应期间超时断开是**预期**行为，不该刷一屏 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


_ENDPOINTS = (
    ('ts', '/click/now'),
    ('version', 'getHomePageLiveVersion'),
    ('start', '/startLive'),
    ('stop', '/stopLive'),
    ('query', '/get_info'),
)


def endpoint_of(path: str) -> str:
    for name, marker in _ENDPOINTS:
        if marker in path:
            return name
    return 'other'


class LoopbackPlatform:
    """本地平台：真实 HTTP、独立房间状态、可编程响应投递。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: Optional[_QuietHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

        self.requests: List[Request] = []
        self._seq = 0
        self.inflight = 0
        self.max_inflight = 0

        #: room_id -> {'open': bool, 'area_id': Optional[int]}
        self.rooms: Dict[int, Dict[str, Any]] = {}
        #: endpoint -> [Plan, ...]
        self.plans: Dict[str, List[Plan]] = {}
        #: endpoint -> Event；置位时该端点的请求先阻塞在闸门后
        self.gates: Dict[str, threading.Event] = {}
        #: 所有端点共用的闸门
        self.global_gate: Optional[threading.Event] = None
        #: 每个端点的"请求已到达"事件（测试等待"服务端确实收到并卡住"）
        self.arrived: Dict[str, threading.Event] = {}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> 'LoopbackPlatform':
        self._server = _QuietHTTPServer(('127.0.0.1', 0), _Handler)
        self._server.owner = self                       # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.02},
                                        name='p3a-loopback', daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    # ---------------------------------------------------------------- 编程

    def plan(self, endpoint: str, **kwargs: Any) -> None:
        self.plans.setdefault(endpoint, []).append(Plan(**kwargs))

    def take_plan(self, endpoint: str) -> Plan:
        with self._lock:
            queue = self.plans.get(endpoint) or []
            return queue.pop(0) if queue else Plan()

    def block(self, endpoint: str) -> threading.Event:
        """让该端点的请求阻塞，直到返回的 Event 被置位。"""
        gate = threading.Event()
        self.gates[endpoint] = gate
        return gate

    def unblock(self, endpoint: str) -> None:
        gate = self.gates.pop(endpoint, None)
        if gate is not None:
            gate.set()

    def wait_arrived(self, endpoint: str, timeout: float = 5.0) -> bool:
        return self._arrive_event(endpoint).wait(timeout)

    def _arrive_event(self, endpoint: str) -> threading.Event:
        with self._lock:
            return self.arrived.setdefault(endpoint, threading.Event())

    def _wait_gate(self, endpoint: str) -> None:
        for gate in (self.global_gate, self.gates.get(endpoint)):
            if gate is not None:
                gate.wait(timeout=30.0)

    # ---------------------------------------------------------------- 房间

    def room(self, room_id: int) -> Dict[str, Any]:
        with self._lock:
            return self.rooms.setdefault(room_id, {'open': False, 'area_id': None})

    def open_room_externally(self, room_id: int, area_id: Optional[int]) -> None:
        room = self.room(room_id)
        with self._lock:
            room['open'] = True
            room['area_id'] = area_id

    def set_room(self, room_id: int, open_: bool,
                 area_id: Optional[int] = None) -> None:
        room = self.room(room_id)
        with self._lock:
            room['open'] = bool(open_)
            room['area_id'] = area_id if open_ else None

    def room_open(self, room_id: int) -> bool:
        return bool(self.room(room_id)['open'])

    def room_area(self, room_id: int) -> Optional[int]:
        return self.room(room_id)['area_id']

    # ---------------------------------------------------------------- 轨迹

    def calls(self, endpoint: Optional[str] = None) -> List[Request]:
        with self._lock:
            items = list(self.requests)
        return [r for r in items if endpoint is None or r.endpoint == endpoint]

    def count(self, endpoint: str) -> int:
        return len(self.calls(endpoint))

    def paths(self) -> List[str]:
        return [r.path for r in self.calls()]

    def timeline(self) -> List[Dict[str, Any]]:
        return [{'seq': r.seq, 'endpoint': r.endpoint, 'path': r.path,
                 'room_id': r.room_id, 'requested_area': r.requested_area,
                 'reply_code': r.reply_code, 'dropped': r.dropped,
                 'at_mono': round(r.at_mono, 3), 'elapsed': round(r.elapsed, 3)}
                for r in self.calls()]

    # ---------------------------------------------------------------- 处理

    def _record(self, endpoint: str, path: str, query: str, room_id,
                requested_area) -> Request:
        with self._lock:
            self._seq += 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            request = Request(seq=self._seq, method='', path=path, query=query,
                              endpoint=endpoint, at_mono=time.monotonic(),
                              room_id=room_id, requested_area=requested_area)
            self.requests.append(request)
        self._arrive_event(endpoint).set()
        return request

    def _finish(self, request: Request, reply_code, dropped: bool) -> None:
        with self._lock:
            self.inflight -= 1
            request.reply_code = reply_code
            request.dropped = dropped
            request.elapsed = time.monotonic() - request.at_mono

    def handle(self, handler: 'BaseHTTPRequestHandler', method: str) -> None:
        parsed = urlsplit(handler.path)
        endpoint = endpoint_of(parsed.path)
        length = int(handler.headers.get('Content-Length') or 0)
        raw = handler.rfile.read(length) if length else b''
        form = parse_qs(raw.decode('utf-8', 'replace')) if raw else {}
        query = parse_qs(parsed.query)
        room_raw = (form.get('room_id') or query.get('room_id') or [None])[0]
        try:
            room_id = int(room_raw) if room_raw is not None else None
        except (TypeError, ValueError):
            room_id = None
        area_raw = (form.get('area_v2') or [None])[0]
        try:
            requested_area = int(area_raw) if area_raw is not None else None
        except (TypeError, ValueError):
            requested_area = None

        request = self._record(endpoint, parsed.path, parsed.query, room_id,
                               requested_area)
        request.room_open_before = (room_id is not None
                                    and bool(self.room(room_id)['open']))
        reply_code: Optional[int] = None
        dropped = False
        try:
            self._wait_gate(endpoint)
            plan = self.take_plan(endpoint)
            # 先受理并施加**真实副作用**，再决定回包怎么走：这样"动作已经发生、
            # 答复丢失/迟到"才是被如实表达的，而不是靠本地"回滚"。
            payload = self._payload(endpoint, plan, room_id, requested_area)
            reply_code = plan.code if plan.code is not None else payload.get('code')
            if plan.drop:
                dropped = True
                reply_code = None
                handler.close_connection = True
                return
            if plan.delay > 0:
                time.sleep(plan.delay)
            body = (plan.raw_body if plan.raw_body is not None
                    else json.dumps(payload, ensure_ascii=False))
            data = body.encode('utf-8')
            handler.send_response(plan.status)
            handler.send_header('Content-Type', 'application/json; charset=utf-8')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            if plan.body_delay > 0:
                time.sleep(plan.body_delay)
            handler.wfile.write(data)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            dropped = True
        finally:
            # 每个请求一条连接：慢响应/超时断开不会在下一条 keep-alive 请求上
            # 抛出与业务无关的读取异常。
            handler.close_connection = True
            self._finish(request, reply_code, dropped)

    def _payload(self, endpoint: str, plan: Plan, room_id, requested_area) -> dict:
        if plan.body is not None:
            return plan.body
        if endpoint == 'ts':
            return {'code': plan.code if plan.code is not None else 0,
                    'data': {'now': int(time.time())}}
        if endpoint == 'version':
            return {'code': plan.code if plan.code is not None else 0,
                    'data': {'build': 4321, 'curr_version': '1.9.9'}}
        if endpoint == 'start':
            self._apply_side(plan.side or 'open', room_id, plan, requested_area)
            return {'code': plan.code if plan.code is not None else 0,
                    'message': '' if plan.code in (None, 0) else 'refused',
                    'data': {'room_id': room_id}}
        if endpoint == 'stop':
            self._apply_side(plan.side or 'close', room_id, plan, requested_area)
            return {'code': plan.code if plan.code is not None else 0,
                    'message': '' if plan.code in (None, 0) else 'refused'}
        if endpoint == 'query':
            room = self.room(room_id)
            open_ = bool(room['open'])
            body: Dict[str, Any] = {
                'code': plan.code if plan.code is not None else 0,
                'data': {
                    'room_id': room_id,
                    'live_status': 1 if open_ else 0,
                    'area_id': room['area_id'],
                },
            }
            return body
        return {'code': 0}

    def _apply_side(self, side: str, room_id: Optional[int], plan: Plan,
                    requested_area: Optional[int]) -> None:
        if room_id is None:
            return
        if side == 'open':
            area = plan.area_id if plan.area_id is not None else requested_area
            self.set_room(room_id, True, area)
        elif side == 'close':
            self.set_room(room_id, False)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'phase3a-loopback'
    sys_version = ''

    def do_GET(self) -> None:      # noqa: N802
        self._platform().handle(self, 'GET')

    def do_POST(self) -> None:     # noqa: N802
        self._platform().handle(self, 'POST')

    def _platform(self) -> LoopbackPlatform:
        return self.server.owner          # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        return
