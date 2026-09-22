import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useRequest } from '@/api/request'
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
} from '@/types/api'

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
  // - `_readGeneration`：所有权代际。登出 / 认证失效 / 认证意图切换时由
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

  function _readsAllowed(): boolean {
    // App 的业务加载只在启动结论 loggedIn=true 时启动；这里挡的是"写请求收尾的
    // 附带读取"、"定时器残留的刷新"这类绕过 App 开关的入口。
    return auth.isLoggedIn
  }

  /**
   * 停止任务只读请求（登出 / 认证失效 / 认证意图切换 / 所有者销毁）。
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
        // 写回门控：旧代响应、或已被更新读取代的响应，一律不得落地。
        if (generation !== _readGeneration || seq !== _readSeq) return
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
        // 已作废/已被取代（含被取消）：不写回、不恢复缓存，也不算业务失败。
        if (generation !== _readGeneration || seq !== _readSeq) return
        console.error('获取任务列表失败:', error)
        const cached = cache.get<{ tasks: TaskItem[]; stats: TaskStats }>('tasks_cache')
        if (cached) {
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
    const controller = new AbortController()
    _readControllers.add(controller)
    try {
      const res = await request.get<TaskStats & {
        revision?: number
        business_date?: string
      }>('/api/tasks/stats', undefined, { signal: controller.signal })
      // 旧代响应：businessDate 也不得写回。
      if (generation !== _readGeneration) return false
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
    const controller = new AbortController()
    _readControllers.add(controller)
    isLoading.value = true
    try {
      const res = await request.get<TaskDetailResponse & {
        revision?: number
        business_date?: string
      }>('/api/tasks/detail', undefined, { signal: controller.signal })
      if (generation !== _readGeneration) return
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

  // 创建任务（overwrite 时必须带冻结的前置条件：覆盖的是"用户确认时看到的
  // 那一条记录的当时状态"，不是"这个名字"、也不是"现在的最新状态"）
  async function createTask(data: TaskCreate, overwrite: boolean = false,
                           target?: OverwriteTarget) {
    if (overwrite && !target) {
      // 不发无目标的覆盖请求：服务端会明确拒绝（overwrite_requires_id），
      // 前端提前拦下可以保留"刷新后重新确认"的可操作路径。
      throw new Error('覆盖任务缺少目标记录身份（id）：请刷新任务列表后重新确认')
    }
    try {
      const params = new URLSearchParams()
      if (overwrite) params.set('overwrite', 'true')
      const qs = params.toString()
      const url = qs ? `/api/tasks?${qs}` : '/api/tasks'
      // 前置条件原样送出：**绝不**在这里用 tasksRevision/businessDate 的当前值
      // 覆盖它们——传输层重试会重放同一份载荷，现取就等于把用户确认的版本
      // 悄悄换成最新版本，保护形同虚设。
      const body = target
        ? { ...data, id: target.id,
            expected_revision: target.expectedRevision,
            business_date: target.businessDate }
        : data
      await request.post(url, body)
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。
      await fetchTasks({ fresh: true })
      return true
    } catch (error: any) {
      console.error('创建任务失败:', error)
      // 返回错误信息供调用方判断是否为重名
      throw error
    }
  }

  /**
   * 更新任务。
   *
   * 写请求必须带**冻结身份**（task_id）：名字会在删除重建/改名后指向另一条
   * 记录，只按名字发请求会让"响应丢失后的重放"作用到重建出来的新任务上。
   */
  async function updateTask(zoneName: string, data: TaskUpdate, taskId?: number) {
    try {
      const body = taskId === undefined ? data : { ...data, id: taskId }
      await request.put(`/api/tasks/${encodeURIComponent(zoneName)}`, body)
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。
      await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('更新任务失败:', error)
      return false
    }
  }

  // 删除任务（带冻结身份；服务端按 id 定位，找不到就是 404 而不是删掉别人）
  async function deleteTask(zoneName: string, taskId?: number) {
    try {
      const base = `/api/tasks/${encodeURIComponent(zoneName)}`
      await request.delete(taskId === undefined ? base : `${base}?task_id=${taskId}`)
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。
      await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('删除任务失败:', error)
      return false
    }
  }

  // 标记任务完成（带冻结身份 + 渲染时的业务日）
  async function markTaskDone(zoneName: string, taskId?: number,
                              executionDate?: string) {
    try {
      const params = new URLSearchParams()
      if (taskId !== undefined) params.set('task_id', String(taskId))
      const day = executionDate || businessDate.value
      if (day) params.set('execution_date', day)
      const qs = params.toString()
      const base = `/api/tasks/mark-done/${encodeURIComponent(zoneName)}`
      await request.post<MarkDoneResponse>(qs ? `${base}?${qs}` : base)
      // 写入已确认成功：刷新必须读到晚于本次写入的数据（F1）。
      await fetchTasks({ fresh: true })
      cache.set('tasks_cache', {
        tasks: tasks.value,
        stats: stats.value,
      })
      return true
    } catch (error) {
      console.error('标记任务失败:', error)
      return false
    }
  }

  // 标记任务全部完成（带冻结身份）
  async function markTaskAllDone(zoneName: string, taskId?: number) {
    try {
      const base = `/api/tasks/mark-all-done/${encodeURIComponent(zoneName)}`
      await request.post<MarkDoneResponse>(
        taskId === undefined ? base : `${base}?task_id=${taskId}`)
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。
      await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('标记全部完成失败:', error)
      return false
    }
  }

  // 重新加载任务
  async function reloadTasks() {
    try {
      await request.post<ReloadResponse>('/api/tasks/reload')
      // 写入已确认成功：随后的刷新必须读到**晚于本次写入**的数据（F1），
      // 不允许复用写入前已挂起的旧读。
      await fetchTasks({ fresh: true })
      return true
    } catch (error) {
      console.error('重新加载任务失败:', error)
      return false
    }
  }

  // 从 Excel 导入（支持分区校验确认流程 + 合并/全覆盖模式）
  //
  // 经请求封装发送 FormData：认证失败走统一登录引导，
  // Content-Type 由 axios 带出 multipart boundary（不手工写成 JSON）。
  async function importFromExcel(file: File, force: boolean = false, mode: string = 'merge'): Promise<ImportResult> {
    try {
      const formData = new FormData()
      formData.append('file', file)
      const params = new URLSearchParams()
      if (force) params.set('force', 'true')
      if (mode !== 'merge') params.set('mode', mode)
      const qs = params.toString()
      const url = qs ? `/api/tasks/import?${qs}` : '/api/tasks/import'
      const res = await request.postForm<ImportResult>(url, formData)
      if (res.success) {
        // 导入已确认成功：刷新必须读到晚于本次写入的数据（F1）。
        await fetchTasks({ fresh: true })
        cache.set('tasks_cache', { tasks: tasks.value, stats: stats.value })
      }
      return res
    } catch (error) {
      // 脱敏：只记录状态码或消息文本。
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
      // 经请求封装下载：认证处理与其余请求同源，不再裸 fetch。二进制响应不走 JSON 解包。
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
      // 脱敏：直接打印 error 可能把凭据写进控制台/日志，只记录状态码或消息文本。
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
    createTask,
    updateTask,
    deleteTask,
    markTaskDone,
    markTaskAllDone,
    reloadTasks,
    importFromExcel,
    exportToExcel,
  }
})
