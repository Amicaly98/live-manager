/**
 * executingTask.ts - "哪一条任务正在执行"的唯一判定（2026-09-28 工作包 C；
 * 阶段 4C 迁移到新投影）。
 *
 * 任务页可能比控制台更早打开，也可能从未打开过控制台，因此它必须能**只凭
 * 一份服务端投影**就标出正在执行的那一条，而不依赖任何页面间的传递。
 *
 * 判定只用**稳定身份** `run.task_id`：
 * - 分区名会被删除重建、被改名，同名分区不等于同一条任务；
 * - 单次（`mode='single'`）没有任务身份，不能冒充任务在执行；
 * - `step='wait_tasks'`（用户在跑但暂时没有可执行任务）**不是**某条任务在跑。
 *
 * 阶段标签也**只描述已确认的事实**：开播确认中/恢复中/直播中/停止收尾中/
 * 连接中。未知状态返回空串，让页面继续显示"今日完成/待执行"，绝不把未知伪装成
 * "待执行"，也不把未知当成停播。
 */

import type { SessionProjection } from '@/types/session'

/** 与本模块判定相关的那几个投影字段（便于测试注入最小形状）。 */
export interface ExecutingStatusLike {
  step?: string
  goal?: { wanted?: string } | null
  run?: { task_id?: number | null; mode?: string;
          clock?: { checkpoint_seconds?: number } } | null
  run_id?: string | null
  has_run?: boolean
  mode?: string
  waitingTasks?: boolean
}

/** 把投影归一成判定所需的形状（页面直接传投影即可）。 */
export function asExecutingLike(
  projection: SessionProjection | null | undefined,
): ExecutingStatusLike | null {
  if (!projection) return null
  return projection as unknown as ExecutingStatusLike
}

/** 本模块认定的"有活动步骤"：空闲、等待任务、以及还没有结论的空 step 都不算。 */
const INACTIVE_STEPS = new Set(['', 'idle', 'wait_tasks'])

/**
 * 正在执行的任务的稳定 id；没有则返回 null。
 *
 * 返回 null 的四种典型情形：没有场次、单次场次、等待任务、空闲/未知。
 * 它们都不允许被渲染成"某条任务正在执行"。
 */
export function resolveExecutingTaskId(
  status?: ExecutingStatusLike | null
): number | null {
  if (!status || typeof status !== 'object') return null
  const run = status.run
  if (!run) return null
  if (INACTIVE_STEPS.has(String(status.step || ''))) return null
  if (String(run.mode || '') && String(run.mode) !== 'task') return null
  return typeof run.task_id === 'number' ? run.task_id : null
}

/**
 * 正在执行的阶段标签；未在执行时返回空串。
 *
 * ``stale`` 表示"页面已失去新鲜投影"（浏览器↔服务器失联），此时只说"连接中"，
 * 不把未知当成停播，也不继续假装秒数在走。
 */
export function resolveExecutingLabel(
  status?: ExecutingStatusLike | null,
  stale: boolean = false,
  taskId?: number | null
): string {
  const current = resolveExecutingTaskId(status)
  if (current === null) return ''
  if (typeof taskId === 'number' && taskId !== current) return ''
  if (stale) return '连接中'
  const step = String(status?.step || '')
  switch (step) {
    case 'close_query':
    case 'close_stop':
    case 'close_check':
      return '停止收尾中'
    case 'resume_query':
      // 恢复等待首次确认：已有检查点就是"恢复中"，全新场才是"开播确认中"。
      return Number(status?.run?.clock?.checkpoint_seconds ?? 0) > 0
        ? '恢复中' : '开播确认中'
    case 'open':
    case 'choose':
      return '开播确认中'
    default:
      return '正在执行'
  }
}

/** 标签的展示色（'warning' 常规 / 'danger' 需人工处理 / 'info' 未知）。 */
export function resolveExecutingTagType(
  label: string
): 'warning' | 'danger' | 'info' {
  if (label === '需要人脸验证' || label === '恢复中') return 'danger'
  if (label === '连接中' || label === '等待任务') return 'info'
  return 'warning'
}
