/**
 * session.ts - 阶段 4C 的新控制协议类型（唯一一份，不复制冲突 DTO）。
 *
 * 与后端 `app/models/control_dto.py`、`app/core/session_runtime/projection.py`
 * 一一对应；字段名与语义以源码为准（见 `PHASE4C-DESIGN.md` §1/§2）。
 *
 * 三条不能混的界线：
 *   1. `ControlReceipt`（回执，`accepted` 只表示意图已落盘）**不是**
 *      `SessionProjection`（GET 只读投影）；回执里的 `projection` 是**原快照**，
 *      默认不把它强转成 GET 类型。
 *   2. `confirmed_seconds` 是唯一可结算的业务秒数；`display_seconds` 只是显示。
 *   3. `display_advancing` / `display_valid_seconds` 是**服务端**给的外推许可与
 *      剩余预算，页面不得用 `accepted===true` 或 `goal.wanted==='running'` 自己推。
 */

export type SessionMode = 'task' | 'single'
export type PushSource = 'ffmpeg' | 'obs'
export type GoalWanted = 'running' | 'stopped'

/** 冻结账号上下文：**只**取 uid/room_id，绝不含展示名。 */
export interface AccountContext {
  uid: number
  room_id: number
}

export interface ProjectionAccount extends AccountContext {
  display_name: string
}

export interface SessionGoal {
  revision: number
  wanted: GoalWanted
  mode: SessionMode
  selection: Record<string, unknown>
  start_kind: string
  stop_reason: string | null
}

export interface SessionClock {
  checkpoint_seconds: number
  interval_start_mono: number | null
  interval_start_wall: string | null
  projecting: boolean
  display_anchor_mono: number | null
}

export interface SessionEnd {
  reason: string
  business_day: string
  settle: boolean
  effective_seconds: number
  completed_at: string | null
  ledger_receipt: unknown
}

export interface SessionRun {
  id: string
  uid: number
  room_id: number
  area_id: number | null
  zone_name: string
  mode: SessionMode
  task_id: number | null
  business_day: string
  target_seconds: number
  selected_by_revision?: number
  push_source: PushSource
  settlement_guard?: number | null
  task_source_revision?: number | null
  clock: SessionClock
  end: SessionEnd | null
}

/** 后台**已确认**事实（有界、账号隔离、稳定身份 `boot_id:seq`）。 */
export interface BackendEvent {
  id: string
  boot_id: string
  seq: number
  tag: string
  type: 'success' | 'danger' | 'warning' | 'info'
  message: string
  /** 事实冻结的业务时钟（ISO 秒），**不是**投递时刻。 */
  time: string
  uid: number
  room_id: number
  kind: string
  result: string
  run_id: string
  zone_name: string
  business_day: string
}

/**
 * 人脸事实。`verify_url` 只在服务端确认是合法 http(s) 地址时才有值；
 * 取不到时 `verify_url_available=false`，界面如实显示"验证链接未取得"。
 */
export interface FaceFacts {
  active: boolean
  goal_revision: number
  uid: number
  room_id: number
  zone_name: string
  reason: string
  verify_url: string
  verify_url_available: boolean
  confirmable: boolean
  block_reason: string
}

/** GET `/api/live/status` | `/state` | `/state/full` 的同一份只读投影。 */
export interface SessionProjection {
  has_active: boolean
  /** 是否存在场次。"没有场次"与"不限时"是两个事实，不能共用一个布尔值。 */
  has_run: boolean
  resumable: boolean
  resume_blocked_reason: string
  step: string
  goal: SessionGoal | null
  run: SessionRun | null
  current_zone: string
  run_id: string | null
  task_id: number | null
  business_day: string
  mode: SessionMode | ''
  push_source: PushSource | ''
  target_seconds: number
  /** 唯一可结算的业务秒数。 */
  confirmed_seconds: number
  /** 乐观显示秒数（服务端基数，含"Start 已派出"的乐观锚点）。 */
  display_seconds: number
  /** 显示是否允许继续外推；**不得**由 accepted/Goal 自行推断。 */
  display_advancing: boolean
  /** 允许外推的**剩余**秒数（服务端时间锚定，GET/重载只会让它变小）。 */
  display_valid_seconds: number
  /** 'optimistic'（已派出 Start、待首次确认）| 'confirmed' | 'frozen'。 */
  display_source: 'optimistic' | 'confirmed' | 'frozen'
  /** 目标是否有限（= 有场次且 target_seconds > 0）。 */
  duration_known: boolean
  room_id: number | null
  end: SessionEnd | null
  wait_reason: string
  account: ProjectionAccount | null
  boot_id: string
  backend_events: BackendEvent[]
  face: FaceFacts | null
}

/** 控制回执的状态取值（与后端 `_http_status_for` 的映射一致）。 */
export type ReceiptStatus =
  | 'accepted' | 'pending' | 'ignored' | 'rejected' | 'token_conflict'

/**
 * 控制回执。`accepted`/`success` 只表示**意图已持久受理**，
 * 既不表示平台已开播，也不表示已经关房。
 */
export interface ControlReceipt {
  operation_token: string
  status: ReceiptStatus
  accepted: boolean
  reason: string
  /** 原快照（**不是**展开投影）；默认只用于调试展示。 */
  projection: Record<string, unknown> | null
  success: boolean
  message: string
  detail: Record<string, unknown> | null
}

// ==================== 请求体 ====================

/** 自动任务：任务身份与推流来源**独立**。 */
export interface StartAutoTaskRequest {
  mode: 'task'
  push_source: PushSource
  account_context?: AccountContext
}

/** 指定任务：三件套必须在**确认框之前**冻结。 */
export interface StartNamedTaskRequest {
  mode: 'task'
  task_id: number
  expected_revision: number
  business_date: string
  push_source: PushSource
  account_context?: AccountContext
}

/** 单次：`target_seconds === 0` 表示不限时。 */
export interface StartSingleRequest {
  mode: 'single'
  zone_name: string
  area_id?: number
  target_seconds: number
  push_source: PushSource
  account_context?: AccountContext
}

export type StartRequest =
  | StartAutoTaskRequest | StartNamedTaskRequest | StartSingleRequest

export interface ResumeRequest {
  run_id: string
  goal_revision: number
  account_context?: AccountContext
}

/** 放弃原场：原 Run/Goal/选择三件套，`selection` 必须是后端投影里的原值。 */
export interface AbandonRequest {
  run_id: string
  goal_revision: number
  selection: Record<string, unknown>
}

export interface RestartAfterAbandonRequest extends Omit<StartAutoTaskRequest, 'account_context'> {
  abandon: AbandonRequest
  account_context?: AccountContext
}

export interface FaceConfirmRequest {
  goal_revision: number
  uid: number
  room_id: number
}

export interface StopRequest {
  account_context?: AccountContext
}
