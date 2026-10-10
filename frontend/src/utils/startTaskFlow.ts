/**
 * startTaskFlow.ts - "点哪条开哪条"的确认流程（阶段 4C 返修 R2）。
 *
 * 抽成纯流程模块的原因与旧版相同：确认框打开期间后台随时可能换场、换目标版本——
 * **绝不允许**在确认之后重读当前 Run/Goal/revision 再发请求，把新状态与旧的用户
 * 确认拼在一起。所有权规则只有一条，且必须在**第一次确认 UI 之前**完成：
 *
 *   目标身份（id + 版本 + 业务日）、原场上下文（run/goal/selection）、账号、推流
 *   来源，全部一次冻结成**一份完整的 `FrozenOperation`**（含客户端令牌）；此后
 *   弹框、重发、网络重试只消费这一份。
 *
 * 返修前的问题是"注释写冻结、执行位置在弹框之后"：`freezeStartTarget` 在模式选择
 * 弹窗结束之后才调用，`buildRestart` 回调还忽略流程传入的冻结 abandon 参数、
 * 改去读当前投影。于是"用户确认 A、实际放弃 B"能真实发生（复核 R2 的两条浏览器
 * 反例）。现在冻结与构建合并成一次 `freezePlan()`，**由调用方在打开任何确认 UI
 * 之前调用**；本模块只负责"确认 → 派发这一份"。
 *
 * 其余三点不变：
 * 1. **没有"再取一张票"这一步**：令牌在冻结那一刻由客户端生成（`api/liveControl.ts`）。
 * 2. **放弃与新开是同一项请求**：`restart-after-abandon` 里带上冻结的原场。
 * 3. **被拒绝不自动重来**：版本过期只会得到"请刷新后重新确认"。
 */
import type { FrozenOperation } from '@/api/liveControl'
import type { FrozenAbandonRef } from '@/stores/live'

/** 弹框之前冻结的**目标身份**（来自同一次任务列表快照）。 */
export interface FrozenTaskTarget {
  taskId: number
  expectedRevision: number
  businessDate: string
}

/** 弹框之前冻结的**原场上下文**（来自同一次投影快照）。 */
export type FrozenAbandonContext = FrozenAbandonRef

/**
 * **第一次确认 UI 之前**构建好的整份操作。
 *
 * `operation` 是唯一权威载荷；`abandon` 只用于生成确认文案，不参与建 body。
 */
export interface FrozenStartPlan {
  operation: FrozenOperation
  abandon: FrozenAbandonContext | null
  target: FrozenTaskTarget
  pushSource: 'ffmpeg' | 'obs'
}

export interface StartTaskFlowDeps {
  /**
   * 取回**已经冻结**的整份操作。调用方必须在打开任何确认 UI **之前**完成冻结
   * （`runSpecifiedTaskStart` 一进来就调用它，绝不放在弹框之后）。
   * 返回 null = 无法冻结（缺稳定身份等），流程不弹框、不发请求。
   */
  plan: () => FrozenStartPlan | null
  /** 真实控制入口（继续复用 `liveStore.dispatch`，不另建第二套请求）。 */
  start: (operation: FrozenOperation) => Promise<{
    superseded: boolean
    notice: { level: 'success' | 'warning' | 'error' | 'info'; message: string } | null
    status: string
  }>
  /** 结果反馈（ElMessage 注入口），缺省为空实现。 */
  notify?: (kind: 'success' | 'warning' | 'error' | 'info',
            message: string) => void
  /** 确认放弃旧场；resolve(true)=用户确认。只在 plan.abandon 非空时调用。 */
  confirmAbandon?: (info: { zone: string; day: string; targetTaskId: number })
    => Promise<boolean>
}

/**
 * 从指定任务开始任务模式。全程不发 `/clear-state`、不重建任务身份。
 *
 * 返回本次实际发出的**冻结操作**（没有发出则为 null），供调用方记录日志。
 */
export async function runSpecifiedTaskStart(
  deps: StartTaskFlowDeps,
): Promise<FrozenOperation | null> {
  const notify = deps.notify ?? (() => {})
  // 1) 取回**已经冻结**的整份操作。它由调用方在第一次确认 UI 之前构建好；
  //    这里再晚一步读 store 就等于把用户没看到的版本塞进已确认的请求。
  const plan = deps.plan()
  if (!plan) {
    notify('error', '无法冻结本次开播请求：任务身份或原场上下文缺失，请刷新后重试')
    return null
  }
  // 2) 有原场才多一次"放弃"确认；确认的是**已冻结**的那一份（文案与载荷同源）。
  if (plan.abandon) {
    const confirmed = deps.confirmAbandon
      ? await deps.confirmAbandon({ zone: plan.abandon.zoneName || '上一次任务',
                                    day: plan.abandon.businessDate || '未取得',
                                    targetTaskId: plan.target.taskId })
      : true
    if (!confirmed) {
      notify('info', '已取消，旧进度未做任何改动')
      return null
    }
  }
  // 3) 发出。冻结载荷已经是唯一的权威：这里不再重读任何 store 值。
  const result = await deps.start(plan.operation)
  if (result.superseded) return plan.operation  // 结果属于别的账号代次：不提示
  const notice = result.notice
  if (result.status === 'rejected') {
    // 版本过期一类：提示用户**重新查看并确认**，绝不用最新 revision 偷偷重发。
    // 已有具体原因时**带上它**（不覆盖、不另发一条互相矛盾的"已过期"）；
    // 回执没给原因时才用通用的过期文案。
    notify('warning', notice?.message
      ? `${notice.message}。请刷新任务列表后重新确认`
      : '这次确认已经过期（任务或目标有变化）。请刷新后重新确认。')
  } else if (notice) {
    notify(notice.level, notice.message)
  }
  return plan.operation
}

/** 从**已有**的冻结目标启动（例如不需要二次确认的场景）。不读任何 store。 */
export async function startWithFrozenOperation(
  deps: Pick<StartTaskFlowDeps, 'start' | 'notify'>,
  operation: FrozenOperation,
): Promise<FrozenOperation> {
  const result = await deps.start(operation)
  if (!result.superseded && result.notice) {
    const notify = deps.notify ?? (() => {})
    notify(result.notice.level, result.notice.message)
  }
  return operation
}
