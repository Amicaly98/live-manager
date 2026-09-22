/**
 * auth-settings-lifecycle.test.mjs
 *
 * Desktop-only browser gate for the authentication and settings handover.
 * The test loads the built manager application, real Vue/Pinia stores and the
 * real Axios request wrapper.  Only the HTTP boundary is a localhost stub;
 * there is no panel password, Bilibili account, SMTP, Electron profile, or
 * external network.
 *
 * Coverage:
 *   - delayed auth/status with stale user_info cache never authenticates the UI;
 *   - logout in flight does not start another auth/status read;
 *   - a late QR response and a late old logout cannot replace a new identity;
 *   - auth_logout_blocked restores the account and resumes task reads;
 *   - settings conflict blocks without a request storm;
 *   - a response for an older settings edit cannot overwrite a newer edit.
 */
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const dist = path.join(root, 'dist')
const playwrightEntry = process.env.PLAYWRIGHT_MODULE
  || path.resolve(root, '../../.tooling/node_modules/playwright/index.mjs')
const { chromium } = await import(pathToFileURL(playwrightEntry).href)

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.json': 'application/json; charset=utf-8',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function makeSettings(overrides = {}) {
  return {
    video_path: 'F:/videosforlive',
    excel_path: 'live_tasks.xlsx',
    db_path: 'live_tasks.db',
    scan_interval_seconds: 30,
    max_reconnect: 3,
    live_retry_cooldown_minutes: 60,
    stream_mode: 'manual',
    auto_open_video: true,
    ffmpeg_path: 'ffmpeg',
    ffmpeg_reencode: true,
    notification_enabled: true,
    notification_channel: 'email',
    email_enabled: false,
    email_smtp_host: 'smtp.qq.com',
    email_smtp_port: 587,
    email_smtp_user: '',
    email_smtp_pass: '',
    email_recipients: '',
    email_notify_start: true,
    email_notify_stop: true,
    email_notify_error: true,
    email_notify_complete: true,
    email_daily_summary: true,
    email_face_verify_port: 19080,
    serverchan_sendkey: '',
    duration_distribution: 'beta',
    duration_multiplier_min: 1.05,
    duration_multiplier_max: 1.25,
    ...overrides,
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (chunk) => chunks.push(chunk))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function createStub() {
  const state = {
    authLoggedIn: false,
    currentUser: { uid: 1, uname: 'CURRENT', face: '', level: 1 },
    nextUser: { uid: 1, uname: 'CURRENT', face: '', level: 1 },
    pollMode: 'ok', // ok | hold
    authStatusDelayMs: 0,
    logoutMode: 'ok', // ok | hold | blocked | fail500
    logoutSeen: 0,
    pollCount: 0,
    taskRows: [{ id: 1, zone_name: 'SYNTHETIC', needs_execution: false }],
    settings: makeSettings(),
    settingsRevision: 10,
    settingsMode: 'ok', // ok | hold | conflict
    settingsGets: 0,
    settingsSaves: [],
  }
  const hits = new Map()
  const totalHits = new Map()
  const pendingLogouts = []
  const pendingPolls = []
  const pendingSaves = []

  const hit = (key) => {
    hits.set(key, (hits.get(key) || 0) + 1)
    totalHits.set(key, (totalHits.get(key) || 0) + 1)
  }
  const json = (res, body, status = 200) => {
    res.writeHead(status, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(body))
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url || '/', 'http://localhost')
    const p = url.pathname
    hit(`${req.method} ${p}`)

    if (p === '/api/auth/status') {
      if (state.authStatusDelayMs) await sleep(state.authStatusDelayMs)
      return json(res, {
        logged_in: state.authLoggedIn,
        user_info: state.authLoggedIn ? state.currentUser : null,
      })
    }

    if (p === '/api/auth/qrcode') {
      return json(res, {
        qrcode_key: `synthetic-${state.pollCount + 1}`,
        qrcode_url: 'data:image/png;base64,AA==',
      })
    }

    if (p.startsWith('/api/auth/poll/')) {
      state.pollCount += 1
      if (state.pollMode === 'hold') {
        await new Promise((resolve) => pendingPolls.push(resolve))
      }
      state.authLoggedIn = true
      state.currentUser = { ...state.nextUser }
      return json(res, { logged_in: true, user_info: state.currentUser })
    }

    if (p === '/api/auth/logout') {
      state.logoutSeen += 1
      if (state.logoutMode === 'hold') {
        await new Promise((resolve) => pendingLogouts.push(resolve))
      }
      if (state.logoutMode === 'blocked') {
        return json(res, {
          detail: 'synthetic live refusal',
          code: 'auth_logout_blocked',
        }, 409)
      }
      if (state.logoutMode === 'fail500') {
        return json(res, { detail: 'synthetic logout failure' }, 500)
      }
      state.authLoggedIn = false
      return json(res, { success: true, message: '已登出' })
    }

    if (p === '/api/tasks') {
      return json(res, {
        tasks: state.taskRows,
        revision: 3,
        business_date: '2026-09-22',
        total: state.taskRows.length,
        pending_total: state.taskRows.length,
        completed: 0,
        today_done: 0,
        today_pending: state.taskRows.length,
        remaining_time: 0,
        avg_remaining: 0,
        urgency: 0,
      })
    }
    if (p === '/api/tasks/stats') {
      return json(res, {
        revision: 3,
        business_date: '2026-09-22',
        total: state.taskRows.length,
        pending_total: state.taskRows.length,
        completed: 0,
        today_done: 0,
        today_pending: state.taskRows.length,
        remaining_time: 0,
        avg_remaining: 0,
        urgency: 0,
      })
    }

    if (p === '/api/live/status') {
      return json(res, {
        is_streaming: false,
        elapsed_seconds: 0,
        current_zone: '',
        backend_events: [],
      })
    }
    if (p === '/api/live/events') return json(res, { events: [] })

    if (p === '/api/settings' && req.method === 'GET') {
      state.settingsGets += 1
      return json(res, { ...state.settings, _revision: state.settingsRevision })
    }
    if (p === '/api/settings' && req.method === 'PUT') {
      const raw = await readBody(req)
      const body = raw ? JSON.parse(raw) : {}
      state.settingsSaves.push({
        body,
        revision: req.headers['x-settings-revision'] || null,
      })
      if (state.settingsMode === 'hold') {
        await new Promise((resolve) => pendingSaves.push(resolve))
      }
      if (state.settingsMode === 'conflict') {
        return json(res, { detail: 'synthetic settings conflict' }, 409)
      }
      state.settingsRevision += 1
      state.settings = { ...state.settings, ...body }
      const sequence = state.settingsSaves.length
      // The first response is intentionally an old correction. The second
      // response is held so the browser assertion can observe that the old
      // response never overwrote the newer user edit.
      if (sequence === 1) {
        return json(res, {
          ...state.settings,
          duration_multiplier_min: 1.99,
          _revision: state.settingsRevision,
        })
      }
      if (sequence === 2) {
        return json(res, {
          _revision: state.settingsRevision,
          duration_multiplier_max: body.duration_multiplier_max,
        })
      }
      return json(res, { ...state.settings, _revision: state.settingsRevision })
    }

    if (p.startsWith('/api/')) return json(res, {})

    let file = path.join(dist, p === '/' ? 'index.html' : p.replace(/^\//, ''))
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(dist, 'index.html')
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' })
    res.end(fs.readFileSync(file))
  })

  return {
    state,
    hitCount(method, p) { return hits.get(`${method} ${p}`) || 0 },
    count(p) {
      let total = 0
      for (const [key, value] of hits) if (key.endsWith(` ${p}`)) total += value
      return total
    },
    resetHits() {
      hits.clear()
      state.logoutSeen = 0
    },
    totalHitCount(method, p) { return totalHits.get(`${method} ${p}`) || 0 },
    totalCount(p) {
      let total = 0
      for (const [key, value] of totalHits) if (key.endsWith(` ${p}`)) total += value
      return total
    },
    totalPrefixCount(p) {
      let total = 0
      for (const [key, value] of totalHits) if (key.endsWith(` ${p}`) || key.includes(` ${p}`)) total += value
      return total
    },
    releaseLogout() {
      const resolve = pendingLogouts.shift()
      if (resolve) resolve()
    },
    pendingPollCount() { return pendingPolls.length },
    releasePoll() {
      const resolve = pendingPolls.shift()
      if (resolve) resolve()
    },
    releaseSave() {
      const resolve = pendingSaves.shift()
      if (resolve) resolve()
    },
    listen() { return new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)) },
    base() { return `http://127.0.0.1:${server.address().port}` },
    async close() {
      server.closeAllConnections?.()
      await new Promise((resolve) => server.close(resolve))
    },
  }
}

async function waitFor(check, label, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await sleep(40)
  }
  throw new Error(`等待 ${label} 超时`)
}

async function waitHash(page, hash, timeout = 15000) {
  await page.waitForFunction((expected) => location.hash === expected, hash, { timeout })
}

async function observe(page) {
  return page.evaluate(() => {
    const app = document.querySelector('#app').__vue_app__
    const stores = app.config.globalProperties.$pinia._s
    const auth = stores.get('auth')
    const tasks = stores.get('tasks')
    const settings = stores.get('settings')
    return {
      hash: location.hash,
      loggedIn: auth?.isLoggedIn ?? null,
      userInfo: auth?.userInfo ?? null,
      cached: localStorage.getItem('user_info'),
      taskCount: tasks?.tasks?.length ?? null,
      settings: settings ? {
        min: settings.settings.duration_multiplier_min,
        host: settings.settings.email_smtp_host,
        revision: settings.settingsRevision,
        blocked: settings.saveBlocked,
        unsaved: settings.hasUnsavedChanges,
        error: settings.lastSaveError,
      } : null,
    }
  })
}

async function loginByQr(page, base, stub, user, { gotoFirst = true } = {}) {
  stub.state.nextUser = { ...user }
  if (gotoFirst) await page.goto(`${base}/#/login`)
  else await waitHash(page, '#/login')
  await page.getByText('等待扫码...', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
  const before = stub.state.pollCount
  await waitFor(() => stub.state.pollCount > before, '二维码轮询')
  await waitHash(page, '#/')
}

async function clickLogout(page) {
  await page.locator('.sidebar .user-info').click()
  await page.getByText('退出登录', { exact: true }).click()
}

async function newContext(browser, base, initScript, initArg) {
  const context = await browser.newContext()
  await context.route('**/*', (route) => {
    if (route.request().url().startsWith(base)) route.continue()
    else route.abort()
  })
  if (initScript) await context.addInitScript(initScript, initArg)
  return context
}

async function run() {
  const stub = createStub()
  await stub.listen()
  const base = stub.base()
  const browser = await chromium.launch({ headless: true })
  const results = []
  const record = (name, ok, detail = '') => {
    results.push([name, ok])
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`)
  }

  try {
    // 1. A delayed false status must finish on login even with stale cached data.
    stub.state.authLoggedIn = false
    stub.state.authStatusDelayMs = 900
    stub.state.logoutMode = 'ok'
    stub.resetHits()
    const staleCache = JSON.stringify({
      data: { uid: 999, uname: 'STALE-CACHE', face: '', level: 1 },
      expireAt: Date.now() + 60_000,
    })
    const ctx = await newContext(browser, base, (cache) => localStorage.setItem('user_info', cache), staleCache)
    const page = await ctx.newPage()
    await page.goto(`${base}/#/tasks`)
    await page.getByText('正在连接…', { exact: true }).waitFor({ state: 'visible' })
    record('auth/status 挂起期间显示可见启动态', true)
    record('auth/status 挂起期间不启动任务读取', stub.count('/api/tasks') === 0,
           `tasks=${stub.count('/api/tasks')}`)
    await waitHash(page, '#/login')
    const delayed = await observe(page)
    record('旧 user_info 缓存不能冒充登录', delayed.loggedIn === false
      && delayed.hash === '#/login' && delayed.taskCount === 0,
    JSON.stringify(delayed))
    await ctx.close()

    // 2. Logout keeps the local account out and does not re-read status while held.
    stub.state.authStatusDelayMs = 0
    stub.state.logoutMode = 'ok'
    stub.state.authLoggedIn = false
    stub.resetHits()
    const ctxLogout = await newContext(browser, base)
    const pageLogout = await ctxLogout.newPage()
    await loginByQr(pageLogout, base, stub, { uid: 10, uname: 'OLD', face: '', level: 1 })
    const statusBeforeLogout = stub.count('/api/auth/status')
    const tasksBeforeLogout = stub.count('/api/tasks')
    stub.state.logoutMode = 'hold'
    await clickLogout(pageLogout)
    await waitFor(() => stub.state.logoutSeen >= 1, 'logout 请求到达')
    await sleep(700)
    const duringLogout = await observe(pageLogout)
    record('退出期间不重读 auth/status', stub.count('/api/auth/status') === statusBeforeLogout,
           `before=${statusBeforeLogout} after=${stub.count('/api/auth/status')}`)
    record('退出期间本地身份与任务读取立即停止', duringLogout.loggedIn === false
      && duringLogout.cached === null
      && stub.count('/api/tasks') - tasksBeforeLogout <= 1,
    JSON.stringify({ duringLogout, taskDelta: stub.count('/api/tasks') - tasksBeforeLogout }))
    stub.releaseLogout()
    await waitHash(pageLogout, '#/login')
    await ctxLogout.close()

    // 3. auth_logout_blocked restores identity and allows task reads again.
    stub.state.authLoggedIn = false
    stub.state.logoutMode = 'ok'
    stub.resetHits()
    const ctxBlocked = await newContext(browser, base)
    const pageBlocked = await ctxBlocked.newPage()
    await loginByQr(pageBlocked, base, stub, { uid: 11, uname: 'LIVE', face: '', level: 1 })
    const taskBeforeBlocked = stub.count('/api/tasks')
    stub.state.logoutMode = 'blocked'
    await clickLogout(pageBlocked)
    await waitFor(() => stub.state.logoutSeen >= 1, 'blocked logout 请求')
    await sleep(1000)
    const blocked = await observe(pageBlocked)
    record('auth_logout_blocked 恢复账号且留在控制台', blocked.loggedIn === true
      && blocked.userInfo?.uname === 'LIVE' && blocked.hash === '#/', JSON.stringify(blocked))
    record('auth_logout_blocked 后任务读取恢复', stub.count('/api/tasks') > taskBeforeBlocked,
           `before=${taskBeforeBlocked} after=${stub.count('/api/tasks')}`)
    await ctxBlocked.close()

    // 4. A QR response already in flight before logout must not restore login.
    stub.state.authLoggedIn = false
    stub.state.logoutMode = 'ok'
    stub.state.pollMode = 'ok'
    stub.resetHits()
    const ctxLateQr = await newContext(browser, base)
    const pageLateQr = await ctxLateQr.newPage()
    await loginByQr(pageLateQr, base, stub, { uid: 12, uname: 'BEFORE-QR', face: '', level: 1 })
    stub.state.nextUser = { uid: 13, uname: 'STALE-QR', face: '', level: 1 }
    stub.state.pollMode = 'hold'
    await pageLateQr.evaluate(() => {
      const app = document.querySelector('#app').__vue_app__
      const auth = app.config.globalProperties.$pinia._s.get('auth')
      window.__lateQr = auth.pollLoginStatus('old-qr-key')
    })
    await waitFor(() => stub.pendingPollCount() >= 1, '迟到扫码请求')
    await clickLogout(pageLateQr)
    await waitHash(pageLateQr, '#/login')
    stub.releasePoll()
    const lateQrResult = await pageLateQr.evaluate(() => window.__lateQr)
    const lateQrState = await observe(pageLateQr)
    record('迟到扫码响应按未登录处理', lateQrResult?.logged_in === false
      && lateQrState.loggedIn === false && lateQrState.cached === null,
    JSON.stringify({ result: lateQrResult, state: lateQrState }))
    await ctxLateQr.close()

    // 5. An old logout response must not clear a later QR login.
    stub.state.authLoggedIn = false
    stub.state.logoutMode = 'ok'
    stub.state.pollMode = 'ok'
    stub.resetHits()
    const ctxRace = await newContext(browser, base)
    const pageRace = await ctxRace.newPage()
    await loginByQr(pageRace, base, stub, { uid: 21, uname: 'OLD', face: '', level: 1 })
    stub.state.logoutMode = 'hold'
    await clickLogout(pageRace)
    await waitFor(() => stub.state.logoutSeen >= 1, '迟到 logout 请求')
    await pageRace.evaluate(async () => {
      const app = document.querySelector('#app').__vue_app__
      await app.config.globalProperties.$router.push('/login')
    })
    await waitHash(pageRace, '#/login')
    await loginByQr(pageRace, base, stub, { uid: 22, uname: 'NEW', face: '', level: 2 }, { gotoFirst: false })
    const beforeRelease = await observe(pageRace)
    stub.releaseLogout()
    await sleep(700)
    const afterRelease = await observe(pageRace)
    record('迟到旧登出前新扫码身份已成立', beforeRelease.loggedIn === true
      && beforeRelease.userInfo?.uname === 'NEW', JSON.stringify(beforeRelease))
    record('迟到旧登出不得覆盖新身份', afterRelease.loggedIn === true
      && afterRelease.userInfo?.uname === 'NEW'
      && afterRelease.cached?.includes('NEW')
      && afterRelease.hash === '#/', JSON.stringify(afterRelease))
    await ctxRace.close()

    // 5. Real request/Axios/Pinia settings path: old response vs newer edit.
    stub.state.authLoggedIn = false
    stub.state.logoutMode = 'ok'
    stub.state.settings = makeSettings()
    stub.state.settingsRevision = 10
    stub.state.settingsMode = 'ok'
    stub.state.settingsGets = 0
    stub.state.settingsSaves.length = 0
    stub.resetHits()
    const ctxSettings = await newContext(browser, base)
    const pageSettings = await ctxSettings.newPage()
    await loginByQr(pageSettings, base, stub, { uid: 31, uname: 'SETTINGS', face: '', level: 1 })
    await pageSettings.evaluate(async () => {
      const app = document.querySelector('#app').__vue_app__
      await app.config.globalProperties.$router.push('/settings')
    })
    await waitHash(pageSettings, '#/settings')
    await pageSettings.getByText('设置', { exact: true }).first().waitFor({ state: 'visible' })
    await waitFor(() => stub.state.settingsGets >= 1, 'settings 初始读取')

    stub.state.settingsMode = 'hold'
    await pageSettings.evaluate(() => {
      const app = document.querySelector('#app').__vue_app__
      const settings = app.config.globalProperties.$pinia._s.get('settings')
      settings.updateField('duration_multiplier_min', 1.11)
      window.__firstSettingsSave = settings.saveSettings()
    })
    await waitFor(() => stub.state.settingsSaves.length === 1, '第一份设置保存')
    assert.equal(stub.state.settingsSaves[0].revision, '10')
    await pageSettings.evaluate(() => {
      const app = document.querySelector('#app').__vue_app__
      app.config.globalProperties.$pinia._s.get('settings')
        .updateField('duration_multiplier_min', 1.22)
    })
    stub.releaseSave()
    await waitFor(() => stub.state.settingsSaves.length === 2, '第二份设置保存')
    const duringSecondSave = await observe(pageSettings)
    record('旧 settings 响应不覆盖保存期间的新编辑', duringSecondSave.settings?.min === 1.22,
           JSON.stringify(duringSecondSave.settings))
    stub.state.settingsMode = 'ok'
    stub.releaseSave()
    await pageSettings.evaluate(() => window.__firstSettingsSave)
    await waitFor(async () => {
      const value = await observe(pageSettings)
      return value.settings?.blocked === false && value.settings?.unsaved === false
    }, '设置保存链收尾')
    record('设置串行保存最终追平用户编辑', (await observe(pageSettings)).settings?.min === 1.22,
           JSON.stringify(await observe(pageSettings)))

    // 6. 409 conflict is visible, retains the edit, and does not storm.
    stub.state.settingsMode = 'conflict'
    await pageSettings.evaluate(() => {
      const app = document.querySelector('#app').__vue_app__
      const settings = app.config.globalProperties.$pinia._s.get('settings')
      settings.updateField('email_smtp_host', 'conflict.example')
      window.__conflictSave = settings.saveSettings()
    })
    await waitFor(() => stub.state.settingsSaves.length === 3, '设置冲突请求')
    await pageSettings.evaluate(() => window.__conflictSave)
    await waitFor(async () => (await observe(pageSettings)).settings?.blocked === true,
                  '设置冲突阻塞')
    const savesAfterConflict = stub.state.settingsSaves.length
    await sleep(700)
    const conflict = await observe(pageSettings)
    record('设置冲突反馈可见且保留编辑内容', conflict.settings?.blocked === true
      && conflict.settings?.unsaved === true
      && conflict.settings?.host === 'conflict.example'
      && conflict.settings?.error.includes('synthetic settings conflict'), JSON.stringify(conflict.settings))
    record('设置冲突不会无限重发', stub.state.settingsSaves.length === savesAfterConflict,
           `saves=${stub.state.settingsSaves.length}`)

    // Explicit retry after a new edit sends the current value with the latest revision.
    stub.state.settingsMode = 'ok'
    await pageSettings.evaluate(() => {
      const app = document.querySelector('#app').__vue_app__
      const settings = app.config.globalProperties.$pinia._s.get('settings')
      settings.updateField('email_smtp_host', 'edited-after-conflict')
      window.__retrySettingsSave = settings.retrySave()
    })
    await waitFor(() => stub.state.settingsSaves.length === 4, '设置显式重试')
    await pageSettings.evaluate(() => window.__retrySettingsSave)
    await waitFor(async () => {
      const value = await observe(pageSettings)
      return value.settings?.blocked === false && value.settings?.unsaved === false
    }, '设置显式重试收尾')
    const retried = stub.state.settingsSaves[3]
    record('设置显式重试发送最新编辑与版本', retried.body.email_smtp_host === 'edited-after-conflict'
      && retried.revision === '12', JSON.stringify(retried))
    await ctxSettings.close()
  } finally {
    await browser.close()
    await stub.close()
  }

  const failures = results.filter(([, ok]) => !ok)
  console.log(`auth-settings-lifecycle: ${results.length - failures.length}/${results.length} passed`)
  console.log(JSON.stringify({
    failures: failures.length,
    assertions: results.length,
    counts: {
      authStatus: stub.totalCount('/api/auth/status'),
      authLogout: stub.totalCount('/api/auth/logout'),
      authPoll: stub.totalPrefixCount('/api/auth/poll/'),
      tasks: stub.totalCount('/api/tasks'),
      settingsGet: stub.totalHitCount('GET', '/api/settings'),
      settingsPut: stub.totalHitCount('PUT', '/api/settings'),
    },
  }))
  if (failures.length) process.exitCode = 1
}

run().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
