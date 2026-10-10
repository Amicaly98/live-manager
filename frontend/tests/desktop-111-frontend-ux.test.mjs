/**
 * Desktop 1.1.1 status UX contracts（桌面移植阶段 2 迁移到新投影）。
 *
 * 这些测试用真实 TypeScript store/helper + 受控边界，守护控制台接线：
 * 恢复进度与缓存的操作日志在慢读取期间保持可见、不被误清零。
 *
 * 与旧版的差别：
 *  - `dashboardResume.ts` 已删除：恢复事实的唯一来源是 `liveStore.projection`
 *    （`resumable` / `resume_blocked_reason` / `confirmed_seconds`），不再有第二份
 *    页面级快照；
 *  - `pending_*` 显示窗口已由 `types/session.ts` 的
 *    `display_seconds / display_advancing / display_valid_seconds` 取代，
 *    对应守卫见 `display-projection.test.mjs`。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { makeAliasFallback } from './cjs-alias.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const req = createRequire(path.join(root, 'package.json'))
const loadCjs = makeAliasFallback(req)
const ts = req('typescript')
const failures = []

async function test(name, body) {
  try {
    await body()
    console.log(`PASS ${name}`)
  } catch (error) {
    failures.push(name)
    console.error(`FAIL ${name}: ${error.message}`)
  }
}

function loadTs(relFile, customRequire = req) {
  const filename = path.join(root, relFile)
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText
  const module = { exports: {} }
  const fn = vm.runInThisContext(`(function(require,module,exports){${compiled}\n})`,
    { filename })
  fn(customRequire, module, module.exports)
  return module.exports
}

/** 一份最小投影（与后端 projection.build_projection 对齐）。 */
function projection({ run = 'run-1', confirmed = 777, target = 1800,
                      resumable = false, blockedReason = '',
                      step = 'live', source = 'confirmed' } = {}) {
  return {
    has_active: true, has_run: true, resumable, resume_blocked_reason: blockedReason,
    step, goal: { revision: 3, wanted: resumable ? 'stopped' : 'running',
      mode: 'task', selection: {}, start_kind: resumable ? 'resume' : 'new',
      stop_reason: null },
    run: { id: run, uid: 92003, room_id: 55003, area_id: 1, zone_name: '手动分区',
      mode: 'task', task_id: 7, business_day: '2026-10-10',
      target_seconds: target, push_source: 'ffmpeg',
      clock: { checkpoint_seconds: confirmed, interval_start_mono: null,
               interval_start_wall: null, projecting: false,
               display_anchor_mono: null },
      end: null },
    current_zone: '手动分区', run_id: run, task_id: 7,
    business_day: '2026-10-10', mode: 'task', push_source: 'ffmpeg',
    target_seconds: target, confirmed_seconds: confirmed,
    display_seconds: confirmed, display_advancing: source === 'confirmed',
    display_valid_seconds: source === 'confirmed' ? 10 : 0, display_source: source,
    duration_known: target > 0, room_id: 55003, end: null, wait_reason: '',
    account: { uid: 92003, room_id: 55003, display_name: '测试' },
    boot_id: 'boot', backend_events: [], face: null,
  }
}

await test('服务端恢复快照保留已确认进度并挡住新任务开播', () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  const transport = { get: async () => ({}), post: async () => ({}) }
  // 传输边界与事件摄取换成替身：`@/api/request` 必须在**递归**装载
  // （live.ts → liveControl.ts → request.ts）时同样生效，因此放进别名兜底的
  // extra 表，而不是只在顶层 require 上拦截。
  const liveModule = loadTs('src/stores/live.ts', makeAliasFallback(req, {
    '@/api/request': () => ({ useRequest: () => transport }),
    '@/stores/operationEvents': () => ({ operationEvents: { ingestFromStatus() {} } }),
  }))
  const live = liveModule.useLiveStore()
  live.applyStatus(projection({ resumable: true, step: 'idle', confirmed: 777 }))

  assert.equal(live.resumable, true, '可恢复事实只来自投影')
  assert.equal(live.zoneName, '手动分区')
  assert.equal(live.confirmedSeconds, 777, '恢复卡片必须保留服务端已确认时长')
  assert.equal(live.targetSeconds, 1800)
  live.teardown()
})

await test('读失败与被拒原因不抹除已确认进度，也不开放恢复或开播', () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  const transport = { get: async () => ({}), post: async () => ({}) }
  // 传输边界与事件摄取换成替身：`@/api/request` 必须在**递归**装载
  // （live.ts → liveControl.ts → request.ts）时同样生效，因此放进别名兜底的
  // extra 表，而不是只在顶层 require 上拦截。
  const liveModule = loadTs('src/stores/live.ts', makeAliasFallback(req, {
    '@/api/request': () => ({ useRequest: () => transport }),
    '@/stores/operationEvents': () => ({ operationEvents: { ingestFromStatus() {} } }),
  }))
  const live = liveModule.useLiveStore()
  // 服务端明确"不可恢复"时保留事实与原因，不虚构可恢复状态。
  live.applyStatus(projection({ resumable: false, blockedReason: '目标时长未知',
                                step: 'idle' }))
  assert.equal(live.resumable, false)
  assert.equal(live.resumeBlockedReason, '目标时长未知')
  assert.equal(live.confirmedSeconds, 777, '被拒原因不抹掉已确认进度')
  live.teardown()
})

await test('平台推流恢复阶段显示短状态，不按正常计时状态呈现', () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  const transport = { get: async () => ({}), post: async () => ({}) }
  // 传输边界与事件摄取换成替身：`@/api/request` 必须在**递归**装载
  // （live.ts → liveControl.ts → request.ts）时同样生效，因此放进别名兜底的
  // extra 表，而不是只在顶层 require 上拦截。
  const liveModule = loadTs('src/stores/live.ts', makeAliasFallback(req, {
    '@/api/request': () => ({ useRequest: () => transport }),
    '@/stores/operationEvents': () => ({ operationEvents: { ingestFromStatus() {} } }),
  }))
  const live = liveModule.useLiveStore()
  // 乐观阶段（Start 已派出、待首次确认）：有已确认进度 → "恢复中"，全新场 → "开播确认中"。
  live.applyStatus(projection({ step: 'resume_query', confirmed: 600,
                                source: 'optimistic', target: 1800 }))
  assert.equal(live.timerHint, '恢复中',
    '有已确认进度的乐观窗口必须显示"恢复中"')
  live.applyStatus(projection({ step: 'resume_query', confirmed: 0,
                                source: 'optimistic', target: 1800 }))
  assert.equal(live.timerHint, '开播确认中')
  live.teardown()
})

await test('标记完成后立即读取新任务顺序并更新下一任务', async () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  let completed = false
  const calls = []
  const taskRow = (id, zone, needsExecution) => ({
    id, zone_name: zone, priority: id, category: 1, total_days: 1,
    actual_days: 1, days_done: needsExecution ? 0 : 1,
    today_done: needsExecution ? 0 : 1, remaining_days: needsExecution ? 1 : 0,
    needs_execution: needsExecution, is_completed: false,
  })
  const transport = {
    get: async (url) => {
      calls.push(`GET ${url}`)
      if (url !== '/api/tasks') throw new Error(`Unexpected GET ${url}`)
      const tasks = completed
        ? [taskRow(2, '下一分区', true), taskRow(1, '已完成分区', false)]
        : [taskRow(1, '已完成分区', true), taskRow(2, '下一分区', true)]
      return {
        tasks, revision: completed ? 2 : 1, business_date: '2026-10-10',
        total: 2, pending_total: 2, completed: completed ? 1 : 0,
        today_done: completed ? 1 : 0, today_pending: completed ? 1 : 2,
        remaining_time: 0, avg_remaining: 0, urgency: 0,
      }
    },
    post: async (url) => {
      calls.push(`POST ${url}`)
      if (!url.startsWith('/api/tasks/mark-done/')) throw new Error(`Unexpected POST ${url}`)
      completed = true
      return { success: true, message: 'done' }
    },
    put: async () => ({}),
    delete: async () => ({}),
    getBlob: async () => new Blob(),
    postForm: async () => ({}),
  }
  const cache = { get: () => undefined, set() {}, remove() {} }
  const auth = { isLoggedIn: true, userInfo: { uid: 92003 } }
  const tasksModule = loadTs('src/stores/tasks.ts', (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/stores/auth') return { useAuthStore: () => auth }
    if (name === '@/composables/useCache') return { __esModule: true, default: cache }
    if (name === '@/stores/operationEvents') return { operationEvents: { addLocal() {} } }
    if (name === 'element-plus') return { ElMessage: { warning() {}, error() {}, success() {} } }
    return loadCjs(name)
  })
  const tasks = tasksModule.useTaskStore()
  await tasks.fetchTasks()
  assert.equal(tasks.nextTask.zone_name, '已完成分区')

  assert.equal(await tasks.markTaskDone('已完成分区', 1), true)
  assert.deepEqual(calls.map((call) => call.split(' ')[0]), ['GET', 'POST', 'GET'])
  assert.equal(tasks.tasks[0].zone_name, '下一分区')
  assert.equal(tasks.nextTask.zone_name, '下一分区', '完成结果应即时反映在下一任务上')
})

await test('Dashboard consumes resumable status and loads cached logs before awaiting reads', () => {
  const dashboard = fs.readFileSync(path.join(root, 'src/views/Dashboard.vue'), 'utf8')
  const sessionTypes = fs.readFileSync(path.join(root, 'src/types/session.ts'), 'utf8')
  // 恢复事实只来自投影：页面不再持有第二份恢复快照。
  assert.match(sessionTypes, /resumable: boolean/)
  assert.match(sessionTypes, /resume_blocked_reason: string/)
  assert.match(dashboard, /liveStore\.resumable/)
  assert.match(dashboard, /liveStore\.resumeBlockedReason/)
  assert.match(dashboard, /liveStore\.confirmedSeconds/)
  // 已退役入口不得再被调用（页面层完全没有这些字符串）。
  assert.doesNotMatch(dashboard, /\/api\/live\/(state\/update|clear-state|switch-area|state\/reload)/)
  const mounted = dashboard.match(/onMounted\(async \(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(mounted, 'Dashboard mount hook must be present')
  assert.ok(mounted.indexOf('operationEvents.ensureLoaded()') < mounted.indexOf('await liveStore.refreshStatus()'),
    'cached events must render before any network read is awaited')
})

await test('FFmpeg 短断流邮件选项默认关闭、读取旧设置仍关闭且可在界面切换', async () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  let storedSettings
  const transport = {
    get: async () => ({ ...storedSettings, _revision: 4 }),
    put: async (_url, body) => ({ ...body, _revision: 5 }),
  }
  const settingsModule = loadTs('src/stores/settings.ts', (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    return loadCjs(name)
  })
  const settings = settingsModule.useSettingsStore()
  assert.equal(settings.settings.email_notify_ffmpeg_retry, false)

  storedSettings = { ...settings.settings }
  delete storedSettings.email_notify_ffmpeg_retry
  await settings.fetchSettings()
  assert.equal(settings.settings.email_notify_ffmpeg_retry, false,
    '未返回新字段的旧设置快照也应使用关闭默认值')

  settings.updateField('email_notify_ffmpeg_retry', true)
  assert.equal(settings.settings.email_notify_ffmpeg_retry, true)
  const settingsType = fs.readFileSync(path.join(root, 'src/types/api.ts'), 'utf8')
  const settingsView = fs.readFileSync(path.join(root, 'src/views/Settings.vue'), 'utf8')
  assert.match(settingsType, /email_notify_ffmpeg_retry: boolean/)
  assert.match(settingsView, /settingsStore\.updateField\('email_notify_ffmpeg_retry'/)
  assert.match(settingsView, /本地 FFmpeg 短断流/)
})

console.log(failures.length ? `desktop 1.1.1 frontend UX: ${failures.length} failed` : 'desktop 1.1.1 frontend UX: all passed')
process.exitCode = failures.length ? 1 : 0
