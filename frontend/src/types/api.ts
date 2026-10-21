/**
 * api.ts - API 请求/响应类型定义
 *
 * 与后端 app/models/schemas.py、app/models/control_dto.py 对应，
 * 统一管理前端所有 API 接口的入参出参类型。
 *
 * 桌面移植阶段 2：旧 `LiveStatus` 一族（`is_streaming` / `elapsed_seconds` /
 * `pending_*` / `timer_state` …）已随旧控制器退役。运行事实的唯一形状是
 * `SessionProjection`（见 `types/session.ts`），由 `GET /api/live/status` 返回。
 */

// ==================== Auth 认证 ====================

/**
 * 已登录账号的**展示**资料（后端 `/api/auth/status` 只报内存里已核验的事实，
 * 只带 uid/uname/room_id；face/level 是旧字段，仅用于向后兼容显示）。
 */
export interface UserInfo {
  uid: number
  uname: string
  room_id?: number
  face?: string
  level?: number
}

export interface LoginStatusResponse {
  logged_in: boolean
  user_info?: UserInfo
  /** 'memory' | 'cached' —— 只用于展示口径，不参与鉴权判断。 */
  source?: string
}

/**
 * `GET /api/auth/qrcode`。
 *
 * `generation` 是**扫码代次**：再次获取二维码会前进一代，旧代次的轮询回包
 * 一律失效（`stale_generation`）。
 */
export interface QRCodeResponse {
  qrcode_key: string
  qr_url: string
  generation: number
}

/**
 * `POST /api/auth/poll/{key}` 与 `GET /api/auth/status` 的**归一化**结果
 * （`api/authWire.ts` 是唯一转换点）。
 *
 * **不是**所有 `logged_in=false` 都等于"还没扫"：`status` 携带真实原因。
 * 取值：`86101`（未扫）/`86090`（已扫待确认）/`86038`（已过期）/
 * `stale_generation`（已被更新的二维码取代）/`candidate_rejected`（无法核验）/
 * `no_session`/`poll_failed`/`rejected`/`installed`。
 */
export interface PollLoginResponse {
  logged_in: boolean
  user_info?: UserInfo
  status?: string
  message?: string
}

export interface LogoutResponse {
  success: boolean
  message: string
  operation_token?: string
  projection?: Record<string, unknown> | null
}

// ==================== Live 直播 ====================
//
// 运行事实的唯一形状是 `SessionProjection`（`GET /api/live/status` 返回）。
// 旧的 `LiveStatus` / `StartLiveResponse` / `AcceptedIntent` / `BackendEvent`
// 已退役：后端不再有旧控制器，那些字段名在新链上根本不会出现。

export type { SessionProjection } from './session'

// ==================== Tasks 任务 ====================

export interface TaskItem {
  /** 稳定身份：写请求（删除/修改/标记完成）按它定位，不按分区名。 */
  id?: number
  priority: number
  zone_name: string
  category: number
  total_days: number
  actual_days: number
  days_done: number
  deadline_raw?: string
  today_done: number | null
  remaining_days: number
  needs_execution: boolean
  is_completed: boolean
}

export interface TaskStats {
  total: number
  pending_total: number
  completed: number
  today_done: number
  today_pending: number
  remaining_time: number
  avg_remaining: number
  urgency: number
  /** 服务端计划时容错余量；旧版服务端可能不返回。 */
  schedule_margin?: ScheduleMargin | null
}

export interface ScheduleMargin {
  business_date: string
  hours: number | null
  bottleneck_date: string | null
  bottleneck_days: number | null
  sufficient: boolean
  reason: string | null
}

export interface TaskListResponse {
  tasks: TaskItem[]
  total: number
  pending_total: number
  completed: number
  today_done: number
  today_pending: number
  remaining_time: number
  avg_remaining: number
  urgency: number
  /** 与统计接口相同，旧版服务端可能不返回。 */
  schedule_margin?: ScheduleMargin | null
}

export interface NextTaskResponse {
  has_next: boolean
  zone_name?: string
  duration_seconds?: number
}

export interface MarkDoneResponse {
  success: boolean
  message: string
}

export interface ReloadResponse {
  success: boolean
  message: string
}

// ==================== Tasks CRUD ====================

export interface TaskDetail extends TaskItem {
  id: number
  deadline_raw: string
  a_val: number
  i_val: number
  j_val: number
  created_at?: string
  updated_at?: string
}

/** 覆盖确认时冻结的目标：稳定身份 + 当时看到的版本与业务日。
 *
 * 只冻结 id 不够：同一条记录在用户确认之后可能已经被结算/编辑（后台任务或
 * 别的页面），旧载荷带着同一个 id 照写就会抹掉刚提交的完成进度。版本与业务日
 * 必须在**弹框之前**与 id 一起取，不能在发请求时现取
 * ——现取等于把用户确认的那份状态悄悄换成最新的。
 *
 * 新流程改用 `CreationPreview` 的冻结件（见下）；本类型保留给兼容形状。
 */
export interface OverwriteTarget {
  id: number
  expectedRevision: number
  businessDate: string
}

/** `GET /api/tasks/creation-preview` 的一致预检。
 *
 * 行、版本、业务日与占用事实来自目标库**同一份读快照**；`uid` 是服务端此刻的
 * 账号绑定，调用方必须核对它仍等于自己冻结的范围，否则不得据此弹框或提交。
 */
export interface CreationPreview {
  uid: number | null
  instance_key: string
  zone_name: string
  tasks_revision: number | null
  business_day: string
  existing_task: {
    id: number
    category: number
    total_days: number
    days_done: number
    deadline_raw: string
    last_done_date?: string | null
  } | null
  /** 任务**总体**未完成（`category > 0 且 days_done < total_days`）。
   *  不是"今天是否需要执行"：今天已完成但总体 3/7 仍为 true，仍要确认。 */
  existing_is_unfinished: boolean
  is_currently_reserved: boolean
  creation_allowed: boolean
  reason: string
}

/** 弹框之前冻结的预检：提交时后端在同一事务里逐项复核。 */
export interface FrozenCreationPreview {
  task_id: number | null
  revision: number | null
  business_day: string
  existing_is_unfinished: boolean
}

/** 创建结果（`POST /api/tasks`）。 */
export interface CreateTaskResponse {
  success: boolean
  message: string
  task_id: number | null
  replaced_task_id: number | null
  replayed: boolean
  revision: number | null
  /** 数据库提交与唤醒投递是**两件事实**；投递失败照样返回创建成功。 */
  wakeup: { delivered: boolean; reason?: string; result?: string }
}

/** `GET /api/tasks/creation-receipt`：缺记录只表示尚未查到成功。 */
export interface CreationReceiptResponse {
  found: boolean
  token: string
  result?: {
    task_id?: number
    replaced_task_id?: number | null
    zone_name?: string
    revision?: number | null
  }
  /** 服务端**领取这次读取的那份绑定**的 UID（命中回执时返回）。 */
  uid?: number | null
}

/** 分区历史里的终结口径（只认已经发生的事实）。 */
export type TerminalCode =
  | 'ongoing' | 'natural_complete' | 'manual_complete' | 'manual_all_done'
  | 'moved_out' | 'replaced' | 'deleted' | 'unknown'

export interface ZoneHistoryEvent {
  id: number
  task_id: number
  event: string
  at: string
  zone_name: string
  category_before: number | null
  total_days_before: number | null
  days_done_before: number | null
  deadline_before: string | null
  category_after: number | null
  total_days_after: number | null
  days_done_after: number | null
  deadline_after: string | null
  source: string
  detail: string
}

export interface ZoneHistoryLedgerRow {
  id: number
  task_id: number
  business_day: string
  source: string
  zone_name: string
  target_seconds: number
  effective_seconds: number
  completed_at: string | null
  committed_at: string
  days_done_after: number
  total_days_after: number
}

export interface ZoneHistoryGroup {
  task_id: number
  zone_name: string
  is_current: boolean
  params: {
    category: number | null
    total_days: number | null
    days_done: number | null
    deadline_raw: string | null
  }
  /** 'created' = 真实创建时刻；'baseline' = 现状收录；'unknown' = 未记录。 */
  origin: { code: 'created' | 'baseline' | 'unknown'; at: string | null }
  terminal: { code: TerminalCode; at: string | null; detail: string }
  last_at: string | null
  events: ZoneHistoryEvent[]
  ledger: ZoneHistoryLedgerRow[]
}

export interface ZoneHistoryResponse {
  zone_name: string
  groups: ZoneHistoryGroup[]
  total: number
  offset: number
  limit: number
  has_more: boolean
  /** 服务端**领取这次读取的那份绑定**的 UID：调用方据此核对来源未换账号。 */
  uid?: number | null
}

export interface TaskCreate {
  /** 兼容形状：带 `overwrite=true` 时用 `id`/`expected_revision`/`business_date`
   *  组装同一份预检。新流程用 `preview` + `confirm_replace`。 */
  id?: number
  expected_revision?: number
  business_date?: string
  /** 弹框之前冻结的预检。 */
  preview?: FrozenCreationPreview
  /** 用户对"替换这一条未完成任务"的明确确认语义。 */
  confirm_replace?: boolean
  /** 客户端生成的一次性令牌；回包丢失后凭它取回原来那份已提交结果。 */
  operation_token?: string
  zone_name: string
  category?: number
  total_days?: number
  days_done?: number
  deadline_raw?: string
  today_done?: number | null
  remaining_days?: number
}

export interface TaskUpdate {
  zone_name?: string
  category?: number
  total_days?: number
  days_done?: number
  deadline_raw?: string
  today_done?: number | null
  remaining_days?: number
}

export interface TaskDetailResponse {
  tasks: TaskDetail[]
  total: number
  pending_total: number
  completed: number
  today_done: number
  today_pending: number
  remaining_time: number
  avg_remaining: number
  urgency: number
  /** 与统计接口相同，旧版服务端可能不返回。 */
  schedule_margin?: ScheduleMargin | null
}

export interface ImportResult {
  success: boolean
  imported_count: number
  message: string
  errors?: string[]
  needs_confirmation?: boolean
  invalid_zones?: string[]
  imported?: number
  updated?: number
  skipped?: number
  rejected?: number
  revision?: number
}

export interface ExportResult {
  success: boolean
  file_path: string
  task_count: number
  message: string
}

// ==================== StartLive 请求体 ====================
//
// 请求体形状已全部迁到 `types/session.ts`
// （`StartRequest` / `ResumeRequest` / `AbandonRequest` /
// `RestartAfterAbandonRequest` / `FaceConfirmRequest` / `StopRequest`）。
// 旧的 `StartLiveRequest`（zone_name/duration_seconds/task_id/
// expected_revision/business_date/abandon_saved）已退役：`elapsed_seconds`、
// `stop_epoch`、`abandon_saved` 等旧字段会被 DTO 的 `extra='forbid'` 直接 422。

// ==================== Areas 分区 ====================

export interface SubArea {
  id: number
  name: string
  parent_id?: number
  parent_name?: string
}

export interface AreaCategory {
  id: number
  name: string
  list: SubArea[]
}

export interface AreaListResponse {
  areas: AreaCategory[]
  message?: string
}

export interface AreaSearchResult {
  id: number
  name: string
  parent_id?: number
  parent_name?: string
}

export interface AreaSearchResponse {
  results: AreaSearchResult[]
  total: number
}

export interface RefreshAreasResponse {
  success: boolean
  message: string
}

// ==================== Settings 设置 ====================

export interface AppSettings {
  video_path: string
  excel_path: string
  db_path: string
  scan_interval_seconds: number
  max_reconnect: number
  live_retry_cooldown_minutes: number
  stream_mode: 'manual' | 'ffmpeg'
  auto_open_video: boolean
  ffmpeg_path: string
  ffmpeg_reencode: boolean
  // 邮箱推送
  /** 推送总开关：关闭后所有渠道（邮箱/Server酱）都不再发送 */
  notification_enabled?: boolean
  notification_channel: 'email' | 'serverchan' | 'both'
  email_enabled: boolean
  email_smtp_host: string
  email_smtp_port: number
  email_smtp_user: string
  email_smtp_pass: string
  email_recipients: string
  email_notify_start: boolean
  email_notify_stop: boolean
  email_notify_error: boolean
  email_notify_complete: boolean
  email_daily_summary: boolean
  /** 默认关闭；只通知一轮本地 FFmpeg 重试，严重连续失败另发需处理通知 */
  email_notify_ffmpeg_retry: boolean
  email_face_verify_port: number
  // Server酱
  serverchan_sendkey: string
  // 服务器公网地址（域名或IP），留空自动检测
  server_host: string
  duration_distribution: 'uniform' | 'normal' | 'beta'
  duration_multiplier_min: number
  duration_multiplier_max: number
  // 旁路诊断（后端默认全部关闭；可选字段，旧版本后端不返回也不影响）
  diagnostics_enabled?: boolean
  diagnostics_media_progress?: boolean
  diagnostics_host_sampling?: boolean
  diagnostics_max_file_mb?: number
  diagnostics_retain_days?: number
}
