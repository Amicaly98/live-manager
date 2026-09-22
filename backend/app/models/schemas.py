"""
schemas.py - Pydantic 数据模型（用于 API 请求/响应验证）

保留原有的 LiveInstruction / Task 核心结构，
增加 RESTful 风格的请求/响应体。
"""

from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


# ==================== 原有核心模型（适配 Pydantic） ====================
class LiveInstruction(BaseModel):
    """直播控制指令（与旧版兼容，用于内部逻辑）

    task_id / run_id / execution_date 是**可选的执行身份**：
    - task_id：开播时选定的那条任务记录的数据库主键。分区名可被删除重建，
      id 才是稳定身份——结算与"运行中任务保护"都以它为准；
    - run_id：本场直播的唯一标识，用于区分"同一场的不同阶段"与"另一场"；
    - execution_date：开播时捕获的业务日，跨日之后到达的旧结算不会记到新的一天。
    旧调用方不传这些字段时行为完全不变。
    """
    zone_name: str
    duration_seconds: int = 7200
    task_id: Optional[int] = None
    run_id: Optional[str] = None
    execution_date: Optional[str] = None

    class Config:
        # 允许使用原有 dataclass 属性
        from_attributes = True

    def to_dict(self):
        return {
            "zone_name": self.zone_name,
            "duration_seconds": self.duration_seconds
        }


class Task(BaseModel):
    """任务数据模型

    category: 0=已完成, >0=每日基准小时数（如2=每天2小时）
    priority: 由 a_val 计算的优先度（越小越优先），加载时从 DB 取
    """
    id: Optional[int] = None                    # 数据库主键（稳定身份，可为空）
    priority: int = 9999                        # 计算优先度（来自 DB a_val）
    zone_name: str
    category: int = 1                           # 0=已完成, >0=时/天
    total_days: int = 1
    days_done: int = 0
    deadline_formula: str = ""
    today_done: Optional[int] = None
    remaining_days: int = 1

    def is_today_done(self) -> bool:
        return self.today_done == 1

    def needs_execution(self) -> bool:
        return not self.is_today_done() and self.category > 0

    def actual_days(self) -> int:
        """实际需要执行的天数 = total_days（不额外增加）"""
        return self.total_days

    def remaining_exec_days(self) -> int:
        """剩余待执行天数（权威派生口径，与 DB 计算列 I 同一含义）。

        历史缺陷（2026-09-21 DS1）：旧 ``remaining_days`` 列默认 1 且静态重算
        从不更新它，邮件/接口读到的是这个陈旧列，于是进度 2/10 的任务显示
        "剩余 1 天"。这里统一为唯一权威口径：

        - 已完成任务（category<=0）为 0；
        - 其余为 ``actual_days() - days_done``；
        - 进度异常超过计划（days_done > total）时按 0 展示——"已超过计划
          天数"比负数天数可读；调度与统计的既有公式不受影响（不偷改）。

        旧列仅作兼容保留（回退到旧版本时仍有合理数据），任何展示/导出
        路径都不得再直接读它。
        """
        if self.category <= 0:
            return 0
        return max(0, self.actual_days() - self.days_done)

    def to_instruction(self) -> LiveInstruction:
        """生成直播指令：时长 = 基础 × 下限 + 基础 × (上限-下限) × 随机因子(0~1)"""
        import random as _random
        base = max(1800, self.category * 3600) if self.category > 0 else 7200
        lo, hi, dist = 1.05, 1.25, "beta"
        try:
            from app.api.settings import load_settings
            s = load_settings()
            lo, hi = s.duration_multiplier_min, s.duration_multiplier_max
            dist = s.duration_distribution
        except Exception:
            pass
        # 随机因子 (0~1)
        if dist == "beta":
            factor = _random.betavariate(2, 6)
        elif dist == "normal":
            factor = _random.gauss(0.5, 0.15)
            factor = max(0.0, min(1.0, factor))
        else:
            factor = _random.random()
        # 时长 = 基础×下限 + 基础×(上限-下限)×factor
        duration = int(base * lo + base * (hi - lo) * factor)
        return LiveInstruction(
            zone_name=self.zone_name,
            duration_seconds=duration,
            task_id=self.id,
        )


# ==================== API 请求/响应模型 ====================
class LoginQRResponse(BaseModel):
    """获取二维码响应"""
    qr_url: str
    qr_key: str
    expires_at: str


class LoginStatusResponse(BaseModel):
    """登录状态查询响应"""
    logged_in: bool
    user_info: Optional[dict] = None
    need_scan: bool = False


class StartLiveRequest(BaseModel):
    """开始直播请求（zone_name 非空=手动模式，空=任务模式自动取下一任务）
    duration_seconds: 手动模式时长，0=不限时，上限86400(24h)
    """
    room_id: Optional[int] = None
    zone_name: Optional[str] = None
    duration_seconds: Optional[int] = None


class StartLiveResponse(BaseModel):
    """开始直播响应"""
    success: bool
    room_id: Optional[int] = None
    stream_url: Optional[str] = None
    message: str = ""
    need_face_verification: bool = False
    qr_data: Optional[str] = None


class StopLiveResponse(BaseModel):
    success: bool
    message: str = ""

class TaskListResponse(BaseModel):
    """任务列表响应"""
    tasks: List[Task] = []
    total: int = 0
    active: int = 0
    completed: int = 0


class LiveStatusResponse(BaseModel):
    """直播状态响应"""
    is_streaming: bool
    current_zone: Optional[str] = None
    elapsed_seconds: int = 0
    remaining_seconds: int = 0
    room_id: Optional[int] = None


class AreaItem(BaseModel):
    """分区项"""
    id: int
    name: str
    parent_id: int = 0
    children: List['AreaItem'] = []


class UserInfo(BaseModel):
    uid: int
    uname: str
    face: str = ""
    level: int = 0


# ==================== 任务 CRUD 模型 ====================

class TaskCreate(BaseModel):
    """创建任务请求（category: 0=已完成, >0=时/天）

    ``id`` 只有 ``overwrite=true`` 时才需要：它是**被覆盖那一条记录**的稳定
    身份（用户在确认框里看到的那一条）。旧模型没有这个字段，Pydantic 会把
    客户端发来的 id 直接丢掉，服务端只能退回按分区名匹配——删除重建同名任务
    之后，旧覆盖就落到了新记录上。

    ``expected_revision`` / ``business_date`` 是覆盖请求的**前置条件**：用户在
    确认框里看到的那份列表的版本与业务日。只有 id 还不够——同一条记录在确认
    之后可能已经被结算/编辑过（别的面板或后台任务），此时旧载荷仍然是"同一个
    id"，照写就会抹掉刚提交的完成进度。旧模型同样会丢弃这两个字段，服务端只
    能无保护地照写，因此这里显式建模并交给事务内的条件检查。
    """
    id: Optional[int] = None
    expected_revision: Optional[int] = None      # 确认时的 tasks_revision
    business_date: Optional[str] = None          # 确认时的业务日（YYYY-MM-DD）
    zone_name: str
    category: int = 1                           # 0=已完成, >0=时/天
    total_days: int = 1
    days_done: int = 0
    deadline_raw: str = ""                      # 截止日期原始值
    today_done: Optional[int] = None
    remaining_days: int = 1


class TaskUpdate(BaseModel):
    """更新任务请求（所有字段可选）

    ``id`` 是记录的稳定身份；带 id 时按 id 定位、body 的 ``zone_name`` 只作
    **重命名目标**（原子改本行）。不带 id 时退回按路径上的分区名定位。
    """
    id: Optional[int] = None
    zone_name: Optional[str] = None
    category: Optional[int] = None
    total_days: Optional[int] = None
    days_done: Optional[int] = None
    deadline_raw: Optional[str] = None
    today_done: Optional[int] = None
    remaining_days: Optional[int] = None


class TaskDetail(BaseModel):
    """任务详情响应（含 id 和计算列）"""
    id: int
    zone_name: str
    category: int = 1
    total_days: int = 1
    days_done: int = 0
    deadline_raw: str = ""
    today_done: Optional[int] = None
    remaining_days: int = 1
    a_val: int = 9999
    i_val: int = 0
    j_val: int = -1
    actual_days: int = 1
    needs_execution: bool = False
    is_completed: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ImportResult(BaseModel):
    """导入结果

    ``rejected=True`` 表示**未提交**（没有有效行 / 正在直播 / 事务回滚），
    此时旧数据保持不变；调用方不得把它当成成功。
    """
    success: bool = True
    imported_count: int = 0
    message: str = ""
    errors: List[str] = []
    needs_confirmation: bool = False          # 是否需要用户确认
    invalid_zones: List[str] = []             # 不存在的分区名列表
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    rejected: bool = False
    revision: Optional[int] = None


class ExportResult(BaseModel):
    """导出结果"""
    success: bool = True
    file_path: str = ""
    task_count: int = 0
    message: str = ""
