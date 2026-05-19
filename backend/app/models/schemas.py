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
    """直播控制指令（与旧版兼容，用于内部逻辑）"""
    zone_name: str
    duration_seconds: int = 7200

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
            duration_seconds=duration
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
    """创建任务请求（category: 0=已完成, >0=时/天）"""
    zone_name: str
    category: int = 1                           # 0=已完成, >0=时/天
    total_days: int = 1
    days_done: int = 0
    deadline_raw: str = ""                      # 截止日期原始值
    today_done: Optional[int] = None
    remaining_days: int = 1


class TaskUpdate(BaseModel):
    """更新任务请求（所有字段可选）"""
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
    """导入结果"""
    success: bool = True
    imported_count: int = 0
    message: str = ""
    errors: List[str] = []
    needs_confirmation: bool = False          # 是否需要用户确认
    invalid_zones: List[str] = []             # 不存在的分区名列表


class ExportResult(BaseModel):
    """导出结果"""
    success: bool = True
    file_path: str = ""
    task_count: int = 0
    message: str = ""