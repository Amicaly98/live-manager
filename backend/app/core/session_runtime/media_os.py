"""媒体端口的 OS 适配层：进程身份、精确结束与实例生命周期组。

这一层把平台差异收拢成**少量真实 OS 原语**，让上面的媒体端口只用事实编程：

- **出生身份**不是 PID 单值：``(pid, creation_time)`` 由 ``GetProcessTimes`` 读取，
  用来识别 PID 复用（B03）。看不出的情况返回 ``None``，绝不猜。
- **实例生命周期组**用 Windows Job Object（``KILL_ON_JOB_CLOSE``）：监督进程消亡时
  OS 立刻收回组内全部子进程。这不是"额外守护服务"，是原生能力；job 的进程 ID 列表
  就是"本实例自有对象"的 OS 枚举，不按进程名全局扫描。
- **创建即在组内**：``spawn(..., job=...)`` 用 ``CreateProcessW`` ＋
  ``PROC_THREAD_ATTRIBUTE_JOB_LIST``：子进程**出生就是生命周期组成员**，不存在
  "先返回 Popen、再很快 assign"的窗口。监督进程在这条路径上的任何时刻被硬杀都不会
  留下组外活进程。（``assign_to_job`` 保留，供"已存在进程"的显式归属。）
- **精确结束**用 argv 形式的 ``taskkill /T``（温和）与 ``taskkill /F /T``（强制），
  ``shell=False``，只作用于给定 PID 的树；超时来自**本轮的绝对截止**（见
  :func:`deadline_scope`），不是固定 15 秒。

POSIX 分支提供等价原语（``setsid`` 进程组 + ``killpg``），但 Linux 内核回收的实测
证据属于后续隔离补证，本包不声称已在 Linux 上实跑。
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

#: 结束动作在没有给定截止时的上限（保持原有默认行为不变）。
DEFAULT_TERMINATE_TIMEOUT = 15.0


class SpawnUnavailable(RuntimeError):
    """要求"出生即在生命周期组内"的原生创建**没能取得对象**：本轮不创建。

    这不是"该 OS 没有 Job 能力"（那是另一条合法事实），也不是退回无归属
    ``Popen`` 的理由 —— 调用方据此**结束本轮**，绝不让"原子创建失败"悄悄变成
    "组外活对象"。
    """


class JobQueryError(RuntimeError):
    """原生生命周期组枚举失败：归属**不可核验**。

    既不是"确证空组"，也不是"该 OS 本来不支持 Job"。上层必须把这三者分开，不能
    把查询失败折成 ``()`` 后用更弱的授权继续。
    """


class LivenessUnknown(RuntimeError):
    """存活读取**没读到**：既不是"确证在跑"，也不是"确证已退出"。

    把它折成 ``False`` 是最危险的错法 —— 上层会据此清记录、放引用、放行第二路。
    原生层只报它确证的事实，读不出**抛本异常**，由调用方的"未知"通道处理。
    """


class SpawnUnreclaimed(RuntimeError):
    """原生创建**已经成功**，但结束它并确认退出的过程没有取得确证。

    与 :class:`SpawnUnavailable` 是两个不同的失败出口：``SpawnUnavailable`` 表示
    "本轮确实没有对象"，本异常表示"有个真实对象，但我们没能确证它已经不在"。
    调用方必须保留现场并阻断第二路，**不能**清掉创建记录当它没发生过。

    ``retained`` 是本次创建取得的**真实对象**（:class:`RetainedChild`：进程句柄、
    未关闭的线程句柄、读端）。"不能确认回收"**不等于**"没有对象"：异常文字里的 PID
    不是可继续处理的资源引用，持有者才是。调用方必须把它交给**现有的**资源权威
    （端口的资源槽与归属记录），让同一份事实继续支撑回收、关闭与报告。
    """

    def __init__(self, detail: str, *, retained: Optional[object] = None) -> None:
        super().__init__(detail)
        self.retained = retained


_DEADLINES = threading.local()


@contextlib.contextmanager
def deadline_scope(deadline: Optional[float]) -> Iterator[None]:
    """把**本轮媒体动作的绝对截止**交给 OS 层（线程内，可嵌套）。

    这不是第二份权威：截止仍由调用方那唯一一份给出，这里只把它传进真正花时间执行的
    OS 原语（``terminate_tree``），让结束动作只消费**剩余**时间。没有作用域时保留原
    默认上限。
    """
    previous = getattr(_DEADLINES, 'value', None)
    _DEADLINES.value = None if deadline is None else float(deadline)
    try:
        yield
    finally:
        _DEADLINES.value = previous


def current_deadline() -> Optional[float]:
    """当前线程内的绝对截止（没有则是 ``None``）。"""
    return getattr(_DEADLINES, 'value', None)


def _budget_for(timeout: Optional[float]) -> float:
    """把"默认上限"和"当前截止的剩余"合成一个真正可用的墙钟预算。"""
    deadline = current_deadline()
    if deadline is None:
        return float(timeout if timeout is not None else DEFAULT_TERMINATE_TIMEOUT)
    remaining = deadline - time.monotonic()
    if timeout is not None:
        remaining = min(remaining, float(timeout))
    return remaining

#: 子进程创建标志（Windows）。
#: ``CREATE_NO_WINDOW`` 不给窗口；``DETACHED_PROCESS`` **不分配控制台** ——
#: 后者很关键：带控制台的子进程会顺带把 ``conhost.exe`` 拉进本实例的生命周期组，
#: 让"组内进程"不再等于"本实例的自有媒体对象"。精确结束用 ``taskkill /T``，
#: 不依赖控制台事件，因此不需要 ``CREATE_NEW_PROCESS_GROUP``。
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


@dataclass(frozen=True)
class ProcessIdentity:
    """一个 OS 进程的**出生身份**：PID 加上创建时间（100ns 单位）。"""

    pid: int
    creation_time: int
    exe: str = ''

    def matches(self, other: Optional['ProcessIdentity']) -> bool:
        if other is None:
            return False
        if self.pid != other.pid:
            return False
        if self.creation_time and other.creation_time:
            return self.creation_time == other.creation_time
        # 创建时间读不到时只能按 PID 判等：这**不是**充分的归属证明，
        # 端口在需要"确证"的路径上会把它当成不可判定。
        return True


def identity_is_conclusive(identity: Optional[ProcessIdentity]) -> bool:
    """创建时间是否真的读到了：只有它才能排除 PID 复用。"""
    return bool(identity is not None and identity.creation_time)


class OsOps:
    """OS 原语基类（POSIX 实现）。Windows 行为见 :class:`WindowsOsOps`。"""

    name = 'posix'
    job_capable = False

    # ---------------------------------------------------------------- 预算传递

    def termination_deadline(self, deadline: Optional[float]):
        """把本轮**绝对截止**交给 OS 层（上下文管理器）。

        被测试注入层包裹时也会经由属性转发落到真实实现上，因此"结束动作只消费剩余
        时间"这条规则对**被包裹的** OS 原语同样生效，注入层不必改签名。
        """
        return deadline_scope(deadline)

    # ---------------------------------------------------------------- 生命周期组

    def create_job(self) -> Optional[object]:
        return None

    def close_job(self, job) -> None:                 # pragma: no cover - 无 job
        return None

    def assign_to_job(self, job, proc) -> bool:       # pragma: no cover - 无 job
        return False

    def job_pids(self, job) -> Tuple[int, ...]:       # pragma: no cover - 无 job
        return ()

    # ---------------------------------------------------------------- 进程

    def spawn(self, argv: Sequence[str], *, cwd=None,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.PIPE, job=None) -> subprocess.Popen:
        return subprocess.Popen(
            list(argv), shell=False, cwd=cwd,
            stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True)

    def identity(self, proc) -> Optional[ProcessIdentity]:
        if proc is None:
            return None
        try:
            pid = int(proc.pid)
        except Exception:                             # noqa: BLE001
            return None
        return self.pid_identity(pid)

    def pid_identity(self, pid: int) -> Optional[ProcessIdentity]:
        creation = 0
        try:
            with open(f'/proc/{int(pid)}/stat', 'r',
                      encoding='utf-8', errors='replace') as handle:
                fields = handle.read().rsplit(')', 1)[-1].split()
            creation = int(fields[19])                # starttime（jiffies）
        except Exception:                             # noqa: BLE001
            creation = 0
        if not creation:
            return None
        return ProcessIdentity(pid=int(pid), creation_time=creation)

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:                             # noqa: BLE001
            return False
        return not self._pid_is_zombie(int(pid))

    @staticmethod
    def _pid_is_zombie(pid: int) -> bool:
        """已退出、尚未被父进程 reap 的僵尸**不是存活对象**。

        Windows 没有这个中间态（进程退出即退出）；POSIX 上僵尸仍占 PID、``kill``
        成功，但它的退出已经是**事实**—— ``kill`` 成功 + ``/proc`` 状态为 ``Z``/``X``
        时必须报"不存活"，否则"确证已退出（含尚未 reaped 的僵尸）"这条既有口径
        在 Linux 上永远得不到否定事实。状态**读不出**只说明没读到，按存活保守处理。
        """
        try:
            with open(f'/proc/{pid}/stat', 'r', encoding='utf-8',
                      errors='replace') as handle:
                fields = handle.read().rsplit(')', 1)[-1].split()
        except OSError:
            return False
        return bool(fields) and fields[0] in ('Z', 'x', 'X')

    def is_alive(self, proc) -> bool:
        try:
            return proc is not None and proc.poll() is None
        except Exception:                             # noqa: BLE001
            return False

    def exit_code(self, proc) -> Optional[int]:
        try:
            return proc.poll()
        except Exception:                             # noqa: BLE001
            return None

    def wait(self, proc, timeout: float) -> bool:
        if proc is None:
            return True
        try:
            proc.wait(timeout=max(0.0, float(timeout)))
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:                             # noqa: BLE001
            return not self.is_alive(proc)

    def wait_pid(self, pid: int, timeout: float, step: float = 0.02) -> bool:
        import time
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if not self.pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(step, 0.05))

    def terminate_tree(self, pid: int, *, force: bool) -> bool:
        """请求结束整个进程树。返回"命令是否被 OS 接受"，**不代表**已退出。

        时间预算＝``termination_deadline`` 作用域内的剩余（若已耗尽则**不发新动作**）；
        没有作用域时退化为 :data:`DEFAULT_TERMINATE_TIMEOUT`。
        """
        timeout = _budget_for(DEFAULT_TERMINATE_TIMEOUT)
        if timeout <= 0:
            return False
        try:
            if force:
                os.killpg(int(pid), 9)
            else:
                os.killpg(int(pid), 15)
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:                             # noqa: BLE001
            return False
        return True

    # ---------------------------------------------------------------- 管道读取

    def read_available(self, proc, max_bytes: int) -> bytes:
        """非阻塞读取当前可用的 stderr 字节；无数据返回 ``b''``。"""
        import select
        stream = getattr(proc, 'stderr', None)
        if stream is None:
            return b''
        try:
            fd = stream.fileno()
        except Exception:                             # noqa: BLE001
            return b''
        try:
            ready, _, _ = select.select([fd], [], [], 0.0)
        except Exception:                             # noqa: BLE001
            return b''
        if not ready:
            return b''
        try:
            return os.read(fd, max(1, int(max_bytes)))
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b''
            return b''
        except Exception:                             # noqa: BLE001
            return b''


class _WinFileTime(ctypes.Structure):
    _fields_ = [('dwLowDateTime', wintypes.DWORD),
                ('dwHighDateTime', wintypes.DWORD)]


class _WinIoCounters(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                ('WriteOperationCount', ctypes.c_ulonglong),
                ('OtherOperationCount', ctypes.c_ulonglong),
                ('ReadTransferCount', ctypes.c_ulonglong),
                ('WriteTransferCount', ctypes.c_ulonglong),
                ('OtherTransferCount', ctypes.c_ulonglong)]


class _WinBasicLimit(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _WinExtLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _WinBasicLimit),
                ('IoInfo', _WinIoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _WinStartupInfo(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
        ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR),
        ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
        ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
        ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
        ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
        ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
        ('lpReserved2', ctypes.c_void_p),
        ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE),
        ('hStdError', wintypes.HANDLE)]


class _WinStartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _WinStartupInfo),
                ('lpAttributeList', ctypes.c_void_p)]


class _WinProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                ('dwProcessId', wintypes.DWORD),
                ('dwThreadId', wintypes.DWORD)]


class _PipeReader:
    """把 C 运行时 fd 包成 ``proc.stderr`` 需要的那一小块接口。

    ``read_available`` 要 ``fileno()`` 才能 ``PeekNamedPipe``；``_close_streams``
    要 ``close()``。**不**提供阻塞式无界 ``read()``：这是非阻塞管道。
    """

    def __init__(self, fd: int) -> None:
        self._fd = int(fd)

    def fileno(self) -> int:
        return self._fd

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def read(self, size: int = -1) -> bytes:
        if size is None or int(size) < 0:
            raise OSError('非阻塞管道不支持无界 read')
        if self._fd < 0:
            return b''
        return os.read(self._fd, int(size))

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class WinChild:
    """``CreateProcessW`` 创建、**出生即在实例生命周期组内**的子进程句柄。

    只提供媒体端口与测试实际用到的那一小块 ``Popen`` 表面：
    ``pid`` / ``poll()`` / ``wait()`` / ``kill()`` / ``stderr`` / ``returncode``。
    进程句柄在 :meth:`dispose` 之前一直持有，因此**PID 复用不会影响存活判定**。
    """

    def __init__(self, process: _WinProcessInformation, read_fd: int,
                 command: Sequence[str]) -> None:
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._handle = process.hProcess
        self.pid = int(process.dwProcessId)
        self._command = list(command)
        self.stderr = _PipeReader(read_fd)
        self.returncode: Optional[int] = None

    # ---------- Popen 表面 ----------

    def poll(self) -> Optional[int]:
        if self._handle is None:
            return self.returncode
        if self.returncode is not None:
            return self.returncode
        if self._kernel32.WaitForSingleObject(self._handle, 0) != 0:
            return None
        code = wintypes.DWORD(0)
        if self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self._handle is None:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None \
            else max(0, int(float(timeout) * 1000))
        if self._kernel32.WaitForSingleObject(self._handle, milliseconds) \
                == 0x00000102:                        # WAIT_TIMEOUT
            raise subprocess.TimeoutExpired(self._command, timeout)
        return self.poll()

    def kill(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateProcess(self._handle, 1)

    def dispose(self) -> None:
        """关闭进程句柄（进程已确证退出后调用，避免句柄泄漏）。"""
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


class RetainedChild(WinChild):
    """``CreateProcessW`` **已经成功**、但没能完成移交的子进程持有者。

    与 :class:`WinChild` 的差别只在"谁负责生命周期"：它不携带观察器与日志，只是把本次
    创建取得的原生事实（进程句柄、进程号、读端、尚未关闭的线程句柄）原样带出去，交给
    **现有的资源权威**（端口的资源槽与归属记录），从而进入同一份回收/关闭/报告。

    它**不是**第二份资源台账：构造它不产生任何新的业务状态，只回答"原生创建成功之后
    的失败出口里，未转交的资源由谁负责"。绑定好的 ``kernel32`` 由调用方传入 —— 失败
    出口不再重复取库，也就不可能因为取库本身失败而丢掉真实对象。
    """

    def __init__(self, kernel, process, read_fd, command,
                 thread_closed: bool = False) -> None:
        self._kernel32 = kernel
        self._handle = process.hProcess
        #: 线程句柄在正常路径上已经关掉；没关掉的那一个必须由本对象负责收尾。
        self._thread = None if thread_closed else process.hThread
        self.pid = int(process.dwProcessId)
        self._command = list(command)
        fd = int(read_fd) if read_fd is not None else -1
        self.stderr = _PipeReader(fd) if fd >= 0 else None
        self.returncode: Optional[int] = None

    def dispose(self) -> None:
        """关闭本对象负责的进程/线程句柄（幂等；进程已确证退出后调用）。"""
        kernel = self._kernel32
        handle, self._handle = self._handle, None
        if handle:
            try:
                kernel.CloseHandle(handle)
            except Exception:                         # noqa: BLE001
                pass
        thread, self._thread = self._thread, None
        if thread:
            try:
                kernel.CloseHandle(thread)
            except Exception:                         # noqa: BLE001
                pass


class WindowsOsOps(OsOps):
    """Windows：Job Object ＋ ``taskkill`` ＋ ``GetProcessTimes``。"""

    name = 'windows'
    job_capable = True

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_OBJECT_0 = 0x00000000
    _INFINITE = 0xFFFFFFFF
    #: ``OpenProcess`` 对**不存在**的 PID 报的错（其余错误一律算"读不出"）。
    _ERROR_INVALID_PARAMETER = 87

    #: ``CreateProcessW`` 的扩展启动属性：子进程**出生即在 Job 内**。
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _STARTF_USESTDHANDLES = 0x00000100
    _HANDLE_FLAG_INHERIT = 0x00000001
    _PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3

    _FILETIME = _WinFileTime
    _IO_COUNTERS = _WinIoCounters
    _BASIC_LIMIT = _WinBasicLimit
    _EXT_LIMIT = _WinExtLimit

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self._kernel32
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(self._FILETIME),
            ctypes.POINTER(self._FILETIME), ctypes.POINTER(self._FILETIME),
            ctypes.POINTER(self._FILETIME)]
        k.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.PeekNamedPipe.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD)]
        # 出生即在生命周期组内所需的扩展启动属性原语。
        k.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.POINTER(_WinProcessInformation)]
        k.CreateProcessW.restype = wintypes.BOOL
        k.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        k.CreateFileW.restype = wintypes.HANDLE
        k.SetHandleInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        k.SetHandleInformation.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        # Python 的 msvcrt.get_osfhandle 返回正确的 intptr 宽度（不用 ctypes 截断版）。
        import msvcrt
        self._get_osfhandle = msvcrt.get_osfhandle

    # ---------------------------------------------------------------- Job

    def create_job(self) -> Optional[object]:
        try:
            handle = self._kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = self._EXT_LIMIT()
            info.BasicLimitInformation.LimitFlags = \
                self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = self._kernel32.SetInformationJobObject(
                handle, self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                self.close_job(handle)
                return None
            return handle
        except Exception:                             # noqa: BLE001
            return None

    def close_job(self, job) -> None:
        if not job:
            return
        try:
            self._kernel32.CloseHandle(job)
        except Exception:                             # noqa: BLE001
            pass

    def assign_to_job(self, job, proc) -> bool:
        if not job or proc is None:
            return False
        handle = self._kernel32.OpenProcess(
            self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA
            | self._PROCESS_QUERY_LIMITED_INFORMATION, False, int(proc.pid))
        if not handle:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(job, handle))
        except Exception:                             # noqa: BLE001
            return False
        finally:
            self._kernel32.CloseHandle(handle)

    def job_pids(self, job) -> Tuple[int, ...]:
        """本实例生命周期组的**原生**进程枚举。

        三种情况必须分开，不能互相折换：

        - ``job`` 为空：本 OS／本实例**没有**生命周期组 —— 合法的"无组"事实 → ``()``；
        - 有 job、查询成功、组里没有进程：**确证空组** → ``()``；
        - 有 job、但原生查询失败或抛错：**不可核验** → :class:`JobQueryError`。

        把第三种折成第二种会让上层把"读不出"当成"确证空组"，进而清记录、放引用、
        放行第二路 —— 这正是要禁止的。
        """
        if not job:
            return ()
        size = 4096
        buffer = (ctypes.c_ubyte * size)()
        returned = wintypes.DWORD(0)
        try:
            ok = self._kernel32.QueryInformationJobObject(
                job, self._JOB_OBJECT_BASIC_PROCESS_ID_LIST, buffer, size,
                ctypes.byref(returned))
        except Exception as exc:                      # noqa: BLE001
            raise JobQueryError(f'native job query raised: {exc!r}') from exc
        if not ok:
            raise JobQueryError('native job query returned failure')
        head = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
        count = int(head[1])
        if count <= 0:
            return ()
        array = ctypes.cast(ctypes.byref(buffer, 8),
                            ctypes.POINTER(ctypes.c_size_t))
        pids = []
        for index in range(min(count, 512)):
            value = int(array[index])
            if value > 0:
                pids.append(value)
        return tuple(pids)

    # ---------------------------------------------------------------- 进程

    def spawn(self, argv, *, cwd=None, stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, job=None):
        """创建子进程。给了 ``job`` 就必须**出生即在生命周期组内**。

        请求了 Job 的创建一旦失败，本轮就**没有**可持有对象：抛
        :class:`SpawnUnavailable`，**不**退化成"先建后加组"的普通 ``Popen`` ——
        那会把"原子创建失败"变成"组外活对象"（没有归属、也收不回来）。
        不给 ``job`` 时才走普通 ``Popen``，只用于"已存在进程的显式归属"等显式场景。
        """
        if job:
            child = self.spawn_in_job(argv, job, cwd=cwd)
            if child is None:
                raise SpawnUnavailable(
                    '原子创建未取得生命周期组内的子进程：本轮不创建组外对象')
            return child
        return subprocess.Popen(
            list(argv), shell=False, cwd=cwd,
            stdin=stdin, stdout=stdout, stderr=stderr,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)

    def spawn_in_job(self, argv, job, *, cwd=None) -> Optional[WinChild]:
        """用 ``CreateProcessW`` ＋ ``PROC_THREAD_ATTRIBUTE_JOB_LIST`` 原子创建。

        子进程在 **CreateProcessW 返回的那一刻就已经是本实例生命周期组的成员** ——
        不存在"Popen 已返回、还没 assign"的窗口。失败返回 ``None``（调用方据此报
        "本轮不持有对象"），**不**退化成"先建后加组"。

        标准句柄固定为：stdin/stdout＝``NUL``，stderr＝非阻塞管道（``WinChild.stderr``）。
        媒体端口的调用形状正是这一种，因此这里不接受调用方自定义句柄。

        **资源出口**：本方法**取得**的资源要么随 :class:`WinChild` 转交，要么在原生创建
        成功后确证收回并释放；确证不了就把真实对象随 :class:`RetainedChild` 交给上层
        （见 :class:`SpawnUnreclaimed`）。其余（写端、``NUL``、属性列表、创建失败时的
        读端）一律在 ``finally`` 释放。反复失败不积累。
        """
        if not job:
            return None
        kernel = self._kernel32
        read_fd = -1
        write_fd = -1
        null_handle = None
        buffer = None
        transferred = False
        created = False
        thread_closed = False
        process = _WinProcessInformation()
        try:
            read_fd, write_fd = os.pipe()
            null_handle = kernel.CreateFileW(
                'NUL', self._GENERIC_READ | self._GENERIC_WRITE,
                self._FILE_SHARE_READ | self._FILE_SHARE_WRITE, None,
                self._OPEN_EXISTING, 0, None)
            if not null_handle:
                return None
            write_handle = int(self._get_osfhandle(write_fd))
            kernel.SetHandleInformation(null_handle, self._HANDLE_FLAG_INHERIT,
                                        self._HANDLE_FLAG_INHERIT)
            kernel.SetHandleInformation(wintypes.HANDLE(write_handle),
                                        self._HANDLE_FLAG_INHERIT,
                                        self._HANDLE_FLAG_INHERIT)
            size = ctypes.c_size_t(0)
            kernel.InitializeProcThreadAttributeList(None, 2, 0,
                                                     ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(
                    buffer, 2, 0, ctypes.byref(size)):
                return None
            job_array = (wintypes.HANDLE * 1)(job)
            handles = (wintypes.HANDLE * 2)(null_handle, wintypes.HANDLE(write_handle))
            if not kernel.UpdateProcThreadAttribute(
                    buffer, 0, self._PROC_THREAD_ATTRIBUTE_JOB_LIST, job_array,
                    ctypes.sizeof(job_array), None, None):
                return None
            if not kernel.UpdateProcThreadAttribute(
                    buffer, 0, self._PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                    ctypes.sizeof(handles), None, None):
                return None

            info = _WinStartupInfoEx()
            ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
            info.StartupInfo.cb = ctypes.sizeof(_WinStartupInfoEx)
            info.StartupInfo.dwFlags = self._STARTF_USESTDHANDLES
            info.StartupInfo.hStdInput = null_handle
            info.StartupInfo.hStdOutput = null_handle
            info.StartupInfo.hStdError = wintypes.HANDLE(write_handle)
            info.lpAttributeList = ctypes.cast(buffer, ctypes.c_void_p)

            command = subprocess.list2cmdline([str(part) for part in argv])
            ok = kernel.CreateProcessW(
                None, ctypes.create_unicode_buffer(command), None, None, True,
                self._EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
                | DETACHED_PROCESS,
                None, str(cwd) if cwd else None, ctypes.byref(info),
                ctypes.byref(process))
            if not ok:
                return None
            # **原生创建已成功**：从这里起，hProcess/hThread 与读端都有真实负责人，
            # 交付出去之前它们只归本方法。先做完原生侧簿记，再包装 WinChild —— 包装
            # 是唯一可能失败的"后处理"，把它放最后，失败时接管的范围最小。
            created = True
            try:
                kernel.CloseHandle(process.hThread)
            except Exception:                         # noqa: BLE001
                pass
            thread_closed = True
            os.close(write_fd)
            write_fd = -1
            child = WinChild(process, read_fd, argv)
            transferred = True                        # 读端自此归 WinChild 所有
            return child
        except SpawnUnreclaimed:
            raise
        except Exception as exc:                      # noqa: BLE001
            if not created:
                return None
            # 原生创建**已经成功**：这个对象真实存在、且出生就在本实例生命周期组内。
            # 绝不能声称"本轮没有对象"而把它留成组外/无主的活进程 —— 先在自己的
            # 取得/转交边界内结束它并**确认退出**；只有确证收回，``None`` 才是实话。
            if self._await_created_exit(process):
                self._close_created_handles(process, thread_closed)
                return None
            # 确证不了：这个对象**真实存在**（Job 会在监督进程消亡时兜底收回）。把本次
            # 创建取得的原生事实交给上层 —— 异常文字里的 PID 和一条 pid=0 记录都不是
            # 可继续处理的资源引用，只有这个持有者才能让它进入同一份回收/关闭/报告。
            retained = RetainedChild(self._kernel32, process, read_fd, argv,
                                     thread_closed)
            read_fd = -1                              # 读端自此归 retained 所有
            raise SpawnUnreclaimed(
                f'原生创建成功后无法确认回收（{type(exc).__name__}: {exc}；'
                f'PID={int(process.dwProcessId)}）', retained=retained) from exc
        finally:
            if buffer is not None:
                kernel.DeleteProcThreadAttributeList(buffer)
            if null_handle:
                kernel.CloseHandle(null_handle)
            if not transferred and read_fd >= 0:
                # 失败出口：读端**没有**转交给任何人，必须自己关掉，否则每失败
                # 一轮就漏一个管道描述符。
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

    def _await_created_exit(self, process) -> bool:
        """结束**刚创建、尚未转交**的对象，并确认它已经退出。

        等待预算＝``termination_deadline`` 作用域内的剩余（与结束动作**同一份**媒体
        绝对截止，不新建超时常量）；没有作用域时退化为
        :data:`DEFAULT_TERMINATE_TIMEOUT`。返回 ``False`` 表示**没取得确证**。
        """
        kernel = self._kernel32
        handle = process.hProcess
        if not handle:
            return True
        try:
            kernel.TerminateProcess(handle, 1)
        except Exception:                             # noqa: BLE001
            pass
        budget = _budget_for(DEFAULT_TERMINATE_TIMEOUT)
        milliseconds = 0 if budget <= 0 else max(1, int(budget * 1000))
        try:
            result = kernel.WaitForSingleObject(handle, milliseconds)
        except Exception:                             # noqa: BLE001
            return False
        return result == self._WAIT_OBJECT_0

    def _close_created_handles(self, process, thread_closed: bool) -> None:
        """关掉**本次创建**取得的线程/进程句柄（进程已确证退出后调用）。"""
        kernel = self._kernel32
        try:
            if not thread_closed and process.hThread:
                kernel.CloseHandle(process.hThread)
        except Exception:                             # noqa: BLE001
            pass
        try:
            if process.hProcess:
                kernel.CloseHandle(process.hProcess)
        except Exception:                             # noqa: BLE001
            pass
        process.hProcess = None

    def identity(self, proc) -> Optional[ProcessIdentity]:
        if proc is None:
            return None
        try:
            pid = int(proc.pid)
        except Exception:                             # noqa: BLE001
            return None
        return self.pid_identity(pid)

    def pid_identity(self, pid: int) -> Optional[ProcessIdentity]:
        pid = int(pid)
        handle = self._kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created = self._FILETIME()
            exited = self._FILETIME()
            kernel = self._FILETIME()
            user = self._FILETIME()
            ok = self._kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            creation = (int(created.dwHighDateTime) << 32) \
                | int(created.dwLowDateTime)
        except Exception:                             # noqa: BLE001
            return None
        finally:
            self._kernel32.CloseHandle(handle)
        return ProcessIdentity(pid=pid, creation_time=creation)

    def pid_alive(self, pid: int) -> bool:
        """存活事实：只为**确证**的情形下结论，读不出抛 :class:`LivenessUnknown`。

        三种情形必须分开：

        - ``OpenProcess`` 报 ``ERROR_INVALID_PARAMETER``：该 PID **确证不存在** → ``False``；
        - 等待返回 ``WAIT_OBJECT_0``：**确证已退出** → ``False``；
        - 等待返回 ``WAIT_TIMEOUT``：**确证仍在跑** → ``True``；
        - 其它（权限不足、``WAIT_FAILED``、原生异常）：**读不出** → 抛异常，走"未知"通道。

        把权限失败或 ``WAIT_FAILED`` 折成 ``False`` 会让上层把"读不出"当成"已退出"，
        进而清记录、放引用、放行第二路 —— 这正是要禁止的。
        """
        pid = int(pid)
        kernel = self._kernel32
        ctypes.set_last_error(0)
        handle = kernel.OpenProcess(self._SYNCHRONIZE, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == self._ERROR_INVALID_PARAMETER:
                return False                          # 该 PID 不存在：确证
            raise LivenessUnknown(f'打开进程 {pid} 失败：原生错误 {error}')
        try:
            result = kernel.WaitForSingleObject(handle, 0)
        except Exception as exc:                      # noqa: BLE001
            raise LivenessUnknown(f'等待进程 {pid} 异常：{exc!r}') from exc
        finally:
            kernel.CloseHandle(handle)
        if result == self._WAIT_OBJECT_0:
            return False                              # 已退出：确证
        if result == self._WAIT_TIMEOUT:
            return True                               # 仍在运行：确证
        raise LivenessUnknown(f'等待进程 {pid} 返回 {int(result):#x}')

    def wait_pid(self, pid: int, timeout: float, step: float = 0.02) -> bool:
        import time
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                gone = not self.pid_alive(pid)
            except LivenessUnknown:
                gone = False                          # 读不出 ≠ 已退出：继续等到截止
            if gone:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(step, 0.05))

    def terminate_tree(self, pid: int, *, force: bool) -> bool:
        """请求结束整个进程树。返回"命令是否被 OS 接受"，**不代表**已退出。

        时间预算＝``termination_deadline`` 作用域内的剩余（耗尽则**不发新动作**）；
        没有作用域时保持原来的 15 秒默认上限。签名与默认行为保持不变。
        """
        timeout = _budget_for(DEFAULT_TERMINATE_TIMEOUT)
        if timeout <= 0:
            return False
        argv = ['taskkill', '/F', '/T', '/PID', str(int(pid))] if force \
            else ['taskkill', '/T', '/PID', str(int(pid))]
        try:
            result = subprocess.run(
                argv, shell=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        except Exception:                             # noqa: BLE001
            return False
        return result.returncode == 0

    # ---------------------------------------------------------------- 管道

    def read_available(self, proc, max_bytes: int) -> bytes:
        stream = getattr(proc, 'stderr', None)
        if stream is None:
            return b''
        try:
            fd = stream.fileno()
            handle = int(self._get_osfhandle(fd))
        except Exception:                             # noqa: BLE001
            return b''
        available = wintypes.DWORD(0)
        try:
            ok = self._kernel32.PeekNamedPipe(
                wintypes.HANDLE(handle), None, 0, None,
                ctypes.byref(available), None)
        except Exception:                             # noqa: BLE001
            return b''
        if not ok or available.value <= 0:
            return b''
        try:
            return os.read(fd, min(int(available.value), max(1, int(max_bytes))))
        except OSError:
            return b''
        except Exception:                             # noqa: BLE001
            return b''


def default_os_ops() -> OsOps:
    if sys.platform == 'win32':
        return WindowsOsOps()
    return OsOps()


# ---------------------------------------------------------------- 进程统计（B09）

@dataclass(frozen=True)
class ProcessStats:
    """当前进程的实时资源事实（不是历史峰值）。"""

    rss_bytes: int
    handle_count: int
    thread_count: int


def current_process_stats() -> ProcessStats:
    """读取**当前进程**的 WorkingSet / 句柄数 / 线程数。

    Windows 用 ``GetProcessMemoryInfo`` 与 ``GetProcessHandleCount``；其它平台退化到
    ``/proc``（RSS）与 ``threading``（线程）。**不**使用 ``ru_maxrss``：那是历史峰值。
    """
    import threading
    threads = threading.active_count()
    if sys.platform == 'win32':
        return _windows_process_stats(threads)
    rss = 0
    try:
        with open('/proc/self/statm', 'r', encoding='utf-8') as handle:
            pages = int(handle.read().split()[1])
        rss = pages * os.sysconf('SC_PAGE_SIZE')
    except Exception:                                 # noqa: BLE001
        rss = 0
    handles = 0
    try:
        handles = len(os.listdir('/proc/self/fd'))
    except Exception:                                 # noqa: BLE001
        handles = 0
    return ProcessStats(rss_bytes=rss, handle_count=handles,
                        thread_count=threads)


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD),
                ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]


def process_stats(pid: int) -> Optional[ProcessStats]:
    """读取**指定进程**的实时资源事实（用于把子进程 RSS 与监督进程分开记录）。"""
    pid = int(pid)
    if sys.platform != 'win32':
        rss = 0
        try:
            with open(f'/proc/{pid}/statm', 'r', encoding='utf-8') as handle:
                rss = int(handle.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        except Exception:                             # noqa: BLE001
            return None
        try:
            handles = len(os.listdir(f'/proc/{pid}/fd'))
        except Exception:                             # noqa: BLE001
            handles = 0
        return ProcessStats(rss_bytes=rss, handle_count=handles, thread_count=0)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                          ctypes.sizeof(counters)):
            return None
        rss = int(counters.WorkingSetSize)
    except Exception:                                 # noqa: BLE001
        return None
    finally:
        kernel32.CloseHandle(handle)
    return ProcessStats(rss_bytes=rss, handle_count=0, thread_count=0)


def _windows_process_stats(threads: int) -> ProcessStats:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    rss = 0
    try:
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                      ctypes.sizeof(counters)):
            rss = int(counters.WorkingSetSize)
    except Exception:                                 # noqa: BLE001
        rss = 0
    handles = wintypes.DWORD(0)
    try:
        if not kernel32.GetProcessHandleCount(handle, ctypes.byref(handles)):
            handles = wintypes.DWORD(0)
    except Exception:                                 # noqa: BLE001
        handles = wintypes.DWORD(0)
    return ProcessStats(rss_bytes=rss, handle_count=int(handles.value),
                        thread_count=int(threads))
