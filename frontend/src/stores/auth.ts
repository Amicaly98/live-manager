
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRequest } from '@/api/request'
import { boot } from '@/boot'
import { operationEvents } from '@/stores/operationEvents'
import cache from '@/composables/useCache'
import type {
  UserInfo,
  LoginStatusResponse,
  QRCodeResponse,
  PollLoginResponse,
  LogoutResponse,
} from '@/types/api'

type LogoutResult = {
  ok: boolean
  superseded?: boolean
  blocked?: boolean
}

/** 业务拒绝只按稳定错误码分支，不能把可变的中文 detail 当协议。 */
function responseCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return undefined
  const response = (error as { response?: unknown }).response
  if (!response || typeof response !== 'object') return undefined
  const data = (response as { data?: unknown }).data
  if (!data || typeof data !== 'object') return undefined
  const code = (data as { code?: unknown }).code
  return typeof code === 'string' ? code : undefined
}

export const useAuthStore = defineStore('auth', () => {
  const isLoggedIn = ref(false)
  const userInfo = ref<UserInfo | null>(null)
  const qrcodeKey = ref('')
  const qrcodeUrl = ref('')
  const isLoading = ref(false)

  const request = useRequest()

  // ==================== 认证意图（S1，2026-09-21） ====================
  //
  // 登录/登出是**用户的显式意图**，状态查询（auth/status）只是对既有会话的复核，
  // 永远无权产生或恢复一次登录。旧实现的缺陷链：logout 先清本地并作废启动快照 →
  // App 随即自动重读 auth/status → 服务端尚未处理完登出、查询合法地返回 true →
  // 结果被写回 store，"刚登出的人被重新查询登录回去"。
  //
  // - `_authIntentSeq`：单调递增的意图代际。每次显式认证动作（退出开始、登录提交）
  //   前进一步；读取在发起时捕获代际，**写回之前**复核——代际不一致的响应一律
  //   丢弃（含用户缓存写入），不能只靠 BootController 拒绝发布而 store 已被污染。
  // - `_logoutIntent`：本会话内用户表达过退出意图。此后 status 查询**不再发起也
  //   不再采信**（服务端此刻可能尚未处理完登出，任何 true 都是"尚未处理登出的
  //   旧会话"）；恢复登录只能来自新的显式登录（扫码提交）。页面整体刷新是新的
  //   会话引导，不受此标记约束。
  // 退出请求本身的结果（成功/失败/被新登录取代）由 `logout()` 如实返回给调用方，
  // 不需要单独的"退出中"标志：退出意图窗口内的一切行为都由 `_logoutIntent` 界定，
  // 请求 settle 只影响对用户的结果呈现，不产生额外状态。
  let _authIntentSeq = 0
  let _logoutIntent = false

  /**
   * 检查登录状态（已登录则跳过，防止竞态覆盖）。
   *
   * 2026-09-21（A0）两点变化：
   * - `signal` / `maxAttempts` 透传到传输层：启动读取要有总预算，且必须是**真取消**；
   * - 读取失败**不再用缓存资料冒充登录成功**。旧实现在 `/api/auth/status` 失败时
   *   把缓存的昵称头像当成"已登录"，于是启动流程会带着一个未经验证的登录态去发
   *   受保护的任务请求，把 401 → 跳登录 → 再 401 的循环放大。缓存资料现在只用于
   *   **显示**，不参与鉴权判断。
   */
  async function checkLoginStatus(options: { signal?: AbortSignal;
                                             maxAttempts?: number } = {}) {
    // 退出意图存在期间（含退出请求尚未明确结束）：不发查询、不接受查询结果。
    // status 查询是对既有会话的复核，无权在退出后恢复登录。
    if (_logoutIntent) return false
    // 已登录状态不再重新验证，避免旧请求覆盖
    if (isLoggedIn.value) return true

    // 发起时捕获意图代际；写回前复核（S1）。
    const intentSeq = _authIntentSeq
    try {
      const res = await request.get<LoginStatusResponse>('/api/auth/status', undefined, {
        signal: options.signal,
        maxAttempts: options.maxAttempts,
      })
      // 退出/新登录发生后才回来的旧读取：不写状态、不写缓存。
      if (intentSeq !== _authIntentSeq) return isLoggedIn.value
      if (res?.logged_in) {
        isLoggedIn.value = true
        if (res.user_info) {
          userInfo.value = res.user_info
          cache.set('user_info', res.user_info)
        }
      }
      // res.logged_in 为 false 时，不覆盖已有状态（可能是旧请求）
      return isLoggedIn.value
    } catch (error) {
      // 已被更新意图取代的旧读取：维持现状上抛，不做显示恢复。
      if (intentSeq !== _authIntentSeq) throw error
      console.error('检查登录状态失败:', error)
      // 缓存资料只补显示，绝不当成"平台认证仍然有效"。
      if (!isLoggedIn.value && !userInfo.value) {
        const cached = cache.get<UserInfo>('user_info')
        if (cached) userInfo.value = cached
      }
      // 向上抛，让启动编排把"超时 / 网络失败"分开呈现。
      throw error
    }
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
    // 发起时捕获意图代际：这是"当前这一次扫码"的上下文。
    const intentSeq = _authIntentSeq
    try {
      const res = await request.post<PollLoginResponse>('/api/auth/poll/' + key)
      if (res.logged_in && res.user_info) {
        // 退出发生后才回来的旧扫码回调（发起早于退出）：无权恢复登录或写缓存。
        if (intentSeq !== _authIntentSeq) return { logged_in: false }
        // 新的显式登录提交：取代退出意图（此后旧 logout 的迟到结果不得清掉它）。
        _authIntentSeq += 1
        _logoutIntent = false
        isLoggedIn.value = true
        userInfo.value = res.user_info
        cache.set('user_info', res.user_info)
        // 账号上下文变更：操作日志缓存按 uid 命名空间重载，
        // 旧账号的近期记录不会串进新会话（2026-09-21 工作包 A）。
        operationEvents.handleContextSwitch()
        // 平台登录成功 = 认证事实变更：作废启动快照，否则路由守卫会继续按
        // "启动时未登录"的旧结论把用户按回 /login（2026-09-21 R1）。
        boot.invalidate('platform-login')
        return { logged_in: true, user_info: res.user_info }
      }
      return { logged_in: false }
    } catch (error) {
      console.error('轮询登录状态失败:', error)
      return { logged_in: false }
    }
  }

  /**
   * 登出（B站账号）。
   *
   * 顺序刻意如此：**先定本地结论并作废启动快照，再尽力通知服务端**。
   * 如果先 await 服务端，网络不通时用户会卡在"点了登出但界面还是已登录"，
   * 而且启动快照里的 `loggedIn=true` 会继续放行受保护路由。
   *
   * S1（2026-09-21）补充语义：
   * - 退出是显式意图事件：意图代际前进一步，此前发起的旧 status / 旧扫码轮询 /
   *   旧启动读取从此无权恢复登录或写用户缓存（写回前复核代际）。
   * - 请求期间 `_logoutIntent=true`：业务读取停止，不靠"最后再清一次值"。
   * - 退出请求的结果**如实返回**（`{ ok }`）；失败时本地维持已退出（那是用户的
   *   明确意图），但用户会得到真实提示，而不是无限"退出中"。
   * - 服务端以稳定错误码拒绝退出（例如直播仍在进行）时，只对当前这一代恢复
   *   退出前的本地身份/缓存，释放退出意图并重新驱动业务读取；旧请求迟到时
   *   不得恢复旧账号或影响后来登录。
   * - 旧登出的迟到成功/失败**不得清掉后来成功的新登录**：settle 时复核代际，
   *   已被新登录取代就什么都不改。
   * - 客户端边界：这里不宣称"前端取消 = 服务端会话已撤销"；失败提示里说明了
   *   服务端通知未完成。不改后端 clear_cookies 行为。
   */
  async function logout(): Promise<LogoutResult> {
    const intentSeq = ++_authIntentSeq
    const previousLoggedIn = isLoggedIn.value
    const previousUserInfo = userInfo.value
    const previousCachedUser = cache.get<UserInfo>('user_info')
    _logoutIntent = true
    isLoggedIn.value = false
    userInfo.value = null
    cache.remove('user_info')
    // 账号上下文变更：切回匿名命名空间，旧账号的记录不再展示（工作包 A）。
    operationEvents.handleContextSwitch()
    boot.invalidate('platform-logout')
    try {
      await request.post<LogoutResponse>('/api/auth/logout')
      if (intentSeq === _authIntentSeq) {
        return { ok: true }
      }
      // 已有更新的显式登录提交：旧登出就此作罢，不改任何状态。
      return { ok: true, superseded: true }
    } catch (error) {
      // 旧登出响应（无论是业务拒绝还是网络错误）不得给新登录添提示，
      // 更不能把旧快照恢复到新账号。
      if (intentSeq !== _authIntentSeq) {
        return { ok: false, superseded: true }
      }

      // The desktop backend's stable code is `auth_logout_blocked`.  Keep the
      // older `auth_session_active` alias readable while mixed-version local
      // bundles are being upgraded; both mean that the account must remain in
      // the UI and the user must stop the live session first.
      if (['auth_logout_blocked', 'auth_session_active'].includes(responseCode(error) || '')) {
        // 这是当前用户明确点击的退出被服务端拒绝：恢复点击前的本地状态，
        // 让当前页面继续工作。缓存的 TTL 由统一 cache 封装重新写入，避免
        // 直接拼接 localStorage 形状而破坏现有协议。
        isLoggedIn.value = previousLoggedIn
        userInfo.value = previousUserInfo ?? previousCachedUser ?? null
        if (previousCachedUser) cache.set('user_info', previousCachedUser)
        else cache.remove('user_info')
        _logoutIntent = false
        operationEvents.handleContextSwitch()
        // 登出开始时已作废启动快照并停掉受保护读取；拒绝后以恢复的认证事实
        // 开新代，让 App 重新放行业务读取而不依赖旧快照。
        boot.invalidate('platform-logout-rejected')
        ElMessage.warning('直播进行中，请先停止直播后再登出')
        return { ok: false, blocked: true }
      }

      console.error('登出请求失败:', error)
      // 真实结果交回调用方/用户：本地已退出，但服务端通知没有确认完成。
      ElMessage.warning('本地已退出，但服务端登出请求未完成（网络异常或超时）')
      return { ok: false }
    }
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

