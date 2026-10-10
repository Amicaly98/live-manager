/**
 * live.ts - 本场运行事实的**唯一**前端持有者（桌面移植阶段 2 / 服务端阶段 4C）。
 *
 * 桌面适配点只有一处：**默认推流来源是本机 FFmpeg**（`startNextTask` 等的缺省
 * 参数）。服务器版把缺省写成 `obs`（多实例操作台按目标自己的设置推流），桌面
 * 没有操作台，推流方式只由设置页决定，缺省必须落到产品默认。
 *
 * 设计口径：
 *
 * 1. **唯一后端投影**：`projection` 是 GET `/api/live/status` 返回的那份只读投影
 *    的最近一次快照。前端不持有第二份 Goal/Run/Clock 权威，不结算、不跨日轮转、
 *    不改进度文件。
 * 2. **唯一应用路径** `applyStatus()`：所有状态写入（轮询 / 短观察 / 控制回执后的
 *    刷新）都经过它；它负责账号代次门、读取代次门与事件摄取。
 * 3. **状态读取单飞 + 可取消**：`refreshStatus()` 同时只有一个在途请求，离开页面
 *    或换账号时真正中止（abort），迟到响应没有应用资格。
 * 4. **显示秒表只画不做**：`display_seconds` 是服务端给的基数，
 *    `display_advancing` / `display_valid_seconds` 是服务端给的许可与**剩余**预算；
 *    浏览器只用 `performance.now()` 做绘制外推。**任何分支都不发 Stop/Start/Settle，
 *    也不写业务秒数。**
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { useRequest } from '@/api/request'
import { operationEvents } from '@/stores/operationEvents'
import {
  CONTROL_URLS, freezeOperation, sendControl,
  type ControlOutcome, type FrozenOperation,
} from '@/api/liveControl'
import type {
  AbandonRequest, AccountContext, ControlReceipt, FaceConfirmRequest, PushSource,
  SessionProjection, StartNamedTaskRequest, StartSingleRequest,
} from '@/types/session'

/**
 * 本地**单调耗时源**（毫秒）——绘制外推与"预算已经用掉多少"的唯一时间来源。
 *
 * 不能用 `Date.now()`：日历时间会被校时回拨，回拨一次就等于把已经用掉的显示预算
 * 还给页面（显示重新开始增长），也会把请求已经花掉的时间藏起来。
 * `performance.now()` 单调且与系统时间无关，正好是这里需要的语义。
 * 单位全程为毫秒；日历时间仍用于日志时间戳。
 */
let _monoFallbackMs = 0
let _monoFallbackSeen = false
function _monoFallback(): number {
  const wall = Date.now()
  if (!Number.isFinite(wall)) return _monoFallbackMs
  if (!_monoFallbackSeen || wall > _monoFallbackMs) {
    _monoFallbackMs = wall
    _monoFallbackSeen = true
  }
  return _monoFallbackMs
}
function monoNowMs(): number {
  const perf = (globalThis as { performance?: { now?: () => number } }).performance
  const fromPerf = perf && typeof perf.now === 'function' ? perf.now() : NaN
  if (Number.isFinite(fromPerf)) return fromPerf
  return _monoFallback()
}

/** 状态读取的总尝试预算：贯通传输层，一次轮询最多实际发出 3 次请求。 */
const STATUS_MAX_ATTEMPTS = 3
/** 有界观察：开播/停止/放弃后的收敛短轮询（最多 20 次 × 1 秒）。 */
const SETTLE_WATCH_MAX_TICKS = 20
const SETTLE_WATCH_INTERVAL_MS = 1000
/** 首次确认时"确认前显示值"的收敛时间（既有 45 秒口径）。 */
const FIRST_CONFIRM_CONVERGE_SEC = 45
/** 同一次首次确认允许保留的最大领先量（与后端展示预算同量级）。 */
const FIRST_CONFIRM_MAX_LEAD_SEC = 120

/** 一次控制操作的**冻结**载荷 + 它的展示承诺。 */
export interface DispatchResult {
  outcome: ControlOutcome
  /** 由调用方决定如何呈现；`null` = 无需提示（取消或被账号换代丢弃）。 */
  notice: { level: 'success' | 'warning' | 'error' | 'info'; message: string } | null
  /** 结果属于**另一个**账号代次，已被丢弃，不得写入当前界面。 */
  superseded: boolean
}

/**
 * 弹框**之前**冻结的一份"原场"上下文（阶段 4C 返修 R2）。
 *
 * `run_id` / `goal_revision` / `selection` 都不是前端能重建的东西：它们必须来自
 * 用户**看到弹框那一刻**的投影。弹框期间后台完全可能换了一整场（新 Run / 新
 * Goal），确认之后再去读当前投影就会把 B 的上下文塞进 A 的确认里。
 */
export interface FrozenAbandonRef {
  runId: string
  goalRevision: number
  /** 原场选择：**原样**取自后端投影，不从分区文案重建。 */
  selection: Record<string, unknown>
  zoneName: string
  businessDate: string
}

export const useLiveStore = defineStore('live', () => {
  const request = useRequest()

  // ==================== 唯一投影 ====================

  const projection = ref<SessionProjection | null>(null)
  /** 是否已经拿到过**至少一份**状态（首屏未收到任何响应时为 false）。 */
  const statusLoaded = ref(false)
  /** 最近一次状态读是否失败（浏览器↔服务器失联）：只影响提示，不影响服务器计时。 */
  const statusStale = ref(false)
  const isStarting = ref(false)
  const isStopping = ref(false)

  // ---- 账号代次：投影里的 (uid, room_id) 一变就前进 ----
  const accountGeneration = ref(0)
  let _accountKey = 'anon'

  function accountKeyOf(p: SessionProjection | null): string {
    const account = p?.account
    return account && account.uid > 0 ? `u${account.uid}:${account.room_id}` : 'anon'
  }

  // ==================== 显示秒表（只画） ====================

  /** 服务端给的展示基数（快照生成那一刻的 `display_seconds`）。 */
  let _serverSeconds = 0
  /** 快照的单调接收时刻——绘制外推的本地起点。 */
  let _receivedAtMonoMs = 0
  /** 服务端给的**剩余**外推预算（秒）；请求路上花掉的时间要扣掉。 */
  let _budgetSec = 0
  /** 这份许可属于哪个 Run，以及是否允许外推。 */
  let _displayRunId = ''
  let _mayAdvance = false
  /** 已经扣掉本次传输耗时的预算（同一份快照只授予一次）。 */
  let _displayArmedSnapshot: SessionProjection | null = null

  // ---- 首次确认的视觉 carry：确认前的显示值不直接跳回 0 ----
  let _preConfirmShownSec = 0
  let _preConfirmRunId = ''
  let _carryActive = false
  let _carryRunId = ''
  let _carryLeadSec = 0
  let _carryStartMonoMs = 0

  const displaySeconds = ref(0)

  function clearCarry(): void {
    _preConfirmShownSec = 0
    _preConfirmRunId = ''
    _carryActive = false
    _carryRunId = ''
    _carryLeadSec = 0
    _carryStartMonoMs = 0
  }

  function carryForDisplay(nowMs: number): number {
    if (!_carryActive) return 0
    const since = (nowMs - _carryStartMonoMs) / 1000
    const remaining = _carryLeadSec
      * Math.max(0, 1 - since / FIRST_CONFIRM_CONVERGE_SEC)
    return Math.max(0, remaining)
  }

  function budgetGrowth(nowMs: number): number {
    if (!_mayAdvance || _budgetSec <= 0) return 0
    return Math.min(Math.max(0, (nowMs - _receivedAtMonoMs) / 1000), _budgetSec)
  }

  /** 纯绘制函数：服务器基数 + 有界外推 + 首次确认 carry。不做任何业务动作。 */
  function readDisplaySeconds(nowMs: number = monoNowMs()): number {
    const value = _serverSeconds + budgetGrowth(nowMs) + carryForDisplay(nowMs)
    return Math.max(0, value)
  }

  /** 预算耗尽（服务端许可仍在，但页面已经用完了它的剩余量）→ 冻结并提示。 */
  const displayFrozen = computed(() => {
    // 依赖 displaySeconds：每一帧重画时同步重算，否则这个 computed 永远不更新。
    void displaySeconds.value
    if (!_mayAdvance) return false
    return budgetGrowth(monoNowMs()) >= _budgetSec
  })

  const confirmedSeconds = computed(
    () => Number(projection.value?.confirmed_seconds ?? 0) || 0)

  // ==================== 投影派生（页面只读这些） ====================

  const step = computed(() => String(projection.value?.step ?? ''))
  const goalWanted = computed(() => String(projection.value?.goal?.wanted ?? ''))
  const goalRevision = computed(
    () => Number(projection.value?.goal?.revision ?? 0) || 0)
  const runId = computed(() => String(projection.value?.run_id ?? ''))
  const taskId = computed(() => {
    const value = projection.value?.task_id
    return typeof value === 'number' ? value : null
  })
  const zoneName = computed(() => String(projection.value?.current_zone ?? ''))
  const sessionMode = computed(() => String(projection.value?.mode ?? ''))
  const pushSource = computed(() => String(projection.value?.push_source ?? ''))
  const hasRun = computed(() => projection.value?.has_run === true)
  const durationKnown = computed(() => projection.value?.duration_known === true)
  const targetSeconds = computed(
    () => Number(projection.value?.target_seconds ?? 0) || 0)
  const resumable = computed(() => projection.value?.resumable === true)
  const resumeBlockedReason = computed(
    () => String(projection.value?.resume_blocked_reason ?? ''))
  const face = computed(() => projection.value?.face ?? null)
  const backendEvents = computed(() => projection.value?.backend_events ?? [])
  const account = computed(() => projection.value?.account ?? null)

  /** 用户在跑但暂时没有可执行任务：**不是**用户停播，浏览器也不反复 Start。 */
  const waitingTasks = computed(
    () => goalWanted.value === 'running' && step.value === 'wait_tasks')
  /** 用户目标是在跑（含等待任务与切换过程中）。 */
  const running = computed(() => goalWanted.value === 'running')
  /** 平台已确认在播。 */
  const live = computed(() => step.value === 'live')
  /** 停止收尾中（目标已停止，房间尚未核对完成）。 */
  const closing = computed(
    () => goalWanted.value === 'stopped'
      && step.value.startsWith('close') && step.value !== '')
  /** 彻底空闲：目标停止且没有任何在途步骤。 */
  const idle = computed(() => goalWanted.value === 'stopped' && step.value === 'idle')

  /** 进度百分比：**已确认**秒数达标才算 100；外推最多画到 99。 */
  const progressPercent = computed(() => {
    const total = targetSeconds.value
    if (!hasRun.value || !Number.isFinite(total) || total <= 0) return 0
    if (confirmedSeconds.value >= total) return 100
    const shown = Number(displaySeconds.value) || 0
    if (!Number.isFinite(shown) || shown <= 0) return 0
    return Math.max(0, Math.min(99, Math.round((shown / total) * 100)))
  })

  /** 已确认秒数超过目标：只做展示提示，**不触发任何完成动作**。 */
  const overtime = computed(() => {
    const total = targetSeconds.value
    return hasRun.value && Number.isFinite(total) && total > 0
      && confirmedSeconds.value > total
  })

  /**
   * 计时区的一句话说明。全部来自投影事实，不新增术语：
   * - 失联只说"连接中"，不把未知当停播；
   * - `optimistic` 是"已派出 Start、等首次确认"；有已确认进度时才是"恢复中"；
   * - `frozen` 且有场次 = 可信检查点，不再外推。
   */
  const timerHint = computed(() => {
    const p = projection.value
    if (!p) return ''
    if (statusStale.value) return '连接中'
    if (p.display_source === 'frozen' && p.has_run && p.goal?.wanted === 'running') {
      return ''
    }
    if (p.display_source === 'optimistic') {
      return p.confirmed_seconds > 0 ? '恢复中' : '开播确认中'
    }
    return ''
  })

  // ==================== 状态读取（单飞 + 代次） ====================

  let _requestSeq = 0
  let _appliedSeq = 0
  let _pollInflight: Promise<SessionProjection | undefined> | null = null
  let _pollGeneration = 0
  let _statusAbort: AbortController | null = null
  let _eventPollTimer: ReturnType<typeof setInterval> | null = null
  let _tickTimer: ReturnType<typeof setInterval> | null = null

  /**
   * 所有状态响应的**唯一**应用入口。
   *
   * `generation` 是发起这次读取时捕获的读取代次：取消/离开页面之后晚到的响应
   * **没有应用资格**（判断在应用**之前**，不是在补救里）。
   */
  function applyStatus(next: SessionProjection, seq?: number, generation?: number): void {
    if (typeof generation === 'number' && generation !== _pollGeneration) return
    if (typeof seq === 'number') {
      if (seq < _appliedSeq) return      // 后发先至的旧响应不得把状态拉回去
      _appliedSeq = seq
    }

    const previousShown = readDisplaySeconds()
    const nextAgeKey = accountKeyOf(next)
    const accountChanged = nextAgeKey !== _accountKey
    if (accountChanged) {
      _accountKey = nextAgeKey
      accountGeneration.value += 1
      // 换账号：显示锚点、视觉 carry、在途意图一律作废，绝不继承上一账号的秒表。
      clearCarry()
      _displayArmedSnapshot = null
      _displayRunId = ''
      _mayAdvance = false
      _serverSeconds = 0
      _budgetSec = 0
      cancelPendingIntents('account-changed')
    }
    const nextRunId = String(next.run_id ?? '')
    if (!accountChanged && nextRunId !== _displayRunId && _displayRunId !== '') {
      // 换场：同一账号内的新 Run 也不得继承上一场的显示。
      clearCarry()
      _displayArmedSnapshot = null
      _budgetSec = 0
    }

    projection.value = next
    statusStale.value = false
    statusLoaded.value = true

    // 近期操作日志的**唯一**摄取点（响应已通过代次/取消门）。
    try {
      operationEvents.ingestFromStatus(next.backend_events)
    } catch { /* 事件摄取失败不影响状态应用 */ }

    // ---- 绑定展示口径 ----
    _displayRunId = nextRunId
    _serverSeconds = Number(next.display_seconds) || 0
    _mayAdvance = next.display_advancing === true
    if (_displayArmedSnapshot !== next) {
      // 一份**新**快照 = 一次新的许可授予：重置绘制起点与预算。
      // 同一份快照被再次应用（同一对象）**不**重新授予——否则"重新应用"
      // 就能把已经用掉的预算还回来。
      _displayArmedSnapshot = next
      _receivedAtMonoMs = monoNowMs()
      _budgetSec = _mayAdvance ? Math.max(0, Number(next.display_valid_seconds) || 0) : 0
    }

    if (!_mayAdvance) {
      // unknown / closed / 停止 / End / 没有场次：立即采用服务器可信检查点，
      // 不保留历史最大值，也不继续外推。
      clearCarry()
    } else if (next.display_source === 'optimistic') {
      // 乐观阶段（Start 已派出、待首次确认）：记录"确认前最后显示值"，
      // 供首次确认时算 carry。
      _preConfirmRunId = nextRunId
      _preConfirmShownSec = Math.max(_preConfirmShownSec, previousShown)
    } else if (next.display_source === 'confirmed') {
      if (_preConfirmRunId === nextRunId && !_carryActive
          && _carryRunId !== nextRunId
          && _preConfirmShownSec > _serverSeconds) {
        _carryActive = true
        _carryRunId = nextRunId
        _carryStartMonoMs = monoNowMs()
        _carryLeadSec = Math.min(FIRST_CONFIRM_MAX_LEAD_SEC,
          _preConfirmShownSec - _serverSeconds)
      }
    }

    displaySeconds.value = readDisplaySeconds()
    startTick()
  }

  function startTick(): void {
    if (_tickTimer) return
    _tickTimer = setInterval(() => {
      // 只重画。不发请求、不做业务判断。
      displaySeconds.value = readDisplaySeconds()
    }, 1000)
  }

  function stopTick(): void {
    if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null }
  }

  /**
   * 获取直播状态（成功接收即应用，不依赖外部再调一次 tick）。
   *
   * `startedMonoMs` 在**发请求之前**取：它到应用时刻的本地耗时就是这份快照的年龄，
   * 用来把服务端给的**剩余**预算折算成"页面还能外推多少"。
   */
  async function fetchStatus(options: {
    signal?: AbortSignal; generation?: number; maxAttempts?: number
  } = {}): Promise<SessionProjection | undefined> {
    const seq = ++_requestSeq
    const startedMonoMs = monoNowMs()
    const generation = options.generation ?? _pollGeneration
    try {
      const res = await request.get<SessionProjection>('/api/live/status', undefined, {
        signal: options.signal,
        maxAttempts: options.maxAttempts ?? STATUS_MAX_ATTEMPTS,
      })
      if (generation !== _pollGeneration) return undefined
      if (options.signal?.aborted) return undefined
      const spentSec = Math.max(0, monoNowMs() - startedMonoMs) / 1000
      // 传输耗时从**剩余**预算里扣掉：迟到的快照不能凭空换来一段新的外推时间。
      if (res && typeof res.display_valid_seconds === 'number') {
        // 先在本地折一次；applyStatus 里再按快照身份只授予一次。
        applyStatus({
          ...res,
          display_valid_seconds: Math.max(0, Number(res.display_valid_seconds) - spentSec),
        }, seq, generation)
      } else {
        applyStatus(res, seq, generation)
      }
      return res
    } catch (error) {
      if (generation !== _pollGeneration) return undefined
      console.error('获取直播状态失败:', error)
      // 请求失败不等于停播：保留陈旧显示，绝不归零。但如实标记"已失去新鲜快照"，
      // 并且**不**重新授予预算（预算只能由服务器给的新快照续）。
      statusStale.value = true
      return undefined
    }
  }

  /** 轮询用的状态读：单飞 + 总预算 + 可取消。 */
  async function pollStatus(): Promise<SessionProjection | undefined> {
    if (_pollInflight) return _pollInflight
    const generation = _pollGeneration
    const controller = new AbortController()
    _statusAbort = controller
    const run = fetchStatus({
      signal: controller.signal, generation, maxAttempts: STATUS_MAX_ATTEMPTS,
    })
    _pollInflight = run
    try {
      return await run
    } finally {
      if (_pollInflight === run) _pollInflight = null
      if (_statusAbort === controller) _statusAbort = null
    }
  }

  /** 唯一的"刷新状态"入口：控制回执之后也走这里，不套回执快照。 */
  async function refreshStatus(): Promise<SessionProjection | undefined> {
    return pollStatus()
  }

  /**
   * 取消状态读（页面离开 / 登出 / 换账号）。
   *
   * 三件事一起做：作废旧响应（generation）、中止在途请求与退避（abort）、
   * 断开单飞引用（下一次订阅能立刻发起新读）。只做其中一件都会留下
   * "取消后页面仍被旧数据改写"或"重新进入后一直读到旧读的结果"。
   */
  function stopStatusReads(): void {
    _pollGeneration += 1
    const controller = _statusAbort
    _statusAbort = null
    _pollInflight = null
    if (controller) {
      try { controller.abort('status-reads-stopped') } catch { /* 已完成则忽略 */ }
    }
  }

  function startEventPolling(intervalMs = 30000): void {
    if (_eventPollTimer) return
    _eventPollTimer = setInterval(() => {
      void pollStatus().catch(() => { /* silent */ })
    }, intervalMs)
  }

  function stopEventPolling(): void {
    if (_eventPollTimer) { clearInterval(_eventPollTimer); _eventPollTimer = null }
  }

  // ==================== 有界收敛观察 ====================

  let _settleTimer: ReturnType<typeof setTimeout> | null = null
  let _settleToken = 0
  const watching = ref(false)

  function stopSettleWatch(): void {
    _settleToken += 1
    watching.value = false
    if (_settleTimer) { clearTimeout(_settleTimer); _settleTimer = null }
  }

  /**
   * 受理之后的有界观察：控制入口只回"已受理"，真实状态要靠读收敛。
   *
   * 判据全部来自投影（不是本地猜测），最多 20 次 × 1 秒；停止会取消**此前**的
   * 观察，但不会取消之后用户新发起的那一次。
   */
  function beginSettleWatch(settled: () => boolean,
                            maxTicks = SETTLE_WATCH_MAX_TICKS): void {
    stopSettleWatch()
    watching.value = true
    const token = ++_settleToken
    let ticks = 0
    const stepOnce = async () => {
      if (token !== _settleToken) return
      ticks += 1
      await pollStatus()
      if (token !== _settleToken) return
      if (settled() || ticks >= maxTicks) { stopSettleWatch(); return }
      _settleTimer = setTimeout(stepOnce, SETTLE_WATCH_INTERVAL_MS)
    }
    _settleTimer = setTimeout(stepOnce, 300)
  }

  // ==================== 在途意图（Stop 取消的是"此前"的启动类操作） ====================

  const _intents = new Set<AbortController>()

  function _newIntent(): AbortController {
    const controller = new AbortController()
    _intents.add(controller)
    return controller
  }

  function cancelPendingIntents(reason = 'stop'): void {
    for (const controller of Array.from(_intents)) {
      try { controller.abort(reason) } catch { /* 已结束则忽略 */ }
    }
    _intents.clear()
  }

  /** 停止/放弃是**取消语义**：先撤掉此前尚未完成的启动类意图。 */
  function _cancelStartIntents(reason: string): void {
    cancelPendingIntents(reason)
    stopSettleWatch()
  }

  // ==================== 操作冻结（确认之前取值，之后只复用） ====================

  function accountContext(): AccountContext | null {
    const account = projection.value?.account
    if (!account || account.uid <= 0 || account.room_id <= 0) return null
    // 只取 uid/room_id：把带 display_name 的展示对象整份回传会被 DTO 的
    // extra='forbid' 拒绝。
    return { uid: account.uid, room_id: account.room_id }
  }

  function freezeStart(body: Record<string, unknown>, label: string): FrozenOperation {
    return freezeOperation(CONTROL_URLS.start, withAccount(body),
                           accountGeneration.value, label)
  }

  function withAccount(body: Record<string, unknown>): Record<string, unknown> {
    const context = accountContext()
    return context ? { ...body, account_context: context } : { ...body }
  }

  /** 自动任务（服务端按当前业务日排序选取）。任务身份与推流来源**独立**。 */
  function buildAutoTaskOp(pushSource: PushSource): FrozenOperation {
    return freezeStart({ mode: 'task', push_source: pushSource }, '开始下一任务')
  }

  /**
   * **指定任务**：`task_id / expected_revision / business_date` 必须来自
   * **同一次任务列表快照**，并且在弹窗之前冻结好。
   */
  function buildNamedTaskOp(target: {
    taskId: number; expectedRevision: number; businessDate: string
  }, pushSource: PushSource): FrozenOperation {
    const body: StartNamedTaskRequest = {
      mode: 'task', task_id: target.taskId,
      expected_revision: target.expectedRevision,
      business_date: target.businessDate, push_source: pushSource,
    }
    return freezeStart({ ...body }, '指定任务开播')
  }

  /** 单次（界面称"手动模式"）。`targetSeconds === 0` = 不限时。 */
  function buildSingleOp(payload: {
    zoneName: string; durationSeconds: number; areaId?: number
  }, pushSource: PushSource): FrozenOperation {
    const body: StartSingleRequest = {
      mode: 'single', zone_name: payload.zoneName,
      target_seconds: Math.max(0, Math.floor(payload.durationSeconds)),
      push_source: pushSource,
    }
    if (typeof payload.areaId === 'number') body.area_id = payload.areaId
    return freezeStart({ ...body }, '单次开播')
  }

  /** 恢复原场：必须携带**用户看到的那一份** run_id 与 goal_revision。 */
  function buildResumeOp(source: {
    runId: string; goalRevision: number
  }): FrozenOperation {
    const body = withAccount({
      run_id: source.runId, goal_revision: source.goalRevision,
    })
    return freezeOperation(CONTROL_URLS.resume, body, accountGeneration.value, '恢复直播')
  }

  /**
   * 冻结当前投影里的"原场"上下文（`null` = 当前没有**已停止但保留**的场次）。
   *
   * 必须在**任何确认弹框之前**调用一次；弹框之后的确认只允许消费这份返回值，
   * 不再回读投影（复核 R2）。
   */
  function freezeAbandonRef(p: SessionProjection | null): FrozenAbandonRef | null {
    const run = p?.run
    const goal = p?.goal
    if (!run || !goal || p?.resumable !== true) return null
    return {
      runId: String(run.id),
      goalRevision: Number(goal.revision),
      selection: { ...(goal.selection ?? {}) },
      zoneName: String(run.zone_name || ''),
      businessDate: String(run.business_day || ''),
    }
  }

  /**
   * 放弃原场：**只**接受已经冻结的三件套。
   *
   * 参数是值而不是"去读 store"：调用方无法在确认之后"顺手"读一份更新的 Run/Goal
   * 拼进这份已经给用户看过的载荷。
   */
  function buildAbandonOp(ref: FrozenAbandonRef,
                          label = '放弃原场'): FrozenOperation {
    const body: AbandonRequest = {
      run_id: ref.runId, goal_revision: ref.goalRevision,
      selection: { ...ref.selection },
    }
    return freezeOperation(CONTROL_URLS.abandon, body, accountGeneration.value, label)
  }

  /**
   * 放弃并一步新开：**一项**请求同时携带**冻结的**原场上下文与新选择。
   * `startBody` 与 `ref` 都必须由调用方在确认框之前取好（见 §5.4）。
   */
  function buildRestartAfterAbandonOp(startBody: Record<string, unknown>,
                                      ref: FrozenAbandonRef,
                                      label = '放弃并重新开播'): FrozenOperation {
    const body = withAccount({
      ...startBody,
      abandon: {
        run_id: ref.runId, goal_revision: ref.goalRevision,
        selection: { ...ref.selection },
      },
    })
    return freezeOperation(CONTROL_URLS.restartAfterAbandon, body,
                           accountGeneration.value, label)
  }

  /**
   * 人脸确认：**冻结**当刻的 goal_revision / uid / room_id。
   * 弹窗打开时取一次，提交时不再读当前投影补齐。
   */
  function buildFaceConfirmOp(frozen: FaceConfirmRequest): FrozenOperation {
    return freezeOperation(CONTROL_URLS.confirmFace, { ...frozen },
                           accountGeneration.value, '人脸确认')
  }

  /** 停止：随时可达；不携带必需字段（可带冻结账号上下文）。 */
  function buildStopOp(): FrozenOperation {
    return freezeOperation(CONTROL_URLS.stop, withAccount({}),
                           accountGeneration.value, '停止直播')
  }

  // ==================== 派发（唯一执行路径） ====================

  /**
   * 派发一次**已冻结**的操作。
   *
   * - 结果属于另一个账号代次 → 丢弃（不写新账号界面、不提示）；
   * - 回执：只说明受理/待确认/拒绝，**不**在这一层宣告"已开播/已停止"；
   * - 之后一律走 `refreshStatus()` 的单一 GET 路径（不把回执快照当状态）；
   * - 无论回执还是传输未知，都**不**自动换新令牌再次 Start。
   */
  async function dispatch(frozen: FrozenOperation): Promise<DispatchResult> {
    // 派发**之前**的账号代次门：确认框期间换了账号，这份为旧账号冻结的操作
    // 连发都不发（"不得发向新账号"）。迟到的结果同样按代次丢弃。
    if (frozen.accountGeneration !== accountGeneration.value) {
      return { outcome: { kind: 'cancelled' }, notice: null, superseded: true }
    }
    const isStop = frozen.url === CONTROL_URLS.stop
    if (isStop) { isStopping.value = true } else { isStarting.value = true }
    const intent = _newIntent()
    try {
      const outcome = await sendControl(frozen, { signal: intent.signal })
      if (frozen.accountGeneration !== accountGeneration.value) {
        return { outcome, notice: null, superseded: true }
      }
      const notice = noticeForOperation(outcome, frozen)
      await refreshStatus()
      if (outcome.kind === 'receipt'
          && (outcome.receipt.accepted || outcome.receipt.status === 'pending')) {
        beginSettleWatch(() => (isStop ? settleStopped() : settleStarted()))
      }
      return { outcome, notice, superseded: false }
    } finally {
      _intents.delete(intent)
      isStarting.value = false
      isStopping.value = false
    }
  }

  function settleStarted(): boolean {
    return live.value || waitingTasks.value
  }

  function settleStopped(): boolean {
    return idle.value
  }

  function noticeForOperation(outcome: ControlOutcome, frozen: FrozenOperation)
    : DispatchResult['notice'] {
    if (outcome.kind === 'cancelled') return null
    if (outcome.kind === 'unknown') {
      return { level: 'warning', message: outcome.message }
    }
    const receipt: ControlReceipt = outcome.receipt
    const reason = receipt.reason || ''
    switch (receipt.status) {
      case 'accepted':
        // 受理回执不弹提示：受理≠成功，真实结果由随后的状态收敛表达；
        // 失败时状态自然退回原状（服务端事实为准），不再先报"已受理"。
        return null
      case 'pending':
        return { level: 'info', message: `${frozen.label || '控制'}仍在处理中，结果待确认` }
      case 'ignored':
        return { level: 'warning',
                 message: `该输入与当前事实无关，未改变任何状态${reason ? `（${reason}）` : ''}` }
      case 'token_conflict':
        return { level: 'error', message: '同一操作令牌已用于不同请求，请重新操作' }
      case 'rejected':
      default:
        return { level: 'error',
                 message: `未被受理${reason ? `：${reason}` : ''}` }
    }
  }

  // ==================== 便捷操作 ====================

  /**
   * 桌面默认推流来源：**本机 FFmpeg**。
   *
   * 推流方式只由设置页决定（`manual` → obs/自行推流，其余 → ffmpeg）；面板不另
   * 设来源选择，因此这里不能硬编码 `obs`（那会让"立即开播"在没有 OBS 的机器上
   * 按错误的来源开工）。调用方（页面）显式传入时以传入值为准。
   */
  async function startNextTask(pushSource: PushSource = 'ffmpeg') {
    return dispatch(buildAutoTaskOp(pushSource))
  }

  async function startNamedTask(
    target: { taskId: number; expectedRevision: number; businessDate: string },
    pushSource: PushSource = 'ffmpeg') {
    return dispatch(buildNamedTaskOp(target, pushSource))
  }

  async function startSingle(
    payload: { zoneName: string; durationSeconds: number; areaId?: number },
    pushSource: PushSource = 'ffmpeg') {
    return dispatch(buildSingleOp(payload, pushSource))
  }

  async function resumeLive() {
    const p = projection.value
    if (!p?.run || !p.goal) {
      return { outcome: { kind: 'cancelled' } as ControlOutcome,
               notice: { level: 'error' as const, message: '当前没有可恢复的原场' },
               superseded: false }
    }
    return dispatch(buildResumeOp({
      runId: String(p.run.id), goalRevision: Number(p.goal.revision),
    }))
  }

  async function abandonRun(ref: FrozenAbandonRef) {
    return dispatch(buildAbandonOp(ref))
  }

  /** 放弃并重新开播：`startBody` 与 `ref` 都必须已经冻结（见 §5.4）。 */
  async function restartAfterAbandon(startBody: Record<string, unknown>,
                                     ref: FrozenAbandonRef) {
    return dispatch(buildRestartAfterAbandonOp(startBody, ref))
  }

  async function stopLive() {
    _cancelStartIntents('user-stop')
    return dispatch(buildStopOp())
  }

  async function confirmFace(frozen: FaceConfirmRequest) {
    return dispatch(buildFaceConfirmOp(frozen))
  }

  /** 登出/换账号时由 App 调用：停掉页面轮询与本地绘制。 */
  function teardown(): void {
    stopEventPolling()
    stopStatusReads()
    stopSettleWatch()
    stopTick()
  }

  return {
    // 唯一投影与读取状态
    projection, statusLoaded, statusStale, accountGeneration,
    refreshStatus, pollStatus, fetchStatus, stopStatusReads,
    startEventPolling, stopEventPolling, applyStatus,
    // 在途标记与观察
    isStarting, isStopping, watching, beginSettleWatch, stopSettleWatch,
    cancelPendingIntents,
    // 派生事实（页面只读）
    step, goalWanted, goalRevision, runId, taskId, zoneName, sessionMode,
    pushSource, hasRun, durationKnown, targetSeconds, resumable,
    resumeBlockedReason, face, backendEvents, account,
    waitingTasks, running, live, closing, idle, timerHint, displayFrozen,
    // 显示秒表（只画）
    displaySeconds, confirmedSeconds, progressPercent, overtime,
    // 操作
    accountContext, buildAutoTaskOp, buildNamedTaskOp, buildSingleOp,
    buildResumeOp, freezeAbandonRef, buildAbandonOp, buildRestartAfterAbandonOp,
    buildFaceConfirmOp, buildStopOp,
    dispatch, startNextTask, startNamedTask, startSingle, resumeLive,
    abandonRun, restartAfterAbandon, stopLive, confirmFace, teardown,
  }
})
