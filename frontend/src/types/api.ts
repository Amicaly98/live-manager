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
  current_zone: string
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

export interface TaskCreate {
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
  duration_distribution: 'uniform' | 'normal' | 'beta'
  duration_multiplier_min: number
  duration_multiplier_max: number
}
