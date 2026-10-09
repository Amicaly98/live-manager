"""有界单轮传输：**一轮一份绝对截止**，覆盖解析、连接、TLS 握手、发送与读取。

它替换的不是"重试策略"，而是"一次物理调用怎么退出"这个接缝。旧默认
``BilibiliApi._req``（有限重试 ＋ 10 s socket 超时）一个字节都不动；只有处在
``bounded_attempt`` 里的那一轮才走这里。

**每一步实际怎么退出（先写清楚，再实现）：**

1. **名字解析**：``socket.getaddrinfo`` 在辅助线程里执行，调用线程只等
   ``截止 - 现在``。到点即放弃这次解析并返回 ``resolve_timeout`` —— 慢解析不会
   把唯一工作者继续按在旧请求上。健康路径上辅助线程毫秒级返回（可回收）；只有
   **超过截止仍未返回**的才计入孤儿，孤儿上限 ``_MAX_ORPHAN_RESOLVERS``，达到
   上限后不再新建：不会按轮无限堆积解析器。
2. **连接**：阻塞 ``connect``，但每次尝试前把 socket 超时设为**当前剩余**。
   连接本身是一次握手，超时即整轮退出；这里不用 0 超时（那会把 socket 变成
   非阻塞语义，掩盖"其实没时间了"）。
3. **TLS 握手**：非阻塞 ＋ ``SSLWantReadError``/``SSLWantWriteError`` ＋
   ``select`` 等到剩余时间为止 —— 慢速分片握手同样按同一份截止退出。
4. **发送**：手写 ``send`` 循环，每次发送前把超时重置为当前剩余。
5. **读取**：每次 ``recv`` 前把超时重置为当前剩余。剩余量随已用时间单调递减，
   所以"每 60 ms 收到 3 字节"这类慢字节流也会在截止处退出 —— 总时长不超过一份
   预算，不因为"一直在收到一点数据"就得到无限新时间。**EOF 不是"又收到一点
   数据"**：见下条。
6. **帧完整性**：读到 EOF 只是"对端不再发了"，不是"帧收完了"。三条口径都必须
   自己判足 —— 定长正文读不满声明的字节数、分块正文任一块的数据／块尾 CRLF／
   终止块缺失、任何一行在见到 CRLF 之前就 EOF，一律按协议失败退出
   （``status == 0``），**绝不**把可解析的 JSON 前缀当成完整响应去确认业务
   事实。合法"无长度、读到连接关闭"的正文仍按其原有口径读到 EOF。
7. **解析**：按已定长／分块／读到 EOF 三种口径读完响应体后在本地解析；格式不对
   按传输失败返回，**不伪造**业务码。

出口只有一个三元组 ``(status, body, error)``：``status == 0`` 表示**没有拿到任何
HTTP 回复**（预算耗尽、解析／连接／握手／读取失败、协议错误），非 0 是真实的 HTTP
状态码。远端是否已经受理动作不由这里回滚：调用方按原操作身份把它记成 unknown，
后续仍交 Core 查询处理。
"""

from __future__ import annotations

import json
import select
import socket
import ssl
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

#: 小于它就当作"没有时间了"。不用 0：0 会切换成非阻塞语义。
_EPSILON = 0.001

#: 单次 recv 的字节数（只影响分片，不影响总时长上界）。
_RECV_CHUNK = 65536

#: 头部行／头部总量／响应体的上界：对端不能用超长响应把内存吃光。
_MAX_LINE = 64 * 1024
_MAX_HEADER_BYTES = 256 * 1024
_MAX_BODY_BYTES = 8 * 1024 * 1024

#: 允许同时存在的"超时未返回"解析辅助线程数上限。
_MAX_ORPHAN_RESOLVERS = 2

#: 我们不重复发送的逐跳／自算头部。
_MANAGED_HEADERS = frozenset({'host', 'content-length', 'cookie', 'connection',
                              'accept-encoding', 'transfer-encoding'})

_ORPHAN_LOCK = threading.Lock()
_ORPHANS: set = set()

_SSL_CONTEXT: Optional[ssl.SSLContext] = None
_SSL_LOCK = threading.Lock()


class DeadlineExceeded(Exception):
    """整轮预留时间已经用完。它不是业务失败，也不是平台拒绝。"""


class _ProtocolError(Exception):
    """对端回了不合规的 HTTP；按传输失败处理，不猜业务码。"""


class _TooLarge(_ProtocolError):
    """响应超过本地上界。"""


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = ssl.create_default_context()
        return _SSL_CONTEXT


# ------------------------------------------------------------------ 名字解析


def resolve_within(host: str, port: int,
                   budget) -> Tuple[Optional[List[Any]], str]:
    """在**同一份预算**内解析；超时就放弃这次解析，而不是无限等下去。

    返回 ``(addrinfo, '')`` 或 ``(None, 原因)``。解析失败（含被网络守卫拦下）
    原样抛出，不在这里吞掉。
    """
    # 已经有两个"超时未返回"的解析在跑时**不再新建**解析线程：孤儿上限是硬上界，
    # 而不是"先堆积再回收"。这一次按未解析处理，核心随后仍按既有节奏重试。
    with _ORPHAN_LOCK:
        if len(_ORPHANS) >= _MAX_ORPHAN_RESOLVERS:
            return None, 'resolver_busy'

    box: Dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            box['addrinfo'] = socket.getaddrinfo(host, port, 0,
                                                 socket.SOCK_STREAM)
        except BaseException as exc:            # 含 NetworkBlocked：原样交回调用方
            box['error'] = exc
        finally:
            done.set()
            _drop_orphan(box.get('thread'))

    box['thread'] = thread = threading.Thread(target=worker, daemon=True,
                                              name='bounded-resolver')
    thread.start()
    if done.wait(max(0.0, budget.remaining())):
        if 'addrinfo' in box:
            return box['addrinfo'], ''
        error = box.get('error')
        if isinstance(error, OSError):
            return None, f'resolve_failed:{type(error).__name__}'
        raise error if error is not None else OSError('resolve failed')
    # 到点仍未返回：记为孤儿（它自己结束时会摘掉），本次按未解析处理。
    with _ORPHAN_LOCK:
        _ORPHANS.add(thread)
        if done.is_set():
            _ORPHANS.discard(thread)
    return None, 'resolve_timeout'


def _drop_orphan(thread: Any) -> None:
    if thread is None:
        return
    with _ORPHAN_LOCK:
        _ORPHANS.discard(thread)


def orphan_resolvers() -> int:
    """当前"超时未返回"的解析辅助线程数（供验收读取，不参与决策）。"""
    with _ORPHAN_LOCK:
        return len(_ORPHANS)


# ------------------------------------------------------------------ socket


def _connect(addrinfos: List[Any], budget) -> socket.socket:
    """逐个候选地址尝试，每次都用**当前剩余**做超时。"""
    last: Optional[BaseException] = None
    for family, socktype, proto, _canon, sockaddr in addrinfos:
        remaining = budget.remaining()
        if remaining <= _EPSILON:
            raise DeadlineExceeded('connect')
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            return sock
        except socket.timeout as exc:
            sock.close()
            raise DeadlineExceeded('connect') from exc
        except OSError as exc:
            last = exc
            sock.close()
    raise last if last is not None else OSError('connect failed')


def _ready(sock, want_read: bool, budget) -> bool:
    remaining = budget.remaining()
    if remaining <= _EPSILON:
        return False
    try:
        readable, writable, _ = select.select(
            [sock] if want_read else [], [] if want_read else [sock], [],
            remaining)
    except (OSError, ValueError):
        return False
    return bool(readable or writable)


def _handshake(tls: ssl.SSLSocket, budget) -> None:
    """非阻塞握手：每次等待都用当时**剩余**的时间，因此分片慢握手也有界。"""
    tls.setblocking(False)
    try:
        while True:
            try:
                tls.do_handshake()
                return
            except ssl.SSLWantReadError:
                if not _ready(tls, True, budget):
                    raise DeadlineExceeded('handshake')
            except ssl.SSLWantWriteError:
                if not _ready(tls, False, budget):
                    raise DeadlineExceeded('handshake')
    finally:
        tls.setblocking(True)


def _wrap_tls(sock: socket.socket, host: str, budget) -> ssl.SSLSocket:
    remaining = budget.remaining()
    if remaining <= _EPSILON:
        raise DeadlineExceeded('tls')
    sock.settimeout(remaining)
    tls = _ssl_context().wrap_socket(sock, server_hostname=host,
                                     do_handshake_on_connect=False)
    try:
        _handshake(tls, budget)
    except BaseException:
        try:
            tls.close()
        except OSError:
            pass
        raise
    return tls


def _send_all(sock, payload: bytes, budget) -> None:
    view = memoryview(payload)
    while view:
        remaining = budget.remaining()
        if remaining <= _EPSILON:
            raise DeadlineExceeded('send')
        sock.settimeout(remaining)
        try:
            sent = sock.send(view)
        except socket.timeout as exc:
            raise DeadlineExceeded('send') from exc
        if sent <= 0:
            raise OSError('send failed')
        view = view[sent:]


# ------------------------------------------------------------------ 读取


class _Reader:
    """按**当时剩余**时间读取的缓冲读者；每次 recv 前重置超时。

    完整性口径与等待口径是两回事：等待由 ``_budget`` 管（绝对截止、单调递减），
    完整性由下面三个方法自己判 —— 少一个字节都算截断，不把"已经收到的部分"
    当成"完整的帧"。
    """

    __slots__ = ('_sock', '_budget', '_buf', '_eof', '_seen')

    def __init__(self, sock, budget) -> None:
        self._sock = sock
        self._budget = budget
        self._buf = bytearray()
        self._eof = False
        #: 本连接累计收到过多少字节 —— 用来区分"对端一个字都没发"
        #: （no_response）与"发了一半就断"（帧截断）。
        self._seen = 0

    def _fill(self) -> bool:
        if self._eof:
            return False
        remaining = self._budget.remaining()
        if remaining <= _EPSILON:
            raise DeadlineExceeded('read')
        self._sock.settimeout(remaining)
        try:
            chunk = self._sock.recv(_RECV_CHUNK)
        except socket.timeout as exc:
            raise DeadlineExceeded('read') from exc
        if not chunk:
            self._eof = True
            return False
        if len(self._buf) + len(chunk) > _MAX_BODY_BYTES:
            raise _TooLarge('response too large')
        self._buf += chunk
        self._seen += len(chunk)
        return True

    def read_line(self) -> bytes:
        """读一个以 CRLF 结尾的行。

        **空行与 EOF 必须区分**：真的读到 ``\\r\\n`` 时返回 ``b''``；在见到
        CRLF 之前就 EOF（对端关连接／对端根本不发了）一律抛协议错误。旧实现把
        两种情形都返回 ``b''``，于是分块读取在 EOF 之后把"空"当成"块间空行"
        一直 ``continue`` —— 既不读 socket 也不再等截止，纯忙转，占住唯一
        工作者。这里把这条出口关掉。
        """
        while True:
            index = self._buf.find(b'\r\n')
            if index >= 0:
                line = bytes(self._buf[:index])
                del self._buf[:index + 2]
                return line
            if len(self._buf) > _MAX_LINE:
                raise _ProtocolError('header_line_too_long')
            if not self._fill():
                raise _ProtocolError('no_response' if not self._seen
                                     else 'eof_in_line')

    def take(self, count: int) -> bytes:
        """读**恰好** ``count`` 字节；不足就是截断，抛协议错误。

        旧实现在 EOF 处 ``break`` 并把短正文原样返回，调用方无从知道 HTTP 帧
        没收完，于是"声明长度还欠 50 字节"的响应被解析成已确认的业务事实。
        """
        while len(self._buf) < count:
            if not self._fill():
                raise _ProtocolError('short_body')
        data = bytes(self._buf[:count])
        del self._buf[:count]
        return data

    def read_to_eof(self) -> bytes:
        """合法的"无长度、读到连接关闭"正文：这里读到 EOF 是**预期**出口。"""
        while self._fill():
            pass
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def read_chunked(self) -> bytes:
        out = bytearray()
        while True:
            line = self.read_line()
            size_text = line.split(b';', 1)[0].strip()
            if not size_text:
                # 这个位置只允许出现"块大小"；空行不是合法分块帧（旧实现在这里
                # ``continue``，恰好把 EOF 也吞成了"空行"，成为永不退出的来源）。
                raise _ProtocolError('empty_chunk_size')
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise _ProtocolError('bad_chunk_size') from exc
            if size == 0:
                while True:                     # 读完终止块之后的 trailer
                    trailer = self.read_line()
                    if not trailer:
                        break
                return bytes(out)
            out += self.take(size)              # 数据块不足 size → 截断
            self.take(2)                        # 块尾 CRLF 缺失 → 截断
            if len(out) > _MAX_BODY_BYTES:
                raise _TooLarge('body_too_large')


# ------------------------------------------------------------------ HTTP


def _request_bytes(method: str, target: str, host_header: str,
                   headers: Dict[str, Any], cookies: Dict[str, Any],
                   body: Optional[bytes]) -> bytes:
    lines = [f'{method} {target} HTTP/1.1', f'Host: {host_header}']
    for name, value in headers.items():
        if str(name).lower() in _MANAGED_HEADERS:
            continue
        lines.append(f'{name}: {value}')
    if cookies:
        lines.append('Cookie: ' + '; '.join(
            f'{key}={value}' for key, value in cookies.items()))
    lines.append('Accept-Encoding: identity')
    lines.append('Connection: close')
    if body is not None:
        lines.append(f'Content-Length: {len(body)}')
    head = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace')
    return head + (body or b'')


def _read_response(reader: _Reader) -> Tuple[int, bytes]:
    status_line = reader.read_line()
    if not status_line:
        raise _ProtocolError('no_response')
    fields = status_line.split(b' ', 2)
    if len(fields) < 2 or not fields[0].upper().startswith(b'HTTP/'):
        raise _ProtocolError('bad_status_line')
    try:
        status = int(fields[1])
    except ValueError as exc:
        raise _ProtocolError('bad_status_code') from exc

    headers: Dict[str, str] = {}
    total = 0
    while True:
        line = reader.read_line()
        if not line:
            break
        total += len(line)
        if total > _MAX_HEADER_BYTES:
            raise _ProtocolError('headers too large')
        name, separator, value = line.partition(b':')
        if not separator:
            continue
        headers[name.decode('latin-1').strip().lower()] = \
            value.decode('latin-1').strip()

    encoding = headers.get('transfer-encoding', '').lower()
    if 'chunked' in encoding:
        body = reader.read_chunked()
    elif 'content-length' in headers:
        try:
            length = int(headers['content-length'])
        except ValueError as exc:
            raise _ProtocolError('bad content-length') from exc
        body = reader.take(max(0, length))
    else:
        body = reader.read_to_eof()
    return status, body


# ------------------------------------------------------------------ 出口


class BoundedHttpSender:
    """一次有界单轮 HTTP 的执行体。实例无状态，可被多个线程共用。

    调用约定（只依赖这几个参数，不读任何全局配置）::

        sender(method, url, params, data, headers, cookies, budget)
            -> (status: int, body: bytes, error: str)

    ``budget`` 只需要有 ``remaining() -> float``（秒），与平台端口用的是
    **同一份**对象，因此不会出现第二份计时。
    """

    __slots__ = ()

    def __call__(self, method: str, url: str, params: Optional[dict],
                 data: Optional[dict], headers: Dict[str, Any],
                 cookies: Dict[str, Any], budget
                 ) -> Tuple[int, bytes, str]:
        try:
            return self._send(method, url, params, data, headers, cookies,
                              budget)
        except DeadlineExceeded as exc:
            return 0, b'', f'deadline:{exc}'
        except _ProtocolError as exc:
            return 0, b'', f'protocol:{exc}'
        except ssl.SSLError as exc:
            return 0, b'', f'tls:{type(exc).__name__}'
        except socket.timeout:
            return 0, b'', 'timeout'
        except OSError as exc:
            return 0, b'', f'{type(exc).__name__}'
        except ValueError as exc:
            return 0, b'', f'{type(exc).__name__}'

    # -------------------------------------------------------------- 内部

    def _send(self, method, url, params, data, headers, cookies, budget
              ) -> Tuple[int, bytes, str]:
        if budget.remaining() <= _EPSILON:
            return 0, b'', 'expired'
        parts = urllib.parse.urlsplit(str(url).strip())
        scheme = (parts.scheme or 'http').lower()
        if scheme not in ('http', 'https'):
            return 0, b'', 'bad_scheme'
        host = parts.hostname or ''
        if not host:
            return 0, b'', 'bad_host'
        port = parts.port or (443 if scheme == 'https' else 80)

        target = parts.path or '/'
        query = urllib.parse.urlencode(params) if params else ''
        if parts.query and query:
            query = f'{parts.query}&{query}'
        elif parts.query:
            query = parts.query
        if query:
            target = f'{target}?{query}'

        body = urllib.parse.urlencode(data, doseq=True).encode('utf-8') \
            if data else None
        payload = _request_bytes(method.upper(), target, parts.netloc,
                                 headers or {}, cookies or {}, body)

        addrinfos, error = resolve_within(host, port, budget)
        if addrinfos is None:
            return 0, b'', error
        sock = _connect(addrinfos, budget)
        try:
            if scheme == 'https':
                sock = _wrap_tls(sock, host, budget)
            _send_all(sock, payload, budget)
            status, raw = _read_response(_Reader(sock, budget))
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return status, raw, ''


def json_body(body: bytes) -> Any:
    """有界传输只送回字节；JSON 判读留给调用方（它才知道业务契约）。"""
    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        return None
