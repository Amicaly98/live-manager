/**
 * request.ts - Axios 请求封装（桌面版）
 *
 * 功能：
 * 1. 基础实例配置（Electron localhost baseURL / Vite 开发代理）
 * 2. 请求/响应拦截器
 * 3. 统一错误处理
 * 4. 断线重连（指数退避，**经同一实例**重试，持续且可取消）
 * 5. 封装返回类型（解决 Axios interceptor 类型传播问题）
 *
 * 重试协议（桌面移植阶段 2 / 服务端阶段 4C）：
 * - 网络失败后经**同一实例**递归重试：重试请求再次经过请求/响应拦截器，
 *   令牌复用、响应分类（409 冲突 / 401 鉴权）与数据解包全部保留；
 *   不会出现"第二次失败绕过拦截器直接 reject"的暗路径。
 * - 传输层失败重试**绝不更换令牌**（同一份 config 复用）。
 * - 取消：调用方传入 AbortSignal 即可随时取消；取消不重试，
 *   退避等待也会被立刻打断，页面保持可操作。
 * - 控制操作令牌由**客户端生成**（见 ./operationToken.ts）；服务端签发票据的
 *   取票链已随 `/api/live/operation-ticket` 退役一并删除。控制请求带**有界**
 *   总尝试预算（`CONTROL_MAX_ATTEMPTS`），不无限重放。
 * - 桌面没有服务器面板密码：401 一律按 B 站登录态失效处理（清 store → 回登录页）。
 */

import axios, { AxiosInstance, AxiosError, AxiosRequestConfig,
                 InternalAxiosRequestConfig,
                 GenericAbortSignal } from 'axios'
import { ElMessage } from 'element-plus'
import {
  ensureOperationToken,
  retryPlan,
  isControlUrl,
  OPERATION_TOKEN_HEADER,
  OPERATION_TOKEN_MAX_LENGTH,
  CONTROL_MAX_ATTEMPTS,
} from './operationToken'

/**
 * 「本次逻辑请求已经有物理尝试在途丢失」的通知回调（局部请求上下文）。
 *
 * 同令牌自动重试就发生在响应拦截器内部：一次逻辑调用（例如一次创建）可能实际
 * 发出**多次**物理请求。若其中某一次已经发出、却没有拿到任何回包（断线 /
 * 响应丢失），这次操作的**前序结果就是未知的**——此后任何一轮收到的
 * 拒绝都只说明"那一轮没受理"，不足以推翻它。
 *
 * 调用方在发起时提供一个回调，拦截器在发生"在途丢失"时调用它。这个事实因此只
 * 穿过必要的传输薄接缝（一个函数引用），不需要后台投递、状态机或全局命令队列。
 *
 * 注意：必须用**回调**而不是一个普通对象来承载——axios 的 `mergeConfig` 会把
 * 普通对象深拷贝成新对象，调用方手里的那份就再也收不到置位；函数按引用传递。
 */
export type AttemptLostNotifier = () => void

type RetryConfig = InternalAxiosRequestConfig & {
  _retryCount?: number
  signal?: AbortSignal
  /**
   * 本次请求带**后端可识别的重放令牌**（`X-Operation-Token`）。
   *
   * 只有这种请求才允许在传输层按 `_maxAttempts` 持续重发：后端能判出
   * "这是同一个意图的重放"，不会重复执行。
   */
  _replaySafe?: boolean
  /**
   * 本次逻辑请求允许实际发出的**总次数**（含首次）。
   *
   * 这是跨传输层的预算：调用方（状态轮询、控制操作）设 3，那么拦截器在第 3 次
   * 传输失败后直接失败，不会再多发一次。旧实现只有 store 外层的"重试 3 次"，
   * 而每次进入 Axios 又会被拦截器无限重放——一次轮询实际发出 15 次请求。
   */
  _maxAttempts?: number
  /**
   * 控制类请求：错误提示由调用方（`api/liveControl.ts`）按回执语义呈现，
   * 这里不弹通用的"操作冲突/请求失败"文案。
   */
  _controlRequest?: boolean
  /**
   * "本次逻辑请求有物理尝试在途丢失"的回调（见 `AttemptLostNotifier`）。
   *
   * 必须按**引用**穿过 axios 的 config 合并：普通对象会被 `mergeConfig` 深拷贝成
   * 新对象，回调（函数）不会。调用方因此能在拦截器重试之后仍读到这个事实。
   */
  _onAttemptLost?: AttemptLostNotifier
}

/**
 * 任务写请求携带的**账号归属标识**。
 *
 * 任务数据属于 UID：同一个 HTTP 入口先后服务 A、B 两号时，"这条旧页面发出的
 * 写请求属于哪个账号"必须由**客户端在用户选定时冻结**并原样带上来。后端据此
 * 拒绝"旧请求落到新账号"的操作，绝不替它补一个"最新账号"。
 *
 * 一次用户操作一个值：同一次操作的传输层重试复用同一份 config，因此不会在重试时
 * 重新读取当前账号。
 */
export const ACCOUNT_UID_HEADER = 'X-Account-UID'

/**
 * 把冻结的账号 UID 写进请求头。
 *
 * 没有有效 UID（未登录 / 冻结时还没身份）时不写头：那一次请求由后端按
 * "没有账号归属"的规则处理，不在这里补一个当前值。
 */
function withAccountUid(config: AxiosRequestConfig, accountUid?: number | null) {
  if (typeof accountUid !== 'number' || !Number.isFinite(accountUid)
      || accountUid <= 0) {
    return config
  }
  config.headers = {
    ...((config.headers as Record<string, unknown>) || {}),
    [ACCOUNT_UID_HEADER]: String(accountUid),
  }
  return config
}

/**
 * 没有后端可识别重放令牌的写请求，传输层最多重试几次。
 *
 * 对**所有**写请求（含 DELETE/PUT/POST）无限重放：一次响应丢失就能在断线期间
 * 反复重发，连接恢复后把旧删除/旧改名作用到已经重建的记录或次日数据上。
 * 控制请求带令牌走 `_maxAttempts`，其余写请求照旧按这个上限。
 */
const MAX_UNSAFE_WRITE_RETRIES = 2

/** 标记"请求已发出，但结果未知"：调用方据此提示核对，而不是当作失败重做。 */
function markResultUnknown(error: unknown, config?: RetryConfig): unknown {
  const target = error as Record<string, unknown>
  target.resultUnknown = true
  if (config) target.requestUrl = config.url
  return error
}

let instance: AxiosInstance | null = null

/** 取消错误判断：取消不重试，也不提示网络错误。 */
function isCancellation(error: unknown): boolean {
  if (axios.isCancel(error)) return true
  const axiosError = error as AxiosError | undefined
  if (axiosError?.code === 'ERR_CANCELED' || axiosError?.code === 'ECONNABORTED') {
    // ECONNABORTED 只有在 signal 已中止时才算取消（否则是超时）。
    return Boolean(axiosError?.config?.signal?.aborted)
  }
  return false
}

/** 可被 AbortSignal 打断的退避等待。 */
function delayWithAbort(ms: number, signal?: GenericAbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new axios.CanceledError('canceled'))
      return
    }
    const timer = setTimeout(finish, ms)
    function finish() {
      cleanup()
      resolve()
    }
    function onAbort() {
      cleanup()
      reject(new axios.CanceledError('canceled'))
    }
    function cleanup() {
      clearTimeout(timer)
      signal?.removeEventListener?.('abort', onAbort)
    }
    signal?.addEventListener?.('abort', onAbort, { once: true })
  })
}

// 获取后端基础 URL（优先从 Electron 环境获取）
function getBaseURL(): string {
  // Electron 环境：后端固定监听 127.0.0.1:8000
  if (typeof window !== 'undefined' && (window as { electronAPI?: unknown }).electronAPI) {
    return 'http://127.0.0.1:8000'
  }
  // 开发模式通过 Vite proxy 转发，baseURL 留空
  return ''
}

function getInstance(): AxiosInstance {
  if (instance) return instance

  instance = axios.create({
    baseURL: getBaseURL(),
    timeout: 15000,
    headers: {
      'Content-Type': 'application/json',
    },
    // localhost 直连：避免环境代理劫持本机后端请求
    proxy: false,
  })

  // ==================== 请求拦截器 ====================
  instance.interceptors.request.use(
    async (config: InternalAxiosRequestConfig) => {
      const headers = config.headers as unknown as Record<string, unknown>
      const existing = headers[OPERATION_TOKEN_HEADER]
      // 控制类操作的幂等令牌：写在 config 上，网络失败重放时复用同一个值，
      // 后端据此识别"响应丢失后的重试"。
      // - 新的控制操作：由**客户端**生成一个新令牌（后端不再签发票据）；
      // - 已经带有令牌（同一份 config 被重试复用）→ 原样保留，不新签、不换票；
      // - 控制请求的传输重试有界（`_maxAttempts`），不无限重放。
      if (isControlUrl(config.url)) {
        const retryConfig = config as RetryConfig
        if (typeof existing !== 'string' || existing.length === 0) {
          const issued = ensureOperationToken(
            headers as unknown as Parameters<typeof ensureOperationToken>[0])
          if (issued.length > OPERATION_TOKEN_MAX_LENGTH) {
            // 超过后端上限会让请求 400；如实暴露，不静默截断。
            console.error('[operation-token] 客户端令牌超过服务端长度上限')
          }
        }
        retryConfig._replaySafe = true
        retryConfig._controlRequest = true
        if (typeof retryConfig._maxAttempts !== 'number') {
          retryConfig._maxAttempts = CONTROL_MAX_ATTEMPTS
        }
      }
      return config
    },
    (error: AxiosError) => {
      return Promise.reject(error)
    }
  )

  // ==================== 响应拦截器 ====================
  instance.interceptors.response.use(
    (response) => {
      // 返回 response.data 解包，但 TypeScript 类型不会自动跟随此变更
      return response.data
    },
    async (error: AxiosError) => {
      // 用户取消：不重试、不提示网络错误，直接向上传递（页面保持可操作）。
      if (isCancellation(error)) {
        return Promise.reject(error)
      }

      const config = error.config as RetryConfig | undefined

      if (!error.response && config) {
        // 一次逻辑请求内部的**前序未知**事实：这一次已经发出，却没有拿到任何回包。
        // 无论随后是重试还是预算用尽，这个事实都已成立——后续任何回包都不能当作
        // "原操作从未发生"（调用方据此继续持有原命令）。
        config._onAttemptLost?.()
        // 预算用尽就先失败：这是**贯通传输层**的总次数上限，判断必须在"要不要
        // 再重试"之前，否则预算等于只是把无限重试改慢一点。
        const attemptsMade = (config._retryCount || 0) + 1
        if (typeof config._maxAttempts === 'number'
            && attemptsMade >= config._maxAttempts) {
          return Promise.reject(error)
        }
        // 网络错误（断线）。写请求分两类处理：
        // - 带重放令牌（控制意图）：后端能判重放，允许持续退避重试；
        // - 其余写请求（任务 CRUD、设置保存…）：至多重试 2 次后明确告知
        //   "结果待确认"，绝不无限重放。
        const method = String(config.method || 'get').toLowerCase()
        const isWrite = method !== 'get' && method !== 'head' && method !== 'options'
        if (isWrite && !config._replaySafe
            && (config._retryCount || 0) >= MAX_UNSAFE_WRITE_RETRIES) {
          markResultUnknown(error, config)
          ElMessage.warning('网络中断：该操作结果待确认，恢复后请刷新页面核对')
          return Promise.reject(error)
        }
        // 关键：经**同一实例**重试（不是全局 axios），重试请求再次进入
        // 请求/响应拦截器——令牌复用与错误分类全部保留。
        const plan = retryPlan(config._retryCount || 0)
        config._retryCount = plan.attempt
        if (config._retryCount === 1) {
          ElMessage.warning('网络连接失败，正在尝试重连...')
        }
        try {
          await delayWithAbort(plan.delayMs, config.signal)
        } catch (cancelError) {
          return Promise.reject(cancelError)
        }
        const recovered = config._retryCount > 1
        try {
          const data = await instance!.request(config)
          if (recovered) {
            ElMessage.success('网络已恢复')
          }
          return data
        } catch (retryError) {
          // 重试请求的失败已经在递归的拦截器里完整处理过
          //（继续重试 / 分类提示），这里原样上抛即可。
          return Promise.reject(retryError)
        }
      }

      if (!error.response) {
        return Promise.reject(error)
      }

      // 服务器返回错误
      const status = error.response.status
      const data = error.response.data as { detail?: string }
      // 控制请求的错误由 `api/liveControl.ts` 按回执语义呈现（受理/待确认/拒绝
      // 三种都不是"网络失败"），这里不弹通用 toast，避免把业务拒绝说成网络问题。
      const silentToast = Boolean(
        (error.config as RetryConfig | undefined)?._controlRequest)

      switch (status) {
        case 400:
          if (!silentToast) ElMessage.error(data?.detail || '请求参数错误')
          break
        case 401:
          // 桌面没有独立面板密码：401 = B 站登录态失效/缺失。
          if (!silentToast) {
            ElMessage.warning('登录已过期，请重新登录')
            try {
              const { useAuthStore } = await import('@/stores/auth')
              const authStore = useAuthStore()
              authStore.logoutLocally()
              const router = (await import('@/router')).default
              router.push({ name: 'Login' })
            } catch { /* 防止循环引用 */ }
          }
          break
        case 404:
          if (!silentToast) ElMessage.error('请求的资源不存在')
          break
        case 409:
          // 控制请求的 409 是**回执**（rejected / token_conflict），由调用方按
          // reason 呈现；其余 409（并发冲突/限流）明确提示，由用户决定是否重试。
          // 两种都不自动换令牌重发。
          if (!silentToast) ElMessage.warning(data?.detail || '操作冲突，请稍后重试')
          break
        case 500:
          if (!silentToast) ElMessage.error(data?.detail || '服务器内部错误')
          break
        default:
          if (!silentToast) ElMessage.error(data?.detail || `请求失败 (${status})`)
      }

      return Promise.reject(error)
    }
  )

  return instance
}

/**
 * ApiWrapper - 类型安全的请求封装
 */
class ApiWrapper {
  private axios: AxiosInstance

  constructor() {
    this.axios = getInstance()
  }

  /**
   * GET 请求。
   *
   * ``options.maxAttempts`` 把"这次读最多发几次"交给传输层执行：状态轮询这类
   * 读请求要的是**总预算**，而不是"每一层各重试几次"（后者会相乘）。
   * ``options.signal`` 用来真正中止在途请求与退避等待。
   */
  async get<T = unknown>(url: string, params?: Record<string, unknown>,
                         options?: { signal?: AbortSignal;
                                     maxAttempts?: number;
                                     timeout?: number;
                                     headers?: Record<string, string>;
                                     accountUid?: number | null }): Promise<T> {
    const config = { params, signal: options?.signal } as AxiosRequestConfig
    if (typeof options?.maxAttempts === 'number') {
      ;(config as unknown as RetryConfig)._maxAttempts = options.maxAttempts
    }
    if (typeof options?.timeout === 'number') {
      config.timeout = options.timeout
    }
    if (options?.headers) {
      config.headers = { ...options.headers }
    }
    withAccountUid(config, options?.accountUid)
    return this.axios.get(url, config) as Promise<T>
  }

  /**
   * POST 请求。
   *
   * ``options.token``：本次操作**冻结**的 `X-Operation-Token`。给了就写进
   * 请求头，拦截器见到已有令牌不会再新签——一次用户操作一个令牌，同一次操作的
   * 传输重试复用同一个。
   *
   * ``options.accountUid``：本次操作**冻结**的账号归属（`X-Account-UID`）。同样
   * 由调用方在用户选定时取一次，传输重试复用同一份 config，**不在重试时重取**。
   *
   * ``options.maxAttempts``：本次逻辑请求允许实际发出的总次数（含首次）。
   * 控制请求由拦截器默认给 `CONTROL_MAX_ATTEMPTS`。
   *
   * ``options.timeout``：技术等待预算超过 axios 全局 15 s 的写口显式给出自己的
   * 预算，否则客户端会先于后端放弃、把一个仍在正常执行的请求报成"结果待核对"。
   *
   * ``options.onAttemptLost``：本次逻辑请求有物理尝试在途丢失时回调一次
   * （见 `AttemptLostNotifier`）。创建的写口必须带上它：一次逻辑调用内部可能已经
   * 真实提交过一次、只是回包丢了，此时最后收到的拒绝不能作为原操作的终态。
   */
  async post<T = unknown>(url: string, data?: unknown, options?: {
    signal?: AbortSignal
    token?: string
    maxAttempts?: number
    timeout?: number
    accountUid?: number | null
    onAttemptLost?: AttemptLostNotifier
  }): Promise<T> {
    const config: AxiosRequestConfig = { signal: options?.signal }
    if (typeof options?.maxAttempts === 'number') {
      ;(config as unknown as RetryConfig)._maxAttempts = options.maxAttempts
    }
    if (typeof options?.timeout === 'number') {
      config.timeout = options.timeout
    }
    if (typeof options?.onAttemptLost === 'function') {
      ;(config as unknown as RetryConfig)._onAttemptLost = options.onAttemptLost
    }
    if (typeof options?.token === 'string' && options.token.length > 0) {
      config.headers = { [OPERATION_TOKEN_HEADER]: options.token }
    }
    withAccountUid(config, options?.accountUid)
    return this.axios.post(url, data, config) as Promise<T>
  }

  async put<T = unknown>(url: string, data?: unknown,
                         options?: { signal?: AbortSignal;
                                     headers?: Record<string, string>;
                                     accountUid?: number | null }): Promise<T> {
    const config: AxiosRequestConfig = {
      signal: options?.signal,
      headers: options?.headers,
    }
    withAccountUid(config, options?.accountUid)
    return this.axios.put(url, data, config) as Promise<T>
  }

  /**
   * 上传文件（FormData）。
   *
   * 必须经**同一实例**：错误分类与网络重试协议都与其余请求一致。
   * 不手工设置 Content-Type——交给 axios/浏览器带出 multipart boundary，
   * 手工写成 `application/json` 会让后端解析不到 file 字段。
   */
  async postForm<T = unknown>(url: string, formData: FormData,
                              options?: { signal?: AbortSignal;
                                          accountUid?: number | null }): Promise<T> {
    const config: AxiosRequestConfig = { signal: options?.signal }
    withAccountUid(config, options?.accountUid)
    return this.axios.post(url, formData, config) as Promise<T>
  }

  async delete<T = unknown>(url: string, options?: {
    accountUid?: number | null
  }): Promise<T> {
    const config: AxiosRequestConfig = {}
    withAccountUid(config, options?.accountUid)
    return this.axios.delete(url, config) as Promise<T>
  }

  /**
   * 下载二进制响应（Excel 导出等）。
   *
   * 必须经**同一实例**：401 处理与网络重试协议都与其余请求完全一致。
   * 拦截器返回 response.data，因此这里拿到的是二进制缓冲，不做 JSON 解包。
   */
  async getBlob(url: string): Promise<Blob> {
    // 用 arraybuffer 而非 blob：axios 浏览器适配器下 blob 返回 Blob，但 node
    // 适配器会退化成字符串；arraybuffer 两端都返回二进制缓冲，再显式构造 Blob，
    // 保证浏览器与测试/CLI 环境下的行为一致。
    const data = await (this.axios.get(url, { responseType: 'arraybuffer' }) as unknown as Promise<ArrayBuffer | Uint8Array>)
    return new Blob([data as unknown as BlobPart], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
  }
}

let apiInstance: ApiWrapper | null = null

export function useRequest(): ApiWrapper {
  if (!apiInstance) {
    apiInstance = new ApiWrapper()
  }
  return apiInstance
}

export default useRequest
