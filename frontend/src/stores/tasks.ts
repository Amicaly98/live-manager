/**
 * tasks.ts - 任务列表的唯一前端持有者（桌面移植阶段 2 / 服务端 M0A–M1）。
 *
 * 桌面适配点：**没有**多实例聚合、跨实例同步创建与账号目录读取
 * （`/api/instances/*`）。任务数据仍然按 UID 归属——写请求携带发起时冻结的
 * `X-Account-UID`，创建走"一致预检 → 冻结 → 明确确认 → 一份不可变命令 →
 * 持久回执"，回包丢失后按**原 UID + 原令牌**核对。
 *
 * 本机已迁移账号的只读筛选不在前端做：分区历史只查**请求所属账号**的库
 * （`/api/tasks/history`），不联网聚合、也不接受任意 UID 参数绕过绑定。
 */
import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { useRequest } from '@/api/request'
import type { AttemptLostNotifier } from '@/api/request'
import { useAuthStore } from '@/stores/auth'
import cache from '@/composables/useCache'
import type {
  TaskItem,
  TaskDetail,
  TaskStats,
  TaskListResponse,
  TaskDetailResponse,
  TaskCreate,
  TaskUpdate,
  OverwriteTarget,
  MarkDoneResponse,
  ReloadResponse,
  ImportResult,
  CreationPreview,
  FrozenCreationPreview,
  CreateTaskResponse,
  CreationReceiptResponse,
  ZoneHistoryResponse,
} from '@/types/api'

/**
 * 一次任务写操作的**冻结账号上下文**（M0A / A16）。
 *
 * 由视图层在"用户选定操作、**第一个确认 UI 之前**"取一次（见
 * `views/TaskManager.vue` 的调用点），此后：
 *
 * - 请求与它的传输层重试都带同一份 UID（`X-Account-UID`）；
 * - 服务端据此**明确拒绝**"旧页面/旧请求落到新账号"的操作，绝不替它补一个
 *   最新账号后执行；
 * - 回包回到页面时再核一次（`writeScopeIsCurrent`），旧账号的响应既不刷新
 *   也不覆写新账号的展示与缓存。
 */
export type FrozenWriteScope = {
  uid: number | null
  /** 冻结时的业务日：人工完成按"用户看到的那一天"结算，不按回包时的当前日。 */
  businessDate: string
}

export const useTaskStore = defineStore('tasks', () => {
  const tasks = ref<TaskItem[]>([])
  const taskDetails = ref<TaskDetail[]>([])
  const stats = ref<TaskStats>({ total: 0, pending_total: 0, completed: 0, today_done: 0, today_pending: 0, remaining_time: 0, avg_remaining: 0, urgency: 0 })
  const isLoading = ref(false)
  /** 服务端快照版本：后台完成/结算会推进它，前端据此决定是否重取列表。 */
  const tasksRevision = ref(-1)
  /** 列表渲染时的业务日：写请求带上它，重放才不会作用到次日。 */
  const businessDate = ref('')

  const request = useRequest()
  // 任务列表是受保护读取的**唯一数据所有者**：认证意图不允许时（未登录 /
  // 退出中 / 退出后），这里不发任务读、不写回任务状态（S3，2026-09-21）。
  const auth = useAuthStore()

  // ==================== 只读请求的可取消上下文（S3，2026-09-21） ====================
  //
  // - `_readGeneration`：所有权代际。登出 / 面板失效 / 认证意图切换时由
  //   `stopTaskReads()` 前进一步；**写回之前**核对代际——即便底层取消没有及时
  //   生效，旧代响应也不能写 tasks/stats/revision/businessDate/loading。
  // - `_readControllers`：在途读的取消登记。`stopTaskReads()` 真正 abort 它们
  //   （含退避等待），不是只把响应藏起来。
  // - `_readInflight` / `_slotController`：/api/tasks 的单飞合并——同一代里
  //   **普通**重复调用共用同一次读取，重复的业务激活不会叠加请求。
  // - `_readSeq`（写回次序门，F1，2026-09-21 第三轮监督）：每次启动读取取一个
  //   递增序号，**只有最新启动的读**有权写回。为什么需要：带 fresh 的刷新
  //   （写入确认成功 / 版本提示已前进）会取消在途读并立即补一轮新读，被取消
  //   的读的响应仍可能迟到（对端已发出、取消没来得及生效、替身无视 signal）；
  //   它与新读同代，只按代际门控挡不住"新结果之后旧响应倒退"，次序门让它连
  //   落地资格都没有。
  let _readGeneration = 0
  let _readInflight: Promise<void> | null = null
  let _slotController: AbortController | null = null
  let _readSeq = 0
  const _readControllers = new Set<AbortController>()

  // ==================== 账号身份绑定（M0A，A16） ====================
  //
  // 任务数据属于 **UID**（同机共享账号库按 UID 分库），因此列表/统计/业务日/
  // localStorage 缓存都必须绑定"它们是哪个账号的数据"：
  //
  // - `_accountUid`：当前内存里这份任务数据所属的 UID。发起读取时定格，写回前
  //   复核——换账号之后，迟到的旧账号响应没有落地资格（除代际/次序门外多一道
  //   身份门，因为它们挡的是"同一账号内的新旧"，挡不住"跨账号"）；
  // - `_accountKey()`：未登录时为 null。账号切换（含 A→B、A→未登录、未登录→B）
  //   都会触发 `resetForAccount`：中止在途读、前进代际、清空内存展示并删除
  //   localStorage 缓存——否则 B 在读取失败时会从缓存里读到 A 的任务。
  let _accountUid: number | null = null
  let _accountBound = false

  function _accountKey(): number | null {
    const uid = auth.userInfo?.uid
    return typeof uid === 'number' && uid > 0 ? uid : null
  }

  /**
   * 一次写操作的**冻结账号上下文**（M0A / A16）。
   *
   * 由视图层在"用户选定操作、**第一个确认 UI 之前**"取一次（见
   * `views/TaskManager.vue` 的调用点），此后不再重取：
   *
   * - 请求与它的传输层重试都带同一份 UID（`X-Account-UID`）；
   * - 服务端据此**明确拒绝**"旧页面/旧请求落到新账号"的操作，绝不替它补一个
   *   最新账号后执行；
   * - 回包回到页面时再核一次（`writeScopeIsCurrent`），旧账号的响应既不刷新
   *   也不覆写新账号的展示与缓存。
   */
  function freezeWriteScope(): FrozenWriteScope {
    return { uid: _accountKey(), businessDate: businessDate.value }
  }

  /** 冻结的账号上下文是否仍是**当前**账号（迟到的回包据此决定还属不属于本页面）。 */
  function writeScopeIsCurrent(scope?: FrozenWriteScope): boolean {
    return (scope?.uid ?? null) === _accountKey()
  }

  const _EMPTY_STATS: TaskStats = {
    total: 0, pending_total: 0, completed: 0, today_done: 0, today_pending: 0,
    remaining_time: 0, avg_remaining: 0, urgency: 0,
  }

  /**
   * 换账号：先清旧账号的任务/进度展示与缓存，再由后续读取加载当前绑定。
   *
   * 顺序是承重的：**先清**再让新账号的读取落地，不能让 A 的列表停留在界面上
   * 直到 B 的首个响应到达；B 的读取失败时也不得回落到 A 的缓存。
   */
  function resetForAccount(uid: number | null): void {
    _accountUid = uid
    _accountBound = true
    stopTaskReads()
    tasks.value = []
    taskDetails.value = []
    stats.value = { ..._EMPTY_STATS }
    tasksRevision.value = -1
    businessDate.value = ''
    cache.remove('tasks_cache')
  }

  // 认证身份变化（登录成功 / 退出 / 服务端校准）→ 按新身份重置。
  //
  // 首次绑定**不重置**：页面刚装载、身份还没读到时清空是多余动作，也会把
  // "同一账号刷新页面后短暂读取失败时的缓存兜底"一并抹掉。真正要挡的是
  // **身份发生变化**（A→B、A→未登录、未登录→B）。
  // `flush: 'sync'` 是**承重**的：身份一旦落到 store 上，旧账号的展示与缓存必须
  // 在**同一刻**被清掉。默认的 `pre` 会把重置排进微任务队列，而启动路径是
  // "身份落地 → 业务加载起步"，重置可能晚于首个 `/api/tasks` 发出 —— 那样这次
  // 读取会被自己的重置作废（代际前进），任务表停在空列表。
  watch(() => _accountKey(), (uid) => {
    if (!_accountBound) {
      _accountBound = true
      _accountUid = uid
      return
    }
    if (uid === _accountUid) return
    resetForAccount(uid)
  }, { immediate: true, flush: 'sync' })


  function _readsAllowed(): boolean {
    // App 的业务加载只在启动结论 loggedIn=true 时启动；这里挡的是"写请求收尾的
    // 附带读取"、"定时器残留的刷新"这类绕过 App 开关的入口。
    return auth.isLoggedIn
  }

  /**
   * 停止任务只读请求（登出 / 面板失效 / 认证意图切换 / 所有者销毁）。
   *
   * 三件事一起做：代际前进一步（旧响应失去写回资格）、abort 全部在途读与退避、
   * 清掉单飞引用与挂起的加载态。只做其中一件都会留下"取消后仍被旧数据改写"
   * 或"重新登录后一直读到旧读的结果"。
   */
  function stopTaskReads() {
    _readGeneration += 1
    _readInflight = null
    _slotController = null
    const controllers = Array.from(_readControllers)
    _readControllers.clear()
    for (const controller of controllers) {
      try { controller.abort('task-reads-stopped') } catch { /* 已完成则忽略 */ }
    }
    isLoading.value = false
  }

  // 计算当前应该执行的任务
  const nextTask = computed(() => {
    return tasks.value.find(t => t.needs_execution)
  })

  /**
   * 取消槽内在途读（F1：需要更鲜数据的刷新到来时）。
   *
   * 只 abort 不等它结束：它的响应即便仍会到达（对端已发出 / 取消没来得及
   * 生效），也会被写回次序门拦下——所以我们不必为它等待或再作补偿。
   */
  function _cancelSlotRead(): void {
    const controller = _slotController
    _readInflight = null
    _slotController = null
    if (controller) {
      try { controller.abort('superseded-by-fresh-refresh') } catch { /* 已完成则忽略 */ }
    }
  }

  /**
   * 获取任务列表。
   *
   * - 普通读取（默认）：同一代已有在途读取就合并（单飞），不叠加请求。
   * - `fresh: true`：**需要更鲜数据**的刷新——本地任务写入确认成功之后的
   *   收尾刷新、后台 stats 已确认较新版本之后的重取。在途读开始于本次失效
   *   之前，无论返回什么都不能满足承诺：取消之并立即补一轮新读（旧响应由
   *   次序门拦下）。每次失效至多引发一次补读 → 有界，不会为每个编辑/定时器
   *   事件无限叠加请求；失败也不自动开新刷新循环（失败只恢复缓存，是否补读
   *   仍由后续真实事件决定）。
   *
   * （F1，2026-09-21 第三轮监督：此前只按认证代际合并，写入/版本提示之后
   * 的刷新复用了写入前的旧 GET，界面停留在旧值却报告刷新完成。）
   */
  async function fetchTasks(options?: { silent?: boolean; fresh?: boolean }): Promise<void> {
    if (!_readsAllowed()) return
    if (options?.fresh && _readInflight) {
      _cancelSlotRead()
    }
    return _startRead(options)
  }

  async function _startRead(options?: { silent?: boolean }): Promise<void> {
    if (!_readsAllowed()) return
    // 同一代已有在途读取就合并（普通重复读取的单飞）；作废/取消后这里为
    // null，会开一次新读。
    if (_readInflight) return _readInflight
    const silent = options?.silent === true
    const generation = _readGeneration
    const seq = ++_readSeq
    // 这次读取属于发起那一刻的账号身份；换账号后（resetForAccount 会前进代际）
    // 旧响应既过不了代际门、也过不了身份门。
    const accountUid = _accountKey()
    const controller = new AbortController()
    _readControllers.add(controller)
    _slotController = controller
    // 加载态责任交接（P2，2026-09-21 第四轮监督）：
    // - 可见读点亮 loading，并负责在收尾时释放（成功/失败都走 finally）。
    // - 静默读**不无故点亮**；但若启动时 loading 已被点亮（fresh 接替取消的
    //   正是那次可见读），就必须**继承**这份责任——否则旧读被次序门拦住不能
    //   清、静默读又因 !silent 不清，遮罩永远挂着（监督复现：数据已更新到
    //   新版本，isLoading 却恒为 true，任务表被遮罩盖住无法操作）。
    // - 责任在启动时定格为本次读取的局部量：被取代的旧读即使迟到，seq 门
    //   让它连"清掉更晚可见读取的 loading"的资格都没有；登出路径由
    //   stopTaskReads 直接收尾（代际已前进，在途读的 finally 不再插手）。
    const ownsLoading = !silent || isLoading.value
    if (!silent) isLoading.value = true
    const run = (async () => {
      try {
        const res = await request.get<TaskListResponse & {
          revision?: number
          business_date?: string
        }>('/api/tasks', undefined, { signal: controller.signal })
        // 写回门控：旧代响应、已被更新读取代的响应、或换了账号的响应，
        // 一律不得落地（后两者分别由代际门与身份门挡下）。
        if (generation !== _readGeneration || seq !== _readSeq) return
        if (accountUid !== _accountKey()) return
        tasks.value = res.tasks
        if (typeof res.revision === 'number') tasksRevision.value = res.revision
        if (res.business_date) businessDate.value = res.business_date
        stats.value = {
          total: res.total,
          pending_total: res.pending_total,
          completed: res.completed,
          today_done: res.today_done,
          today_pending: res.today_pending,
          remaining_time: res.remaining_time,
          avg_remaining: res.avg_remaining,
          urgency: res.urgency,
        }
      } catch (error) {
        // 已作废/已被取代/已换账号（含被取消）：不写回、**不恢复缓存**
        // （缓存也是旧账号的），也不算业务失败。
        if (generation !== _readGeneration || seq !== _readSeq) return
        if (accountUid !== _accountKey()) return
        console.error('获取任务列表失败:', error)
        const cached = cache.get<{ uid: number | null; tasks: TaskItem[]; stats: TaskStats }>('tasks_cache')
        // 缓存也属于某个账号：身份不匹配（换过号、或从未登录）时**不**恢复，
        // 否则 B 在读取失败时会看到 A 的任务列表。
        if (cached && (cached.uid ?? null) === _accountKey()) {
          tasks.value = cached.tasks
          stats.value = cached.stats
        }
      } finally {
        _readControllers.delete(controller)
        if (_slotController === controller) _slotController = null
        // 只有仍是最新读、且启动时接过 loading 责任的读，才有资格收尾加载态
        // （可见读必担责；静默读仅在接替已点亮 loading 时担责——P2 修复）。
        if (generation === _readGeneration && seq === _readSeq && ownsLoading) {
          isLoading.value = false
        }
      }
    })()
    _readInflight = run
    // 单飞引用在读取结束后解除（不会误清新一代的引用）。
    const clearInflight = (): void => {
      if (_readInflight === run) _readInflight = null
    }
    void run.then(clearInflight, clearInflight)
    return run
  }

  /**
   * 版本驱动刷新：先读轻量统计里的 revision，只有前进时才重取完整列表。
   *
   * 旧实现的任务页只在挂载/用户操作后刷新：用户一直停在任务页时，后台
   * "任务完成 → 重算优先度 → 重排"不会反映到界面上（tasks_revision 没有
   * 进入前端一致性协议）。这里也不把整个列表改成高频轮询：只轮询统计。
   */
  async function refreshTasksIfStale(): Promise<boolean> {
    if (!_readsAllowed()) return false
    const generation = _readGeneration
    const accountUid = _accountKey()
    const controller = new AbortController()
    _readControllers.add(controller)
    try {
      const res = await request.get<TaskStats & {
        revision?: number
        business_date?: string
      }>('/api/tasks/stats', undefined, { signal: controller.signal })
      // 旧代/换账号响应：businessDate 也不得写回。
      if (generation !== _readGeneration) return false
      if (accountUid !== _accountKey()) return false
      if (res.business_date) businessDate.value = res.business_date
      const revision = typeof res.revision === 'number' ? res.revision : -1
      if (revision < 0 || revision === tasksRevision.value) return false
      // stats 已确认服务端有更新的版本（如后台结算/重排）：本次重取必须读到
      // 晚于该提示的数据，不得复用提示前已挂起的旧读（F1）。
      await fetchTasks({ silent: true, fresh: true })
      return true
    } catch (error) {
      if (generation === _readGeneration) console.error('刷新任务版本失败:', error)
      return false
    } finally {
      _readControllers.delete(controller)
    }
  }

  // 获取任务详情（含 ID 和计算列）
  async function fetchTaskDetails() {
    if (!_readsAllowed()) return
    const generation = _readGeneration
    const accountUid = _accountKey()
    const controller = new AbortController()
    _readControllers.add(controller)
    isLoading.value = true
    try {
      const res = await request.get<TaskDetailResponse & {
        revision?: number
        business_date?: string
      }>('/api/tasks/detail', undefined, { signal: controller.signal })
      if (generation !== _readGeneration) return
      if (accountUid !== _accountKey()) return
      taskDetails.value = res.tasks
      if (typeof res.revision === 'number') tasksRevision.value = res.revision
      if (res.business_date) businessDate.value = res.business_date
      stats.value = {
        total: res.total,
        pending_total: res.pending_total,
        completed: res.completed,
        today_done: res.today_done,
        today_pending: res.today_pending,
        remaining_time: res.remaining_time,
        avg_remaining: res.avg_remaining,
        urgency: res.urgency,
      }
    } catch (error) {
      if (generation === _readGeneration) console.error('获取任务详情失败:', error)
    } finally {
      _readControllers.delete(controller)
      if (generation === _readGeneration) isLoading.value = false
    }
  }

  /**
   * 冻结覆盖目标：在**弹框之前**取到的那三件套。
   *
   * 返回 null 表示"当前列表里没有这条记录"或"记录没有稳定 id"——调用方据此
   * 走新建流程/提示刷新，不要退回按名字覆盖。
   */
  function freezeOverwriteTarget(zoneName: string): OverwriteTarget | null {
    const row = tasks.value.find(t => t.zone_name === zoneName)
    if (!row || typeof row.id !== 'number') return null
    return {
      id: row.id,
      expectedRevision: tasksRevision.value,
      businessDate: businessDate.value,
    }
  }

  // ==================== M1：创建预检 / 一次替换 / 回执 / 分区历史 ============

  /**
   * 取一次**一致预检**（`GET /api/tasks/creation-preview`）。
   *
   * 请求带上这次操作冻结的账号归属（`X-Account-UID`）；服务端据此**核原请求
   * 账号**并冻结实际绑定，读取期间换号即拒绝。回包里的 `uid` 与本次冻结的范围
   * 不同就把它丢掉并明确报错——绝不拿别的账号的预检去弹框或提交。
   */
  async function fetchCreationPreview(
    zoneName: string, scope?: FrozenWriteScope,
  ): Promise<CreationPreview> {
    const frozen = scope ?? freezeWriteScope()
    const res = await request.get<CreationPreview>(
      `/api/tasks/creation-preview?zone_name=${encodeURIComponent(zoneName)}`,
      undefined, { accountUid: frozen.uid })
    if ((res.uid ?? null) !== (frozen.uid ?? null)) {
      throw new Error('预检回包属于另一个账号：已丢弃，请刷新页面后按当前账号重新操作')
    }
    if (!writeScopeIsCurrent(frozen)) {
      throw new Error('预检回包到达时账号已经变化：已丢弃，请按当前账号重新操作')
    }
    return res
  }

  /**
   * 提交一次创建：**一份不可变命令 + 一个客户端令牌**。
   *
   * - 令牌与完整冻结命令一起送出，传输层重试重放的就是同一份载荷（不重取预检、
   *   不换令牌、不重新覆盖）；
   * - 成功后的刷新必须读到**晚于本次写入**的数据；账号已换时不刷新。
   * - `options.onAttemptLost`：传输层有物理尝试在途丢失时回调一次（见
   *   `AttemptLostNotifier`）。一次逻辑调用可能已经真实提交过一次、只是那次回包
   *   丢了；调用方据此知道"后续收到的拒绝只说明那一轮没受理"，不会把它当成原
   *   创建的终态。
   */
  async function createNewTask(
    data: TaskCreate, preview: FrozenCreationPreview | null, token: string,
    scope?: FrozenWriteScope,
    options?: { onAttemptLost?: AttemptLostNotifier },
  ): Promise<CreateTaskResponse> {
    const frozen = scope ?? freezeWriteScope()
    const body: TaskCreate = { ...data, operation_token: token }
    if (preview) {
      body.preview = preview
      body.confirm_replace = true
    }
    const res = await request.post<CreateTaskResponse>('/api/tasks', body, {
      accountUid: frozen.uid,
      onAttemptLost: options?.onAttemptLost,
    })
    if (writeScopeIsCurrent(frozen)) await fetchTasks({ fresh: true })
    return res
  }

  /**
   * 只读核对同一令牌的**已提交回执**（回包丢失后）。
   *
   * 缺记录只表示"尚未查到成功"，不证明请求没执行——调用方据此显示"结果待核对"，
   * 不重新创建、不自动后台重试。
   *
   * 请求带冻结的账号归属；回包到达时**先**核这次操作所属的账号是否仍是当前账号，
   * **再**核命中回执自己声明的来源账号。两道门任一不过就**抛出**，都不返回一份
   * 别的账号的回执：把它当成"本次操作已成功"正是要挡的那种错。
   *
   * 来源缺失与来源不符同样拒绝——回执必须能证明它来自本次那份冻结的账号；而
   * 别的账号的回执若被接受，刷新出来的会是错账号的任务。``found=false`` 仍只是
   * "没查到"，不倒推成创建失败。
   */
  async function lookupCreationReceipt(
    token: string, scope?: FrozenWriteScope,
  ): Promise<CreationReceiptResponse> {
    const frozen = scope ?? freezeWriteScope()
    const res = await request.get<CreationReceiptResponse>(
      `/api/tasks/creation-receipt?operation_token=${encodeURIComponent(token)}`,
      undefined, { accountUid: frozen.uid })
    if (!writeScopeIsCurrent(frozen)) {
      throw new Error('回执回包属于另一个账号：已丢弃，请按当前账号重新核对')
    }
    if (res.found && (res.uid ?? null) !== (frozen.uid ?? null)) {
      throw new Error('回执回包声明的来源账号与本次操作不一致：已丢弃，请按当前账号重新核对')
    }
    return res
  }

  /**
   * 只读分区历史（**请求所属账号**）。**不**触发任务变更、日切或任何直播动作。
   *
   * 请求带冻结的账号归属；回包属于发起那一刻的账号——换账号后返回 null，
   * 调用方据此丢弃，旧响应不写进新账号的历史窗口。
   */
  async function fetchZoneHistory(
    zoneName: string, offset = 0, limit = 20, scope?: FrozenWriteScope,
  ): Promise<ZoneHistoryResponse | null> {
    const frozen = scope ?? freezeWriteScope()
    const accountUid = _accountKey()
    const res = await request.get<ZoneHistoryResponse>(
      `/api/tasks/history?zone_name=${encodeURIComponent(zoneName)}`
      + `&offset=${offset}&limit=${limit}`,
      undefined, { accountUid: frozen.uid })
    if (accountUid !== _accountKey()) return null
    if ((frozen.uid ?? null) !== _accountKey()) return null
    if ((res.uid ?? null) !== (frozen.uid ?? null)) return null
    return res
  }

  /**
   * 更新任务。
   *
   * 写请求必须带**冻结身份**（task_id）：名字会在删除重建/改名后指向另一条
   * 记录，只按名字发请求会让"响应丢失后的重放"作用到重建出来的新任务上。
   */
  async function updateTask(zoneName: string, data: TaskUpdate, taskId?: number,
                            scope?: FrozenWriteScope) {
    const frozen = scope ?? freezeWriteScope()
    try {
      const body = taskId === undefined ? data : { ...data, id: taskId }
      await request.put(`/api/tasks/${encodeURIComponent(zoneName)}`, body,
                        { accountUid: frozen.uid })
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。账号已换时不刷新（见 createTask）。
      if (writeScopeIsCurrent(frozen)) await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('更新任务失败:', error)
      return false
    }
  }

  // 删除任务（带冻结身份；服务端按 id 定位，找不到就是 404 而不是删掉别人）
  async function deleteTask(zoneName: string, taskId?: number,
                            scope?: FrozenWriteScope) {
    const frozen = scope ?? freezeWriteScope()
    try {
      const base = `/api/tasks/${encodeURIComponent(zoneName)}`
      await request.delete(taskId === undefined ? base : `${base}?task_id=${taskId}`,
                           { accountUid: frozen.uid })
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。账号已换时不刷新（见 createTask）。
      if (writeScopeIsCurrent(frozen)) await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('删除任务失败:', error)
      return false
    }
  }

  // 标记任务完成（带冻结身份 + 渲染时的业务日）
  async function markTaskDone(zoneName: string, taskId?: number,
                              executionDate?: string,
                              scope?: FrozenWriteScope) {
    const frozen = scope ?? freezeWriteScope()
    try {
      const params = new URLSearchParams()
      if (taskId !== undefined) params.set('task_id', String(taskId))
      const day = executionDate || frozen.businessDate || businessDate.value
      if (day) params.set('execution_date', day)
      const qs = params.toString()
      const base = `/api/tasks/mark-done/${encodeURIComponent(zoneName)}`
      await request.post<MarkDoneResponse>(qs ? `${base}?${qs}` : base, undefined,
                                           { accountUid: frozen.uid })
      // 写入已确认成功：刷新必须读到晚于本次写入的数据（F1）。
      if (writeScopeIsCurrent(frozen)) {
        await fetchTasks({ fresh: true })
        cache.set('tasks_cache', {
          uid: _accountKey(),
          tasks: tasks.value,
          stats: stats.value,
        })
      }
      return true
    } catch (error) {
      console.error('标记任务失败:', error)
      return false
    }
  }

  // 标记任务全部完成（带冻结身份）
  async function markTaskAllDone(zoneName: string, taskId?: number,
                                 scope?: FrozenWriteScope) {
    const frozen = scope ?? freezeWriteScope()
    try {
      const base = `/api/tasks/mark-all-done/${encodeURIComponent(zoneName)}`
      await request.post<MarkDoneResponse>(
        taskId === undefined ? base : `${base}?task_id=${taskId}`, undefined,
        { accountUid: frozen.uid })
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。账号已换时不刷新（见 createTask）。
      if (writeScopeIsCurrent(frozen)) await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('标记全部完成失败:', error)
      return false
    }
  }

  // 重新加载任务
  async function reloadTasks(scope?: FrozenWriteScope) {
    const frozen = scope ?? freezeWriteScope()
    try {
      await request.post<ReloadResponse>('/api/tasks/reload', undefined,
                                         { accountUid: frozen.uid })
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。账号已换时不刷新（见 createTask）。
      if (writeScopeIsCurrent(frozen)) await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('重新加载任务失败:', error)
      return false
    }
  }

  // 从 Excel 导入（支持分区校验确认流程 + 合并/全覆盖模式）
  //
  // 经请求封装发送 FormData：面板开启密码时自动带上 Bearer，401 走统一登录引导，
  // Content-Type 由 axios 带出 multipart boundary（不手工写成 JSON）。
  async function importFromExcel(file: File, force: boolean = false,
                                 mode: string = 'merge',
                                 scope?: FrozenWriteScope): Promise<ImportResult> {
    const frozen = scope ?? freezeWriteScope()
    try {
      const formData = new FormData()
      formData.append('file', file)
      const params = new URLSearchParams()
      if (force) params.set('force', 'true')
      if (mode !== 'merge') params.set('mode', mode)
      const qs = params.toString()
      const url = qs ? `/api/tasks/import?${qs}` : '/api/tasks/import'
      const res = await request.postForm<ImportResult>(url, formData,
                                                       { accountUid: frozen.uid })
      if (res.success && writeScopeIsCurrent(frozen)) {
        // 导入已确认成功：刷新必须读到晚于本次写入的数据（F1）。
        await fetchTasks({ fresh: true })
        cache.set('tasks_cache', {
          uid: _accountKey(), tasks: tasks.value, stats: stats.value })
      }
      return res
    } catch (error) {
      // 脱敏：请求对象的 headers 带面板 token，只记录状态码或消息文本。
      const status = (error as { response?: { status?: number } })?.response?.status
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      console.error('导入失败:', status ? `HTTP ${status}${detail ? ' ' + detail : ''}` : ((error as Error)?.message || '未知错误'))
      return {
        success: false,
        imported_count: 0,
        message: detail || (status ? `导入失败（HTTP ${status}）` : '导入失败'),
        errors: [],
      }
    }
  }

  /**
   * 判断二进制响应是不是 xlsx。
   *
   * xlsx 是 ZIP 容器（魔数 "PK\x03\x04"）。用于挡住"鉴权失败/服务端异常时把
   * HTML 错误页或空响应当成 Excel 下载"的情况。
   */
  async function looksLikeXlsx(blob: Blob): Promise<boolean> {
    if (!blob || blob.size < 4) return false
    const head = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
    return head[0] === 0x50 && head[1] === 0x4b  // 'P' 'K'
  }

  // 导出到 Excel
  async function exportToExcel() {
    try {
      // 经请求封装下载：面板鉴权头（Authorization: Bearer）与 401 处理与其余请求同源，
      // 不再裸 fetch 导致导出缺少鉴权头。二进制响应不走 JSON 解包。
      const blob = await request.getBlob('/api/tasks/export')

      // 鉴权失败/异常响应：绝不落盘成"伪 Excel"
      if (!(await looksLikeXlsx(blob))) {
        console.error('导出失败：响应不是有效的 xlsx（size=%d）', blob?.size ?? 0)
        return false
      }

      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // 使用本地时区时间，格式: live_tasks_2026-05-11_12-03-16.xlsx
      const now = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`
      a.download = `live_tasks_${ts}.xlsx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      window.URL.revokeObjectURL(url)
      return true
    } catch (error) {
      // 脱敏：AxiosError 的 config.headers.Authorization 带面板 token，
      // 直接打印 error 会把凭据写进控制台/日志。只记录状态码或消息文本。
      const status = (error as { response?: { status?: number } })?.response?.status
      console.error('导出失败:', status ? `HTTP ${status}` : (error as Error)?.message || '未知错误')
      return false
    }
  }

  return {
    tasks,
    taskDetails,
    stats,
    isLoading,
    tasksRevision,
    businessDate,
    nextTask,
    fetchTasks,
    refreshTasksIfStale,
    fetchTaskDetails,
    stopTaskReads,
    freezeOverwriteTarget,
    freezeWriteScope,
    writeScopeIsCurrent,
    fetchCreationPreview,
    createNewTask,
    lookupCreationReceipt,
    fetchZoneHistory,
    updateTask,
    deleteTask,
    markTaskDone,
    markTaskAllDone,
    reloadTasks,
    importFromExcel,
    exportToExcel,
  }
})