/**
 * Real Electron file:// smoke, isolated from the product main process.
 * It uses a temporary Electron userData directory and a localhost-only stub;
 * no Bilibili account, product profile, SMTP, tray process, or backend is used.
 */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const { app, BrowserWindow, ipcMain } = require('electron')

const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'dist', 'index.html')
const preload = path.join(__dirname, 'electron-file-url-preload.cjs')
assert.ok(fs.existsSync(dist), 'run npm run build before the Electron smoke')

app.disableHardwareAcceleration()
app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-frontend-smoke-')))

let authHits = 0
let quitAttempts = 0
let trayReady = false
ipcMain.handle('smoke-confirm-quit', async (_event, stopLive) => {
  quitAttempts += 1
  assert.equal(typeof stopLive, 'boolean')
  if (quitAttempts === 1) return { success: false }
  if (quitAttempts === 2) throw new Error('simulated IPC rejection')
  return { success: true }
})
ipcMain.on('smoke-force-quit', () => {})
ipcMain.on('smoke-tray-ready', () => { trayReady = true })

const server = http.createServer((req, res) => {
  const url = (req.url || '').split('?')[0]
  res.setHeader('Content-Type', 'application/json')
  if (url === '/api/auth/status') {
    authHits += 1
    res.end(JSON.stringify({ logged_in: false }))
    return
  }
  if (url === '/api/auth/qrcode') {
    res.end(JSON.stringify({ qrcode_url: 'data:image/png;base64,AA==', qrcode_key: 'smoke' }))
    return
  }
  res.end(JSON.stringify({}))
})

function finish(code, error) {
  if (error) console.error(error)
  try { server.close() } catch { /* already closed */ }
  app.exit(code)
}

async function waitUntil(check, label, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error(`timed out waiting for ${label}`)
}

async function hasQuitDialog(win) {
  return win.webContents.executeJavaScript(
    "(() => { const el = document.querySelector('.el-overlay-message-box'); if (!el) return false; const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && el.getClientRects().length > 0; })()",
  )
}

async function approveQuit(win) {
  await waitUntil(() => hasQuitDialog(win), 'quit confirmation')
  await win.webContents.executeJavaScript(
    "(() => { const buttons = [...document.querySelectorAll('.el-overlay-message-box .el-button--primary')]; buttons.at(-1)?.click() })()",
  )
}

app.whenReady().then(async () => {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(8000, '127.0.0.1', resolve)
  })
  const win = new BrowserWindow({
    show: false,
    webPreferences: {
      preload,
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  win.webContents.on('console', (message) => {
    if (message.type() === 'error') console.error(`[electron console] ${message.text()}`)
  })
  await win.loadFile(dist)
  await new Promise((resolve) => setTimeout(resolve, 1000))
  const text = await win.webContents.executeJavaScript('document.body.innerText')
  assert.match(text, /扫描二维码登录 B 站账号/)
  assert.ok(authHits >= 1, 'Electron file:// page must reach the localhost auth route')
  await waitUntil(() => trayReady, 'renderer tray IPC registration')

  // Exercise the actual renderer↔main IPC path.  The first response is an
  // explicit cleanup failure; the second is an IPC rejection.  Both must
  // release App.vue's close guard so a later tray event opens confirmation
  // again.  The third attempt proves the guard is still usable afterwards.
  win.webContents.send('smoke-tray-quit')
  await approveQuit(win)
  await waitUntil(() => quitAttempts === 1, 'first quit IPC attempt')
  await new Promise((resolve) => setTimeout(resolve, 100))

  win.webContents.send('smoke-tray-quit')
  await approveQuit(win)
  await waitUntil(() => quitAttempts === 2, 'second quit IPC attempt')
  await new Promise((resolve) => setTimeout(resolve, 100))

  win.webContents.send('smoke-tray-quit')
  await approveQuit(win)
  await waitUntil(() => quitAttempts === 3, 'third quit IPC attempt')
  assert.equal(quitAttempts, 3, 'a failed/rejected quit must permit a later tray retry')
  console.log('Electron file:// CSP/API smoke: PASS')
  win.destroy()
  finish(0)
}).catch((error) => finish(1, error))
