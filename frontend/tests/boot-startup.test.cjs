/**
 * Desktop startup contract: real BootController + real request.ts/Axios + a
 * local HTTP server. The desktop boot reader has one protected read
 * (/api/auth/status) and no separate password handshake.
 */
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const Module = require('node:module')
const assert = require('node:assert/strict')
const ts = require('typescript')

globalThis.window = { electronAPI: undefined }
globalThis.localStorage = {
  _v: Object.create(null),
  getItem(key) { return this._v[key] ?? null },
  setItem(key, value) { this._v[key] = String(value) },
  removeItem(key) { delete this._v[key] },
}

function loadTs(filename) {
  const file = path.resolve(__dirname, filename)
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText
  const mod = new Module(file, module)
  mod.filename = file
  mod.paths = Module._nodeModulePaths(path.dirname(file))
  const originalRequire = mod.require.bind(mod)
  mod.require = function (name) {
    if (name === 'element-plus') return { ElMessage: { warning() {}, error() {}, success() {} } }
    if (name === './operationToken') return loadTs('../src/api/operationToken.ts')
    if (name === '@/stores/auth') return { useAuthStore: () => ({ checkLoginStatus: async () => false }) }
    if (name === '@/router') return { default: { currentRoute: { value: {} }, push() {} } }
    return originalRequire(name)
  }
  mod._compile(compiled, file)
  return mod.exports
}

const { BootController, BOOT_READ_MAX_ATTEMPTS } = loadTs('../src/boot.ts')
const { useRequest } = loadTs('../src/api/request.ts')
const request = useRequest()
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function makeServer(behaviour) {
  const hits = []
  const aborted = []
  const server = http.createServer((req, res) => {
    hits.push(req.url)
    if (behaviour === 'hang') {
      req.socket.on('close', () => { if (!res.writableEnded) aborted.push(req.url) })
      return
    }
    if (behaviour === 'destroy') {
      req.socket.destroy()
      return
    }
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ logged_in: behaviour === 'logged-in' }))
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const base = `http://127.0.0.1:${server.address().port}`
  return {
    base, hits, aborted,
    close: async () => {
      server.closeAllConnections()
      await new Promise((resolve) => server.close(resolve))
    },
  }
}

function readers(base) {
  return {
    readAuth: ({ signal, maxAttempts }) => request.get(
      `${base}/api/auth/status`, undefined, { signal, maxAttempts })
      .then((value) => ({ loggedIn: Boolean(value.logged_in) })),
  }
}

async function run() {
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

  await test('auth 读取挂起：预算内结束并真正 abort', async () => {
    const ctx = await makeServer('hang')
    try {
      const boot = new BootController(readers(ctx.base), 180, BOOT_READ_MAX_ATTEMPTS)
      const result = await boot.start()
      assert.equal(result.ok, false)
      assert.equal(result.failure.kind, 'timeout')
      await sleep(80)
      assert.ok(ctx.aborted.length >= 1)
      assert.ok(ctx.hits.length <= BOOT_READ_MAX_ATTEMPTS)
    } finally { await ctx.close() }
  })

  await test('网络断开与超时分支区分', async () => {
    const ctx = await makeServer('destroy')
    try {
      const boot = new BootController(readers(ctx.base), 180, 1)
      const result = await boot.start()
      assert.equal(result.ok, false)
      assert.equal(result.failure.kind, 'network')
    } finally { await ctx.close() }
  })

  await test('未登录是 ready 结果而不是启动失败', async () => {
    const ctx = await makeServer('logged-out')
    try {
      const boot = new BootController(readers(ctx.base), 500, 1)
      const result = await boot.start()
      assert.deepEqual({ ok: result.ok, loggedIn: result.loggedIn }, { ok: true, loggedIn: false })
    } finally { await ctx.close() }
  })

  await test('登录成功与并发 ensure 只使用同一代结果', async () => {
    const ctx = await makeServer('logged-in')
    try {
      const boot = new BootController(readers(ctx.base), 500, 1)
      const results = await Promise.all([boot.ensure(), boot.ensure(), boot.ensure()])
      assert.equal(results[0], results[1])
      assert.equal(results[1], results[2])
      assert.equal(results[0].loggedIn, true)
      assert.equal(ctx.hits.length, 1)
      const generation = results[0].generation
      boot.invalidate('test-login-change')
      const next = await boot.ensure()
      assert.ok(next.generation > generation)
    } finally { await ctx.close() }
  })

  if (failures.length) process.exitCode = 1
  else console.log('desktop boot startup: all passed')
}

run().catch((error) => { console.error(error); process.exitCode = 1 })
