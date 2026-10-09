"""阶段 4B：已确认事实 → 既有通知入口的**窄**适配器。

它只做三件事，别的一概不做：

1. **只在已确认转换上产生通知**：输入是 :class:`~app.core.session.effects.ConfirmedFact`
   （由唯一决策点在其确认出口形成并冻结身份/时间），不是 ``Runtime.subscribe`` 的
   latest-only 快照。快速 End→账本→下一场不会因为只留最后一张快照而丢掉原事实。
2. **冻结身份与时间**：正文里的用户名、run/业务日、事件时刻全部取自事实本身；
   投递时**不**回读当前账号或当前 Run，晚投递也不会被新账号改写。
3. **复用既有发送系统**：仍然走 ``EmailSender`` 的既有格式、队列、去重、限频与
   渠道选择；本模块不重写发送、不建事件总线、不建持久通知账本、不建第二份运行
   状态机。

唯一的一份暂存状态是"轮转时 A 的通知资料等 B 的结果"（见 :meth:`_merge_rotation`）：
它只冻结这一次通知载荷，不参与任何直播决策，进程退出即丢弃。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..session import effects as fx

logger = logging.getLogger(__name__)


def _frozen_when(text: Any) -> Optional[datetime]:
    """把事实里冻结的秒级时刻还原成既有邮件入口要的 ``datetime``。

    ``ConfirmedFact.happened_at`` / ``completed_at`` 是**冻结的 ISO 文本**（事实
    形成那一刻的业务时钟），而既有 ``EmailSender`` 的时间参数是 ``datetime``。
    直接把字符串传进去会在真实发送器里抛 ``AttributeError`` —— 记录式替身看不出来
    （复核 D4/E04-e 的形态）。解析不出来时返回 ``None``，让邮件退回"此刻"，绝不
    编造一个看起来像事实的假时刻。
    """
    if isinstance(text, datetime):
        return text
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        logger.warning('已确认事实的时刻无法解析，邮件改用当前时刻：%r', text)
        return None

#: 去重表上界：只是"同一事实不要重复投递"的技术上界，不是业务状态。
_MAX_SEEN = 512

#: 展示缓冲上界（阶段 4C）：只服务于面板"最近后台日志"的**有界只读**窗口。
#: 它不是事件账本、不落盘、不参与任何决策；进程退出即丢弃。
_MAX_DISPLAY_EVENTS = 64

#: 哪些投递决定算"用户可见的已确认事实"。
#:
#: ``duplicate`` 是**同一事实**的重复投递（``_already`` 命中），进缓冲就会让
#: 面板把同一件事画两遍——F04-e 的"重复事件一次"正是靠这里的排除实现。
#: ``silent``/``pending``/``adapter_error`` 都不是用户可见事实：前者不产生通知，
#: 中间那个只是轮转合并的暂存态，最后一个由日志如实记录。
_DISPLAY_DECISIONS = frozenset(('sent', 'merged', 'settled'))

#: 顶部显示标签（沿用既有通知类别，见 PHASE4B-API-CONTRACT §6）。
_TAG_START = '开播'
_TAG_RESUME = '恢复'
_TAG_ROTATION = '轮转'
_TAG_STOPPED = '停止'
_TAG_FACE = '人脸'
_TAG_ANOMALY = '异常'


def _display_record(boot_id: str, seq: int, entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条事实记录翻成**面板可直接渲染**的只读条目。

    只翻译既有字段，不新增判断：标签、类型与文案全部由 ``kind``/``result``/
    ``decision`` 决定，时间取事实冻结的 ``happened_at``（不用投递时刻）。
    文案里的分区/执行日缺失时**不编造**，直接把该片段省掉。

    ``goal_revision`` 与 ``reason`` 是事实**本来就已经冻结**的身份/载荷字段
    （``ConfirmedFact.goal_revision`` / ``.reason``），这里只做透传：人脸出口要
    按 Goal 版本过滤、并把本次的验证原因/链接如实展示，缺了它们投影就只能退回
    "验证链接未取得"（复核 R4）。透传不新增判断，也不改变任何投递决定。
    """
    tag, kind_type, message = _describe(entry)
    return {
        'id': f'{boot_id}:{seq}',
        'boot_id': boot_id,
        'seq': seq,
        'tag': tag,
        'type': kind_type,
        'message': message,
        'time': str(entry.get('happened_at') or ''),
        'uid': int(entry.get('uid') or 0),
        'room_id': int(entry.get('room_id') or 0),
        'kind': str(entry.get('kind') or ''),
        'result': str(entry.get('result') or ''),
        'run_id': str(entry.get('run_id') or ''),
        'zone_name': str(entry.get('zone') or ''),
        'business_day': str(entry.get('business_day') or ''),
        'goal_revision': int(entry.get('goal_revision') or 0),
        'reason': str(entry.get('reason') or ''),
    }


def _describe(entry: Dict[str, Any]) -> tuple:
    """``(tag, type, message)``：一条事实记录的可读说法。"""
    kind = str(entry.get('kind') or '')
    result = str(entry.get('result') or '')
    decision = str(entry.get('decision') or '')
    zone = str(entry.get('zone') or '')
    day = str(entry.get('business_day') or '')

    if decision == 'settled':
        return (_TAG_ROTATION, 'info',
                _with_zone(zone, '的轮转未成对，已按完成事实单独结清'))

    if kind == fx.FACT_SESSION_STOPPED:
        return (_TAG_STOPPED, 'info', _with_zone(zone, '已确认停止'))

    if kind == fx.FACT_RUN_COMPLETED:
        if result == fx.FACT_RESULT_SETTLE_FAILED:
            return (_TAG_ANOMALY, 'warning',
                    _with_zone(zone, '的任务完成未能记账，将按既有重试处理'))
        return (_TAG_ROTATION, 'success',
                _with_zone(zone, f'已完成 {day} 的场次并记账' if day else '的场次已记账'))

    if kind == fx.FACT_SESSION_OPEN:
        if decision == 'merged':
            if result == fx.FACT_RESULT_STARTED:
                return (_TAG_ROTATION, 'success', _with_zone(zone, '已接续开播'))
            if result == fx.FACT_RESULT_RESUMED:
                return (_TAG_ROTATION, 'success', _with_zone(zone, '已接续恢复'))
            if result == fx.FACT_RESULT_FACE_VERIFY:
                return (_TAG_ROTATION, 'danger',
                        _with_zone(zone, '需要人脸验证，本轮已暂停'))
            if result == fx.FACT_RESULT_NONE:
                return (_TAG_ROTATION, 'info', '今天没有可继续执行的场次')
            return (_TAG_ROTATION, 'warning', _with_zone(zone, '未能开播，本轮已结束'))
        if result == fx.FACT_RESULT_STARTED:
            return (_TAG_START, 'success', _with_zone(zone, '已由平台确认开播'))
        if result == fx.FACT_RESULT_RESUMED:
            return (_TAG_RESUME, 'success', _with_zone(zone, '已由平台确认恢复在播'))
        if result == fx.FACT_RESULT_FACE_VERIFY:
            return (_TAG_FACE, 'danger', _with_zone(zone, '开播需要人脸验证'))
        if result == fx.FACT_RESULT_REJECTED:
            return (_TAG_ANOMALY, 'warning', _with_zone(zone, '开播被平台拒绝'))

    return (_TAG_ANOMALY, 'info', _with_zone(zone, '后台有新的已确认事实'))


def _with_zone(zone: str, tail: str) -> str:
    return f'{zone} {tail}' if zone else tail

#: ``EmailSender.notify_task_rotation`` 的 ``next_result`` 取值（既有约定）。
_NEXT_STARTED = 'started'
_NEXT_FAILED = 'failed'
_NEXT_VERIFICATION = 'verification'
_NEXT_NONE = 'none'
_NEXT_CANCELLED = 'cancelled'


class NotificationAdapter:
    """把已确认事实翻译成既有邮件的**唯一**适配器（每个应用一份）。"""

    def __init__(self, sender: Any) -> None:
        self._sender = sender
        self._lock = threading.Lock()
        #: A 的冻结完成资料（等 B 的结果合并成一封）。不是业务状态，退出即丢。
        self._pending_rotation: Optional[Dict[str, Any]] = None
        #: 已投递过的事实指纹：同一事实（含重试回执）只投一次。
        self._seen: 'OrderedDict[str, bool]' = OrderedDict()
        #: 证据：每一次**投递决定**（供 4B 报告/E04 断言读取，不参与决策）。
        self.events: List[Dict[str, Any]] = []
        # ---- 阶段 4C：面板"最近后台日志"的**有界只读**展示缓冲 ----
        # 它与 ``events`` 是两件事：``events`` 是无界证据列表（4B 监督读它），
        # 这里是给人看的窗口，有上界、有稳定身份、有账号标签、可安全并发读。
        # 它不是第二份事件权威：数据全部来自同一条 ``_note`` 事实记录。
        self._boot_id = f'nf-{uuid.uuid4().hex[:12]}'
        self._display: 'deque[Dict[str, Any]]' = deque(maxlen=_MAX_DISPLAY_EVENTS)
        self._display_seq = 0

    # ---------------------------------------------------------------- 入口

    def enqueue_frozen(self, fact: fx.ConfirmedFact) -> str:
        """接收一条已确认事实。返回本次投递决定（``sent``/``merged``/``silent``…）。

        本方法**只入队**：真正投递由 ``EmailSender`` 自己的 worker 完成，因此
        它绝不会把 SMTP/网络等待带进 actor。
        """
        try:
            return self._handle(fact)
        except Exception as exc:                      # noqa: BLE001
            # 通知适配器自身的问题绝不影响直播推进；如实记录并保持沉默。
            logger.warning('已确认事实投递失败（不影响直播）：%s', exc)
            self._note(fact, 'adapter_error', str(exc))
            return 'adapter_error'

    def pending_rotation(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return None if self._pending_rotation is None \
                else dict(self._pending_rotation)

    # ------------------------------------------------- 阶段 4C：展示缓冲窄读口

    @property
    def boot_id(self) -> str:
        """本进程通知出口的身份前缀（前端用它给事件去重、跨重启换代）。"""
        return self._boot_id

    def recent_events(self) -> List[Dict[str, Any]]:
        """最近**已确认**的后台事实（有界、拷贝、锁内取值）。

        纯读：不触发通知、不发网络、不改任何状态。返回的是副本，调用方改它不会
        影响缓冲。账号隔离由调用方（投影）按 ``uid`` 过滤，这里原样给出标签。
        """
        with self._lock:
            return [dict(item) for item in self._display]

    def reset(self) -> None:
        """进程 Close：把还没成对的暂存资料**结清**，不落盘、不残留。

        "结清"是既有格式的一封 ``[轮转]``（A 的完成事实如实保留，只说明轮转已
        停止），而不是静默丢弃：完成事实一旦已经确认，就不能因为进程退出而无声
        消失（D3）。发送本身仍交给既有队列，本方法不做网络。
        """
        with self._lock:
            pending, self._pending_rotation = self._pending_rotation, None
        if pending is not None:
            try:
                self._settle_rotation(pending, _NEXT_CANCELLED,
                                      reason='进程关闭时轮转尚未成对')
            except Exception as exc:                  # noqa: BLE001
                logger.warning('关闭时结清暂存完成资料失败（不影响关闭）：%s', exc)

    # ---------------------------------------------------------------- 分派

    def _handle(self, fact: fx.ConfirmedFact) -> str:
        if fact.kind == fx.FACT_SESSION_OPEN:
            return self._on_session_open(fact)
        if fact.kind == fx.FACT_RUN_COMPLETED:
            return self._on_run_completed(fact)
        if fact.kind == fx.FACT_SESSION_STOPPED:
            return self._on_session_stopped(fact)
        # 不结算的结束决定（午夜丢弃 / 运行中被替换）不产生邮件。
        return self._note(fact, 'silent')

    # ---------- 开播/恢复/无任务/被拒 ----------

    def _on_session_open(self, fact: fx.ConfirmedFact) -> str:
        if fact.result == fx.FACT_RESULT_NONE:
            # "今天没有合格场次"只有在**紧接一场已完成之后**才是轮转结论；
            # 用户新开一场却无任务时保持运行等待，不发过程信。
            pending = self._take_pending_for(fact)
            if pending is None:
                return self._note(fact, 'silent')
            return self._merge_rotation(fact, _NEXT_NONE, pending)

        if fact.result in (fx.FACT_RESULT_STARTED, fx.FACT_RESULT_RESUMED):
            # 去重键带上这次转换的 goal 版本：同一版本的重试回执只投一次；而
            # "用户停→恢复→再停"这类**同一 Run 的新转换**每次都会推进版本，
            # 各自成信，不被误当成重复丢弃（复核：恢复/停止只发了第一封）。
            if self._already(fact, fact.run_id, fact.result,
                             fact.goal_revision):
                return self._note(fact, 'duplicate')
            pending = self._take_pending_for(fact)
            if pending is not None:
                return self._merge_rotation(fact, _NEXT_STARTED, pending)
            return self._send_running(fact)

        if fact.result == fx.FACT_RESULT_FACE_VERIFY:
            if self._already(fact, fact.run_id, 'face_verify', fact.goal_revision):
                return self._note(fact, 'duplicate')
            pending = self._take_pending_for(fact)
            if pending is not None:
                return self._merge_rotation(fact, _NEXT_VERIFICATION, pending)
            return self._send_face(fact)

        if fact.result == fx.FACT_RESULT_REJECTED:
            if self._already(fact, fact.run_id, 'rejected', fact.goal_revision):
                return self._note(fact, 'duplicate')
            pending = self._take_pending_for(fact)
            if pending is not None:
                return self._merge_rotation(fact, _NEXT_FAILED, pending)
            if fact.run_id:
                self._sender.notify_start_failed(
                    fact.zone_name, identity=fact.run_id,
                    when=_frozen_when(fact.happened_at),
                    account_name=fact.account_name)
            return self._note(fact, 'sent')
        if fact.result == fx.FACT_RESULT_FAILED:
            if self._already(fact, fact.run_id, 'failed', fact.goal_revision):
                return self._note(fact, 'duplicate')
            pending = self._take_pending_for(fact)
            if pending is not None:
                return self._merge_rotation(fact, _NEXT_FAILED, pending)
            return self._note(fact, 'silent')
        return self._note(fact, 'silent')

    def _send_running(self, fact: fx.ConfirmedFact) -> str:
        self._sender.notify_stream_running(
            fact.zone_name,
            source=('resume' if fact.result == fx.FACT_RESULT_RESUMED else 'new'),
            room_id=fact.room_id or None,
            run_id=fact.run_id,
            when=_frozen_when(fact.happened_at),
            elapsed_seconds=fact.effective_seconds,
            duration_seconds=fact.target_seconds,
            account_name=fact.account_name)
        return self._note(fact, 'sent')

    def _send_face(self, fact: fx.ConfirmedFact) -> str:
        """人脸：使用**冻结 UID** 的既有模板；回包没有合法 URL 时如实写"未取得"。

        登录二维码地址绝不冒充人脸地址。令牌随事实冻结
        ``goal_revision``/``uid``/``room_id``，旧停止/退出/新目标后由唯一决策点
        （``UserConfirmation`` 身份核对）作废。
        """
        self._sender.send_face_verify(
            fact.reason or '',
            run_id=fact.run_id, room_id=fact.room_id or None,
            uid=int(fact.uid), goal_revision=int(fact.goal_revision),
            zone=fact.zone_name, stage='start_blocked',
            when=_frozen_when(fact.happened_at),
            account_name=fact.account_name)
        return self._note(fact, 'sent')

    # ---------- 完成/停止 ----------

    def _on_run_completed(self, fact: fx.ConfirmedFact) -> str:
        if self._already(fact, fact.run_id, 'ledger', fact.result):
            return self._note(fact, 'duplicate')
        if fact.result == fx.FACT_RESULT_SETTLE_FAILED:
            # 结算失败：如实报一次异常，等既有重试；不冒充新完成。
            self._sender.notify_task_complete(
                fact.zone_name, fact.business_day, scope='today',
                result='failed', when=_frozen_when(fact.happened_at),
                effective_seconds=fact.effective_seconds,
                account_name=fact.account_name)
            return self._note(fact, 'sent')
        # 完成事实先**暂存**：正常轮转要把 A 的完成与 B 的实际结果合并成一封。
        with self._lock:
            self._pending_rotation = {
                'zone': fact.zone_name,
                'execution_date': fact.business_day,
                'effective_seconds': fact.effective_seconds,
                'target_seconds': fact.target_seconds,
                'days_done': fact.days_done,
                'all_done': fact.all_done,
                'result': fact.result,
                'completed_at': fact.completed_at,
                'happened_at': fact.happened_at,
                'account_name': fact.account_name,
                'run_id': fact.run_id,
                # 冻结身份随资料一起暂存：合并前要能判断"对面是不是同一个账号/房间"，
                # 否则旧 A 会被合并进无关的新账号（D3）。
                'uid': int(fact.uid),
                'room_id': int(fact.room_id),
            }
        return self._note(fact, 'pending')

    def _on_session_stopped(self, fact: fx.ConfirmedFact) -> str:
        # 与开播/恢复同理：按 goal 版本区分"同一次停止的重试"与"新一轮停止"。
        if self._already(fact, fact.run_id, 'stopped', fact.result,
                         fact.goal_revision):
            return self._note(fact, 'duplicate')
        # 用户停止是**独立结果**：不把暂存的 A 完成合并进停止邮件（那不是轮转）。
        self._sender.notify_stopped(
            fact.zone_name, stage='stopped',
            when=_frozen_when(fact.happened_at),
            elapsed_seconds=fact.effective_seconds,
            account_name=fact.account_name)
        # 但暂存的 A 完成必须**结清**：它不能留到下一次开播去污染一封无关的轮转信。
        pending = self._take_pending_for(fact, allow_mismatch=True)
        if pending is not None:
            self._settle_rotation(pending, _NEXT_CANCELLED,
                                  reason='用户停止，轮转未成对')
        return self._note(fact, 'sent')

    # ---------- 轮转合并 ----------

    def _merge_rotation(self, fact: fx.ConfirmedFact, next_result: str,
                        pending: Optional[Dict[str, Any]] = None) -> str:
        """A 的冻结完成资料 + B 的**实际**结果 → 一封既有格式的合并邮件。

        B 的结果未知时不发过程信（既有的 ``preparing`` 分支由调用方排除）。
        人脸结果走**既有** token 发行入口取确认链接，再合并进同一封（复核 R10）。
        """
        completion = dict(pending) if pending is not None else {}
        next_task: Dict[str, Any] = {}
        if next_result != _NEXT_NONE:
            next_task = {'zone': fact.zone_name,
                         'duration_seconds': fact.target_seconds}
        verify_url = ''
        confirm_url = ''
        if next_result == _NEXT_VERIFICATION:
            verify_url, confirm_url = self._issue_face_confirmation(fact)
        self._sender.notify_task_rotation(
            completion, next_task, next_result,
            verify_url=verify_url, confirm_url=confirm_url,
            identity=completion.get('run_id') or fact.run_id,
            when=_frozen_when(completion.get('happened_at')
                              or fact.happened_at),
            account_name=fact.account_name)
        return self._note(fact, 'merged')

    def _issue_face_confirmation(self, fact: fx.ConfirmedFact
                                 ) -> tuple:
        """沿既有发行口取一个**真实**确认入口；不额外发第二封人脸邮件。

        生产 ``EmailSender.send_face_verify(defer_notification=True)`` 正是
        "先发行 token 并返回链接、但不单独发信"的既有语义。旧替身没有这个参数时
        退化成"没有链接、请在面板点击"——既不硬置空，也不假装拿到了链接。
        """
        try:
            result = self._sender.send_face_verify(
                fact.reason or '', run_id=fact.run_id,
                room_id=fact.room_id or None, uid=int(fact.uid),
                goal_revision=int(fact.goal_revision), zone=fact.zone_name,
                stage='start_blocked', when=_frozen_when(fact.happened_at),
                account_name=fact.account_name, defer_notification=True)
        except TypeError:
            return '', ''
        if isinstance(result, dict):
            return (str(result.get('verify_url') or fact.reason or ''),
                    str(result.get('confirm_url') or ''))
        return '', ''

    def _settle_rotation(self, pending: Dict[str, Any], next_result: str,
                         reason: str = '') -> str:
        """把一份没能成对的完成资料单独结清（保留 A 的全部已确认事实）。"""
        self._sender.notify_task_rotation(
            dict(pending), {}, next_result, reason=reason,
            identity=pending.get('run_id') or '',
            when=_frozen_when(pending.get('happened_at')),
            account_name=pending.get('account_name') or '')
        self._append_event({
            'kind': fx.FACT_RUN_COMPLETED, 'result': pending.get('result', ''),
            'decision': 'settled', 'run_id': pending.get('run_id', ''),
            'zone': pending.get('zone', ''),
            'business_day': pending.get('execution_date', ''),
            'happened_at': pending.get('happened_at', ''),
            'uid': int(pending.get('uid') or 0),
            'room_id': int(pending.get('room_id') or 0),
            'account_name': pending.get('account_name', ''),
            'target_seconds': float(pending.get('target_seconds') or 0.0),
            'effective_seconds': float(pending.get('effective_seconds') or 0.0),
            'completed_at': pending.get('completed_at'),
            'detail': f'{next_result}:{reason}',
        })
        return 'settled'

    # ---------------------------------------------------------------- 小工具

    def _take_pending(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            pending, self._pending_rotation = self._pending_rotation, None
        return pending

    def _take_pending_for(self, fact: fx.ConfirmedFact,
                          allow_mismatch: bool = False
                          ) -> Optional[Dict[str, Any]]:
        """领取暂存的完成资料，并挡住"跨账号/跨房间合并"。

        身份不一致时先把旧资料**单独结清**（保留它自己的分区/秒数/完成时刻），
        返回 ``None`` 让当前事实走自己的路径：一份属于旧账号的完成事实绝不会被
        合并进新账号的轮转信（D3）。``allow_mismatch`` 用于"主动结清"的调用点。
        """
        pending = self._take_pending()
        if pending is None or allow_mismatch:
            return pending
        if not self._same_identity(pending, fact):
            self._settle_rotation(pending, _NEXT_CANCELLED,
                                  reason='完成事实属于另一个账号/房间')
            return None
        return pending

    @staticmethod
    def _same_identity(pending: Dict[str, Any],
                       fact: fx.ConfirmedFact) -> bool:
        """两边都给出身份且不一致时才判为不同：缺一边时不据此拒绝合并。"""
        for key, current in (('uid', int(fact.uid or 0)),
                             ('room_id', int(fact.room_id or 0))):
            frozen = int(pending.get(key) or 0)
            if frozen and current and frozen != current:
                return False
        return True

    def _already(self, fact: fx.ConfirmedFact, *parts: Any) -> bool:
        key = '|'.join(str(part) for part in parts)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return True
            self._seen[key] = True
            while len(self._seen) > _MAX_SEEN:
                self._seen.popitem(last=False)
        return False

    def _note(self, fact: fx.ConfirmedFact, decision: str,
              detail: str = '') -> str:
        self._append_event({
            'kind': fact.kind, 'result': fact.result, 'decision': decision,
            'run_id': fact.run_id, 'zone': fact.zone_name,
            'business_day': fact.business_day, 'happened_at': fact.happened_at,
            'uid': int(fact.uid), 'room_id': int(fact.room_id),
            'account_name': fact.account_name,
            'goal_revision': int(fact.goal_revision),
            'reason': fact.reason,
            'target_seconds': float(fact.target_seconds),
            'effective_seconds': float(fact.effective_seconds),
            'completed_at': fact.completed_at,
            'detail': detail,
        })
        return decision

    # ------------------------------------------------- 阶段 4C：事件记录（单一入口）

    def _append_event(self, entry: Dict[str, Any]) -> None:
        """一条事实记录同时进**证据列表**与**有界展示缓冲**。

        两条出口共用同一份数据，因此面板显示的内容不可能与 4B 监督读到的证据
        分叉。只有"用户可见的已确认事实"（``_DISPLAY_DECISIONS``）进展示缓冲；
        稳定身份在**锁内**分配，读者拿到的永远是完整记录。
        """
        self.events.append(entry)
        if entry.get('decision') not in _DISPLAY_DECISIONS:
            return
        with self._lock:
            self._display_seq += 1
            self._display.append(_display_record(
                self._boot_id, self._display_seq, entry))
