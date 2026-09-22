/**
 * api.ts - API 请求/响应类型定义
 *
 * 与后端 app/models/schemas.py 对应，
 * 统一管理前端所有 API 接口的入参出参类型。
 */

// ==================== Auth 认证 ====================

export interface UserInfo {
  uid: number
  uname: string
  face: string
  level: number
}

export interface LoginStatusResponse {
  logged_in: boolean
  user_info?: UserInfo
}

export interface QRCodeResponse {
  qrcode_url: string
  qrcode_key: string
}

export interface PollLoginResponse {
  logged_in: boolean
  user_info?: UserInfo
  expired?: boolean
  message?: string
}

export interface LogoutResponse {
  success: boolean
  message: string
}

// ==================== Live 直播 ====================

export interface LiveStatus {
  is_streaming: boolean
  is_starting?: boolean
  is_cancelling?: boolean
  recovery_blocked?: string
  current_zone: string
  /**
   * **权威有效时长**：服务端已确认（平台连续回报在播）的累计秒数。
   * 不含待确认区间；同一个字段不会有时代表已确认值、有时代表墙钟总时长。
   */
  elapsed_seconds: number
  remaining_seconds: number
  room_id: number
  duration_seconds?: number
  is_anomaly?: boolean
  stream_mode?: string  // 'task' | 'manual' | ''
  ffmpeg_active?: boolean
  ffmpeg_current_video?: string
  pending_face_verify?: boolean
  face_verify_url?: string
  /** 最近一次可信观察之后的待确认区间（秒）：只可**有界**外推显示。 */
  pending_seconds?: number
  /** 还能继续外推多久（秒）；0 表示页面必须冻结在已确认值上。 */
  pending_valid_seconds?: number
  /** 单段待确认区间上限（页面外推的硬边界）。 */
  pending_limit_seconds?: number
  /** 服务端是否允许对这段待确认区间做显示外推。 */
  pending_extrapolatable?: boolean
  /** 'running' | 'paused_closed' | 'paused_unknown' | 'paused_blocked' */
  timer_state?: string
  phase?: string
  run_id?: string
  session_version?: number
  boot_id?: string
  duration_known?: boolean
  backend_events?: BackendEvent[]
}

export interface BackendEvent {
  tag: string
  type: 'success' | 'danger' | 'warning' | 'info'
  message: string
  time: string
}

export interface StartLiveResponse {
  success: boolean
  room_id?: number
  message: string
  need_face_verification?: boolean
  qr_data?: string
}

export interface StopLiveResponse {
  success: boolean
  message: string
}

export interface RunNextResponse {
  success: boolean
  message: string
}

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
 * 别的面板），旧载荷带着同一个 id 照写就会抹掉刚提交的完成进度。版本与业务日
 * 必须在**弹框之前**与 id 一起取，不能在发请求时现取——现取等于把用户确认的
 * 那份状态悄悄换成最新的。
 */
export interface OverwriteTarget {
  id: number
  expectedRevision: number
  businessDate: string
}

export interface TaskCreate {
  /** 覆盖（overwrite=true）时必填：被覆盖那一条记录的稳定身份。 */
  id?: number
  /** 覆盖请求的前置条件：确认时的 tasks_revision（界面渲染的那份快照）。 */
  expected_revision?: number
  /** 覆盖请求的前置条件：确认时的业务日（YYYY-MM-DD）。 */
  business_date?: string
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

export interface StartLiveRequest {
  zone_name?: string
  duration_seconds?: number  // 手动模式时长，0=不限时，上限86400
}

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
  email_face_verify_port: number
  // Server酱
  serverchan_sendkey: string
  // 服务器公网地址（域名或IP），留空自动检测
  duration_distribution: 'uniform' | 'normal' | 'beta'
  duration_multiplier_min: number
  duration_multiplier_max: number
}
