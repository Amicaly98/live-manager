/**
 * frontendChain.test.cjs - 真实前端请求链测试（桌面移植阶段 2 重写，新协议）
 *
 * 用真实 `stores/live.ts` + 真实 `api/request.ts` + 真实 Axios + 真实 HTTP
 * （监听产品 Electron baseURL 的固定端口 8000）验证：
 *   1. 一次新的冻结操作带**客户端生成**的 `X-Operation-Token`（没有任何取票请求）；
 *   2. 传输层失败重试**复用同一令牌**；
 *   3. **取消链**：停止撤销在途的启动类意图——取消不重试（不复活被取消的操作），
 *      停止本身是一个新令牌；
 *   4. 409 是**回执**（rejected / token_conflict）：如实回到调用方，不换令牌重发。
 *
 * 运行：node backend/tests/frontend/frontendChain.test.cjs（需空闲 8000 端口）
 */

const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');
const fs = require('node:fs');
const Module = require('node:module');
const { createRequire } = require('node:module');

const REPO = path.resolve(__dirname, '../../..');
const FRONTEND = path.join(REPO, 'frontend');
const req = createRequire(path.join(FRONTEND, 'package.json'));
const ts = req('typescript');
const axios = req('axios');
const { createPinia, setActivePinia } = req('pinia');

// request.ts 的 Electron 分支固定 127.0.0.1:8000 —— 本地替身就听在这个端口上
//（测试即真实链路）。
globalThis.window = { electronAPI: {} };
globalThis.localStorage = {
  getItem: () => null, setItem: () => {}, removeItem: () => {},
};

const seen = [];                     // { url, token }
let mode = 'normal';                 // normal | destroy_once | delay_start | conflict_409
let destroyedOnce = false;

function handle(req, res) {
  const url = req.url.split('?')[0];
  const token = req.headers['x-operation-token'] || null;
  seen.push({ url, token });
  if (mode === 'destroy_once' && !destroyedOnce) {
    destroyedOnce = true;
    req.socket.destroy();            // 服务端已收下，响应在回程丢失
    return;
  }
  if (mode === 'delay_start') {
    setTimeout(() => {
      if (res.destroyed) return
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ operation_token: token, status: 'accepted',
        accepted: true, reason: '', projection: null, success: true,
        message: 'ok', detail: null }));
    }, 1500);
    return;
  }
  if (mode === 'conflict_409' && url.startsWith('/api/live/start')) {
    res.writeHead(409, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ operation_token: token, status: 'token_conflict',
      accepted: false, reason: '同一操作令牌已用于不同请求', projection: null,
      success: false, message: '', detail: null }));
    return;
  }
  if (url === '/api/live/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      has_active: false, has_run: false, resumable: false,
      resume_blocked_reason: '', step: 'idle', goal: null, run: null,
      current_zone: '', run_id: null, task_id: null, business_day: '', mode: '',
      push_source: '', target_seconds: 0, confirmed_seconds: 0,
      display_seconds: 0, display_advancing: false, display_valid_seconds: 0,
      display_source: 'frozen', duration_known: false, room_id: null, end: null,
      wait_reason: '', account: null, boot_id: 'boot', backend_events: [],
      face: null,
    }));
    return;
  }
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ operation_token: token, status: 'accepted',
    accepted: true, reason: '', projection: null, success: true,
    message: 'ok', detail: null }));
}

const PORT = 8000;
const server = http.createServer((req2, res) => {
  const chunks = [];
  req2.on('data', (c) => chunks.push(c));
  req2.on('end', () => handle(req2, res));
});

/** 真实 TS 模块递归装载（`@/` 与相对导入同源）。 */
function load(relative, overrides = {}, seenModules = new Map()) {
  const file = path.join(FRONTEND, 'src', relative)
  if (seenModules.has(file)) return seenModules.get(file)
  const module = { exports: {} }
  seenModules.set(file, module.exports)
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
      esModuleInterop: true,
    },
  }).outputText
  function local(name) {
    if (name in overrides) return overrides[name]
    if (name === 'element-plus') {
      return { ElMessage: { warning() {}, error() {}, success() {}, info() {} } }
    }
    if (name.startsWith('@/')) return load(`${name.slice(2)}.ts`, overrides, seenModules)
    if (name.startsWith('.')) {
      return load(`${path.relative(path.join(FRONTEND, 'src'),
                                     path.resolve(path.dirname(file), name))}.ts`,
                  overrides, seenModules)
    }
    return req(name)
  }
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                      { filename: file })(local, module, module.exports)
  return module.exports
}

async function main() {
  esbuildNotNeeded();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(PORT, '127.0.0.1', resolve);
  });

  let failures = 0;
  const test = async (name, fn) => {
    try {
      setActivePinia(createPinia());
      seen.length = 0;
      destroyedOnce = false;
      mode = 'normal';
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failures += 1;
      console.error(`not ok - ${name}\n  ${err && err.message}`);
    }
  };

  function freshStore() {
    const request = load('api/request.ts')
    const { useLiveStore } = load('stores/live.ts', { '@/api/request': request })
    return useLiveStore()
  }

  const tokensFor = (url) => seen.filter((s) => s.url === url).map((s) => s.token);

  // ---------- 1. 客户端令牌 ----------
  await test('新操作带客户端生成的令牌（无取票请求）', async () => {
    const store = freshStore();
    const result = await store.startSingle({ zoneName: '学习区', durationSeconds: 60 });
    assert.equal(result.outcome.kind, 'receipt');
    assert.equal(result.outcome.receipt.status, 'accepted');
    assert.equal(result.superseded, false);
    const tokens = tokensFor('/api/live/start');
    assert.deepEqual(tokens, [result.outcome.receipt.operation_token],
      '请求携带冻结的令牌');
    assert.equal(seen.filter((s) => s.url === '/api/live/operation-ticket').length, 0,
      '取票端点已退役：不得再访问');
    await store.teardown();
  });

  // ---------- 2. 传输失败重试复用同一令牌 ----------
  await test('传输层失败重试复用同一令牌并收敛为受理', async () => {
    const store = freshStore();
    mode = 'destroy_once';
    // resumeLive 需要一份可恢复投影；这里测的是传输层令牌复用，用单次开播即可。
    const result = await store.startSingle({ zoneName: '学习区', durationSeconds: 60 });
    assert.equal(result.outcome.kind, 'receipt');
    const tokens = tokensFor('/api/live/start');
    assert.ok(tokens.length >= 2, `应有首次请求 + 重放（实际 ${tokens.length}）`);
    assert.ok(tokens.every((t) => t === tokens[0]), '重放必须复用同一令牌');
    await store.teardown();
  });

  // ---------- 3. 取消链 ----------
  await test('停止撤销在途开播：取消不重试，停止用新令牌', async () => {
    const store = freshStore();
    mode = 'delay_start';
    const startPromise = store.startSingle({ zoneName: '学习区', durationSeconds: 60 });
    await new Promise((r) => setTimeout(r, 400));
    const startSeen = seen.filter((s) => s.url === '/api/live/start').length;
    assert.equal(startSeen, 1, 'start 应已到达服务端');
    const stopResult = await store.stopLive();
    const startResult = await startPromise;
    assert.equal(startResult.outcome.kind, 'cancelled', '被停止打断的开播应报告取消');
    assert.equal(seen.filter((s) => s.url === '/api/live/start').length, startSeen,
      '取消后不得重试开播（否则等于复活被取消的操作）');
    assert.equal(stopResult.outcome.kind, 'receipt');
    const stopTokens = tokensFor('/api/live/stop');
    assert.equal(stopTokens.length, 1);
    assert.notEqual(stopTokens[0],
      seen.find((s) => s.url === '/api/live/start').token,
      '停止是新意图：必须带自己的令牌');
    await store.teardown();
  });

  // ---------- 4. 409 回执 ----------
  await test('409 是回执：明确失败且不换令牌重发', async () => {
    const store = freshStore();
    mode = 'conflict_409';
    const result = await store.startNextTask();
    assert.equal(result.outcome.kind, 'receipt');
    assert.equal(result.outcome.receipt.status, 'token_conflict');
    assert.equal(result.outcome.receipt.accepted, false);
    assert.ok(result.notice, '应给出用户可读的失败提示');
    assert.match(result.notice.message, /重新操作|令牌/);
    const tokens = tokensFor('/api/live/start');
    assert.equal(tokens.length, 1, '409 之后不得用新令牌重发');
    await store.teardown();
  });

  server.closeAllConnections?.();
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 250);
    server.close(() => { clearTimeout(timer); resolve(); });
  });
  if (failures > 0) process.exit(1);
  console.log(`frontend chain: 4/4 passed`);
  process.exit(0);
}

function esbuildNotNeeded() { /* 新协议链直接用 TS 转译装载，不再需要打包步骤 */ }

main().catch((err) => {
  console.error('fatal:', err);
  process.exit(1);
});
