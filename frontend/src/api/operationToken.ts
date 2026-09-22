/**
 * operationToken.ts - 控制操作的重放票据（与框架无关的纯逻辑）
 *
 * 移植自服务器版（bilibili-live-server 26c170c），按桌面语义裁剪：
 * 控制票据只服务于本机后端的直播控制接口。
 *
 * 背景：控制类请求（开播 / 执行任务 / 恢复 / 停止 / 确认验证）在传输层
 * 失败后会被重试。如果每次重试都新签一个票据，后端就无法区分"响应丢失
 * 后的重放"和"用户新发起的一次操作"，停止之后被取消的旧操作可能复活。
 *
 * - 一次**新的**操作 → 向后端取一张新的签发票据（`<boot>:<代际>:<序号>`）；
 * - 同一次操作的**传输层重试** → 复用同一票据（config 上已有票据就不新签）；
 * - 后端对过期重放返回 409（STALE_OPERATION_STATUS），前端明确报错，
 *   绝不用新票据重发（等于复活已取消的操作）。
 */

export const OPERATION_TOKEN_HEADER = 'X-Operation-Token'

/** 后端对"停止之后重放的旧操作"返回的状态码。 */
export const STALE_OPERATION_STATUS = 409

/** 后端 409 文案里的稳定标识，用于把"过期重放"与"其他冲突"区分开。 */
export const STALE_OPERATION_MARKER = '响应已丢失'

/** 控制类操作的 URL 识别（与后端 X-Operation-Token 消费入口一致）。 */
const CONTROL_URL_PATTERN =
  /\/api\/live\/(start|stop|resume|run-next|confirm-face-verify|switch-area)(\?|$)/

export function isControlUrl(url?: string): boolean {
  if (!url) return false
  return CONTROL_URL_PATTERN.test(url)
}

/** 后端票据端点（GET /api/live/operation-ticket）。 */
export const SERVER_TICKET_URL = '/api/live/operation-ticket'

/** 取票结果（后端返回 `{ticket, epoch}`）。 */
export type TicketResponse = { ticket?: string; epoch?: number }

/** 结构化中止信号（与 axios 的 GenericAbortSignal/DOM AbortSignal 都兼容）。 */
export type AbortableSignal = { aborted: boolean }

/** 传输层失败后的重试计划：只推迟时间，绝不更换票据。 */
export function retryPlan(
  previousRetries: number,
  baseDelayMs = 1000,
  maxDelayMs = 30000
): { attempt: number; delayMs: number } {
  const attempt = previousRetries + 1
  return { attempt, delayMs: Math.min(baseDelayMs * 2 ** attempt, maxDelayMs) }
}

/**
 * 把后端的控制响应分类。
 *
 * `stale_operation`：停止已生效，旧操作被拒绝——必须**上报失败**，
 * 不能用新票据重新发起，否则等于复活已取消的操作。
 */
export function classifyControlResponse(
  status: number,
  detail?: string
): 'stale_operation' | 'conflict' | 'ok' | 'error' {
  if (status === STALE_OPERATION_STATUS) {
    if (detail && detail.includes(STALE_OPERATION_MARKER)) return 'stale_operation'
    return 'conflict'
  }
  if (status >= 200 && status < 300) return 'ok'
  return 'error'
}

/**
 * 对取票失败的分类（调用方据此决定安全行为）。
 *
 * - `legacy_backend`：404 —— 旧后端没有签发端点。唯一允许回退客户端
 *   UUID 的情况（明确的旧 API 兼容边界）；
 * - `transient`：网络失败 / 5xx —— 可恢复，继续重试取票，不降级。
 */
export function classifyTicketFailure(
  status: number | undefined
): 'legacy_backend' | 'transient' {
  if (status === 404) return 'legacy_backend'
  return 'transient'
}

/**
 * 单次取票（无缓存、无 in-flight 去重——一项新控制意图对应一张新票据）。
 *
 * `get` 由调用方注入（生产里是**同一 axios 实例**的 get——同一 baseURL、
 * 可取消，且 operation-ticket 不属于控制 URL，不会递归进入取票逻辑）。
 * 成功返回票据；失败抛出原始错误，由调用方按 classifyTicketFailure
 * 分类后决定安全行为——这里绝不吞错、绝不降级。
 */
export function createServerTicketFetcher(
  get: (url: string, signal?: AbortableSignal) => Promise<TicketResponse | undefined>
): { fetch: (signal?: AbortableSignal) => Promise<string> } {
  return {
    async fetch(signal?: AbortableSignal): Promise<string> {
      const data = await get(SERVER_TICKET_URL, signal)
      const ticket = data?.ticket
      if (typeof ticket !== 'string' || ticket.length === 0) {
        throw new Error('取票端点返回缺少 ticket 字段')
      }
      return ticket
    },
  }
}

/** 兜底客户端票据（仅旧后端 404 时使用）。 */
export function newOperationToken(): string {
  const cryptoObj = (globalThis as unknown as { crypto?: Crypto }).crypto
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  return `op-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

type HeaderCarrier = { [key: string]: unknown }

/**
 * 保证本次请求带有一个票据。
 *
 * - headers 上已有票据（同一份 config 被重试复用）→ 原样返回，**不新签**；
 * - 没有票据（一次新操作）→ 用 create() 新签一个并写回 headers。
 */
export function ensureOperationToken(
  headers: HeaderCarrier | undefined,
  create: () => string = newOperationToken
): string {
  const existing = headers?.[OPERATION_TOKEN_HEADER]
  if (typeof existing === 'string' && existing.length > 0) {
    return existing
  }
  const token = create()
  if (headers) headers[OPERATION_TOKEN_HEADER] = token
  return token
}
