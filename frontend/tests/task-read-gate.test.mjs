/**
 * task-read-gate.test.mjs — 任务只读请求的取消与写回门控（S3，2026-09-21）。
 *
 * 真实 tasks store（TS 转译）+ 真实 pinia + 真实 auth store + 真实 boot，
 * 只把传输边界换成可编排替身。与监督原件 supervision-task-read.mjs 互补：
 * 浏览器原件证明"真实 dist 上登出会取消在途读"，这里证明**即便底层取消
 * 没有及时生效（响应照样回来），旧代响应也不得写回任何任务状态**——
 * 这条在真实 abort 生效的浏览器里恰好观察不到（socket 已断）。
 *
 * 覆盖：
 *   1. 旧读取在 stop 后返回（替身故意无视 signal）→ tasks/stats/revision/
 *      businessDate 一律不写回，isLoading 不回退；
 *   2. stopTaskReads 真正 abort 在途读（signal.aborted 为真）；
 *   3. 未登录（退出意图期间）不发任务读——挡住"写请求收尾的附带读取"；
 *   4. 新登录后的新读取可用，且旧读取的 finally 不清掉新读取的加载态；
 *   5. 同一代重复调用合并为一次请求（重复转换不积累读取）；
 *   6. 控制组：当前代读取正常应用；当前代失败恢复缓存也正常（门控不是
 *      无差别吞掉一切）。
 *
 * 运行：node --experimental-strip-types --import ./tests/alias-register.mjs
 *       tests/task-read-gate.test.mjs
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

  return {
    get: make('GET'),
    post: make('POST'),
    put: make('PUT'),
    delete: make('DELETE'),
    release(urlPrefix, body) {
      let n = 0
      for (const entry of parked.splice(0)) {
        if (entry.url.startsWith(urlPrefix)) { entry.resolve(body); n += 1 }
        else parked.push(entry)
      }
      return n
    },
    /** 只放行**最早**挂起的那一条（用于新旧读取同时在途的交错场景）。 */
    releaseOne(urlPrefix, body) {
      for (let i = 0; i < parked.length; i += 1) {
        if (parked[i].url.startsWith(urlPrefix)) {
          const [entry] = parked.splice(i, 1)
          entry.resolve(body)
          return 1
        }
      }
      return 0
    },
    releaseError(urlPrefix, error) {
      let n = 0
      for (const entry of parked.splice(0)) {
        if (entry.url.startsWith(urlPrefix)) { entry.reject(error); n += 1 }
        else parked.push(entry)
      }
      return n
    },
    script(url, body) { immediate.set(url, { body }) },
    parkedFor(urlPrefix) { return parked.filter(e => e.url.startsWith(urlPrefix)) },
    count(url) {
      let total = 0
      for (const [key, value] of counts) {
        if (key.endsWith(' ' + url)) total += value
      }
      return total
    },
    signalsFor(urlPrefix) { return signals.filter(e => e.url.startsWith(urlPrefix)).map(e => e.signal) },
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

async function waitParked(transport, urlPrefix, ms = 1500) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    if (transport.parkedFor(urlPrefix).length) return
    await sleep(10)
  }
  throw new Error(`等待 ${urlPrefix} 的挂起请求超时（请求根本没发出）`)
}

const taskRows = (id, zone) => ({
  tasks: [{ id, zone_name: zone, needs_execution: false }],
  revision: 100 + id, business_date: '2026-09-21',
  total: 1, pending_total: 1, completed: 0, today_done: 0, today_pending: 1,
  remaining_time: 0, avg_remaining: 0, urgency: 0,
})

const tests = []
function test(name, fn) { tests.push([name, fn]) }

test('旧读取在 stop 后返回（底层取消没生效）不得写回任何任务状态', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const staleRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  w.tasks.stopTaskReads()                       // 登出：代际前进 + abort
  const signals = w.transport.signalsFor('/api/tasks')
  assert.ok(signals.length && signals[signals.length - 1].aborted,
            'stop 必须真正 abort 在途读')
  // 替身故意无视 signal，把响应照样送回来（模拟"取消没有及时生效"）
  w.transport.release('/api/tasks', taskRows(987, 'STALE'))
  await staleRead
  assert.deepEqual(w.tasks.tasks, [], '旧代响应不得写回任务列表')
  assert.equal(w.tasks.tasksRevision, -1, '旧代响应不得写回 revision')
  assert.equal(w.tasks.businessDate, '', '旧代响应不得写回 businessDate')
  assert.equal(w.tasks.stats.total, 0, '旧代响应不得写回 stats')
  assert.equal(w.tasks.isLoading, false, '停止后加载态必须干净')
})

test('未登录（退出意图期间）不发任务读——挡住收尾附带读取与定时器残留', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = false
  await w.tasks.fetchTasks()
  await w.tasks.refreshTasksIfStale()
  await sleep(30)
  assert.equal(w.transport.count('/api/tasks'), 0,
               '认证意图不允许时不得发出任务读')
  assert.equal(w.transport.count('/api/tasks/stats'), 0)
})

test('新登录后的新读取可用；旧读取的 finally 不清掉新读取的加载态', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  // 第一代读取挂起
  const oldRead = w.tasks.fetchTasks()          // 非静默：isLoading=true
  await waitParked(w.transport, '/api/tasks')
  w.tasks.stopTaskReads()                       // 认证意图切换（登出→再登录）
  w.auth.isLoggedIn = true                      // 新登录成立
  // 新一代读取（也挂起）
  const newRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.tasks.isLoading, true, '新读取拥有加载态')
  // 旧读取此刻才返回（非静默）：它的 finally 不得清掉新读取的加载态。
  // 用 releaseOne 只放行**旧**那一条——两条挂起读并存时不能一起放。
  w.transport.releaseOne('/api/tasks', taskRows(987, 'STALE'))
  await oldRead
  assert.equal(w.tasks.isLoading, true,
               '旧读取的收尾不得清掉新读取的加载态')
  // 新读取返回：正常应用
  w.transport.releaseOne('/api/tasks', taskRows(5, 'NEW'))
  await newRead
  assert.equal(w.tasks.tasks[0]?.id, 5, '新登录后的新读取必须可用')
  assert.equal(w.tasks.isLoading, false, '新读取完成后加载态正常解除')
  assert.equal(w.tasks.tasksRevision, 105)
})

test('同一代重复调用合并为一次请求（重复转换不积累读取）', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const a = w.tasks.fetchTasks()
  const b = w.tasks.fetchTasks()
  const c = w.tasks.fetchTasks({ silent: true })
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.transport.count('/api/tasks'), 1, '同一代在途读取必须合并')
  w.transport.release('/api/tasks', taskRows(1, 'MERGED'))
  await Promise.all([a, b, c])
  assert.equal(w.tasks.tasks[0]?.zone_name, 'MERGED')
})

test('控制组：当前代失败恢复缓存正常工作（门控不是无差别吞掉）', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  w.cacheStore.set('tasks_cache', {
    tasks: [{ id: 42, zone_name: 'CACHED', needs_execution: false }],
    stats: { total: 1, pending_total: 1, completed: 0, today_done: 0,
             today_pending: 1, remaining_time: 0, avg_remaining: 0, urgency: 0 },
  })
  const failing = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  w.transport.releaseError('/api/tasks', Object.assign(new Error('boom'), { status: 500 }))
  await failing
  assert.equal(w.tasks.tasks[0]?.id, 42, '当前代失败时缓存恢复必须照常')
  // 同代成功读取照常应用
  const ok = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  w.transport.release('/api/tasks', taskRows(7, 'FRESH'))
  await ok
  assert.equal(w.tasks.tasks[0]?.zone_name, 'FRESH')
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
