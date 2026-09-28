/**
 * "正在执行"任务的稳定判定与阶段标签（2026-09-28 收口，工作包 C）。
 *
 * 为什么不能按分区名比较：分区名可被删除重建，同名任务会互相冒充；
 * 只有任务库主键（`task_id`）才是稳定身份。手动模式没有任务身份，
 * 任何行都不得被打上"正在执行"。
 */

export interface ExecutingStatusView {
  is_streaming?: boolean
  is_starting?: boolean
  is_cancelling?: boolean
  pending_face_verify?: boolean
  stream_mode?: string
  /** 当前执行任务的稳定 id（推迟接管窗口内也是目标任务自己的 id）。 */
  current_task_id?: number | null
  /** FFmpeg 任务模式：平台已开房但本地媒体尚未确认推进。 */
  takeover_pending?: boolean
  recovery_blocked?: string | boolean
}

/**
 * 当前"正在执行"的任务 id；不满足条件一律返回 null。
 * - 手动模式（stream_mode !== 'task'）→ null：零任务标签；
 * - 不在播/未受理/停止中之外的空转状态 → null；
 * - 已结束任务（后端不再报当前 id）→ null。
 */
export function resolveExecutingTaskId(
  status: ExecutingStatusView | null | undefined,
): number | null {
  if (!status) return null
  if (status.stream_mode && status.stream_mode !== 'task') return null
  const active = Boolean(
    status.is_streaming || status.is_starting
    || status.is_cancelling || status.pending_face_verify)
  if (!active) return null
  const id = status.current_task_id
  return typeof id === 'number' && Number.isFinite(id) ? id : null
}

/**
 * 执行阶段标签；传入的 taskId 与当前执行任务不同时返回空串（不误标）。
 * 顺序即优先级：待验证 → 正在停止 → 开播中 → 恢复中 → 正在执行。
 */
export function resolveExecutingLabel(
  status: ExecutingStatusView | null | undefined,
  taskId: number | null | undefined,
): string {
  if (!status) return ''
  if (resolveExecutingTaskId(status) !== taskId) return ''
  if (status.pending_face_verify) return '待验证'
  if (status.is_cancelling) return '正在停止'
  if (status.is_starting || status.takeover_pending) return '开播中'
  if (status.recovery_blocked) return '恢复中'
  return '正在执行'
}

/** 标签颜色（element-plus tag type）。 */
export function resolveExecutingTagType(
  status: ExecutingStatusView | null | undefined,
  taskId: number | null | undefined,
): 'warning' | 'info' | 'danger' | 'success' {
  if (status?.pending_face_verify
      && resolveExecutingTaskId(status) === taskId) return 'danger'
  return 'warning'
}
