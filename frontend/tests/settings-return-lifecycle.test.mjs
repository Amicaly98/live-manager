/**
 * Real browser regression for settings ownership across route leave/re-entry.
 *
 * This reuses the localhost-only browser harness from auth-settings-lifecycle:
 * the built app, Vue, Pinia and Axios are real; only the HTTP boundary is a
 * synthetic local server.  There is no account, panel password, SMTP or
 * external network involved.
 */
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const source = fs.readFileSync(new URL('./auth-settings-lifecycle.test.mjs', import.meta.url), 'utf8')
let harness = source.slice(0, source.indexOf('async function run()'))
harness = harness.replace(
  "const here = path.dirname(fileURLToPath(import.meta.url))",
  'const here = ' + JSON.stringify(fileURLToPath(new URL('.', import.meta.url))),
)
harness += `
const stub = createStub()
stub.state.authLoggedIn = true
stub.state.settings = makeSettings({ video_path: 'SYNTHETIC-OLD' })
stub.state.settingsSaves = []
await stub.listen()
const base = stub.base()
const browser = await chromium.launch({ headless: true })
const results = []
const record = (name, ok, detail = '') => {
  results.push([name, ok])
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name + (detail ? ' :: ' + detail : ''))
}

try {
  const context = await newContext(browser, base)
  const page = await context.newPage()
  await page.goto(base + '/#/settings')
  const field = page.locator('input[placeholder="F:/videosforlive"]')
  await field.waitFor()
  await waitFor(async () => await field.inputValue() === 'SYNTHETIC-OLD', 'initial settings')

  await field.fill('SYNTHETIC-FIRST-EDIT')
  await page.evaluate(() => document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/'))
  await waitHash(page, '#/')
  await waitFor(() => stub.state.settingsSaves.length === 1, '离页保存')
  record('离开设置页仍保存首个编辑', stub.state.settings.video_path === 'SYNTHETIC-FIRST-EDIT'
    && stub.state.settingsSaves[0].body.video_path === 'SYNTHETIC-FIRST-EDIT', JSON.stringify({
      disk: stub.state.settings.video_path,
      writes: stub.state.settingsSaves.length,
    }))

  await page.evaluate(() => document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings'))
  await waitHash(page, '#/settings')
  await waitFor(async () => await field.inputValue() === 'SYNTHETIC-FIRST-EDIT', '保存后的设置重入')
  await field.fill('SYNTHETIC-SECOND-EDIT')
  await sleep(2300)
  const second = await observe(page)
  record('重入后的首个真实编辑不被初始化逻辑吞掉', second.settings?.unsaved === false
    && await field.inputValue() === 'SYNTHETIC-SECOND-EDIT'
    && stub.state.settings.video_path === 'SYNTHETIC-SECOND-EDIT'
    && stub.state.settingsSaves.length === 2, JSON.stringify({
      value: await field.inputValue(),
      disk: stub.state.settings.video_path,
      writes: stub.state.settingsSaves.length,
      store: second.settings,
    }))

  await page.reload()
  const reloadedField = page.locator('input[placeholder="F:/videosforlive"]')
  await reloadedField.waitFor()
  await waitFor(async () => await reloadedField.inputValue() === 'SYNTHETIC-SECOND-EDIT', 'reload settings')
  record('重载后仍保留第二次编辑', await reloadedField.inputValue() === 'SYNTHETIC-SECOND-EDIT'
    && stub.state.settings.video_path === 'SYNTHETIC-SECOND-EDIT')

  stub.state.settingsMode = 'conflict'
  await reloadedField.fill('SYNTHETIC-CONFLICT-EDIT')
  await waitFor(() => stub.state.settingsSaves.length === 3, '冲突保存')
  await waitFor(async () => await page.locator('.save-blocked-alert').count() === 1, '可见保存失败提示')
  const blocked = await observe(page)
  const retry = page.getByRole('button', { name: '重试保存' })
  record('保存冲突保留编辑并提供可操作提示', blocked.settings?.unsaved === true
    && blocked.settings?.blocked === true
    && await retry.count() === 1
    && await page.getByText('设置未保存', { exact: false }).count() >= 1, JSON.stringify(blocked.settings))
  const writesAfterConflict = stub.state.settingsSaves.length
  await sleep(700)
  record('保存冲突不会无限重发', stub.state.settingsSaves.length === writesAfterConflict,
    \`writes=\${stub.state.settingsSaves.length}\`)

  stub.state.settingsMode = 'ok'
  await retry.click()
  await waitFor(async () => {
    const value = await observe(page)
    return value.settings?.blocked === false && value.settings?.unsaved === false
      && stub.state.settings.video_path === 'SYNTHETIC-CONFLICT-EDIT'
  }, '显式重试保存')
  record('失败后显式重试保存当前编辑', stub.state.settingsSaves.length === 4
    && stub.state.settings.video_path === 'SYNTHETIC-CONFLICT-EDIT')
  await context.close()
} finally {
  await browser.close()
  await stub.close()
}

const failures = results.filter(([, ok]) => !ok)
console.log('settings-return-lifecycle: ' + (results.length - failures.length) + '/' + results.length + ' passed')
if (failures.length) process.exitCode = 1
`
await import('data:text/javascript;base64,' + Buffer.from(harness).toString('base64'))
