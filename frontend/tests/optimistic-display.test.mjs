/**
 * 受理即有界显示（2026-09-28 收口，B01/B02；真实 store + 真实 request.ts + 真实 Axios）。
 *
 * 被验证的规则：
 *   1) 受理成功后显示从冻结基线起有界推算（首跳为 0 秒，封顶 +120 秒），
 *      首个权威观察平滑接管并在 45 秒内线性收敛；
 *   2) 乐观值只存在于显示层：权威记账（status.elapsed_seconds / 已确认锚点）
 *      不被外推污染——失败/停止后显示回落权威值，绝不把显示量当 confirmed；
 *   3) 受理失败不产生任何乐观显示；
 *   4) 换会话（run 变化）撤除旧乐观；
 *   5) 指定任务开播请求必须携带冻结的 task_id/expected_revision/business_date
 *      （abandon_saved 存在时一并携带）。
 *
 * 只替换传输边界（axios adapter）与本地单调时钟；store、request.ts、拦截器
 * （取票/重试/取消）全部是生产实现。不发真实网络请求。
 *
 * 运行：node --experimental-strip-types --import ./tests/alias-register.mjs
 *       tests/optimistic-display.test.mjs
 * （本文件自包含装载器，直接 node tests/optimistic-display.test.mjs 亦可。）
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = process.env.OUTER_TEST_SRC || path.resolve(here, '..')
const repoRoot = path.resolve(here, '..')
const req = createRequire(path.join(repoRoot, 'package.json'))
const ts = req('typescript')
const axios = req('axios')
const { createPinia, setActivePinia } = req('pinia')

global.localStorage = {
  getItem: () => null, setItem() {}, removeItem() {},
}
// request.ts 探测 Electron 预加载桥（浏览器/Node 下为空即可）。
global.window = global.window || { electronAPI: undefined }

/** 真实 TS 模块装载（与 effective-time 套件同一模式）。 */
function load(relative, overrides = {}) {
  const file = path.join(root, 'src', relative)
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  function local(name) {
    if (name in overrides) return overrides[name]
    if (name === 'element-plus') {
      return { ElMessage: { warning() {}, error() {}, success() {} } }
    }
    if (name.startsWith('@/stores/')) {
      return load(name.slice('@/'.length) + '.ts')
    }
    if (name.startsWith('.')) {
      return load(path.relative(path.join(root, 'src'),
                                path.resolve(path.dirname(file), name)) + '.ts')
    }
    return req(name)
  }
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
    { filename: file })(local, module, module.exports)
  return module.exports
}

// ---- 可控单调时钟（与生产 store 同一注入点：performance.now） ----
const realPerformance = Object.getOwnPropertyDescriptor(globalThis, 'performance')
let monoMs = 1_000_000
Object.defineProperty(globalThis, 'performance', {
  configurable: true,
  value: { now: () => monoMs },
})
const tick = (seconds) => { monoMs += Math.round(seconds * 1000) }
const realSetTimeout = globalThis.setTimeout
// 有界短轮询（beginStartWatch）与本地 1s tick 用真实定时器；测试收口时统一清理。
// monoMs 不随真实时间流动，定时器何时触发都不影响时钟断言。

function snap({
  elapsed = 600, pending = 0, pendingValid = 0, extrapolatable = false,
  timerState = 'running', run = 'run-B', version = 1, duration = 7200,
  starting = false, streaming = true, takeover = false,
} = {}) {
  return {
    is_streaming: streaming, is_starting: starting, run_id: run, boot_id: 'boot-A',
    session_version: version, duration_known: true, duration_seconds: duration,
    elapsed_seconds: elapsed, pending_seconds: pending,
    pending_valid_seconds: pendingValid, pending_limit_seconds: 90,
    pending_extrapolatable: extrapolatable, timer_state: timerState,
    phase: 'live', takeover_pending: takeover,
  }
}

function makeAdapter(plan) {
  let index = 0
  const seen = []
  const adapter = async (config) => {
    const step = plan[Math.min(index, plan.length - 1)]
    index += 1
    seen.push({ url: String(config.url), body: config.data })
    monoMs += step.latency || 0
    if (step.fail) {
      const error = new Error('Network Error')
      error.config = config
      error.code = 'ERR_NETWORK'
      error.isAxiosError = true
      error.request = {}
      throw error
    }
    return { status: 200, statusText: 'OK', headers: {}, config, data: step.data }
  }
  adapter.seen = seen
  return adapter
}

const realAdapter = axios.defaults.adapter
const results = []
async function test(name, body) {
  try {
    await body()
    results.push({ name, status: 'PASS' })
    console.log(`PASS  ${name}`)
  } catch (error) {
    results.push({ name, status: 'FAIL', message: error.message })
    console.log(`FAIL  ${name} — ${error.message}`)
  } finally {
    axios.defaults.adapter = realAdapter
  }
}

function freshStore() {
  setActivePinia(createPinia())
  const request = load('api/request.ts')
  const { useLiveStore } = load('stores/live.ts', { '@/api/request': request })
  return useLiveStore()
}

function cleanup(store) {
  if (!store) return
  store.stopStartWatch()
  store.stopStatusReads()
  store.stopLocalTick()
}

const TICKET = { ticket: 'boot-A:0:1', epoch: 0 }
const ACCEPTED = {
  run_id: 'run-B', task_id: 41, zone_name: '游戏区',
  confirmed_elapsed_seconds: 600, duration_seconds: 7200,
  duration_known: true, source: 'new', accepted_epoch: 0,
}

await test('B01 受理即有界显示：冻结基线 + 有界流逝，权威观察后收敛回落', async () => {
  axios.defaults.adapter = makeAdapter([
    { data: TICKET },                                       // 取票
    { data: { success: true, message: '直播已开始', accepted: ACCEPTED } },  // start
    { latency: 30_000, data: snap({ elapsed: 600 }) },      // 首次权威观察（在途 30 秒）
  ])
  const store = freshStore()
  try {
    const outcome = await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
    })
    assert.equal(outcome.success, true, outcome.message)
    assert.equal(store.status.elapsed_seconds, 600, '权威记账保持 600（外推不写回）')
    // 首个权威观察在途 30 秒：乐观地板=600+30；领先量冻结为 30 并线性收敛。
    const displayAtFirst = store.localElapsed
    assert.ok(displayAtFirst >= 600 && displayAtFirst <= 600 + 31,
      `受理显示应有界（600..630），实际 ${displayAtFirst}`)
    // 时钟推进 45 秒（收敛完成）：显示回落权威值。
    tick(45)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 600, `收敛后显示=权威值，实际 ${store.localElapsed}`)
    assert.equal(store.status.elapsed_seconds, 600)
  } finally { cleanup(store) }
})

await test('B01 乐观封顶：流逝 200 秒显示也不超过基线 +120', async () => {
  // 受理后没有任何权威观察（status 读持续失败也不影响有界性）。
  axios.defaults.adapter = makeAdapter([
    { data: TICKET },
    { data: { success: true, message: '直播已开始', accepted: ACCEPTED } },
  ])
  const store = freshStore()
  try {
    await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
    })
    store.stopStartWatch()
    store.applyStatus(snap({ elapsed: 600, run: 'run-B' }))
    tick(200)
    store.refreshFromAnchor()
    assert.ok(store.localElapsed <= 600 + 121,
      `显示封顶基线+120，实际 ${store.localElapsed}`)
    assert.equal(store.status.elapsed_seconds, 600, '权威记账不变')
  } finally { cleanup(store) }
})

await test('B01 受理失败：零乐观显示', async () => {
  axios.defaults.adapter = makeAdapter([
    { data: TICKET },
    { data: { success: false, message: '已有直播进行中，不能重复开播',
              refusal: 'already_streaming' } },
  ])
  const store = freshStore()
  try {
    const outcome = await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
    })
    assert.equal(outcome.success, false)
    assert.equal(store.optimisticRunId, '', '失败不得建立乐观会话')
  } finally { cleanup(store) }
})

await test('B02 停止撤除乐观：显示立即回落权威值', async () => {
  axios.defaults.adapter = makeAdapter([
    { data: TICKET },
    { data: { success: true, message: '直播已开始', accepted: ACCEPTED } },
    { latency: 30_000, data: snap({ elapsed: 600 }) },   // 首个权威观察（在途 30 秒）
    { data: TICKET },
    { data: { success: true, message: '直播已停止' } },
  ])
  const store = freshStore()
  try {
    await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
    })
    store.stopStartWatch()
    assert.equal(store.optimisticRunId, 'run-B', '权威观察后乐观会话仍在（未接管完）')
    tick(20)
    store.refreshFromAnchor()
    const before = store.localElapsed
    assert.ok(before > 600, `乐观期间显示领先（${before} > 600）`)
    await store.stopLive()
    assert.equal(store.optimisticRunId, '', '停止撤除乐观会话')
    assert.ok(store.localElapsed <= 600, `显示不再领先（${store.localElapsed} <= 600）`)
  } finally { cleanup(store) }
})

await test('B02 换会话（run 变化）撤除旧乐观', async () => {
  axios.defaults.adapter = makeAdapter([
    { data: TICKET },
    { data: { success: true, message: '直播已开始', accepted: ACCEPTED } },
    { latency: 30_000, data: snap({ elapsed: 600 }) },
  ])
  const store = freshStore()
  try {
    await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
    })
    store.stopStartWatch()
    assert.equal(store.optimisticRunId, 'run-B')
    // 新会话快照（另一场直播）：旧乐观必须撤除，显示跟随新快照。
    store.applyStatus(snap({ elapsed: 30, run: 'run-C', version: 1 }))
    assert.equal(store.optimisticRunId, '', '换 run 撤除旧乐观')
    assert.equal(store.localElapsed, 30, '显示跟随新会话权威值')
  } finally { cleanup(store) }
})

await test('A01 指定任务请求携带冻结身份；abandon_saved 存在时一并携带', async () => {
  const adapter = makeAdapter([
    { data: TICKET },
    { data: { success: true, message: '直播已开始', accepted: ACCEPTED } },
  ])
  axios.defaults.adapter = adapter
  const store = freshStore()
  try {
    await store.startLive(undefined, undefined, {
      taskId: 41, expectedRevision: 12, businessDate: '2026-09-28',
      abandonSaved: {
        old_run_id: 'run-old', old_task_id: 17, old_zone_name: '学习区',
        old_execution_date: '2026-09-28', old_elapsed_seconds: 600,
        confirm_id: 'confirm-1', stop_epoch: 0,
      },
    })
    const startCall = adapter.seen.find(c => c.url.includes('/api/live/start'))
    assert.ok(startCall, '必须发出 /api/live/start')
    const body = typeof startCall.body === 'string'
      ? JSON.parse(startCall.body) : startCall.body
    assert.equal(body.task_id, 41)
    assert.equal(body.expected_revision, 12)
    assert.equal(body.business_date, '2026-09-28')
    assert.equal(body.abandon_saved.old_elapsed_seconds, 600)
    assert.equal(body.abandon_saved.old_run_id, 'run-old')
  } finally { cleanup(store) }
})

const failed = results.filter(r => r.status === 'FAIL').length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
