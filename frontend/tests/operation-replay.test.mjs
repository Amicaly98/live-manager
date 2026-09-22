/**
 * operation-replay.test.mjs - 前端重放行为的**可执行**验证
 *
 * 用真实的 axios + 本地 HTTP 服务驱动真实的 `operationToken.ts`，验证：
 *   1. 一次新操作会带上票据；
 *   2. 传输层失败后的重试**复用同一个票据**（不会新签）；
 *   3. 停止之后，用同一票据重放会被服务端 409 拒绝，客户端把它归类为
 *      `stale_operation`（因此不会用新票据偷偷重发）；
 *   4. 用户重新发起的新操作会拿到**新票据**并成功。
 *
 * 运行：node --experimental-strip-types tests/operation-replay.test.mjs
 * 不需要浏览器，也不会访问外部网络。
 */
import http from 'node:http'
import assert from 'node:assert/strict'
import axios from 'axios'

import {
  ensureOperationToken,
  retryPlan,
  classifyControlResponse,
  OPERATION_TOKEN_HEADER,
  STALE_OPERATION_MARKER,
} from '../src/api/operationToken.ts'

const results = []
function check(name, fn) {
  try {
    fn()
    results.push({ name, ok: true })
  } catch (error) {
    results.push({ name, ok: false, error: String(error && error.message) })
  }
}

// 服务端替身：模拟“响应丢失 + 停止后拒绝重放” ----------------

const seenTokens = []
let stopped = false
let firstRequestDestroyed = false
/** 第一次“开播”请求把连接destroy掉，模拟响应丢失。 */
const server = http.createServer((req, res) => {
  const token = req.headers[OPERATION_TOKEN_HEADER.toLowerCase()]
  seenTokens.push({ url: req.url, token: token ?? null })

  if (req.url === '/api/live/stop') {
    stopped = true
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ success: true }))
    return
  }

  if (req.url !== '/api/live/start') {
    res.writeHead(404).end()
    return
  }

  // 同一次操作的票据：若期间已经停止过，服务端拒绝重放。
  const replayOfCancelledOperation = stopped && token && seenTokens.filter(
    item => item.token === token).length > 1
  if (replayOfCancelledOperation) {
    res.writeHead(409, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({
      detail: `该操作的响应已丢失，且此后直播已被停止，${STALE_OPERATION_MARKER}；如需重新开播请发起新的操作`,
    }))
    return
  }

  if (!firstRequestDestroyed) {
    firstRequestDestroyed = true
    // 服务端已接受，但响应在回到客户端前丢失。
    res.socket.destroy()
    return
  }

  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ success: true }))
})

await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const baseURL = `http://127.0.0.1:${server.address().port}`

// ---------------- 客户端：与 request.ts 拦截器同构的薄适配 ----------------

// 关闭 keep-alive：否则被 destroy 的连接会留在连接池里，
// 重试一直拿到坏连接（真实浏览器/axios 在服务端 reset 后同样会换连接）。
const client = axios.create({
  baseURL,
  timeout: 5000,
  httpAgent: new http.Agent({ keepAlive: false }),
})

/** 与 request.ts 的请求拦截器一致的票据写入（axios 总是给 config.headers）。 */
function attachToken(config) {
  if (!config.headers) config.headers = {}
  ensureOperationToken(config.headers)
  return config
}

/** 测试用的重试上限：真实拦截器是无限退避重试，这里必须有界否则无法收敛。 */
const MAX_TEST_ATTEMPTS = 3

/**
 * 与 request.ts 的响应拦截器一致的传输失败重试：
 * 复用同一份 config → 复用同一票据。
 */
async function requestWithReplay(config) {
  attachToken(config)
  try {
    const response = await client.request(config)
    return {
      status: response.status,
      data: response.data,
      token: config.headers[OPERATION_TOKEN_HEADER],
      attempts: config._retryCount || 0,
    }
  } catch (error) {
    if (error.response) {
      return {
        status: error.response.status,
        data: error.response.data,
        token: config.headers[OPERATION_TOKEN_HEADER],
        disposition: classifyControlResponse(error.response.status, error.response.data?.detail),
        attempts: config._retryCount || 0,
      }
    }
    const plan = retryPlan(config._retryCount || 0)
    if (plan.attempt > MAX_TEST_ATTEMPTS) {
      throw error
    }
    // 传输层失败：退避后重放，票据不变（真实拦截器同样复用同一份 config）。
    config._retryCount = plan.attempt
    await new Promise(resolve => setTimeout(resolve, Math.min(plan.delayMs, 50)))
    const retried = await requestWithReplay(config)
    return { ...retried, retried: true }
  }
}

// ---------------- 用例 ----------------

const first = await requestWithReplay({ url: '/api/live/start', method: 'post', data: {} })

check('新操作带上票据', () => {
  assert.ok(first.token, '首个请求必须带 X-Operation-Token')
})

check('响应丢失后重试复用同一票据', () => {
  const tokensForStart = seenTokens.filter(item => item.url === '/api/live/start')
  assert.equal(tokensForStart.length, 2, '应有首次请求 + 一次重放')
  assert.equal(tokensForStart[0].token, tokensForStart[1].token,
    '重放必须复用同一票据，否则服务端无法识别这是同一次操作')
  assert.equal(first.retried, true)
})

check('重放成功后拿到成功响应', () => {
  assert.equal(first.status, 200)
  assert.equal(first.data.success, true)
})

// 用户在开播过程中的“停止”，随后旧开播请求的响应才姗姗来迟（重放）。
const stopResponse = await client.post('/api/live/stop', null, { headers: {} })
check('停止成功', () => assert.equal(stopResponse.status, 200))

const staleConfig = {
  url: '/api/live/start',
  method: 'post',
  data: {},
  headers: { [OPERATION_TOKEN_HEADER]: first.token },
}
const stale = await requestWithReplay(staleConfig)

check('停止后重放旧票据被服务端 409 拒绝', () => {
  assert.equal(stale.status, 409)
})

check('客户端把 409 归类为 stale_operation（不会用新票据重发）', () => {
  assert.equal(stale.disposition, 'stale_operation')
})

check('过期重放没有改变票据', () => {
  assert.equal(stale.token, first.token)
})

// 用户重新发起的新操作：新 config → 新票据 → 成功。
const retryConfig = { url: '/api/live/start', method: 'post', data: {} }
const fresh = await requestWithReplay(retryConfig)

check('新操作拿到新票据并成功', () => {
  assert.ok(fresh.token)
  assert.notEqual(fresh.token, first.token, '新操作不得复用旧票据')
  assert.equal(fresh.status, 200)
  assert.equal(fresh.data.success, true)
})

server.close()

// ---------------- 输出 ----------------

let failed = 0
for (const item of results) {
  if (item.ok) {
    console.log(`  ok  ${item.name}`)
  } else {
    failed += 1
    console.log(`  FAIL ${item.name}\n       ${item.error}`)
  }
}
console.log(`\n${results.length - failed}/${results.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
