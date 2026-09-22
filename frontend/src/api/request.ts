/**
 * request.ts - Axios 请求封装
 *
 * 功能：
 * 1. 基础实例配置（Electron localhost baseURL / Vite 开发代理）
 * 2. 请求/响应拦截器
 * 3. 统一错误处理
 * 4. 断线重连（指数退避，**经同一实例**重试，持续且可取消）
 * 5. 封装返回类型（解决 Axios interceptor 类型传播问题）
 *
 * A3 重试协议（移植自服务器版，桌面语义）：
 * - 网络失败后经**同一实例**递归重试：重试请求再次经过请求/响应拦截器，
 *   票据复用、响应分类与数据解包全部保留；不会出现"第二次失败绕过
 *   拦截器直接 reject"的暗路径（旧版用全局 axios.request 重试）。
 * - 传输层失败重试**绝不更换票据**（同一份 config 复用）。
 * - 取消：调用方传入 AbortSignal 即可随时取消；取消不重试，
 *   退避等待也会被立刻打断，页面保持可操作。
 * - 控制操作（start/stop/resume/run-next/switch-area/confirm-face-verify）
 *   每次新意图向后端取一张签发票据（X-Operation-Token）。
 */

import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig,
                 GenericAbortSignal } from 'axios'
import { ElMessage } from 'element-plus'
import {
  retryPlan,
  classifyControlResponse,
  classifyTicketFailure,
  isControlUrl,
  createServerTicketFetcher,
  ensureOperationToken,
  newOperationToken,
  OPERATION_TOKEN_HEADER,
} from './operationToken'

// 获取后端基础 URL（优先从 Electron 环境获取）
function getBaseURL(): string {
  // Electron 环境：后端固定监听 127.0.0.1:8000
  if (window.electronAPI) {
    return 'http://127.0.0.1:8000'
  }
  // 开发模式通过 Vite proxy 转发，baseURL 留空
  return ''
}

type RetryConfig = InternalAxiosRequestConfig & {
  _retryCount?: number
  signal?: AbortSignal
  /** 标记"这是内部取票请求"：响应错误不弹通用 toast，由取票逻辑分类处理。 */
  _ticketRequest?: boolean
  /** 已有服务端票据，可安全识别同一控制意图的传输层重放。 */
  _replaySafe?: boolean
  /** 一次逻辑请求允许实际发出的总次数（含首次）。 */
  _maxAttempts?: number
}

let instance: AxiosInstance | null = null

/** 没有服务端重放票据的写请求最多重试两次，之后结果必须待确认。 */
const MAX_UNSAFE_WRITE_RETRIES = 2

function markResultUnknown(error: unknown, config?: RetryConfig): unknown {
  const target = error as Record<string, unknown>
  target.resultUnknown = true
  if (config) target.requestUrl = config.url
  return error
}

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

  // 服务端签发票据的取票器：**经同一实例**取票（同一 baseURL、可取消），
  // operation-ticket 不属于控制 URL，不会递归进入取票逻辑。
  // 无缓存：一项新控制意图对应一张新票据。
  const serverTickets = createServerTicketFetcher(
    (url, signal) => instance!.get(url, {
      signal,
      timeout: 2000,
      _ticketRequest: true,
    } as unknown as InternalAxiosRequestConfig) as unknown as Promise<{ ticket?: string }>
  )

  /**
   * 为一次新的控制意图取票。
   * - 网络失败：取票请求本身经实例传输层无限重试（可取消）——与控制请求
   *   的瞬态恢复策略一致；控制请求在此等待，直到取到票或被取消；
   * - 5xx：这里退避重试（响应 toast 已按 _ticketRequest 抑制）；
   * - 404：旧后端没有签发端点——唯一允许回退客户端 UUID 的情况，
   *   属于明确的旧 API 兼容边界（console.warn 一次性声明，不静默）。
   */
  async function obtainServerTicket(signal?: GenericAbortSignal): Promise<string | null> {
    let ticketRetries = 0
    let warnedLegacy = false
    for (;;) {
      try {
        return await serverTickets.fetch(signal)
      } catch (error) {
        if (isCancellation(error)) throw error
        const status = (error as AxiosError).response?.status
        const failure = classifyTicketFailure(status)
        if (failure === 'legacy_backend') {
          if (!warnedLegacy) {
            warnedLegacy = true
            console.warn('[operation-token] 后端无签发票据端点（旧版本后端），'
                         + '控制操作回退客户端 UUID；重放保护能力受限')
          }
          return null
        }
        // transient（网络失败 / 5xx）：退避后重试取票。
        const plan = retryPlan(ticketRetries)
        ticketRetries = plan.attempt
        await delayWithAbort(plan.delayMs, signal)
      }
    }
  }

  // ==================== 请求拦截器 ====================
  instance.interceptors.request.use(
    async (config: InternalAxiosRequestConfig) => {
      const headers = config.headers as unknown as Record<string, unknown>
      const existing = headers[OPERATION_TOKEN_HEADER]
      // 控制类操作的幂等票据：写在 config 上，网络失败重放时复用同一个值，
      // 后端据此识别"响应丢失后的重试"。
      // - 新的控制操作：向后端取**一张新的**签发票据（无跨意图缓存）；
      // - 已经带有票据（同一份 config 被重试复用）→ 原样保留，不新签、不换票。
      if (typeof existing !== 'string' || existing.length === 0) {
        if (isControlUrl(config.url)) {
          const ticket = await obtainServerTicket(config.signal)
          if (ticket) {
            headers[OPERATION_TOKEN_HEADER] = ticket
            ;(config as RetryConfig)._replaySafe = true
            return config
          }
          // 只有旧后端（404）才会走到这里：明确的兼容边界。
        }
        ensureOperationToken(headers, newOperationToken)
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
        const attemptsMade = (config._retryCount || 0) + 1
        if (typeof config._maxAttempts === 'number'
            && attemptsMade >= config._maxAttempts) {
          return Promise.reject(error)
        }
        const method = String(config.method || 'get').toLowerCase()
        const isWrite = method !== 'get' && method !== 'head' && method !== 'options'
        if (isWrite && !config._replaySafe
            && (config._retryCount || 0) >= MAX_UNSAFE_WRITE_RETRIES) {
          markResultUnknown(error, config)
          ElMessage.warning('网络中断：该操作结果待确认，恢复后请刷新页面核对')
          return Promise.reject(error)
        }
        // 网络错误（断线）——持续重试，指数退避上限 30s，可用 signal 取消。
        // 关键：经**同一实例**重试（不是全局 axios），重试请求再次进入
        // 请求/响应拦截器——票据复用与错误分类全部保留。
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
          return Promise.reject(retryError)
        }
      }

      if (!error.response) {
        return Promise.reject(error)
      }

      // 服务器返回错误
      const status = error.response.status
      const data = error.response.data as { detail?: string }
      // 内部取票请求的错误由 obtainServerTicket 分类处理（重试/降级/取消），
      // 不弹通用 toast，避免重试风暴期间刷屏。
      const silentToast = Boolean((error.config as RetryConfig | undefined)?._ticketRequest)
      const disposition = classifyControlResponse(status, data?.detail)

      // 停止已生效的旧操作重放：必须明确失败，不能用新票据重发，
      // 否则等于把用户取消掉的开播又拉起来。
      if (disposition === 'stale_operation') {
        ElMessage.error(data?.detail || '该操作已被停止取消，未重复执行')
        return Promise.reject(error)
      }

      switch (status) {
        case 400:
          if (!silentToast) ElMessage.error(data?.detail || '请求参数错误')
          break
        case 401:
          ElMessage.warning('登录已过期，请重新登录')
          try {
            const { useAuthStore } = await import('@/stores/auth')
            const authStore = useAuthStore()
            authStore.logout()
            const router = (await import('@/router')).default
            router.push({ name: 'Login' })
          } catch { /* 防止循环引用 */ }
          break
        case 404:
          if (!silentToast) ElMessage.error('请求的资源不存在')
          break
        case 409:
          // 并发冲突/限流：明确提示，由用户决定是否重试；
          // 不在此自动换票重发（见 stale_operation 分支的说明）。
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
 *
 * 由于 Axios 响应拦截器统一解包 response.data，
 * 此处通过泛型 <T> 将类型传播给调用方。
 */
class ApiWrapper {
  private axios: AxiosInstance

  constructor() {
    this.axios = getInstance()
  }

  async get<T = unknown>(url: string, params?: Record<string, unknown>, options?: { signal?: AbortSignal; maxAttempts?: number }): Promise<T> {
    const config = { params, signal: options?.signal } as InternalAxiosRequestConfig
    if (typeof options?.maxAttempts === 'number') {
      ;(config as unknown as RetryConfig)._maxAttempts = options.maxAttempts
    }
    return this.axios.get(url, config) as Promise<T>
  }

  async post<T = unknown>(url: string, data?: unknown, options?: { signal?: AbortSignal }): Promise<T> {
    return this.axios.post(url, data, { signal: options?.signal }) as Promise<T>
  }

  async put<T = unknown>(url: string, data?: unknown, options?: { signal?: AbortSignal; headers?: Record<string, string> }): Promise<T> {
    return this.axios.put(url, data, {
      signal: options?.signal,
      headers: options?.headers,
    }) as Promise<T>
  }

  async postForm<T = unknown>(url: string, formData: FormData, options?: { signal?: AbortSignal }): Promise<T> {
    // The shared instance defaults to JSON.  Leaving that default on a
    // FormData request makes Axios/browser serialize the File as `{}` and
    // removes the multipart boundary.  `undefined` clears the inherited
    // header so the browser supplies `multipart/form-data; boundary=...`.
    // The same Axios config still carries the operation token and retry
    // counter, so a transport retry reuses the original ticket.
    return this.axios.post(url, formData, {
      signal: options?.signal,
      headers: { 'Content-Type': undefined },
    }) as Promise<T>
  }

  async delete<T = unknown>(url: string): Promise<T> {
    return this.axios.delete(url) as Promise<T>
  }

  async getBlob(url: string): Promise<Blob> {
    const data = await (this.axios.get(url, {
      responseType: 'arraybuffer',
    }) as unknown as Promise<ArrayBuffer | Uint8Array>)
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
