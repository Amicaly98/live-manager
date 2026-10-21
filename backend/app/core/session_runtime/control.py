"""窄控制服务：协议校验、操作令牌、账号移交与只读 Core 投影。

本模块不保存 Goal/Run/elapsed，不直接写状态文件，也不执行平台或媒体动作。
所有控制事件均经 Runtime.submit 进入唯一 Core actor。

三个承重职责（阶段 4A）：

1. **请求与确认由调用方冻结**：恢复/放弃/放弃并新开/人工确认所需的原账号、
   Goal revision、Run.id 与原选择，必须由页面或原链接在**发出请求那一刻**带入；
   服务只校验形状并按原样转交，绝不用当前快照补最新身份。缺参或陈旧一律明确拒绝
   （``ControlServiceError`` 或 rejected 回执），零 submit。
2. **回执与短锁**：令牌领取、内容指纹比较、回执发布、实例指针交换都在短锁内完成
   （``_lock``）；``submit`` 等待、收尾等待、``close`` 与实例工厂构造都在锁外。
   操作令牌**全局**绑定首次请求，不随账号代次重新作用到新账号；同 token 同内容回原
   回执（只提交一次），不同内容冲突，并发重复请求共享一份在途执行权。账号移交与
   运行输入**双向**互斥：任一类仍有在途未完成领取时，另一类的新领取都暂拒（不缓存，
   可重试），因此先领取的运行请求也无法绕过退出、在退出落盘后晚到翻回 running。
3. **资源移交**：``close`` 未通过核验时保留旧实例引用与真实报告，不安装新账号、
   不丢资源所有者。旧 actor 完成收尾、安全核验通过后才原子换指针；等待与关闭共用
   一份**绝对截止**。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..session.events import (
    Accepted,
    Ignored,
    Rejected,
    UserConfirmation,
    UserGoal,
)
from ..session.model import (
    MODE_SINGLE,
    MODE_TASK,
    SELECT_AUTO,
    SELECT_SINGLE,
    START_NEW,
    START_RESUME,
    STOP_ACCOUNT_EXIT,
    STOP_USER,
    WANT_RUNNING,
    WANT_STOPPED,
    Selection,
)


logger = logging.getLogger(__name__)


class ControlServiceError(ValueError):
    """请求无法形成一条安全的 Core 输入。"""


@dataclass(frozen=True)
class ControlReceipt:
    """服务边界的持久受理回执；accepted 只表示 Goal 已保存。"""

    operation_token: str
    status: str
    accepted: bool
    reason: str = ''
    projection: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            'operation_token': self.operation_token,
            'status': self.status,
            'accepted': self.accepted,
            'reason': self.reason,
            'projection': copy.deepcopy(self.projection),
        }


class _TokenClaim:
    """一次操作令牌的领取记录：指纹 + 执行权分类 + 已发布回执 + 完成通知。

    并发到达的**同 token 同内容**请求共享同一份领取：后来者等待首个执行者发布回执，
    不排队再次执行平台命令。``kind`` 是这次执行权的**临时分类**（``'handoff'`` /
    ``'running'`` / ``None``）：只用于短锁内的互斥领取，不是第二份 Goal/账号状态，
    也不落盘。回执发布后该领取不再"在途"，互斥据此自动解除。
    """

    __slots__ = ('fingerprint', 'kind', 'receipt', 'done')

    def __init__(self, fingerprint: str, kind: Optional[str] = None) -> None:
        self.fingerprint = fingerprint
        self.kind = kind
        self.receipt: Optional[ControlReceipt] = None
        self.done = threading.Event()


class ControlService:
    """围绕一个 RuntimeInstance 的小控制面。账号切换必须由实例工厂完成。"""

    _MAX_TOKEN_LENGTH = 160
    _MAX_RECEIPTS = 256
    #: 会改变运行授权或抢占账号资源移交的操作：退出/移交进行期间明确拒绝。
    _RUNNING_ACTIONS = frozenset((
        'start', 'resume', 'confirm', 'abandon', 'restart_after_abandon',
    ))
    #: 账号资源取得/移交操作：同一时刻只允许一个领取执行权。
    _HANDOFF_ACTIONS = frozenset(('login', 'logout'))
    _FORBIDDEN_EDIT_FIELDS = frozenset((
        'elapsed_seconds', 'checkpoint_seconds', 'restore_elapsed_seconds',
        'restore_target_seconds', 'is_streaming', 'current_zone',
    ))

    def __init__(self, instance: Any,
                 instance_factory: Optional[Callable[[Any, Dict[str, str]], Any]] = None,
                 *, submit_timeout: float = 5.0,
                 close_budget: float = 10.0):
        self._instance = instance
        self._instance_factory = instance_factory
        self._submit_timeout = max(0.1, float(submit_timeout))
        self._close_budget = max(0.0, float(close_budget))
        self._lock = threading.RLock()
        self._generation = 0
        #: 操作令牌 → 领取记录。**全局**，不以账号代次为键：账号变化不会令旧
        #: token 重新作用到新账号（旧回执可重放，但绝不再执行）。
        self._receipts: 'OrderedDict[str, _TokenClaim]' = OrderedDict()
        #: 未能安装、又**没有收齐资源**的候选实例：``(实例, 真实关闭报告)``。
        #: 它们不是已登录账号（``_instance`` 保持为空、不写凭据），但仍然是本服务
        #: 拥有的**资源所有者**——引用绝不能随调用栈返回而丢失（复核 R2）。存在期间
        #: 新的账号安装在**工厂之前**被暂拒，避免同一实例目录出现第二份运行实例。
        self._unreleased: List[Tuple[Any, Any]] = []

    @property
    def instance(self) -> Any:
        return self._instance

    @property
    def unreleased_candidates(self) -> List[dict]:
        """未收齐资源的候选摘要（只读观察口；不含凭据，不参与任何判定）。"""
        with self._lock:
            return [{'report': (report.to_dict() if callable(
                getattr(report, 'to_dict', None)) else None),
                'has_reference': instance is not None}
                for instance, report in self._unreleased]

    def install_instance(self, instance: Any) -> bool:
        """启动路径原子安装**一份**账号实例；槽位非空时绝不覆盖。

        这是生产默认入口在启动时把"已登录账号实例"交给控制面的唯一入口：返回
        ``False`` 表示槽位已被占用（例如并发扫码先装上了），调用方必须自己释放
        传入的实例——它不会替调用方关闭，也不会丢掉已有所有者。
        """
        with self._lock:
            if self._instance is not None:
                return False
            self._instance = instance
            self._generation += 1
            return True

    def read(self) -> Optional[dict]:
        """返回 Runtime 发布的深副本；调用方编辑它不会改变 Core 或 JSON。"""
        return self._read_instance(self._instance)

    # ------------------------------------------------------------------ 入口

    def start(self, request: dict, operation_token: str, *,
              account_context: Optional[dict] = None) -> ControlReceipt:
        frozen = self._freeze_start_request(request)
        return self._with_token(
            operation_token, 'start',
            {'frozen': frozen, 'account': account_context},
            lambda token: self._start(token, frozen, account_context),
            account=account_context)

    def stop(self, operation_token: str, *,
             account_context: Optional[dict] = None) -> ControlReceipt:
        return self._with_token(
            operation_token, 'stop', {'account': account_context},
            lambda token: self._stop(token, account_context),
            account=account_context)

    def resume(self, operation_token: str, *, run_id: Optional[str] = None,
               goal_revision: Optional[int] = None,
               account_context: Optional[dict] = None) -> ControlReceipt:
        """恢复原场：必须携带用户看到的原 Run.id 与 Goal revision。"""
        request = {'run_id': run_id, 'goal_revision': goal_revision,
                   'account': account_context}
        return self._with_token(
            operation_token, 'resume', request,
            lambda token: self._resume(token, run_id, goal_revision,
                                       account_context),
            account=account_context)

    def confirm(self, operation_token: str, *, goal_revision: Optional[int] = None,
                uid: Optional[int] = None,
                room_id: Optional[int] = None) -> ControlReceipt:
        """人工确认：必须携带原 Goal revision 与原账号/房间，服务不补最新身份。"""
        request = {'goal_revision': goal_revision, 'uid': uid, 'room_id': room_id}
        return self._with_token(
            operation_token, 'confirm', request,
            lambda token: self._confirm(token, goal_revision, uid, room_id))

    def abandon(self, operation_token: str, *, run_id: Optional[str] = None,
                goal_revision: Optional[int] = None,
                selection: Any = None) -> ControlReceipt:
        """单独放弃原场：必须携带原 Run.id、Goal revision 与原选择。"""
        canonical_selection = self._canonical_selection(selection)
        request = {'run_id': run_id, 'goal_revision': goal_revision,
                   'selection': canonical_selection}
        return self._with_token(
            operation_token, 'abandon', request,
            lambda token: self._abandon(token, run_id, goal_revision,
                                        canonical_selection))

    def restart_after_abandon(self, request: dict,
                              operation_token: str) -> ControlReceipt:
        """放弃旧场并一步开新场：请求须同时携带原场冻结上下文与新选择。

        ``request['abandon']`` = ``{'run_id', 'goal_revision', 'selection'}``，
        由页面/链接在弹框那一刻冻结；缺失即明确拒绝，服务不读当前快照填空。
        """
        if not isinstance(request, dict):
            raise ControlServiceError('控制请求必须是对象')
        frozen_abandon = request.get('abandon')
        frozen = self._freeze_start_request(request)
        if not isinstance(frozen_abandon, dict):
            raise ControlServiceError('放弃并新开缺少原场冻结上下文')
        run_id = frozen_abandon.get('run_id')
        goal_revision = frozen_abandon.get('goal_revision')
        selection = self._canonical_selection(frozen_abandon.get('selection'))
        if run_id is None or goal_revision is None or selection is None:
            raise ControlServiceError('放弃并新开的原场上下文不完整')
        request_echo = {'frozen': frozen, 'abandon': {
            'run_id': run_id, 'goal_revision': goal_revision,
            'selection': selection}}
        return self._with_token(
            operation_token, 'restart_after_abandon', request_echo,
            lambda token: self._restart_after_abandon(
                token, frozen, run_id, goal_revision, selection))

    def logout(self, operation_token: str, *, next_account: Any = None,
               credentials: Optional[Dict[str, str]] = None,
               timeout: float = 15.0,
               account_context: Optional[dict] = None) -> ControlReceipt:
        account_tag = None
        if next_account is not None:
            account_tag = {
                'uid': getattr(next_account, 'uid', None),
                'room_id': getattr(next_account, 'room_id', None),
            }
        credential_tag = None
        if credentials is not None:
            credential_tag = hashlib.sha256(
                self._canonical(credentials).encode('utf-8')).hexdigest()
        request = {'next_account': account_tag,
                   'credentials_fingerprint': credential_tag,
                   'account': account_context}
        return self._with_token(
            operation_token, 'logout', request,
            lambda token: self._logout(
                token, next_account, credentials, max(0.1, float(timeout)),
                account_context))

    def login(self, operation_token: str, account: Any,
              credentials: Dict[str, str], *,
              still_valid: Optional[Callable[[], bool]] = None) -> ControlReceipt:
        """安装一个已核验的账号候选；``still_valid`` 是**扫码代次**之类的候选身份判定。

        ``still_valid`` 在**实际安装提交点**（实例已经建好、即将占用账号插槽那一刻）
        再问一次，而不是只在本服务入口之前问一次：构造实例的慢活（数据接管、组装、
        actor 启动、Bootstrap）期间用户可能已经换了二维码。判定为失效时不安装、
        不写凭据，并把**已经建好的实例**按既有 ``close`` 门释放（复核 R1）。
        """
        account_tag = {'uid': getattr(account, 'uid', None),
                       'room_id': getattr(account, 'room_id', None)}
        credential_tag = hashlib.sha256(
            self._canonical(credentials).encode('utf-8')).hexdigest()
        request = {'account': account_tag,
                   'credentials_fingerprint': credential_tag}
        return self._with_token(
            operation_token, 'login', request,
            lambda token: self._login(token, account, credentials, still_valid))

    # ---------------------------------------------------------------- inputs

    def _freeze_start_request(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ControlServiceError('控制请求必须是对象')
        forbidden = self._FORBIDDEN_EDIT_FIELDS.intersection(request)
        if forbidden:
            raise ControlServiceError(
                '状态编辑字段已退役：' + ', '.join(sorted(forbidden)))
        allowed_request = {
            'mode', 'selection', 'task_id', 'expected_revision',
            'business_date', 'push_source', 'kind', 'zone_name',
            'area_id', 'target_seconds', 'abandon',
        }
        unknown = set(request).difference(allowed_request)
        if unknown:
            raise ControlServiceError('控制请求包含未知字段')
        mode = str(request.get('mode') or MODE_TASK)
        if mode not in (MODE_TASK, MODE_SINGLE):
            raise ControlServiceError('不支持的开播模式')
        raw = request.get('selection')
        if raw is None:
            if mode == MODE_TASK:
                if request.get('task_id') is None:
                    raw = {'kind': SELECT_AUTO,
                           'push_source': request.get('push_source')}
                else:
                    raw = {
                        'kind': 'task',
                        'task_id': request.get('task_id'),
                        'expected_revision': request.get('expected_revision'),
                        'business_date': request.get('business_date'),
                        'push_source': request.get('push_source'),
                    }
            else:
                raw = {key: request[key] for key in (
                    'kind', 'zone_name', 'area_id', 'push_source',
                    'target_seconds') if key in request}
                raw.setdefault('kind', SELECT_SINGLE)
        if not isinstance(raw, (dict, Selection)):
            raise ControlServiceError('开播选择无效')
        if isinstance(raw, dict):
            nested_forbidden = self._FORBIDDEN_EDIT_FIELDS.intersection(raw)
            if nested_forbidden:
                raise ControlServiceError(
                    '状态编辑字段已退役：' + ', '.join(sorted(nested_forbidden)))
        selection = (copy.deepcopy(raw) if isinstance(raw, Selection)
                     else Selection.from_dict(raw))
        if mode == MODE_TASK and selection.kind not in (SELECT_AUTO, 'task'):
            raise ControlServiceError('任务模式只接受自动或指定任务选择')
        if mode == MODE_SINGLE and selection.kind != SELECT_SINGLE:
            raise ControlServiceError('单次模式必须提供单次分区参数')
        if mode == MODE_TASK and 'target_seconds' in request:
            raise ControlServiceError('任务目标时长由任务本身确定')
        return {'mode': mode, 'selection': selection}

    def _current_push_source(self) -> str:
        """本实例**当前可信设置**解释出的推流来源（唯一权威）。

        推流方式只由设置页决定（manual→obs，其余→ffmpeg，见
        :func:`app.api.settings.current_push_source`）。页面/管理端传来的
        ``push_source`` 只是兼容字段，不再是覆盖设置的独立权威；读取沿用既有
        宽松语义（拿不到可信配置时返回默认并已记录错误），**不**静默回退 OBS。
        """
        from app.api.settings import current_push_source
        return current_push_source()

    def _with_current_push_source(self, selection: Selection) -> Selection:
        """把选择里的推流来源换成当前设置值；其余字段原样保留。

        解释发生在**本次操作首次有效受理时**（令牌领取之后）：同令牌重放回原
        回执，不会因期间设置变化而执行成另一种操作，也不额外制造第二次开播。
        """
        source = self._current_push_source()
        if selection.push_source == source:
            return selection
        updated = copy.copy(selection)
        updated.push_source = source
        return updated

    def _start(self, token: str, frozen: dict,
               account_context: Optional[dict]) -> ControlReceipt:
        instance = self._require_instance(token)
        self._check_account_context(instance, account_context)
        selection = self._with_current_push_source(frozen['selection'])
        refusal = self._preflight_start(instance, frozen['mode'], selection)
        if refusal:
            # 同步拦截：不提交 Goal、不发平台请求；页面直接显示“未被受理：<原因>”。
            return self._receipt(token, 'rejected', False, refusal,
                                 self._read_instance(instance))
        return self._submit_goal(UserGoal(
            wanted=WANT_RUNNING, mode=frozen['mode'],
            selection=selection,
            start_kind=START_NEW),
            token, instance)

    def _preflight_start(self, instance: Any, mode: str, selection: Any) -> str:
        """提交 Goal 前的只读分区预检；返回非空字符串表示拒绝原因。

        端口未提供预检能力（旧夹具/隔离构造）时放行；预检自身异常也放行——
        选场/开播端口仍会在本地拒绝空分区，绝不让预检 bug 挡住正常开播。
        """
        port = getattr(instance, 'task_port', None)
        checker = getattr(port, 'preflight_start', None)
        if not callable(checker):
            return ''
        try:
            reason = checker(mode, selection)
        except Exception:                                 # noqa: BLE001
            logger.warning('开播前分区预检失败（按放行处理）', exc_info=True)
            return ''
        return reason if isinstance(reason, str) else ''

    def _stop(self, token: str,
              account_context: Optional[dict]) -> ControlReceipt:
        instance = self._require_instance(token)
        self._check_account_context(instance, account_context)
        snapshot = self._snapshot_copy(instance)
        goal = (snapshot or {}).get('goal') or {}
        if goal.get('wanted') == WANT_STOPPED:
            # 已经持久为停止/退出中：直接给出这份事实，不再排队第二条停止命令，
            # 也不等待关房收尾（复核 R4）。
            return self._receipt(token, 'accepted', True,
                                 '当前已是停止状态', snapshot)
        selection = Selection.from_dict(goal.get('selection') or {})
        return self._submit_goal(UserGoal(
            wanted=WANT_STOPPED, mode=goal.get('mode') or MODE_TASK,
            selection=selection, start_kind=START_NEW,
            stop_reason=STOP_USER), token, instance)

    def _resume(self, token: str, run_id: Optional[str],
                goal_revision: Optional[int],
                account_context: Optional[dict]) -> ControlReceipt:
        instance = self._require_instance(token)
        self._check_account_context(instance, account_context)
        if run_id is None or goal_revision is None:
            raise ControlServiceError('恢复请求缺少原 Run/Goal 冻结上下文')
        snapshot = self._snapshot_copy(instance) or {}
        run = snapshot.get('run') or {}
        goal = snapshot.get('goal') or {}
        if not run:
            return self._receipt(token, 'rejected', False, '没有可恢复的原场',
                                 snapshot)
        if (str(run.get('id')) != str(run_id)
                or not self._same_revision(goal.get('revision'), goal_revision)):
            # 用户看到的是另一场/另一版本：不沿当前快照续播。
            return self._receipt(token, 'rejected', False,
                                 '原场已变化，请刷新后重试', snapshot)
        # 恢复仍是**原场**（run/task/业务日/目标/进度全保留），但推流方式只由
        # 设置页决定：沿用旧选择其余字段，来源换成当前设置值。旧场曾被错误保存
        # 成 OBS 的，改设置后恢复必须以新方式继续（Core 端同一保存里同步本场来源）。
        selection = self._with_current_push_source(
            Selection.from_dict(goal.get('selection') or {}))
        return self._submit_goal(UserGoal(
            wanted=WANT_RUNNING, mode=goal.get('mode') or MODE_TASK,
            selection=selection, start_kind=START_RESUME,
            # 把调用方冻结的原场身份原样带进 actor：真正的条件核对在 Core 保存新
            # Goal 之前完成，服务入口预检不能代替它（复核 R1）。
            expected_run_id=str(run_id),
            expected_goal_revision=int(goal_revision)), token, instance)

    def _confirm(self, token: str, goal_revision: Optional[int],
                 uid: Optional[int], room_id: Optional[int]) -> ControlReceipt:
        instance = self._require_instance(token)
        if goal_revision is None or uid is None or room_id is None:
            raise ControlServiceError('人工确认缺少原 Goal/账号冻结上下文')
        try:
            revision = int(goal_revision)
            frozen_uid = int(uid)
            frozen_room = int(room_id)
        except (TypeError, ValueError):
            raise ControlServiceError('人工确认的冻结上下文非法')
        result = self._submit_to(instance, UserConfirmation(
            goal_revision=revision, uid=frozen_uid, room_id=frozen_room))
        return self._result_receipt(token, result, instance)

    def _abandon(self, token: str, run_id: Optional[str],
                 goal_revision: Optional[int],
                 selection: Optional[dict]) -> ControlReceipt:
        instance = self._require_instance(token)
        if run_id is None or goal_revision is None or selection is None:
            raise ControlServiceError('放弃请求缺少原 Run/Goal/选择冻结上下文')
        snapshot = self._snapshot_copy(instance) or {}
        goal = snapshot.get('goal') or {}
        return self._submit_goal(UserGoal(
            wanted=WANT_STOPPED, mode=goal.get('mode') or MODE_TASK,
            selection=Selection.from_dict(goal.get('selection') or {}),
            start_kind=START_NEW, stop_reason=STOP_USER,
            abandon_run_id=str(run_id),
            abandon_goal_revision=int(goal_revision),
            abandon_selection=copy.deepcopy(selection)), token, instance)

    def _restart_after_abandon(self, token: str, frozen: dict,
                               run_id: Any, goal_revision: Any,
                               selection: dict) -> ControlReceipt:
        instance = self._require_instance(token)
        snapshot = self._snapshot_copy(instance) or {}
        run = snapshot.get('run') or {}
        if not run:
            return self._receipt(token, 'rejected', False,
                                 '没有需要放弃的原场', snapshot)
        next_selection = self._with_current_push_source(frozen['selection'])
        refusal = self._preflight_start(instance, frozen['mode'], next_selection)
        if refusal:
            return self._receipt(token, 'rejected', False, refusal, snapshot)
        return self._submit_goal(UserGoal(
            wanted=WANT_RUNNING, mode=frozen['mode'],
            selection=next_selection,
            start_kind=START_NEW,
            abandon_run_id=str(run_id),
            abandon_goal_revision=int(goal_revision),
            abandon_selection=copy.deepcopy(selection)), token, instance)

    def _logout(self, token: str, next_account: Any,
                credentials: Optional[Dict[str, str]], timeout: float,
                account_context: Optional[dict]) -> ControlReceipt:
        # 一份绝对截止：条件等待与资源关闭共用，收尾用掉的时间不在 close 重新获得。
        deadline = time.monotonic() + max(0.1, float(timeout))
        instance = self._require_instance(token)
        self._check_account_context(instance, account_context)
        snapshot = self._snapshot_copy(instance) or {}
        goal = snapshot.get('goal') or {}
        result = self._submit_to(instance, UserGoal(
            wanted=WANT_STOPPED, mode=goal.get('mode') or MODE_TASK,
            selection=Selection.from_dict(goal.get('selection') or {}),
            start_kind=START_NEW, stop_reason=STOP_ACCOUNT_EXIT))
        if not isinstance(result, Accepted):
            return self._result_receipt(token, result, instance)

        wait_for = getattr(instance, 'wait_for_snapshot', None)
        if wait_for is None and getattr(instance, 'runtime', None) is not None:
            wait_for = instance.runtime.wait_for_snapshot
        if wait_for is None:
            return self._receipt(token, 'pending', False,
                                 '执行器没有状态变更等待接口',
                                 self._snapshot_copy(instance))
        settled = wait_for(
            self._account_exit_settled, max(0.0, deadline - time.monotonic()))
        if settled is None:
            return self._receipt(token, 'pending', False,
                                 '旧账号仍在收尾，凭据保持不变',
                                 self._snapshot_copy(instance))

        # 资源关闭用**这份服务自己的关闭预算**（与候选释放门同一个值），而不是把
        # 整个移交截止都给它：``timeout`` 是"等旧账号收尾"的预算，而"等已在途账号
        # 工作收齐"必须与其它出口同一条有界预算。两者取小，因此绝对截止仍然成立
        # （收尾用掉的时间不会在 close 重新获得），但关闭不会把移交预算整个吃掉——
        # 吃掉之后"未收齐"就永远来不及被如实报告成"保留真实所有者"（复核 R2）。
        report = instance.close(min(self._close_budget,
                                    max(0.0, deadline - time.monotonic())))
        if not self._close_verified_clean(report):
            # 关闭未核验：保留旧实例引用与真实报告，不安装新账号、不丢所有者。
            return self._receipt(token, 'rejected', False,
                                 '旧账号资源关闭未通过核验，保留原所有者', settled)

        replacement = None
        if next_account is not None:
            if self._instance_factory is None or not isinstance(credentials, dict):
                return self._receipt(token, 'rejected', False,
                                     '缺少新账号实例工厂或凭据', settled)
            try:
                replacement = self._instance_factory(next_account,
                                                     dict(credentials))
            except Exception as exc:  # noqa: BLE001
                return self._receipt(token, 'rejected', False,
                                     f'新账号实例创建失败：{type(exc).__name__}',
                                     settled)
        with self._lock:
            if self._instance is instance:
                self._instance = replacement
                self._generation += 1
        return self._receipt(token, 'accepted', True, '',
                             self._read_instance(replacement))

    def _login(self, token: str, account: Any, credentials: Dict[str, str],
               still_valid: Optional[Callable[[], bool]] = None) -> ControlReceipt:
        blocked = self._unreleased_blocker()
        if blocked:
            # 上一份候选**没有收齐资源**：它仍归本服务所有，且无法用缓存报告证明
            # 后续清理。此刻在**调用工厂之前**就暂拒新的安装，绝不创建第二份相同
            # 目录的运行实例、不写新凭据；read/health 不受影响（复核 R2 / 验收 B2）。
            return self._receipt(token, 'rejected', False, blocked)
        if self._instance is not None:
            return self._receipt(token, 'rejected', False,
                                 '当前账号仍在使用，请先完成退出')
        if self._instance_factory is None or not isinstance(credentials, dict):
            return self._receipt(token, 'rejected', False,
                                 '缺少账号实例工厂或凭据')
        try:
            replacement = self._instance_factory(account, dict(credentials))
        except Exception as exc:  # noqa: BLE001
            return self._receipt(token, 'rejected', False,
                                 f'新账号实例创建失败：{type(exc).__name__}')
        if still_valid is not None and not self._candidate_still_valid(still_valid):
            # 候选身份在**慢构造之后**已经失效（同一位置、同一份判定）：绝不安装
            # 一个已被取代的候选，也绝不把它的凭据写进本实例；已经取得的资源沿
            # 既有 close 门释放，不放任 actor/观察线程成为孤儿（复核 R1）。
            note = self._dispose_replacement(replacement)
            return self._receipt(
                token, 'stale_generation', False,
                '这次扫码已被更新的二维码取代，未安装账号、未写入凭据' + note)
        with self._lock:
            if self._instance is not None:
                # 并发登录已安装：不覆盖，也不丢已建好的实例所有者。
                note = self._dispose_replacement(replacement)
                return self._receipt(token, 'rejected', False,
                                     '当前账号仍在使用，请先完成退出' + note)
            self._instance = replacement
            self._generation += 1
        return self._receipt(token, 'accepted', True, '',
                             self._read_instance(replacement))

    @staticmethod
    def _candidate_still_valid(still_valid: Callable[[], bool]) -> bool:
        """候选身份判定：判定本身出错按**失效**处理（宁可拒绝安装，不放行未知）。"""
        try:
            return bool(still_valid())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------- 未安装候选的资源归属

    def dispose_replacement(self, replacement: Any) -> str:
        """**共同释放入口**：把一份"已取得、但未能安装"的实例交回既有释放门。

        实例**一经创建**就已经拥有真实的 actor、观察线程、Job 与媒体读者；它后来
        是否失败只说明"不能作为账号安装"，不说明"已经释放干净"。因此工厂一侧在
        **启动失败出口**上也必须经这里移交，否则那份实例会随调用栈返回而丢失所有者，
        本服务的未释放候选保留与下一次登录门都接不到资源（F1）。

        返回的是给回执/错误列表用的说明文字；判据与 ``_login`` / ``_logout`` 内部
        完全同源（同一条 ``_dispose_replacement``）：收齐则释放，未收齐则保留真实
        实例与**首份**真实报告为资源所有者。``close`` 幂等，重复交回返回既有报告，
        不会把首份不干净的报告洗成干净。
        """
        return self._dispose_replacement(replacement)

    def _dispose_replacement(self, replacement: Any) -> str:
        """释放一份**未能安装**的候选，并把结果如实写进回执原因。

        释放不成立时**保留引用与真实报告**（``_retain_unreleased``）：调用方返回后
        资源仍必须有所有者，不能让"下一份合法扫码"在同一目录上再建一份运行实例
        （复核 R2）。返回的是给回执用的说明文字，释放是否成立由保留动作决定。
        """
        if replacement is None:
            return ''
        released, report, note = self._release_replacement(replacement)
        if released:
            return note
        return self._retain_unreleased(replacement, report, note)

    def _release_replacement(self, replacement: Any) -> Tuple[bool, Any, str]:
        """按既有 ``close`` 门释放候选；返回 ``(是否收齐, 真实报告, 说明)``。

        三种失败入口**复用同一条规则**：``close`` 抛错、没有可信关闭报告（拿不到
        ``clean()`` 钩子）、报告核验不干净。任何一种是 False 都表示资源**仍归本服务
        所有**，绝不能丢引用（验收 B1/B6）。
        """
        if replacement is None:
            return True, None, ''
        closer = getattr(replacement, 'close', None)
        if closer is None:
            return False, None, '；候选实例没有关闭门，未确认已释放'
        try:
            report = closer(self._close_budget)
        except Exception as exc:  # noqa: BLE001
            return False, None, f'；候选实例释放异常（{type(exc).__name__}）'
        if not self._close_verified_clean(report):
            return False, report, '；候选实例释放未通过核验'
        return True, report, ''

    def _retain_unreleased(self, replacement: Any, report: Any, note: str) -> str:
        """保留未收齐的候选资源与**真实报告**；它仍是"未认证安装"，不是已登录账号。"""
        with self._lock:
            self._unreleased.append((replacement, report))
        return (note + '；候选实例已保留为资源所有者（真实报告一并保留），'
                       '未收齐前不接受新的账号安装；重启服务后重试')

    def _unreleased_blocker(self) -> str:
        """存在未收齐资源时给新登录的拒绝说明；没有则返回空串。"""
        with self._lock:
            count = len(self._unreleased)
        if not count:
            return ''
        return (f'上一份候选实例的资源尚未收齐（{count} 份），'
                '暂不接受新的账号安装：资源与真实报告已保留，重启服务后重试')

    def close_unreleased(self, budget: Optional[float] = None) -> dict:
        """关闭门兜底：应用关闭时把未收齐的候选交回**既有** ``close`` 门。

        不改变"是否干净"的判定，也不把它安装成账号；只保证进程退出前不再持有孤儿
        actor/观察线程。已由调用方关闭过的实例返回既有缓存报告（``close`` 幂等）。
        """
        with self._lock:
            pending, self._unreleased = self._unreleased, []
        reports: List[dict] = []
        for instance, _report in pending:
            closer = getattr(instance, 'close', None)
            if closer is None:
                reports.append({'released': False, 'reason': 'no_close'})
                continue
            try:
                report = closer(self._close_budget if budget is None
                                else max(0.0, float(budget)))
            except Exception as exc:  # noqa: BLE001
                reports.append({'released': False,
                                'reason': f'close:{type(exc).__name__}'})
                continue
            reports.append({'released': self._close_verified_clean(report)})
        return {'count': len(pending), 'reports': reports}


    # --------------------------------------------------------------- helpers

    def _submit_goal(self, event: UserGoal, token: str,
                     instance: Any) -> ControlReceipt:
        return self._result_receipt(token, self._submit_to(instance, event),
                                    instance)

    def _submit_to(self, instance: Any, event: Any) -> Any:
        harness_send = getattr(instance, 'send', None)
        if harness_send is not None:
            return harness_send(event, timeout=self._submit_timeout)
        submitted = instance.submit(event, timeout=self._submit_timeout)
        if hasattr(submitted, 'result'):
            return submitted.result(self._submit_timeout)
        return submitted

    def _result_receipt(self, token: str, result: Any,
                        instance: Any) -> ControlReceipt:
        projection = self._read_instance(instance)
        if isinstance(result, Accepted):
            return self._receipt(token, 'accepted', True, '', projection)
        if isinstance(result, Ignored):
            return self._receipt(token, 'ignored', False,
                                 getattr(result, 'reason', ''), projection)
        if isinstance(result, Rejected):
            return self._receipt(token, 'rejected', False,
                                 getattr(result, 'reason', ''), projection)
        return self._receipt(token, 'rejected', False,
                             f'未知 Core 回执：{type(result).__name__}', projection)

    def _receipt(self, token: str, status: str, accepted: bool,
                 reason: str, projection: Optional[dict] = None
                 ) -> ControlReceipt:
        return ControlReceipt(token, status, accepted, reason,
                              copy.deepcopy(projection))

    def _read_instance(self, instance: Any) -> Optional[dict]:
        if instance is None:
            return None
        snapshot = self._snapshot_copy(instance)
        return copy.deepcopy(snapshot) if snapshot is not None else None

    @staticmethod
    def _snapshot_copy(instance: Any) -> Optional[dict]:
        reader = getattr(instance, 'snapshot_copy', None)
        if reader is not None:
            return reader()
        runtime = getattr(instance, 'runtime', None)
        if runtime is not None:
            return runtime.snapshot_copy()
        reader = getattr(instance, 'snapshot', None)
        return reader() if reader is not None else None

    @staticmethod
    def _account_exit_settled(snapshot: Optional[dict]) -> bool:
        if not isinstance(snapshot, dict):
            return False
        goal = snapshot.get('goal') or {}
        return (goal.get('wanted') == WANT_STOPPED
                and goal.get('stop_reason') == STOP_ACCOUNT_EXIT
                and snapshot.get('step') == 'idle'
                and snapshot.get('run') is None)

    @staticmethod
    def _close_verified_clean(report: Any) -> bool:
        if report is None:
            return False
        clean = getattr(report, 'clean', None)
        if not callable(clean):
            # 报告没有核验钩子：无法证明资源已收齐，不假装干净。
            return False
        try:
            return bool(clean())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _same_revision(actual: Any, expected: Any) -> bool:
        try:
            return int(actual) == int(expected)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _check_account_context(instance: Any, context: Optional[dict]) -> None:
        """请求携带的冻结账号上下文必须与当前实例账号一致。

        调用方不携带（``None``）时保持"最新意图入口"语义；一旦携带就必须匹配，
        否则旧页面/旧链接不会被服务补成新账号的有效命令。
        """
        if context is None:
            return
        if not isinstance(context, dict):
            raise ControlServiceError('账号上下文格式无效')
        account = getattr(instance, 'account', None)
        uid = context.get('uid')
        room_id = context.get('room_id')
        if account is None or uid is None or room_id is None:
            raise ControlServiceError('账号上下文不完整')
        try:
            matches = (int(uid) == int(account.uid)
                       and int(room_id) == int(account.room_id))
        except (TypeError, ValueError):
            raise ControlServiceError('账号上下文格式无效')
        if not matches:
            raise ControlServiceError('请求的账号上下文与当前账号不一致')

    def _canonical_selection(self, selection: Any) -> Optional[dict]:
        if selection is None:
            return None
        if isinstance(selection, Selection):
            return selection.to_dict()
        if isinstance(selection, dict):
            return copy.deepcopy(selection)
        raise ControlServiceError('原选择快照无效')

    def _require_instance(self, token: str) -> Any:
        instance = self._instance
        if instance is None:
            raise ControlServiceError('当前没有已登录账号')
        unsupported = getattr(getattr(instance, 'runtime', None),
                              'unsupported_reason', '')
        if unsupported:
            raise ControlServiceError(str(unsupported))
        return instance

    def _with_token(self, operation_token: str, action: str, request: dict,
                    operation: Callable[[str], ControlReceipt], *,
                    account: Optional[dict] = None,
                    wait_seconds: Optional[float] = None) -> ControlReceipt:
        """短锁领取一次执行权，锁外执行，锁内发布回执。

        - 同 token 同内容：回原回执，**不再**提交（旧 token 绝不重放到新账号）；
        - 同 token 不同内容：``token_conflict``；
        - 并发重复：后来者等待首个执行者发布回执，不排队再次执行平台命令。
        """
        token = str(operation_token or '').strip()
        if not token or len(token) > self._MAX_TOKEN_LENGTH:
            raise ControlServiceError('operation_token 缺失或长度无效')
        fingerprint = hashlib.sha256(self._canonical({
            'action': action, 'request': request, 'account': account,
        }).encode('utf-8')).hexdigest()
        wait = self._submit_timeout if wait_seconds is None else wait_seconds

        while True:
            with self._lock:
                entry = self._receipts.get(token)
                if entry is None:
                    entry = _TokenClaim(fingerprint, self._action_kind(action))
                    self._receipts[token] = entry
                    self._trim_receipts()
                    claimed = True
                elif entry.fingerprint != fingerprint:
                    return self._receipt(token, 'token_conflict', False,
                                         '同一操作令牌已用于不同请求')
                elif entry.receipt is not None:
                    self._receipts.move_to_end(token)
                    return copy.deepcopy(entry.receipt)
                else:
                    claimed = False
                if claimed:
                    # 在**同一短锁内**领取执行权：账号移交与运行意图互斥。
                    refusal = self._claim_policy(action, token)
                    if refusal is not None:
                        # 没取得执行权：不留缓存，允许稍后重试。
                        if self._receipts.get(token) is entry:
                            del self._receipts[token]
                        return refusal
            if claimed:
                break
            if not entry.done.wait(wait):
                return self._receipt(token, 'pending', False,
                                     '同一操作的另一个请求仍在处理中')
            with self._lock:
                if entry.receipt is not None:
                    return copy.deepcopy(entry.receipt)

        try:
            receipt = operation(token)
        except BaseException:
            with self._lock:
                if self._receipts.get(token) is entry:
                    del self._receipts[token]
            raise
        with self._lock:
            entry.receipt = copy.deepcopy(receipt)
            entry.done.set()
            self._receipts[token] = entry
            self._receipts.move_to_end(token)
            self._trim_receipts()
        return copy.deepcopy(receipt)

    def _claim_policy(self, action: str, token: str) -> Optional[ControlReceipt]:
        """短锁内的执行权领取；调用方必须持 ``_lock``。

        账号移交（login/logout）与运行输入（start/resume/confirm/abandon/
        restart_after_abandon）**双向**互斥：任一类仍有**尚未完成**的领取时，另一类
        的新领取都明确暂拒、不留缓存（稍后重试即可）。两个移交之间也互斥。运行输入
        彼此不互斥 —— 它们仍由 Core 按用户意图唯一决策。Stop/read 不在此领取，随时可达。

        分类来自现有 token 领取记录，不新建动作队列/计数器/锁层。
        """
        kind = self._action_kind(action)
        if kind is None:
            return None
        for other_token, entry in self._receipts.items():
            if other_token == token or entry.receipt is not None:
                continue  # 只看**尚未完成投递**的其他领取
            if entry.kind == 'handoff':
                return self._receipt(token, 'rejected', False,
                                     '账号正在移交，请稍后重试')
            if kind == 'handoff' and entry.kind == 'running':
                return self._receipt(token, 'rejected', False,
                                     '另一个开播/停止操作仍在处理中，请稍后重试')
        return None

    def _action_kind(self, action: str) -> Optional[str]:
        """这次操作属于哪一类互斥执行权；``None`` 表示不参与互斥领取。"""
        if action in self._HANDOFF_ACTIONS:
            return 'handoff'
        if action in self._RUNNING_ACTIONS:
            return 'running'
        return None

    def _trim_receipts(self) -> None:
        # 只裁剪**已发布回执**的旧记录；在途领取（尚未发布回执）必须保留，
        # 否则双向互斥会失去依据。按插入顺序从最旧开始裁。
        for token in list(self._receipts):
            if len(self._receipts) <= self._MAX_RECEIPTS:
                break
            if self._receipts[token].receipt is not None:
                del self._receipts[token]

    @staticmethod
    def _canonical(value: Any) -> str:
        if isinstance(value, Selection):
            value = value.to_dict()
        return json.dumps(value, sort_keys=True, ensure_ascii=True,
                          separators=(',', ':'), default=str)
