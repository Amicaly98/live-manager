/**
 * authWire.ts - B 站认证回包的**唯一**归一化点（阶段 4C 返修 R1）。
 *
 * 背景：后台有**两种**认证回包形状，且都是真实存在的：
 *
 * - `GET /api/auth/status`：嵌套 —— `{logged_in, source, user_info:{uid,uname,room_id}}`；
 * - `POST /api/auth/poll/{key}` 安装成功：**平铺** ——
 *   `{logged_in:true, status:'installed', uid, room_id, uname, operation_token, projection}`
 *   （见 `backend/app/api/auth.py::_install_candidate` 的返回；4B 正式 runner
 *   捕获的 `actual-login-response.json` 原样如此）。**没有** `user_info`。
 *
 * 前端以前直接读 `res.user_info`，于是真实安装回包虽然 `logged_in=true`，
 * store 里却是 `isLoggedIn=false / userInfo=null`，`QRCodeLogin` 的成功分支
 * 永远不成立、`Login` 页也就永远不导航（复核 R1）。
 *
 * 修法**只有一处**：两种形状都从这里转成同一份 `NormalizedLogin`。读方
 * （store / 组件 / 路由守卫）不再各自解释线格式，也不再依赖"回包里恰好有
 * `user_info`"。**不**为旧 mock 改后端安装接口，也不拿当前账号去填旧 poll。
 */
import type { UserInfo } from '@/types/api'

/** `GET /api/auth/status` 的线格式（只读结论，嵌套 user_info）。 */
export interface AuthStatusWire {
  logged_in?: boolean
  user_info?: UserInfo | null
  source?: string
}

/**
 * `POST /api/auth/poll/{key}` 的线格式。
 *
 * 安装成功是平铺字段；等待/过期/被拒/换代只有 `status`/`message`。
 * 两种都收在这里，**不**在调用点分支。
 */
export interface PollLoginWire {
  logged_in?: boolean
  status?: string
  message?: string
  uid?: number
  room_id?: number
  uname?: string
  face?: string
  operation_token?: string
  projection?: unknown
  user_info?: UserInfo | null
}

/** 归一化之后的唯一形状：store、组件、路由守卫都只认它。 */
export interface NormalizedLogin {
  logged_in: boolean
  status: string
  message: string
  /** `logged_in === true` 时**一定有**可用身份；否则为 null。 */
  user_info: UserInfo | null
}

/** 只有**正整数** uid 才算身份：缺 uid / uid<=0 的回包不建立登录态。 */
function _positiveInt(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

function _text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function _userInfo(uid: number, uname: unknown, roomId: unknown,
                   face: unknown): UserInfo {
  const info: UserInfo = { uid, uname: _text(uname) }
  const room = _positiveInt(roomId)
  if (room > 0) info.room_id = room
  // 头像地址只在服务端**已核验**回报时带上；空值不写字段（前端按无头像显示，
  // 不伪造、也不拿旧缓存头像冒充当前账号）。
  const avatar = _text(face)
  if (avatar) info.face = avatar
  return info
}

/**
 * 把线上回包（任意一种形状）转成 `NormalizedLogin`。**纯粹的数据转换**：
 * 不发请求、不读 store、不改任何状态。
 */
export function normalizeLoginPayload(raw: unknown): NormalizedLogin {
  if (!raw || typeof raw !== 'object') {
    return { logged_in: false, status: '', message: '', user_info: null }
  }
  const wire = raw as PollLoginWire & AuthStatusWire
  const status = _text(wire.status)
  const message = _text(wire.message)
  if (!wire.logged_in) {
    return { logged_in: false, status, message, user_info: null }
  }

  // 1) 嵌套形状（GET /status、以及兼容旧替身）：以 user_info 为准。
  const nested = wire.user_info
  if (nested && typeof nested === 'object') {
    const uid = _positiveInt(nested.uid)
    if (uid > 0) {
      return {
        logged_in: true, status, message,
        user_info: _userInfo(uid, nested.uname, nested.room_id, nested.face),
      }
    }
  }

  // 2) 平铺形状（真实安装回包）：uid/room_id/uname/face 直接在顶层。
  const uid = _positiveInt(wire.uid)
  if (uid > 0) {
    return {
      logged_in: true, status, message,
      user_info: _userInfo(uid, wire.uname, wire.room_id, wire.face),
    }
  }

  // 3) 声称成功却没有可用身份：如实当作"没有建立登录态"，绝不编造 uid。
  return {
    logged_in: false,
    status: status || 'identity_missing',
    message: message || '登录回包没有可用的账号身份',
    user_info: null,
  }
}
