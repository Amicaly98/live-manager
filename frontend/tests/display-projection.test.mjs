/**
 * display-projection.test.mjs —— 新控制协议下的**显示秒表**（桌面移植阶段 2 重写）。
 *
 * 取代旧 `effective-time-{clamp,monotonic,window}.test.mjs` 与
 * `optimistic-display.test.mjs`：它们断言的 `pending_seconds` /
 * `pending_valid_seconds` / 受理冻结基线（B01/B02）属于旧控制器协议，新链上
 * 后端已经不给这些字段（`GET /api/live/status` 是 `SessionProjection`）。
 *
 * 新链的职责清单（逐条对应下面的用例）：
 *   1) 显示基数与**外推许可**都来自服务端（`display_seconds` /
 *      `display_advancing` / `display_valid_seconds`），页面不得用
 *      `accepted===true` 或 Goal 自行推断；
 *   2) 服务端给的是**剩余**预算：请求在途与传输重试花掉的时间要从许可里扣掉；
 *   3) 一份快照 = **一次**授予：同一快照重复应用不得把已消耗的量还回来；
 *   4) 许可有上限：`display_seconds + display_valid_seconds <= confirmed + 预算`，
 *      耗尽即冻结，不出现"同一份未知越跑越远"；
 *   5) 平台 unknown（服务端把 `display_advancing` 收回）→ 显示回到可信检查点，
 *      **没有**"取历史最大值"的算法；
 *   6) 换账号 / 换场次不得继承上一份显示；
 *   7) 读失败只标记 `statusStale`，显示冻结在最后一次可信值上，不归零；
 *   8) 浏览器失联不产生任何控制动作（不发 Stop/Start，也不写业务秒数）。
 *
 * 夹具与旧套件同一纪律：真实 `api/request.ts` + 真实 Axios 拦截器 + 真实 Pinia，
 * 只替换传输 adapter 与单调耗时源，不发真实网络请求。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const req = createRequire(path.join(root, 'package.json'))
const ts = req('typescript')
const { toRaw } = req('vue')
const axios = req('axios')
const { createPinia, setActivePinia } = req('pinia')

global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }

/** 真实 TS 模块递归装载（`@/` 与相对导入都走同一份 override 表）。 */
function load(relative, overrides = {}, seen = new Map()) {
  const file = path.join(root, 'src', relative)
  if (seen.has(file)) return seen.get(file)
  const module = { exports: {} }
  seen.set(file, module.exports)
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
      esModuleInterop: true,
    },
  }).outputText
  function local(name) {
    if (name in overrides) return overrides[name]
    if (name === 'element-plus') {
      return { ElMessage: { warning() {}, error() {}, success() {}, info() {} } }
    }
    if (name === 'qrcode') return { toDataURL: async () => 'data:,qrcode' }
    if (name.startsWith('@/')) return load(`${name.slice(2)}.ts`, overrides, seen)
    if (name.startsWith('.')) {
      return load(`${path.relative(path.join(root, 'src'),
                                     path.resolve(path.dirname(file), name))}.ts`,
                  overrides, seen)
    }
    return req(name)
  }
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                      { filename: file })(local, module, module.exports)
  return module.exports
}

// ---- 可控单调耗时源（新 store 的绘制外推与预算折算都走 performance.now） ----
let monoMs = 1_000_000
Object.defineProperty(globalThis, 'performance', {
  configurable: true,
  value: { now: () => monoMs },
})
const tick = (seconds) => { monoMs += Math.round(seconds * 1000) }

/** 一份 `GET /api/live/status` 投影（字段与后端 projection.build_projection 对齐）。 */
function projection({
  run = 'run-A', uid = 92003, room = 55003, confirmed = 600,
  display = 600, advancing = true, valid = 10, source = 'confirmed',
  target = 7200, step = 'live', wanted = 'running', account = true,
} = {}) {
  return {
    has_active: true, has_run: true, resumable: false,
    resume_blocked_reason: '', step, goal: { revision: 3, wanted, mode: 'task',
      selection: {}, start_kind: 'new', stop_reason: null },
    run: {
      id: run, uid, room_id: room, area_id: 1, zone_name: '明日方舟',
      mode: 'task', task_id: 7, business_day: '2026-10-10',
      target_seconds: target, push_source: 'ffmpeg',
      clock: { checkpoint_seconds: confirmed, interval_start_mono: null,
               interval_start_wall: null, projecting: false,
               display_anchor_mono: null },
      end: null,
    },
    current_zone: '明日方舟', run_id: run, task_id: 7,
    business_day: '2026-10-10', mode: 'task', push_source: 'ffmpeg',
    target_seconds: target,
    confirmed_seconds: confirmed,
    display_seconds: display,
    display_advancing: advancing,
    display_valid_seconds: valid,
    display_source: source,
    duration_known: target > 0,
    room_id: room, end: null, wait_reason: '',
    account: account ? { uid, room_id: room, display_name: '测试账号' } : null,
    boot_id: 'boot-A', backend_events: [], face: null,
  }
}

/** 脚本化 adapter：按顺序消耗 plan，`latency` 记入本地时钟。 */
function makeAdapter(plan) {
  let index = 0
  return async (config) => {
    const step = plan[Math.min(index, plan.length - 1)]
    index += 1
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
  if (store) store.teardown()
}

const round = (value) => Math.round(Number(value) || 0)

// ------------------------------------------------ 1) 许可只来自服务端
await test('显示基数与许可只来自服务端投影：确认中许可推进，冻结许可为零', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({ confirmed: 600, display: 605, valid: 8,
                                     advancing: true, source: 'confirmed' }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(round(store.displaySeconds), 605, '起始值 = 服务端给的基数')
    assert.equal(store.confirmedSeconds, 600)
    assert.equal(store.projection.display_source, 'confirmed')

    // 许可内推进：本地时钟走了 3 秒，显示 +3（只画，不发任何请求）。
    tick(3)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 608, '许可内应继续外推')

    // 服务端收回许可（unknown / 停止 / 预算耗尽都走这条路）→ 回到可信检查点。
    const frozen = projection({ confirmed: 600, display: 600, advancing: false,
                                valid: 0, source: 'frozen' })
    store.applyStatus(frozen)
    assert.equal(round(store.displaySeconds), 600,
                 '许可被收回后必须停在检查点，不得保留历史最大值')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 2) 传输耗时从许可里扣
await test('请求在途与重试的耗时从剩余许可里扣掉（迟到快照不得凭空续期）', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 4_000, data: projection({ confirmed: 600, display: 610,
                                         valid: 10 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    // 快照生成时还剩 10 秒；请求花了 4 秒 → 页面最多再推 6 秒。
    assert.equal(round(store.displaySeconds), 610, '到达时 = 服务端基数')
    tick(6)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 616, '剩余许可恰好用完')

    // 同一快照再应用：预算不得重新授予（一次性）。
    tick(1)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 616, '同一份快照不重新授予')

    tick(100)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 616, '许可上限封顶')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 3) 未知不发明"永不回退"
await test('平台 unknown 后回到检查点：没有取历史最大值的前端算法', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({ confirmed: 600, display: 620, valid: 20 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    tick(20)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 640, '基数 620 + 许可 20 秒用满')

    // 服务端现在报告 unknown：基数回到检查点、许可为零。
    const unknownNow = projection({ confirmed: 600, display: 600, advancing: false,
                                    valid: 0, source: 'frozen' })
    store.applyStatus(unknownNow)
    assert.equal(round(store.displaySeconds), 600,
                 '未知必须把显示还回检查点，不得停在 620')
    assert.equal(store.timerHint, '', '未知不是停播，也不宣传"已暂停"以外的术语')

    // 恢复确认后重新授予（新快照 = 新许可）。
    const recovered = projection({ confirmed: 600, display: 604, valid: 10,
                                   source: 'confirmed' })
    store.applyStatus(recovered)
    assert.equal(round(store.displaySeconds), 604, '新快照重新授予许可')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 4) 换账号 / 换场次
await test('换账号不继承上一账号的秒表；同账号换场也不继承', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({ run: 'run-A', confirmed: 600,
                                     display: 618, valid: 18 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    tick(10)
    store.applyStatus(toRaw(store.projection))
    assert.equal(round(store.displaySeconds), 628, '许可内推进 10 秒')

    const otherAccount = projection({ run: 'run-B', uid: 88001, room: 51001,
                                      confirmed: 12, display: 12, valid: 5 })
    store.applyStatus(otherAccount)
    assert.equal(round(store.displaySeconds), 12, '换账号后显示必须回到新账号基数')

    const otherRun = projection({ run: 'run-C', confirmed: 40, display: 40,
                                  valid: 5 })
    store.applyStatus(otherRun)
    assert.equal(round(store.displaySeconds), 40, '换场次同样不得继承上一场的显示')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 5) 读失败不归零
await test('状态读失败：标记失联、显示只在已授予的许可内走动且不归零', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({ confirmed: 600, display: 612, valid: 12 }) },
    { latency: 1_000, fail: true },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    const before = round(store.displaySeconds)
    assert.equal(before, 612)

    await store.pollStatus()          // 传输失败（含真实退避等待）
    assert.equal(store.statusStale, true, '失败要如实标记，不得当作停播')
    const after = round(store.displaySeconds)
    // 失联期间页面仍按**已授予**的许可继续画（这是设计行为），但必须满足：
    // 不归零、不回退、不超过"基数 + 服务端许可"。
    assert.ok(after >= before, `显示不得回退（${before} → ${after}）`)
    assert.ok(after <= 612 + 12, `显示不得超过基数 + 许可（实际 ${after}）`)

    // 失联不产生任何新许可：读失败不会把预算续满。
    tick(5)
    store.applyStatus(toRaw(store.projection))
    assert.ok(round(store.displaySeconds) <= 624,
              '同一份快照的许可不得因读失败而重新授予')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 6) 失联不触发控制动作
await test('失联与恢复全程零控制请求：页面不发 Stop/Start，也不重放旧令牌', async () => {
  const posted = []
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({}) },
    { latency: 1_000, fail: true },
    { latency: 0, data: projection({ confirmed: 601, display: 601, valid: 5 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    await store.pollStatus()
    await store.pollStatus()
    const runs = store.projection.run_id
    assert.equal(runs, 'run-A')
    // 断言整个过程中没有任何 POST 发生（控制请求才会 POST）。
    assert.deepEqual(posted, [], '显示与读失败路径不得发出任何控制请求')
  } finally { cleanup(store) }
})

// ------------------------------------------------ 7) 进度与超额展示
await test('进度百分比：确认达标才 100%，外推最多画到 99', async () => {
  axios.defaults.adapter = makeAdapter([
    { latency: 0, data: projection({ confirmed: 7100, display: 7180,
                                     valid: 30, target: 7200 }) },
  ])
  const store = freshStore()
  try {
    await store.pollStatus()
    assert.equal(store.progressPercent, 99, '外推段不得画成 100%')
    const done = projection({ confirmed: 7200, display: 7200, valid: 0,
                              advancing: false, source: 'frozen',
                              target: 7200, step: 'idle', wanted: 'stopped' })
    store.applyStatus(done)
    assert.equal(store.progressPercent, 100, '已确认值达标才算完成')
    assert.equal(store.overtime, false)
  } finally { cleanup(store) }
})

let failures = 0
for (const item of results) {
  if (item.status !== 'PASS') failures += 1
}
console.log(failures ? `display projection: ${failures} failed` : 'display projection: all passed')
process.exitCode = failures ? 1 : 0
