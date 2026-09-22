import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { useRequest } from '@/api/request'
import { operationEvents } from '@/stores/operationEvents'
import type {
  LiveStatus,
  StartLiveResponse,
  StopLiveResponse,
  StartLiveRequest,
} from '@/types/api'

/**
 * 本地**单调耗时源**（毫秒）——外推有效期的唯一时间来源（2026-09-20 监督 E3）。
 *
 * 为什么不能继续用 `Date.now()`：外推窗口的"已消耗量"由**本地耗时**推导，这个值必须
 * 只增不减。日历时间会被校时（NTP 校准、用户改表、时区/夏令时调整）回拨，回拨一次
 * 就等于把已经用掉的窗口还给页面（显示重新开始增长），也会把请求已经花掉的时间藏起来
 * （一条早就过期的快照被当成新鲜的，重新领到一整段外推）。`performance.now()` 单调且
 * 与系统时间无关，正好是这里需要的语义。
 *
 * 兜底路径只在宿主没有 `performance.now()` 时启用（浏览器与 Node 都有，正常走不到）：
 * 退回墙钟但**单向钳制**（只取历史最大值），保证它仍然单调不减——宁可少走也不倒退，
 * 因此回拨同样既还不了窗口、也藏不了请求耗时。
 *
 * 单位全程为毫秒。日历时间仍用于日志/时间戳（如 `backend_events` 的显示时间），
 * 只是不参与外推有效期。
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

export const useLiveStore = defineStore('live', () => {
  const status = ref<LiveStatus>({
    is_streaming: false,
    current_zone: '',
    elapsed_seconds: 0,
    remaining_seconds: 0,
    room_id: 0,
    is_anomaly: false,
    ffmpeg_active: false,
    ffmpeg_current_video: '',
  })
  const isStarting = ref(false)
  const isStopping = ref(false)

  // 会话身份与计时锚点（跨页面持久）
  // 计时模型（2026-09-20 起）：显示值 = **服务端已确认的有效时长** +
  // **有界外推的待确认区间**。计数器仍然靠"锚点 + 当前耗时"插值（后台标签页
  // 节流会让 setInterval++ 越走越慢），但这里的"当前耗时"取的是**单调耗时源**
  // （`monoNowMs()`）而不是日历时间：系统校时既不能让外推倒退重来，也不能把
  // 请求的真实耗时藏起来。外推量被服务端给出的窗口夹住：
  //   - pending_extrapolatable=false 或窗口用尽 → 立刻冻结在已确认值上；
  //   - 浏览器失联时窗口照样会走完，因此失联后自然冻结，不需要额外的本地判断。
  // 已确认值在同一会话内只增不减：待确认段被撤回时显示可以小幅回退，
  // 但永远不会退到 0。
  const localElapsed = ref(0)
  const fixedTotal = ref(0)
  /** 目标时长是否已知：未知（null）≠ 不限时（0）。 */
  const durationKnown = ref(false)
  /** 会话身份：同一 run_id 才允许继承/校正，旧响应不得覆盖新会话。 */
  const sessionId = ref('')
  const tickActive = ref(false)
  /** 最近一次状态读是否失败（浏览器↔服务器失联）：只影响提示与冻结，不影响服务器计时。 */
  const statusStale = ref(false)
  let tickTimer: ReturnType<typeof setInterval> | null = null
  // 显示锚点是否已建立。**用布尔量、不用 0 值判断**：单调时钟的起点完全可以是 0
  // （测试注入的时钟就常常从 0 起算），把 0 当成"未初始化"会让外推永远停在第 0 秒。
  let _displayArmed = false
  let _confirmedAtAnchor = 0        // 锚点时刻的**已确认**有效时长（秒）
  let _pendingAtAnchor = 0          // 锚点时刻的待确认区间（秒）
  let _sessionConfirmedMax = 0      // 本会话见过的最大已确认值（防旧响应把进度拉回去）
  // 外推窗口的**授予**（2026-09-20 监督 E2）：
  // `pending_valid_seconds` 是"**快照生成那一刻**还剩多少可以外推"，不是"收到快照时
  // 还剩多少"。响应在路上花掉的时间（含 Axios 重试与退避）必须从窗口里扣掉，否则一条
  // 迟到的响应会凭空换来一段全新的外推时间，页面会在窗口早已耗尽时继续往前跳。
  // 窗口一旦授予就只减不增：已消耗的量单独记账，因此
  //   - 同一缓存快照被重复应用（重复 applyStatus / syncFromServer）不会重新授予；
  //   - 计时器重启（startLocalTick 重设锚点）不会把用掉的窗口还回来。
  // 这里涉及的三处时间（请求起点、窗口起点、窗口消耗）全部走 `monoNowMs()`
  // （单调耗时源，见文件顶部说明），没有任何一处使用日历时间。
  let _armedSnapshot: unknown = null  // 窗口是为哪一份快照授予的（按响应式对象身份）
  let _windowArmed = false            // 是否已授予过窗口（同样**不用 0 值判断**）
  let _windowBudgetSec = 0            // 这份快照授予的外推窗口（秒，已扣除请求耗时）
  let _windowStartMonoMs = 0          // 窗口的本地起算时刻（单调毫秒，**只在重新授予时更新**）
  let _eventPollTimer: ReturnType<typeof setInterval> | null = null

  // ---- 状态一致性协议 ----
  // 1) 请求序号：同一页面内向服务端发出的状态读顺序。晚发出的请求已经应用过
  //    更新的快照，此时**先发出但后返回**的旧响应必须丢弃（否则进度会回零）。
  // 2) 服务身份 + 快照版本：服务重启（boot_id 变）或同一 run 的旧 session_version
  //    同样按迟到处理。
  // 3) 单飞：轮询/事件/启动观察共用同一个在途读，慢请求不会叠成请求风暴。
  const serverBootId = ref('')
  const snapshotVersion = ref(0)
  let _requestSeq = 0
  let _appliedSeq = 0
  let _pollInflight: Promise<LiveStatus | undefined> | null = null
  let _pollGeneration = 0
  /** 状态读的**总尝试预算**：贯通传输层，最多实际发出 3 次。 */
  const STATUS_MAX_ATTEMPTS = 3
  /** 在途状态读的取消器：取消要同时中止请求与退避，而不只是丢弃结果。 */
  let _statusAbort: AbortController | null = null
  // 启动观察（已受理 → 真正开播）的有界刷新
  const START_WATCH_MAX_TICKS = 20
  const START_WATCH_INTERVAL_MS = 1000
  let _startWatchTimer: ReturnType<typeof setTimeout> | null = null
  let _startWatchToken = 0
  const startWatching = ref(false)

  const request = useRequest()

  // 后台事件轮询（跨页面持续运行）：只负责**驱动** pollStatus；事件的摄取
  // 统一发生在 applyStatus 受控路径里（2026-09-21 工作包 A），这里不再有
  // 第二条写 localStorage 的路径。
  function startEventPolling() {
    if (_eventPollTimer) return
    _eventPollTimer = setInterval(async () => {
      try {
        // 走**同一个**状态入口：事件轮询取得的状态快照不再被丢掉，
        // 迟到的响应同样受请求序号约束。
        await pollStatus()
      } catch { /* silent */ }
    }, 30000)
  }

  function stopEventPolling() {
    if (_eventPollTimer) { clearInterval(_eventPollTimer); _eventPollTimer = null }
  }

  /**
   * 为一份快照授予外推窗口（**只减不增**）。
   *
   * ``startedMonoMs``：这次逻辑请求的**单调**发起时刻（含重试/退避的总耗时由它界定）。
   * 只用本地单调耗时差值，不假设浏览器与服务端时钟同步；同一份快照对象再次进来时
   * 不重新授予。
   */
  function armWindow(snapshot: unknown, startedMonoMs?: number) {
    if (!snapshot || snapshot === _armedSnapshot) return
    const s = snapshot as {
      pending_valid_seconds?: number, pending_extrapolatable?: boolean,
    }
    _armedSnapshot = snapshot
    _windowArmed = true
    _windowStartMonoMs = monoNowMs()
    const raw = Number(s.pending_valid_seconds ?? 0)
    if (s.pending_extrapolatable !== true || !Number.isFinite(raw) || raw <= 0) {
      // 服务端没给窗口（暂停/冻结）：立刻封住，不许外推。
      _windowBudgetSec = 0
      return
    }
    // 请求起点 → 应用时刻的本地耗时。单调时钟保证这个差值不会因校时被抹掉
    // （回拨会把它变小甚至变负，等于把过期快照当成新鲜的），因此不存在"夹成 0"的兜底。
    const spentSec = (typeof startedMonoMs === 'number'
                      && Number.isFinite(startedMonoMs))
      ? Math.max(0, monoNowMs() - startedMonoMs) / 1000
      : 0
    _windowBudgetSec = Math.max(0, raw - spentSec)
  }

  /**
   * 这份快照已经用掉的外推秒数 = 从**窗口起算时刻**到现在的单调耗时，被窗口夹住。
   *
   * 关键是它不看显示锚点：计时器重启（重设锚点）不会把它清零，因此"重启计时器"换不来
   * 新的外推时间。窗口用尽后恒等于上限，页面自然冻结；校时前后也取不到更大的值。
   */
  function windowConsumedSec() {
    if (!_windowArmed || _windowBudgetSec <= 0) return 0
    const elapsed = Math.max(0,
      Math.floor((monoNowMs() - _windowStartMonoMs) / 1000))
    return Math.min(_windowBudgetSec, elapsed)
  }

  /**
   * 所有状态响应的**唯一**应用入口。
   *
   * 只要服务端真的回了快照就立刻更新计时显示（不等下一次轮询），并按会话
   * 身份决定"继承 / 重置 / 拒绝旧响应"：
   * - 同一 run_id：校正锚点，不打断正在推进的计时；
   * - 换 run_id（新会话）：重建锚点；
   * - 响应里没有 run_id 但正在推流：按分区名兜底，不做无谓重置。
   *
   * ``startedMonoMs``：请求的**单调**发起时刻，用来把窗口折算成"剩余有效期"（见 armWindow）。
   */
  function applyStatus(res: LiveStatus, seq?: number, startedMonoMs?: number) {
    const incoming = res as LiveStatus & {
      run_id?: string, duration_known?: boolean,
      duration_seconds?: number, elapsed_seconds?: number,
      remaining_seconds?: number | null, phase?: string,
      boot_id?: string, session_version?: number,
      pending_seconds?: number, pending_valid_seconds?: number,
      pending_extrapolatable?: boolean, timer_state?: string,
    }
    // 迟到响应：晚发出的请求已经应用过更新的快照，这个更旧的必须丢弃。
    // 旧实现只在注释里写"拒绝旧响应"，实际没有任何序号判断，两次并发读
    // 只要后发先至就会把进度倒回旧会话。
    if (typeof seq === 'number') {
      if (seq < _appliedSeq) return
      _appliedSeq = seq
    }
    const boot = String(incoming.boot_id || '')
    if (boot) serverBootId.value = boot
    const nextSession = incoming.run_id || ''
    const incomingVersion = Number(incoming.session_version)
    if (nextSession && nextSession === sessionId.value
        && Number.isFinite(incomingVersion)) {
      if (incomingVersion < snapshotVersion.value) return  // 同一会话的旧版本快照
      snapshotVersion.value = incomingVersion
    }
    const incomingElapsed = Number(incoming.elapsed_seconds ?? 0)
    const known = incoming.duration_known === true

    const newSession = Boolean(nextSession) && nextSession !== sessionId.value
    if (newSession) {
      sessionId.value = nextSession
      snapshotVersion.value = Number.isFinite(incomingVersion) ? incomingVersion : 0
      _sessionConfirmedMax = 0
    }
    status.value = res
    statusStale.value = false
    durationKnown.value = known

    // 近期操作日志的**唯一摄取点**（2026-09-21 工作包 A）：能走到这里的
    // 响应都已通过代际/取消/会话版本门，backend_events 立即并入响应式
    // 事件 store——旧实现要再等一个独立 30 秒定时器写 localStorage、
    // 再等页面轮询读缓存，首屏日志因此固定延迟半分钟。
    try {
      const be = (incoming as { backend_events?: unknown }).backend_events
      if (be) operationEvents.ingestFromStatus(be)
    } catch { /* 事件摄取失败不影响状态应用 */ }

    if (known) {
      const dur = Number(incoming.duration_seconds ?? 0)
      fixedTotal.value = Number.isFinite(dur) && dur >= 0 ? dur : 0
    } else {
      // 未知时长：不猜测，也不把 0 显示成"不限时"
      fixedTotal.value = 0
    }

    if (incoming.is_streaming) {
      // 已确认值在同一会话内只增不减：晚到的旧快照即使序号更"新"，
      // 也不能把用户看到的已确认进度拉回去。
      const confirmedRaw = Number.isFinite(incomingElapsed) && incomingElapsed >= 0
        ? incomingElapsed : 0
      const confirmed = Math.max(confirmedRaw, _sessionConfirmedMax)
      _sessionConfirmedMax = confirmed
      const pendingRaw = Number(incoming.pending_seconds ?? 0)
      _pendingAtAnchor = Number.isFinite(pendingRaw) && pendingRaw > 0 ? pendingRaw : 0
      // 窗口按"剩余有效期"授予：请求已经花掉的时间不再重复授予。
      // 这里传 `status.value`（Vue 对同一对象缓存同一个响应式代理）而不是原始 `res`：
      // 之后 startLocalTick / syncFromServer 读到的也是 `status.value`，身份必须一致，
      // 否则同一份缓存快照会被误判成新快照、把窗口重新给满。
      armWindow(status.value, startedMonoMs)
      _confirmedAtAnchor = confirmed
      _displayArmed = true
      localElapsed.value = _confirmedAtAnchor + _pendingAtAnchor + windowConsumedSec()
      if (!tickActive.value) startLocalTick(true)
      else refreshFromAnchor()
    } else if (!incoming.is_starting) {
      // 真的停了（不是"正在开播"）才清显示；"停止中"保留现场
      localElapsed.value = 0
      fixedTotal.value = 0
      sessionId.value = ''
      _sessionConfirmedMax = 0
      _confirmedAtAnchor = 0
      _pendingAtAnchor = 0
      _armedSnapshot = null
      _windowArmed = false
      _windowBudgetSec = 0
      _windowStartMonoMs = 0
      stopLocalTick(false)
    }
  }

  function refreshFromAnchor() {
    if (!_displayArmed) return
    const shown = _confirmedAtAnchor + _pendingAtAnchor + windowConsumedSec()
    // 已确认进度不得归零；待确认段允许小幅回退（服务端撤回时）。
    localElapsed.value = Math.max(_confirmedAtAnchor, shown)
  }

  /**
   * 获取直播状态（成功接收即应用，不依赖外部再调一次 tick）。
   *
   * ``generation``/``signal`` 由轮询方传入：取消之后**晚到的响应不得应用**。
   * 判断放在 applyStatus **之前**（不是在里面补救）——旧实现只清 Promise 引用，
   * 被取消的在途请求照样把旧快照写进页面。
   *
   * ``startedMonoMs`` 在**发请求之前**取（单调时钟）：它到应用时刻的本地耗时（含传输层
   * 重试与退避）就是这份快照的年龄，用来把外推窗口折算成"剩余有效期"。用单调时钟而不是
   * 日历时间，是为了让校时既不能把这段年龄抹掉、也不能把它虚增。
   */
  async function fetchStatus(options: { signal?: AbortSignal;
                                        generation?: number;
                                        maxAttempts?: number } = {}) {
    const seq = ++_requestSeq
    const startedMonoMs = monoNowMs()
    const generation = options.generation ?? _pollGeneration
    try {
      const res = await request.get<LiveStatus>('/api/live/status', undefined, {
        signal: options.signal,
        maxAttempts: options.maxAttempts ?? STATUS_MAX_ATTEMPTS,
      })
      if (generation !== _pollGeneration) return undefined
      if (options.signal?.aborted) return undefined
      applyStatus(res, seq, startedMonoMs)
      return res
    } catch (error) {
      console.error('获取直播状态失败:', error)
      // 请求失败不等于停播：保留陈旧显示，绝不归零（离线时页面不应显示已停止）。
      // 但要把"页面已失去新鲜快照"记下来，供短状态提示使用；服务器计时不受影响。
      statusStale.value = true
      return undefined
    }
  }

  /**
   * 轮询用的状态读：单飞 + 总尝试预算 + 可取消。
   *
   * - 单飞：同时只有一个在途请求，慢请求不会叠加；
   * - 总预算：**下沉到传输层**（``maxAttempts``），一次轮询最多实际发出
   *   STATUS_MAX_ATTEMPTS 次请求。旧实现外层限 3 次、内层 Axios 无限重放，
   *   叠加后一次轮询真的发了 15 次；
   * - 取消：stopStatusReads() 中止在途请求与退避等待，并让响应失去应用资格。
   */
  async function pollStatus(): Promise<LiveStatus | undefined> {
    if (_pollInflight) return _pollInflight
    const generation = _pollGeneration
    const controller = new AbortController()
    _statusAbort = controller
    const run = fetchStatus({
      signal: controller.signal,
      generation,
      maxAttempts: STATUS_MAX_ATTEMPTS,
    })
    _pollInflight = run
    try {
      return await run
    } finally {
      if (_pollInflight === run) _pollInflight = null
      if (_statusAbort === controller) _statusAbort = null
    }
  }

  /**
   * 取消状态读（页面离开 / 订阅取消）。
   *
   * 三件事一起做：作废旧响应（generation）、中断在途请求与退避（abort）、
   * 断开单飞引用（下一次订阅能立刻发起新读）。只做其中一件都会留下"取消后
   * 页面仍被旧数据改写"或"重新进入后一直读到旧读的结果"。
   */
  function stopStatusReads() {
    _pollGeneration += 1
    const controller = _statusAbort
    _statusAbort = null
    _pollInflight = null
    if (controller) {
      try { controller.abort('status-reads-stopped') } catch { /* 已完成则忽略 */ }
    }
  }

  /**
   * 开播/恢复"已受理但未真正开始"期间的有限、可取消刷新。
   *
   * 旧实现只在 start 返回后 GET 一次，若那一刻仍在 starting，就要等默认 30 秒
   * 轮询才看到真正的进度与目标时长。这里做有界（最多 20 次 × 1 秒）的短轮询，
   * 一旦确认已经在播就交给常规轮询；停止会取消**此前**的观察，但不会取消之后
   * 用户新发起的那一次。
   */
  function stopStartWatch() {
    _startWatchToken += 1
    startWatching.value = false
    if (_startWatchTimer) { clearTimeout(_startWatchTimer); _startWatchTimer = null }
  }

  function beginStartWatch() {
    stopStartWatch()
    startWatching.value = true
    const token = ++_startWatchToken
    let ticks = 0
    const step = async () => {
      if (token !== _startWatchToken) return
      ticks += 1
      await pollStatus()
      if (token !== _startWatchToken) return
      if (status.value.is_streaming && !status.value.is_starting) {
        stopStartWatch()
        return
      }
      if (ticks >= START_WATCH_MAX_TICKS) {
        stopStartWatch()
        return
      }
      _startWatchTimer = setTimeout(step, START_WATCH_INTERVAL_MS)
    }
    _startWatchTimer = setTimeout(step, 300)
  }

  // 在途控制意图的取消器：
  // 停止必须能中止尚在取票/重试中的 start / resume / run-next / 验证后恢复，
  // 否则慢取票完成后请求会带着新代际票到达，被服务端当成用户新意图执行，
  // 复活已经被取消的操作。
  const _intents = new Set<AbortController>()

  function _newIntent() {
    const controller = new AbortController()
    _intents.add(controller)
    return controller
  }

  /** 取消此前**所有**尚未完成的启动类意图（停止时调用）。 */
  function cancelPendingIntents(reason = 'stop') {
    for (const controller of Array.from(_intents)) {
      try {
        controller.abort(reason)
      } catch { /* 已结束则忽略 */ }
    }
    _intents.clear()
  }

  function isAbortError(error: unknown): boolean {
    const candidate = error as { code?: string; name?: string } | null
    return candidate?.code === 'ERR_CANCELED'
      || candidate?.name === 'CanceledError'
      || candidate?.name === 'AbortError'
  }

  // 开始直播（zoneName 非空=手动模式，空=任务模式）
  async function startLive(zoneName?: string, durationSeconds?: number) {
    isStarting.value = true
    const startAbort = _newIntent()
    try {
      const body: StartLiveRequest = zoneName
        ? { zone_name: zoneName, duration_seconds: durationSeconds }
        : {}
      const res = await request.post<StartLiveResponse>('/api/live/start', body,
        { signal: startAbort.signal })
      if (res.success) {
        await pollStatus()
        // "已受理"不等于"已开播"：启动有界短轮询，真正开播后立刻显示进度。
        beginStartWatch()
        return { success: true, message: res.message, needFaceVerify: false, qrData: '' }
      }
      return {
        success: false,
        message: res.message,
        needFaceVerify: res.need_face_verification || false,
        qrData: res.qr_data || '',
      }
    } catch (error: unknown) {
      if (isAbortError(error)) {
        return { success: false, message: '已取消开播', needFaceVerify: false, qrData: '' }
      }
      const msg = error instanceof Error ? error.message : '开播失败'
      return { success: false, message: msg, needFaceVerify: false, qrData: '' }
    } finally {
      _intents.delete(startAbort)
      isStarting.value = false
    }
  }

  // 恢复进行中的任务（与 start 共用同一套在途意图管理）
  async function resumeLive() {
    isStarting.value = true
    const abort = _newIntent()
    try {
      const res = await request.post<StartLiveResponse>('/api/live/resume',
        undefined, { signal: abort.signal })
      if (res.success) {
        await pollStatus()
        beginStartWatch()
        return { success: true, message: res.message, needFaceVerify: false, qrData: '' }
      }
      return {
        success: false,
        message: res.message,
        needFaceVerify: res.need_face_verification || false,
        qrData: res.qr_data || '',
      }
    } catch (error: unknown) {
      if (isAbortError(error)) {
        return { success: false, message: '已取消恢复', needFaceVerify: false, qrData: '' }
      }
      const msg = error instanceof Error ? error.message : '恢复失败'
      return { success: false, message: msg, needFaceVerify: false, qrData: '' }
    } finally {
      _intents.delete(abort)
      isStarting.value = false
    }
  }

  // 执行下一个任务（同样受停止取消约束）
  async function runNext() {
    isStarting.value = true
    const abort = _newIntent()
    try {
      const res = await request.post<{ success: boolean; message?: string }>(
        '/api/live/run-next', undefined, { signal: abort.signal })
      await pollStatus()
      if (res?.success) beginStartWatch()
      return res
    } catch (error: unknown) {
      if (isAbortError(error)) {
        return { success: false, message: '已取消执行', }
      }
      const msg = error instanceof Error ? error.message : '执行任务失败'
      return { success: false, message: msg }
    } finally {
      _intents.delete(abort)
      isStarting.value = false
    }
  }

  // 停止直播
  async function stopLive() {
    isStopping.value = true
    // 用户明确的停止意图：中止**此前**所有尚未完成的启动类意图
    // （开播/恢复/下一任务/验证后重试，含取票等待与传输重试）。
    // 之后用户新发起的意图不受影响——它们会拿到新的控制器。
    cancelPendingIntents('user-stop')
    // 只取消**此前**的启动观察：之后用户新发起的开播会重新 beginStartWatch。
    stopStartWatch()
    try {
      const res = await request.post<StopLiveResponse>('/api/live/stop')
      if (res.success) {
        // 后端返回的是“已接收”，清理在后台线程进行。这里不能立即清空状态，
        // 否则面板会在推流仍在收尾时误报已停止并放行冲突操作；改由状态轮询收敛。
        status.value.is_cancelling = true
        await pollStatus()
      }
      return res
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '停止失败'
      return { success: false, message: msg }
    } finally {
      isStopping.value = false
    }
  }

  // 本地计时器：内部只有一个，用 ref 暴露存活状态（普通 let 不会被 setup
  // store 的返回对象更新，外部判断会永远读到初始值）。
  function startLocalTick(keepAnchor = false) {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
    if (!keepAnchor) {
      // 从状态快照重建锚点（已确认值只增不减）。
      // 注意这里读的是 `status.value`——很可能就是刚才授予过窗口的那份快照，
      // 因此 armWindow 会直接返回：**重启计时器不会重新授予窗口**，已消耗的量
      // 也保留着，页面不会因为重启而多涨一段。
      const s = status.value as LiveStatus & {
        pending_seconds?: number, pending_valid_seconds?: number,
        pending_extrapolatable?: boolean,
      }
      const confirmed = Number(s.elapsed_seconds) || 0
      _confirmedAtAnchor = Math.max(_confirmedAtAnchor, confirmed)
      _sessionConfirmedMax = Math.max(_sessionConfirmedMax, _confirmedAtAnchor)
      const pending = Number(s.pending_seconds ?? 0)
      _pendingAtAnchor = Number.isFinite(pending) && pending > 0 ? pending : 0
      armWindow(s)
      _displayArmed = true
      localElapsed.value = _confirmedAtAnchor + _pendingAtAnchor + windowConsumedSec()
      const dur = Number((status.value as any).duration_seconds ?? 0)
      fixedTotal.value = Number.isFinite(dur) && dur > 0 ? dur : 0
    }
    tickActive.value = true
    tickTimer = setInterval(() => {
      if (status.value.is_streaming && !status.value.is_anomaly) {
        refreshFromAnchor()
      }
    }, 1000)
  }

  function stopLocalTick(clearDisplay = true) {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
    tickActive.value = false
    if (clearDisplay) {
      localElapsed.value = 0
      fixedTotal.value = 0
      durationKnown.value = false
      _confirmedAtAnchor = 0
      _pendingAtAnchor = 0
      _armedSnapshot = null
      _windowArmed = false
      _windowBudgetSec = 0
      _windowStartMonoMs = 0
      _displayArmed = false
    }
  }

  /**
   * 兼容旧调用：用**新鲜权威快照**校正显示锚点。
   *
   * 注意不能再用"|本地 - 服务端| > 2 就拉回"的判据：本地值本来就合法地领先于
   * 已确认值（它包含有界外推的待确认区间），那样会把正在推进的显示按回已确认值。
   * 只有服务端已确认值**超过了**本地已确认值时，才重建锚点向前校正。
   */
  function syncFromServer() {
    if (!status.value.is_streaming) return
    const s = status.value as LiveStatus & {
      pending_seconds?: number, pending_valid_seconds?: number,
      pending_extrapolatable?: boolean,
    }
    const serverConfirmed = Number(s.elapsed_seconds || 0)
    const dur = Number((status.value as any).duration_seconds ?? 0)
    if (Number.isFinite(dur) && dur > 0) fixedTotal.value = dur
    if (!Number.isFinite(serverConfirmed) || serverConfirmed <= 0) return
    if (serverConfirmed > _confirmedAtAnchor
        || serverConfirmed > _sessionConfirmedMax) {
      const pending = Number(s.pending_seconds ?? 0)
      _pendingAtAnchor = Number.isFinite(pending) && pending > 0 ? pending : 0
      // 同一份快照再次同步：不重新授予窗口，也不清空已消耗的量。
      armWindow(s)
      _confirmedAtAnchor = Math.max(serverConfirmed, _sessionConfirmedMax)
      _sessionConfirmedMax = _confirmedAtAnchor
      _displayArmed = true
      refreshFromAnchor()
    }
  }

  /**
   * 异常时的短状态文案（复用页面已有位置，不新增术语/弹窗）。
   * 正常直播返回空串，由页面显示原有的"直播进度"。
   */
  const timerHint = computed(() => {
    const s = status.value
    if (!s.is_streaming) return ''
    if (statusStale.value) return '连接中'
    if ((s as any).recovery_blocked || (s as any).phase === 'recovering') return '恢复中'
    const ts = String((s as any).timer_state || '')
    if (ts === 'paused_closed') return '已暂停'
    if (ts === 'paused_blocked') return '恢复中'
    if (ts === 'paused_unknown') return '连接中'
    return ''
  })

  return {
    status,
    isStarting,
    isStopping,
    localElapsed,
    fixedTotal,
    durationKnown,
    sessionId,
    // 计时器存活状态用 ref 暴露：外部据此判断是否已启动，不再依赖普通变量。
    tickActive,
    // 页面是否已失去新鲜快照（浏览器↔服务器失联）；只影响提示，不影响服务器计时。
    statusStale,
    timerHint,
    applyStatus,
    fetchStatus,
    pollStatus,
    stopStatusReads,
    refreshFromAnchor,
    serverBootId,
    snapshotVersion,
    startWatching,
    beginStartWatch,
    stopStartWatch,
    startLive,
    resumeLive,
    runNext,
    stopLive,
    cancelPendingIntents,
    startLocalTick,
    stopLocalTick,
    syncFromServer,
    startEventPolling,
    stopEventPolling,
  }
})
