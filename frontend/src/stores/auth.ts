
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useRequest } from '@/api/request'
import cache from '@/composables/useCache'
import type {
  UserInfo,
  LoginStatusResponse,
  QRCodeResponse,
  PollLoginResponse,
  LogoutResponse,
} from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const isLoggedIn = ref(false)
  const userInfo = ref<UserInfo | null>(null)
  const qrcodeKey = ref('')
  const qrcodeUrl = ref('')
  const isLoading = ref(false)

  const request = useRequest()

  // 检查登录状态（已登录则跳过，防止竞态覆盖）
  async function checkLoginStatus() {
    // 已登录状态不再重新验证，避免旧请求覆盖
    if (isLoggedIn.value) return true

    try {
      const res = await request.get<LoginStatusResponse>('/api/auth/status')
      if (res.logged_in) {
        isLoggedIn.value = true
        if (res.user_info) {
          userInfo.value = res.user_info
          cache.set('user_info', res.user_info)
        }
      }
      // res.logged_in 为 false 时，不覆盖已有状态（可能是旧请求）
    } catch (error) {
      console.error('检查登录状态失败:', error)
      if (!isLoggedIn.value) {
        const cached = cache.get<UserInfo>('user_info')
        if (cached) {
          userInfo.value = cached
          isLoggedIn.value = true
        }
      }
    }
    return isLoggedIn.value
  }

  // 获取二维码
  async function fetchQRCode(): Promise<QRCodeResponse> {
    isLoading.value = true
    try {
      const res = await request.get<QRCodeResponse>('/api/auth/qrcode')
      qrcodeUrl.value = res.qrcode_url
      qrcodeKey.value = res.qrcode_key
      return res
    } catch (error) {
      console.error('获取二维码失败:', error)
      throw error
    } finally {
      isLoading.value = false
    }
  }

  // 轮询登录状态
  async function pollLoginStatus(key: string): Promise<PollLoginResponse> {
    try {
      const res = await request.post<PollLoginResponse>('/api/auth/poll/' + key)
      if (res.logged_in && res.user_info) {
        isLoggedIn.value = true
        userInfo.value = res.user_info
        cache.set('user_info', res.user_info)
        return { logged_in: true, user_info: res.user_info }
      }
      return { logged_in: false }
    } catch (error) {
      console.error('轮询登录状态失败:', error)
      return { logged_in: false }
    }
  }

  // 登出
  async function logout() {
    try {
      await request.post<LogoutResponse>('/api/auth/logout')
    } catch (error) {
      console.error('登出失败:', error)
    }
    isLoggedIn.value = false
    userInfo.value = null
    cache.remove('user_info')
  }

  return {
    isLoggedIn,
    userInfo,
    qrcodeKey,
    qrcodeUrl,
    isLoading,
    checkLoginStatus,
    fetchQRCode,
    pollLoginStatus,
    logout,
  }
})

