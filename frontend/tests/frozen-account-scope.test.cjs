/**
 * M0A A 包（R1）：**冻结的账号上下文**必须进入任务写请求。
 *
 * 任务数据属于 UID：同一个 HTTP 入口先后服务 A、B 两号时，一次"用户已经确认过"
 * 的写操作只能属于**冻结它的那个账号**。界面在用户点下操作、第一个确认 UI 之前
 * 取一次账号上下文（`freezeWriteScope`），此后：
 *
 * - 请求（与它的传输层重试）带同一份 UID 的 `X-Account-UID`；
 * - 回包回到页面时再核一次，旧账号的结果不刷新、不覆写新账号的展示与缓存。
 *
 * 全部用仓库真实源码：真实 `stores/tasks.ts` + 真实 `api/request.ts` + 真实
 * Axios（只替换网络边界）。负向对照会复制 src 到临时目录并注入"此刻再取一次
 * 当前账号"的旧行为，用 `OUTER_TEST_SRC` 指回那份副本。
 */
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const assert = require('node:assert/strict')
const { createRequire } = require('node:module')

const root = process.env.OUTER_TEST_SRC || path.resolve(__dirname, '..')
const req = createRequire(path.join(__dirname, '..', 'package.json'))
const ts = req('typescript')
const axios = req('axios')
const { createPinia, setActivePinia } = req('pinia')

globalThis.localStorage = {
  getItem: () => null, setItem: () => {}, removeItem: () => {},
}

const ACCOUNT_UID_HEADER = 'X-Account-UID'

function load(relative, overrides = {}) {
  const file = path.join(root, 'src', relative)
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    },
  }).outputText
  const module = { exports: {} }
  const requireLocal = (name) => {
    if (name in overrides) return overrides[name]
    if (name === 'element-plus') {
      return {
        ElMessage: { warning() {}, error() {}, success() {}, info() {} },
      }
    }
    if (name === '@/router') return { default: { currentRoute: { value: {} }, push() {} } }
    if (name.startsWith('@/')) return load(name.slice(2) + '.ts', overrides)
    if (name.startsWith('.')) {
      return load(path.relative(path.join(root, 'src'),
                                path.resolve(path.dirname(file), name)) + '.ts',
                  overrides)
    }
    return req(name)
  }
  vm.runInThisContext(`(function(require,module,exports,setTimeout){${code}\n})`,
                      { filename: file })(
    requireLocal, module, module.exports,
    (fn, ms, ...args) => setTimeout(fn, Math.min(ms, 1), ...args))
  return module.exports
}

function headerOf(config, name) {
  const headers = config.headers
  if (!headers) return undefined
  if (typeof headers.get === 'function') return headers.get(name)
  return headers[name]
}

/** 只替换网络边界：记录方法 / URL / 请求头 / 载荷。 */
function installAdapter(handler) {
  const calls = []
  const original = axios.defaults.adapter
  axios.defaults.adapter = (config) => {
    const record = {
      url: config.url,
      method: String(config.method || 'get').toLowerCase(),
      headers: {
        [ACCOUNT_UID_HEADER]: headerOf(config, ACCOUNT_UID_HEADER),
      },
      body: config.data,
    }
    calls.push(record)
    return handler(config, record)
  }
  return {
    calls,
    restore() { axios.defaults.adapter = original },
  }
}

function ok(config, data) {
  return Promise.resolve({ status: 200, data, headers: {}, config })
}

function taskList(revision, businessDate) {
  return {
    tasks: [{
      id: 11, priority: 1, zone_name: '学习区', category: 2, total_days: 5,
      days_done: 0, deadline_raw: '=DATE(2027,12,31)', today_done: null,
      remaining_days: 1, needs_execution: true,
    }],
    revision, business_date: businessDate,
    total: 1, pending_total: 1, completed: 0, today_done: 0, today_pending: 1,
    remaining_time: 10, avg_remaining: 5, urgency: 1,
  }
}

/** 缓存只影响"请求失败时的兜底展示"，与本用例无关：用最薄替身。 */
const CACHE_STUB = { default: { get: () => null, set: () => {}, remove: () => {} } }

/** 真实 store ＋ 可控的 auth 身份（账号切换就是改它）。 */
function freshStores() {
  setActivePinia(createPinia())
  const request = load('api/request.ts', {})
  const opEventsModule = load('stores/operationEvents.ts', {})
  const authModule = load('stores/auth.ts', {
    '@/api/request': request,
    '@/composables/useCache': CACHE_STUB,
    '@/boot': load('boot.ts', {}),
    '@/stores/operationEvents': opEventsModule,
  })
  const { useTaskStore } = load('stores/tasks.ts', {
    '@/api/request': request,
    '@/composables/useCache': CACHE_STUB,
    '@/stores/auth': authModule,
  })
  const auth = authModule.useAuthStore()
  auth.isLoggedIn = true
  auth.userInfo = { uid: 7001, uname: '甲' }
  return { store: useTaskStore(), auth }
}

const FORM = { zone_name: '学习区', category: 2, total_days: 5, days_done: 1 }

const results = []
async function test(name, body) {
  try {
    await body()
    results.push({ name, status: 'PASS' })
  } catch (error) {
    results.push({ name, status: 'FAIL', message: error.message })
  }
}

const suppressed = []
const originalError = console.error
console.error = (...args) => { suppressed.push(args) }

async function main() {
  // 载入的是哪一棵源码树：负向对照据此核对"红来自被注入的那份副本"。
  console.log(`source-root=${root}`)

  await test('删除：请求带的是**冻结那一刻**的账号，不是确认时的当前账号', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, revision: 8 })))
    try {
      const { store, auth } = freshStores()
      await store.fetchTasks()
      const scope = store.freezeWriteScope()
      assert.equal(scope.uid, 7001)
      // 确认框还开着的时候，别的标签页把账号换成了乙。
      auth.userInfo = { uid: 7002, uname: '乙' }
      await store.deleteTask('学习区', 11, scope)
      const del = adapter.calls.find((c) => c.method === 'delete')
      assert.ok(del, '必须发出删除请求')
      assert.equal(del.headers[ACCOUNT_UID_HEADER], '7001',
                   '旧确认框的请求必须带冻结时的账号，绝不能补成此刻的账号')
    } finally { adapter.restore() }
  })

  await test('换号之后：旧账号的回包不刷新新账号的页面', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, revision: 8 })))
    try {
      const { store, auth } = freshStores()
      await store.fetchTasks()
      const scope = store.freezeWriteScope()
      auth.userInfo = { uid: 7002, uname: '乙' }
      const before = adapter.calls.filter((c) => c.method === 'get').length
      await store.deleteTask('学习区', 11, scope)
      const after = adapter.calls.filter((c) => c.method === 'get').length
      assert.equal(after, before,
                   '账号已换：旧账号的写回包不得触发一次针对新账号的读取')
    } finally { adapter.restore() }
  })

  await test('同一账号（正面守卫）：写入确认成功后照旧刷新', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, revision: 8 })))
    try {
      const { store } = freshStores()
      await store.fetchTasks()
      const scope = store.freezeWriteScope()
      const before = adapter.calls.filter((c) => c.method === 'get').length
      await store.deleteTask('学习区', 11, scope)
      const after = adapter.calls.filter((c) => c.method === 'get').length
      assert.ok(after > before, 'F1：写入确认成功后必须重取列表')
    } finally { adapter.restore() }
  })

  await test('五种写口都带冻结的账号归属', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, revision: 8 })))
    try {
      const { store } = freshStores()
      await store.fetchTasks()
      const scope = store.freezeWriteScope()
      const mark = (method, url) => adapter.calls.some(
        (c) => c.method === method && c.url.includes(url))

      await store.updateTask('学习区', { today_done: 1 }, 11, scope)
      await store.markTaskDone('学习区', 11, undefined, scope)
      await store.markTaskAllDone('学习区', 11, scope)
      await store.reloadTasks(scope)
      // M1：新建入口改成"冻结预检 + 一次性令牌"的不可变命令；账号归属仍是
      // 同一份冻结上下文，由调用方在**弹框之前**取好并原样交给 store。
      await store.createNewTask(FORM,
                                { task_id: 11, revision: 7,
                                  business_day: '2026-09-19',
                                  existing_is_unfinished: true },
                                'frozen-account-scope-token', scope)

      const writes = adapter.calls.filter((c) => c.method !== 'get')
      assert.ok(writes.length >= 5, `写请求数量不足：${writes.length}`)
      for (const call of writes) {
        assert.equal(call.headers[ACCOUNT_UID_HEADER], '7001',
                     `${call.method} ${call.url} 必须带冻结的账号归属`)
      }
      assert.ok(mark('put', '/api/tasks/'))
      assert.ok(mark('post', '/api/tasks/mark-done/'))
      assert.ok(mark('post', '/api/tasks/mark-all-done/'))
      assert.ok(mark('post', '/api/tasks/reload'))
      assert.ok(mark('post', '/api/tasks'))
    } finally { adapter.restore() }
  })

  await test('导入也带冻结的账号归属（FormData 同一条接缝）', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, imported: 1 })))
    try {
      const { store } = freshStores()
      await store.fetchTasks()
      const scope = store.freezeWriteScope()
      await store.importFromExcel({ size: 1 }, false, 'merge', scope)
      const post = adapter.calls.find(
        (c) => c.method === 'post' && c.url.includes('/api/tasks/import'))
      assert.ok(post, '必须发出导入请求')
      assert.equal(post.headers[ACCOUNT_UID_HEADER], '7001')
    } finally { adapter.restore() }
  })

  console.error = originalError
  if (suppressed.length) {
    console.log(`（预期内的失败日志 ${suppressed.length} 条，已收敛）`)
  }
  console.log(JSON.stringify(results, null, 2))
  const failed = results.filter((r) => r.status !== 'PASS')
  console.log(`frozen-account-scope: ${results.length - failed.length}/${results.length} passed`)
  return failed.length === 0 ? 0 : 1
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(error)
  process.exit(2)
})
