/**
 * 外推有效期的**时间来源**（单调耗时源）：真实 request.ts + 真实 Axios + 真实 Pinia
 * （2026-09-20 监督 E3）。
 *
 * 被验证的规则：外推窗口的"已消耗量"由**只增不减**的本地耗时推导，因此
 *   1) 系统校时（墙钟后跳）**不能**把已经用掉的窗口还给页面；
 *   2) 系统校时**不能**把请求已经花掉的时间藏起来（一条早就过期的快照不得被当成新鲜的
 *      而重新领到一整段外推）；
 *   3) 系统校时（墙钟前跳）**不能**让显示虚增；
 *   4) 单调时钟的起点 0 是合法值，不能被当成"未初始化"（否则外推永远停在第 0 秒）；
 *   5) 宿主没有 `performance.now()` 时的兜底路径必须同样单调（钳制墙钟），
 *      即"退回墙钟"不等于"退回可回拨"；
 *   6) 窗口耗尽后收到新快照可以重新开启窗口，正常推进不受影响。
 *
 * 只替换传输边界与两个时钟（墙钟 `Date.now` 与单调时钟 `performance.now` 独立控制）；
 * store、request.ts、Axios 拦截器（含重试/退避/取消）用的都是生产实现，不发真实网络。
 *
 * 负向对照会把 src/ 复制到临时目录并注入"回落墙钟"补丁，再用 OUTER_TEST_SRC 指过来；
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

// ---------------------------------------------------------------- 双时钟
// 墙钟与单调时钟**独立**控制：正常时间前进时两者一起走，校时只动墙钟。
const realWall = Date.now
const realPerfDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'performance')
const realAdapter = axios.defaults.adapter
const WALL0 = 1_700_000_000_000
const MONO0 = 0
let wallMs = WALL0
let monoMs = MONO0

Date.now = () => wallMs
function usePerformanceClock() {
  Object.defineProperty(globalThis, 'performance', {
    configurable: true, value: { now: () => monoMs },
  })
}
function useNoPerformanceClock() {
  // 模拟"宿主没有 performance.now()"：走生产代码里的钳制兜底路径。
  delete globalThis.performance
}
usePerformanceClock()

function resetClocks() { wallMs = WALL0; monoMs = MONO0 }
/** 正常时间前进：两个时钟一起走。 */
function advanceBoth(ms) { wallMs += ms; monoMs += ms }
/** 只让单调耗时才前进（模拟"墙钟被冻结"）。 */
function advanceMonoOnly(ms) { monoMs += ms }
/** 只让日历时间跳变（模拟校时：正数前跳，负数回拨），单调耗时才不动。 */
function adjustWall(ms) { wallMs += ms }

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

// ---- 脚本化 adapter ----
// 注意：`axios.create()` 会把当时的 `axios.defaults.adapter` **快照**进实例，
// 所以这里**只安装一次**（在任何 store 之前），行为完全由可变的 `plan` 决定；
// 中途替换 `axios.defaults.adapter` 对已经建好的实例无效（那会让"测试里换了行为"
// 却什么都没发生的假通过）。
let plan = []
let planIndex = 0
function setPlan(steps) { plan = steps; planIndex = 0 }

axios.defaults.adapter = async (config) => {
  const step = plan[Math.min(planIndex, Math.max(plan.length - 1, 0))]
  planIndex += 1
  if (!step) throw new Error('adapter 脚本为空：测试没有设置这一步的行为')
  if (step.gate) await step.gate
  if (step.monoMs) advanceMonoOnly(step.monoMs)
  if (step.wallAdvanceMs) adjustWall(step.wallAdvanceMs)
  if (step.wallMs) adjustWall(step.wallMs)
  return { status: 200, statusText: 'OK', headers: {}, config, data: step.data }
}

/** 用真实 request.ts 建一个 store。 */
function freshStore() {
  setActivePinia(createPinia())
  const request = load('api/request.ts')
  const { useLiveStore } = load('stores/live.ts', { '@/api/request': request })
  return useLiveStore()
}

function cleanup(store) {
  if (store) { store.stopStatusReads(); store.stopLocalTick() }
}

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
    resetClocks()
    plan = []; planIndex = 0
  }
}

const FAST_FIVE = () => snap({ elapsed: 600, pending: 85, pendingValid: 5,
                               extrapolatable: true })

// ------------------------------------------------- 1) 正常推进（正向对照）
await test('正常推进：窗口内平滑外推，用尽后冻结（正向对照）', async () => {
  setPlan([{ data: FAST_FIVE() }])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 685, '到达时显示已确认 + 服务端给出的待确认段')
    advanceBoth(3000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 688, '窗口内应继续外推')
    advanceBoth(3000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 690, '窗口 5 秒用尽后停在 690')
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 690, '再久也不得继续外推')
  } finally { cleanup(store) }
})

// ------------------------------------- 2) 窗口耗尽后墙钟回拨（监督 E3 反例 1）
await test('窗口耗尽后墙钟回拨：已用掉的窗口不得被还回来', async () => {
  setPlan([{ data: FAST_FIVE() }])
  const store = freshStore()
  try {
    await store.pollStatus()
    advanceBoth(6000)
    store.refreshFromAnchor()
    const expired = store.localElapsed
    assert.equal(expired, 690, '窗口正常到期')

    // 校时回拨 4 秒 + 真实过去 1 秒：显示必须保持冻结
    adjustWall(-4000)
    advanceBoth(1000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, expired,
                 '墙钟回拨让已经用掉的窗口复活了（应保持 690）')

    // 再回拨一小时：仍然不得复活
    adjustWall(-3600_000)
    advanceBoth(1000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, expired, '大幅回拨同样不得复活窗口')
  } finally { cleanup(store) }
})

// ------------------------- 3) 请求进行中校时：耗时不得被藏起来（反例 2）
await test('请求在途时墙钟回拨：过期的快照不得被当成新鲜的', async () => {
  // 快照在路上真实花了 20 秒（单调耗时），期间墙钟被回拨 1 小时
  setPlan([{ monoMs: 20_000, wallMs: -3600_000, data: FAST_FIVE() }])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 685,
                 '请求耗时 20 秒 > 有效期 5 秒：到达即冻结，不得重新领窗口')
    advanceBoth(1000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 685, '此后一直冻结（旧行为会涨到 686）')
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 685, '之后同样不得复活')
  } finally { cleanup(store) }
})

// ---------------------------------------- 4) 墙钟前跳不得让显示虚增
await test('墙钟前跳：显示不得因校时虚增', async () => {
  setPlan([{ data: FAST_FIVE() }])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 685)
    // 校时前跳 1 小时 + 真实过去 1 秒：只应前进 1 秒
    adjustWall(3600_000)
    advanceBoth(1000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 686, '日历时间前跳不得参与外推有效期')
    advanceBoth(10_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 690, '仍按单调耗时在 5 秒窗口处冻结')
  } finally { cleanup(store) }
})

// ------------------------- 5) 单调时钟起点 0 是合法值（零值不得当未初始化）
await test('单调时钟起点为 0：零值是合法起点，窗口照常推进与冻结', async () => {
  setPlan([{ data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                          extrapolatable: true }) }])
  const store = freshStore()
  try {
    assert.equal(monoMs, 0, '前置条件：单调时钟从 0 起算')
    await store.pollStatus()
    assert.equal(store.localElapsed, 620, '起点 0 时窗口必须已授予（否则永远停在第 0 秒）')
    advanceBoth(4000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 624, '起点 0 也要能正常推进')
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630, '起点 0 也要能在窗口处冻结')
  } finally { cleanup(store) }
})

// ------------------------------- 6) 耗尽后校时 + 新快照恢复
await test('耗尽后就算校时，新快照仍能重新开启窗口并正常推进', async () => {
  setPlan([
    { data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                   extrapolatable: true }) },
    { wallMs: -3600_000,
      data: snap({ elapsed: 700, pending: 30, pendingValid: 40,
                   extrapolatable: true, version: 2 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630, '第一段窗口用尽后冻结')

    await store.pollStatus()
    assert.equal(store.localElapsed, 730,
                 '新快照把显示校正到新的已确认 + 待确认（与校时无关）')
    advanceBoth(5000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 735, '新窗口重新开始外推')
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 770, '第二段窗口 40 秒用尽后冻结')
  } finally { cleanup(store) }
})

// ------------- 7) 兜底路径：宿主没有 performance.now() 也必须单调
await test('兜底路径（无 performance.now）：墙钟回拨既还不了窗口也藏不了耗时', async () => {
  useNoPerformanceClock()
  try {
    setPlan([{ data: FAST_FIVE() }])
    const store = freshStore()
    try {
      await store.pollStatus()
      advanceBoth(6000)
      store.refreshFromAnchor()
      assert.equal(store.localElapsed, 690, '兜底路径窗口正常到期')
      adjustWall(-4000)
      advanceBoth(1000)
      store.refreshFromAnchor()
      assert.equal(store.localElapsed, 690, '兜底路径的钳制必须挡住回拨')
    } finally { cleanup(store) }

    // 请求在途 20 秒 + 回拨 1 小时：兜底路径同样不得重新授予窗口。
    // 注意兜底路径以墙钟为时间源，所以这里的 20 秒必须记在墙钟上。
    resetClocks()
    setPlan([{ wallAdvanceMs: 20_000, wallMs: -3600_000, data: FAST_FIVE() }])
    const store2 = freshStore()
    try {
      await store2.pollStatus()
      assert.equal(store2.localElapsed, 685, '兜底路径也必须扣掉请求耗时')
      advanceBoth(1000)
      store2.refreshFromAnchor()
      assert.equal(store2.localElapsed, 685, '兜底路径此后一直冻结')
    } finally { cleanup(store2) }
  } finally { usePerformanceClock() }
})

// ---------------------------------------- 8) 取消 + 校时：门控不受影响
await test('取消后的迟到响应在墙钟回拨下同样不得应用', async () => {
  let release = () => {}
  const gate = new Promise((resolve) => { release = resolve })
  setPlan([
    { data: snap({ elapsed: 600, pending: 20, pendingValid: 10,
                   extrapolatable: true }) },
    // 第二条响应停在在途状态：测试期间取消，放行后它带着"更新的进度 + 校时回拨"到达
    { gate, monoMs: 30_000, wallMs: -3600_000,
      data: snap({ elapsed: 900, pending: 50, pendingValid: 40,
                   extrapolatable: true, version: 2 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.localElapsed, 620)

    const inflight = store.pollStatus()
    store.stopStatusReads()
    advanceBoth(3000)
    store.refreshFromAnchor()
    const frozen = store.localElapsed
    release()
    await inflight
    assert.equal(store.localElapsed, frozen, '被取消的响应不得改写显示')
    assert.equal(String(store.status.elapsed_seconds), '600',
                 '被取消的响应不得改写状态')
    assert.equal(store.sessionId, 'run-A')
    advanceBoth(600_000)
    store.refreshFromAnchor()
    assert.equal(store.localElapsed, 630,
                 '旧窗口照旧按自己的剩余有效期冻结，不因被取消的响应延长')
  } finally { cleanup(store) }
})

// ---------------------------------------------------------------- 收尾
Date.now = realWall
if (realPerfDescriptor) {
  Object.defineProperty(globalThis, 'performance', realPerfDescriptor)
} else {
  delete globalThis.performance
}
axios.defaults.adapter = realAdapter

const failed = results.filter((r) => r.status === 'FAIL')
console.log(`\neffective-time-monotonic: ${results.length - failed.length}/`
            + `${results.length} passed`)
process.exit(failed.length === 0 ? 0 : 1)
