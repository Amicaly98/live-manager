/**
 * R06 面板状态闭合（阶段 4C 迁移）：真实 store + 真实请求链的行为回归。
 *
 * 判据沿用旧版，载荷形状按新投影改写（迁移登记见
 * `PHASE4C-MIGRATION-REGISTER.md`）：
 *   1) 旧响应倒序：先发后到的旧快照被请求序号丢弃；
 *   2) 单飞：同时只有一个在途状态读，重复调用共享同一份；
 *   3) 离线：读失败保留陈旧显示与"仍在运行"的事实，绝不归零、不解释成停播；
 *   4) 页面离开/回来：取消在途读后重新订阅仍能同步新场次；
 *   5) 换账号：账号代次前进，显示不继承上一账号；
 *   6) 任务页版本驱动重排：revision 前进才重取列表。
 *
 * 只替换传输边界（真实 TS store 转译 + 真实 Vue/Pinia），不访问网络。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..')
const root = process.env.OUTER_TEST_SRC || repoRoot
const req = createRequire(path.join(repoRoot, 'package.json'))
const ts = req('typescript')
const { createPinia, setActivePinia } = req('pinia')

// 浏览器 API 最小替身：store 会用到 localStorage（事件去重 / 任务缓存）。
if (typeof globalThis.localStorage === 'undefined') {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
}

const _cache = new Map()
function loadTsFile(relPath, customRequire) {
  const full = path.join(root, 'src', relPath)
  const code = ts.transpileModule(fs.readFileSync(full, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
  }}).outputText
  const mod = { exports: {} }
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                      { filename: full })(customRequire, mod, mod.exports)
  return mod.exports
}

function loadStore(file, transport, extraRequire = {}) {
  const key = `${file}::${Object.keys(extraRequire).join(',')}`
  if (_cache.has(key)) return _cache.get(key)
  const customRequire = (name) => {
    if (name in extraRequire) return extraRequire[name]
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/stores/operationEvents') {
      return loadStore('operationEvents.ts', transport)
    }
    if (name === '@/boot') return loadTsFile('boot.ts', (n) => req(n))
    if (name === '@/composables/useCache') {
      const store = new Map()
      return { default: { get: (k) => store.get(k), set: (k, v) => store.set(k, v),
                          remove: (k) => store.delete(k) } }
    }
    if (name.startsWith('@/')) {
      return loadTsFile(name.slice(2) + '.ts', customRequire)
    }
    return req(name)
  }
  const loaded = loadTsFile(path.join('stores', file), customRequire)
  _cache.set(key, loaded)
  return loaded
}

function makeTransport(routes = {}) {
  const calls = []
  const transport = {
    get: (url, params, options) => { calls.push({ method: 'GET', url, options });
      return routes.get ? routes.get(url, options) : Promise.resolve({}) },
    post: (url, body, options) => { calls.push({ method: 'POST', url, body, options });
      return routes.post ? routes.post(url, body, options) : Promise.resolve({}) },
    put: (url) => { calls.push({ method: 'PUT', url }); return Promise.resolve({}) },
    delete: (url) => { calls.push({ method: 'DELETE', url }); return Promise.resolve({}) },
  }
  return { transport, calls }
}

const ACCOUNT = { uid: 90001, room_id: 5001, display_name: '夹具主播' }

/** 一份**新投影**（阶段 4C 的 GET 形状）。 */
function snap({ runId = 'r1', confirmed = 0, display = 0, target = 7200,
                advancing = true, validSeconds = 120, step = 'live',
                wanted = 'running', account = ACCOUNT, bootId = 'boot-A',
                source = 'confirmed', hasRun = true, events = [] } = {}) {
  const run = hasRun ? {
    id: runId, uid: account?.uid ?? 0, room_id: account?.room_id ?? 0,
    area_id: 10003, zone_name: '学习区', mode: 'task', task_id: 4,
    business_day: '2026-10-04', target_seconds: target, push_source: 'obs',
    clock: { checkpoint_seconds: confirmed, interval_start_mono: 500,
             interval_start_wall: null, projecting: advancing,
             display_anchor_mono: null },
    end: null,
  } : null
  return {
    has_active: hasRun && wanted === 'running', has_run: hasRun,
    resumable: false, resume_blocked_reason: '', step,
    goal: hasRun ? { revision: 3, wanted, mode: 'task', selection: {},
                     start_kind: 'new', stop_reason: null } : null,
    run, current_zone: run ? '学习区' : '', run_id: run ? runId : null,
    task_id: run ? 4 : null, business_day: '2026-10-04', mode: 'task',
    push_source: 'obs', target_seconds: hasRun ? target : 0,
    confirmed_seconds: confirmed, display_seconds: display,
    display_advancing: advancing, display_valid_seconds: validSeconds,
    display_source: source, duration_known: hasRun && target > 0,
    room_id: run ? run.room_id : null, end: null, wait_reason: '',
    account, boot_id: bootId, backend_events: events, face: null,
  }
}

const results = []
async function test(name, body) {
  try { await body(); results.push({ name, status: 'PASS' }) }
  catch (error) { results.push({ name, status: 'FAIL', message: error.message }) }
}

// ---------------------------------------------------------------- 1) 旧响应倒序
await test('旧响应倒序：先发后到的旧快照被请求序号丢弃', async () => {
  setActivePinia(createPinia()); _cache.clear()
  const pending = []
  const { transport } = makeTransport({
    get: () => new Promise((resolve) => pending.push(resolve)),
  })
  const { useLiveStore } = loadStore('live.ts', transport)
  const store = useLiveStore()
  const older = store.fetchStatus()
  const newer = store.fetchStatus()
  pending[1](snap({ runId: 'new-run', confirmed: 800, display: 800 }))
  await newer
  pending[0](snap({ runId: 'old-run', confirmed: 0, display: 0 }))
  await older
  if (store.runId !== 'new-run') throw new Error(`run_id 被旧响应改写：${store.runId}`)
  if (store.confirmedSeconds !== 800) {
    throw new Error(`已确认秒数被旧响应拉回：${store.confirmedSeconds}`)
  }
  store.teardown()
})

// ---------------------------------------------------------------- 2) 单飞
await test('状态读取单飞：并发调用共享同一个在途请求', async () => {
  setActivePinia(createPinia()); _cache.clear()
  const { transport, calls } = makeTransport({
    get: () => new Promise((resolve) => setTimeout(() => resolve(snap()), 30)),
  })
  const { useLiveStore } = loadStore('live.ts', transport)
  const store = useLiveStore()
  const [a, b, c] = await Promise.all([
    store.pollStatus(), store.pollStatus(), store.pollStatus(),
  ])
  if (a !== b || b !== c) throw new Error('并发读必须共享同一份结果')
  const reads = calls.filter((call) => call.url === '/api/live/status').length
  if (reads !== 1) throw new Error(`在途读必须单飞（实际发出 ${reads} 次）`)
  store.teardown()
})

// ---------------------------------------------------------------- 3) 离线
await test('离线恢复：失败保留陈旧显示，网络恢复后正常应用', async () => {
  setActivePinia(createPinia()); _cache.clear()
  let online = true
  const { transport } = makeTransport({
    get: () => online
      ? Promise.resolve(snap({ runId: 'run-online', confirmed: 600, display: 600 }))
      : Promise.reject(new Error('network down')),
  })
  const { useLiveStore } = loadStore('live.ts', transport)
  const store = useLiveStore()
  await store.pollStatus()
  if (store.confirmedSeconds !== 600) throw new Error('首次读必须落地')
  online = false
  await store.pollStatus().catch(() => {})
  if (store.displaySeconds < 600) throw new Error('离线不得把显示归零')
  if (store.runId !== 'run-online') throw new Error('离线不得丢掉场次身份')
  if (store.statusStale !== true) throw new Error('离线必须如实标记为陈旧')
  online = true
  await store.pollStatus()
  if (store.statusStale !== false) throw new Error('恢复后必须清掉陈旧标记')
  store.teardown()
})

// ---------------------------------------------------------------- 4) 离开/回来
await test('页面离开/回来：取消在途读后重新订阅仍能同步新场次', async () => {
  setActivePinia(createPinia()); _cache.clear()
  let current = snap({ runId: 'run-A', confirmed: 100, display: 100 })
  const { transport } = makeTransport({ get: () => Promise.resolve(current) })
  const { useLiveStore } = loadStore('live.ts', transport)
  const store = useLiveStore()
  await store.pollStatus()
  if (store.runId !== 'run-A') throw new Error('首次读必须落地')
  // 页面离开：取消在途读与收敛观察
  store.stopStatusReads()
  store.stopSettleWatch()
  // 回来：新一场
  current = snap({ runId: 'run-B', confirmed: 30, display: 30 })
  await store.pollStatus()
  if (store.runId !== 'run-B') throw new Error('回来后必须同步新场次')
  if (Math.abs(store.displaySeconds - 30) > 0.5) {
    throw new Error('新场不得继承上一场的显示：' + store.displaySeconds)
  }
  store.teardown()
})

// ---------------------------------------------------------------- 5) 换账号
await test('换账号：账号代次前进且显示不继承', async () => {
  setActivePinia(createPinia()); _cache.clear()
  let current = snap({ confirmed: 900, display: 900 })
  const { transport } = makeTransport({ get: () => Promise.resolve(current) })
  const { useLiveStore } = loadStore('live.ts', transport)
  const store = useLiveStore()
  await store.pollStatus()
  const generation = store.accountGeneration
  current = snap({ hasRun: false, step: 'idle', wanted: 'stopped',
                   account: { uid: 90002, room_id: 5002, display_name: '另一个账号' } })
  await store.pollStatus()
  if (store.accountGeneration <= generation) throw new Error('账号代次必须前进')
  if (store.displaySeconds > 0.5) {
    throw new Error('换账号不得继承上一账号的显示：' + store.displaySeconds)
  }
  store.teardown()
})

// ---------------------------------------------------------------- 6) 任务页重排
await test('任务页版本驱动重排：revision 前进才重取列表', async () => {
  setActivePinia(createPinia()); _cache.clear()
  let revision = 7
  const taskA = { id: 1, zone_name: '学习区', priority: 10 }
  const taskB = { id: 2, zone_name: '游戏区', priority: 20 }
  let tasks = [taskA, taskB]
  const listCalls = []
  const { transport } = makeTransport({
    get: (url) => {
      if (url === '/api/tasks/stats') {
        return Promise.resolve({ total: 2, revision, business_date: '2026-10-04' })
      }
      if (url === '/api/tasks') {
        listCalls.push(revision)
        return Promise.resolve({
          tasks, total: 2, pending_total: 2, completed: 0, today_done: 0,
          today_pending: 2, remaining_time: 0, avg_remaining: 0, urgency: 0,
          revision, business_date: '2026-10-04',
        })
      }
      return Promise.resolve({})
    },
  })
  // tasks store 依赖 auth store（认证意图门控）：必须共用**同一个** transport。
  const authModule = loadStore('auth.ts', transport)
  const { useTaskStore } = loadStore('tasks.ts', transport,
                                     { '@/stores/auth': authModule })
  const store = useTaskStore()
  authModule.useAuthStore().isLoggedIn = true
  await store.fetchTasks()
  if (store.tasks.map((t) => t.zone_name).join() !== '学习区,游戏区') {
    throw new Error('首次读必须落地列表')
  }
  const unchanged = await store.refreshTasksIfStale()
  if (unchanged !== false) throw new Error('revision 未变时不得重取列表')
  if (listCalls.length !== 1) throw new Error('revision 未变却重取了列表')

  revision = 8
  tasks = [taskB, taskA]
  const changed = await store.refreshTasksIfStale()
  if (changed !== true) throw new Error('revision 前进必须重取列表')
  if (store.tasks.map((t) => t.zone_name).join() !== '游戏区,学习区') {
    throw new Error('重取后必须按新顺序渲染')
  }
})

const failed = results.filter((r) => r.status === 'FAIL')
console.log(JSON.stringify(results, null, 2))
console.log(`store-status-protocol: ${results.length - failed.length}/${results.length} passed`)
process.exitCode = failed.length ? 1 : 0
