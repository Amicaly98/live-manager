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

import logging
from dataclasses import replace
from typing import Any, Callable, Optional, Tuple

from ..session import effects as fx
from .bounded_transport import BoundedHttpSender
from ..session.events import (
    OUTCOME_AREA_UNAVAILABLE,
    OUTCOME_FACE_VERIFY,
    OUTCOME_OK,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    OP_QUERY,
    OP_START,
    OP_STOP,
    STATUS_CLOSED,
    STATUS_LIVE,
    STATUS_ROUND,
    STATUS_UNKNOWN,
    PlatformResult,
)

logger = logging.getLogger(__name__)

#: 平台要求人工继续（人脸验证）的业务码。它们不是"失败"，也不是停止目标本身：
#: 核心把这条事实落成 stopped + face_verify，由用户重新开播。
FACE_VERIFY_CODES = (60043, 60024)

#: ``_req`` 在网络/解析失败时用的哨兵码（见 ``BilibiliApi._req``）。
_TRANSPORT_CODE = -1

#: 平台文案里**明确**说明分区不存在/已下架的标记（兜底用；实测的
#: "请求错误" 是通用文案，不会误触发）。现场确认平台措辞后可在此扩充。
AREA_DELISTED_MARKERS = (
    '分区不存在', '分区已下架', '分区不可用',
    'invalid area', 'area not exist',
)


class RemainingBudget:
    """一次操作的**剩余**预算：截止来自 Core 的 Operation，时钟是同一个。"""

    __slots__ = ('_deadline_mono', '_clock')

    def __init__(self, deadline_mono: float, clock) -> None:
        self._deadline_mono = float(deadline_mono)
        self._clock = clock

    def remaining(self) -> float:
        return max(0.0, self._deadline_mono - float(self._clock.mono()))


def parse_live_status(value: Any) -> Optional[int]:
    """只接受接口允许的数值状态：0（未开播）/ 1（直播中）/ 2（平台轮播）。

    ``bool`` 是 ``int`` 的子类，但 ``True`` 不是状态 1；浮点与字符串垃圾同样
    一律非法 —— 不把"字段不全或类型不对"强转成一个看似有效的状态。轮播是平台
    明确返回的合法状态，但不是**本程序**的直播；由核心按 closed 处理、按原目标
    直接开播，不能当成 ordinary unknown 无限重查。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (0, 1, 2):
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
    - ``round``（``live_status=2``，平台轮播）同样不是本程序的直播，且不需要分区；
      核心按 closed 处理，直接按目标开播，不反复调用 Stop；
    - ``live`` 需要有效分区才算完整在播观察，缺/非法一律 **ordinary unknown**；
    - ``data`` 不是对象、状态缺失/非法、响应无法解析 → 同样是 ordinary unknown。
    这里不区分邮件类别、也不把 unknown 当成 closed。
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
    if status == 2:
        return STATUS_ROUND, None
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
                 sender=None, area_index: Optional[Callable[[], dict]] = None,
                 area_refresh: Optional[Callable[..., bool]] = None,
                 area_refresh_seconds: float = 2.0,
                 area_refresh_cooldown_seconds: float = 300.0) -> None:
        self._api = api
        self._clock = clock
        self._endpoint_base = endpoint_base
        self._sender = BoundedHttpSender() if sender is None else sender
        #: 共享分区列表的热索引与“限频自动刷新”能力（默认关闭：测试/隔离构造
        #: 不会因为一次拒绝就联网）。
        self._area_index = area_index
        self._area_refresh = area_refresh
        self._area_refresh_seconds = max(0.0, float(area_refresh_seconds))
        self._area_refresh_cooldown = max(0.0, float(
            area_refresh_cooldown_seconds))
        self._last_area_refresh: Optional[float] = None

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
            return self._start_result(effect, ok, raw, budget)

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

    def _start_result(self, effect, ok: bool, raw: Any,
                      budget: RemainingBudget) -> PlatformResult:
        """开播结果分类；业务拒绝且分区已下架时改判 ``area_unavailable``。"""
        result = self._platform_result(effect, OP_START, ok, raw)
        if result.outcome != OUTCOME_REJECTED or not effect.zone_name:
            return result
        if self._zone_missing(effect):
            return replace(result, outcome=OUTCOME_AREA_UNAVAILABLE,
                           detail=result.detail or 'area_unavailable')
        # 被拒但当前列表仍“看起来有效”：可能是本地列表过期。限频刷新一次
        # （同一操作剩余预算内，最多约 2 秒）后再判定。
        self._refresh_areas_within(budget)
        if self._zone_missing(effect):
            return replace(result, outcome=OUTCOME_AREA_UNAVAILABLE,
                           detail=result.detail or 'area_unavailable')
        if self._detail_marks_delisted(result.detail):
            return replace(result, outcome=OUTCOME_AREA_UNAVAILABLE,
                           detail=result.detail)
        return result

    def _zone_missing(self, effect) -> bool:
        """可用目录明确缺少冻结分区名，才支持“任务分区已下架”的判定。

        同名分区的 ID 变化只说明旧请求的映射可能过期，不证明任务本身无法
        完成。保持这次平台拒绝的原结果，由正常拒绝路径说明原因、保留任务；
        不改写本次冻结目标，也不把 ID 变化写成永久移出历史。
        """
        if self._area_index is None:
            return False
        try:
            index = self._area_index()
        except Exception:                                 # noqa: BLE001
            return False
        if not isinstance(index, dict) or not index:
            return False                    # 列表不可用：不做判断
        zone = str(effect.zone_name or '').strip()
        return zone not in index

    def _refresh_areas_within(self, budget: RemainingBudget) -> None:
        refresh = self._area_refresh
        if refresh is None:
            return
        now = float(self._clock.mono())
        if (self._last_area_refresh is not None
                and now - self._last_area_refresh < self._area_refresh_cooldown):
            return
        seconds = min(self._area_refresh_seconds, budget.remaining())
        if seconds < 0.25:
            return
        self._last_area_refresh = now
        sub_budget = RemainingBudget(now + seconds, self._clock)
        try:
            refresh(sub_budget, self._sender, self._endpoint_base)
        except Exception:                                 # noqa: BLE001
            logger.warning('开播被拒后刷新共享分区失败（不影响本次判定）',
                           exc_info=True)

    @staticmethod
    def _detail_marks_delisted(detail: str) -> bool:
        text = str(detail or '').lower()
        return any(marker in text for marker in AREA_DELISTED_MARKERS)

    @staticmethod
    def _platform_result(effect, kind: str, ok: bool, raw: Any) -> PlatformResult:
        outcome, detail = classify_outcome(ok, raw)
        # 身份全部来自**冻结请求**，不按"当前最新 Run"重拼。
        return PlatformResult(operation_id=effect.op_id, kind=kind,
                              run_id=getattr(effect, 'run_id', None),
                              room_id=getattr(effect, 'room_id', None),
                              outcome=outcome, status=STATUS_UNKNOWN,
                              detail=detail)
