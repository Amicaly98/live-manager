"""有界单轮平台 HTTP 适配：一份预算、一份响应、三类结果。

它把已验收 Core 发出的平台效果变成**一次**真实请求，并只带回**事实**：

- ``Start``：服务端时间戳／版本／最终 POST 共用同一份剩余预算，不进入旧
  ``start_live`` 的无限重试；受理不等成功。
- ``Stop``：只表示"下播请求的结果"；平台是否真关由随后的 ``Query`` 判断。
- ``Query``：**同一份** ``get_info`` 响应里同时取状态与分区。

端口不决定轮转、不重试、不 sleep、不发信、不改 Goal，也不新增停止态。
测试端点（loopback）由调用方显式注入；默认走真实端点、旧行为不变。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from ..session import effects as fx
from .bounded_transport import BoundedHttpSender
from ..session.events import (
    OUTCOME_FACE_VERIFY,
    OUTCOME_OK,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    OP_QUERY,
    OP_START,
    OP_STOP,
    STATUS_CLOSED,
    STATUS_LIVE,
    STATUS_UNKNOWN,
    PlatformResult,
)

#: 平台要求人工继续（人脸验证）的业务码。它们不是"失败"，也不是停止目标本身：
#: 核心把这条事实落成 stopped + face_verify，由用户重新开播。
FACE_VERIFY_CODES = (60043, 60024)

#: ``_req`` 在网络/解析失败时用的哨兵码（见 ``BilibiliApi._req``）。
_TRANSPORT_CODE = -1


class RemainingBudget:
    """一次操作的**剩余**预算：截止来自 Core 的 Operation，时钟是同一个。"""

    __slots__ = ('_deadline_mono', '_clock')

    def __init__(self, deadline_mono: float, clock) -> None:
        self._deadline_mono = float(deadline_mono)
        self._clock = clock

    def remaining(self) -> float:
        return max(0.0, self._deadline_mono - float(self._clock.mono()))


def parse_live_status(value: Any) -> Optional[int]:
    """只接受接口允许的数值状态：0（未开播）/ 1（直播中）。

    ``bool`` 是 ``int`` 的子类，但 ``True`` 不是状态 1；浮点与字符串垃圾同样
    一律非法 —— 不把"字段不全或类型不对"强转成一个看似有效的状态。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (0, 1):
        return value
    return None


def parse_positive_area_id(value: Any) -> Optional[int]:
    """只接受真正的正整数分区 ID。名称反查失败**不能**把有效 ID 变成关闭。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def parse_observation(ok: bool, raw: Any) -> Tuple[str, Optional[int]]:
    """从**一份** ``get_info`` 响应里取出 (status, area_id)。

    - ``closed`` **不要求**分区字段；
    - ``live`` 需要有效分区才算完整在播观察，缺/非法一律 **ordinary unknown**；
    - ``data`` 不是对象、状态缺失/非法、响应无法解析 → 同样是 ordinary unknown。
    这里不新增状态、不区分邮件类别、也不把 unknown 当成 closed。
    """
    if not ok or not isinstance(raw, dict):
        return STATUS_UNKNOWN, None
    data = raw.get('data')
    if not isinstance(data, dict):
        return STATUS_UNKNOWN, None
    status = parse_live_status(data.get('live_status'))
    if status is None:
        return STATUS_UNKNOWN, None
    if status == 0:
        return STATUS_CLOSED, None
    area = parse_positive_area_id(data.get('area_id'))
    if area is None:
        return STATUS_UNKNOWN, None
    return STATUS_LIVE, area


def classify_outcome(ok: bool, raw: Any) -> Tuple[str, str]:
    """把一次原始回包归成 (outcome, detail)。

    分类只看**一次**响应：
    ``code == 0`` → ok；``60043/60024`` → face_verify；
    传输/解析哨兵（-1）与 HTTP 4xx-5xx → unknown；其余已解析业务码 → rejected。
    """
    if ok:
        return OUTCOME_OK, ''
    if not isinstance(raw, dict):
        return OUTCOME_UNKNOWN, ''
    code = raw.get('code')
    detail = str(raw.get('message') or raw.get('msg') or '')[:200]
    if isinstance(code, bool) or not isinstance(code, int):
        return OUTCOME_UNKNOWN, detail
    if code == 0:
        return OUTCOME_OK, detail
    if code in FACE_VERIFY_CODES:
        return OUTCOME_FACE_VERIFY, detail
    if code == _TRANSPORT_CODE or 400 <= code <= 599:
        # -1 = 网络/解析失败；4xx-5xx = HTTP 层没收下这次请求。
        # 两者都不是平台的业务决定，按 ordinary unknown 处理。
        return OUTCOME_UNKNOWN, detail
    return OUTCOME_REJECTED, detail


class PlatformHttpPort:
    """一个实例一份：复用生产签名/参数/凭据路径，只改成有界单轮。

    ``sender`` 是这一轮的**物理执行体**：默认用本包的
    :class:`~.bounded_transport.BoundedHttpSender` —— 整轮一份绝对截止，覆盖
    DNS／连接／TLS 握手／发送／读取／解析。它在构造时注入 ``BilibiliApi`` 的单轮
    策略，因此生产文件不需要引用本包。
    """

    def __init__(self, api, clock, endpoint_base: Optional[str] = None,
                 sender=None) -> None:
        self._api = api
        self._clock = clock
        self._endpoint_base = endpoint_base
        self._sender = BoundedHttpSender() if sender is None else sender

    def execute(self, job) -> PlatformResult:
        effect = job.effect
        budget = RemainingBudget(job.deadline_mono, self._clock)
        credentials = job.credentials if isinstance(job.credentials, dict) else {}
        # 凭据来自**冻结封套**，不在这里回读账号：中途换账号不会改写这一轮。
        csrf = str(credentials.get('bili_jct') or '')

        if isinstance(effect, fx.StartPlatform):
            with self._api.cookie_context(credentials):
                ok, raw = self._api.start_live_once_with_budget(
                    effect.room_id, effect.area_id, csrf, budget,
                    self._endpoint_base, self._sender)
            return self._platform_result(effect, OP_START, ok, raw)

        if isinstance(effect, fx.StopPlatform):
            with self._api.cookie_context(credentials):
                ok, raw = self._api.stop_live_once_with_budget(
                    effect.room_id, csrf, budget, self._endpoint_base,
                    self._sender)
            return self._platform_result(effect, OP_STOP, ok, raw)

        if isinstance(effect, fx.QueryPlatform):
            with self._api.cookie_context(credentials):
                ok, raw = self._api.get_info_once_with_budget(
                    effect.room_id, budget, self._endpoint_base, self._sender)
            status, area_id = parse_observation(ok, raw)
            return PlatformResult(
                operation_id=effect.op_id, kind=OP_QUERY,
                run_id=effect.run_id, room_id=effect.room_id,
                outcome=OUTCOME_OK if ok else OUTCOME_UNKNOWN,
                status=status, area_id=area_id,
                detail='' if ok else 'query_failed')

        raise TypeError(f'不是平台效果：{effect!r}')

    @staticmethod
    def _platform_result(effect, kind: str, ok: bool, raw: Any) -> PlatformResult:
        outcome, detail = classify_outcome(ok, raw)
        # 身份全部来自**冻结请求**，不按"当前最新 Run"重拼。
        return PlatformResult(operation_id=effect.op_id, kind=kind,
                              run_id=getattr(effect, 'run_id', None),
                              room_id=getattr(effect, 'room_id', None),
                              outcome=outcome, status=STATUS_UNKNOWN,
                              detail=detail)
