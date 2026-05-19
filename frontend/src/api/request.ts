/**
 * request.ts - Axios 请求封装
 *
 * 功能：
 * 1. 基础实例配置
 * 2. 请求/响应拦截器
 * 3. 统一错误处理
 * 4. 断线重连（指数退避）
 * 5. 与 Electron 兼容的 baseURL 获取
 * 6. 封装返回类型（解决 Axios interceptor 类型传播问题）
 */

import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

// 获取后端基础 URL（优先从 Electron 环境获取）
function getBaseURL(): string {
  // Electron 环境：后端固定监听 127.0.0.1:8000
  if (window.electronAPI) {
    return 'http://127.0.0.1:8000'
  }
  // 开发模式通过 Vite proxy 转发，baseURL 留空
  return ''
}

let instance: AxiosInstance | null = null

function getInstance(): AxiosInstance {
  if (instance) return instance

  instance = axios.create({
    baseURL: getBaseURL(),
    timeout: 15000,
    headers: {
      'Content-Type': 'application/json',
    },
  })

  // ==================== 请求拦截器 ====================
  instance.interceptors.request.use(
    (config: InternalAxiosRequestConfig) => {
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
      if (!error.response) {
        // 网络错误（断线）—— 无限重试，指数退避上限30s
        const config = error.config as InternalAxiosRequestConfig & { _retryCount?: number }
        config._retryCount = (config._retryCount || 0) + 1
        const delay = Math.min(1000 * Math.pow(2, config._retryCount), 30000)

        if (config._retryCount === 1) {
          ElMessage.warning('网络连接失败，正在尝试重连...')
        }

        await new Promise(resolve => setTimeout(resolve, delay))
        try {
          const res = await axios.request(config)
          if (config._retryCount > 1) {
            ElMessage.success('网络已恢复')
          }
          return res.data
        } catch (retryError) {
          return Promise.reject(retryError)
        }
      }

      // 服务器返回错误
      const status = error.response.status
      const data = error.response.data as { detail?: string }

      switch (status) {
        case 400:
          ElMessage.error(data?.detail || '请求参数错误')
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
          ElMessage.error('请求的资源不存在')
          break
        case 500:
          ElMessage.error(data?.detail || '服务器内部错误')
          break
        default:
          ElMessage.error(data?.detail || `请求失败 (${status})`)
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
 * 此处通过泛型 <T> 将类型传播给调用方，
 * 彻底消除各处 `: any` 断言。
 */
class ApiWrapper {
  private axios: AxiosInstance

  constructor() {
    this.axios = getInstance()
  }

  async get<T = unknown>(url: string, params?: Record<string, unknown>): Promise<T> {
    return this.axios.get(url, { params }) as Promise<T>
  }

  async post<T = unknown>(url: string, data?: unknown): Promise<T> {
    return this.axios.post(url, data) as Promise<T>
  }

  async put<T = unknown>(url: string, data?: unknown): Promise<T> {
    return this.axios.put(url, data) as Promise<T>
  }

  async delete<T = unknown>(url: string): Promise<T> {
    return this.axios.delete(url) as Promise<T>
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