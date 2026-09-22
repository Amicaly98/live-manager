/**
 * task-loading-handover.test.mjs — 任务读加载态责任交接（P2，2026-09-21 第四轮监督）。
 *
 * 真实 tasks store（TS 转译）+ 真实 pinia + 真实 auth store + 真实 boot，
 * 只把传输边界换成可编排替身。监督原件 supervision-refresh-loading.mjs 在
 * 真实 dist + Chromium 上证明"静默接替后遮罩恒挂"；这里在 store 层把同一
 * 责任规则的**全部分支**钉死：
 *
 *   1. 可见读 → 静默 fresh 接替（成功）：继承的 loading 必须释放，旧读迟到
 *      不得清、不得倒退（监督场景的 store 级镜像）；
 *   2. 可见读 → 静默 fresh 接替（失败）：失败也必须收尾，缓存恢复照常；
 *   3. 旧读**先**收尾（在静默读结束前）：不得清掉继承的 loading；
 *   4. 静默 → 更晚可见读取（写确认 fresh）：loading 保持到后者结束；被取代
 *      的静默读迟到不得清；更早的旧读最后迟到也不得倒退；
 *   5. 登出取消：stopTaskReads 释放（含继承的）loading，迟到响应不得写回；
 *   6. 无前置 loading 的静默刷新：全程不点亮遮罩；
 *   7. 写路径全流程：PUT 恰好一次（不重放）、快照不倒退、loading 收尾。
 *
 * 运行：node tests/task-loading-handover.test.mjs（也兼容
 *       node --experimental-strip-types --import ./tests/alias-register.mjs）
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

/**
 * 传输替身。本轮全部用**精确 URL** 匹配（releaseAt/releaseErrorAt/parkedExact/
 * signalsExact），避免 '/api/tasks' 前缀把 '/api/tasks/SYNTHETIC' 的 PUT 一起
 * 扫走、或把 '/api/tasks/stats' 计入任务读计数——上一轮矩阵在这里踩过坑。
 */
function createTransport() {
  const parked = []
  const counts = new Map()
  const signals = []
  const immediate = new Map()

  function make(method) {
    return (url, _data, config) => new Promise((resolve, reject) => {
      const key = `${method} ${url}`
      counts.set(key, (counts.get(key) || 0) + 1)
      const entry = { method, url, resolve, reject, signal: config?.signal ?? null }
      signals.push(entry)
      const script = immediate.get(url)
      if (script === undefined) { parked.push(entry); return }
      if (script.error) reject(script.error)
      else resolve(script.body)
    })
  }

  function matchingExact(url) { return parked.filter(e => e.url === url) }

  function releaseIndexAt(url, index, how) {
    const list = matchingExact(url)
    const entry = list[index]
    if (!entry) return 0
    parked.splice(parked.indexOf(entry), 1)
    how(entry)
    return 1
  }

  return {
    get: make('GET'),
    post: make('POST'),
    put: make('PUT'),
    delete: make('DELETE'),
    /** 放行某 URL 精确匹配的第 index 条挂起请求（0 基，按发出顺序）。 */
    releaseAt(url, index, body) { return releaseIndexAt(url, index, e => e.resolve(body)) },
    /** 放行某 URL 精确匹配的**最后**一条挂起请求（最新发出的）。 */
    releaseLast(url, body) {
      const list = matchingExact(url)
      if (!list.length) return 0
      return releaseIndexAt(url, list.length - 1, e => e.resolve(body))
    },
    releaseErrorAt(url, index, error) { return releaseIndexAt(url, index, e => e.reject(error)) },
    releaseLastError(url, error) {
      const list = matchingExact(url)
      if (!list.length) return 0
      return releaseIndexAt(url, list.length - 1, e => e.reject(error))
    },
    script(url, body) { immediate.set(url, { body }) },
    parkedExact(url) { return matchingExact(url) },
    count(url) {
      let total = 0
      for (const [key, value] of counts) {
        if (key.endsWith(' ' + url)) total += value
      }
      return total
    },
    signalsExact(url) { return signals.filter(e => e.url === url).map(e => e.signal) },
  }
}

function loadTsModule(relFile, customRequire) {
  const file = path.join(root, relFile)
  const source = fs.readFileSync(file, 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
  } }).outputText
  const module = { exports: {} }
  const fn = vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                                 { filename: file })
  fn(customRequire, module, module.exports)
  return module.exports
}

function buildWorld() {
  const pinia = createPinia()
  setActivePinia(pinia)

  const transport = createTransport()
  const cacheStore = new Map()
  const cache = { get: (k) => cacheStore.get(k), set: (k, v) => cacheStore.set(k, v),
                  remove: (k) => cacheStore.delete(k) }

  const bootModule = loadTsModule('src/boot.ts', (name) => req(name))
  const elStub = { warning: () => {}, error: () => {}, success: () => {} }

  // 2026-09-21（A）：auth/live store 新增 operationEvents 依赖，装载器补上解析。
  const opEventsModule = loadTsModule('src/stores/operationEvents.ts', (name) => req(name))
  const authModule = loadTsModule('src/stores/auth.ts', (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/composables/useCache') return { default: cache }
    if (name === '@/boot') return bootModule
    if (name === '@/stores/operationEvents') return opEventsModule
    if (name === 'element-plus') return { ElMessage: elStub }
    return req(name)
  })
  const tasksModule = loadTsModule('src/stores/tasks.ts', (name) => {
    if (name === '@/stores/auth') return authModule
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/composables/useCache') return { default: cache }
    if (name === '@/boot') return bootModule
    if (name === '@/stores/operationEvents') return opEventsModule
    if (name === 'element-plus') return { ElMessage: elStub }
    return req(name)
  })

  const auth = authModule.useAuthStore()
  const tasks = tasksModule.useTaskStore()
  return { transport, cacheStore, boot: bootModule.boot, auth, tasks }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitParked(transport, url, ms = 1500) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    if (transport.parkedExact(url).length) return
    await sleep(10)
  }
  throw new Error(`等待 ${url} 的挂起请求超时（请求根本没发出）`)
}

const taskRows = (revision, zone) => ({
  tasks: [{ id: 1, zone_name: zone, needs_execution: false }],
  revision, business_date: '2026-09-21',
  total: 1, pending_total: 1, completed: 0, today_done: 0, today_pending: 1,
  remaining_time: 0, avg_remaining: 0, urgency: 0,
})

const tests = []
function test(name, fn) { tests.push([name, fn]) }

/**
 * 监督场景的公共驱动：可见读挂起（loading 亮）→ stats 报新版本 → 静默 fresh 接替。
 * `releaseFresh`：是否在本驱动内放行新读。场景 1/2 要放行；场景 3/4/5 需要新读
 * **保持在途**，由各场景自己按条目放行。
 */
async function driveSilentTakeover(w, { failFresh = false, releaseFresh = true } = {}) {
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()                       // 可见：isLoading=true
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, true, '前置：可见读点亮 loading')
  w.transport.script('/api/tasks/stats', taskRows(11, 'STALE-STATS'))
  const refresh = w.tasks.refreshTasksIfStale()              // 取消旧读 + 静默 fresh
  await waitParked(w.transport, '/api/tasks')                // 新读已发出并挂起
  assert.equal(w.tasks.isLoading, true, '接替期间 loading 保持（继承）')
  assert.equal(w.transport.signalsExact('/api/tasks')[0].aborted,
               true, '旧读必须被真正 abort')
  if (releaseFresh) {
    if (failFresh) {
      w.transport.releaseLastError('/api/tasks',
                                   Object.assign(new Error('boom'), { status: 500 }))
    } else {
      w.transport.releaseLast('/api/tasks', taskRows(11, 'NEW-DATA'))
    }
  }
  return { oldRead, refresh }
}

test('可见→静默接替（成功）：继承的 loading 必须释放；旧读迟到不得清、不得倒退', async () => {
  const w = buildWorld()
  const { oldRead, refresh } = await driveSilentTakeover(w)
  await refresh
  assert.equal(w.tasks.tasksRevision, 11, '新数据必须落地')
  assert.equal(w.tasks.tasks[0]?.zone_name, 'NEW-DATA')
  assert.equal(w.tasks.isLoading, false,
               '静默读继承了可见加载，收尾时必须释放（本轮修复的核心判据）')
  // 旧读的响应此刻才迟到（替身无视 signal）——不得清 loading、不得倒退。
  w.transport.releaseAt('/api/tasks', 0, taskRows(10, 'OLD-DATA'))
  await oldRead
  assert.equal(w.tasks.isLoading, false, '被取代的旧读迟到不得改写加载态')
  assert.equal(w.tasks.tasksRevision, 11, '快照不得倒退')
  assert.equal(w.tasks.tasks[0]?.zone_name, 'NEW-DATA', '快照不得倒退')
})

test('可见→静默接替（失败）：失败也必须收尾，缓存恢复照常', async () => {
  const w = buildWorld()
  w.cacheStore.set('tasks_cache', {
    tasks: [{ id: 42, zone_name: 'CACHED', needs_execution: false }],
    stats: { total: 1, pending_total: 1, completed: 0, today_done: 0,
             today_pending: 1, remaining_time: 0, avg_remaining: 0, urgency: 0 },
  })
  const { oldRead, refresh } = await driveSilentTakeover(w, { failFresh: true })
  await refresh
  assert.equal(w.tasks.isLoading, false, '静默读失败也必须释放继承的 loading')
  assert.equal(w.tasks.tasks[0]?.zone_name, 'CACHED', '失败时缓存恢复必须照常')
  w.transport.releaseAt('/api/tasks', 0, taskRows(10, 'OLD-DATA'))
  await oldRead
  assert.equal(w.tasks.isLoading, false, '旧读迟到不得改写加载态')
})

test('旧读先收尾（静默读结束前）：不得清掉继承的 loading', async () => {
  const w = buildWorld()
  const { refresh } = await driveSilentTakeover(w, { releaseFresh: false })
  // 旧读（第 0 条）先返回——它的 finally 不得清掉静默读继承的 loading。
  w.transport.releaseAt('/api/tasks', 0, taskRows(10, 'OLD-DATA'))
  await sleep(20)
  assert.equal(w.tasks.isLoading, true, '旧读先收尾不得清 loading')
  w.transport.releaseLast('/api/tasks', taskRows(11, 'NEW-DATA'))
  await refresh
  assert.equal(w.tasks.isLoading, false, '静默读结束时释放')
  assert.equal(w.tasks.tasksRevision, 11)
})

test('静默→更晚可见读取（写确认 fresh）：loading 保持到后者结束', async () => {
  const w = buildWorld()
  await driveSilentTakeover(w, { releaseFresh: false })  // 旧读(abort)+静默读(挂起)
  // 更晚的可见读取：写确认后的 fresh 收尾刷新（可见）取消静默读、再开新读。
  const later = w.tasks.fetchTasks({ fresh: true })
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, true, '更晚可见读取期间 loading 保持')
  // 被取代的静默读（第 1 条）此刻迟到——不得清掉更晚可见读的 loading。
  w.transport.releaseAt('/api/tasks', 1, taskRows(11, 'SUPERSEDED'))
  await sleep(20)
  assert.equal(w.tasks.isLoading, true,
               '被取代的静默读迟到不得清更晚可见读取的 loading')
  w.transport.releaseLast('/api/tasks', taskRows(12, 'WRITE-CONFIRMED'))
  await later
  assert.equal(w.tasks.isLoading, false, '更晚可见读取结束才释放')
  assert.equal(w.tasks.tasksRevision, 12)
  // 最早的旧读最后迟到：不得倒退、不得改写加载态。
  w.transport.releaseAt('/api/tasks', 0, taskRows(9, 'ANCIENT'))
  await sleep(20)
  assert.equal(w.tasks.tasksRevision, 12, '快照不得倒退')
  assert.equal(w.tasks.isLoading, false)
})

test('登出取消：释放（含继承的）loading；迟到响应不得写回', async () => {
  const w = buildWorld()
  await driveSilentTakeover(w, { releaseFresh: false })  // 静默读在途，继承 loading
  w.tasks.stopTaskReads()                         // 登出：代际前进 + abort + 收尾
  assert.equal(w.tasks.isLoading, false, '登出必须释放（含继承的）loading')
  // 静默读的响应迟到（替身无视 signal）——不得写回、不得重新点亮。
  w.transport.releaseLast('/api/tasks', taskRows(11, 'LATE'))
  await sleep(20)
  assert.equal(w.tasks.isLoading, false, '迟到响应不得重新点亮 loading')
  assert.deepEqual(w.tasks.tasks, [], '登出后迟到响应不得写回任务列表')
  assert.equal(w.tasks.tasksRevision, -1)
})

test('无前置 loading 的静默刷新：全程不点亮遮罩', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  w.transport.script('/api/tasks/stats', taskRows(5, 'BACK'))
  const refresh = w.tasks.refreshTasksIfStale()   // 无在途读 → 直接静默 fresh
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, false, '静默刷新中途不得点亮 loading')
  w.transport.releaseLast('/api/tasks', taskRows(5, 'BACK'))
  await refresh
  assert.equal(w.tasks.tasksRevision, 5, '数据照常落地')
  assert.equal(w.tasks.isLoading, false, '全程无遮罩')
})

test('写路径全流程：PUT 恰好一次、快照不倒退、loading 正常收尾', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()            // 可见读挂起
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, true)
  const update = w.tasks.updateTask('SYNTHETIC', { total_days: 8 }, 1)
  await waitParked(w.transport, '/api/tasks/SYNTHETIC')
  w.transport.releaseLast('/api/tasks/SYNTHETIC', { success: true })
  // 写确认成功 → fresh 可见刷新取消旧读、开新读（loading 一直保持）。
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, true, '写后刷新期间 loading 保持')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 1, 'PUT 恰好一次')
  // 旧读迟到：不得写回、不得清写后刷新的 loading。
  w.transport.releaseAt('/api/tasks', 0, taskRows(10, 'PRE-WRITE'))
  await oldRead
  assert.equal(w.tasks.isLoading, true, '旧读迟到不得清写后刷新的 loading')
  assert.equal(w.tasks.tasksRevision, -1, '旧读不得写回')
  w.transport.releaseLast('/api/tasks', taskRows(108, 'POST-WRITE'))
  const r = await update
  assert.notEqual(r, undefined, 'updateTask 必须如实返回')
  assert.equal(w.tasks.tasks[0]?.zone_name, 'POST-WRITE', '快照反映写入后的数据')
  assert.equal(w.tasks.tasksRevision, 108)
  assert.equal(w.tasks.isLoading, false, '写后刷新结束释放 loading')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 1, '写入不因刷新失败/取消重放')
})

async function run() {
  let failures = 0
  for (const [name, fn] of tests) {
    try {
      globalThis.localStorage.clear()
      await fn()
      console.log('PASS', name)
    } catch (error) {
      failures += 1
      console.log('FAIL', name, '::', error.message)
    }
  }
  console.log(failures ? `${failures} 项失败` : `全部通过（${tests.length}）`)
  process.exitCode = failures ? 1 : 0
}

run().catch((error) => { console.error(error); process.exitCode = 1 })
