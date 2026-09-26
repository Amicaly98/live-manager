/**
 * Desktop 1.1.1 status UX contracts. These tests exercise the real TypeScript
 * store/helper with controlled boundaries and guard the Dashboard wiring that
 * keeps recovery progress and cached operation logs visible during slow reads.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const req = createRequire(path.join(root, 'package.json'))
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

await test('server resume snapshot restores saved progress and gates task start', () => {
  const resume = loadTs('src/composables/dashboardResume.ts')
  const previous = resume.emptyDashboardResumeState()
  const restored = resume.applyDashboardResumeStatus(previous, {
    is_streaming: false,
    source_mode: 'manual',
    resumable: true,
    current_zone: '手动分区',
    elapsed_seconds: 777,
    duration_seconds: 1800,
    room_id: 12345,
  })

  assert.equal(restored.resume_status_fresh, true)
  assert.equal(restored.has_active, true)
  assert.equal(restored.current_zone, '手动分区')
  assert.equal(restored.elapsed_seconds, 777, '恢复卡片必须保留服务端已确认时长')
  assert.equal(resume.canResumeDashboardTask(restored), true)
  assert.equal(resume.canStartDashboardTask(restored), false)
})

await test('未知或断流状态不抹除已保存进度，也不开放恢复或任务开播', () => {
  const resume = loadTs('src/composables/dashboardResume.ts')
  const previous = {
    ...resume.emptyDashboardResumeState(),
    has_active: true,
    resume_status_fresh: true,
    current_zone: '待恢复分区',
    elapsed_seconds: 901,
    duration_seconds: 2400,
    room_id: 45,
  }
  const stale = resume.applyDashboardResumeStatus(previous, undefined)
  assert.equal(stale.resume_status_fresh, false)
  assert.equal(stale.elapsed_seconds, 901, '旧快照保留显示，网络未知不能把进度归零')
  assert.equal(resume.canResumeDashboardTask(stale), false)
  assert.equal(resume.canStartDashboardTask(stale), false)

  const blocked = resume.applyDashboardResumeStatus(stale, {
    is_streaming: false,
    resumable: false,
    resume_blocked_reason: '恢复状态的目标时长未知',
    current_zone: '待恢复分区',
    elapsed_seconds: 901,
    duration_seconds: 0,
    room_id: 45,
  })
  assert.equal(blocked.resume_status_fresh, true)
  assert.equal(blocked.has_active, false)
  assert.equal(blocked.resume_blocked_reason, '恢复状态的目标时长未知')
  assert.equal(blocked.elapsed_seconds, 901)
  assert.equal(resume.canResumeDashboardTask(blocked), false)
  assert.equal(resume.canStartDashboardTask(blocked), true)
})

await test('平台推流恢复阶段显示短状态，不按正常计时状态呈现', () => {
  const { createPinia, setActivePinia } = req('pinia')
  setActivePinia(createPinia())
  const transport = { get: async () => ({}), post: async () => ({}) }
  const operationEvents = { ingestFromStatus() {} }
  const liveModule = loadTs('src/stores/live.ts', (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/stores/operationEvents') return { operationEvents }
    return req(name)
  })
  const live = liveModule.useLiveStore()
  live.applyStatus({
    is_streaming: true,
    current_zone: '分区',
    elapsed_seconds: 600,
    remaining_seconds: 1200,
    room_id: 1,
    duration_seconds: 1800,
    duration_known: true,
    run_id: 'run-1',
    pending_seconds: 0,
    pending_valid_seconds: 0,
    pending_extrapolatable: false,
    timer_state: 'paused_pusher',
  })
  assert.equal(live.timerHint, '恢复中')
  live.stopLocalTick()
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
        tasks, revision: completed ? 2 : 1, business_date: '2026-09-25',
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
  const auth = { isLoggedIn: true }
  const tasksModule = loadTs('src/stores/tasks.ts', (name) => {
    if (name === '@/api/request') return { useRequest: () => transport }
    if (name === '@/stores/auth') return { useAuthStore: () => auth }
    if (name === '@/composables/useCache') return { __esModule: true, default: cache }
    if (name === '@/stores/operationEvents') return { operationEvents: { addLocal() {} } }
    if (name === 'element-plus') return { ElMessage: { warning() {}, error() {}, success() {} } }
    return req(name)
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
  const apiTypes = fs.readFileSync(path.join(root, 'src/types/api.ts'), 'utf8')
  assert.match(apiTypes, /resumable\?: boolean/)
  assert.match(apiTypes, /resume_blocked_reason\?: string/)
  assert.match(dashboard, /applyDashboardResumeStatus\(activeTaskState\.value, status\)/)
  assert.doesNotMatch(dashboard, /\/api\/live\/state\/full/)
  const mounted = dashboard.match(/onMounted\(async \(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(mounted, 'Dashboard mount hook must be present')
  assert.ok(mounted.indexOf('loadCrossEvents()') < mounted.indexOf('await fetchStatus()'),
    'cached events must render before any network read is awaited')
  assert.match(dashboard, /canResumeDashboardTask\(activeTaskState\)/)
  assert.match(dashboard, /canStartDashboardTask\(activeTaskState\)/)
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
    return req(name)
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
