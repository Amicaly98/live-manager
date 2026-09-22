/**
 * Browser half of the desktop file:// smoke gate. It uses only the built
 * static bundle and an in-memory localhost auth stub; it never touches a real
 * account or the user's Electron profile.
 */
import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const playwrightEntry = process.env.PLAYWRIGHT_MODULE
  || path.resolve(root, '../../.tooling/node_modules/playwright/index.mjs')
const { chromium } = await import(pathToFileURL(playwrightEntry).href)

// Chromium's standalone file origin applies a stricter module CORS check than
// Electron's loadFile path. The flag models Electron's local-file privilege;
// CSP remains active and the test still verifies the localhost connect rule.
const browser = await chromium.launch({ headless: true, args: ['--allow-file-access-from-files'] })
try {
  const context = await browser.newContext()
  await context.addInitScript(() => {
    window.electronAPI = {
      isElectron: true,
      showNotification: async () => {},
      onTrayQuit: () => {},
      confirmQuit: async () => {},
      forceQuit: () => {},
    }
  })
  const page = await context.newPage()
  let authHits = 0
  await page.route('http://127.0.0.1:8000/api/auth/status', async (route) => {
    authHits += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ logged_in: false }),
    })
  })
  await page.goto(pathToFileURL(path.join(root, 'dist', 'index.html')).href)
  await page.getByText('扫描二维码登录 B 站账号').waitFor({ state: 'visible' })
  assert.equal(authHits, 1, 'file:// boot must reach the allowed localhost API origin')
  await context.close()

  const failedContext = await browser.newContext()
  const failedPage = await failedContext.newPage()
  await failedPage.route('**/assets/index-*.js', (route) => route.abort())
  await failedPage.goto(pathToFileURL(path.join(root, 'dist', 'index.html')).href)
  await failedPage.getByText('正在连接…').waitFor({ state: 'visible' })
  const failedEntry = failedPage.url()
  await failedPage.getByRole('link', { name: '重新加载' }).click()
  await failedPage.getByText('正在连接…').waitFor({ state: 'visible' })
  assert.match(failedPage.url(), /[\\/]index\.html(?:$|#)/,
    'file:// fallback reload must target index.html, never the dist directory')
  assert.equal(failedPage.url(), failedEntry,
    'relative file:// reload must preserve the entry document URL')
  await failedContext.close()

  const noScriptContext = await browser.newContext({ javaScriptEnabled: false })
  const noScriptPage = await noScriptContext.newPage()
  await noScriptPage.goto(pathToFileURL(path.join(root, 'dist', 'index.html')).href)
  await noScriptPage.getByText('正在连接…').waitFor({ state: 'visible' })
  await noScriptContext.close()
  console.log('file:// browser shell: PASS')
} finally {
  await browser.close()
}
