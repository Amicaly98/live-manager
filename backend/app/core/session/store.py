"""权威快照的原子持久化与**唯一**读取判定。

复用部分（来自被退役原型，算法不变）：同目录临时文件 → fsync → 原子替换；
带 SHA256 校验和；保留**最后可用副本**；损坏时绝不静默清零。

判定器保留原有的三种结论，它们是**数据安全**判据，与被替换的决策模型无关：

``trusted``      有可信快照（主文件或副本），可以继续
``first_start``  **两份都不存在** —— 唯一允许从零开始的情形
``untrusted``    至少存在一份、却都不可信 —— fail closed，拒绝任何会抹掉进度的转换

"合法 JSON"不等于"可信快照"：缺校验和、schema 不受支持、必要字段不完整、
字段之间自相矛盾，都属于不可信。

**旧 live_state.json 的一次性迁移不属于本阶段**：这里只把它识别为旧格式并判为
不可信（fail closed），不做半可信的解释。真正带新模型的迁移在阶段 2 实施。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .model import (
    ACTIVE_STEPS,
    END_COMPLETED,
    END_MIDNIGHT,
    END_REPLACED,
    MODE_SINGLE,
    MODE_TASK,
    PUSH_FFMPEG,
    PUSH_OBS,
    SCHEMA_VERSION,
    SELECT_AUTO,
    SELECT_SINGLE,
    SELECT_TASK,
    START_NEW,
    START_RESUME,
    STEP_CHOOSE,
    STEP_CLOSE_CHECK,
    STEP_CLOSE_DONE,
    STEP_CLOSE_QUERY,
    STEP_CLOSE_STOP,
    STEP_IDLE,
    STEP_LIVE,
    STEP_OPEN,
    STEP_RESUME_QUERY,
    STEP_WAIT_TASKS,
    STOP_ACCOUNT_EXIT,
    STOP_FACE_VERIFY,
    STOP_PLATFORM_REJECTED,
    STOP_SELECTION_REJECTED,
    STOP_TARGET_REACHED,
    STOP_USER,
    WANT_RUNNING,
    WANT_STOPPED,
    EndDecision,
    Goal,
    PlatformClock,
    Run,
    Selection,
    Snapshot,
)

logger = logging.getLogger(__name__)

_INTEGRITY_KEY = 'integrity'
_BACKUP_SUFFIX = '.bak'

#: ``load()`` 的三种结论
VERDICT_TRUSTED = 'trusted'
VERDICT_FIRST_START = 'first_start'
VERDICT_UNTRUSTED = 'untrusted'

#: 单个文件的读取状态
_FILE_MISSING = 'missing'
_FILE_VALID = 'valid'
_FILE_INVALID = 'invalid'

ALL_STEPS = (
    STEP_IDLE, STEP_WAIT_TASKS, STEP_CLOSE_QUERY, STEP_CLOSE_STOP,
    STEP_CLOSE_CHECK, STEP_CLOSE_DONE, STEP_CHOOSE, STEP_RESUME_QUERY,
    STEP_OPEN, STEP_LIVE,
)
STOP_REASONS = (STOP_USER, STOP_TARGET_REACHED, STOP_FACE_VERIFY,
                STOP_PLATFORM_REJECTED, STOP_ACCOUNT_EXIT,
                STOP_SELECTION_REJECTED)
END_REASONS = (END_COMPLETED, END_MIDNIGHT, END_REPLACED)

#: 旧 ``live_state.json`` 的特征键。只有**完全没有**新格式键时才按旧格式解释。
_LEGACY_KEYS = (
    'is_streaming', 'current_zone', 'effective_seconds', 'elapsed_seconds',
    'execution_date', 'auto_resume', 'phase', 'duration_seconds',
    'duration_known', 'start_time', 'task_id', 'intent', 'run',
)
_NEW_FORMAT_KEYS = ('schema_version', 'goal', 'step')


def _digest(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(body).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_day(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass
class LoadResult:
    snapshot: Snapshot
    status: str = VERDICT_FIRST_START
    source: str = 'none'           # file | backup | none
    corrupt: bool = False          # == (status == untrusted)
    legacy: bool = False           # 读到了旧格式（判为不可信）
    error: str = ''


@dataclass
class _FileRead:
    state: str                     # missing | valid | invalid
    snapshot: Optional[Snapshot] = None
    legacy: bool = False
    error: str = ''

    @property
    def ok(self) -> bool:
        return self.state == _FILE_VALID and self.snapshot is not None


def _looks_legacy(document: Dict[str, Any]) -> bool:
    """只有**完全没有**新格式键、且带旧特征键的文件才按旧格式解释。"""
    if any(key in document for key in _NEW_FORMAT_KEYS):
        return False
    return any(key in document for key in _LEGACY_KEYS)


# ---------------- 严格判定 ----------------

def _validate_selection(raw: Any) -> Tuple[Optional[Selection], str]:
    if not isinstance(raw, dict):
        return None, 'goal.selection 不是对象'
    kind = raw.get('kind')
    if kind not in (SELECT_AUTO, SELECT_TASK, SELECT_SINGLE):
        return None, f'goal.selection.kind 非法：{kind!r}'
    if raw.get('push_source') not in (PUSH_FFMPEG, PUSH_OBS):
        return None, f'goal.selection.push_source 非法：{raw.get("push_source")!r}'
    target = raw.get('target_seconds', 0)
    if not _is_int(target) or target < 0:
        return None, 'goal.selection.target_seconds 非法'
    task_id = raw.get('task_id')
    if kind == SELECT_TASK and not _is_int(task_id):
        return None, 'goal.selection 指定任务却缺少 task_id'
    expected_revision = raw.get('expected_revision')
    business_date = raw.get('business_date')
    if kind == SELECT_TASK:
        if not _is_int(expected_revision) or expected_revision < 0:
            return None, 'goal.selection 指定任务缺少有效 expected_revision'
        if not _is_day(business_date):
            return None, 'goal.selection 指定任务缺少有效 business_date'
    elif expected_revision is not None or business_date is not None:
        return None, '非指定任务选择不能携带任务票据'
    if task_id is not None and not _is_int(task_id):
        return None, 'goal.selection.task_id 非法'
    zone = raw.get('zone_name', '')
    if not isinstance(zone, str):
        return None, 'goal.selection.zone_name 非法'
    area = raw.get('area_id')
    if area is not None and not _is_int(area):
        return None, 'goal.selection.area_id 非法'
    return Selection.from_dict(raw), ''


def _validate_goal(raw: Any) -> Tuple[Optional[Goal], str]:
    if not isinstance(raw, dict):
        return None, 'goal 不是对象'
    revision = raw.get('revision', 0)
    if not _is_int(revision) or revision < 0:
        return None, 'goal.revision 非法'
    wanted = raw.get('wanted')
    if wanted not in (WANT_RUNNING, WANT_STOPPED):
        return None, f'goal.wanted 非法：{wanted!r}'
    if raw.get('mode') not in (MODE_TASK, MODE_SINGLE):
        return None, f'goal.mode 非法：{raw.get("mode")!r}'
    if raw.get('start_kind') not in (START_NEW, START_RESUME):
        return None, f'goal.start_kind 非法：{raw.get("start_kind")!r}'
    reason = raw.get('stop_reason')
    if reason is not None and reason not in STOP_REASONS:
        return None, f'goal.stop_reason 非法：{reason!r}'
    selection, error = _validate_selection(raw.get('selection') or {})
    if selection is None:
        return None, error
    return Goal.from_dict(raw), ''


def _validate_clock(raw: Any) -> Tuple[Optional[PlatformClock], str]:
    if not isinstance(raw, dict):
        return None, 'run.clock 不是对象'
    checkpoint = raw.get('checkpoint_seconds', 0)
    if not _is_number(checkpoint) or checkpoint < 0:
        return None, 'run.clock.checkpoint_seconds 非法'
    if not isinstance(raw.get('projecting', False), bool):
        return None, 'run.clock.projecting 非法'
    mono = raw.get('interval_start_mono')
    if mono is not None and not _is_number(mono):
        return None, 'run.clock.interval_start_mono 非法'
    wall = raw.get('interval_start_wall')
    if wall is not None and _parse_dt(wall) is None:
        return None, 'run.clock.interval_start_wall 非法'
    anchor = raw.get('display_anchor_mono')
    if anchor is not None and not _is_number(anchor):
        return None, 'run.clock.display_anchor_mono 非法'
    # 区间开着却没有墙钟锚点：重启后无法把计时坐标重建出来，
    # 这份进度的"当前区间"部分就不再可信（沿用原存储的数据安全判据）。
    if mono is not None and wall is None:
        return None, 'run.clock 区间开着但缺少墙钟锚点'
    return PlatformClock.from_dict(raw), ''


def _validate_end(raw: Any) -> Tuple[Optional[EndDecision], str]:
    if not isinstance(raw, dict):
        return None, 'run.end 不是对象'
    if raw.get('reason') not in END_REASONS:
        return None, f'run.end.reason 非法：{raw.get("reason")!r}'
    if not _is_day(raw.get('business_day')):
        return None, 'run.end.business_day 非法'
    if not isinstance(raw.get('settle', False), bool):
        return None, 'run.end.settle 非法'
    seconds = raw.get('effective_seconds', 0)
    if not _is_number(seconds) or seconds < 0:
        return None, 'run.end.effective_seconds 非法'
    receipt = raw.get('ledger_receipt')
    if receipt is not None and not isinstance(receipt, str):
        return None, 'run.end.ledger_receipt 非法'
    completed_at = raw.get('completed_at')
    if completed_at is not None and _parse_dt(completed_at) is None:
        return None, 'run.end.completed_at 非法'
    return EndDecision.from_dict(raw), ''


def _validate_run(raw: Any) -> Tuple[Optional[Run], str]:
    if not isinstance(raw, dict):
        return None, 'run 不是对象'
    run_id = raw.get('id')
    if not isinstance(run_id, str) or not run_id:
        return None, 'run.id 缺失或为空'
    for field_name in ('uid', 'room_id'):
        value = raw.get(field_name, 0)
        if not _is_int(value) or value < 0:
            return None, f'run.{field_name} 非法'
    area = raw.get('area_id')
    if area is not None and not _is_int(area):
        return None, 'run.area_id 非法'
    if not isinstance(raw.get('zone_name', ''), str):
        return None, 'run.zone_name 非法'
    mode = raw.get('mode')
    if mode not in (MODE_TASK, MODE_SINGLE):
        return None, f'run.mode 非法：{mode!r}'
    if raw.get('push_source') not in (PUSH_FFMPEG, PUSH_OBS):
        return None, f'run.push_source 非法：{raw.get("push_source")!r}'
    task_id = raw.get('task_id')
    if task_id is not None and not _is_int(task_id):
        return None, 'run.task_id 非法'
    if not _is_day(raw.get('business_day')):
        return None, 'run.business_day 非法'
    target = raw.get('target_seconds', 0)
    if not _is_number(target) or target < 0:
        return None, 'run.target_seconds 非法'
    revision = raw.get('selected_by_revision', 0)
    if not _is_int(revision) or revision < 0:
        return None, 'run.selected_by_revision 非法'
    guard = raw.get('settlement_guard')
    # 缺字段 = 这份快照没有冻结过人工进度凭据（缺 guard 时提交不会被临时补值），
    # 是**合法**的历史状态；给了一个非法值才是损坏。
    if guard is not None and (not _is_int(guard) or guard < 0):
        return None, 'run.settlement_guard 非法'
    task_source_revision = raw.get('task_source_revision')
    if task_source_revision is not None and (
            not _is_int(task_source_revision) or task_source_revision < 0):
        return None, 'run.task_source_revision 非法'
    clock, error = _validate_clock(raw.get('clock') or {})
    if clock is None:
        return None, error
    end, error = _validate_end(raw.get('end')) if raw.get('end') is not None else (None, '')
    if error:
        return None, error
    if end is not None and end.settle and task_id is None:
        return None, 'run.end 要求结算却没有 task_id'
    return Run.from_dict(raw), ''


def validate_snapshot(document: Any) -> Tuple[Optional[Snapshot], str]:
    """严格判定一份**新格式**文档；返回 (快照, 错误)。"""
    if not isinstance(document, dict):
        return None, '内容不是 JSON 对象'

    integrity = document.get(_INTEGRITY_KEY)
    if not isinstance(integrity, dict) or not integrity.get('digest'):
        return None, '缺少校验和（被截断或外部改写的文件）'
    body = {k: v for k, v in document.items() if k != _INTEGRITY_KEY}
    if _digest(body) != integrity.get('digest'):
        return None, '校验和不匹配（文件被截断或改写）'

    version = document.get('schema_version')
    if version != SCHEMA_VERSION:
        return None, f'不支持的 schema_version：{version!r}'

    goal, error = _validate_goal(document.get('goal'))
    if goal is None:
        return None, error

    step = document.get('step')
    if step not in ALL_STEPS:
        return None, f'step 非法：{step!r}'

    raw_run = document.get('run')
    run = None
    if raw_run is not None:
        run, error = _validate_run(raw_run)
        if run is None:
            return None, error

    # 字段之间的自相矛盾一律判为不可信，不从零受理。
    # 注意：`close_*` 允许 run 为空 —— 新场"先关再开"的首次核对本来就还没有
    # 自己场次，此时核对的是当前登录上下文的房间。
    if step in (STEP_OPEN, STEP_RESUME_QUERY, STEP_LIVE) and run is None:
        return None, f'step={step} 需要当前场次，但 run 为空'
    if run is not None and run.end is not None and step not in ACTIVE_STEPS:
        return None, f'已形成结束决定的场次不能停留在 step={step}'
    if run is not None and run.mode == MODE_SINGLE and run.task_id is not None:
        return None, '单次场次不应带 task_id'
    if run is not None and run.business_day == '':
        return None, 'run.business_day 为空'

    return Snapshot(
        schema_version=version,
        goal=goal,
        run=run,
        step=step,
        saved_wall=document.get('saved_wall'),
        health=dict(document.get('health') or {}),
    ), ''


class SessionStore:
    """单文件原子快照存储。"""

    def __init__(self, path):
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + _BACKUP_SUFFIX)
        self.last_error = ''

    # ---------- 读 ----------

    def load(self) -> LoadResult:
        """唯一读取判定：可信 / 首次启动 / 不可信。"""
        primary = self._read(self.path)
        if primary.ok:
            return LoadResult(primary.snapshot, status=VERDICT_TRUSTED,
                              source='file', error=primary.error)

        backup = self._read(self.backup_path)
        if backup.ok:
            logger.error('主状态文件不可用（%s），已回退到最后可用副本 %s',
                         primary.error or '缺失', self.backup_path)
            snapshot = backup.snapshot
            snapshot.health = dict(snapshot.health or {})
            snapshot.health['recovered_from_backup'] = True
            return LoadResult(snapshot, status=VERDICT_TRUSTED, source='backup',
                              error=primary.error or backup.error)

        if primary.state == _FILE_MISSING and backup.state == _FILE_MISSING:
            return LoadResult(Snapshot(goal=Goal()), status=VERDICT_FIRST_START,
                              source='none')

        # 至少有一份存在，却没有一份可信：绝不从 0 秒受理。
        error = primary.error or backup.error or '状态文件不可信'
        legacy = primary.legacy or backup.legacy
        logger.error('状态文件不可信，已保留现场不做覆盖：%s（%s）', self.path, error)
        empty = Snapshot(goal=Goal())
        empty.health = {
            'corrupt': True, 'error': error, 'legacy': legacy,
            'primary': primary.state, 'backup': backup.state,
        }
        return LoadResult(empty, status=VERDICT_UNTRUSTED, source='none',
                          corrupt=True, legacy=legacy, error=error)

    def _read(self, path: Path) -> _FileRead:
        if not path.exists():
            return _FileRead(_FILE_MISSING)
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            return _FileRead(_FILE_INVALID, error=f'无法读取（{exc}）')
        try:
            document = json.loads(text)
        except ValueError as exc:
            return _FileRead(_FILE_INVALID, error=f'JSON 解析失败（{exc}）')
        if not isinstance(document, dict):
            return _FileRead(_FILE_INVALID, error='内容不是 JSON 对象')

        if _looks_legacy(document):
            # 一次性迁移属于阶段 2：这里只 fail closed，不做半可信解释。
            return _FileRead(_FILE_INVALID, legacy=True,
                             error='旧格式状态文件（迁移在阶段 2 实施，当前判为不可信）')

        snapshot, error = validate_snapshot(document)
        if snapshot is None:
            return _FileRead(_FILE_INVALID, error=error)
        return _FileRead(_FILE_VALID, snapshot)

    # ---------- 写 ----------

    def save(self, snapshot: Snapshot) -> bool:
        """原子保存。失败返回 False，且不改动现有权威文件。"""
        self.last_error = ''
        snapshot.schema_version = SCHEMA_VERSION
        snapshot.saved_wall = datetime.now().isoformat(timespec='seconds')
        payload = snapshot.to_dict()
        document = dict(payload)
        document[_INTEGRITY_KEY] = {'algo': 'sha256', 'digest': _digest(payload)}
        text = json.dumps(document, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(self.path, text)
            try:
                self._atomic_write(self.backup_path, text)
            except OSError as exc:
                # 备份写失败不影响权威文件的可用性，但必须可见。
                logger.warning('状态备份写入失败（不影响本次保存）：%s', exc)
            return True
        except OSError as exc:
            self.last_error = str(exc)
            logger.error('状态保存失败：%s', exc)
            return False

    def _atomic_write(self, path: Path, text: str) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp',
                                        dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            self._fsync_dir(path.parent)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
