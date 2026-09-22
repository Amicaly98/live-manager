/**
 * task-refresh-freshness.test.mjs — 任务刷新新鲜度矩阵（F1，2026-09-21 第四轮监督）。
 *
 * 真实 tasks/auth/boot store（TS 转译）+ 真实 pinia，只把传输边界换成可编排替身。
 * 对应监督原件 supervision-postwrite-refresh.mjs 的两个场景，并补齐
 * WORKBUDDY-FOLLOWUP.md 的有限验收矩阵：
 *
 *   1. 无写入的普通重复读取仍单飞（只发一份）；
 *   2. 写后刷新·旧读先回：旧 GET 先返回旧值 → 被次序门放行？不——它已被
 *      fresh 取消、序号落后，旧值不得落地；补读返回新值并写回；
 *   3. 写后刷新·旧读晚回：新读先完成并写回，旧 GET 的响应迟到 → 次序门拦下，
 *      最终状态不倒退；
 *   4. 多次写后刷新连续到达：每次失效至多一次补读（有界），最后一次刷新承诺
 *      被满足；PUT 次数等于写入次数（不重放）；
 *   5. 补读失败：不重放写入、不自动无限开新刷新循环、无旧 loading 残留；
 *   6. 登出取消在途补读；迟到响应不写回；新登录后读取恢复；
 *   7. 版本提示（refreshTasksIfStale）路径同样遵守新鲜度（旧读晚回变体）。
 *
 * 运行：node tests/task-refresh-freshness.test.mjs
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
    /** 放行全部挂起的、URL 前缀匹配的请求。 */
    release(urlPrefix, body) {
      let n = 0
      for (const entry of parked.splice(0)) {
        if (entry.url.startsWith(urlPrefix)) { entry.resolve(body); n += 1 }
        else parked.push(entry)
      }
      return n
    },
    /** 只放行最早挂起的那一条。 */
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
    /** 按挂起顺序放行第 index 条（0 起）——用于"旧读晚回"的精确次序控制。 */
    releaseAt(urlPrefix, index, body) {
      const matching = []
      parked.forEach((entry, i) => {
        if (entry.url.startsWith(urlPrefix)) matching.push(i)
      })
      const target = matching[index]
      if (target === undefined) return 0
      const [entry] = parked.splice(target, 1)
      entry.resolve(body)
      return 1
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

/** 等待某 URL 前缀的**GET**请求数达到 n（PUT/POST 的 URL 也带该前缀，必须排除）。 */
async function waitRequests(transport, urlPrefix, n, ms = 1500) {
  const deadline = Date.now() + ms
  const getSeen = () => transport
    .signalsFor(urlPrefix)
    .filter((s) => s !== null).length   // GET 带 signal；写请求的 signal 为 null
  while (Date.now() < deadline) {
    if (getSeen() >= n) return
    await sleep(10)
  }
  throw new Error(`等待 ${urlPrefix} 的第 ${n} 个 GET 超时（当前 ${getSeen()}）`)
}

const response = (revision, totalDays) => ({
  tasks: [{ id: 1, zone_name: 'SYNTHETIC', total_days: totalDays, needs_execution: false }],
  revision, business_date: '2026-09-21',
  total: 1, pending_total: 1, completed: 0, today_done: 0, today_pending: 1,
  remaining_time: 0, avg_remaining: 0, urgency: 0,
})

/**
 * 写入路径的标准驱动：发 updateTask → 等 PUT 到达受控边界 → 放行成功 →
 * 等 fresh 补读发出。
 *
 * **必须 await**：PUT 的放行与补读的发出都要在本函数内完成，否则调用方
 * 随后的 release/releaseAt 会与这里竞态（'/api/tasks' 前缀连 PUT 一起扫走）。
 * 返回 `{ update }`——updateTask 的最终 promise（含刷新完成），供测试按
 * 自己选择的次序放行各读响应。
 */
async function commitUpdate(w, totalDays, marker) {
  const update = w.tasks.updateTask('SYNTHETIC', { total_days: totalDays }, 1)
    .then((v) => ({ ok: v }))
  await waitParked(w.transport, '/api/tasks/SYNTHETIC')
  const released = w.transport.release('/api/tasks/SYNTHETIC', { success: true })
  assert.equal(released, 1, `PUT 必须真的到达受控边界并成功（${JSON.stringify(marker)}）`)
  await waitRequests(w.transport, '/api/tasks', marker.readsAfter)
  return { update }
}

const tests = []
function test(name, fn) { tests.push([name, fn]) }

test('无写入的普通重复读取仍单飞（只发一份）', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const a = w.tasks.fetchTasks()
  const b = w.tasks.fetchTasks()
  const c = w.tasks.fetchTasks({ silent: true })
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.transport.count('/api/tasks'), 1, '普通重复读取必须合并')
  w.transport.release('/api/tasks', response(10, 2))
  await Promise.all([a, b, c])
  assert.equal(w.tasks.tasksRevision, 10)
})

test('写后刷新·旧读先回：旧值不得落地，新读写回提交后的数据', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')            // 旧 GET 挂起（版本 10 之前发出）
  const { update } = await commitUpdate(w, 8, { readsAfter: 2 })   // PUT 成功 → fresh 取消旧读、补新读
  // 旧读先回：携带旧值（版本 10 / 2 天）
  w.transport.releaseOne('/api/tasks', response(10, 2))
  await oldRead
  assert.equal(w.tasks.tasksRevision, -1, '被取消的旧读响应不得落地（次序门）')
  // 补读返回提交后的数据
  w.transport.release('/api/tasks', response(11, 8))
  const result = await update
  assert.equal(result.ok, true, '写入确认成功，updateTask 如实返回')
  assert.equal(w.tasks.tasks[0]?.total_days, 8, '界面必须是提交后的值')
  assert.equal(w.tasks.tasksRevision, 11)
  assert.equal(w.tasks.businessDate, '2026-09-21')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 1, 'PUT 只发一次，不重放')
  assert.equal(w.transport.count('/api/tasks'), 2, '有界：旧读 + 一次补读')
  assert.equal(w.tasks.isLoading, false, '无 loading 残留')
})

test('写后刷新·旧读晚回：新读先写回，旧读响应迟到不得倒退', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  const { update } = await commitUpdate(w, 8, { readsAfter: 2 })   // fresh 已取消旧读、补新读
  // 新读**先**完成并写回（挂起队列里旧读在前，用 releaseAt 精确放行第二条）
  w.transport.releaseAt('/api/tasks', 1, response(11, 8))
  const result = await update
  assert.equal(w.tasks.tasksRevision, 11)
  assert.equal(w.tasks.tasks[0]?.total_days, 8)
  // 旧读响应此刻才迟到到达
  w.transport.releaseAt('/api/tasks', 0, response(10, 2))
  await oldRead
  await sleep(30)
  assert.equal(w.tasks.tasksRevision, 11, '迟到的旧响应不得倒退 revision')
  assert.equal(w.tasks.tasks[0]?.total_days, 8, '迟到的旧响应不得倒退列表')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 1, 'PUT 不重放')
})

test('多次写后刷新连续到达：每次失效至多一次补读，最后一次承诺被满足', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')            // 旧读 A（#1）
  const { update: u1 } = await commitUpdate(w, 8, { readsAfter: 2 })   // 写1 → fresh 取消 A，补读 B（#2）
  const { update: u2 } = await commitUpdate(w, 12, { readsAfter: 3 })  // 写2 → fresh 取消 B，补读 C（#3）
  // 最后一次刷新的读返回最新数据
  w.transport.releaseAt('/api/tasks', 2, response(12, 12))
  const r2 = await u2
  assert.equal(r2.ok, true)
  assert.equal(w.tasks.tasksRevision, 12, '最后一次刷新承诺必须被满足')
  assert.equal(w.tasks.tasks[0]?.total_days, 12)
  // 被取消的两次读迟到返回：都不得倒退
  w.transport.releaseAt('/api/tasks', 1, response(11, 8))
  w.transport.releaseAt('/api/tasks', 0, response(10, 2))
  const r1 = await u1
  assert.equal(r1.ok, true)
  await sleep(30)
  assert.equal(w.tasks.tasksRevision, 12, '迟到的中间读不得倒退')
  assert.equal(w.tasks.tasks[0]?.total_days, 12)
  assert.equal(w.transport.count('/api/tasks'), 3, '有界：每次失效至多一次补读')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 2, '两次写各发一次 PUT')
})

test('补读失败：不重放写入、不自动开新刷新循环、无旧 loading', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  const { update } = await commitUpdate(w, 8, { readsAfter: 2 })
  // 补读失败
  w.transport.releaseError('/api/tasks', Object.assign(new Error('boom'), { status: 500 }))
  const result = await update
  assert.equal(result.ok, true, '写入已成功：刷新失败不得改写写入结果')
  assert.equal(w.transport.count('/api/tasks/SYNTHETIC'), 1, '刷新失败不得重放写入')
  assert.equal(w.transport.count('/api/tasks'), 2, '失败后不得自动开新刷新循环')
  await sleep(200)
  assert.equal(w.transport.count('/api/tasks'), 2, '等待窗口内无额外轮询')
  assert.equal(w.tasks.isLoading, false, '无 loading 残留')
  void oldRead
})

test('登出取消在途补读；迟到响应不写回；新登录后读取恢复', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  const { update } = await commitUpdate(w, 8, { readsAfter: 2 })   // fresh 取消旧读，补读 B 挂起
  w.tasks.stopTaskReads()                                 // 登出：取消在途补读
  const getSignals = w.transport.signalsFor('/api/tasks').filter((s) => s !== null)
  assert.equal(getSignals.length, 2)                      // GET 两次（旧读 + 补读）
  assert.ok(getSignals[1].aborted, '补读被真正 abort')
  w.transport.release('/api/tasks', response(11, 8))      // 补读响应迟到
  await oldRead
  const result = await update
  assert.equal(result.ok, true, '写入结果与刷新取消分清：写入已确认成功')
  assert.equal(w.tasks.tasksRevision, -1, '登出后旧代响应不得写回')
  await sleep(150)
  assert.equal(w.transport.count('/api/tasks'), 2, '登出后不得重新发出补读')
  assert.equal(w.tasks.isLoading, false)
  // 新登录恢复
  w.auth.isLoggedIn = true
  const fresh = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')
  assert.equal(w.transport.count('/api/tasks'), 3, '新登录后的新读取可用')
  w.transport.release('/api/tasks', response(11, 8))
  await fresh
  assert.equal(w.tasks.tasksRevision, 11)
})

test('版本提示路径（refreshTasksIfStale）：旧读晚回同样不倒退', async () => {
  const w = buildWorld()
  w.auth.isLoggedIn = true
  const oldRead = w.tasks.fetchTasks()
  await waitParked(w.transport, '/api/tasks')             // 旧读挂起
  const refresh = w.tasks.refreshTasksIfStale()
  await waitParked(w.transport, '/api/tasks/stats')
  assert.equal(w.transport.release('/api/tasks/stats', response(11, 8)), 1,
               'stats 必须真的到达受控边界')
  await waitRequests(w.transport, '/api/tasks', 2)        // 版本提示 → 补读已发出
  w.transport.releaseAt('/api/tasks', 1, response(11, 8)) // 补读先回
  w.transport.releaseAt('/api/tasks', 0, response(10, 2)) // 旧读晚回
  await oldRead
  const changed = await refresh
  assert.equal(changed, true, '版本提示后的刷新确实执行了新读')
  assert.equal(w.tasks.tasksRevision, 11, '发布的是提示之后的版本')
  assert.equal(w.tasks.tasks[0]?.total_days, 8)
  await sleep(30)
  assert.equal(w.tasks.tasksRevision, 11, '迟到的旧读不得倒退')
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
