/**
 * S1 前端贯通：确认时冻结的前置条件（id + 版本 + 业务日）必须原样进入覆盖载荷，
 * 且传输层重试**不得**刷新它们。
 *
 * 全部用仓库真实源码：真实 stores/tasks.ts + 真实 api/request.ts + 真实 Axios
 * 适配器（只替换网络边界）。`freezeOverwriteTarget` 就是界面在**弹框之前**调用
 * 的那一个函数（见 TaskManager.vue:doCreateTask），因此这条用例覆盖的是同一条
 * 路径：确认时冻结 → store → 请求载荷。
 */
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const assert = require('node:assert/strict')
const { createRequire } = require('node:module')

//: 负向对照会把 src 复制到临时目录并注入"旧行为"补丁，再用 OUTER_TEST_SRC
//: 指回那份副本；正常跑法不设该变量，用的就是仓库里的源码。
const root = process.env.OUTER_TEST_SRC || path.resolve(__dirname, '..')
const req = createRequire(path.join(__dirname, '..', 'package.json'))
const ts = req('typescript')
const axios = req('axios')
const { createPinia, setActivePinia } = req('pinia')

globalThis.localStorage = {
  getItem: () => null, setItem: () => {}, removeItem: () => {},
}
globalThis.window = globalThis.window || {}

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

/** 只替换网络边界：记录每次调用的方法/URL/载荷。 */
function installAdapter(handler) {
  const calls = []
  const original = axios.defaults.adapter
  axios.defaults.adapter = (config) => {
    calls.push({
      url: config.url,
      method: String(config.method || 'get').toLowerCase(),
      body: parseBody(config.data),
    })
    return handler(config, calls[calls.length - 1])
  }
  return {
    calls,
    restore() { axios.defaults.adapter = original },
  }
}

function parseBody(data) {
  if (typeof data === 'string') {
    try { return JSON.parse(data) } catch { return data }
  }
  return data
}

function ok(config, data) {
  return Promise.resolve({ status: 200, data, headers: {}, config })
}

function networkError(config) {
  return Promise.reject(new axios.AxiosError('synthetic offline', 'ERR_NETWORK', config))
}

function conflict(config, detail) {
  return Promise.reject(new axios.AxiosError(
    'synthetic 409', 'ERR_BAD_REQUEST', config, null,
    { status: 409, data: { detail }, headers: {}, config }))
}

/** 真实 store：真实 request.ts（含重试/票据）+ 真实 Axios 适配器。 */
function freshTaskStore() {
  setActivePinia(createPinia())
  const request = load('api/request.ts', {})
  // 2026-09-21（S3）：tasks store 新增对 auth store 的依赖（认证意图门控）；
  // 加载器补上解析，auth 与 tasks 共用同一个 request 实例。
  const opEventsModule = load('stores/operationEvents.ts', {})
  const authModule = load('stores/auth.ts', {
    '@/api/request': request,
    '@/composables/useCache': { default: { get: () => null, set: () => {} } },
    '@/boot': load('boot.ts', {}),
    '@/stores/operationEvents': opEventsModule,
  })
  const { useTaskStore } = load('stores/tasks.ts', {
    '@/api/request': request,
    // 缓存只影响"请求失败时的兜底展示"，与本用例无关；用最薄替身，
    // 避免测试依赖 localStorage 的序列化行为。
    '@/composables/useCache': { default: { get: () => null, set: () => {} } },
    '@/stores/auth': authModule,
  })
  const store = useTaskStore()
  // 2026-09-21（S3）：任务只读请求以认证意图为前置（生产里 App 只在启动结论
  // loggedIn=true 时才发起任务读）。本用例测的是写路径的前置条件，因此先把
  // 认证状态置为已登录——这是生产调用这些读取时**必然已满足**的前提。
  authModule.useAuthStore().isLoggedIn = true
  return store
}

/** 服务端列表快照：id=11 的「学习区」，版本 7、业务日 2026-09-19。 */
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
  await test('确认时冻结的 id/版本/业务日原样进入覆盖载荷', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : ok(config, { success: true, revision: 8 })))
    try {
      const store = freshTaskStore()
      await store.fetchTasks()
      const frozen = store.freezeOverwriteTarget('学习区')
      assert.deepEqual(frozen,
                       { id: 11, expectedRevision: 7, businessDate: '2026-09-19' })
      assert.equal(store.freezeOverwriteTarget('不存在的分区'), null,
                   '没有这条记录时必须返回 null，让调用方走新建/提示刷新')

      // 冻结之后界面上的版本与业务日又前进了（后台结算 / 跨日刷新）
      store.tasksRevision = 42
      store.businessDate = '2026-09-20'
      await store.createTask(FORM, true, frozen)

      const post = adapter.calls.find((c) => c.method === 'post')
      assert.ok(post, '必须发出覆盖请求')
      assert.match(post.url, /overwrite=true/)
      assert.equal(post.body.id, 11)
      assert.equal(post.body.expected_revision, 7, '必须带确认时的版本')
      assert.equal(post.body.business_date, '2026-09-19', '必须带确认时的业务日')
    } finally {
      adapter.restore()
    }
  })

  await test('传输层重试重放同一份载荷：前置条件不被刷新', async () => {
    let posts = 0
    const adapter = installAdapter((config) => {
      if (config.method === 'get') return ok(config, taskList(7, '2026-09-19'))
      posts += 1
      return posts === 1 ? networkError(config)   // 首次响应丢失
                         : ok(config, { success: true, revision: 12 })
    })
    try {
      const store = freshTaskStore()
      await store.fetchTasks()
      const frozen = store.freezeOverwriteTarget('学习区')
      store.tasksRevision = 99          // 断线期间后台推进了版本
      store.businessDate = '2026-09-20'
      await store.createTask(FORM, true, frozen)

      const bodies = adapter.calls.filter((c) => c.method === 'post').map((c) => c.body)
      assert.equal(bodies.length, 2, '第一次传输失败后必须重试一次')
      assert.deepEqual(bodies[1], bodies[0], '重试必须重放同一份载荷')
      assert.equal(bodies[1].expected_revision, 7, '重试不得刷新到最新版本')
      assert.equal(bodies[1].business_date, '2026-09-19')
    } finally {
      adapter.restore()
    }
  })

  await test('服务端报告确认过期：明确失败，不拿新版本静默重发', async () => {
    const adapter = installAdapter((config) => (
      config.method === 'get' ? ok(config, taskList(7, '2026-09-19'))
                              : conflict(config, '「学习区」在确认之后已被改动'
                                                 + '…（overwrite_stale_revision）')))
    try {
      const store = freshTaskStore()
      await store.fetchTasks()
      const frozen = store.freezeOverwriteTarget('学习区')
      let detail = ''
      await assert.rejects(
        () => store.createTask(FORM, true, frozen),
        (error) => { detail = error?.response?.data?.detail || ''; return true })
      assert.match(detail, /overwrite_stale_revision/)
      const posts = adapter.calls.filter((c) => c.method === 'post')
      assert.equal(posts.length, 1, '409 是明确响应：不得自动重发')
      assert.equal(posts[0].body.expected_revision, 7)
      assert.equal(store.tasksRevision, 7, '冲突后不得悄悄把本地版本改成最新值')
    } finally {
      adapter.restore()
    }
  })

  await test('没有冻结目标时不发出无保护的覆盖请求', async () => {
    const adapter = installAdapter((config) => ok(config, taskList(7, '2026-09-19')))
    try {
      const store = freshTaskStore()
      await store.fetchTasks()
      await assert.rejects(() => store.createTask(FORM, true))
      assert.equal(adapter.calls.filter((c) => c.method === 'post').length, 0,
                   '缺少前置条件时必须在本地就拦住，而不是发一次"无保护覆盖"')
    } finally {
      adapter.restore()
    }
  })

  await test('界面在弹框之前冻结目标，并把冻结值交给 store', async () => {
    const source = fs.readFileSync(
      path.join(root, 'src/views/TaskManager.vue'), 'utf8')
    const start = source.indexOf('async function doCreateTask')
    assert.ok(start >= 0, '必须存在 doCreateTask')
    const body = source.slice(start, source.indexOf('async function confirmDelete'))
    const frozenAt = body.indexOf('freezeOverwriteTarget')
    const dialogAt = body.indexOf('await confirmOverwrite')
    assert.ok(frozenAt >= 0, '必须在确认之前冻结目标')
    assert.ok(dialogAt >= 0)
    assert.ok(frozenAt < dialogAt,
              '冻结必须发生在弹框之前：弹框期间列表随时可能刷新')
    assert.match(body, /taskStore\.createTask\(data, overwrite, target\)/)
  })

  console.error = originalError
  if (suppressed.length) {
    console.log(`（预期内的失败日志 ${suppressed.length} 条，已收敛）`)
  }
  console.log(JSON.stringify(results, null, 2))
  const failed = results.filter((r) => r.status === 'FAIL')
  console.log(`overwrite-precondition: ${results.length - failed.length}/${results.length} passed`)
  process.exit(failed.length ? 1 : 0)
}

main()
