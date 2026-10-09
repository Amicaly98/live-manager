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
    # "点哪条开哪条"在**用户确认那一刻**冻结的前置条件（工作包 B 监督返修）：
    # select_task_instruction 填写，与任务 CRUD 共用的 _mutation_lock 内的
    # 预留/接管复核据此再核一遍。None = 非指定任务选择（轮转/手动），不做
    # 该复核。旧调用方不传这些字段时行为完全不变。
    source_revision: Optional[int] = None
    source_business_date: Optional[str] = None

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

    任务身份（2026-09-28 工作包 B）：任务列表点哪一行就从哪一行开始。
    - ``task_id``：被点任务的稳定身份（DB 主键）。有值时**只**启动这一条任务，
      不再按优先度自动另选；没有值时沿用既有自动选择（控制台「下一任务」）。
    - ``expected_revision`` / ``business_date``：确认那一刻冻结的任务列表版本与
      业务日。缺任何一个都不受理带 task_id 的请求——否则"用户在弹框里看到的
      那一条"与"真正被启动的那一条"之间就没有可核对的约束。
    - ``abandon_saved``：用户对"放弃本次未结算旧进度"的确认凭证。只有它存在且
      冻结身份全部核对通过时才绕过旧存档保护（见 ``AbandonSavedRequest``）。
    """
    room_id: Optional[int] = None
    zone_name: Optional[str] = None
    duration_seconds: Optional[int] = None
    task_id: Optional[int] = None
    expected_revision: Optional[int] = None
    business_date: Optional[str] = None
    abandon_saved: Optional['AbandonSavedRequest'] = None


class AbandonSavedRequest(BaseModel):
    """受控放弃旧进度的确认凭证（每一项都是**确认时冻结**的事实）。

    服务端在启动新任务之前逐个核对：
    - 旧会话身份（``old_run_id``/``old_task_id``/``old_zone_name``/
      ``old_execution_date``/``old_elapsed_seconds``）必须与**当前**存档一致：
      期间任何一次面板操作、后台结算、另一个标签页的停止/恢复都会让它失效；
    - ``stop_epoch`` 是确认时的控制代际，用来识别"停止之后的迟到确认"；
    - ``confirm_id`` 是一次性标识：同一张票的重放不能第二次触发同一次放弃。

    这里**不**clean任何东西，真正的"丢弃"发生在新任务被平台确认开播之后。
    """
    old_run_id: str = ''
    old_task_id: Optional[int] = None
    old_zone_name: str = ''
    old_execution_date: str = ''
    # 已播秒数是确认的主体本身：None = 凭证没带（直接拒绝），0 只代表
    # "确认时旧进度确实是 0"。
    old_elapsed_seconds: Optional[int] = None
    stop_epoch: int = 0
    confirm_id: str = ''


class SavedProgressConflict(BaseModel):
    """旧存档仍然持有一份未结算进度时的冲突资料（供前端生成确认文案）。

    只描述**已存在的事实**：旧会话身份、已确认秒数、目标时长、执行日，以及
    这份进度现在能不能恢复。前端必须原样展示，不得自行推断将要丢弃多少。
    """
    run_id: str = ''
    task_id: Optional[int] = None
    zone_name: str = ''
    execution_date: str = ''
    elapsed_seconds: int = 0
    duration_seconds: int = 0
    duration_known: bool = False
    resumable: bool = False
    reason: str = ''


class AcceptedIntent(BaseModel):
    """一次**受理**的控制意图的显示基线。

    面板拿到它就可以立刻显示乐观推进的秒数，不必等第一次平台观察；它与
    权威计时是两件事：这里全是受理时刻冻结的值，不含任何推测，也不参与
    结算、邮件与跨日判定。

    - ``run_id``：本次会话身份。后续所有状态都以它为准，另一场的迟到响应
      不得拿来改写本场的显示。
    - ``task_id``：受理时实际选中的任务（手动模式为 null）。
    - ``confirmed_elapsed_seconds``：受理时**已确认**的进度。恢复场景下它是
      继承值（例如 3590），新开播为 0；它不是"预计"，是服务端已有的事实。
    """
    run_id: str = ''
    task_id: Optional[int] = None
    zone_name: str = ''
    duration_seconds: int = 0
    duration_known: bool = False
    confirmed_elapsed_seconds: int = 0
    stream_mode: str = ''
    source: str = ''
    epoch: int = 0
    accepted_at: str = ''


class StartLiveResponse(BaseModel):
    """开始直播响应"""
    success: bool
    room_id: Optional[int] = None
    stream_url: Optional[str] = None
    message: str = ""
    need_face_verification: bool = False
    qr_data: Optional[str] = None
    #: 受理成功时才给出；数据全部是受理时刻的冻结值（见 ``AcceptedIntent``）。
    accepted: Optional[AcceptedIntent] = None
    #: 旧存档仍有未结算进度时需要用户显式确认（见 ``SavedProgressConflict``）。
    saved_progress_conflict: Optional[SavedProgressConflict] = None


class RunNextTaskResponse(BaseModel):
    """执行下一个任务的响应（含受理意图，前端可立即乐观显示）。"""
    success: bool
    message: str = ""
    need_face_verification: bool = False
    qr_data: Optional[str] = None
    zone_name: str = ''
    duration_seconds: int = 0
    accepted: Optional[AcceptedIntent] = None


StartLiveRequest.model_rebuild()


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

class CreatePreview(BaseModel):
    """创建预检时**冻结**的那份状态（M1）。

    用户在弹框里确认的是"他当时看到的那一条记录 + 那一刻的列表版本与业务日"，
    不是"这个名字"、也不是"现在的最新状态"。三样必须在**弹框之前**一起取，
    提交时由服务端在同一事务里逐项复核；任何一项不符就是明确冲突，绝不自动换
    最新版本后重试。
    """
    task_id: Optional[int] = None            # 预检时该分区的当前行 id（无则 None）
    revision: Optional[int] = None           # 预检时的 tasks_revision
    business_day: Optional[str] = None       # 预检时的业务日（YYYY-MM-DD）
    existing_is_unfinished: Optional[bool] = None


class TaskCreate(BaseModel):
    """创建任务请求（category: 0=已完成, >0=时/天）

    **M1 起，创建一律产生新身份**：新 task_id、进度 0；同分区已有的任务留在
    历史与账本里（未完成时终结为"被新任务覆盖"）。旧 ``overwrite=true`` 的
    UPDATE 旧 id 语义已退役（见 ``M1-MIGRATION-REGISTER.md``）。

    三个新字段都是**可选**的，旧载荷不带时走"服务端自行判定"的兼容分支：

    - ``preview``：弹框之前冻结的预检（身份 + 版本 + 业务日 + 未完成标志）；
    - ``confirm_replace``：用户对"替换这一条未完成任务"的明确确认语义；
    - ``operation_token``：客户端生成的一次性令牌；回包丢失后凭它取回原来那份
      已提交结果，不再执行第二次创建。

    ``id`` / ``expected_revision`` / ``business_date`` 保留为**旧调用形状**：
    带 ``overwrite=true`` 与 id 时，服务端用它们组装出同一份预检，走同一条事务。
    """
    id: Optional[int] = None
    expected_revision: Optional[int] = None      # 确认时的 tasks_revision
    business_date: Optional[str] = None          # 确认时的业务日（YYYY-MM-DD）
    preview: Optional[CreatePreview] = None      # 弹框前冻结的完整预检
    confirm_replace: Optional[bool] = None       # 对未完成旧任务的明确替换确认
    operation_token: Optional[str] = None        # 创建回执的操作令牌
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