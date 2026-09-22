/**
 * 外推窗口的**授予**：真实 request.ts + 真实 Axios + 真实 Pinia（2026-09-20 监督 E2）。
 *
 * 被验证的规则：
 *   1) `pending_valid_seconds` 是"快照生成那一刻还剩多少可外推"，**不是**"收到时还剩
 *      多少"——请求在路上花掉的时间（含传输层重试与退避）必须从窗口里扣掉；
 *   2) 延迟把窗口耗尽时，到达即冻结在"已确认 + 服务端给出的待确认段"上，不再增长；
 *   3) 窗口一旦授予只减不增：同一缓存快照重复应用、计时器重启（startLocalTick 重设
 *      锚点）、syncFromServer 都不重新授予，也不会把已消耗的量还回去；
 *   4) 取消后的迟到响应不得应用（generation 门控前置），窗口与显示都不受影响；
 *   5) 新鲜快照可以重新开启窗口（恢复），普通页面仍然平滑推进。
 *
 * 只替换传输边界与本地时钟：store、request.ts、Axios 拦截器（含重试/退避/取消）
 * 用的都是生产实现。不发真实网络请求（adapter 被替换）。
 *
 * 负向对照会把 src/ 复制到临时目录并注入"旧行为"补丁，再用 OUTER_TEST_SRC 指过来；
 * 正常跑法不设该变量，用的就是仓库里的源码。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

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

/** 真实 TS 模块加载：`@/api/request` 换成真实模块，其余相对导入递归加载。 */
function load(relative, overrides = {}) {
  const file = path.join(root, 'src', relative)
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    },
  }).outputText
  function local(name) {
    if (name in overrides) return overrides[name]
    if (name === 'element-plus') {
      return { ElMessage: { warning() {}, error() {}, success() {} } }
    }
    if (name.startsWith('@/stores/')) {
      // 2026-09-21（A）：live store 新增 operationEvents 依赖，装载器递归加载。
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

// ---- 可控本地时钟：替换**单调耗时源**（store 的插值与窗口折算走的正是它） ----
// 迁移说明（2026-09-20 监督 E3）：夹具原来替换的是 `Date.now`。生产改用单调耗时源
// （`performance.now()`）推导窗口消耗后，只改 `Date.now` 已经推不动 store 的时钟，
// 那会让"请求耗时是否被扣掉"这类断言失去意义（不是产品回归，是注入点失效）。
// 注入点换到同一个单调源，业务断言一行未动。
const realPerformance = Object.getOwnPropertyDescriptor(globalThis, 'performance')
let monoMs = 1_000_000
Object.defineProperty(globalThis, 'performance', {
  configurable: true,
  value: { now: () => monoMs },
})
const tick = (seconds) => { monoMs += Math.round(seconds * 1000) }

/** 一份服务端状态快照（字段与后端 `get_session_snapshot` 对齐）。 */
function snap({
  elapsed = 600, pending = 0, pendingValid = 0, extrapolatable = false,
  timerState = 'running', run = 'run-A', version = 1, duration = 7200,
} = {}) {
  return {
    is_streaming: true, is_starting: false, run_id: run, boot_id: 'boot-A',
    session_version: version, duration_known: true, duration_seconds: duration,
    elapsed_seconds: elapsed, pending_seconds: pending,
    pending_valid_seconds: pendingValid, pending_limit_seconds: 90,
    pending_extrapolatable: extrapolatable, timer_state: timerState,
    phase: 'live',
  }
}

/**
 * 脚本化 adapter：按顺序消耗 `plan` 里的每一步。
 *
 * `latency` 表示这一步**消耗掉多少本地时间**（请求在途/传输重试本身的花费）。
 * 失败步骤用 `fail: true` 造一个"没有响应"的网络错误，走真实拦截器的重试路径
 * （退避由真实 setTimeout 执行，这里用等效的本地时钟推进把它的耗时记进窗口）。
 */
function makeAdapter(plan) {
  let index = 0
  return async (config) => {
    const step = plan[Math.min(index, plan.length - 1)]
    index += 1
    monoMs += step.latency
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

/** 用真实 request.ts 建一个 store（adapter 已按 scenario 装好）。 */
function freshStore() {
  setActivePinia(createPinia())
  const request = load('api/request.ts')
  const { useLiveStore } = load('stores/live.ts', { '@/api/request': request })
  return useLiveStore()
}

function cleanup(store) {
  if (store) { store.stopStatusReads(); store.stopLocalTick() }
}

// ---------------------------------------------------------------- 1) 新鲜：平滑推进
await test('新鲜快照：按剩余窗口平滑推进，用尽后冻结', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                               extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 620, '起始值 = 已确认 + 当时待确认')
    tick(1)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 621, '窗口内应继续外推')
    tick(5)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 626)
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630, '窗口 10 秒用尽后必须冻结')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 2) 边界
await test('边界：请求耗时正好等于剩余有效期 ⇒ 到达即冻结（不再授予窗口）', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 10_000, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                                   extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 620, '保留安全显示值（已确认 + 待确认段）')
    tick(5)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 620, '剩余有效期为 0：一秒都不许再涨')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 3) 延迟超过有效期
await test('延迟超过有效期：到达即冻结，1 秒后仍不增长（监督 E2 反例）', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 20_000, data: snap({ elapsed: 600, pending: 85, pendingValid: 5,
                                   extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 685, '到达时显示已确认 + 服务端给出的待确认段')
    tick(1)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 685,
                 '窗口在传输期间已耗尽：不得再领取那 5 秒')
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 685, '此后一直冻结')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 4) 取消
await test('取消后的迟到响应不得应用：窗口与显示都不变', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                              extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 620)

    // 第二次读：响应停在在途状态，测试期间取消
    let release
    const gate = new Promise((resolve) => { release = resolve })
    axios.defaults.adapter = async (config) => {
      await gate
      monoMs += 30_000
      return { status: 200, statusText: 'OK', headers: {}, config,
               data: snap({ elapsed: 900, pending: 50, pendingValid: 40,
                            extrapolatable: true, version: 2 }) }
    }
    const inflight = store.pollStatus()
    store.stopStatusReads()
    tick(3)
    store.refreshFromAnchor()
    const frozen = store.localElapsed
    release()
    await inflight
    assert.equal(store.localElapsed, frozen,
                 '被取消的响应不得改写显示')
    assert.equal(String(store.status.elapsed_seconds), '600',
                 '被取消的响应不得改写状态')
    assert.equal(store.sessionId, 'run-A')
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630,
                 '旧窗口照旧按自己的剩余有效期冻结，不因被取消的响应延长')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 5) 重试耗时
await test('含一次传输重试：两次尝试的耗时都从窗口里扣除', async () => {
  axios.defaults.adapter = makeAdapter([
    // 第一次：网络错误（真实拦截器会退避后经同一实例重试）
    { latency: 2_000, fail: true },
    // 第二次：成功，但在途又花了 3 秒 ⇒ 共 5 秒，窗口只剩 5 秒
    { latency: 3_000, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                                   extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 620)
    tick(5)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 625, '重试+在途共 5 秒：只剩 5 秒窗口')
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 625,
                 '窗口必须按剩余有效期冻结（未扣除重试耗时时会涨到 630）')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 6) 同一快照重复应用
await test('同一缓存快照重复应用 / 计时器重启：不重新授予窗口', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                              extrapolatable: true }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    const cached = store.status
    assert.equal(store.localElapsed, 620)

    tick(4)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 624, '已用 4 秒')

    // 计时器重启（重设锚点）：不得把用掉的 4 秒还回来
    store.startLocalTick()
    assert.equal(store.localElapsed, 624, '重启计时器不得回退显示')
    tick(1)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 625,
                 '重启后继续按原窗口剩余量推进（只有 6 秒了）')

    // 同一份缓存快照重复应用 + syncFromServer：都不得重新授予
    store.applyStatus(cached)
    assert.equal(store.localElapsed, 625, '重复应用同一快照不得重新授予窗口')
    store.syncFromServer()
    assert.equal(store.localElapsed, 625)

    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630,
                 '窗口总消耗仍只有 10 秒（旧行为会按"再次收到"重新给满 10 秒）')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 7) 新鲜快照恢复
await test('新鲜快照可以重新开启窗口：冻结之后收到新快照继续平滑推进', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                               extrapolatable: true }) },
    { latency: 0, data: snap({ elapsed: 700, pending: 30, pendingValid: 40,
                               extrapolatable: true, version: 2 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630, '第一段窗口用尽后冻结')

    await store.pollStatus()
    assert.equal(store.localElapsed, 730, '新快照把显示校正到新的已确认 + 待确认')
    tick(5)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 735, '新窗口重新开始外推')
    tick(600)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 770, '第二段窗口 40 秒用尽后冻结')
  } finally { cleanup(store) }
})

if (realPerformance) {
  Object.defineProperty(globalThis, 'performance', realPerformance)
} else {
  delete globalThis.performance
}
axios.defaults.adapter = realAdapter

const failed = results.filter((r) => r.status === 'FAIL')
console.log(`\neffective-time-window: ${results.length - failed.length}/`
            + `${results.length} passed`)
process.exit(failed.length === 0 ? 0 : 1)
