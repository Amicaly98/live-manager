/**
 * 指定任务开播的确认流程（2026-09-28 收口，工作包 B 前端半场）。
 *
 * 核心规则：
 * 1. 目标在**任何弹框之前**冻结一次（id + expected_revision + business_date）；
 *    弹框后的任务列表刷新/后台结算都不得改它——用户确认的是他当时看到的那一条；
 * 2. 存在进行中的任务时，冲突事实来自**服务端状态快照**（不虚构数字），
 *    用户确认"覆盖"后携带完整冻结凭证（old_* 五元组 + confirm_id + stop_epoch）；
 * 3. 服务端 409（确认已过期/竞态）→ 重取快照 → 再次确认 → **复用同一个
 *    target** 重发：绝不重新冻结（那等于把用户确认的内容悄悄换掉）。
 */

export interface FrozenStartTarget {
  id: number
  expectedRevision: number
  businessDate: string
}

export interface AbandonConflictInfo {
  hasSaved: boolean
  oldRunId?: string
  oldTaskId?: number | null
  oldZoneName?: string
  oldExecutionDate?: string
  oldElapsedSeconds?: number
}

export interface StartOutcome {
  success: boolean
  message: string
  needFaceVerify?: boolean
  qrData?: string
  /** 服务端拒绝原因码（409 分类用）。 */
  refusal?: string
}

export interface AbandonSavedClaimBody {
  old_run_id?: string
  old_task_id?: number | null
  old_zone_name?: string
  old_execution_date?: string
  old_elapsed_seconds?: number
  confirm_id?: string
  stop_epoch?: number
}

export interface StartTaskFlowDeps {
  /** 冻结当前目标（任何弹框之前调用一次）。返回 null 表示目标不明确。 */
  freezeStartTarget: () => FrozenStartTarget | null
  /** 服务端状态快照：有进行中的任务时返回其完整身份，否则 null。 */
  fetchSavedState: () => Promise<AbandonConflictInfo | null>
  /** 发起指定开播。 */
  start: (target: FrozenStartTarget,
          abandonSaved?: AbandonSavedClaimBody) => Promise<StartOutcome>
  /** 冲突确认框：用户确认"覆盖旧进度"返回 true。 */
  confirmAbandon: (info: AbandonConflictInfo) => Promise<boolean>
  /** 当前控制代际（凭证里的 stop_epoch）。 */
  fetchControlEpoch: () => Promise<number | null>
  /** 一次性确认 id。 */
  newConfirmId: () => string
  /** 轻提示。 */
  notify: (message: string, kind?: 'info' | 'warning' | 'error') => void
}

/** 从服务端状态快照提取冲突事实（字段缺失按"未知"处理，不虚构数字）。 */
export function conflictInfoFromState(
  state: Record<string, unknown> | null | undefined,
): AbandonConflictInfo | null {
  if (!state || state.has_active !== true) return null
  return {
    hasSaved: true,
    oldRunId: typeof state.run_id === 'string' ? state.run_id : undefined,
    oldTaskId: typeof state.task_id === 'number' ? state.task_id : null,
    oldZoneName: typeof state.current_zone === 'string'
      ? state.current_zone : undefined,
    oldExecutionDate: typeof state.execution_date === 'string'
      ? state.execution_date : undefined,
    oldElapsedSeconds: typeof state.elapsed_seconds === 'number'
      ? state.elapsed_seconds : undefined,
  }
}

function claimFromConflict(
  info: AbandonConflictInfo,
  deps: Pick<StartTaskFlowDeps, 'newConfirmId' | 'fetchControlEpoch'>,
): AbandonSavedClaimBody | null {
  // 返回 null = 凭证事实不完整；调用方拒绝发送而不是猜。
  // 凭证必须完整（后端逐项核对，缺失/变化即 409）——不完整就拒绝发送而不是猜。
  if (!info.oldRunId || !info.oldTaskId
      || typeof info.oldElapsedSeconds !== 'number') {
    return null
  }
  return {
    old_run_id: info.oldRunId,
    old_task_id: info.oldTaskId,
    old_zone_name: info.oldZoneName,
    old_execution_date: info.oldExecutionDate,
    old_elapsed_seconds: info.oldElapsedSeconds,
    confirm_id: deps.newConfirmId(),
  }
}

/**
 * 运行一次"从这一行开始"的完整流程。
 * 目标冻结一次 → 查服务端快照 → 有保存进度先确认再带凭证开播；
 * 服务端 409 冲突时重取快照、重新确认、复用同一 target 重发。
 */
export async function runSpecifiedTaskStart(
  deps: StartTaskFlowDeps,
): Promise<StartOutcome> {
  // 1) 弹框之前冻结一次；之后的一切确认/重发复用它。
  const target = deps.freezeStartTarget()
  if (!target || !Number.isFinite(target.id)) {
    deps.notify('目标任务不明确：请刷新任务列表后重试', 'error')
    return { success: false, message: '目标不明确' }
  }

  let claim: AbandonSavedClaimBody | undefined
  const saved = await deps.fetchSavedState()
  if (saved) {
    const confirmed = await deps.confirmAbandon(saved)
    if (!confirmed) {
      deps.notify('已保留原任务进度', 'info')
      return { success: false, message: '已取消' }
    }
    claim = claimFromConflict(saved, deps) ?? undefined
    if (!claim) {
      deps.notify('无法确定旧进度凭证：请刷新任务列表后重试', 'warning')
      return { success: false, message: '凭证不完整' }
    }
    const epoch = await deps.fetchControlEpoch()
    claim.stop_epoch = epoch ?? undefined
  }

  const first = await deps.start(target, claim)
  if (first.success || first.needFaceVerify) return first
  // 409 竞态（检查与受理之间出现了进行中的任务/确认过期）：
  // 重取快照 → 重新确认 → **复用同一个 target** 重发。
  if (first.refusal === 'already_streaming'
      || first.message.includes('已有直播进行中')) {
    const conflict = await deps.fetchSavedState()
    if (!conflict) return first
    const confirmed = await deps.confirmAbandon(conflict)
    if (!confirmed) {
      deps.notify('已保留原任务进度', 'info')
      return { success: false, message: '已取消' }
    }
    const retryClaim = claimFromConflict(conflict, deps)
    if (!retryClaim) {
      deps.notify('无法确定旧进度凭证：请刷新任务列表后重试', 'warning')
      return { success: false, message: '凭证不完整' }
    }
    const retryEpoch = await deps.fetchControlEpoch()
    retryClaim.stop_epoch = retryEpoch ?? undefined
    return deps.start(target, retryClaim)
  }
  return first
}
