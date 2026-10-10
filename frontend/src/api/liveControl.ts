/**
 * liveControl.ts - 控制请求的**唯一**接缝（阶段 4C）。
 *
 * 职责只有三件，别的一概不做：
 *   1. **一次用户操作 = 一个令牌**：令牌在"用户明确操作成立"那一刻生成并随
 *      冻结载荷一起交给传输层；同一次操作的传输重试复用同一个令牌、同一份 body、
 *      同一份账号代次。绝不在重试里重新冻结（那是把新状态拼进旧确认）。
 *   2. **回执不是状态**：这里只解释 `accepted/pending/ignored/rejected/
 *      token_conflict`，不做任何"已开播/已停止"的判断。调用方拿到结果后统一走
 *      `liveStore.refreshStatus()` 的唯一 GET 路径。
 *   3. **账号代次隔离**：操作建立时冻结的账号代次与当前代次不一致时，调用方
 *      **不得**把这次迟到结果写进新账号的界面（由 `isCurrent` 判定）。
 *
 * 不新建第二套 Axios：全部经 `@/api/request` 的既有实例（面板鉴权头注入、
 * 401 处理、网络退避、取消语义都在那里）。
 */
import { useRequest } from '@/api/request'
import { CONTROL_MAX_ATTEMPTS, isControlUrl, newOperationToken } from '@/api/operationToken'
import type { ControlReceipt, ReceiptStatus } from '@/types/session'

/** 控制入口 URL（与后端路由一致）。 */
export const CONTROL_URLS = {
  start: '/api/live/start',
  runNext: '/api/live/run-next',
  resume: '/api/live/resume',
  stop: '/api/live/stop',
  abandon: '/api/live/abandon',
  restartAfterAbandon: '/api/live/restart-after-abandon',
  confirmFace: '/api/live/confirm-face-verify',
} as const

/** 一次**已冻结**的用户操作：重试只复用，不重建。 */
export interface FrozenOperation {
  url: string
  body: unknown
  /** 操作身份：一次明确操作生成一次；传输重试不重新生成。 */
  token: string
  /** 建立这次操作时的账号代次（用于丢弃跨账号的迟到结果）。 */
  accountGeneration: number
  /** 人类可读的操作名（日志与提示用）。 */
  label: string
}

export type ControlOutcome =
  | { kind: 'receipt'; receipt: ControlReceipt }
  | { kind: 'unknown'; message: string }
  | { kind: 'cancelled' }

/**
 * 冻结一次用户操作。**必须在弹窗/确认之前或确认成立的那一刻调用一次**，
 * 之后的任何重试、任何列表刷新都不得重新调用它来"补票"。
 */
export function freezeOperation(
  url: string,
  body: unknown,
  accountGeneration: number,
  label = '',
  token: string = newOperationToken(),
): FrozenOperation {
  return { url, body, token, accountGeneration, label }
}

function isReceipt(value: unknown): value is ControlReceipt {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.status === 'string'
    && typeof candidate.accepted === 'boolean'
}

function isCancellation(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const candidate = error as { code?: string; name?: string; config?: { signal?: { aborted?: boolean } } }
  if (candidate.code === 'ERR_CANCELED') return true
  if (candidate.name === 'CanceledError') return true
  return Boolean(candidate.config?.signal?.aborted)
}

/**
 * 发送一次**已冻结**的控制操作。
 *
 * - 2xx → 回执（`accepted` / `pending` 也在 2xx：`pending` 是 202）。
 * - 409 → **也是回执**（`rejected` / `token_conflict`），业务拒绝不是传输失败，
 *   调用方据 `reason` 如实说明，绝不自动换令牌重发。
 * - 传输层在预算内失败 → `unknown`：结果待确认，调用方显示"结果待确认"并读状态；
 *   后端自己会继续负责平台重试。
 * - 用户取消 → `cancelled`：不提示、不读状态。
 */
export async function sendControl(
  frozen: FrozenOperation,
  options: { signal?: AbortSignal } = {},
): Promise<ControlOutcome> {
  if (!isControlUrl(frozen.url)) {
    // 控制接缝只接受控制 URL：把普通写请求混进来会悄悄改变它的重试语义。
    throw new Error(`liveControl 只处理控制入口：${frozen.url}`)
  }
  const request = useRequest()
  try {
    const receipt = await request.post<ControlReceipt>(frozen.url, frozen.body, {
      signal: options.signal,
      token: frozen.token,
      maxAttempts: CONTROL_MAX_ATTEMPTS,
    })
    if (isReceipt(receipt)) return { kind: 'receipt', receipt }
    return { kind: 'unknown', message: '控制回执形状无法识别，结果待确认' }
  } catch (error) {
    const response = (error as { response?: { data?: unknown } } | undefined)?.response
    if (isReceipt(response?.data)) {
      return { kind: 'receipt', receipt: response!.data as ControlReceipt }
    }
    if (isCancellation(error)) return { kind: 'cancelled' }
    return {
      kind: 'unknown',
      message: error instanceof Error && error.message
        ? `控制请求结果待确认：${error.message}`
        : '控制请求结果待确认，请稍候查看状态',
    }
  }
}

/**
 * 回执 → 用户可读的一句话。
 *
 * 文案严格区分"已受理"与"已成功"：`accepted` 只说明意图落了盘，平台事实由投影表达。
 */
export function describeReceipt(receipt: ControlReceipt, label = ''): {
  level: 'success' | 'warning' | 'error' | 'info'
  message: string
} {
  const prefix = label ? `${label}：` : ''
  const reason = receipt.reason || ''
  switch (receipt.status as ReceiptStatus) {
    case 'accepted':
      return { level: 'success', message: `${prefix}请求已受理，后台按该目标处理` }
    case 'pending':
      return { level: 'info', message: `${prefix}同一操作仍在处理中，结果待确认` }
    case 'ignored':
      return { level: 'warning', message: `${prefix}该输入与当前事实无关，未改变任何状态`
        + (reason ? `（${reason}）` : '') }
    case 'token_conflict':
      return { level: 'error', message: `${prefix}同一操作令牌已用于不同请求，请重新操作` }
    case 'rejected':
    default:
      return { level: 'error', message: `${prefix}未被受理${reason ? `：${reason}` : ''}` }
  }
}
