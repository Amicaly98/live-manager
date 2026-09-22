/**
 * frontendChain.test.cjs - 真实前端请求链测试（D7：store→request→Axios→HTTP）
 *
 * 用 esbuild 把 stores/live.ts（含 api/request.ts + api/operationToken.ts）
 * 打包成 CJS，在 Node 里对接**真实 HTTP 服务**（监听产品固定端口 8000），
 * 验证监督 D7 指出的"40 项 Python 测试不能替代前端请求链"的场景：
 *   1. 新控制意图 → 经同一实例向 /operation-ticket 取票 → 请求带票据头；
 *   2. 传输层失败重试 → 复用同一票据（不换票、不重复取票）；
 *   3. 取消链：stopLive 打断在途开播 → 取消不重试 → 停止用新票据；
 *   4. 409 过期重放 → 明确失败，绝不用新票据重发。
 *
 * 运行：node backend/tests/frontend/frontendChain.test.cjs（需空闲 8000 端口）
 */

const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const Module = require('node:module');
const fs = require('node:fs');

const REPO = path.resolve(__dirname, '../../..');
const FRONTEND = path.join(REPO, 'frontend');
const OUT_DIR = path.join(FRONTEND, '.test-build');

// ---------- esbuild 打包真实前端源码（store→request→operationToken） ----------
const esbuild = require(path.join(FRONTEND, 'node_modules', 'esbuild'));
fs.mkdirSync(OUT_DIR, { recursive: true });
// '@/stores/auth' 与 '@/router' 只在 401 分支动态导入（会把 .vue 拖进
// bundle）；测试不触发 401，直接给空模块。
const stubAuth = {
  name: 'stub-401-deps',
  setup(build) {
    for (const target of ['@/stores/auth', '@/router']) {
      build.onResolve({ filter: new RegExp('^' + target.replace('/', '\\/') + '$') }, (args) => ({
        path: args.path, namespace: 'stub-401',
      }));
    }
    build.onLoad({ filter: /.*/, namespace: 'stub-401' }, () => ({
      contents: 'export const useAuthStore = () => ({ logout() {} }); export default { push() {} }',
      loader: 'js',
    }));
  },
};
let esbuildBuild; // 异步构建（plugins 不支持同步 API）
async function bundleFrontend() {
  await esbuildBuild({
    entryPoints: [path.join(FRONTEND, 'src', 'stores', 'live.ts')],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    outfile: path.join(OUT_DIR, 'live-bundle.cjs'),
    alias: { '@': path.join(FRONTEND, 'src') },
    external: ['element-plus', 'pinia', 'vue'], // element-plus 桩替；pinia/vue 与测试共享实例
    plugins: [stubAuth],
    logLevel: 'silent',
  });
}

// ---------- Node 环境桩 ----------
const toasts = [];
const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === 'element-plus') {
    return {
      ElMessage: {
        warning: (m) => toasts.push(['warning', m]),
        error: (m) => toasts.push(['error', m]),
        success: (m) => toasts.push(['success', m]),
      },
    };
  }
  return origLoad.apply(this, arguments);
};
globalThis.window = { electronAPI: {} }; // 触发产品 baseURL（http://127.0.0.1:8000）
const memStorage = new Map();
globalThis.localStorage = {
  getItem: (k) => (memStorage.has(k) ? memStorage.get(k) : null),
  setItem: (k, v) => memStorage.set(k, String(v)),
  removeItem: (k) => memStorage.delete(k),
};

// ---------- 模拟后端（记录票据与控制请求） ----------
const serverState = {
  ticketFetches: 0,
  ticketSeq: 0,
  startRequests: [],   // {ticket, at}
  stopRequests: [],
  mode: 'normal',      // normal | destroy_once | stale_409 | delay_start
  destroyedOnce: false,
};
const BOOT = 'testboot';

function handle(req, res, body) {
  const url = req.url.split('?')[0];
  if (req.method === 'GET' && url === '/api/live/operation-ticket') {
    serverState.ticketFetches += 1;
    const ticket = `${BOOT}:0:${++serverState.ticketSeq}`;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ticket, epoch: 0 }));
    return;
  }
  if (req.method === 'POST' && url === '/api/live/start') {
    const ticket = req.headers['x-operation-token'] || '';
    serverState.startRequests.push({ ticket, at: Date.now() });
    if (serverState.mode === 'destroy_once' && !serverState.destroyedOnce) {
      serverState.destroyedOnce = true;
      req.socket.destroy(); // 传输层硬断：触发同一实例重试
      return;
    }
    if (serverState.mode === 'stale_409') {
      res.writeHead(409, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: '操作已失效（响应已丢失，旧操作在停止后到达被拒绝）' }));
      return;
    }
    if (serverState.mode === 'delay_start') {
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: true, room_id: 1, message: 'ok' }));
      }, 3000);
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: true, room_id: 1, message: 'ok' }));
    return;
  }
  if (req.method === 'POST' && url === '/api/live/stop') {
    serverState.stopRequests.push({ ticket: req.headers['x-operation-token'] || '', at: Date.now() });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: true, message: '已停止' }));
    return;
  }
  if (req.method === 'GET' && url === '/api/live/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      is_streaming: true, current_zone: 'z', elapsed_seconds: 1,
      remaining_seconds: 1, room_id: 1, is_anomaly: false,
      ffmpeg_active: false, ffmpeg_current_video: '',
    }));
    return;
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ detail: 'not found' }));
}

const PORT = 8000; // 产品 Electron baseURL 固定端口——测试即真实链路
const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => handle(req, res, Buffer.concat(chunks).toString('utf-8')));
});

async function main() {
  esbuildBuild = esbuild.build;
  await bundleFrontend();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(PORT, '127.0.0.1', resolve);
  });
  let failures = 0;
  const piniaMod = require(path.join(FRONTEND, 'node_modules', 'pinia'));
  const test = async (name, fn) => {
    try {
      // 每个用例重置模块级单例（重新加载 bundle 拿全新实例）
      delete require.cache[require.resolve(path.join(OUT_DIR, 'live-bundle.cjs'))];
      piniaMod.setActivePinia(piniaMod.createPinia()); // Node 环境外用 store 需显式激活
      serverState.startRequests = [];
      serverState.stopRequests = [];
      serverState.destroyedOnce = false;
      serverState.mode = 'normal';
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failures += 1;
      console.error(`not ok - ${name}\n  ${err && err.message}`);
    }
  };

  // ---------- 1. 新意图取票 + 票据头 ----------
  await test('新控制意图经同一实例取票并携带票据头', async () => {
    const { useLiveStore } = require(path.join(OUT_DIR, 'live-bundle.cjs'));
    const store = useLiveStore();
    const before = serverState.ticketFetches;
    const r = await store.startLive('手动区', 60);
    assert.equal(r.success, true, `startLive 应成功：${r.message}`);
    assert.equal(serverState.ticketFetches, before + 1, '应恰好取票一次');
    assert.equal(serverState.startRequests.length, 1);
    assert.match(serverState.startRequests[0].ticket, new RegExp(`^${BOOT}:0:\\d+$`),
      'start 必须携带服务端签发票据');
  });

  // ---------- 2. 传输层失败重试复用同一票据 ----------
  await test('传输层失败经同一实例重试并复用同一票据', async () => {
    const { useLiveStore } = require(path.join(OUT_DIR, 'live-bundle.cjs'));
    const store = useLiveStore();
    serverState.mode = 'destroy_once';
    const before = serverState.ticketFetches;
    const r = await store.startLive('手动区', 60);
    assert.equal(r.success, true, `重试后应成功：${r.message}`);
    assert.equal(serverState.ticketFetches, before + 1, '重试不得重新取票（复用同票）');
    assert.equal(serverState.startRequests.length, 2, '首连硬断 + 重试 = 两次请求');
    assert.equal(serverState.startRequests[0].ticket, serverState.startRequests[1].ticket,
      '两次请求必须携带同一票据');
  });

  // ---------- 3. 取消链：stopLive 打断在途开播 ----------
  await test('stopLive 取消在途开播：取消不重试，停止用新票据', async () => {
    const { useLiveStore } = require(path.join(OUT_DIR, 'live-bundle.cjs'));
    const store = useLiveStore();
    serverState.mode = 'delay_start';
    const startPromise = store.startLive('手动区', 60);
    await new Promise((r) => setTimeout(r, 400)); // 让 start 真正在途
    const startSeen = serverState.startRequests.length;
    assert.equal(startSeen, 1, 'start 应已到达服务端');
    const stopResult = await store.stopLive(); // 应先取消在途 start
    const r = await startPromise;
    assert.equal(r.success, false, '被取消的开播必须返回失败');
    assert.match(r.message, /取消/, `取消语义要明确：${r.message}`);
    assert.equal(serverState.startRequests.length, startSeen,
      '取消后不得重试开播（否则等于复活被取消的操作）');
    assert.equal(stopResult.success, true, 'stop 应成功');
    assert.equal(serverState.stopRequests.length, 1);
    assert.match(serverState.stopRequests[0].ticket, new RegExp(`^${BOOT}:0:\\d+$`),
      'stop 是新意图：必须带自己的票据');
  });

  // ---------- 4. 409 过期重放：明确失败，不换票重发 ----------
  await test('409 过期重放明确失败且不用新票据重发', async () => {
    const { useLiveStore } = require(path.join(OUT_DIR, 'live-bundle.cjs'));
    const store = useLiveStore();
    serverState.mode = 'stale_409';
    const before = serverState.ticketFetches;
    const r = await store.startLive('手动区', 60);
    assert.equal(r.success, false, '过期操作必须失败');
    assert.equal(serverState.ticketFetches, before + 1, '只允许取过一次票');
    assert.equal(serverState.startRequests.length, 1, '409 后不得用新票据重发');
    assert.ok(toasts.some(([lvl, m]) => lvl === 'error' && /已停止取消|已失效/.test(m)),
      `应给出明确错误提示（实际 toasts=${JSON.stringify(toasts)}）`);
  });

  // Explicitly tear down keep-alive sockets and the delayed-start stub so the
  // real-chain gate has a clean process exit on Node 24/Windows as well.
  server.closeAllConnections?.();
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 250);
    server.close(() => { clearTimeout(timer); resolve(); });
  });
  if (failures > 0) process.exit(1);
  console.log(`frontend chain: 4/4 passed`);
  process.exit(0);
}

main().catch((err) => {
  console.error('fatal:', err);
  process.exit(1);
});
