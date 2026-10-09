"""阶段 4B 控制面 HTTP DTO。

**薄到只做协议校验**：字段名、类型与"哪些是必填冻结上下文"在这里判定，业务字段
原样转交 :class:`~app.core.session_runtime.control.ControlService`。任何缺字段一律
**拒绝**，服务端绝不用当前快照/最新票据替它补值（见 ``PHASE4B-API-CONTRACT.md``）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AccountContextDTO(BaseModel):
    """冻结的账号上下文（旧页面/旧链接携带时原样校验）。"""

    model_config = ConfigDict(extra='forbid')

    uid: int
    room_id: int


class ControlRequest(BaseModel):
    """开播/执行下一个任务/放弃并新开的**共同**请求形状。

    - ``mode='task'``：``selection.kind='auto'``（或省略，走自动选取）；给
      ``task_id`` 时**必须**同时给 ``expected_revision`` 与 ``business_date``。
    - ``mode='single'``：必须给单次参数（``zone_name`` 与 ``target_seconds``）。
    - 状态编辑字段（``elapsed_seconds`` 等）在这里就是未知字段 → 422。
    """

    model_config = ConfigDict(extra='forbid')

    mode: str = 'task'
    selection: Optional[Dict[str, Any]] = None
    task_id: Optional[int] = None
    expected_revision: Optional[int] = None
    business_date: Optional[str] = None
    push_source: Optional[str] = None
    kind: Optional[str] = None
    zone_name: Optional[str] = None
    area_id: Optional[int] = None
    target_seconds: Optional[int] = None
    account_context: Optional[AccountContextDTO] = None

    def to_service_request(self) -> Dict[str, Any]:
        """转成服务层请求体（业务字段原样透传，``account_context`` 单独走）。"""
        payload = self.model_dump(exclude_none=True)
        payload.pop('account_context', None)
        if payload.get('selection') is None:
            payload.pop('selection', None)
        return payload


class ResumeRequest(BaseModel):
    """恢复原场：**必须**携带用户看到的原 Run 与 Goal 版本。"""

    model_config = ConfigDict(extra='forbid')

    run_id: str
    goal_revision: int
    account_context: Optional[AccountContextDTO] = None


class AbandonRequest(BaseModel):
    """放弃原场：**必须**携带原 Run/Goal/选择三件套。"""

    model_config = ConfigDict(extra='forbid')

    run_id: str
    goal_revision: int
    selection: Dict[str, Any]


class RestartAfterAbandonRequest(ControlRequest):
    """放弃并新开：**一项**请求同时携带原场冻结上下文与新选择。"""

    abandon: AbandonRequest

    def to_service_request(self) -> Dict[str, Any]:
        payload = super().to_service_request()
        payload['abandon'] = self.abandon.model_dump()
        return payload


class StopRequest(BaseModel):
    """停止：可携带冻结账号上下文；没有必需字段（停止必须随时可达）。"""

    model_config = ConfigDict(extra='forbid')

    account_context: Optional[AccountContextDTO] = None


class FaceConfirmRequest(BaseModel):
    """面板人脸确认：**必须**携带原 Goal 版本与原账号/房间。"""

    model_config = ConfigDict(extra='forbid')

    goal_revision: int
    uid: int
    room_id: int


class LoginRequest(BaseModel):
    """登录安装：候选凭据由扫码结果或显式载荷提供。"""

    model_config = ConfigDict(extra='forbid')

    uid: int
    room_id: int
    display_name: str = ''
    SESSDATA: str
    bili_jct: str = ''
    DedeUserID: str = ''

    def credentials(self) -> Dict[str, str]:
        payload = {
            'SESSDATA': self.SESSDATA,
            'bili_jct': self.bili_jct,
            'DedeUserID': self.DedeUserID or str(self.uid),
        }
        return {key: value for key, value in payload.items() if value}


class LogoutRequest(BaseModel):
    """退出：可选地同一请求安装下一个账号。"""

    model_config = ConfigDict(extra='forbid')

    next_account: Optional[LoginRequest] = None
    account_context: Optional[AccountContextDTO] = None
    timeout: float = 15.0


class ControlResponse(BaseModel):
    """控制回执。``accepted`` 只表示**意图已持久保存**，不宣告平台动作成败。"""

    operation_token: str
    status: str
    accepted: bool
    reason: str = ''
    projection: Optional[Dict[str, Any]] = None
    #: 兼容旧前端的布尔位：语义 = 已受理（不是"平台已开播"）。
    success: bool = False
    message: str = ''
    detail: Optional[Dict[str, Any]] = None


class RetiredEndpointResponse(BaseModel):
    """已退役入口的稳定拒绝形状。"""

    status: str = 'retired'
    accepted: bool = False
    reason: str = ''
    replacement: List[str] = Field(default_factory=list)
