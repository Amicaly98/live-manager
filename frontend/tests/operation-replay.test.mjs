/**
 * operation-replay.test.mjs - 前端重放行为的**可执行**验证（新协议版）
 *
 * 用真实的 axios + 真实 `api/request.ts` 拦截器 + 真实 `api/liveControl.ts` 驱动
 * 一个本地 HTTP 服务（产品 Electron baseURL 的固定端口 8000），验证：
 *   1. **令牌由客户端生成**：一次新的冻结操作带 `X-Operation-Token`；
 *   2. 传输层失败后的重试**复用同一个令牌**（不会重新生成）；
 *   3. 传输预算用尽 → `unknown`（结果待确认），**不**换令牌重发；
 *   4. 409 是**回执**（rejected / token_conflict）：如实回到调用方，也不换令牌；
 *   5. 用户新发起的一次明确操作 → **新**令牌；
 *   6. `accepted` 只表示意图已落盘：`describeReceipt` 不把它说成"已开播"。
 *
 * 运行：node --experimental-strip-types --import ./tests/alias-register.mjs
 *       --test tests/operation-replay.test.mjs
 * 不需要浏览器，也不会访问外部网络（只连 127.0.0.1 的本地替身）。
 */
import http from 'node:http'
import assert from 'node:assert/strict'
import axios from 'axios'

// request.ts 的 Electron 分支固定 127.0.0.1:8000 —— 本地替身就听在这个端口上
//（与 frontendChain 的既有做法一致：测试即真实链路）。
globalThis.window = { electronAPI: {} }

const { OPERATION_TOKEN_HEADER, isControlUrl, retryPlan } =
  await import('../src/api/operationToken.ts')
const liveControl = await import('../src/api/liveControl.ts')
const { freezeOperation, sendControl, describeReceipt, CONTROL_URLS } = liveControl

// ---------------- 本地替身：记录令牌，模拟"响应丢失"与"业务拒绝" ----------------

const seenTokens = []
let mode = 'destroy_once'
let destroyedOnce = false
let receiptToken = null

const server = http.createServer((req, res) => {
  const chunks = []
  req.on('data', (c) => chunks.push(c))
  req.on('end', () => {
    const token = req.headers[OPERATION_TOKEN_HEADER.toLowerCase()] ?? null
    seenTokens.push({ url: req.url, token })
    if (mode === 'always_destroy') {
      req.socket.destroy()          // 每一次尝试都在回程丢失响应
      return
    }
    if (mode === 'destroy_once' && !destroyedOnce) {
      destroyedOnce = true
      req.socket.destroy()          // 服务端已收下，但响应在回程丢失
      return
    }
    if (mode === 'receipt_409') {
      res.writeHead(409, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({
        operation_token: receiptToken ?? token,
        status: 'token_conflict',
        accepted: false,
        reason: '同一操作令牌已用于不同请求',
        projection: null, success: false, message: '', detail: null,
      }))
      return
    }
    if (mode === 'garbage') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ success: true }))     // 缺回执字段 → 形状不可识别
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({
      operation_token: token ?? '',
      status: 'accepted', accepted: true, reason: '',
      projection: null, success: true, message: '控制意图已保存', detail: null,
    }))
  })
})

await new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(8000, '127.0.0.1', resolve)
})

const results = []
async function test(name, body) {
  seenTokens.length = 0
  destroyedOnce = false
  try {
    await body()
    results.push({ name, ok: true })
    console.log(`  ok  ${name}`)
  } catch (error) {
    results.push({ name, ok: false, error: String(error?.message || error) })
    console.log(`  FAIL ${name}\n       ${error?.message || error}`)
  } finally {
    mode = 'normal'
    axios.defaults.adapter = undefined
  }
}

function tokensFor(url) {
  return seenTokens.filter((item) => item.url === url).map((item) => item.token)
}

// ---------------- 用例 ----------------

await test('新操作：令牌由客户端生成并随请求携带', async () => {
  mode = 'normal'
  const frozen = freezeOperation(CONTROL_URLS.start, { mode: 'task' }, 0, '测试开播')
  assert.ok(frozen.token, '冻结时就生成令牌')
  const outcome = await sendControl(frozen)
  assert.equal(outcome.kind, 'receipt')
  assert.equal(outcome.receipt.status, 'accepted')
  assert.equal(outcome.receipt.accepted, true)
  const tokens = tokensFor('/api/live/start')
  assert.equal(tokens.length, 1)
  assert.equal(tokens[0], frozen.token, '请求携带冻结的令牌')
  assert.ok(isControlUrl('/api/live/start'), '控制入口要被识别为控制 URL')
})

await test('传输失败重试复用同一令牌，预算内收敛为已受理', async () => {
  destroyedOnce = false
  mode = 'destroy_once'
  const frozen = freezeOperation(CONTROL_URLS.resume,
                                 { run_id: 'run-1', goal_revision: 2 }, 0, '恢复')
  seenTokens.length = 0
  const outcome = await sendControl(frozen)
  assert.equal(outcome.kind, 'receipt', '重试成功后应拿到回执')
  assert.equal(outcome.receipt.status, 'accepted')
  const tokens = tokensFor('/api/live/resume')
  assert.ok(tokens.length >= 2, `应有首次请求 + 重放（实际 ${tokens.length} 次）`)
  assert.deepEqual([...new Set(tokens)], [frozen.token],
    '重放必须复用同一令牌，否则服务端无法识别这是同一次操作')
})

await test('传输预算用尽：unknown（结果待确认），不换令牌重发', async () => {
  mode = 'always_destroy'                // 每次都毁连接
  const frozen = freezeOperation(CONTROL_URLS.stop, {}, 0, '停止')
  seenTokens.length = 0
  const outcome = await sendControl(frozen)
  assert.equal(outcome.kind, 'unknown', '结果待确认，不是失败也不是成功')
  const tokens = tokensFor('/api/live/stop')
  assert.ok(tokens.length >= 1)
  assert.deepEqual([...new Set(tokens)], [frozen.token],
    '预算内每一次尝试都用同一个令牌')
  assert.ok(tokens.length <= 3, `不得超过控制预算（实际 ${tokens.length} 次）`)
  // 传输退避的真实节奏（请求拦截器不换令牌，这里核对计划本身有界）。
  const plan = retryPlan(0)
  assert.ok(plan.delayMs > 0)
})

await test('409 是回执不是传输失败：如实上抛，不换令牌重发', async () => {
  mode = 'receipt_409'
  const frozen = freezeOperation(CONTROL_URLS.start, { mode: 'task' }, 0, '开播')
  receiptToken = frozen.token
  seenTokens.length = 0
  const outcome = await sendControl(frozen)
  assert.equal(outcome.kind, 'receipt', '业务拒绝是回执')
  assert.equal(outcome.receipt.status, 'token_conflict')
  assert.equal(outcome.receipt.accepted, false)
  assert.deepEqual(tokensFor('/api/live/start'), [frozen.token],
    '409 之后不得用新令牌重发（那等于复活已取消的操作）')
})

await test('回执形状不可识别 → unknown（结果待确认）', async () => {
  mode = 'garbage'
  const frozen = freezeOperation(CONTROL_URLS.stop, {}, 0, '停止')
  const outcome = await sendControl(frozen)
  assert.equal(outcome.kind, 'unknown')
  assert.match(outcome.message, /待确认/)
})

await test('用户新发起的一次明确操作 → 新令牌', async () => {
  mode = 'normal'
  const first = freezeOperation(CONTROL_URLS.start, { mode: 'task' }, 0, '开播')
  await sendControl(first)
  const second = freezeOperation(CONTROL_URLS.start, { mode: 'task' }, 0, '再开播')
  assert.notEqual(second.token, first.token, '新操作不得复用旧令牌')
  await sendControl(second)
  const tokens = tokensFor('/api/live/start')
  assert.deepEqual([...new Set(tokens)], [first.token, second.token])
})

await test('accepted 只表示意图已落盘：文案不得宣称平台已开播', async () => {
  const accepted = describeReceipt({
    operation_token: 't', status: 'accepted', accepted: true, reason: '',
    projection: null, success: true, message: '', detail: null,
  }, '开播')
  assert.equal(accepted.level, 'success')
  assert.match(accepted.message, /受理/)
  assert.doesNotMatch(accepted.message, /已开播|已经开播/)

  const rejected = describeReceipt({
    operation_token: 't', status: 'rejected', accepted: false,
    reason: '目标已变化', projection: null, success: false, message: '', detail: null,
  }, '开播')
  assert.equal(rejected.level, 'error')
  assert.match(rejected.message, /目标已变化/)

  const pending = describeReceipt({
    operation_token: 't', status: 'pending', accepted: false, reason: '',
    projection: null, success: false, message: '', detail: null,
  }, '开播')
  assert.equal(pending.level, 'info')
  assert.match(pending.message, /待确认/)
})

server.closeAllConnections?.()
await new Promise((resolve) => {
  const timer = setTimeout(resolve, 250)
  server.close(() => { clearTimeout(timer); resolve() })
})

let failed = 0
for (const item of results) {
  if (!item.ok) failed += 1
}
console.log(`\n${results.length - failed}/${results.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
