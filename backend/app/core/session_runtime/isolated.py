"""阶段 3C 隔离 worker 的 JSON Lines 控制壳。

此模块只解析窄测试输入并调用 RuntimeInstance；所有业务意图仍由 Core 归约。
调用进程须先安装网络守卫、完成 ``config.init``，再导入本模块。
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import signal
import sys
import threading
import time
from datetime import date, datetime
from typing import Any, Optional, TextIO


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        payload = _jsonable(dataclasses.asdict(value))
        payload['__type__'] = type(value).__name__
        return payload
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, 'model_dump'):
        return _jsonable(value.model_dump())
    return repr(value)


_OUTPUT_LOCK = threading.Lock()


def write_line(stream: TextIO, value: dict) -> None:
    with _OUTPUT_LOCK:
        stream.write(json.dumps(_jsonable(value), ensure_ascii=True,
                                separators=(',', ':')) + '\n')
        stream.flush()


def dispatch(instance: Any, command: dict, *, timeout: float = 5.0) -> dict:
    """执行一个显式测试输入；没有房间/恢复/任务业务分支。"""
    name = command.get('cmd')
    if name == 'snapshot':
        return {'ok': True, 'snapshot': instance.snapshot_copy()}
    if name == 'mutate_snapshot_copy':
        copied = instance.snapshot_copy()
        if copied is None:
            raise RuntimeError('当前没有可读快照')
        copied.setdefault('goal', {})['wanted'] = 'stopped'
        copied['step'] = 'mutated-copy-only'
        return {'ok': True, 'mutated_copy': copied,
                'snapshot_after_mutation': instance.snapshot_copy()}
    if name == 'stats':
        media = instance.media_port
        owned = getattr(media, '_owned', None)
        media_stats = {
            key: getattr(media, key, None)
            for key in ('spawns', 'reclaims', 'reports', 'os_errors', 'bindings')
        }
        media_stats['pid'] = getattr(owned, 'pid', None)
        media_stats['run_id'] = getattr(owned, 'run_id', None)
        return {'ok': True, 'snapshot': instance.snapshot_copy(),
                'runtime': instance.runtime.stats(),
                'media': media_stats,
                'notifications': {
                    'summaries': len(instance.notifications.summaries),
                    'messages': len(instance.notifications.messages),
                },
                'data_port': {
                    'select_calls': getattr(instance.task_port, 'select_calls', 0),
                    'reset_calls': getattr(instance.task_port, 'reset_calls', 0),
                },
                'clock': {
                    'wall': instance.clock.wall().isoformat(timespec='seconds'),
                    'mono': instance.clock.mono(),
                },
                'inventory': instance.inventory}
    if name == 'summary_copy':
        return {'ok': True, 'summary': _jsonable(instance.notifications.summaries)}
    if name == 'close_repeat':
        first = instance.close(float(command.get('budget', 5.0)))
        second = instance.close(float(command.get('budget', 5.0)))
        return {'ok': True, 'closed': True, 'report': first,
                'repeat_report': second, 'same_report': first is second}
    if name in ('start', 'stop', 'confirm'):
        from ..session.events import UserConfirmation, UserGoal
        from ..session.model import (
            MODE_SINGLE, MODE_TASK, PUSH_FFMPEG, SELECT_AUTO, SELECT_SINGLE,
            START_NEW, STOP_USER, WANT_RUNNING, WANT_STOPPED, Selection,
        )
        if name == 'confirm':
            event = UserConfirmation(
                goal_revision=int(command.get('goal_revision', -1)),
                uid=int(command.get('uid', -1)),
                room_id=int(command.get('room_id', -1)))
        elif name == 'stop':
            mode = command.get('mode', MODE_TASK)
            if mode not in (MODE_TASK, MODE_SINGLE):
                raise ValueError('stop.mode 必须为 task 或 single')
            event = UserGoal(wanted=WANT_STOPPED, mode=mode,
                             start_kind=START_NEW, stop_reason=STOP_USER)
        else:
            mode = command.get('mode', MODE_TASK)
            source = command.get('push_source', PUSH_FFMPEG)
            if mode not in (MODE_TASK, MODE_SINGLE):
                raise ValueError('start.mode 必须为 task 或 single')
            if source not in (PUSH_FFMPEG, 'obs'):
                raise ValueError('start.push_source 必须为 ffmpeg 或 obs')
            if mode == MODE_SINGLE:
                raw_target = command.get('target_seconds', 3)
                target_seconds = 3 if raw_target is None else int(raw_target)
                if target_seconds < 0:
                    raise ValueError('single.target_seconds 不能小于 0')
                raw_area = command.get('area_id', 1)
                area_id = int(raw_area)
                if area_id <= 0:
                    raise ValueError('single.area_id 必须为正整数')
                selection = Selection(
                    kind=SELECT_SINGLE,
                    zone_name=str(command.get('zone_name') or '测试区'),
                    area_id=area_id,
                    push_source=source,
                    target_seconds=target_seconds)
            else:
                selection = Selection(kind=SELECT_AUTO, push_source=source)
            event = UserGoal(wanted=WANT_RUNNING, mode=mode, selection=selection)
        result = instance.submit(event, timeout=timeout)
        return {'ok': True, 'result': result,
                'snapshot': instance.snapshot_copy()}
    if name == 'signal':
        number = getattr(signal, 'SIGBREAK' if os.name == 'nt' else 'SIGTERM', None)
        if number is None or not hasattr(signal, 'raise_signal'):
            raise RuntimeError('当前 Python 未提供本机退出信号 raise_signal')
        def delayed_raise():
            # 返回命令回执后，给 run_jsonl 留出进入空闲等待的时间；信号仍由真实
            # Python API 投递，handler 只设置退出事件。
            time.sleep(0.25)
            signal.raise_signal(number)

        thread = threading.Thread(target=delayed_raise,
                                  name='p3c-delayed-local-signal', daemon=False)
        instance._test_signal_thread = thread
        thread.start()
        return {'ok': True, 'signal_requested': number,
                'signal_name': 'SIGBREAK' if os.name == 'nt' else 'SIGTERM',
                'delivery': 'child helper waits 250ms then calls signal.raise_signal'}
    if name == 'release_settle':
        gate = getattr(instance, '_test_settle_gate', None)
        if gate is None:
            raise RuntimeError('当前实例没有暂停中的测试结算')
        gate.set()
        return {'ok': True, 'released': True}
    if name == 'release_select':
        gate = getattr(instance, '_test_select_gate', None)
        if gate is None:
            raise RuntimeError('当前实例没有暂停中的测试选取')
        gate.set()
        return {'ok': True, 'released': True}
    if name == 'tasks_changed':
        result = instance.tasks_changed(
            str(command.get('business_day') or ''),
            str(command.get('reason') or 'test'), timeout=timeout)
        return {'ok': True, 'result': result,
                'snapshot': instance.snapshot_copy()}
    if name == 'create_task':
        task = command.get('task')
        if not isinstance(task, dict):
            raise ValueError('create_task.task 必须是对象')
        created = instance.task_manager.create_task(task)
        return {'ok': True, 'created': bool(created)}
    if name == 'close':
        report = instance.close(float(command.get('budget', 5.0)))
        return {'ok': True, 'closed': True, 'report': report}
    raise ValueError(f'未知隔离控制命令：{name!r}')


def run_jsonl(instance: Any, *, input_stream: Optional[TextIO] = None,
              output_stream: Optional[TextIO] = None,
              exit_event=None, command_timeout: float = 5.0) -> None:
    """逐行转交输入；信号回调只唤醒此循环，不自行写 Goal 或发平台请求。"""
    source = input_stream or sys.stdin
    sink = output_stream or sys.stdout
    while True:
        if exit_event is not None and exit_event.is_set():
            report = instance.close()
            write_line(sink, {'ok': True, 'signal_exit': True, 'report': report})
            return
        if not _input_ready(source):
            # This bounded wait belongs to the local JSONL shell, not Core's
            # business schedule. It lets a signal wake an otherwise idle worker.
            time.sleep(0.05)
            continue
        line = source.readline()
        if line == '':
            break
        try:
            command = json.loads(line)
            if not isinstance(command, dict):
                raise ValueError('命令必须是 JSON 对象')
            result = dispatch(instance, command, timeout=command_timeout)
            if '_id' in command:
                result['id'] = command['_id']
            write_line(sink, result)
            if command.get('cmd') in ('close', 'close_repeat'):
                return
            if exit_event is not None and exit_event.is_set():
                report = instance.close()
                write_line(sink, {'ok': True, 'signal_exit': True,
                                  'report': report})
                return
        except BaseException as exc:  # noqa: BLE001 - 协议必须报告而非吞掉
            write_line(sink, {'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
        if exit_event is not None and exit_event.is_set():
            break
    report = instance.close()
    write_line(sink, {'ok': True, 'input_closed': True, 'report': report})


def _input_ready(source: TextIO) -> bool:
    """检查本地控制管道是否可读，避免信号 handler 被阻塞 readLine 挂住。"""
    try:
        fd = source.fileno()
    except (AttributeError, OSError, ValueError):
        return True
    if os.name == 'nt':
        import ctypes
        import msvcrt
        available = ctypes.c_ulong(0)
        handle = msvcrt.get_osfhandle(fd)
        peek = ctypes.windll.kernel32.PeekNamedPipe
        ok = peek(ctypes.c_void_p(handle), None, 0, None,
                  ctypes.byref(available), None)
        return not bool(ok) or available.value > 0
    import select
    readable, _, _ = select.select([source], [], [], 0)
    return bool(readable)
