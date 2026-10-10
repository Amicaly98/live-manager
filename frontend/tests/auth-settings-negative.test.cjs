/**
 * Negative sensitivity for the auth/settings browser gate.
 *
 * The current source is copied to a temporary directory, transpiled and run
 * there.  A second isolated copy removes one protection at a time.  The
 * repaired scenario must pass while the withdrawn-protection scenario must
 * fail.  The repository source and current dist are never modified.
 */
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const vm = require('node:vm')
const assert = require('node:assert/strict')
const ts = require('typescript')
const { createPinia, setActivePinia, defineStore } = require('pinia')

const root = path.resolve(__dirname, '..')
const sourceRoot = path.join(root, 'src')
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-auth-settings-negative-'))
fs.cpSync(sourceRoot, path.join(tempRoot, 'src'), { recursive: true })

globalThis.defineStore = defineStore
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
  clear: () => {},
}

function loadTs(file, overrides = {}) {
  let source = fs.readFileSync(file, 'utf8').replace(/\r\n/g, '\n')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      esModuleInterop: true,
    },
  }).outputText
  const module = { exports: {} }
  const req = (name) => {
    if (Object.prototype.hasOwnProperty.call(overrides, name)) return overrides[name]
    // 递归解析 `@/`（auth store 依赖 authWire / operationToken 等纯模块）：
    // 这些模块不参与本套件的"撤保护"对照，直接装载同一份副本。
    if (name.startsWith('@/')) {
      // `@/` 相对的是 **src 根**，不是当前文件所在目录（stores/api/... 是错误路径）。
      let dir = path.dirname(file)
      while (dir !== path.dirname(dir) && path.basename(dir) !== 'src') {
        dir = path.dirname(dir)
      }
      const srcRoot = path.basename(dir) === 'src' ? dir : sourceRoot
      return loadTs(path.join(srcRoot, name.slice(2) + '.ts'), overrides)
    }
    return require(name)
  }
  const fn = vm.runInThisContext(`(function(require,module,exports){${compiled}\n})`, {
    filename: file,
  })
  fn(req, module, module.exports)
  return module.exports
}

function makeAuthWorld(authFile) {
  const pinia = createPinia()
  setActivePinia(pinia)
  let statusCalls = 0
  const cacheData = new Map()
  const boot = { generation: 0, invalidate() { this.generation += 1 } }
  const operationEvents = { handleContextSwitch() {} }
  // 一次 status GET 可以被**挂起**：用来构造"发起于退出之前、解析于退出受理之后"
  // 的迟到读取 —— 这正是意图代次门要挡的那种回包。
  let holdArmed = false
  let pendingStatus = null
  const transport = {
    get: async (url) => {
      assert.equal(url, '/api/auth/status')
      statusCalls += 1
      if (holdArmed) {
        holdArmed = false
        await new Promise((resolve) => { pendingStatus = resolve })
      }
      return {
        logged_in: true,
        user_info: { uid: 7, uname: 'LIVE', face: '', level: 1 },
      }
    },
    post: async (url) => {
      assert.equal(url, '/api/auth/logout')
      return { success: true }
    },
  }
  const authModule = loadTs(authFile, {
    '@/api/request': { useRequest: () => transport },
    '@/composables/useCache': {
      __esModule: true,
      default: {
        get: (key) => cacheData.get(key) || null,
        set: (key, value) => cacheData.set(key, value),
        remove: (key) => cacheData.delete(key),
      },
    },
    '@/boot': { boot },
    '@/stores/operationEvents': { operationEvents },
    'element-plus': { ElMessage: { warning() {}, error() {}, success() {} } },
  })
  const auth = authModule.useAuthStore()
  return {
    auth,
    statusCalls: () => statusCalls,
    /** 挂起**下一次** status GET（调用一次只挂起一次）。 */
    holdNextStatus() { holdArmed = true },
    /** 放行当前挂起的那次 GET。 */
    releaseStatus() {
      const release = pendingStatus
      pendingStatus = null
      if (release) release()
    },
  }
}

async function runAuthScenario(authFile) {
  const world = makeAuthWorld(authFile)
  // 启动期的一次 status 读取（尚未登录）被挂起在途；退出在它解析之前受理。
  // 这正是"启动读取 vs 受理退出"的真实交错。
  world.holdNextStatus()
  const lateRead = world.auth.checkLoginStatus()
  for (let i = 0; i < 200 && world.statusCalls() < 1; i += 1) {
    await new Promise((r) => setTimeout(r, 1))
  }
  assert.equal(world.statusCalls() >= 1, true, '迟到读取必须已在途')
  await world.auth.logout()                    // 受理：撤销本地身份 + 前进代次
  world.releaseStatus()
  const lateResult = await lateRead
  return {
    lateResult,
    afterLogoutStatus: world.auth.isLoggedIn,
    statusCalls: world.statusCalls(),
  }
}

function settingsFixture(overrides = {}) {
  return {
    video_path: 'F:/videosforlive', excel_path: 'live_tasks.xlsx', db_path: 'live_tasks.db',
    scan_interval_seconds: 30, max_reconnect: 3, live_retry_cooldown_minutes: 60,
    stream_mode: 'manual', auto_open_video: true, ffmpeg_path: 'ffmpeg',
    ffmpeg_reencode: true, notification_enabled: true, notification_channel: 'email',
    email_enabled: false, email_smtp_host: 'smtp.qq.com', email_smtp_port: 587,
    email_smtp_user: '', email_smtp_pass: '', email_recipients: '',
    email_notify_start: true, email_notify_stop: true, email_notify_error: true,
    email_notify_complete: true, email_daily_summary: true, email_face_verify_port: 19080,
    serverchan_sendkey: '', duration_distribution: 'beta',
    duration_multiplier_min: 1.05, duration_multiplier_max: 1.25,
    ...overrides,
  }
}

async function runSettingsScenario(settingsFile) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const fixture = settingsFixture()
  const puts = []
  let releaseFirst = null
  let putCount = 0
  const transport = {
    get: async () => ({ ...fixture, _revision: 10 }),
    put: async (_url, body) => {
      putCount += 1
      puts.push(body)
      if (putCount === 1) {
        return new Promise((resolve) => {
          releaseFirst = () => resolve({ duration_multiplier_min: 1.99, _revision: 11 })
        })
      }
      return { _revision: 12 }
    },
  }
  const settingsModule = loadTs(settingsFile, {
    '@/api/request': { useRequest: () => transport },
  })
  const settings = settingsModule.useSettingsStore()
  await settings.fetchSettings()
  settings.updateField('duration_multiplier_min', 1.11)
  const firstSave = settings.saveSettings()
  for (let i = 0; i < 100 && !releaseFirst; i += 1) await new Promise((r) => setTimeout(r, 1))
  assert.equal(typeof releaseFirst, 'function', '第一份设置保存必须进入在途状态')
  settings.updateField('duration_multiplier_min', 1.22)
  releaseFirst()
  await firstSave
  for (let i = 0; i < 100 && puts.length < 2; i += 1) await new Promise((r) => setTimeout(r, 1))
  await new Promise((r) => setTimeout(r, 10))
  return {
    valueAfterOldResponse: settings.settings.duration_multiplier_min,
    firstPayload: puts[0]?.duration_multiplier_min,
    secondPayload: puts[1]?.duration_multiplier_min,
    putCount,
  }
}

function patchFile(file, oldText, newText) {
  const source = fs.readFileSync(file, 'utf8').replace(/\r\n/g, '\n')
  assert.ok(source.includes(oldText), `negative patch anchor missing: ${oldText}`)
  fs.writeFileSync(file, source.replace(oldText, newText))
}

async function main() {
  const rows = []
  const authFile = path.join(tempRoot, 'src', 'stores', 'auth.ts')
  const settingsFile = path.join(tempRoot, 'src', 'stores', 'settings.ts')

  // 新退出语义（阶段 2）：退出**受理之后**才撤销本地身份。此后的一次重新读取
  // 是合法的复核，但它**不得**把已受理的退出翻回去——写回前的意图代次门是那道
  // 唯一的闸，撤掉它场景必须失败。
  const fixedAuth = await runAuthScenario(authFile)
  rows.push({
    case: '修复后：退出受理前的在途读取不得恢复登录',
    ...fixedAuth,
    sensitive: fixedAuth.afterLogoutStatus === false
      && fixedAuth.lateResult === false,
  })

  patchFile(authFile,
    '      if (intentSeq !== _authIntentSeq) return isLoggedIn.value\n', '')
  const oldAuth = await runAuthScenario(authFile)
  rows.push({
    case: '旧行为：退出受理前的在途读取把账号装回去',
    ...oldAuth,
    sensitive: oldAuth.afterLogoutStatus === true,
  })

  // Restore a clean settings copy before testing the settings mutation.
  fs.copyFileSync(path.join(sourceRoot, 'stores', 'settings.ts'), settingsFile)
  const fixedSettings = await runSettingsScenario(settingsFile)
  rows.push({
    case: '修复后：旧 settings 响应不覆盖新编辑',
    ...fixedSettings,
    sensitive: fixedSettings.valueAfterOldResponse === 1.22
      && fixedSettings.firstPayload === 1.11
      && fixedSettings.secondPayload === 1.22,
  })

  patchFile(settingsFile, '      if (_dirtyRevision === revision) {\n', '      if (true) {\n')
  const oldSettings = await runSettingsScenario(settingsFile)
  rows.push({
    case: '旧行为：旧 settings 响应覆盖新编辑',
    ...oldSettings,
    sensitive: oldSettings.valueAfterOldResponse === 1.99
      && oldSettings.firstPayload === 1.11
      && oldSettings.secondPayload === 1.99,
  })

  console.log(JSON.stringify(rows, null, 2))
  const bad = rows.filter((row) => !row.sensitive)
  console.log(`auth-settings-negative: ${rows.length - bad.length}/${rows.length} sensitive`)
  process.exitCode = bad.length ? 1 : 0
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 2
}).finally(() => {
  fs.rmSync(tempRoot, { recursive: true, force: true })
})
