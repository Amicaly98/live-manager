/**
 * auth.ts - B 站账号认证的唯一前端持有者（桌面移植阶段 2）。
 *
 * 与原桌面版的语义差别（都是服务端阶段 4C 已经收口的结论）：
 *  - 登录回包**统一经 `api/authWire.ts` 归一化**：安装成功是平铺字段，只有正整数
 *    uid 才是身份；身份缺失一律按"没有建立登录态"，不编造。
 *  - 退出**不再**先清本地再通知服务端：只有后端受理移交之后才撤销本地身份；被
 *    拒绝或结果不明时**保留原账号**并用只读 `/api/auth/status` 核对实际状态。
 *  - 退出同样走控制令牌协议（`X-Operation-Token`）。
 *
 * 桌面新增 `logoutLocally()`：请求层收到 401 时只做本地撤销，**不再**发一次
 * logout 请求（已经 401 说明服务端不认这次会话，补发只会再拿一个 401）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRequest } from '@/api/request'
import { normalizeLoginPayload } from '@/api/authWire'
import type { PollLoginWire } from '@/api/authWire'
import { newOperationToken } from '@/api/operationToken'
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
  /** 服务端明确拒绝（业务原因），本地身份**保持不变**。 */
  blocked?: boolean
  /** 结果属于更早的认证代次（已被新登录取代），本次不改任何状态。 */
  superseded?: boolean
  /** 传输结果不明：已按只读状态核对过，`ok` 反映核对结论。 */
  unconfirmed?: boolean
}

export const useAuthStore = defineStore('auth', () => {
  const isLoggedIn = ref(false)
  const userInfo = ref<UserInfo | null>(null)
  /** B 站扫码：二维码地址与**扫码代次**（重新获取会换代）。 */
  const qrcodeKey = ref('')
  const qrUrl = ref('')
  const qrGeneration = ref(0)
  /** 扫码状态码（后端原样透传）：86101 未扫 / 86090 已扫未确认 / 86038 已过期。 */
  const qrStatus = ref('')
  const qrMessage = ref('')
  const isLoading = ref(false)
  const isLoggingOut = ref(false)

  const request = useRequest()

  // ==================== 认证意图（S1，2026-09-21；4C 调整时序） ====================
  //
  // `_authIntentSeq`：单调递增的意图代际。每次显式认证动作（退出开始、登录提交）
  // 前进一步；读取在发起时捕获代际，**写回之前**复核——代际不一致的响应一律
  // 丢弃（含用户缓存写入）。
  //
  // 阶段 4C 的时序变化（修 B-3）：退出**不再**先清本地再通知服务端。
  // 服务端受理之后才撤销本地身份；被拒绝或结果不明时保留原账号，并用
  // 只读的 `/api/auth/status` 核对实际状态。这样"服务端仍有旧账号、界面却已经
  // 不认它"的裂缝就不存在了。
  let _authIntentSeq = 0

  function setLoggedIn(info: UserInfo | null) {
    isLoggedIn.value = Boolean(info)
    userInfo.value = info
    if (info) cache.set('user_info', info)
    else cache.remove('user_info')
  }

  /** 用**服务端只读结论**校准本地认证事实；代际已变则丢弃。 */
  async function reconcileAuthStatus(intentSeq: number): Promise<Boolean | null> {
    try {
      const res = await request.get<LoginStatusResponse>('/api/auth/status', undefined, {
        maxAttempts: 2,
      })
      if (intentSeq !== _authIntentSeq) return null
      const normalized = normalizeLoginPayload(res)
      if (normalized.logged_in && normalized.user_info) {
        setLoggedIn(normalized.user_info)
        return true
      }
      setLoggedIn(null)
      return false
    } catch {
      return null
    }
  }

  /**
   * 检查登录状态（已登录则跳过，防止竞态覆盖）。
   *
   * - `signal` / `maxAttempts` 透传到传输层：启动读取要有总预算，且必须是**真取消**；
   * - 读取失败**不用缓存资料冒充登录成功**。缓存只用于**显示**，不参与鉴权判断。
   */
  async function checkLoginStatus(options: { signal?: AbortSignal;
                                             maxAttempts?: number } = {}) {
    // 已登录状态不再重新验证，避免旧请求覆盖
    if (isLoggedIn.value) return true

    // 发起时捕获意图代际；写回前复核。
    const intentSeq = _authIntentSeq
    try {
      const res = await request.get<LoginStatusResponse>('/api/auth/status', undefined, {
        signal: options.signal,
        maxAttempts: options.maxAttempts,
      })
      if (intentSeq !== _authIntentSeq) return isLoggedIn.value
      const normalized = normalizeLoginPayload(res)
      if (normalized.logged_in) {
        // 后端只报内存里**已核验**的账号事实（GET 不查询房间）。
        setLoggedIn(normalized.user_info)
      }
      return isLoggedIn.value
    } catch (error) {
      if (intentSeq !== _authIntentSeq) throw error
      console.error('检查登录状态失败:', error)
      // 缓存资料只补显示，绝不当成"平台认证仍然有效"。
      if (!isLoggedIn.value && !userInfo.value) {
        const cached = cache.get<UserInfo>('user_info')
        if (cached) userInfo.value = cached
      }
      throw error
    }
  }

  /**
   * 获取二维码。每次调用都会**取代**上一次的扫码代次（服务端保证旧回包无效）。
   */
  async function fetchQRCode(): Promise<QRCodeResponse> {
    isLoading.value = true
    try {
      const res = await request.get<QRCodeResponse>('/api/auth/qrcode')
      qrcodeKey.value = String(res.qrcode_key || '')
      qrUrl.value = String(res.qr_url || '')
      qrGeneration.value = Number(res.generation) || 0
      qrStatus.value = ''
      qrMessage.value = ''
      return res
    } catch (error) {
      console.error('获取二维码失败:', error)
      throw error
    } finally {
      isLoading.value = false
    }
  }

  /**
   * 轮询这一次扫码的当前状态。
   *
   * **不是**所有 `logged_in=false` 都等于"还没扫"：`status` 携带真实原因，
   * 页面必须按它分别说明（等待扫码 / 已扫码待确认 / 已过期 / 已被更新的二维码
   * 取代 / 候选被拒绝）。旧二维码的回包**不**复活账号。
   *
   * 安装成功的真实回包是**平铺**的（uid/room_id/uname，没有 `user_info`）：
   * 身份统一经 `normalizeLoginPayload` 归一化，成功时真实建立 store 身份，
   * 让 `QRCodeLogin` 的成功分支与 `Login` 页的导航都能成立（复核 R1）。
   */
  async function pollLoginStatus(key: string): Promise<PollLoginResponse> {
    const intentSeq = _authIntentSeq
    try {
      const res = await request.post<PollLoginWire>('/api/auth/poll/' + key)
      const normalized = normalizeLoginPayload(res)
      qrStatus.value = normalized.status
      qrMessage.value = normalized.message
      if (normalized.logged_in && normalized.user_info) {
        if (intentSeq !== _authIntentSeq) return { logged_in: false }
        _authIntentSeq += 1
        setLoggedIn(normalized.user_info)
        operationEvents.handleContextSwitch()
        // 平台登录成功 = 认证事实变更：作废启动快照。
        boot.invalidate('platform-login')
        return { logged_in: true, user_info: normalized.user_info }
      }
      return { logged_in: false, status: normalized.status, message: normalized.message }
    } catch (error) {
      console.error('轮询登录状态失败:', error)
      return { logged_in: false, status: 'poll_failed', message: '轮询失败' }
    }
  }

  /**
   * 扫码状态 → 页面提示。**照抄服务端结论**，不在前端猜。
   *
   * 返回 null 表示"继续等待，不要改 UI"。
   */
  function describeQrStatus(status: string): {
    kind: 'waiting' | 'scanned' | 'expired' | 'stale' | 'rejected' | 'error'
    message: string
  } | null {
    switch (String(status || '')) {
      case '':
      case '86101':
        return { kind: 'waiting', message: '等待扫码…' }
      case '86090':
        return { kind: 'scanned', message: '已扫码，请在手机上确认登录' }
      case '86038':
        return { kind: 'expired', message: '二维码已过期，请重新获取' }
      case 'stale_generation':
        return { kind: 'stale', message: '这次扫码已被更新的二维码取代，请扫描当前二维码' }
      case 'candidate_rejected':
        return { kind: 'rejected', message: '扫码结果无法核验账号身份，未安装账号' }
      case 'no_session':
        return { kind: 'rejected', message: '扫码结果没有携带会话凭据，未安装账号' }
      case 'poll_failed':
        return { kind: 'error', message: '轮询失败，请稍后重试' }
      case 'rejected':
        return { kind: 'rejected', message: '账号安装被拒绝' }
      case 'installed':
        return null
      default:
        return { kind: 'error', message: '未知的扫码状态' }
    }
  }

  /**
   * 登出（B站账号）。**服务确认之后**才撤销本地身份（阶段 4C 修 B-3）。
   *
   * - 受理成功：此时才清本地账号与缓存、切换日志命名空间、作废启动快照；
   * - 服务端 409（业务暂拒，例如旧账号仍在收尾）：**保留**原账号与原缓存，
   *   显示服务端给的真实原因，并按只读状态核对一次；
   * - 传输结果不明：同样保留原账号，显示"结果待确认"，用只读状态核对——
   *   若服务端已经是未登录，则按实际状态落地（不谎称成功，也不假退出）；
   * - 已有更新的显式登录提交：本次结果作废，不改任何状态。
   */
  async function logout(): Promise<LogoutResult> {
    const intentSeq = ++_authIntentSeq
    isLoggingOut.value = true
    try {
      // 退出也是一次**控制操作**：服务端的唯一移交协议要求携带
      // `X-Operation-Token`（`ControlService._with_token`）。不带上就是 400
      // 「operation_token 缺失或长度无效」——真实浏览器点退出会直接失败。
      // 令牌在发起时就冻结，传输重试复用同一个（服务端按它识别"同一次移交"）。
      await request.post<LogoutResponse>('/api/auth/logout', undefined, {
        token: newOperationToken(),
      })
      if (intentSeq !== _authIntentSeq) {
        // 已有更新的显式登录提交：旧登出就此作罢，不改任何状态。
        return { ok: true, superseded: true }
      }
      // 服务端**已受理**移交：现在才撤销本地身份。
      _authIntentSeq += 1          // 作废在途的旧认证读取
      setLoggedIn(null)
      operationEvents.handleContextSwitch()
      boot.invalidate('platform-logout')
      return { ok: true }
    } catch (error) {
      if (intentSeq !== _authIntentSeq) {
        return { ok: false, superseded: true }
      }
      const detail = (error as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail
      const hasResponse = Boolean(
        (error as { response?: unknown } | undefined)?.response)
      // 只读核对：服务端此刻的**实际**账号事实才是结论。
      const actual = await reconcileAuthStatus(_authIntentSeq)
      if (actual === false) {
        // 服务端已经是未登录（例如移交其实已经生效）：按事实落地。
        _authIntentSeq += 1
        setLoggedIn(null)
        operationEvents.handleContextSwitch()
        boot.invalidate('platform-logout')
        return { ok: true }
      }
      // 旧账号仍在服务端：**保留**本地账号与缓存，如实说明原因。
      _authIntentSeq += 1
      if (hasResponse) {
        ElMessage.warning(detail || '退出未被受理，账号保持不变')
        return { ok: false, blocked: true }
      }
      ElMessage.warning('退出结果待确认：网络异常，账号状态按服务端事实保持不变')
      return { ok: false, unconfirmed: true }
    } finally {
      isLoggingOut.value = false
    }
  }

  /**
   * **只在本机**撤销登录态（桌面：请求层 401 的落点）。
   *
   * 与 `logout()` 的三点区别，都是有意的：
   *  - **不发** `/api/auth/logout`：401 已经说明这次会话不被接受，补发只会再收
   *    一个 401，还会把"用户点击的退出"和"被动失效"混成同一件事；
   *  - 同样作废在途的旧认证读取（`_authIntentSeq` 前进），迟到回包不得把旧账号
   *    装回新界面；
   *  - 同样切日志命名空间并作废启动快照，让路由守卫按新结论把人带回登录页。
   */
  function logoutLocally(): void {
    _authIntentSeq += 1
    setLoggedIn(null)
    operationEvents.handleContextSwitch()
    boot.invalidate('auth-401')
  }

  return {
    isLoggedIn,
    userInfo,
    qrcodeKey,
    qrUrl,
    qrGeneration,
    qrStatus,
    qrMessage,
    isLoading,
    isLoggingOut,
    checkLoginStatus,
    fetchQRCode,
    pollLoginStatus,
    describeQrStatus,
    reconcileAuthStatus,
    logout,
    logoutLocally,
  }
})
