/**
 * operationToken.ts - 控制操作的重放令牌（与框架无关的纯逻辑）。
 *
 * 背景：控制类请求（开播 / 停止 / 恢复 / 放弃 / 放弃并新开 / 人工确认）在传输层
 * 失败后会被重试。如果每次重试都新生成一个令牌，后端就无法区分"响应丢失后的
 * 重放"和"用户新发起的一次操作"。
 *
 * **阶段 2 的桌面协议**：服务端票据签发入口
 * `GET /api/live/operation-ticket` 已随旧控制器一起退役（稳定 410）。令牌改由
 * **客户端生成**，后端只认请求头 `X-Operation-Token`：
 *
 * - 一次**新的**操作 → 新令牌；
 * - 同一次操作的**传输层重试** → 复用同一个令牌（`ensureOperationToken`
 *   发现 config 上已有令牌就不再新签）；
 * - 后端对同令牌不同内容返回 409 `token_conflict`，对已发布的拒绝回原回执。
 *
 * 桌面边界：只识别本机后端的直播控制入口。多实例操作台
 * （`/api/instances/<id>/control`、`/api/instances/<id>/tasks`）**不**在桌面产品
 * 范围里，识别它等于给一个不存在的后端入口开重试语义，因此不纳入。
 *
 * 该模块不依赖 axios / element-plus，便于在 Node 中直接执行真实的回归测试。
 */

export const OPERATION_TOKEN_HEADER = 'X-Operation-Token'

/**
 * 后端控制入口的 URL 识别（与后端 `X-Operation-Token` 消费入口一致）。
 *
 * 只列 `backend/app/api/live.py` 里真实存在的控制端点：
 * start / stop / resume / run-next / abandon / restart-after-abandon /
 * confirm-face-verify。已退役的 `switch-area` 与取票端点**不**在这里：它们返回
 * 410，不需要（也不应）获得"同令牌有界重试"的语义。
 */
const CONTROL_URL_PATTERN =
  /\/api\/live\/(start|stop|resume|run-next|abandon|restart-after-abandon|confirm-face-verify)(\?|$)/

export function isControlUrl(url?: string): boolean {
  if (!url) return false
  return CONTROL_URL_PATTERN.test(url)
}

/** 生成一个新的操作令牌（优先 UUID，退化到时间戳 + 随机数）。 */
export function newOperationToken(
  random: () => number = Math.random,
  now: () => number = Date.now
): string {
  const cryptoObj = (globalThis as unknown as { crypto?: Crypto }).crypto
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  return `op-${now()}-${random().toString(16).slice(2)}`
}

export type HeaderCarrier = {
  get?: (name: string) => unknown
  set?: (name: string, value: string) => unknown
  [key: string]: unknown
}

function readHeader(headers: HeaderCarrier | undefined, name: string): unknown {
  if (!headers) return undefined
  if (typeof headers.get === 'function') {
    const value = headers.get(name)
    if (value !== undefined && value !== null) return value
  }
  return headers[name]
}

function writeHeader(headers: HeaderCarrier | undefined, name: string, value: string): void {
  if (!headers) return
  if (typeof headers.set === 'function') {
    headers.set(name, value)
    return
  }
  headers[name] = value
}

/**
 * 保证本次请求带有一个令牌。
 *
 * - config 上已有令牌（同一份 config 被重试复用）→ 原样返回，**不新签**；
 * - 没有令牌（一次新操作）→ 新签一个并写回 headers。
 *
 * 返回本次实际使用的令牌，便于测试与日志。
 */
export function ensureOperationToken(
  headers: HeaderCarrier | undefined,
  create: () => string = newOperationToken
): string {
  const existing = readHeader(headers, OPERATION_TOKEN_HEADER)
  if (typeof existing === 'string' && existing.length > 0) {
    return existing
  }
  const token = create()
  writeHeader(headers, OPERATION_TOKEN_HEADER, token)
  return token
}

/** 传输层失败后的重试计划：只推迟时间，绝不更换令牌。 */
export function retryPlan(
  previousRetries: number,
  baseDelayMs = 1000,
  maxDelayMs = 30000
): { attempt: number; delayMs: number } {
  const attempt = previousRetries + 1
  return { attempt, delayMs: Math.min(baseDelayMs * 2 ** attempt, maxDelayMs) }
}

/** 后端令牌长度上限（`ControlService._MAX_TOKEN_LENGTH`）。 */
export const OPERATION_TOKEN_MAX_LENGTH = 160

/** 控制请求的**总尝试预算**（含首次）：有界，不为"无限重放"开口子。 */
export const CONTROL_MAX_ATTEMPTS = 3
