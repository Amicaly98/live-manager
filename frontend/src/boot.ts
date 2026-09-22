/**
 * boot.ts - 启动阶段的一次性读取编排（2026-09-21 工作包 A0）
 *
 * 要解决的问题：用户报告"页面底色已经出现，长时间没有其他内容"。只读核对发现
 * 一条真实代码路径——首屏导航在路由守卫里等待 `/api/auth/status`，该读请求在
 * 传输层遇到网络错误会持续重试且**没有总预算**，
 * 而 `App.vue` 只有一个裸 `<router-view>`：守卫不返回，路由组件就永远不会挂载，
 * 页面只剩 CSS 底色。`/api/auth/status` 在已登录时还会同步访问 B 站用户信息接口，
 * 因此后端 health 200 并不代表这条路径能走完。
 *
 * 本模块的职责（**只做启动读取的编排，不改业务语义**）：
 *
 * - 单飞：路由守卫与 `App.vue` 共用同一次读取，不再各发一份（旧实现两处都调用
 *   `checkLoginStatus()`，首屏至少两条重复的 auth 请求）；
 * - 有界：**一次启动共用一个总时间预算**与有限次数，并给下游传真实的
 *   `AbortSignal`。只用 `Promise.race` 把旧请求藏起来不算取消——这里在超时时
 *   同时 `abort()`，让在途请求与退避等待真正中止；
 * - 代际：重试/重新导航创建新代，旧代迟到的响应一律丢弃，不得覆盖新界面；
 * - 桌面版启动只读取 B 站登录状态，不包含服务端独立密码代际；后端地址由
 *   request.ts 的 Electron 分支决定；
 * - 失败可恢复：超时/网络失败/资源失败/初始化异常是**不同分支**，分别给出文案，
 *   提供"重试"与"重新加载"，不自动无限刷新。
 *
 * 常量集中在这里，便于审查与调整：
 * - `BOOT_TOTAL_BUDGET_MS`：整个启动读取的总预算（工程选择，不是线上最佳阈值）；
 * - `BOOT_READ_MAX_ATTEMPTS`：单次逻辑请求在传输层最多实际发出的次数。
 *
 * 控制类写请求的票据重试语义**未改动**——这里只管启动读取。
 */

/** 整个启动读取的总时间预算（毫秒）。约 15 秒内必须给出可恢复状态。 */
export const BOOT_TOTAL_BUDGET_MS = 15000

/** 单次逻辑读请求在传输层最多实际发出的次数（含首次）。 */
export const BOOT_READ_MAX_ATTEMPTS = 2

export type BootPhase = 'connecting' | 'ready' | 'failed'

/** 失败分支：文案与处理方式不同，不能合并成一种"密码错误"。 */
export type BootFailureKind =
  | 'timeout'      // 总预算用尽：读请求没在预算内拿到结果
  | 'network'      // 网络失败（请求确实失败了）
  | 'resource'     // 入口 JS / 页面 chunk 加载失败
  | 'init'         // 应用初始化异常

export interface BootFailure {
  kind: BootFailureKind
  /** 只用于日志，不展示给用户（不暴露内部模块/错误码）。 */
  detail?: string
}

export interface BootResult {
  /** 启动读取是否完成。**不代表已登录**。 */
  ok: boolean
  generation: number
  loggedIn: boolean
  failure?: BootFailure
}

export interface BootReadContext {
  signal: AbortSignal
  maxAttempts: number
}

export interface BootReaders {
  readAuth(ctx: BootReadContext): Promise<{ loggedIn: boolean }>
}

export interface BootSnapshot {
  phase: BootPhase
  generation: number
  failure: BootFailure | null
}

type Listener = (snapshot: BootSnapshot) => void

export class BudgetExceededError extends Error {
  constructor() {
    super('boot-budget-exceeded')
    this.name = 'BudgetExceededError'
  }
}

/** 默认读取器：接真实 store。动态 import，避免在模块加载期形成循环依赖。 */
const defaultReaders: BootReaders = {
  async readAuth({ signal, maxAttempts }) {
    const { useAuthStore } = await import('@/stores/auth')
    const loggedIn = await useAuthStore().checkLoginStatus({ signal, maxAttempts })
    return { loggedIn: Boolean(loggedIn) }
  },
}

/**
 * 启动控制器。
 *
 * 刻意做成**可注入 reader** 的纯编排对象：离线测试可以直接注入挂起/失败的
 * reader，不必碰真实 store，也不会因为 signal 没被传到 store 而测出假通过。
 */
export class BootController {
  private _phase: BootPhase = 'connecting'
  private _failure: BootFailure | null = null
  private _generation = 0
  private _inflight: Promise<BootResult> | null = null
  private _result: BootResult | null = null
  private _abort: AbortController | null = null
  private _timer: ReturnType<typeof setTimeout> | null = null
  private _readers: BootReaders
  private _listeners = new Set<Listener>()
  private _budgetMs: number
  private _maxAttempts: number

  /**
   * 导航被启动读取挡下时，用户本来想去的地址。
   * 恢复成功后由 `App.vue` 重新导航过去（不让用户原地打转）。
   */
  pendingPath: string | null = null

  /** 最近一次作废的原因（只用于诊断，不展示给用户）。 */
  lastInvalidationReason = ''

  constructor(readers: BootReaders = defaultReaders,
              budgetMs: number = BOOT_TOTAL_BUDGET_MS,
              maxAttempts: number = BOOT_READ_MAX_ATTEMPTS) {
    this._readers = readers
    this._budgetMs = budgetMs
    this._maxAttempts = maxAttempts
  }

  /** 供测试替换读取器（生产不需要调用）。 */
  installReaders(readers: BootReaders): void {
    this._readers = readers
  }

  get snapshot(): BootSnapshot {
    return { phase: this._phase, generation: this._generation, failure: this._failure }
  }

  get phase(): BootPhase {
    return this._phase
  }

  get failure(): BootFailure | null {
    return this._failure
  }

  get generation(): number {
    return this._generation
  }

  get result(): BootResult | null {
    return this._result
  }

  subscribe(listener: Listener): () => void {
    this._listeners.add(listener)
    return () => this._listeners.delete(listener)
  }

  private _emit(): void {
    const snapshot = this.snapshot
    for (const listener of Array.from(this._listeners)) {
      try {
        listener(snapshot)
      } catch {
        /* 订阅者异常不得影响启动流程 */
      }
    }
  }

  /**
   * 等待"当前这一代"的启动读取结果。
   *
   * - 已经就绪 → 直接给缓存结果（单飞的意义就在于此）；
   * - 有在途 → 搭同一次车；
   * - 否则开一次新的。
   *
   * **作废语义**：如果拿到的结果属于已被作废的旧代（例如用户在等待期间提交了
   * 密码、完成了扫码、或登出了），绝不给调用方消费——这里会重新等到新代的结果。
   * 因此"启动单飞缓存"不会变成一个永久的认证事实。
   */
  ensure(): Promise<BootResult> {
    if (this._phase === 'ready' && this._result) {
      return Promise.resolve(this._result)
    }
    const pending = this._inflight ?? this.start()
    return this._awaitCurrent(pending, 0)
  }

  /**
   * 只在结果属于最新代、且仍然是"当前已发布结果"时才返回它。
   * `hops` 限制重新等待的次数：连续作废时不会无限递归，最终返回最后一次结果
   * （调用方仍可用 :func:`isCurrent` 自行判断）。
   */
  private async _awaitCurrent(pending: Promise<BootResult>,
                              hops: number): Promise<BootResult> {
    const result = await pending
    if (this.isCurrent(result)) return result
    if (hops >= 4) return result
    const next = this._inflight ?? this.start()
    return this._awaitCurrent(next, hops + 1)
  }

  /** 这个结果是否属于最新代、并且正是当前对外发布的那一份。 */
  isCurrent(result: BootResult | null | undefined): boolean {
    if (!result) return false
    if (result.generation !== this._generation) return false
    if (!result.ok) return true          // 失败结果本身就是这一代的最终结论
    return this._result === result && this._phase === 'ready'
  }

  /**
   * **认证状态已改变的显式入口**（2026-09-21 返修 R1）。
   *
   * 所有会改变鉴权事实的地方都必须调它，否则启动缓存会变成永久的认证事实：
   * 平台扫码登录成功、平台登出，以及认证请求收到 401。
   *
   * 语义（三件事一起做，缺一不可）：
   * 1. **中止在途读取**：连带取消它的下游阶段（signal 是真的传下去的）；
   * 2. **作废旧代**：`generation` 前进，旧结果不再被 `ensure()` 返回、也不会被
   *    `_settle` 发布（等待旧代的调用方会被重新导向新代）；
   * 3. **清空已发布结果**：`_result = null`，订阅者与守卫据 `isCurrent()` 立刻停止
   *    消费旧结论（业务加载随之停止）。
   *
   * **刻意不动 `phase`**：如果把它打回 `connecting`，根层的启动遮罩会立刻把
   * `<router-view>` 拆掉——而作废恰恰是由**当前页面里的动作**触发的（扫码成功、
   * 点登出）。拆掉再挂回来会让那个页面重新挂载、把它的副作用（例如扫码
   * 轮询）重新跑一遍，于是"登录成功 → 作废 → 重挂 → 再登录成功 → 再作废"形成死循环。
   * 保持阶段不变，页面原地等一次新的读取即可；导航决定仍由守卫按最新结论做。
   */
  invalidate(reason: string): void {
    this._abortInFlight()
    this._generation += 1
    this._inflight = null
    this._result = null
    this._failure = null
    this.lastInvalidationReason = reason
    this._emit()
  }

  /** 开始一次新的启动读取（会开新代际，旧代迟到的响应作废）。 */
  start(): Promise<BootResult> {
    this._abortInFlight()
    const generation = ++this._generation
    // 只有"还没有任何可展示内容"时才回到 connecting（首次启动 / 失败后重试）。
    // 已经就绪时（作废后的重新校验）**保持 ready**：把阶段打回 connecting 会让根层
    // 启动遮罩拆掉 `<router-view>`，触发作废的那个页面会被重新挂载、副作用重跑——
    // 扫码轮询因此"成功 → 作废 → 重挂 → 再成功"，实测形成死循环。
    if (this._phase !== 'ready') this._phase = 'connecting'
    this._failure = null
    this._result = null

    const controller = new AbortController()
    this._abort = controller
    const run = this._run(generation, controller)
    this._inflight = run
    void run.then(() => {
      if (this._inflight === run) this._inflight = null
    })
    // **必须在登记 _inflight 之后再通知订阅者**：订阅者（App）会在收到通知时调用
    // `ensure()` 去驱动读取，而 `ensure()` 是"有在途就搭车、否则 start()"。
    // 先 emit 后登记会让那次 ensure() 看不到在途，于是再开一代、再 emit、再 ensure……
    // 一代一代递归下去直到爆栈（实测 "Maximum call stack size exceeded"）。
    this._emit()
    return run
  }

  /** 用户点"重试"：创建新代，旧响应不得覆盖新界面。 */
  retry(): Promise<BootResult> {
    return this.start()
  }

  private _abortInFlight(): void {
    if (this._timer) {
      clearTimeout(this._timer)
      this._timer = null
    }
    if (this._abort) {
      try {
        this._abort.abort()
      } catch {
        /* ignore */
      }
      this._abort = null
    }
  }

  private _settle(generation: number, result: BootResult): BootResult {
    // 迟到的旧代结果：一律丢弃。
    if (generation !== this._generation) return result
    this._result = result
    this._phase = result.ok ? 'ready' : 'failed'
    this._failure = result.failure ?? null
    this._emit()
    return result
  }

  private async _run(generation: number, controller: AbortController): Promise<BootResult> {
    let budgetHit = false
    let rejectBudget: ((error: Error) => void) | null = null
    // 预算到点：既真正 abort 在途请求/退避等待（不是只把 promise 藏起来），
    // 也让"等待"本身结束——万一下游没响应 abort，页面也不能一直停在底色。
    const budgetPromise = new Promise<never>((_resolve, reject) => {
      rejectBudget = reject
    })
    budgetPromise.catch(() => { /* 预算到点是预期分支，不产生未处理拒绝 */ })
    const timer = setTimeout(() => {
      budgetHit = true
      try {
        controller.abort()
      } catch {
        /* ignore */
      }
      if (rejectBudget) rejectBudget(new BudgetExceededError())
    }, this._budgetMs)
    this._timer = timer

    const withBudget = <T>(work: Promise<T>): Promise<T> =>
      Promise.race([work, budgetPromise])

    // 这一代是否已被作废（invalidate / 新的 start / 资源失败）。
    // 每个阶段之间都要检查：旧代不仅不能发布结果，也**不能继续派发下一阶段读取**
    // ——否则一次作废之后还会替旧结论多发一次 auth 请求。
    const abandoned = (): boolean => generation !== this._generation
    const bail = (result: BootResult): BootResult => {
      // 丢弃：不写 _result、不改 phase、不通知订阅者。
      return result
    }

    try {
      let loggedIn = false
      try {
        const auth = await withBudget(
          this._readers.readAuth({ signal: controller.signal,
                                   maxAttempts: this._maxAttempts }))
        loggedIn = Boolean(auth.loggedIn)
      } catch (error) {
        return this._settle(generation, {
          ok: false, generation, loggedIn: false,
          failure: { kind: budgetHit || error instanceof BudgetExceededError
                            ? 'timeout' : 'network',
                     detail: String((error as Error)?.name || error) },
        })
      }
      if (abandoned()) {
        return bail({ ok: false, generation, loggedIn: false,
                      failure: { kind: 'network', detail: 'abandoned' } })
      }

      return this._settle(generation, {
        ok: true, generation, loggedIn,
      })
    } finally {
      if (this._timer === timer) {
        clearTimeout(timer)
        this._timer = null
      }
    }
  }

  /**
   * 资源/初始化失败入口：chunk 加载失败、Vite preload 失败、根组件初始化异常。
   * 与网络失败是**不同分支**，文案与处理方式都不相同。
   */
  fail(kind: BootFailureKind, detail?: string): void {
    // 已经成功启动之后出现的**局部组件渲染错误**不把整页换成"请重试"。
    // 但资源加载失败（chunk / preload）不同：目标路由根本渲染不出来，界面会停在
    // 空白 router-view，因此无论当前是否已就绪都要给可恢复提示。
    if (this._phase === 'ready' && kind === 'init') return
    this._abortInFlight()
    this._generation += 1
    this._inflight = null
    this._result = null
    this._phase = 'failed'
    this._failure = { kind, detail }
    this._emit()
  }

  /** 测试用：恢复初始状态。 */
  reset(): void {
    this._abortInFlight()
    this._generation += 1
    this._inflight = null
    this._result = null
    this._phase = 'connecting'
    this._failure = null
  }
}

/** 全局单例：路由守卫与 `App.vue` 共用。 */
export const boot = new BootController()

/** 文案集中在这里：只给"正在连接…"/"暂时无法连接，请重试"这类简短可操作提示，
 *  不暴露内部模块名、错误码或计时原理。 */
export const BOOT_MESSAGES: Record<BootPhase, string> = {
  connecting: '正在连接…',
  ready: '',
  failed: '暂时无法连接，请重试',
}

export const BOOT_FAILURE_HINTS: Record<BootFailureKind, string> = {
  timeout: '服务响应超时，请检查网络后重试',
  network: '网络或服务暂时不可用，请稍后重试',
  resource: '页面资源加载失败，可重新加载后再试',
  init: '页面初始化异常，可重新加载后再试',
}
