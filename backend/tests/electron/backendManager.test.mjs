/**
 * backendManager.test.mjs - E2/E1 生命周期状态机与端口判定（Node 原生测试）
 *
 * 运行：node --test backend/tests/electron/backendManager.test.mjs
 * 前提：先编译 electron TS（npm run build:electron-ts）
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const path = await import('node:path');
const { BackendLifecycle, parseNetstatListeners, classifyPortConflict } =
  require(path.resolve(process.cwd(), 'dist-electron/backendManager.js'));

function makeDeps(overrides = {}) {
  const log = [];
  return {
    spawnCalls: 0,
    deps: {
      async spawn() {
        this.spawnCalls = (this.spawnCalls || 0) + 1;
        return {
          pid: 1000 + this.spawnCalls,
          isAlive: () => false, // 默认立即退出
          killTree: async () => true,
        };
      },
      async requestShutdown() { return true; },
      async waitReady() { return true; },
      log: (level, message) => log.push({ level, message }),
      autoRestartDelayMs: 1,
      ...overrides,
    },
    log,
  };
}

class FakeProc {
  constructor(pid, alive = true) {
    this.pid = pid;
    this._alive = alive;
    this.killCalls = 0;
  }
  isAlive() { return this._alive; }
  async killTree() { this.killCalls += 1; this._alive = false; return true; }
}

test('E2: start 拒绝 starting/ready/stopping 状态下的重复启动', async () => {
  const { deps } = makeDeps();
  let proc = new FakeProc(1);
  const lc = new BackendLifecycle({
    ...deps,
    async spawn() { return proc; },
    async waitReady() { await new Promise(r => setTimeout(r, 50)); return true; },
  });
  const first = lc.start();
  await new Promise(r => setTimeout(r, 10)); // 让它进入 starting
  const second = await lc.start();
  assert.equal(second.started, false, 'starting 期间不得重复启动');
  await first;
  const third = await lc.start();
  assert.equal(third.started, false, 'ready 期间不得重复启动');
});

test('E2: restart 取消挂起的自动重启并先回收旧进程', async () => {
  let spawnCount = 0;
  let current = new FakeProc(1);
  const lc = new BackendLifecycle({
    async spawn() { spawnCount += 1; current = new FakeProc(1000 + spawnCount); return current; },
    async requestShutdown() { return true; },
    async waitReady() { return true; },
    log: () => {},
    autoRestartDelayMs: 5,
  });
  await lc.start();
  current._alive = false;
  // 非零退出 → 安排自动重启
  lc.handleClose(1);
  // 立刻手动 restart：必须取消挂起的定时器并完整走 stop→start
  const ok = await lc.restart();
  assert.equal(ok, true);
  await new Promise(r => setTimeout(r, 30)); // 若定时器未被取消，会多 spawn
  assert.equal(spawnCount, 2, 'restart 后只应有新一代，旧定时器必须被取消');
});

test('E2: 旧进程未确认回收时 restart 失败且不拉新代', async () => {
  const stub = new FakeProc(1, true);
  stub.killTree = async () => false; // 杀不掉
  let spawnCount = 0;
  const lc = new BackendLifecycle({
    async spawn() { spawnCount += 1; return stub; },
    async requestShutdown() { return true; },
    async waitReady() { return true; },
    log: () => {},
  });
  await lc.start();
  const ok = await lc.restart(500);
  assert.equal(ok, false, '未回收不得拉起新代');
  assert.equal(spawnCount, 1);
  assert.equal(lc.getPid(), 1, '引用保留');
});

test('E2: quitting 期间 close 不自动重启', async () => {
  let spawnCount = 0;
  const proc = new FakeProc(1);
  const lc = new BackendLifecycle({
    async spawn() { spawnCount += 1; return proc; },
    async requestShutdown() { return true; },
    async waitReady() { return true; },
    log: () => {},
    autoRestartDelayMs: 5,
  });
  await lc.start();
  lc.setQuitting();
  proc._alive = false;
  lc.handleClose(1);
  await new Promise(r => setTimeout(r, 30));
  assert.equal(spawnCount, 1, '退出期间后端不得复活');
});

test('E2: stop 优雅路径调用 shutdown 且幂等', async () => {
  let shutdownCalls = 0;
  const proc = new FakeProc(1);
  const lc = new BackendLifecycle({
    async spawn() { return proc; },
    async requestShutdown() { shutdownCalls += 1; proc._alive = false; return true; },
    async waitReady() { return true; },
    log: () => {},
  });
  await lc.start();
  const r1 = await lc.stop(true);
  const r2 = await lc.stop(true);
  assert.equal(r1, true);
  assert.equal(r2, true, '重复 stop 幂等');
  assert.equal(shutdownCalls, 1, '第二次 stop 无进程，不再调用 shutdown');
});

test('D2: 退出不停播时 requestShutdown 收到 stopPlatform=false', async () => {
  const seen = [];
  const proc = new FakeProc(1);
  const lc = new BackendLifecycle({
    async spawn() { return proc; },
    async requestShutdown(stopPlatform) { seen.push(stopPlatform); proc._alive = false; return true; },
    async waitReady() { return true; },
    log: () => {},
  });
  await lc.start();
  await lc.stop(true, 1000, false);
  assert.deepEqual(seen, [false], '"不停止并退出"必须以 stopPlatform=false 请求关闭');
  // 对照：常规停止/重启走 true
  const proc2 = new FakeProc(2);
  const lc2 = new BackendLifecycle({
    async spawn() { return proc2; },
    async requestShutdown(stopPlatform) { seen.push(stopPlatform); proc2._alive = false; return true; },
    async waitReady() { return true; },
    log: () => {},
  });
  await lc2.start();
  await lc2.restart(1000);
  assert.deepEqual(seen, [false, true], 'backend-restart 保留完整停止语义');
});

test('D2: 旧代 close/error 回调不得影响新一代', async () => {
  let spawnCount = 0;
  let current = null;
  const lc = new BackendLifecycle({
    async spawn() { spawnCount += 1; current = new FakeProc(1000 + spawnCount); return current; },
    async requestShutdown() { return true; },
    async waitReady() { return true; },
    log: () => {},
    autoRestartDelayMs: 1,
  });
  await lc.start();
  const oldPid = lc.getPid();
  const oldGeneration = lc.getGeneration();
  await lc.stop(true, 500);
  await lc.start();
  const newPid = lc.getPid();
  // 旧代 close（带身份）到达：不得清新代引用、不得触发自动重启
  lc.handleClose(1, oldPid, oldGeneration);
  assert.equal(lc.getPid(), newPid, '旧代 close 不得清掉新代引用');
  assert.equal(lc.getState(), 'ready', '旧代 close 不得改变新代状态');
  await new Promise(r => setTimeout(r, 20));
  assert.equal(spawnCount, 2, '旧代 close 不得触发新代自动重启');
  // 旧代 error 同理（只记日志，不改状态）
  lc.handleError(new Error('old pipe'), oldPid, oldGeneration);
  assert.equal(lc.getState(), 'ready');
});

test('E1: parseNetstatListeners 只认指定端口的 LISTENING 行', () => {
  const sample = [
    '',
    '  TCP    127.0.0.1:8000     0.0.0.0:0    LISTENING    4321',
    '  TCP    192.168.1.5:8000   0.0.0.0:0    LISTENING    4321',
    '  TCP    127.0.0.1:8001     0.0.0.0:0    LISTENING    5555',
    '  TCP    127.0.0.1:8000     1.2.3.4:55   ESTABLISHED  9999',
    '  UDP    127.0.0.1:8000     *:*                       6666',
  ].join('\n');
  const pids = parseNetstatListeners(sample, 8000);
  assert.deepEqual(pids, [4321]);
});

test('E1/D2: classifyPortConflict 需要身份验证（PID 相同不再足以认领）', () => {
  const always = () => true;
  const never = () => false;
  // 空闲
  assert.equal(classifyPortConflict([], [{ pid: 7, command: 'x' }], always), 'none');
  // PID 在册 + 身份验证通过 → 自家旧后端
  assert.equal(
    classifyPortConflict([7, 8], [{ pid: 7, command: 'a' }, { pid: 8, command: 'b' }], always),
    'own_stale');
  // D2/PID 复用：PID 数字在册，但实时命令行与登记不一致 → 未知占用
  assert.equal(
    classifyPortConflict([7], [{ pid: 7, command: 'our-backend' }], never),
    'foreign', 'PID 复用后不得凭 PID 数字认领并 taskkill');
  // 部分在册 → 未知
  assert.equal(
    classifyPortConflict([7, 9], [{ pid: 7, command: 'a' }], always),
    'foreign');
  // 不在册 → 未知
  assert.equal(classifyPortConflict([9], [], always), 'foreign');
  // 身份查询抛错 → 按无法验证处理
  assert.equal(
    classifyPortConflict([7], [{ pid: 7, command: 'a' }], () => { throw new Error('query failed'); }),
    'foreign');
});
