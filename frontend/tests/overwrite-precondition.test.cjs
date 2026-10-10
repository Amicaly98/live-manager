/**
 * M1 前端贯通（桌面移植阶段 2 重写）：确认时冻结的**创建预检**必须原样进入创建
 * 载荷，且传输层重试**不得**刷新它们。
 *
 * 旧版断言的是旧覆盖协议（`overwrite=true` + `id/expected_revision/business_date`，
 * 界面入口 `doCreateTask`）。桌面移植后创建一律走
 * "一致预检 → 冻结预检 → 唯一确认框 → 一份不可变命令（含客户端令牌）"：
 * `POST /api/tasks` 带 `preview` + `confirm_replace` + `operation_token`，结果一律
 * 是**新 task_id、进度 0**，旧任务留在历史。
 *
 * 全部用仓库真实源码：真实 stores/tasks.ts + 真实 api/request.ts + 真实 Axios
 * 适配器（只替换网络边界）。
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
      esModuleInterop: true,
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
    if (name.startsWith('@/')) {
      // 递归解析 `@/`（auth store 依赖 authWire / operationToken 等纯模块）。
      return load(`${name.slice(2)}.ts`, overrides)
    }
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

/** 只替换网络边界：记录每次调用的方法/URL/载荷/令牌。 */
function installAdapter(handler) {
  const calls = []
  const original = axios.defaults.adapter
  axios.defaults.adapter = (config) => {
    calls.push({
      url: config.url,
      method: String(config.method || 'get').toLowerCase(),
      body: parseBody(config.data),
      token: config.headers?.[OPERATION_TOKEN_HEADER]
        ?? config.headers?.['x-operation-token'] ?? null,
      uid: config.headers?.['X-Account-UID']
        ?? config.headers?.['x-account-uid'] ?? null,
    })
    return handler(config, calls[calls.length - 1])
  }
  return {
    calls,
    restore() { axios.defaults.adapter = original },
  }
}

const OPERATION_TOKEN_HEADER = 'X-Operation-Token'

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

/** 真实 store：真实 request.ts（含重试/令牌/账号头）+ 真实 Axios 适配器。 */
function freshTaskStore() {
  setActivePinia(createPinia())
  const request = load('api/request.ts', {})
  const opEventsModule = load('stores/operationEvents.ts', {})
  const authModule = load('stores/auth.ts', {
    '@/api/request': request,
    '@/composables/useCache': { __esModule: true, default: { get: () => null, set: () => {}, remove: () => {} } },
    '@/boot': load('boot.ts', {}),
    '@/stores/operationEvents': opEventsModule,
  })
  const tasksOverrides = {
    '@/api/request': request,
    '@/composables/useCache': { __esModule: true, default: { get: () => null, set: () => {}, remove: () => {} } },
    '@/stores/auth': authModule,
  }
  const { useTaskStore } = load('stores/tasks.ts', tasksOverrides)
  const store = useTaskStore()
  const auth = authModule.useAuthStore()
  auth.isLoggedIn = true
  auth.userInfo = { uid: 92003, uname: '本地账号' }
  return { store, auth }
}

/** 服务端列表快照：id=11 的「学习区」，版本 7、业务日 2026-09-19。 */
function taskList(revision, businessDate) {
  return {
    tasks: [{
      id: 11, priority: 1, zone_name: '学习区', category: 2, total_days: 5,
      days_done: 1, deadline_raw: '=DATE(2027,12,31)', today_done: null,
      remaining_days: 1, needs_execution: true,
    }],
    revision, business_date: businessDate,
    total: 1, pending_total: 1, completed: 0, today_done: 0, today_pending: 1,
    remaining_time: 10, avg_remaining: 5, urgency: 1,
  }
}

/** 服务端一致预检：与 GET /api/tasks/creation-preview 的形状对齐。 */
function previewResponse({ revision = 7, businessDay = '2026-09-19' } = {}) {
  return {
    uid: 92003, instance_key: 'desktop-test', zone_name: '学习区',
    tasks_revision: revision, business_day: businessDay,
    existing_task: {
      id: 11, category: 2, total_days: 5, days_done: 1,
      deadline_raw: '=DATE(2027,12,31)', last_done_date: null,
    },
    existing_is_unfinished: true,
    is_currently_reserved: false, creation_allowed: true, reason: '',
  }
}

const FORM = { zone_name: '学习区', category: 2, total_days: 5, deadline_raw: '=DATE(2027,12,31)' }

const results = []
async function test(name, body) {
  try {
    await body()
    results.push({ name, status: 'PASS' })
  } catch (error) {
    results.push({ name, status: 'FAIL', message: error.message, stack: error.stack })
  }
}

const suppressed = []
const originalError = console.error
console.error = (...args) => { suppressed.push(args) }

async function main() {
  await test('确认时冻结的预检与令牌原样进入创建载荷', async () => {
    const adapter = installAdapter((config) => {
      if (config.method === 'get') {
        return ok(config, String(config.url).includes('creation-preview')
          ? previewResponse() : taskList(7, '2026-09-19'))
      }
      return ok(config, { success: true, task_id: 99, replaced_task_id: 11,
                          replayed: false, revision: 8,
                          wakeup: { delivered: true } })
    })
    try {
      const { store } = freshTaskStore()
      const preview = await store.fetchCreationPreview('学习区')
      assert.equal(preview.existing_task.id, 11)
      assert.equal(preview.tasks_revision, 7)

      // 弹框期间界面上的版本与业务日又前进了（后台结算 / 跨日刷新）
      store.tasksRevision = 42
      store.businessDate = '2026-09-20'
      const frozen = { task_id: 11, revision: 7, business_day: '2026-09-19',
                       existing_is_unfinished: true }
      await store.createNewTask(FORM, frozen, 'create-token-1')

      const post = adapter.calls.find((c) => c.method === 'post')
      assert.ok(post, '必须发出创建请求')
      assert.equal(post.url, '/api/tasks', '创建不再走 overwrite 查询参数')
      assert.equal(post.body.preview.task_id, 11, '冻结的目标身份原样上送')
      assert.equal(post.body.preview.revision, 7, '必须带确认时的版本')
      assert.equal(post.body.preview.business_day, '2026-09-19', '必须带确认时的业务日')
      assert.equal(post.body.preview.existing_is_unfinished, true)
      assert.equal(post.body.confirm_replace, true, '明确确认语义')
      assert.equal(post.body.operation_token, 'create-token-1', '一次性令牌')
      assert.equal(post.body.id, undefined, '旧覆盖形状不再上送')
      assert.equal(post.uid, '92003', '写请求携带冻结的账号归属')
    } finally {
      adapter.restore()
    }
  })

  await test('传输层重试重放同一份载荷：预检与令牌不被刷新', async () => {
    let posts = 0
    const adapter = installAdapter((config) => {
      if (config.method === 'get') {
        return ok(config, String(config.url).includes('creation-preview')
          ? previewResponse() : taskList(7, '2026-09-19'))
      }
      posts += 1
      return posts === 1 ? networkError(config)
                         : ok(config, { success: true, task_id: 99,
                                        replaced_task_id: 11, replayed: false,
                                        revision: 12,
                                        wakeup: { delivered: true } })
    })
    try {
      const { store } = freshTaskStore()
      const preview = await store.fetchCreationPreview('学习区')
      const frozen = { task_id: 11, revision: 7, business_day: '2026-09-19',
                       existing_is_unfinished: true }
      store.tasksRevision = 99
      store.businessDate = '2026-09-20'
      await store.createNewTask(FORM, frozen, 'create-token-1')

      const bodies = adapter.calls.filter((c) => c.method === 'post').map((c) => c.body)
      assert.equal(bodies.length, 2, '第一次传输失败后必须重试一次')
      assert.deepEqual(bodies[1], bodies[0], '重试必须重放同一份载荷')
      assert.equal(bodies[1].preview.revision, 7, '重试不得刷新到最新版本')
      assert.equal(bodies[1].preview.business_day, '2026-09-19')
      assert.equal(bodies[1].operation_token, 'create-token-1',
        '重试不换令牌（任务创建的令牌在载荷里，不是控制头）')
    } finally {
      adapter.restore()
    }
  })

  await test('服务端报告确认过期：明确失败，不拿新版本静默重发', async () => {
    const adapter = installAdapter((config) => {
      if (config.method === 'get') {
        return ok(config, String(config.url).includes('creation-preview')
          ? previewResponse() : taskList(7, '2026-09-19'))
      }
      return conflict(config, '创建确认已过期（create_stale_revision）：'
                             + '该分区任务在确认之后已被改动')
    })
    try {
      const { store } = freshTaskStore()
      await store.fetchTasks()          // 版本基线：7（与预检一致）
      assert.equal(store.tasksRevision, 7)
      await store.fetchCreationPreview('学习区')
      const frozen = { task_id: 11, revision: 7, business_day: '2026-09-19',
                       existing_is_unfinished: true }
      await assert.rejects(
        () => store.createNewTask(FORM, frozen, 'create-token-1'),
        (error) => {
          assert.match(error?.response?.data?.detail || '', /create_stale_revision/)
          return true
        })
      const posts = adapter.calls.filter((c) => c.method === 'post')
      assert.equal(posts.length, 1, '409 是明确响应：不得自动重发')
      assert.equal(posts[0].body.preview.revision, 7)
      assert.equal(store.tasksRevision, 7, '冲突后不得悄悄把本地版本改成最新值')
    } finally {
      adapter.restore()
    }
  })

  await test('缺少冻结预检或令牌的创建请求会被后端明确拒绝', async () => {
    const adapter = installAdapter((config) => {
      if (config.method === 'get') return ok(config, taskList(7, '2026-09-19'))
      return conflict(config, '缺少客户端一次性令牌 operation_token')
    })
    try {
      const { store } = freshTaskStore()
      // 没有预检（兼容形状也不完整）→ 后端输入合同明确拒绝，零写入。
      await assert.rejects(() => store.createNewTask(FORM, null, ''))
      const posts = adapter.calls.filter((c) => c.method === 'post')
      assert.equal(posts.length, 1, '只发出被拒的那一次，前端不得自动补预检重发')
      assert.equal(posts[0].body.preview, undefined)
    } finally {
      adapter.restore()
    }
  })

  await test('界面在确认框之前冻结预检，并把冻结值交给 store', async () => {
    const source = fs.readFileSync(
      path.join(root, 'src/views/TaskManager.vue'), 'utf8')
    const start = source.indexOf('async function runCreateFlow')
    assert.ok(start >= 0, '必须存在 runCreateFlow（新建流程）')
    const body = source.slice(start, source.indexOf('async function submitCreation'))
    const previewAt = body.indexOf('fetchCreationPreview')
    const frozenAt = body.indexOf('const frozen: FrozenCreationPreview')
    const confirmAt = body.indexOf('confirmReplaceTask')
    const submitAt = body.indexOf('submitCreation(')
    assert.ok(previewAt >= 0, '必须先取一致预检')
    assert.ok(frozenAt >= 0, '必须在弹框之前冻结预检')
    assert.ok(confirmAt >= 0)
    assert.ok(submitAt >= 0)
    assert.ok(frozenAt < confirmAt, '冻结必须发生在确认框之前')
    assert.ok(confirmAt < submitAt, '确认之后才允许派发')
    // 提交的是**冻结那份**预检与同一令牌，而不是现查的最新值。
    assert.match(body, /submitCreation\(data, frozen, newOperationToken\(\), scope\)/)
    // 已完成或已移出的旧任务不弹覆盖框：只有 existing_is_unfinished 才确认。
    assert.match(body, /if \(frozen\.existing_is_unfinished\)/)
  })

  console.error = originalError
  if (suppressed.length) {
    console.log(`（预期内的失败日志 ${suppressed.length} 条，已收敛）`)
  }
  const failed = results.filter((r) => r.status === 'FAIL')
  for (const item of failed) {
    console.log(`FAIL ${item.name}\n       ${item.message}\n${(item.stack||"").split("\n").slice(0,6).join("\n")}`)
  }
  console.log(`overwrite-precondition: ${results.length - failed.length}/${results.length} passed`)
  process.exit(failed.length ? 1 : 0)
}

main()
