/**
 * 有效时长的**有界外推**：真实 Pinia store + 真实请求链的行为回归（2026-09-20）。
 *
 * 计时模型：显示值 = 服务端**已确认**有效时长 + **有界**外推的待确认区间。
 * 这里覆盖 IMPLEMENTATION-PLAN §6 的第 12 项（浏览器侧）与第 13 项（文案）：
 *
 *   1) pending_extrapolatable=false → 不做任何外推，显示值等于已确认值；
 *   2) 允许外推时，显示值随本地时间增长，但到 pending_valid_seconds 就**冻结**
 *      （浏览器失联多久都不会继续涨）；
 *   3) 新鲜快照校正：已确认值前进时向前校正，绝不倒退；
 *   4) 待确认段被服务端撤回时，显示可以小幅回退，但**不得低于已确认值**；
 *   5) 同一会话的旧快照（session_version 更低）与迟到响应不得把进度拉回去；
 *   6) 短状态文案只使用"连接中/恢复中/已暂停"，不出现任何内部术语。
 *
 * 只替换传输边界（真实 TS store 转译 + 真实 Vue/Pinia），不访问网络。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
//: 负向对照会把 src 复制到临时目录并注入"旧行为"补丁，再用 OUTER_TEST_SRC
//: 指回那份副本；正常跑法不设该变量，用的就是仓库里的源码。
const root = process.env.OUTER_TEST_SRC || path.resolve(here, '..')
const repoRoot = path.resolve(here, '..')
const req = createRequire(path.join(repoRoot, 'package.json'))
const ts = req('typescript')
const { createPinia, setActivePinia } = req('pinia')

if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
}

let opEventsCache = null
function loadStore(file, transport) {
  const source = fs.readFileSync(path.join(root, 'src/stores', file), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
  }}).outputText
  const module = { exports: {} }
  const customRequire = (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/stores/operationEvents') {
      // 2026-09-21（A）：live store 新增 operationEvents 依赖，装载器补上解析。
      opEventsCache ||= loadStore('operationEvents.ts', transport)
      return opEventsCache
    }
    if (name === '@/composables/useCache') {
      const store = new Map()
      return { default: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) } }
    }
    return req(name)
  }
  const fn = vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                                 { filename: file })
  fn(customRequire, module, module.exports)
  return module.exports
}

function makeTransport(routes = {}) {
  return {
    get: (url, params, options) => (routes.get
      ? routes.get(url, options) : Promise.resolve({})),
    post: (url) => (routes.post ? routes.post(url) : Promise.resolve({ success: true })),
    postForm: () => Promise.resolve({ success: true }),
    getBlob: () => Promise.resolve(new Blob()),
  }
}

function snap({
  run = 'run-1', version = 1, elapsed = 0, duration = 3600, known = true,
  streaming = true, starting = false, boot = 'boot-A', zone = '学习区',
  pending = 0, pendingValid = 0, extrapolatable = false, timerState = 'running',
  phase = 'live', recoveryBlocked = '', extra = {},
} = {}) {
  return {
    is_streaming: streaming, is_starting: starting, is_cancelling: false,
    recovery_blocked: recoveryBlocked, current_zone: zone, run_id: run,
    session_version: version, boot_id: boot, duration_known: known,
    duration_seconds: duration, elapsed_seconds: elapsed,
    remaining_seconds: null, backend_events: [], phase, timer_state: timerState,
    pending_seconds: pending, pending_valid_seconds: pendingValid,
    pending_limit_seconds: 90, pending_extrapolatable: extrapolatable,
    ...extra,
  }
}

// ---- 可控时钟：替换**单调耗时源**（store 的插值与窗口折算走的正是它） ----
// 迁移说明（2026-09-20 监督 E3）：夹具原来替换的是 `Date.now`，因为生产代码当时用
// 日历时间推导外推消耗。生产改用 `performance.now()`（单调）之后，只改 `Date.now`
// 已经推不动 store 的时钟——不是产品回归，而是注入点失效。这里把注入点换到同一个
// 单调源；下面的业务断言一行未动。
// 起点故意取 **0**：单调时钟从 0 起算是合法值，顺带守住"别把 0 当成未初始化"
// （生产代码里 `_windowArmed`/`_displayArmed` 就是为此改成布尔量的）。
const realPerformance = Object.getOwnPropertyDescriptor(globalThis, 'performance')
let monoMs = 0
Object.defineProperty(globalThis, 'performance', {
  configurable: true,
  value: { now: () => monoMs },
})
function tick(seconds) { monoMs += Math.round(seconds * 1000) }

const INTERNAL_TERMS = ['confirmed', 'pending', '待确认', '丢弃', '扣时',
                        '补偿', '累计扣除', '已确认区间']

const results = []
async function test(name, body) {
  try {
    await body()
    results.push({ name, status: 'PASS' })
    console.log(`PASS  ${name}`)
  } catch (error) {
    results.push({ name, status: 'FAIL', message: error.message })
    console.log(`FAIL  ${name} — ${error.message}`)
  }
}

function freshStore(transport) {
  setActivePinia(createPinia())
  const { useLiveStore } = loadStore('live.ts', transport)
  return useLiveStore()
}

// ---------------------------------------------- 1) 窗口用尽后不再增长
await test('窗口用尽：显示值停在"已确认 + 可推算上限"，不再随时间增长', async () => {
  // 服务端给的 pending_seconds 已经是**可推算上限**（不会超过 pending_limit）
  const status = snap({ elapsed: 600, pending: 45, pendingValid: 0,
                        extrapolatable: false, timerState: 'paused_unknown' })
  const store = freshStore(makeTransport({ get: () => Promise.resolve(status) }))
  store.applyStatus(status)
  assert.equal(store.localElapsed, 645, '显示已确认 + 服务端给出的推算上限')
  tick(30)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 645, '窗口用尽后不得继续增长')
  store.stopLocalTick()
})

// ---------------------------------------------- 1b) 服务端撤回待确认区间
await test('服务端不给待确认区间时，显示值就是已确认值', async () => {
  const status = snap({ elapsed: 600, pending: 0, pendingValid: 0,
                        extrapolatable: false })
  const store = freshStore(makeTransport({ get: () => Promise.resolve(status) }))
  store.applyStatus(status)
  assert.equal(store.localElapsed, 600)
  tick(30)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 600)
  store.stopLocalTick()
})

// ---------------------------------------------- 2) 有界外推：涨到窗口就冻结
await test('有界外推：增长到 pending_valid_seconds 后冻结', async () => {
  const status = snap({ elapsed: 600, pending: 10, pendingValid: 30,
                        extrapolatable: true })
  const store = freshStore(makeTransport({ get: () => Promise.resolve(status) }))
  store.applyStatus(status)
  assert.equal(store.localElapsed, 610, '起始值 = 已确认 + 当时待确认')
  tick(15)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 625, '窗口内应继续外推')
  tick(600)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 640,
               '窗口用尽后必须冻结（浏览器失联多久都不再涨）')
  store.stopLocalTick()
})

// ---------------------------------------------- 3) 新鲜快照向前校正
await test('新鲜快照：已确认值前进时向前校正，不倒退', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ elapsed: 600, pending: 10, pendingValid: 30,
                           extrapolatable: true }))
  tick(20)
  store.refreshFromAnchor()
  const before = store.localElapsed
  store.applyStatus(snap({ elapsed: 900, pending: 5, pendingValid: 30,
                           extrapolatable: true, version: 2 }))
  assert.equal(store.localElapsed, 905)
  assert.ok(store.localElapsed >= before, '校正后不得低于校正前')
  store.stopLocalTick()
})

// ---------------------------------------------- 4) 待确认段撤回：小幅回退但不归零
await test('待确认段被撤回：可以小幅回退，但不得低于已确认值、不得归零', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ elapsed: 600, pending: 20, pendingValid: 30,
                           extrapolatable: true }))
  tick(25)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 645)
  // 查询失败：服务端撤回待确认区间
  store.applyStatus(snap({ elapsed: 600, pending: 0, pendingValid: 0,
                           extrapolatable: false, timerState: 'paused_unknown',
                           version: 2 }))
  assert.equal(store.localElapsed, 600, '回退到已确认值')
  assert.ok(store.localElapsed > 0, '已确认进度不得归零')
  store.stopLocalTick()
})

// ---------------------------------------------- 5) 旧快照/迟到响应不得拉回进度
await test('旧快照（更低 session_version）不覆盖新进度', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ elapsed: 900, version: 5, pending: 0,
                           extrapolatable: false }))
  assert.equal(store.localElapsed, 900)
  store.applyStatus(snap({ elapsed: 100, version: 4, pending: 0,
                           extrapolatable: false }))
  assert.equal(store.localElapsed, 900, '旧版本快照必须被丢弃')
  assert.equal(store.sessionId, 'run-1')
  store.stopLocalTick()
})

await test('同会话内已确认值只增不减（防止旧响应把进度拉回去）', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ elapsed: 900, version: 3, pending: 0,
                           extrapolatable: false }))
  // 同一 run、更高版本，但已确认值变小（不应发生）：页面不得因此回退
  store.applyStatus(snap({ elapsed: 200, version: 4, pending: 0,
                           extrapolatable: false }))
  assert.equal(store.localElapsed, 900, '已确认进度不得被拉回')
  store.stopLocalTick()
})

// ---------------------------------------------- 6) 真正的停止才清显示
await test('服务端确实停止时才清空显示', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ elapsed: 900, pending: 0, extrapolatable: false }))
  store.applyStatus(snap({ streaming: false, elapsed: 0, duration: 0,
                           known: false, version: 4 }))
  assert.equal(store.localElapsed, 0)
  assert.equal(store.sessionId, '')
  store.stopLocalTick()
})

// ---------------------------------------------- 7) 短状态文案
await test('短状态文案：只给"连接中/恢复中/已暂停"，不含内部术语', async () => {
  const store = freshStore(makeTransport({ get: () => Promise.resolve(snap()) }))
  store.applyStatus(snap({ timerState: 'paused_closed' }))
  assert.equal(store.timerHint, '已暂停')
  store.applyStatus(snap({ timerState: 'paused_unknown', version: 2 }))
  assert.equal(store.timerHint, '连接中')
  store.applyStatus(snap({ recoveryBlocked: '需要人脸验证', version: 3 }))
  assert.equal(store.timerHint, '恢复中')
  store.applyStatus(snap({ phase: 'recovering', version: 4 }))
  assert.equal(store.timerHint, '恢复中')
  store.applyStatus(snap({ version: 5 }))
  assert.equal(store.timerHint, '', '正常直播不显示异常文案')
  for (const term of INTERNAL_TERMS) {
    assert.ok(!store.timerHint.includes(term),
              `文案不得出现内部术语：${term}`)
  }
  store.stopLocalTick()
})

await test('浏览器失联：提示"连接中"，显示值冻结在服务端给的窗口上且不归零', async () => {
  let online = true
  const status = snap({ elapsed: 600, pending: 10, pendingValid: 20,
                        extrapolatable: true })
  const store = freshStore(makeTransport({
    get: () => (online ? Promise.resolve(status) : Promise.reject(new Error('offline'))),
  }))
  await store.pollStatus()
  assert.equal(store.localElapsed, 610)
  online = false
  await store.pollStatus()
  assert.equal(store.timerHint, '连接中')
  assert.equal(store.status.is_streaming, true, '失联不得解释成已停播')
  // 失联后仍然只能走到"服务端给的外推窗口"为止：610 + 20
  tick(600)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 630, '失联后冻结在服务端给的窗口上')
  tick(600)
  store.refreshFromAnchor()
  assert.equal(store.localElapsed, 630, '再久也不得继续外推')
  assert.ok(store.localElapsed >= 600, '不得归零')
  store.stopLocalTick()
})

if (realPerformance) {
  Object.defineProperty(globalThis, 'performance', realPerformance)
} else {
  delete globalThis.performance
}

const failed = results.filter((r) => r.status === 'FAIL')
console.log(`\neffective-time-clamp: ${results.length - failed.length}/${results.length} passed`)
process.exit(failed.length === 0 ? 0 : 1)
