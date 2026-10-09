"""diagnostics.py - 默认关闭的旁路观测。

设计约束（来自可靠性复核 B 批）：
- 默认关闭；关闭时不给 FFmpeg 增加任何参数，不启动采样线程，不改变推流算法。
- 启用后只能记录。本模块没有任何停止/重启/重挂载/修改任务完成状态的能力。
- 所有写入都在独立目录（<data_dir>/diagnostics），不写业务数据库、不改状态文件。
- 队列满、磁盘慢或写失败、解析异常、读取器退出，一律丢弃并计数，
  绝不把背压传给 FFmpeg：进度管道必须持续排空到子进程退出。
- 不抓取网络载荷，不记录 Cookie、推流码、签名或完整推流 URL。
- 不每秒扫描巨型日志，不遍历 OSS，不重新 ffprobe。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024   # 单文件上限（实现目标，需实测）
DEFAULT_TOTAL_BYTES = 256 * 1024 * 1024    # 单个实例诊断目录总量上限
DEFAULT_MAX_RECORD_BYTES = 8 * 1024        # 单条记录字节预算
DIRECTORY_BUDGET_MB = 256
DEFAULT_RETAIN_DAYS = 7
DEFAULT_QUEUE_SIZE = 512
EVENT_BUFFER = 500
EVENT_BUFFER_BYTES = 2 * 1024 * 1024      # 事件内存缓冲的字节上限
PROGRESS_PERIOD_SECONDS = 1
# 已退出通道的有界历史：活跃集合（_channels）之外保留少量最近对象供观测，
# 防止反复接管时确认退出的旧消费者无界积累（第 5 轮 U2）。
RETIRED_CHANNEL_HISTORY = 16
# 只排空通道遇到读端错误时的退避与上限：避免忙循环，同时保留重试机会。
DRAIN_READ_BACKOFF = 0.2
MAX_DRAIN_READ_ERRORS = 5
# 有序播放清单的单文件条数上限（指纹相同则追加）。
MAX_PLAYLIST_ENTRIES = 500

# 重点观测时段（本地时间 18:00–24:00）在该区间内提高采样频率
PEAK_START_HOUR = 18
PEAK_END_HOUR = 24
STARTUP_BOOST_SECONDS = 180

def resolve_pusher_identity(launcher_pid: int, max_entries: int = 400) -> dict:
    """找出 shell 包装下真实的 FFmpeg 进程身份。

    `Popen(shell=True)` 拿到的 PID 是外壳进程，不等于真实 FFmpeg。Linux 上按
    ppid 关系与 cmdline 匹配；其他平台明确标记为 unknown（不猜、不填零）。
    """
    identity = {'launcher_pid': launcher_pid,
                'ffmpeg_pid': 'unknown',
                'resolution': 'unknown'}
    if not sys.platform.startswith('linux'):
        identity['resolution'] = 'unsupported-platform'
        return identity
    try:
        entries = os.listdir('/proc')
    except OSError:
        identity['resolution'] = 'proc-unavailable'
        return identity
    scanned = 0
    for entry in entries:
        if not entry.isdigit() or scanned >= max_entries:
            continue
        scanned += 1
        try:
            stat = Path(f'/proc/{entry}/stat').read_text(
                encoding='utf-8', errors='replace')
            fields = stat.rsplit(')', 1)[-1].split()
            if int(fields[1]) != launcher_pid:
                continue
            cmdline = Path(f'/proc/{entry}/cmdline').read_bytes()
        except Exception:
            continue
        if b'ffmpeg' in cmdline:
            identity['ffmpeg_pid'] = int(entry)
            identity['resolution'] = 'ppid-scan'
            return identity
    identity['resolution'] = 'not-found'
    return identity


_SENSITIVE_KEY = re.compile(
    r'(cookie|sessdata|bili_jct|dedeuserid|token|sign|auth|pwd|password|'
    r'secret|csrf|access_key|refresh_token|key)', re.I)
_SENSITIVE_QUERY = re.compile(r'(key|token|sign|auth|pwd|password|secret)=', re.I)
_URLISH = re.compile(r'^[a-z][a-z0-9+.-]*://', re.I)
# 任意字符串（例如 message）里可识别的凭据片段：`Cookie: SESSDATA=xxx`、`key=xxx`。
_CREDENTIAL_FRAGMENT = re.compile(
    r'(?i)(sessdata|bili_jct|dedeuserid|csrf|access_key|refresh_token|'
    r'auth|token|secret|password|pwd|key|cookie)'
    r'(\s*[:=]\s*)"?[^\s;,"\']+"?'
)
REDACTED = '<redacted>'
# 脱敏前先限制规模：避免在超长/超深结构上做正则回溯与无界内存。
MAX_SANITIZE_CHARS = 2048
MAX_SANITIZE_ITEMS = 64
MAX_SANITIZE_DEPTH = 6


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_url(url: str) -> str:
    """只保留 scheme://host/path，去掉查询串（推流码、签名、key 都在里面）。"""
    if not url:
        return ''
    text = str(url)
    for separator in ('?', '#'):
        if separator in text:
            text = text.split(separator, 1)[0]
    # 极端情况下查询串被拼进路径：再按敏感键名兜底截断
    match = _SENSITIVE_QUERY.search(text)
    if match:
        text = text[:match.start()].rstrip('&/?')
    return text


def redact_text(text: str) -> str:
    """对**任意**字符串脱敏，不要求键名敏感。

    覆盖两类此前会漏掉的形态：列表里的裸 URL，以及消息文本中的
    `Cookie: SESSDATA=...` / `key=...` 之类凭据片段。
    """
    if not text:
        return text
    result = text[:MAX_SANITIZE_CHARS]
    if _URLISH.match(result) and '://' in result:
        result = redact_url(result)
    if _CREDENTIAL_FRAGMENT.search(result):
        result = _CREDENTIAL_FRAGMENT.sub(
            lambda m: f'{m.group(1)}{m.group(2)}{REDACTED}', result)
    if len(text) > MAX_SANITIZE_CHARS:
        result += '...<truncated>'
    return result


def sanitize_value(key: str, value):
    """按字段名 + 值形态对单个值脱敏。"""
    if isinstance(value, str):
        # 键名敏感 → 整个值遮盖，而不是只处理字符串内容。
        if _SENSITIVE_KEY.search(str(key)):
            return REDACTED
        return redact_text(value)
    return value


def sanitize_record(payload, _depth: int = 0):
    """入口统一脱敏：覆盖任意层级的 dict/list 及其中的字符串值。

    只依赖键名与值形态，不依赖调用方是否记得脱敏；异常不得传回调用方。
    先做规模限制（深度/条数/字符数），再进入内存缓冲或落盘。
    """
    if _depth > MAX_SANITIZE_DEPTH:
        return REDACTED
    try:
        if isinstance(payload, dict):
            return {k: sanitize_record(v, _depth + 1)
                    if not isinstance(v, str) else sanitize_value(str(k), v)
                    for k, v in payload.items()}
        if isinstance(payload, (list, tuple)):
            head = [sanitize_record(item, _depth + 1)
                    for item in payload[:MAX_SANITIZE_ITEMS]]
            if len(payload) > MAX_SANITIZE_ITEMS:
                head.append(f'<truncated {len(payload) - MAX_SANITIZE_ITEMS}>')
            return head
        if isinstance(payload, str):
            return redact_text(payload)
        return payload
    except Exception:
        return REDACTED


def _parse_number(value):
    """把 FFmpeg 进度字段转成数字；N/A、缺失、负值与异常都返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in ('N/A', 'NA', '-'):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


class _DirectoryBudget:
    """诊断目录的总容量预算。

    单文件限额只保证“单个文件不无限增长”；真正约束磁盘的是目录总量。
    这里按 UTF-8 实际字节数（不是字符数）计费，并负责回收与丢弃计数。

    并发安全：多个 sink（events/media/host）与导出会同时写同一个目录，
    “先检查余额、再写入、最后扣账”会让它们重复使用同一份剩余容量
    （实测 150 字节预算写出 214 字节）。因此改为**预留 → 提交 / 回退**：
    预留与余额判断在同一把锁内完成，未提交的预留也算已占用。
    """

    # 余额扫描的最短间隔：期间用增量记账，避免每条记录都扫整个目录。
    SCAN_INTERVAL = 1.0

    def __init__(self, directory: Path, total_bytes: int = DEFAULT_TOTAL_BYTES,
                 max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES):
        self.directory = directory
        self.total_bytes = max(1, int(total_bytes))
        self.max_record_bytes = max(256, int(max_record_bytes))
        self.dropped_capacity = 0
        self.truncated_records = 0
        self.reclaimed_bytes = 0
        self.reserved_bytes = 0
        # RLock：stats() 持锁期间会再次调用 usage()，普通 Lock 会自锁死。
        self._lock = threading.RLock()
        self._cached = None
        self._cache_time = 0.0

    def _rescan_unlocked(self) -> int:
        total = 0
        try:
            for path in self.directory.glob('*'):
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass
        self._cached = total
        self._cache_time = time.monotonic()
        return total

    def usage(self, force: bool = False) -> int:
        """已用字节 = 磁盘实测（含缓存窗口内的增量）+ 未提交的预留。"""
        with self._lock:
            if force or self._cached is None \
                    or time.monotonic() - self._cache_time >= self.SCAN_INTERVAL:
                self._rescan_unlocked()
            return self._cached + self.reserved_bytes

    def note_written(self, nbytes: int):
        """兼容入口：等价于“提交一次预留”。"""
        self.commit(nbytes)

    def reserve(self, nbytes: int, protect: Path = None) -> bool:
        """预留 nbytes：与余额判断在同一把锁内完成，返回是否成功。"""
        with self._lock:
            if self._cached is None \
                    or time.monotonic() - self._cache_time >= self.SCAN_INTERVAL:
                self._rescan_unlocked()
            if self._cached + self.reserved_bytes + nbytes <= self.total_bytes:
                self.reserved_bytes += nbytes
                return True
            if self._reclaim_unlocked(nbytes, protect):
                self.reserved_bytes += nbytes
                return True
            self.dropped_capacity += 1
            return False

    def commit(self, nbytes: int):
        """写入成功后提交预留，并把增量计入缓存。"""
        with self._lock:
            self.reserved_bytes = max(0, self.reserved_bytes - nbytes)
            if self._cached is not None:
                self._cached += nbytes

    def rollback(self, nbytes: int):
        """写入失败后释放预留，不改变磁盘计费。"""
        with self._lock:
            self.reserved_bytes = max(0, self.reserved_bytes - nbytes)

    def commit_replace(self, previous_bytes: int, new_bytes: int):
        """覆盖写（导出）成功后提交：旧内容被替换，按差额修正缓存。"""
        with self._lock:
            self.reserved_bytes = max(0, self.reserved_bytes - new_bytes)
            if self._cached is not None:
                self._cached += (new_bytes - previous_bytes)

    def admit(self, nbytes: int, protect: Path = None) -> bool:
        """兼容入口：只判断（不预留），供不需要两阶段提交的调用方使用。"""
        with self._lock:
            if self._cached is None \
                    or time.monotonic() - self._cache_time >= self.SCAN_INTERVAL:
                self._rescan_unlocked()
            if self._cached + self.reserved_bytes + nbytes <= self.total_bytes:
                return True
            return self._reclaim_unlocked(nbytes, protect)

    def _reclaim_unlocked(self, nbytes: int, protect: Path = None) -> bool:
        try:
            files = [p for p in self.directory.glob('*') if p.is_file()]
        except OSError:
            return False
        # 并发写者可能在我们 glob 与排序之间删除文件：逐个 stat 并跳过消失的，
        # 绝不让清理路径把异常抛回落盘/排空线程。
        decorated = []
        for path in files:
            try:
                decorated.append((path.stat().st_mtime, path))
            except OSError:
                continue
        decorated.sort(key=lambda item: item[0])
        files = [path for _, path in decorated]
        need = self._cached + self.reserved_bytes + nbytes - self.total_bytes
        freed = 0
        for path in files:
            if need <= 0:
                break
            if protect is not None:
                try:
                    if path.resolve() == Path(protect).resolve():
                        continue
                except OSError:
                    pass
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            freed += size
            need -= size
        if freed:
            self.reclaimed_bytes += freed
            self._rescan_unlocked()
        return need <= 0

    def stats(self) -> dict:
        with self._lock:
            return {'total_bytes': self.total_bytes,
                    'max_record_bytes': self.max_record_bytes,
                    'usage_bytes': self.usage(),
                    'reserved_bytes': self.reserved_bytes,
                    'dropped_capacity': self.dropped_capacity,
                    'truncated_records': self.truncated_records,
                    'reclaimed_bytes': self.reclaimed_bytes}

    def snapshot_fast(self) -> dict:
        """锁外快照：绝不获取预算锁（磁盘维护可能长时间持有它）。

        usage 取缓存的最近一次扫描值；没有缓存时明确标 unknown，
        绝不为了取数而现扫磁盘。
        """
        cached = self._cached
        return {'total_bytes': self.total_bytes,
                'max_record_bytes': self.max_record_bytes,
                'usage_bytes': cached if cached is not None else 'unknown',
                'usage_fresh': False,
                'reserved_bytes': self.reserved_bytes,
                'dropped_capacity': self.dropped_capacity,
                'truncated_records': self.truncated_records,
                'reclaimed_bytes': self.reclaimed_bytes}


def _json_bytes(record) -> int:
    return len(json.dumps(record, ensure_ascii=False, default=str).encode('utf-8'))


def _clip_text(value, limit: int):
    text = '' if value is None else str(value)
    return text[:limit]


def _bound_record(record: dict, limit: int) -> dict:
    """把记录裁剪到**字节**预算内，返回有界 dict。

    用于内存缓冲与落盘两处：缓冲阶段就限制字节，才能证明
    “500 条事件 / 512 条队列”的内存有上限，而不是先存完整记录再裁剪。
    依次降级：原样 → 截断长字符串 → 最小字段集 → 极端兜底。
    """
    limit = max(64, int(limit))
    if _json_bytes(record) <= limit:
        return record
    clipped = {}
    for key, value in record.items():
        if isinstance(value, str) and len(value) > 256:
            clipped[key] = value[:256] + '...<clipped>'
        else:
            clipped[key] = value
    if _json_bytes(clipped) <= limit:
        clipped['clipped'] = True
        return clipped
    minimal = {
        'ts': _clip_text(record.get('ts'), 48),
        'kind': _clip_text(record.get('kind'), 48),
        'instance_id': _clip_text(record.get('instance_id'), 48),
        'clipped': True,
    }
    if _json_bytes(minimal) <= limit:
        return minimal
    return {'clipped': True}


def _clip_record(record: dict, limit: int) -> str:
    """兼容入口：返回裁剪后的 JSONL 行（不含换行）。"""
    return json.dumps(_bound_record(record, limit), ensure_ascii=False, default=str)


class _JsonlSink(threading.Thread):
    """有界 JSONL 落盘线程：写失败只丢弃并计数，不影响调用方。"""

    def __init__(self, path: Path, max_bytes: int, retain_days: int,
                 budget: '_DirectoryBudget' = None):
        super().__init__(name=f'DiagSink-{path.stem}', daemon=True)
        self.path = path
        self.max_bytes = max_bytes
        self.retain_days = retain_days
        self.budget = budget
        self._queue: 'queue.Queue[dict]' = queue.Queue(maxsize=DEFAULT_QUEUE_SIZE)
        self._closed = threading.Event()
        self.dropped = 0
        self.written = 0
        self.write_errors = 0
        self.bytes_written = 0
        self._lock = threading.Lock()

    def submit(self, record: dict) -> bool:
        """入队；队列满则丢弃并计数，返回是否接受（绝不阻塞调用方）。"""
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def stop(self):
        self._closed.set()

    def run(self):
        while not self._closed.is_set():
            try:
                record = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._write_safe(record)
        # 收尾：有界地排空剩余记录，避免停止时静默丢失已接受的记录。
        for _ in range(DEFAULT_QUEUE_SIZE):
            try:
                record = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write_safe(record)

    def _write_safe(self, record):
        try:
            self._write(record)
        except Exception:
            with self._lock:
                self.write_errors += 1

    def _write(self, record: dict):
        # 记录在入队前已经按字节裁剪过；这里再校验一次最终字节数。
        line = json.dumps(record, ensure_ascii=False, default=str) + '\n'
        nbytes = len(line.encode('utf-8'))
        if self.budget is not None and nbytes > self.budget.max_record_bytes:
            line = _clip_record(record, self.budget.max_record_bytes) + '\n'
            nbytes = len(line.encode('utf-8'))
            with self._lock:
                self.budget.truncated_records += 1
        reserved = False
        if self.budget is not None:
            # 预留 + 提交：并行 sink 不能重复使用同一份剩余容量。
            if not self.budget.reserve(nbytes, protect=self.path):
                with self._lock:
                    self.dropped += 1
                return
            reserved = True
        try:
            self._rotate_if_needed(nbytes)
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(line)
            with self._lock:
                self.written += 1
                self.bytes_written += nbytes
            if reserved:
                self.budget.commit(nbytes)
        except Exception:
            if reserved:
                self.budget.rollback(nbytes)
            with self._lock:
                self.write_errors += 1
            raise

    def _rotate_if_needed(self, incoming: int):
        try:
            if not self.path.exists() or self.path.stat().st_size + incoming <= self.max_bytes:
                return
        except OSError:
            return
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        rotated = self.path.with_name(f'{self.path.stem}.{stamp}{self.path.suffix}')
        index = 0
        while rotated.exists() and index < 50:
            index += 1
            rotated = self.path.with_name(f'{self.path.stem}.{stamp}-{index}{self.path.suffix}')
        try:
            self.path.replace(rotated)
        except OSError:
            self.path.unlink(missing_ok=True)
        self._purge_old()
        if self.budget is not None:
            self.budget.usage(force=True)

    def _purge_old(self):
        cutoff = time.time() - self.retain_days * 86400
        try:
            for candidate in self.path.parent.glob(f'{self.path.stem}*{self.path.suffix}'):
                try:
                    if candidate.stat().st_mtime < cutoff:
                        candidate.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    def stats(self):
        with self._lock:
            return {'dropped': self.dropped, 'written': self.written,
                    'write_errors': self.write_errors,
                    'queued': self._queue.qsize()}


class ProgressChannel(threading.Thread):
    """FFmpeg `-progress` 通道读取器。

    只有一个职责：把管道读干。解析、落盘与推流完全解耦；队列满就丢记录，
    消费者退出或诊断被停用也不能关闭读端（否则 FFmpeg 写入报错）。
    """

    def __init__(self, stream, identity: dict, sink: '_JsonlSink',
                 period: float = PROGRESS_PERIOD_SECONDS, drain_only: bool = False):
        super().__init__(name='FFmpegProgressReader', daemon=True)
        self.stream = stream
        self.identity = dict(identity)
        self.sink = sink
        self.period = period
        self.drain_only = drain_only
        self.record_limit = (
            sink.budget.max_record_bytes
            if (sink is not None and getattr(sink, 'budget', None) is not None)
            else DEFAULT_MAX_RECORD_BYTES)
        self._closed = threading.Event()
        self.dropped = 0
        self.parsed = 0
        self.parse_errors = 0
        self.records = 0
        self.read_errors = 0
        self.unrecoverable = False
        self.exited = threading.Event()
        self._last_media_us = None
        self._last_monotonic = None
        self._lock = threading.Lock()

    def stop(self):
        """请求停止持久化；管道仍会继续排空直到子进程退出。"""
        self._closed.set()

    def run(self):
        """把管道读干是唯一不可放弃的职责。

        解析、序列化、入队、落盘的任何异常都只影响这一条记录，
        不允许中断读取循环——否则 FFmpeg 写端可能被填满并阻塞推流。

        读端本身出错时分两种情形：
        - 解析模式：立即退出，由控制器把排空责任交接给只排空通道；
        - 只排空模式：退避重试；连续失败达到上限才判定为**不可恢复读端错误**
          并退出（此时读端已失效，没有可排空的数据），因此不会忙循环。
        """
        pending = {}
        consecutive_errors = 0
        try:
            while True:
                try:
                    raw = self.stream.readline()
                except Exception:
                    with self._lock:
                        self.read_errors += 1
                    if not self.drain_only:
                        break
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_DRAIN_READ_ERRORS:
                        self.unrecoverable = True
                        break
                    time.sleep(DRAIN_READ_BACKOFF)
                    continue
                if not raw:
                    break
                consecutive_errors = 0
                if self.drain_only:
                    # 兜底模式：只把管道读干，不解析、不落盘。
                    with self._lock:
                        self.parsed += 1
                    continue
                try:
                    self._consume_line(
                        raw.decode('utf-8', 'replace').rstrip('\r\n'), pending)
                except Exception:
                    # 解析/入队失败只丢弃本行，继续排空。
                    with self._lock:
                        self.parse_errors += 1
        finally:
            if pending:
                try:
                    self._emit(pending, 'truncated')
                except Exception:
                    with self._lock:
                        self.parse_errors += 1
            self.exited.set()
            # 读端只在这里关闭：此时子进程已经退出或管道已到 EOF。

    def _consume_line(self, line: str, pending: dict):
        if not line:
            return
        if '=' not in line:
            with self._lock:
                self.parse_errors += 1
            return
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        with self._lock:
            self.parsed += 1
        if key == 'progress':
            pending['progress_state'] = value
            self._emit(dict(pending), value)
            pending.clear()
            return
        pending[key] = value

    def _emit(self, fields: dict, state: str):
        """生成一条进度记录。

        原始值与有效派生值分开：负时间、N/A、缺失都保持 None 并给出质量标记；
        窗口速度只在“上一份样本有效”时才计算，绝不跨缺失样本。
        """
        raw_out_time_us = fields.get('out_time_us')
        raw_out_time_ms = fields.get('out_time_ms')
        media_us = _parse_number(raw_out_time_us)
        source = 'out_time_us'
        if media_us is None and raw_out_time_ms is not None:
            # 历史字段 out_time_ms 实际使用的单位同样是微秒（本机 FFmpeg 已核对）。
            media_us = _parse_number(raw_out_time_ms)
            source = 'out_time_ms'
        quality = 'ok'
        if media_us is None:
            quality = 'missing'
        elif media_us < 0:
            # 负值不是有效样本：原始值保留，派生值不填。
            quality = 'negative'
        now = time.monotonic()
        window_speed = None
        if (quality == 'ok' and self._last_media_us is not None
                and self._last_monotonic is not None):
            delta_media = (media_us - self._last_media_us) / 1_000_000.0
            delta_wall = now - self._last_monotonic
            if delta_wall > 0 and delta_media >= 0:
                window_speed = round(delta_media / delta_wall, 4)
        # 基线只在有效样本上延续；缺失/负值会打断窗口，下一份样本没有窗口速度。
        if quality == 'ok':
            self._last_media_us = media_us
            self._last_monotonic = now
        else:
            self._last_media_us = None
            self._last_monotonic = None
        record = dict(self.identity)
        record.update({
            'ts': utc_now_iso(),
            'monotonic': round(now, 3),
            'progress_state': state,
            'media_seconds': (round(media_us / 1_000_000.0, 3)
                              if quality == 'ok' else None),
            'media_seconds_raw': fields.get('out_time'),
            'media_time_source': source,
            'media_quality': quality,
            'media_us_raw': raw_out_time_us if raw_out_time_us is not None else raw_out_time_ms,
            'total_size': _parse_number(fields.get('total_size')),
            'bitrate': fields.get('bitrate'),
            'speed_cumulative': fields.get('speed'),
            'speed_window': window_speed,
            'drop_frames': _parse_number(fields.get('drop_frames')),
            'dup_frames': _parse_number(fields.get('dup_frames')),
            'frame': _parse_number(fields.get('frame')),
            'fps': _parse_number(fields.get('fps')),
        })
        with self._lock:
            self.records += 1
        # 记录最近一份有效输出样本（输出推进 ↔ 输入位置的对应关系用）。
        if quality == 'ok':
            self._last_sample = {'media_seconds': round(media_us / 1_000_000.0, 3),
                                 'total_size': _parse_number(fields.get('total_size')),
                                 'ts': record['ts']}
        # 入队前先按字节裁剪：512 条队列的内存才有可证明的上限。
        record = _bound_record(record, self.record_limit)
        if self._closed.is_set() or self.sink is None:
            # 诊断已停用：仍然排空管道，只是不再落盘。
            with self._lock:
                self.dropped += 1
            return
        if not self.sink.submit(record):
            # 队列满：丢弃并计数，不等待、不回压。
            with self._lock:
                self.dropped += 1

    def stats(self):
        with self._lock:
            return {'parsed': self.parsed, 'records': self.records,
                    'dropped': self.dropped, 'parse_errors': self.parse_errors,
                    'read_errors': self.read_errors,
                    'drain_only': self.drain_only,
                    'unrecoverable': self.unrecoverable,
                    'exited': self.exited.is_set()}

    def last_media_sample(self):
        """最近一份有效输出样本（media_seconds / total_size），无则 None。

        供监控线程建立“输出推进 ↔ 当前输入文件位置”的对应关系；
        只读快照，不阻塞读取器。
        """
        with self._lock:
            sample = getattr(self, '_last_sample', None)
            return dict(sample) if sample else None


# `ss` 输出的连接状态行首词。
#
# 2026-09-21 修正：实际采样命令是 `ss -tinp state established`（本轮起为
# `-tinep`）。**带 `state` 过滤时 State 列会被省略**，行首直接是 Recv-Q。
# 旧解析只认 ESTAB 等状态前缀，遇到省略 State 的输出会把所有连接整块丢掉
# （表现为"解析到了 0 块"，而不是"连接确实为 0"）。因此这里同时支持有/无
# State 列两种形状。
_SS_STATE_PREFIXES = frozenset({
    'ESTAB', 'TIME-WAIT', 'SYN-SENT', 'SYN-RECV', 'FIN-WAIT-1', 'FIN-WAIT-2',
    'CLOSE-WAIT', 'CLOSE', 'LAST-ACK', 'CLOSING', 'LISTEN', 'UNCONN',
})
# Netid 列（`ss -a` / 多协议输出时最前面的 tcp/udp/...）。
_SS_NETID_PREFIXES = ('tcp', 'udp', 'raw', 'unix', 'nr', 'vsock', 'xdp')
# 表头行首词。
_SS_HEADER_FIRSTS = frozenset({'State', 'Netid', 'Recv-Q', 'Local'})


def _split_ss_blocks(output: str):
    """把 `ss` 输出切分成连接块（兼容有/无 State 列、有/无表头、IPv4/IPv6）。

    每块 = 一条连接行（``ESTAB ...``，或省略 State 列的 ``0 0 local peer ...``）
    + 其后续信息行（timer/ino、cubic/rtt、retrans、bytes_sent…）。
    表头行与空行跳过。

    形状识别与列定位**全部放在这里**，这样 :func:`HostSampler.parse_ss` 只依赖
    ``re`` 和本函数（离线抽取测试/监督原件也只需抽取这两处）。

    每块返回：

    - ``head_tokens``：连接行的列；
    - ``text``：整块文本（用于 ino/pid/rtt/retrans 的块级提取）；
    - ``inodes`` / ``pids``：块内出现的 socket inode 与 pid（字符串列表，按出现顺序）；
    - ``usable_inodes``：去掉 ``ino:0`` 后的有效 inode（归属的主要依据）；
    - ``offset``：Recv-Q 所在列下标（前面可能有 Netid 列和/或 State 列）；
    - ``local`` / ``peer``：本地端点与对端端点（取不到为 'unknown'）。
    """
    netid_prefixes = ('tcp', 'udp', 'raw', 'unix', 'nr', 'vsock', 'xdp')
    header_firsts = frozenset({'State', 'Netid', 'Recv-Q', 'Local'})

    def is_endpoint(token):
        """是否像 `地址:端口`。

        不能粗略用 `split(':')[0]`：IPv6 既可能写成 `[2001:db8::1]:443`
        也可能裸写成 `2001:db8::1:443`，前者按第一个冒号切会得到 `[2001`。
        """
        if not token or ':' not in token:
            return False
        if token.startswith('['):
            host, sep, port = token[1:].partition(']:')
            return bool(sep) and bool(host) and port.isdigit()
        host, _, port = token.rpartition(':')
        return bool(host) and port.isdigit()

    def is_connection_head(tokens):
        if not tokens:
            return False
        if tokens[0] in header_firsts:
            return False                       # 表头
        if tokens[0] in _SS_STATE_PREFIXES or tokens[0].startswith(netid_prefixes):
            return True
        # 省略 State 列的形状：`Recv-Q Send-Q Local:Port Peer:Port ...`
        if len(tokens) >= 4 and tokens[0].isdigit() and tokens[1].isdigit():
            return any(is_endpoint(t) for t in tokens[2:])
        return False

    def column_offset(tokens):
        offset = 0
        if tokens and tokens[0].startswith(netid_prefixes):
            offset += 1
        if len(tokens) > offset and tokens[offset] in _SS_STATE_PREFIXES:
            offset += 1
        return offset

    def is_field_line(line):
        """像不像 `ss` 的字段续行。

        真实的续行几乎总带 `:`（`rtt:12.5`、`bytes_sent:…`）、`(`（`skmem:(…)`、
        `users:((…))`）或至少一个数字（`delivery_rate 1.0Gbps`、`119ms`、`1001`）。
        三者都没有的多半是"换了格式、我们认不出来"的内容——不能默默当成续行吞掉，
        否则整段无法解析的输出会被当成一条字段齐全的连接。

        这只是**启发式**：误判为可疑只会给结果打上 `partial` 并留一条限频日志，
        绝不会改变任何数值；宁可多标一次"不完整"，也不静默输出看似完整的总数。
        """
        return ':' in line or '(' in line or any(ch.isdigit() for ch in line)

    blocks = []
    current = None
    # 既不是连接行、也不是表头、又出现在第一条连接之前的行：**认不出来**的输出。
    # 它们是区分"合法空表"与"输出形状变了"的关键证据，不能默默丢掉。
    stray = []
    header_lines = 0
    for raw_line in (output or '').splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        if is_connection_head(tokens):
            current = {'head_tokens': tokens, 'lines': [stripped]}
            blocks.append(current)
            continue
        if tokens[0] in header_firsts:
            header_lines += 1
            continue
        if current is None or not is_field_line(stripped):
            # 还没有连接行可归属，或这一行不像任何已知字段：记成"认不出来的行"。
            stray.append(stripped)
            continue
        current['lines'].append(stripped)

    for block in blocks:
        text = '\n'.join(block['lines'])
        block['text'] = text
        inodes = [m.group(1) for m in re.finditer(r'\bino:(\d+)', text)]
        block['inodes'] = inodes
        block['pids'] = [m.group(1) for m in re.finditer(r'\bpid=(\d+)', text)]
        # ino:0 = 该 inode 不可用（未取得 / 尚未被应用接受），绝不能把一堆 0
        # 互相匹配成同一条连接。
        usable = []
        for value in inodes:
            if value and value != '0' and value not in usable:
                usable.append(value)
        block['usable_inodes'] = usable
        block['inode_unusable'] = bool(inodes) and not usable
        head = block['head_tokens']
        offset = column_offset(head)
        block['offset'] = offset
        block['local'] = head[offset + 2] if len(head) > offset + 3 and is_endpoint(head[offset + 2]) else 'unknown'
        block['peer'] = head[offset + 3] if len(head) > offset + 3 and is_endpoint(head[offset + 3]) else 'unknown'
        # 一块"看着像连接、其实什么都没取到"的行：没有任何端点，也没有任何字段。
        # 这类块说明我们认错了形状，不能把它当成一条真实连接。
        block['recognized'] = bool(
            block['local'] != 'unknown' or block['peer'] != 'unknown'
            or usable or block['pids']
            or re.search(r'\b(?:rtt|retrans|bytes_sent|bytes_acked|bytes_retrans'
                         r'|segs_out|segs_in|cwnd):', text))
    return blocks, stray, header_lines


def _ss_usable_inodes(values):
    """过滤出可用于归属的 socket inode（见 :func:`_split_ss_blocks` 的说明）。

    ``ino:0`` 视为不可用。返回去重后的有序列表。
    """
    seen = []
    for value in values or ():
        if not value or value == '0':
            continue
        if value not in seen:
            seen.append(value)
    return seen


# ==================== 输入侧证据（第 5 轮 U4 → 收口复核后仅存离线基础） ====================
#
# 时长探测/位置估计的**生产路径已全部删除**（见 locate_input_position 之后
# 的注释块）：监控循环零探测调用、无调度入口、无执行线程、无事件生产。
# 仅保留两个纯函数作为离线可测基础（无生产调用方）：
#
# - :func:`probe_input_durations`：ffprobe 有界时长探测（条数上限 +
#   单文件超时 + 总预算，失败/超时记 None），也是监督验收测试的
#   patch 锚点属性；
# - :func:`locate_input_position`：媒体输出秒 → (文件序号, 文件内偏移)
#   纯函数。其结果由**输出媒体时间 − 文件时长**推算，属于**输出时间线
#   估计**：输出阻塞而输入预读继续时两者不同，输出停住时推算同样停住，
#   不能区分输入饥饿与输出受阻，因此不得作为输入/输出阻塞归因证据。
#   真正的输入证据（按 FFmpeg 身份动态枚举输入 fd/位置/wchan）已推迟，
#   需 Linux 实测；线程等待状态（wchan）由 Linux 主机采样提供，
#   Windows 恒为 unknown（未在 Linux 实测，单独标记）。
#
# 两者可在离线测试中直接注入替身（runner / 纯函数）。

INPUT_PROBE_MAX_FILES = 64        # 有界：单次最多探测的文件数
INPUT_PROBE_FILE_TIMEOUT = 5.0    # 单文件 ffprobe 超时（秒）
INPUT_PROBE_TOTAL_BUDGET = 60.0   # 全部探测的总时间预算（秒）


def _ffprobe_path(ffmpeg_path: str) -> str:
    """找 ffprobe：优先与 ffmpeg 同目录，其次 PATH；找不到返回 ''。"""
    if ffmpeg_path:
        suffix = '.exe' if sys.platform == 'win32' else ''
        candidate = Path(ffmpeg_path).with_name('ffprobe' + suffix)
        if candidate.exists():
            return str(candidate)
    return shutil.which('ffprobe') or ''


def _default_ffprobe_runner(command, cmd_timeout: float):
    """默认 ffprobe 执行器：有超时、失败返回 None，绝不抛出。"""
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=cmd_timeout)
        if completed.returncode != 0:
            return None
        return completed.stdout
    except Exception:
        return None


def probe_input_durations(ffmpeg_path: str, entries, timeout: float = None,
                          max_files: int = INPUT_PROBE_MAX_FILES,
                          total_budget: float = INPUT_PROBE_TOTAL_BUDGET,
                          runner=None) -> list:
    """按序探测播放列表各文件时长（秒）；失败/超时条目为 None（unknown）。

    有界：最多 ``max_files`` 个文件；单个文件超时 ``timeout``；全部探测
    共享 ``total_budget`` 总预算，超预算后剩余条目直接记 None。
    ``runner`` 供离线测试注入（签名 ``(command, timeout) -> str|None``）。
    """
    entries = list(entries)
    if not entries:
        return []
    timeout = INPUT_PROBE_FILE_TIMEOUT if timeout is None else timeout
    probe = _ffprobe_path(ffmpeg_path)
    if runner is None:
        if not probe:
            # ffprobe 不可用：全部记 unknown（与数量对齐），不猜。
            return [None] * len(entries)
        runner = _default_ffprobe_runner
    command_template = [probe or 'ffprobe',
                        '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1']
    durations = []
    started = time.monotonic()
    for entry in entries[:max_files]:
        remaining_budget = total_budget - (time.monotonic() - started)
        if remaining_budget <= 0:
            durations.append(None)
            continue
        output = runner(command_template + [str(entry)],
                        min(timeout, remaining_budget))
        value = None
        if output:
            try:
                value = float(str(output).strip().splitlines()[0])
            except (ValueError, IndexError):
                value = None
        durations.append(value if value is not None and value >= 0 else None)
    durations.extend([None] * max(0, len(entries) - max_files))
    return durations


def locate_input_position(media_seconds, durations) -> dict:
    """媒体输出秒 → 当前输入文件位置（纯函数，离线可测）。

    返回 ``{'file_index': int, 'file_offset_seconds': float}``；
    无法定位（样本缺失/遇到未知时长/超出已知总时长）返回 ``None``，不猜。
    """
    if media_seconds is None or not durations:
        return None
    try:
        position = float(media_seconds)
    except (TypeError, ValueError):
        return None
    if position < 0:
        return None
    offset = position
    for index, duration in enumerate(durations):
        if duration is None:
            # 从第一个未知时长起无法再精确归属：明确 unknown。
            return None
        if offset < duration:
            return {'file_index': index, 'file_offset_seconds': round(offset, 3)}
        offset -= duration
    return None        # 超出已知总时长（可能在换片/循环边界），不猜


# ==================== 时长探测的生产路径已删除（收口复核） ====================
#
# 第 5 轮 U4 曾把 ffprobe 时长探测同步放进推流监控循环（阻塞退出处理，
# 第 6 轮 V1 反例）；第 6 轮收口改为一次性旁路线程；收口复核认定该估计
# 功能不能定位输入根因、且一次性线程仍在向"当前代"事件流写入晚到结果，
# 故**彻底删除**生产路径：调度入口（schedule_input_probe）、执行线程
# （_run_input_probe）、模块级状态与去重、快照观测字段（input_probe）与
# `output_timeline_position` 事件生产全部移除，监控循环内零探测调用。
#
# 以下两个**纯函数**保留，原因：
# - `probe_input_durations` 是监督验收测试（test_supervision_round6）以
#   `patch.object` 建立测试上下文的锚点属性（监督文件本轮自行迁移，
#   不由本方改动）；删除该属性会使监督测试在 patch 阶段 ERROR。
# - 两者是"若未来重新引入输入证据"的离线可测基础（有界、可注入），
#   **生产代码无任何调用方**，不产生进程、线程、文件或事件。
# 对应的规格测试保留在 test_u4_input_evidence.py（离线约定不退化）；
# 依赖已删除生产路径的测试全部退役。


# 采集器自身的日志（2026-09-21 最小日志补充）。
#
# 只补现有日志真正缺少的两类上下文：外部采集命令**失败/不可用**，以及**解析结果
# 可疑**（命令成功却一个连接块都没切出来）。健康状态下的采样不额外打点——现有
# access/backend/ffmpeg 日志已经覆盖，这里不重复一份。
# 限频：同一类提示最多每 COLLECTION_LOG_INTERVAL 秒一条，绝不每秒刷屏；
# 日志写入失败一律吞掉，绝不影响采样与推流。
import logging as _logging

_logger = _logging.getLogger(__name__)
COLLECTION_LOG_INTERVAL = 300.0

_collection_log_state = {}
_collection_log_lock = threading.Lock()


def _log_collection_once(key: str, level: int, message: str, *args) -> None:
    """同一类采集提示限频输出；写日志失败不得影响调用方。"""
    try:
        now = time.monotonic()
        with _collection_log_lock:
            last = _collection_log_state.get(key)
            if last is not None and now - last < COLLECTION_LOG_INTERVAL:
                return
            _collection_log_state[key] = now
        _logger.log(level, message, *args)
    except Exception:
        pass


class HostSampler(threading.Thread):
    """Linux 主机层只读采样（可选、同机单例）。

    - 锁文件放在**主机共享目录**（默认系统临时目录，可用
      `LIVE_DIAG_HOST_DIR` 覆盖），不使用各实例自己的 data-dir：
      四个实例指向不同文件根本不可能互斥。
    - **每个实例**（不只是持锁者）都会在共享目录登记自己的
      参与者文件（PID / 实例 ID / 当前推流启动器 PID / 心跳时间）；
      唯一采样器读取全部参与者，因此“本机只跑一份采样”不再等于
      “只采到一个实例”。这是四实例归因的前提。
    - 进程身份带 starttime 与 boot_id，可以防止 PID 复用造成的错误归因；
      并用 `/proc/<pid>/comm` 区分 shell 启动器与真正的 ffmpeg。
    - 采样只读、有超时、有容量预算；Windows 上整体标记为不可用。
    """

    SUPPORTED = sys.platform.startswith('linux')
    LOCK_NAME = 'livestream-host-sampler.lock'
    REGISTRY_NAME = 'livestream-host-sampler.registry.json'
    PARTICIPANT_DIR = 'livestream-host-sampler.participants'
    PARTICIPANT_STALE_SECONDS = 180

    @staticmethod
    def shared_dir() -> Path:
        override = os.environ.get('LIVE_DIAG_HOST_DIR')
        if override:
            return Path(override)
        import tempfile
        return Path(tempfile.gettempdir())

    def __init__(self, directory: Path, instance_id: str, sink: '_JsonlSink',
                 pusher_started_at=None):
        super().__init__(name='HostSampler', daemon=True)
        self.directory = directory
        self.instance_id = instance_id
        self.sink = sink
        # 返回“最近一次真实 FFmpeg 启动的单调时间”，None 表示没有在推。
        self._pusher_started_at = pusher_started_at
        self._closed = threading.Event()
        self._lock_handle = None
        self.acquired = False
        self.samples = 0
        self.errors = 0
        self.attributed_pids = []
        self.unavailable_reason = '' if self.SUPPORTED else '当前平台不支持 /proc 与 ss 采样'

    # ---------------- 同机单例 ----------------

    def _acquire_single_instance(self) -> bool:
        shared = self.shared_dir()
        lock_path = shared / self.LOCK_NAME
        try:
            import fcntl
            self._lock_handle = lock_path.open('a+')
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.unavailable_reason = '本机已有其他实例持有主机采样锁'
            return False
        try:
            self._write_registry(shared)
        except Exception:
            pass
        return True

    def _write_registry(self, shared: Path):
        """跨实例注册：谁能看到谁在采，避免重复高频采集。"""
        payload = {
            'owner_pid': os.getpid(),
            'instance_id': self.instance_id,
            'acquired_at': utc_now_iso(),
            'lock_path': str(shared / self.LOCK_NAME),
            'supported': self.SUPPORTED,
        }
        target = shared / self.REGISTRY_NAME
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding='utf-8')
        self._lock_handle.seek(0)
        self._lock_handle.truncate()
        self._lock_handle.write(json.dumps(payload, ensure_ascii=False))
        self._lock_handle.flush()

    @classmethod
    def read_registry(cls) -> dict:
        """只读查询当前主机采样持有者（跨实例发现入口）。"""
        target = cls.shared_dir() / cls.REGISTRY_NAME
        try:
            return json.loads(target.read_text(encoding='utf-8'))
        except Exception:
            return {}

    # ---------------- 每实例参与者登记（四实例归因） ----------------

    @classmethod
    def participant_dir(cls) -> Path:
        return cls.shared_dir() / cls.PARTICIPANT_DIR

    def publish_participant(self, launcher_pid=None) -> None:
        """每个实例都登记自己：持锁者据此发现同机其他实例。

        只写一个很小的 JSON（进程身份 + 推流启动器 PID + 心跳），
        与“谁持有采样锁”无关，因此四个实例都能被唯一采样器看到。

        ``launcher_pid=None``（心跳）时**保留**上一次登记的启动器 PID，
        不会把 note_pusher_start 登记过的值覆盖成 unknown。注册记录同时
        保存登记时的进程身份（starttime/boot_id），采样时用于识别 PID 复用。
        """
        try:
            directory = self.participant_dir()
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f'{os.getpid()}.json'
            previous = {}
            try:
                previous = json.loads(target.read_text(encoding='utf-8'))
            except Exception:
                previous = {}
            effective = launcher_pid if launcher_pid is not None \
                else previous.get('launcher_pid')
            payload = {
                'pid': os.getpid(),
                'instance_id': self.instance_id,
                'launcher_pid': effective if effective is not None else 'unknown',
                'updated_at': utc_now_iso(),
                'updated_monotonic': round(time.monotonic(), 3),
                'worker_identity': self._self_identity(),
            }
            target.write_text(json.dumps(payload, ensure_ascii=False),
                              encoding='utf-8')
        except Exception:
            return

    def _self_identity(self) -> dict:
        """本进程的身份（防 PID 复用）；读不到的字段明确标 unknown。"""
        identity = HostSampler._proc_identity(os.getpid()) \
            if HostSampler.SUPPORTED else {}
        return {
            'starttime': identity.get('starttime', 'unknown'),
            'boot_id': identity.get('boot_id', 'unknown'),
            'comm': identity.get('comm', 'unknown'),
        }

    @classmethod
    def read_participants(cls, purge_stale: bool = True) -> list:
        """读取同机所有活跃实例；顺带清理过期参与者文件。"""
        directory = cls.participant_dir()
        entries = []
        now = time.time()
        try:
            files = list(directory.glob('*.json'))
        except OSError:
            return entries
        for path in files:
            try:
                if purge_stale and now - path.stat().st_mtime > cls.PARTICIPANT_STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict) and data.get('pid'):
                    entries.append(data)
            except Exception:
                continue
        entries.sort(key=lambda item: int(item.get('pid') or 0))
        return entries

    # ---------------- 采样频率 ----------------

    def sample_period(self) -> float:
        """启动期只在**真实 FFmpeg 刚启动**的 180 秒内提高频率。"""
        started = None
        if self._pusher_started_at is not None:
            try:
                started = self._pusher_started_at()
            except Exception:
                started = None
        if started is not None and time.monotonic() - started <= STARTUP_BOOST_SECONDS:
            return 1.0
        hour = datetime.now().hour
        if PEAK_START_HOUR <= hour < PEAK_END_HOUR:
            return 2.0
        return 10.0

    # ---------------- 只读采集 ----------------

    @staticmethod
    def _read_first_line(path: str):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                return handle.readline().strip()
        except OSError:
            return None

    @classmethod
    def _proc_status(cls, pid: int) -> dict:
        result = {}
        try:
            with open(f'/proc/{pid}/status', 'r', encoding='utf-8',
                      errors='replace') as handle:
                for line in handle:
                    if ':' in line:
                        key, _, value = line.partition(':')
                        result[key.strip()] = value.strip()
        except OSError:
            pass
        return result

    @classmethod
    def boot_id(cls) -> str:
        """启动标识：与 starttime 组合可唯一标识一个进程，防 PID 复用。"""
        value = cls._read_first_line('/proc/sys/kernel/random/boot_id')
        return value or 'unknown'

    @classmethod
    def _proc_stat_fields(cls, pid: int):
        """返回 (state, ppid, starttime)。starttime 是 /proc/<pid>/stat 第 22 字段。"""
        stat = cls._read_first_line(f'/proc/{pid}/stat')
        if not stat:
            return None
        try:
            tail = stat.rsplit(')', 1)[-1].split()
            return tail[0], int(tail[1]), int(tail[19])
        except (IndexError, ValueError):
            return None

    @classmethod
    def _proc_identity(cls, pid: int) -> dict:
        """进程身份：comm/exe + starttime + boot_id（防 PID 复用）。

        读不到 /proc（非 Linux、权限、进程已退出）时字段**显式记为 unknown**，
        而不是从记录里消失——缺失必须是可观测的。
        """
        fields = cls._proc_stat_fields(pid)
        if fields is None:
            return {'pid': pid, 'alive': False, 'state': 'unknown',
                    'ppid': 'unknown', 'comm': 'unknown', 'exe': 'unknown',
                    'starttime': 'unknown', 'boot_id': cls.boot_id(),
                    'reason': 'proc-unreadable-or-not-linux'}
        state, ppid, starttime = fields
        comm = cls._read_first_line(f'/proc/{pid}/comm') or 'unknown'
        try:
            exe = os.readlink(f'/proc/{pid}/exe')
        except OSError:
            exe = 'unknown'
        return {
            'pid': pid,
            'alive': True,
            'state': state,
            'ppid': ppid,
            'comm': comm,
            'exe': redact_url(exe) if '://' in exe else exe,
            'starttime': starttime,
            'boot_id': cls.boot_id(),
        }

    @staticmethod
    def _run_bounded(command: list, timeout: float = 2.0):
        """有超时的只读子进程调用；工具缺失或超时返回 None。"""
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout)
            if completed.returncode != 0:
                return None
            return completed.stdout
        except Exception:
            return None

    @staticmethod
    def pid_socket_inodes(pid: int):
        """列出某进程持有的 socket inode（Linux；读不到返回 None）。"""
        inodes = set()
        try:
            entries = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            return None
        for entry in entries[:512]:
            try:
                target = os.readlink(f'/proc/{pid}/fd/{entry}')
            except OSError:
                continue
            match = re.match(r'socket:\[(\d+)\]', target)
            if match:
                inodes.add(match.group(1))
        return inodes

    # `ss` 采样命令。`-e` 提供 socket inode（`ino:`）与 socket cookie（`sk:`），
    # 是按 socket 归属的主要依据；`-p` 提供进程信息（权限不足时可能不可得）。
    SS_COMMAND = ['ss', '-tinep', 'state', 'established']

    @staticmethod
    def parse_ss(output: str, inodes=None, pids=None, sample_id=None) -> dict:
        """解析 `ss -tinep state established` 的输出（离线可测）。

        真实输出是"可选表头 + 若干连接块"：

            State   Recv-Q  Send-Q  Local:Port      Peer:Port       Process
            ESTAB   0       0       10.0.0.1:54321  1.2.3.4:1935    users:(("ffmpeg",pid=1111,fd=5))
            \ttimer:(keepalive,119ms,0) ino:19426 sk:1001 cgroup:/...
            \t cubic wscale:8,8 rto:204 rtt:12.5/3.75 ato:40 mss:1448 ...
            \t retrans:0/3 bytes_sent:1048576 bytes_acked:1048000 ...

        `state established` 过滤时 **State 列会被省略**，行首直接是 Recv-Q；
        两种形状都支持（见 :func:`_split_ss_blocks`）。

        关键点：**socket inode（ino:）在续行上，不在连接行**；信息行以算法名
        （cubic…）或 `<-` 开头。因此解析必须按"连接块"分组后再归属。

        归属（2026-09-21 修正：**实例归属 ≠ 连接身份**）
        - 有效 socket inode（**排除 `ino:0`**）优先；其次是 `ss -p` 的 pid；
        - `cgroup` 只作为"这条属于哪个实例"的**辅助线索**：它不等于 FFmpeg
          进程身份，更不能靠字符串包含把整个 cgroup 单元的网络请求都吞进来；
        - 冲突或无法归属 → `attribution='unknown'`，绝不退化成全机汇总；
        - 字段拿不到 → 'unknown'，不填零。

        重传（2026-09-21 修正）：`retrans:current/total` 对应 iproute2 v6.1.0
        TCP_INFO 路径的 ``tcpi_retrans`` / ``tcpi_total_retrans``（打印条件是
        **任一非零**，不是 `tcpi_retransmits`）。顶层 `retrans_total` 汇总的
        是**累计**那一半；当前值单独放 `retrans_current_total`。两者都只在
        "至少一条连接拿到了数据"时才有值，否则是 'unknown'——不把"没解析到"
        冒充成"重传为零"。
        """
        # **"没给过滤条件" 与 "给了但没有可用目标" 必须分开**（2026-09-21 R2）：
        # - ``inodes``/``pids`` 都是 None → 调用方确实要全机汇总；
        # - 传了（哪怕是 `[]`）→ 调用方要求按实例归属：此时没有任何可用目标就
        #   如实记 unknown，**绝不退化成全机汇总**（那会把别的实例的连接算到本实例头上）。
        filter_requested = inodes is not None or pids is not None
        requested_inodes = {str(i) for i in (inodes or ())}
        requested_pids = {str(p) for p in (pids or ())}
        # ino:0 既不参与匹配，也不能把一堆 0 互相配对。
        target_inodes = {v for v in requested_inodes if v and v != '0'}
        target_pids = requested_pids
        filter_enabled = filter_requested

        blocks, stray_lines, header_lines = _split_ss_blocks(output)
        # 解析口径的**事实**，而不是"打条日志就算了"：
        #   'empty'        → 合法空结果（空输出，或只有表头，或过滤目标没有连接）
        #   'unrecognized' → 有非空输出，但一条连接、一个表头都认不出来 → 形状变了
        #   'partial'      → 认出了一部分，还有认不出来的内容 → 总数不完整
        #   'ok'           → 全部连接块都被识别
        unrecognized_blocks = sum(1 for b in blocks if not b['recognized'])
        if not blocks:
            parse_status = 'unrecognized' if stray_lines else 'empty'
        elif stray_lines or unrecognized_blocks:
            parse_status = 'partial'
        else:
            parse_status = 'ok'
        if parse_status == 'unrecognized':
            # 认不出形状时**不产出任何看起来像事实的数字**：全部保持 unknown，
            # 免得机器消费者把"格式不支持"当成"没有连接"。
            return {
                'connections': 'unknown', 'send_q_total': 'unknown',
                'retrans_total': 'unknown', 'retrans_current_total': 'unknown',
                'retrans_known_connections': 'unknown',
                'retrans_unknown_connections': 'unknown',
                'inode_unusable_connections': 'unknown',
                'rtt_avg_ms': 'unknown', 'peers': [],
                'connections_detail': [], 'totals': {},
                'attribution': 'unknown', 'matched_lines': 0,
                'host_wide_only': False,
                'parse_status': 'unrecognized',
                'unrecognized_lines': len(stray_lines),
                'unrecognized_blocks': unrecognized_blocks,
                'header_lines': header_lines,
                'detail_truncated': False,
                **({'sample_id': sample_id} if sample_id else {}),
            }
        connections = 0
        send_q = 0
        retrans_current_sum = 0
        retrans_cumulative_sum = 0
        retrans_known = 0
        inode_unusable = 0
        rtts = []
        peers = []
        connection_details = []
        totals = {}
        attribution_sources = set()
        attribution = 'all_host' if not filter_enabled else 'unknown'
        for block in blocks:
            head = block['head_tokens']
            offset = block['offset']
            usable_inodes = block['usable_inodes']
            local = block['local']
            peer = block['peer']
            matched = None
            if filter_enabled:
                if usable_inodes and target_inodes & set(usable_inodes):
                    matched = 'inode'
                if matched is None and target_pids and target_pids & set(block['pids']):
                    # pid 回退：可靠性弱于 inode（PID 可被复用，且 ss -p 需要权限）。
                    matched = 'pid_fallback'
                if matched is None:
                    continue        # 与采样对象无关的连接：不计入
                attribution_sources.add(matched)
            else:
                matched = 'all_host'
            connections += 1

            detail = {'local': local, 'peer': peer, 'send_q': 'unknown',
                      'rtt_ms': 'unknown', 'retrans_current': 'unknown',
                      'retrans_total': 'unknown',
                      'inode': 'unknown', 'socket_cookie': 'unknown',
                      'attribution': matched,
                      'cgroup_instance_hint': 'unknown'}
            # Send-Q：连接行的第 offset+1 列（数字才有效）。
            if len(head) > offset + 1 and head[offset + 1].isdigit():
                send_q += int(head[offset + 1])
                detail['send_q'] = int(head[offset + 1])
            if peer != 'unknown':
                peers.append(peer)
            # 有效 inode：取第一个非 0 的；全是 0/缺失 → unknown（不可用）。
            if usable_inodes:
                detail['inode'] = usable_inodes[0]
            elif block['inode_unusable']:
                inode_unusable += 1
            cookie = re.search(r'\bsk:([0-9a-fA-F]+)', block['text'])
            if cookie:
                detail['socket_cookie'] = cookie.group(1)
            cgroup = re.search(r'\bcgroup:(\S+)', block['text'])
            if cgroup:
                # 仅作实例线索，不参与归属匹配，也不作为连接唯一键。
                detail['cgroup_instance_hint'] = cgroup.group(1)[:120]
            # 连接身份：同一条连接跨采样对齐用。inode 未知时这个键很弱，
            # 消费方不得用它做跨连接增量（换序/重连/端口复用都会失效）。
            detail['connection_key'] = '{}|{}|{}'.format(
                local, peer,
                detail['inode'] if detail['inode'] != 'unknown'
                else ('sk:' + detail['socket_cookie']
                      if detail['socket_cookie'] != 'unknown' else 'unknown'))
            if sample_id:
                detail['sample_id'] = sample_id

            text = block['text']
            rtt_match = re.search(r'\brtt:([0-9.]+)(?:/([0-9.]+))?', text)
            if rtt_match:
                try:
                    rtt = float(rtt_match.group(1))
                    rtts.append(rtt)
                    detail['rtt_ms'] = rtt
                except ValueError:
                    pass
            retrans_match = re.search(r'\bretrans:(\d+)/(\d+)', text)
            if retrans_match:
                current, total = int(retrans_match.group(1)), int(retrans_match.group(2))
                retrans_current_sum += current
                retrans_cumulative_sum += total
                retrans_known += 1
                detail['retrans_current'] = current
                detail['retrans_total'] = total
            for key in ('bytes_sent', 'bytes_acked', 'bytes_retrans',
                        'segs_out', 'segs_in', 'cwnd'):
                value_match = re.search(rf'\b{key}:(\d+)', text)
                if value_match:
                    value = int(value_match.group(1))
                    totals[key] = totals.get(key, 0) + value
                    detail[key] = value
            connection_details.append(detail)

        # 有连接但一条都没拿到重传数据 → unknown（不填 0）。
        retrans_any = retrans_known > 0
        result = {'connections': connections, 'send_q_total': send_q,
                  'retrans_total': retrans_cumulative_sum if retrans_any else 'unknown',
                  'retrans_current_total': retrans_current_sum if retrans_any else 'unknown',
                  'retrans_known_connections': retrans_known,
                  'retrans_unknown_connections': connections - retrans_known,
                  'inode_unusable_connections': inode_unusable,
                  'rtt_avg_ms': round(sum(rtts) / len(rtts), 3) if rtts else 'unknown',
                  'peers': peers[:8],
                  # 每连接明细（截断到 8 条）。全机求和只描述"这一刻"，
                  # 不用于跨采样差分；差分必须由消费方按同一条连接（connection_key
                  # 一致且 inode 有效）在相邻采样之间做，并处理重连/计数回退。
                  'connections_detail': connection_details[:8],
                  'totals': {k: v for k, v in sorted(totals.items())},
                  'attribution': attribution,
                  'matched_lines': connections}
        if filter_enabled and attribution_sources:
            if attribution_sources == {'inode'}:
                result['attribution'] = 'inode'
            elif attribution_sources == {'pid_fallback'}:
                # 明说这是回退：PID 归属可靠性弱于 inode。
                result['attribution'] = 'pid'
            else:
                result['attribution'] = 'mixed'
        result['host_wide_only'] = not filter_enabled
        result['detail_truncated'] = len(connection_details) > 8
        # 完整性：'ok' 的总数才是完整总数；'partial' 明确告诉消费方"这不是全量"。
        result['parse_status'] = parse_status
        result['unrecognized_lines'] = len(stray_lines)
        result['unrecognized_blocks'] = unrecognized_blocks
        result['header_lines'] = header_lines
        if sample_id:
            result['sample_id'] = sample_id
        return result

    def _socket_summary(self, pids) -> dict:
        """输出侧证据：只统计**这些进程**（本实例的采样对象）的连接。

        归属优先按 socket inode（读 /proc/<pid>/fd 得到），其次按 `ss -p`
        的 pid 字段；两者都拿不到时明确标记 `attribution='unknown'`，
        **绝不退化成全机汇总**（那不能作为该实例的输出侧证据）。
        """
        pid_list = [int(p) for p in (pids if isinstance(pids, (list, tuple, set))
                                     else [pids]) if str(p).isdigit()]
        summary = {'tool': 'ss', 'available': False, 'attribution': 'unknown',
                   'sampled_pids': pid_list, 'connections': 'unknown',
                   'send_q_total': 'unknown', 'retrans_total': 'unknown',
                   'retrans_current_total': 'unknown', 'rtt_avg_ms': 'unknown',
                   'peers': []}
        inodes = set()
        for pid in pid_list:
            found = self.pid_socket_inodes(pid)
            if found:
                inodes |= found
        if not pid_list and not inodes:
            summary['tool'] = 'ss(no-attribution-target)'
            return summary
        if not shutil.which('ss'):
            summary['tool'] = 'ss(not found)'
            _log_collection_once('ss-not-found', _logging.WARNING,
                                 '诊断采集：ss 不可用，输出侧连接证据记 unknown')
            return summary
        # 有界：单条命令超时 2 秒，采样节奏不变，不为此新增线程或高频查询。
        output = self._run_bounded(list(self.SS_COMMAND))
        if output is None:
            summary['tool'] = 'ss(timeout)'
            _log_collection_once('ss-command-failed', _logging.WARNING,
                                 '诊断采集：ss 命令失败或超时（%s），输出侧连接证据记 unknown',
                                 ' '.join(self.SS_COMMAND))
            return summary
        # 采样身份：同一次采样的明细共享一个 id，便于把"同一次"和"不同次"分开。
        self._sample_seq = getattr(self, '_sample_seq', 0) + 1
        sample_id = '{}#{}'.format(self.instance_id, self._sample_seq)
        summary.update(self.parse_ss(output, inodes=inodes, pids=pid_list,
                                     sample_id=sample_id))
        summary['inode_count'] = len(_ss_usable_inodes(str(i) for i in inodes))
        summary['ss_command'] = ' '.join(self.SS_COMMAND)

        if summary.get('parse_status') == 'unrecognized':
            # 命令成功、输出非空，但一个连接、一个表头都认不出来 —— 这是**解析失败**，
            # 不是"没有连接"。绝不能发布成 available=true / connections=0：那样机器
            # 消费者会把"格式不支持/采集结果无法解析"当成"确实为零"。
            summary['tool'] = 'ss(unrecognized-output)'
            summary['available'] = False
            _log_collection_once(
                'ss-parse-unrecognized', _logging.WARNING,
                '诊断采集：ss 输出无法识别（%d 行认不出来），输出侧连接证据记 unknown；'
                '样本首行=%r', summary.get('unrecognized_lines', 0),
                (output.strip().splitlines() or [''])[0][:120])
            return summary

        if summary.get('parse_status') == 'partial':
            # 认出了一部分，还有认不出来的内容：总数**不是完整总数**，必须显式标注。
            _log_collection_once(
                'ss-parse-partial', _logging.WARNING,
                '诊断采集：ss 输出部分无法识别（%d 行 / %d 块），总数不完整',
                summary.get('unrecognized_lines', 0),
                summary.get('unrecognized_blocks', 0))
        summary['available'] = True
        return summary

    def _ffmpeg_children(self, pid: int) -> list:
        """输入侧证据：本实例拉起的 FFmpeg 进程，带可防 PID 复用的身份。

        用 `/proc/<pid>/comm`（真正的可执行名）而不是 cmdline 子串区分
        shell 启动器与真 ffmpeg。**遍历全部后代**（`shell=True` 下
        ffmpeg 可能位于 shell 启动器的下一层），层级与扫描数都有上限。
        读不到就返回 unknown 而不是零。
        """
        # 1) 构建本机 parent → children 映射（只读 /proc，扫描数有上限）。
        children_map = {}
        try:
            entries = os.listdir('/proc')
        except OSError:
            return []
        scanned = 0
        for entry in entries:
            if not entry.isdigit() or scanned >= 400:
                continue
            scanned += 1
            child_pid = int(entry)
            fields = self._proc_stat_fields(child_pid)
            if fields is None:
                continue
            child_state, parent, starttime = fields
            children_map.setdefault(parent, []).append(child_pid)
        # 2) 后代遍历（离线可测的纯函数），收集 ffmpeg / launcher 角色。
        children = []
        for depth, child_pid in self._walk_descendants(pid, children_map):
            comm = self._read_first_line(f'/proc/{child_pid}/comm') or 'unknown'
            normalised = comm.lower()
            is_ffmpeg = normalised.startswith('ffmpeg')
            is_launcher = normalised in ('sh', 'bash', 'dash', 'cmd')
            if not (is_ffmpeg or is_launcher):
                continue
            try:
                with open(f'/proc/{child_pid}/cmdline', 'rb') as handle:
                    cmdline = handle.read().replace(b'\x00', b' ').decode(
                        'utf-8', 'replace').strip()
            except OSError:
                cmdline = ''
            try:
                fd_count = len(os.listdir(f'/proc/{child_pid}/fd'))
            except OSError:
                fd_count = 'unknown'
            proc_status = self._proc_status(child_pid)
            wchan = self._read_first_line(f'/proc/{child_pid}/wchan')
            children.append({
                'pid': child_pid,
                'role': 'ffmpeg' if is_ffmpeg else 'launcher',
                'depth': depth,
                'state': self._proc_stat_fields(child_pid)[0]
                if self._proc_stat_fields(child_pid) else 'unknown',
                'comm': comm,
                'starttime': (self._proc_stat_fields(child_pid) or (None, None, 'unknown'))[2],
                'boot_id': self.boot_id(),
                'fd_count': fd_count,
                'threads': proc_status.get('Threads', 'unknown'),
                'rss_kb': (proc_status.get('VmRSS', 'unknown kB')
                           .replace(' kB', '') if 'VmRSS' in proc_status else 'unknown'),
                'wchan': wchan or 'unknown',
                # 只保留输入侧形状，不记录完整推流 URL。
                'cmdline_shape': redact_url(cmdline)[:200],
            })
        return children

    @staticmethod
    def _walk_descendants(root_pid: int, children_map: dict,
                          max_depth: int = 4, max_nodes: int = 64) -> list:
        """BFS 遍历 root 的全部后代（离线可测的纯函数）。

        返回 ``[(depth, pid)]``；有环保护与节点/深度上限，防忙循环。
        """
        result = []
        seen = {root_pid}
        frontier = [root_pid]
        depth = 0
        while frontier and depth < max_depth:
            next_frontier = []
            for pid in frontier:
                for child in children_map.get(pid, ()):
                    if child in seen:
                        continue
                    seen.add(child)
                    result.append((depth + 1, child))
                    if len(result) >= max_nodes:
                        return result
                    next_frontier.append(child)
            frontier = next_frontier
            depth += 1
        return result

    def collect(self) -> dict:
        """只读采集；覆盖**同机所有已登记实例**，字段不可用标记 unknown。

        第 4 轮 T6：**每个实例各自**有一份 socket 采样（按该实例的推流
        进程归因），不再展平成顶层汇总；参与者登记时的进程身份与当前
        /proc 身份核对，PID 复用时该实例明确标记 `pid_reuse_suspected`。
        """
        participants = self.read_participants() or [
            {'pid': os.getpid(), 'instance_id': self.instance_id,
             'launcher_pid': 'unknown'}]
        own_pid = os.getpid()
        load = self._read_first_line('/proc/loadavg')
        status = self._proc_status(own_pid)
        instances = []
        for participant in participants:
            try:
                worker_pid = int(participant.get('pid'))
            except (TypeError, ValueError):
                continue
            identity = self._proc_identity(worker_pid)
            # PID 复用检测：登记时保存的 starttime/boot_id 与当前进程身份不一致
            # → 该 PID 现在是另一个进程，归因必须降级为 unknown。
            registered = participant.get('worker_identity') or {}
            pid_reuse = False
            if (registered.get('starttime') not in (None, 'unknown')
                    and identity.get('alive')):
                pid_reuse = (str(registered.get('starttime'))
                             != str(identity.get('starttime')))
            try:
                launcher = int(participant.get('launcher_pid'))
                launcher_identity = self._proc_identity(launcher)
            except (TypeError, ValueError):
                launcher = 'unknown'
                launcher_identity = {'pid': 'unknown', 'alive': False,
                                     'state': 'unknown', 'comm': 'unknown',
                                     'exe': 'unknown', 'starttime': 'unknown',
                                     'boot_id': self.boot_id()}
            entry_status = self._proc_status(worker_pid)
            entry = {
                'worker_pid': worker_pid,
                'instance_id': participant.get('instance_id', 'unknown'),
                'worker_identity': identity,
                'registered_identity': registered,
                'pid_reuse_suspected': pid_reuse,
                'launcher_pid': launcher,
                'launcher_identity': launcher_identity,
                'threads': entry_status.get('Threads', 'unknown'),
                'rss_kb': (entry_status.get('VmRSS', '').replace(' kB', '')
                           if 'VmRSS' in entry_status else 'unknown'),
                'ffmpeg_children': self._ffmpeg_children(worker_pid),
                'is_self': worker_pid == own_pid,
            }
            # 输出侧证据按实例采集：只统计该实例推流进程（或其 shell 启动器
            # 后代里的 ffmpeg）持有的连接，绝不展平成全机汇总。
            pusher_pids = self._pusher_pids(entry)
            entry['pusher_pids'] = pusher_pids
            if pid_reuse:
                entry['socket'] = {'tool': 'ss', 'available': False,
                                   'attribution': 'pid_reuse_suspected',
                                   'connections': 'unknown'}
            else:
                entry['socket'] = self._socket_summary(pusher_pids)
            instances.append(entry)
        record = {
            'ts': utc_now_iso(),
            'monotonic': round(time.monotonic(), 3),
            'instance_id': self.instance_id,
            'pid': own_pid,
            'loadavg': load.split()[0] if load else 'unknown',
            'fd_count': 'unknown',
            'threads': status.get('Threads', 'unknown'),
            'rss_kb': (status.get('VmRSS', '').replace(' kB', '')
                       if 'VmRSS' in status else 'unknown'),
            'rss_peak_kb': 'unknown',
            # 输出侧证据在 `instances[*].socket` 里逐实例给出；
            # 这里只保留采样器自身进程层面的信息，不做跨实例连接归因。
            'instances': instances,
            'instance_count': len(instances),
            'participants': len(participants),
            'registry_owner': self.read_registry().get('owner_pid', 'unknown'),
        }
        try:
            record['fd_count'] = len(os.listdir(f'/proc/{own_pid}/fd'))
        except OSError:
            pass
        try:
            import resource
            # ru_maxrss 是峰值，不是当前值；两者分开记录。
            record['rss_peak_kb'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            pass
        self.attributed_pids = [pid for entry in instances
                                for pid in entry.get('pusher_pids', [])]
        return record

    @staticmethod
    def _pusher_pids(instance_entry) -> list:
        """一个实例的推流相关 pid：优先真 ffmpeg，其次 shell 启动器。"""
        children = instance_entry.get('ffmpeg_children') or []
        ffmpeg = [c['pid'] for c in children if c.get('role') == 'ffmpeg']
        if ffmpeg:
            return ffmpeg
        return [c['pid'] for c in children if c.get('role') == 'launcher']

    def stop(self):
        self._closed.set()

    def run(self):
        if not self.SUPPORTED:
            return
        if not self._acquire_single_instance():
            return
        self.acquired = True
        try:
            while not self._closed.is_set():
                try:
                    if self.sink is not None:
                        self.sink.submit(self.collect())
                    self.samples += 1
                except Exception:
                    self.errors += 1
                self._closed.wait(self.sample_period())
        finally:
            if self._lock_handle is not None:
                try:
                    self._lock_handle.close()
                except Exception:
                    pass

    def stats(self) -> dict:
        return {'supported': self.SUPPORTED, 'running': self.is_alive(),
                'acquired': self.acquired, 'samples': self.samples,
                'errors': self.errors,
                'lock_path': str(self.shared_dir() / self.LOCK_NAME),
                'reason': self.unavailable_reason}


class Diagnostics:
    """旁路观测入口。默认关闭；启用后只记录。"""

    def __init__(self, data_dir=None, instance_id: str = None):
        self._lock = threading.RLock()
        self.instance_id = instance_id or f'{os.getpid()}-{int(time.time())}'
        self.data_dir = Path(data_dir) if data_dir else None
        self.directory = None
        self.enabled = False
        self.media_progress = False
        self.host_sampling = False
        self.max_bytes = DEFAULT_MAX_FILE_BYTES
        self.retain_days = DEFAULT_RETAIN_DAYS
        self.events: deque = deque(maxlen=EVENT_BUFFER)
        self._events_bytes = 0
        self.truncated_records = 0
        self.event_dropped = 0
        self.event_sink = None
        self.progress_sink = None
        self.host_sink = None
        self.host_sampler = None
        self.budget = None
        self._pusher_started_at = None   # 最近一次真实 FFmpeg 启动的单调时间
        self._last_launcher_pid = None   # 参与者登记保留的启动器 PID（心跳不覆盖）
        self._channels = []
        self._drain_only = 0
        self._unreaped = []          # 停止超时未回收的线程（保留所有权以便观测）
        self._participant_thread = None
        self._participant_stop = threading.Event()
        self._configured = False
        # 有界旁路写线程：清单/参与者登记等磁盘写入全部经它异步执行。
        # 队列满或线程死掉只损失诊断数据，绝不拖住推流监控或控制路径。
        # 生命周期（第 5 轮 U2）：每个 worker 持有自己的停止事件；任务带
        # configure 代际——停用/重启后代际过期的任务直接丢弃，不会把旧任务
        # 写进新目录/新预算。shutdown/configure 都会停止并回收 worker。
        self._bypass_queue: 'queue.Queue' = queue.Queue(maxsize=64)
        self._bypass_dropped = 0
        self._bypass_executed = 0
        self._bypass_stale_dropped = 0
        self._bypass_thread = None
        self._bypass_worker_stop = None   # 当前 worker 的停止事件（随 worker 一起更换）
        self._bypass_generation = 0       # configure 周期计数
        # 配置代际（第 6 轮）：每次 configure 的第 1 段 +1；锁外准备返回后
        # 只有"仍是最新一代"才允许在提交段发布目录/预算。旧代准备结果直接丢弃。
        self._configure_generation = 0
        # 已确认退出的通道历史（有界）：活跃集合只保留 _channels。
        self._retired_channels = []

    # ---------------- 有界旁路执行器（磁盘写入不出现在调用线程） ----------------

    def _enqueue_bypass(self, job, description: str) -> bool:
        """把一个磁盘写入任务交给旁路线程；队列满/已停用只丢任务并计数。

        返回是否被接受（调用方据此做去重标志复位等收尾；既有调用方
        不检查返回值，行为不变）。
        """
        with self._lock:
            # 停止哨兵：shutdown/configure(False) 后保持置位，新任务一律丢弃
            # （绝不在关闭后新建 worker 执行任务）；重新启用时由 configure 清空。
            stop_event = self._bypass_worker_stop
            if stop_event is not None and stop_event.is_set():
                self._bypass_dropped += 1
                return False
            generation = self._bypass_generation
            try:
                self._bypass_queue.put_nowait((description, job, generation))
            except queue.Full:
                self._bypass_dropped += 1
                return False
        self._ensure_bypass_thread()
        return True

    def _ensure_bypass_thread(self):
        with self._lock:
            thread = self.__dict__.get('_bypass_thread')
            if thread is not None and thread.is_alive():
                return
            # 启动前复核停止哨兵/关闭状态（第 6 轮）：shutdown 与
            # enqueue→启动之间可能交错。哨兵已置位说明配置已关闭或正在
            # 停止——此时绝不创建新 worker，也不得用新 Event 覆盖哨兵
            # （否则关闭后的迟到启动会创建一个永远运行的空转线程）。
            sentinel = self._bypass_worker_stop
            if sentinel is not None and sentinel.is_set():
                return
            stop_event = threading.Event()
            thread = threading.Thread(target=self._bypass_worker,
                                      args=(stop_event,),
                                      name='DiagBypassWriter', daemon=True)
            self.__dict__['_bypass_thread'] = thread
            self._bypass_worker_stop = stop_event
            thread.start()

    def _bypass_worker(self, stop_event: threading.Event):
        while not stop_event.is_set():
            try:
                description, job, generation = self._bypass_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                stale = generation != self._bypass_generation
            if stale:
                # 旧代任务：停用/重配置后不再执行，绝不把旧任务写进新目录/新预算。
                with self._lock:
                    self._bypass_stale_dropped += 1
                continue
            try:
                job()
                with self._lock:
                    self._bypass_executed += 1
            except Exception:
                # 旁路证据写失败只记账，绝不外传异常。
                with self._lock:
                    self._bypass_dropped += 1

    def _bypass_stats_unlocked(self) -> dict:
        thread = self._bypass_thread
        return {'queued': self._bypass_queue.qsize(),
                'capacity': self._bypass_queue.maxsize,
                'executed': self._bypass_executed,
                'dropped': self._bypass_dropped,
                'stale_dropped': self._bypass_stale_dropped,
                'alive': bool(thread is not None and thread.is_alive())}

    # ---------------- 配置（只在生命周期边界生效） ----------------

    def configure(self, enabled: bool, media_progress: bool = False,
                  host_sampling: bool = False, max_mb: int = 8,
                  retain_days: int = 7, data_dir=None):
        """按当前设置重建/关闭采集通道。

        只影响之后启动的进程与采样线程；不会重启或关闭任何正在运行的推流。
        锁内只做状态变更与停止信号（**无 join、无目录 I/O**）；线程 join 与
        目录创建都在锁外执行，慢退出的线程/慢磁盘不会拖住持有同一把锁的
        控制与推流相关路径（第 5 轮 U2）。
        """
        with self._lock:
            # ---- 第 1 段（锁内）：状态变更 + 停止信号 ----
            # 代际在此刻固化：之后任何更新的 configure 都会使本次的锁外
            # 准备结果过期，提交段必须放弃发布（旧配置不能覆盖新配置）。
            self._configure_generation += 1
            configure_generation = self._configure_generation
            if data_dir is not None:
                self.data_dir = Path(data_dir)
            target_dir = (Path(self.data_dir) / 'diagnostics') if self.data_dir else None
            dir_changed = (target_dir != self.directory)
            self.enabled = bool(enabled)
            self.media_progress = bool(enabled and media_progress)
            self.host_sampling = bool(enabled and host_sampling)
            self.max_bytes = max(1, int(max_mb or 8)) * 1024 * 1024
            self.retain_days = max(1, int(retain_days or 7))
            self._configured = True
            # 旁路 worker：每个 configure 周期都停掉重建（代际 +1 使旧任务作废）。
            self._bypass_generation += 1
            to_reap = self._collect_worker_reapers_unlocked()
            if not self.enabled or dir_changed:
                # 停用 / 换目录：旧 sink 不能再往旧目录写。
                to_reap.extend(self._stop_sinks_unlocked())
            channels_to_join = []
            if not self.enabled:
                channels_to_join = list(self._channels)
                for channel in channels_to_join:
                    channel.stop()
                self._channels = []

        # ---- 第 2 段（锁外）：目录 I/O + 线程 join ----
        # 只在启用时创建目录：默认关闭必须零文件、零目录（S4 约定）。
        directory, budget = (None, None)
        if self.enabled:
            directory, budget = self._prepare_directory(target_dir)
        self._reap_threads(to_reap)
        # 统一通道收尾：仍存活（还在排空健康管道）的登记为未回收，
        # 已退出的进入有界历史——不把"清空集合"当成"回收完成"。
        self._reap_channels(channels_to_join, timeout=3.0)

        # ---- 第 3 段（锁内）：用准备好的目录/预算重建 ----
        with self._lock:
            if configure_generation != self._configure_generation:
                # 锁外准备期间来了更新的 configure：本次准备结果过期，
                # 绝不发布旧目录/旧预算，也不得清空新配置的目录或哨兵。
                # （过期预算只是普通对象，直接丢弃，无线程需要回收。）
                return
            self.directory = directory
            self.budget = budget
            if self.enabled:
                # 重新启用：清掉已置位的哨兵，允许创建新的旁路 worker。
                self._bypass_worker_stop = None
                self._ensure_sinks_unlocked()
                to_reap = self._sync_host_sampler_unlocked()
            else:
                to_reap = []
        self._reap_threads(to_reap)

    def _prepare_directory(self, target_dir):
        """目录创建与预算构建（无锁）：慢磁盘不拖住诊断锁。"""
        if target_dir is None:
            return None, None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None, None
        budget = self.budget
        if budget is None or Path(budget.directory) != target_dir:
            budget = _DirectoryBudget(
                target_dir,
                total_bytes=(DIRECTORY_BUDGET_MB * 1024 * 1024),
                max_record_bytes=DEFAULT_MAX_RECORD_BYTES)
        return target_dir, budget

    def _retire_channels_unlocked_view(self, channels):
        """把确认退出的通道移入有界历史（调用方保证锁外已 join）。"""
        with self._lock:
            retired = [c for c in channels
                       if not getattr(c, 'is_alive', lambda: True)()]
            self._channels = [c for c in self._channels if c not in retired]
            # 去重：同一通道只进一次历史（多次确认路径不产生重复条目）。
            retired = [c for c in retired if c not in self._retired_channels]
            if retired:
                self._retired_channels.extend(retired)
                del self._retired_channels[:-RETIRED_CHANNEL_HISTORY]

    def _reap_threads(self, threads, timeout: float = 3.0):
        """锁外确认线程回收；回收不了也保留**对象引用**与可核验状态。"""
        for thread in threads or []:
            if thread is None:
                continue
            thread.join(timeout=timeout)
            with self._lock:
                if thread.is_alive():
                    self._register_unreaped_unlocked(thread, kind='thread')
                else:
                    # 只按对象身份移除（第 6 轮）：同名线程互相覆盖会丢掉
                    # 另一个仍存活同名的未回收登记。
                    self._unreaped = [item for item in self._unreaped
                                      if item.get('thread') is not thread]

    def _register_unreaped_unlocked(self, thread, kind: str) -> None:
        """登记一个停止超时仍未退出的线程（锁内）。

        未回收是**仍有所有权的事实**：条目保留线程对象本身，后续仍可
        join/观测；按对象身份去重，同名线程互不清除。
        """
        self._unreaped = [item for item in self._unreaped
                          if item.get('thread') is not thread]
        self._unreaped.append({'name': thread.name, 'kind': kind,
                               'alive': True, 'thread': thread})

    def _confirm_unreaped(self, timeout: float = 0.5) -> None:
        """锁外确认未回收登记的线程是否已退出（锁内更新登记）。

        已退出的线程：从登记移除；progress_channel 类进入有界历史，
        其余（sink/worker 等）无历史需求。仍存活的保留登记——
        既不丢所有权，也不无限保留已确认的死对象。
        """
        with self._lock:
            entries = list(self._unreaped)
        retired_channels = []
        for item in entries:
            thread = item.get('thread')
            if thread is None:
                continue
            try:
                thread.join(timeout=timeout)
            except Exception:
                continue
            if thread.is_alive():
                continue
            with self._lock:
                self._unreaped = [e for e in self._unreaped
                                  if e.get('thread') is not thread]
                if item.get('kind') == 'progress_channel':
                    retired_channels.append(thread)
        if retired_channels:
            self._retire_channels_unlocked_view(retired_channels)

    def _reap_channels(self, channels, timeout: float = 3.0) -> None:
        """统一通道收尾（configure(False)/shutdown/detach 共用）。

        join 在锁外；join 后仍存活的通道（还在排空健康 FFmpeg 的管道）
        必须保留所有权并登记为未回收——绝不允许"从集合清空"被当成
        "回收完成"。已退出的进入有界历史。
        """
        for channel in channels or []:
            if channel is None:
                continue
            try:
                channel.join(timeout=timeout)
            except Exception:
                pass
        with self._lock:
            retired, unreaped = [], []
            for channel in channels or []:
                if channel is None:
                    continue
                try:
                    alive = channel.is_alive()
                except Exception:
                    alive = True
                if alive:
                    unreaped.append(channel)
                else:
                    retired.append(channel)
            # 传入的通道一律离开活跃集合：退出的进有界历史；仍存活的
            # （还在排空健康管道）以未回收登记保留所有权，绝不允许
            # "从集合清空"被当成"回收完成"。
            if retired or unreaped:
                self._channels = [c for c in self._channels
                                  if c not in retired and c not in unreaped]
            for channel in unreaped:
                self._register_unreaped_unlocked(channel,
                                                 kind='progress_channel')
        if retired:
            self._retire_channels_unlocked_view(retired)

    def refresh_from_settings(self):
        """从应用设置读取开关（缺失字段按关闭处理，兼容旧配置）。"""
        try:
            from app.api.settings import load_settings
            settings = load_settings()
            data_dir = None
            try:
                from app.core.config import get_data_path
                data_dir = get_data_path('diagnostics').parent
            except Exception:
                pass
            self.configure(
                enabled=getattr(settings, 'diagnostics_enabled', False),
                media_progress=getattr(settings, 'diagnostics_media_progress', False),
                host_sampling=getattr(settings, 'diagnostics_host_sampling', False),
                max_mb=getattr(settings, 'diagnostics_max_file_mb', 8),
                retain_days=getattr(settings, 'diagnostics_retain_days', 7),
                data_dir=data_dir,
            )
        except Exception:
            self.configure(False)

    def _ensure_sinks_unlocked(self):
        if self.directory is None:
            return
        if self.event_sink is None or not self.event_sink.is_alive():
            self.event_sink = _JsonlSink(self.directory / 'events.jsonl',
                                         self.max_bytes, self.retain_days,
                                         budget=self.budget)
            self.event_sink.start()
        if self.progress_sink is None or not self.progress_sink.is_alive():
            self.progress_sink = _JsonlSink(self.directory / 'media-progress.jsonl',
                                            self.max_bytes, self.retain_days,
                                            budget=self.budget)
            self.progress_sink.start()

    @staticmethod
    def _stop_sink_unlocked(sink):
        """锁内助手：**只发停止信号**，返回待回收线程（join 在锁外）。"""
        if sink is None:
            return []
        try:
            sink.stop()
        except Exception:
            pass
        return [sink]

    def _stop_sinks_unlocked(self):
        reapers = []
        for sink in (self.event_sink, self.progress_sink, self.host_sink):
            reapers.extend(self._stop_sink_unlocked(sink))
        self.event_sink = self.progress_sink = self.host_sink = None
        return reapers

    def _collect_worker_reapers_unlocked(self):
        """锁内收集全部 worker 线程的停止信号与待回收对象。

        覆盖：主机采样器、参与者心跳、旁路写线程。这里**只 stop**，
        join 由调用方在锁外统一执行（第 5 轮 U2）。
        """
        reapers = []
        # 1) 参与者心跳
        self._stop_participant_heartbeat_unlocked()
        if self._participant_thread is not None:
            reapers.append(self._participant_thread)
            self._participant_thread = None
        # 2) 旁路写线程（停止事件保留为“已置位”哨兵：关闭后 _enqueue_bypass
        #    直接丢弃新任务；重新启用时由 configure 清空重建）。
        #    即使从未创建过 worker，也要安装已置位的哨兵——否则关闭后
        #    首次 enqueue 会新建 worker 并执行任务。
        stop_event = self._bypass_worker_stop
        if stop_event is None:
            stop_event = threading.Event()
            self._bypass_worker_stop = stop_event
        stop_event.set()
        thread = self.__dict__.get('_bypass_thread')
        if thread is not None:
            reapers.append(thread)
            self.__dict__['_bypass_thread'] = None
        # 3) 主机采样器
        if self.host_sampler is not None:
            try:
                self.host_sampler.stop()
            except Exception:
                pass
            reapers.append(self.host_sampler)
            self.host_sampler = None
        return reapers

    def _sync_host_sampler_unlocked(self):
        reapers = []
        if not self.host_sampling:
            self._stop_participant_heartbeat_unlocked()
            if self._participant_thread is not None:
                reapers.append(self._participant_thread)
                self._participant_thread = None
            if self.host_sampler is not None:
                try:
                    self.host_sampler.stop()
                except Exception:
                    pass
                reapers.append(self.host_sampler)
                self.host_sampler = None
            # 必须回收此前为采样创建的落盘线程，不能只丢引用。
            reapers.extend(self._stop_sink_unlocked(self.host_sink))
            self.host_sink = None
            return reapers
        if not HostSampler.SUPPORTED:
            # 平台不支持：已经建过的 sink 也必须回收，避免留下悬挂线程。
            reapers.extend(self._stop_sink_unlocked(self.host_sink))
            self.host_sink = None
            return reapers
        if self.directory is None:
            return reapers
        # 无论是否抢到采样锁，本实例都要登记参与者：唯一采样器据此覆盖全部实例。
        # 登记走旁路线程，configure 不在锁内做磁盘写入。
        self._publish_participant_async()
        self._ensure_participant_heartbeat_unlocked()
        if self.host_sampler is not None and self.host_sampler.is_alive():
            return reapers
        self.host_sink = _JsonlSink(self.directory / 'host-sample.jsonl',
                                    self.max_bytes, self.retain_days,
                                    budget=self.budget)
        self.host_sink.start()
        self.host_sampler = HostSampler(self.directory, self.instance_id,
                                        self.host_sink,
                                        pusher_started_at=lambda: self._pusher_started_at)
        self.host_sampler.start()
        return reapers

    def _publish_participant_async(self, launcher_pid=None) -> None:
        """参与者登记走有界旁路线程：慢磁盘只损失诊断数据，不拖住调用方。

        与是否持采样锁无关——每个实例都登记自己，唯一采样器据此覆盖全部实例。
        """
        if not HostSampler.SUPPORTED:
            return

        def _write():
            sampler = self.host_sampler or HostSampler(
                self.directory or Path('.'), self.instance_id, None)
            sampler.publish_participant(launcher_pid)

        self._enqueue_bypass(_write, 'publish_participant')

    def _ensure_participant_heartbeat_unlocked(self):
        """参与者心跳线程：每 30 秒刷新一次，避免被当作过期实例清理。

        心跳不传 launcher_pid（保留 note_pusher_start 登记过的值），
        且登记走旁路线程——心跳线程不做同步磁盘 I/O。
        """
        thread = self._participant_thread
        if thread is not None and thread.is_alive():
            return
        def _beat():
            while not self._participant_stop.wait(30):
                with self._lock:
                    if not self.host_sampling:
                        return
                self._publish_participant_async()
        self._participant_stop.clear()
        self._participant_thread = threading.Thread(
            target=_beat, name='DiagParticipant', daemon=True)
        self._participant_thread.start()

    def _stop_participant_heartbeat_unlocked(self):
        self._participant_stop.set()

    # ---------------- 事件 ----------------

    def record(self, kind: str, **fields):
        """记录一条结构化事件。关闭时直接返回（零采集开销）。

        脱敏与**字节裁剪**都在进入内存缓冲之前完成：
        事件缓冲的条数上限（500）之外还有一个字节上限，二者共同约束内存。
        """
        with self._lock:
            if not self.enabled:
                return
            record = {
                'ts': utc_now_iso(),
                'instance_id': self.instance_id,
                'pid': os.getpid(),
                'kind': kind,
            }
            record.update(sanitize_record(fields))
            record = _bound_record(record, self._record_limit_unlocked())
            if _json_bytes(record) > self._record_limit_unlocked():
                self.truncated_records += 1
            self._append_event_unlocked(record)
            if self.event_sink is not None:
                self.event_sink.submit(record)

    def _record_limit_unlocked(self) -> int:
        if self.budget is not None:
            return self.budget.max_record_bytes
        return DEFAULT_MAX_RECORD_BYTES

    def _append_event_unlocked(self, record: dict):
        """条数 + 字节双重上限的事件缓冲。"""
        self.events.append(record)
        self._events_bytes += _json_bytes(record)
        while len(self.events) > EVENT_BUFFER:
            self._events_bytes -= _json_bytes(self.events.pop(0))
        while self._events_bytes > EVENT_BUFFER_BYTES and self.events:
            self._events_bytes -= _json_bytes(self.events.pop(0))

    # ---------------- 媒体进度 ----------------

    def identity_probe_enabled(self) -> bool:
        """进程身份采集（/proc 扫描、播放列表指纹）是否开启。

        默认关闭时必须返回 False，调用方据此完全不做探测——
        否则“关闭零采集开销”只是口号。
        """
        with self._lock:
            return bool(self.enabled and self.media_progress)

    def note_playlist(self, fingerprint: str, entries, **identity) -> None:
        """把本次实际使用的**有序**播放列表写下来（经有界旁路，异步落盘）。

        指纹只能证明“列表变过”；文件被删除后顺序无法还原。这里按指纹落一份
        有序清单（有上限），并与代际/尝试号关联。写入走旁路线程：监控线程
        只做内存中的分片构建，队列满或磁盘慢都只损失诊断数据。
        """
        if not self.identity_probe_enabled() or self.directory is None:
            return
        entries = list(entries)
        safe_entries = []
        for item in entries[:MAX_PLAYLIST_ENTRIES]:
            try:
                safe_entries.append(sanitize_value('playlist_entry', str(item)))
            except Exception:
                continue
        truncated = max(0, len(entries) - MAX_PLAYLIST_ENTRIES)
        # 分片：按**真实序列化字节**切分，每片都完整保持在单条记录预算内；
        # entry_order_start/end 用实际累计索引（不是 index*固定片长），
        # 逐片可重组出完整有序列表，缺片/截断明确标记（第 5 轮 U4）。
        record_limit = self._record_limit_unlocked()
        manifest_id = f'{fingerprint or "unknown"}-{int(time.time() * 1000)}'
        base = {
            'instance_id': self.instance_id,
            'playlist_fingerprint': fingerprint,
            'playlist_size': len(entries),
            'playlist_truncated_tail': truncated,
            'manifest_id': manifest_id,
        }
        base.update(sanitize_record(identity))
        shard_lines = self._shard_playlist_entries(safe_entries, record_limit, base)

        def _write():
            path = self.directory / f'playlist-{fingerprint or "unknown"}.jsonl'
            blob = ''.join(shard_lines)      # 预序列化：写入时不再裁剪
            nbytes = len(blob.encode('utf-8'))
            if self.budget is not None and not self.budget.reserve(nbytes, protect=path):
                return
            try:
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(blob)
            except Exception:
                if self.budget is not None:
                    self.budget.rollback(nbytes)
                return
            if self.budget is not None:
                self.budget.commit(nbytes)

        self._enqueue_bypass(_write, 'note_playlist')

    @staticmethod
    def _shard_playlist_entries(safe_entries, record_limit: int, base: dict) -> list:
        """把清单条目按**真实序列化字节**切成完整行（保留顺序）。

        - 每行是预序列化的 JSON（含 part_index/part_total/entry_order_start/
          entry_order_end），写入路径上不再有任何裁剪步骤；
        - 分片大小由实测字节决定（表头开销按 2 位 part_total 预留——足够
          覆盖真实片数），不依赖每片恰好 50 条的经验假设；
        - 极端情况下单条超预算（理论上只有路径超长才会）：该片保留
          range 信息并把 ``entries_clipped`` 显式置真，绝不静默丢序号。
        """
        def serialize(part_index, part_total, start, items):
            payload = dict(base)
            payload.update({
                'ts': utc_now_iso(),
                'part_index': part_index,
                'part_total': part_total,
                'entry_order_start': start,
                'entry_order_end': start + len(items),
                'entries': items,
            })
            return json.dumps(payload, ensure_ascii=False, default=str) + '\n'

        if not safe_entries:
            empty = serialize(0, 1, 0, [])
            payload = json.loads(empty)
            payload['empty_manifest'] = True
            return [json.dumps(payload, ensure_ascii=False, default=str) + '\n']

        # 表头开销按 2 位 part_total 预留（真实片数最多几百，2 位足够保守）。
        header_overhead = len(serialize(0, 99, 0, []).encode('utf-8'))
        max_shard_bytes = max(256, record_limit - header_overhead)
        byte_lengths = [len(item.encode('utf-8')) + 1 for item in safe_entries]

        spec = []
        index = 0
        total = len(safe_entries)
        while index < total:
            start = index
            used = 0
            while index < total and used + byte_lengths[index] <= max_shard_bytes:
                used += byte_lengths[index]
                index += 1
            if index == start:
                # 单条就超出片预算：单独成片并显式标记（保留 range 与截断标记）。
                index = start + 1
            spec.append((start, index))

        lines = []
        part_total = len(spec)
        for part_index, (start, end) in enumerate(spec):
            line = serialize(part_index, part_total, start, safe_entries[start:end])
            if len(line.encode('utf-8')) > record_limit:
                # 兜底：保持 range 可核验，entries 显式标记被裁剪。
                payload = dict(base)
                payload.update({
                    'ts': utc_now_iso(),
                    'part_index': part_index,
                    'part_total': part_total,
                    'entry_order_start': start,
                    'entry_order_end': end,
                    'entries': [],
                    'entries_clipped': True,
                    'clipped_reason': 'record_limit',
                })
                line = json.dumps(payload, ensure_ascii=False, default=str) + '\n'
            lines.append(line)
        return lines

    def note_pusher_start(self, **identity):
        """登记一次真实推流进程启动：主机采样启动期高频绑定到它。

        只在锁内更新内存状态；参与者登记（磁盘写入）走旁路线程，
        慢磁盘不会拖住推流监控线程。
        """
        launcher_pid = identity.get('launcher_pid')
        with self._lock:
            self._pusher_started_at = time.monotonic()
            if launcher_pid is not None:
                self._last_launcher_pid = launcher_pid
            publish = self.host_sampling
        if publish:
            self._publish_participant_async(self._last_launcher_pid)
        if self.enabled:
            self.record('pusher_spawn', **identity)

    def media_progress_enabled(self) -> bool:
        with self._lock:
            return bool(self.enabled and self.media_progress and self.progress_sink is not None)

    def progress_args(self):
        """给新启动的 FFmpeg 使用的额外参数；关闭时为空。"""
        if not self.media_progress_enabled():
            return [], {}
        return ['-progress', 'pipe:1', '-stats_period', str(PROGRESS_PERIOD_SECONDS)], {}

    def attach_progress(self, process, **identity):
        """接管一个已启动子进程的进度通道；不可用时返回 None。

        创建失败只影响诊断，不影响推流：调用方拿到 None 即可继续。
        """
        if not self.media_progress_enabled():
            return None
        stream = getattr(process, 'stdout', None)
        if stream is None:
            return None
        meta = {'instance_id': self.instance_id, 'launcher_pid': process.pid}
        meta.update(sanitize_record(dict(identity)))
        channel = None
        try:
            channel = ProgressChannel(stream, meta, self.progress_sink)
            with self._lock:
                self._channels.append(channel)
            channel.start()
            return channel
        except Exception:
            # 通道创建失败也不能影响推流；不保留半成品。
            with self._lock:
                if channel in self._channels:
                    self._channels.remove(channel)
            return None

    def attach_drain_only(self, process, **identity):
        """读取器不可用后的兜底：只把管道读干，不解析不落盘。

        调用方（控制器）在“存活子进程 + 无有效消费者”时调用；
        这里只负责创建一个排空通道，不做任何控制决策。
        """
        stream = getattr(process, 'stdout', None)
        if stream is None:
            return None
        try:
            meta = {'instance_id': self.instance_id,
                    'launcher_pid': process.pid,
                    'drain_mode': 'drain_only'}
            meta.update(sanitize_record(dict(identity)))
            channel = ProgressChannel(stream, meta, None, drain_only=True)
            with self._lock:
                self._channels.append(channel)
                self._drain_only += 1
            channel.start()
            return channel
        except Exception:
            return None

    def detach_progress(self, channel, timeout: float = 5.0):
        if channel is None:
            return
        channel.stop()
        # 统一收尾：超时仍未退出的通道保留所有权（登记未回收），
        # 不允许 detach 丢掉仍在排空的通道（第 6 轮出口检查）。
        self._reap_channels([channel], timeout=timeout)

    def reap_exited_channels(self) -> int:
        """把已确认退出的通道移出活跃集合（进入有界历史），返回回收数。

        反复接管时，确认退出的旧消费者不能在 ``_channels`` 里无界积累
        （第 5 轮 U2）：这里以"线程已退出且 exited 置位"为准回收；
        尚未退出的保留在活跃集合（其所有权仍需观测），由后续回收或
        shutdown 处理。历史集合有界（RETIRED_CHANNEL_HISTORY），
        计数只增不减，不保留无限对象引用。

        第 6 轮：同时确认未回收登记中已退出的 progress_channel 条目——
        停止超时的通道随后真正退出时，在这里完成退役（登记移除、
        历史入档），未回收登记不会无限保留已确认的死对象。
        """
        with self._lock:
            still_active, retired = [], []
            for channel in self._channels:
                try:
                    done = channel.exited.is_set() and not channel.is_alive()
                except Exception:
                    done = False
                (retired if done else still_active).append(channel)
            self._channels = still_active
            # 未回收登记中的通道条目：已退出的退役。
            for item in list(self._unreaped):
                if item.get('kind') != 'progress_channel':
                    continue
                channel = item.get('thread')
                try:
                    done = (channel is not None and channel.exited.is_set()
                            and not channel.is_alive())
                except Exception:
                    done = False
                if done:
                    self._unreaped.remove(item)
                    retired.append(channel)
            self._retire_channels_unlocked_view(retired)
            return len(retired)

    # ---------------- 观测与导出 ----------------

    def _unreaped_snapshot_unlocked(self) -> list:
        """未回收登记的**可序列化**快照（内部对象与对外状态分离）。

        只含名称、类别、对象身份与存活状态；线程/锁对象绝不进入
        HTTP JSON（第 6 轮：线程对象直接序列化会让状态接口 500）。
        """
        snapshot = []
        for item in self._unreaped:
            thread = item.get('thread')
            try:
                alive = bool(thread is not None and thread.is_alive())
            except Exception:
                alive = True
            snapshot.append({
                'name': str(item.get('name', 'unknown')),
                'kind': str(item.get('kind', 'thread')),
                'identity': id(thread) if thread is not None else None,
                'alive': alive,
                'state': 'stopping' if alive else 'exited',
            })
        return snapshot

    def stats_fast(self) -> dict:
        """面板路由用快照：只读内存计数，**不拿预算锁、不做磁盘扫描、不 join**。

        诊断目录的预算维护（扫描/stat/回收）在慢磁盘上可能长时间持有预算锁；
        状态接口绝不能等待它，否则诊断旁路反而拖住面板事件循环（第 4 轮 T4）。
        字段不可知时明确标 unknown，不填零。
        """
        sinks = {}
        for name, sink in (('events', self.event_sink),
                           ('media', self.progress_sink),
                           ('host', self.host_sink)):
            if sink is None:
                sinks[name] = None
                continue
            # 普通整数字段的读取在 GIL 下是安全的；qsize 线程安全。
            sinks[name] = {
                'written': sink.written,
                'dropped': sink.dropped,
                'write_errors': sink.write_errors,
                'bytes_written': getattr(sink, 'bytes_written', 0),
                'queued': sink._queue.qsize(),
                'alive': sink.is_alive(),
            }
        channels = [c.stats() for c in list(self._channels)]
        budget = self.budget.snapshot_fast() if self.budget is not None else None
        return {
            'enabled': self.enabled,
            'media_progress': self.media_progress,
            'host_sampling': self.host_sampling,
            'directory': str(self.directory) if self.directory else '',
            'instance_id': self.instance_id,
            'snapshot': 'fast',          # 与完整 stats() 区分：无磁盘扫描
            'events_buffered': len(self.events),
            'events_buffered_bytes': self._events_bytes,
            'truncated_records': self.truncated_records,
            'max_file_bytes': self.max_bytes,
            'retain_days': self.retain_days,
            'progress_channels': len(channels),
            'retired_channels': len(self._retired_channels),
            'drain_only_channels': self._drain_only,
            'unreaped_threads': self._unreaped_snapshot_unlocked(),
            'bypass_writer': self._bypass_stats_unlocked(),
            'budget': budget,
            'host_sampler': self._host_sampler_state_fast(),
            'sinks': sinks,
            'channels': channels,
        }

    def _host_sampler_state_fast(self):
        supported = HostSampler.SUPPORTED
        sampler = self.host_sampler
        running = bool(sampler is not None and sampler.is_alive())
        return {'supported': supported, 'running': running,
                'reason': '' if (not supported or running) else '未运行'}

    def stats(self) -> dict:
        with self._lock:
            payload = {
                'enabled': self.enabled,
                'media_progress': self.media_progress,
                'host_sampling': self.host_sampling,
                'directory': str(self.directory) if self.directory else '',
                'instance_id': self.instance_id,
                'events_buffered': len(self.events),
                'events_buffered_bytes': self._events_bytes,
                'truncated_records': self.truncated_records,
                'max_file_bytes': self.max_bytes,
                'retain_days': self.retain_days,
                'progress_channels': len(self._channels),
                'drain_only_channels': self._drain_only,
                'unreaped_threads': self._unreaped_snapshot_unlocked(),
                'budget': self.budget.stats() if self.budget else None,
                'host_sampler': self._host_sampler_state_unlocked(),
                'sinks': {
                    'events': self.event_sink.stats() if self.event_sink else None,
                    'media': self.progress_sink.stats() if self.progress_sink else None,
                    'host': self.host_sink.stats() if self.host_sink else None,
                },
                'channels': [c.stats() for c in self._channels],
            }
            return payload

    def _host_sampler_state_unlocked(self):
        if not HostSampler.SUPPORTED:
            return {'supported': False,
                    'reason': 'Windows 不支持 /proc 与 ss，主机采样不可用',
                    'running': False,
                    'lock_path': str(HostSampler.shared_dir() / HostSampler.LOCK_NAME),
                    'registry': HostSampler.read_registry()}
        if self.host_sampler is None:
            return {'supported': True, 'running': False,
                    'reason': self.host_sampling and '未启动' or '未启用',
                    'lock_path': str(HostSampler.shared_dir() / HostSampler.LOCK_NAME),
                    'registry': HostSampler.read_registry()}
        state = self.host_sampler.stats()
        state['registry'] = HostSampler.read_registry()
        return state

    def export_bundle(self, limit: int = 200) -> dict:
        """导出脱敏诊断摘要（不含凭据、不含完整推流 URL）。

        导出字节也计入目录预算；超限时只返回摘要并注明未落盘，不阻塞调用方。
        """
        with self._lock:
            events = list(self.events)[-limit:]
        # record() 已在入口脱敏，这里再兜一次，防止历史缓冲来自旧版本。
        sanitized = [sanitize_record(event) for event in events]
        path = ''
        written = 0
        if self.directory is not None:
            target = self.directory / 'diagnostic-bundle.jsonl'
            payload = ''.join(
                json.dumps(item, ensure_ascii=False, default=str) + '\n'
                for item in sanitized)
            nbytes = len(payload.encode('utf-8'))
            if self.budget is None or self.budget.reserve(nbytes, protect=target):
                try:
                    previous = target.stat().st_size if target.exists() else 0
                except OSError:
                    previous = 0
                try:
                    with target.open('w', encoding='utf-8') as handle:
                        handle.write(payload)
                    path = str(target)
                    written = nbytes
                    if self.budget is not None:
                        # 导出同样是并发写者：预留 → 提交，并计入目录总量。
                        self.budget.commit_replace(previous, nbytes)
                except OSError:
                    path = ''
                    if self.budget is not None:
                        self.budget.rollback(nbytes)
        return {'exported': len(sanitized), 'path': path,
                'bytes_written': written,
                'budget_limited': bool(path == '' and sanitized),
                'events': sanitized}

    def shutdown(self, timeout: float = 5.0):
        """关闭诊断：锁内只发停止信号，join 全部在锁外完成。

        这保证等待慢退出的线程期间，持有诊断锁的路径（record、
        progress_args、identity_probe_enabled 等）不被拖住（第 5 轮 U2）。
        """
        with self._lock:
            self.enabled = False
            # 旁路 worker：代际 +1 使队列里的旧任务作废，再停止并回收。
            self._bypass_generation += 1
            reapers = self._collect_worker_reapers_unlocked()
            reapers.extend(self._stop_sinks_unlocked())
            channels = list(self._channels)
            for channel in channels:
                channel.stop()
            self._channels = []
            self._retired_channels = []
        # ---- 锁外：逐个 join（supervision 用例在 join 边界探测诊断锁可获取）----
        self._reap_threads(reapers, timeout=timeout)
        # 统一通道收尾：超时仍在排空的通道保留所有权（登记未回收），
        # 已退出的进入有界历史；随后对全部未回收登记做一次确认。
        self._reap_channels(channels, timeout=timeout)
        self._confirm_unreaped(timeout=timeout)


_diagnostics = Diagnostics()
_diagnostics_lock = threading.Lock()


def get_diagnostics() -> Diagnostics:
    return _diagnostics


def reset_diagnostics(instance_id: str = None) -> Diagnostics:
    """测试用：替换为全新实例。"""
    global _diagnostics
    with _diagnostics_lock:
        _diagnostics.shutdown()
        _diagnostics = Diagnostics(instance_id=instance_id)
        return _diagnostics
