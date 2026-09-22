/**
 * mainLifecycle.integration.test.mjs
 *
 * Drives the compiled Electron entrypoint with local module doubles.  The
 * entrypoint is evaluated as the real dist-electron/main.js would be, while
 * Electron, child_process, HTTP, and filesystem calls stay in-process and
 * synthetic.  No userData, backend, platform account, SMTP, or public
 * network is touched.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const compiledPath = path.resolve(process.cwd(), 'dist-electron/main.js');
const compiledSource = fs.readFileSync(compiledPath, 'utf8');

function loadMain({
  lock,
  unknownPort = false,
  readyImmediate = true,
  holdCleanup = false,
  initialOwner = false,
  stopResults = [],
}) {
  const calls = {
    whenReady: 0,
    quit: 0,
    start: 0,
    stop: [],
    requestShutdown: [],
    fetch: [],
    spawn: 0,
    dialogs: [],
  };
  const listeners = new Map();
  let releaseCleanup = null;
  let lifecycleInstance = null;
  const userDataPath = 'C:\\synthetic\\desktop-sync-user-data';

  const app = {
    isQuitting: false,
    on(name, callback) {
      const bucket = listeners.get(name) || [];
      bucket.push(callback);
      listeners.set(name, bucket);
    },
    emit(name, ...args) {
      for (const callback of listeners.get(name) || []) callback(...args);
    },
    requestSingleInstanceLock() {
      return lock;
    },
    whenReady() {
      calls.whenReady += 1;
      if (readyImmediate) return Promise.resolve();
      return new Promise(() => {});
    },
    quit() {
      calls.quit += 1;
    },
    getPath(name) {
      if (name === 'userData') return userDataPath;
      if (name === 'exe') return 'C:\\synthetic\\desktop\\app.exe';
      throw new Error(`unexpected app path: ${name}`);
    },
    getVersion() {
      return '1.1.0-test';
    },
  };

  class FakeLifecycle {
    constructor(deps) {
      this.deps = deps;
      this.quitting = false;
      // The pending-exit scenario starts with a synthetic current owner so
      // stop() has a real cleanup promise to hold.
      this.owned = holdCleanup || initialOwner;
      this.generation = 0;
      lifecycleInstance = this;
    }
    getGeneration() { return this.generation; }
    getPid() { return this.owned ? 8123 : null; }
    isQuitting() { return this.quitting; }
    setQuitting() { this.quitting = true; }
    start() {
      calls.start += 1;
      this.generation += 1;
      this.owned = true;
      return Promise.resolve({ started: true, reason: 'ok' });
    }
    stop(graceful, timeoutMs, stopPlatform) {
      calls.stop.push({ graceful, timeoutMs, stopPlatform });
      if (!this.owned) return Promise.resolve(true);
      if (holdCleanup) {
        return new Promise(resolve => { releaseCleanup = resolve; });
      }
      const reclaimed = stopResults.length > 0 ? stopResults.shift() : true;
      if (reclaimed) this.owned = false;
      return Promise.resolve(reclaimed);
    }
    restart() { return Promise.resolve(false); }
    handleClose() {}
    handleError() {}
    getState() { return this.owned ? 'ready' : 'idle'; }
  }

  class FakeBrowserWindow {
    constructor() {
      this.webContents = {
        openDevTools() {},
        setWindowOpenHandler() {},
        send() {},
      };
    }
    loadURL() {}
    loadFile() {}
    once() {}
    on() {}
    show() {}
    hide() {}
    focus() {}
    isVisible() { return true; }
    isMinimized() { return false; }
    restore() {}
  }

  class FakeTray {
    setToolTip() {}
    setContextMenu() {}
    on() {}
  }

  const fakeElectron = {
    app,
    BrowserWindow: FakeBrowserWindow,
    ipcMain: {
      handle() {},
      on() {},
    },
    Tray: FakeTray,
    Menu: {
      setApplicationMenu() {},
      buildFromTemplate() { return {}; },
    },
    nativeImage: {
      createFromPath() { return { resize() { return this; } }; },
      createFromDataURL() { return {}; },
    },
    shell: { openExternal() {} },
    dialog: {
      showErrorBox(title, content) { calls.dialogs.push({ title, content }); },
      async showOpenDialog() { return { filePaths: [] }; },
    },
    Notification: class { show() {} },
  };

  const autoUpdater = {
    autoDownload: false,
    autoInstallOnAppQuit: false,
    currentVersion: '1.1.0-test',
    on() {},
    async checkForUpdatesAndNotify() { return null; },
  };

  const fakeFs = {
    mkdirSync() {},
    writeFileSync() {},
    readFileSync() { throw new Error('synthetic file is absent'); },
    unlinkSync() {},
    existsSync() { return false; },
  };

  const fakeChildProcess = {
    spawn() {
      calls.spawn += 1;
      throw new Error('spawn must not run in this integration harness');
    },
    execFile(_command, _args, _options, callback) { callback(new Error('synthetic')); },
    execFileSync() { throw new Error('synthetic process query unavailable'); },
    execSync() {
      if (!unknownPort) throw new Error('synthetic netstat: no listener');
      return Buffer.from('TCP 127.0.0.1:8000 0.0.0.0:0 LISTENING 4321\n');
    },
  };

  const backendManager = {
    BackendLifecycle: FakeLifecycle,
    parseNetstatListeners(output, port) {
      if (port === 8000 && output.includes('LISTENING')) return [4321];
      return [];
    },
    classifyPortConflict(listenerPids) {
      return listenerPids.length === 0 ? 'none' : 'foreign';
    },
  };

  const fakeFetch = async (...args) => {
    calls.fetch.push(args[0]);
    return { ok: true };
  };

  const module = { exports: {} };
  const fakeRequire = (request) => {
    if (request === 'electron') return fakeElectron;
    if (request === 'electron-updater') return { autoUpdater };
    if (request === 'child_process') return fakeChildProcess;
    if (request === 'path') return path;
    if (request === 'fs') return fakeFs;
    if (request === './backendManager') return backendManager;
    throw new Error(`unexpected module request: ${request}`);
  };

  const context = {
    module,
    exports: module.exports,
    require: fakeRequire,
    __dirname: path.dirname(compiledPath),
    __filename: compiledPath,
    process: {
      platform: 'win32',
      argv: [],
      env: { NODE_ENV: 'development' },
      resourcesPath: 'C:\\synthetic\\desktop\\resources',
    },
    console: { log() {}, error() {}, warn() {} },
    Buffer,
    AbortSignal,
    setTimeout,
    clearTimeout,
    fetch: fakeFetch,
  };
  vm.runInNewContext(compiledSource, context, { filename: compiledPath });

  return {
    app,
    calls,
    lifecycle: lifecycleInstance,
    releaseCleanup(value = true) {
      assert.ok(releaseCleanup, 'cleanup must be in flight before release');
      releaseCleanup(value);
    },
  };
}

async function flushMicrotasks() {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}

test('E2: no single-instance lock never resolves ready or starts the backend', async () => {
  const harness = loadMain({ lock: false, readyImmediate: true });
  await flushMicrotasks();
  assert.equal(harness.calls.whenReady, 0);
  assert.equal(harness.calls.start, 0);
  assert.equal(harness.calls.spawn, 0);
  assert.equal(harness.calls.fetch.length, 0);
  assert.equal(harness.calls.quit, 1);
});

test('E2/D2: repeated before-quit events remain blocked until owned cleanup resolves', async () => {
  const harness = loadMain({ lock: true, readyImmediate: false, holdCleanup: true });
  const first = { prevented: false, preventDefault() { this.prevented = true; } };
  const second = { prevented: false, preventDefault() { this.prevented = true; } };
  harness.app.emit('before-quit', first);
  harness.app.emit('before-quit', second);
  assert.equal(first.prevented, true);
  assert.equal(second.prevented, true);
  assert.equal(harness.calls.quit, 0);
  assert.deepEqual(harness.calls.stop, [{ graceful: true, timeoutMs: 2000, stopPlatform: false }]);

  harness.releaseCleanup();
  await flushMicrotasks();
  assert.equal(harness.calls.quit, 1, 'final app.quit occurs after cleanup resolves');
});

test('E2/D2: failed owner reclaim keeps the app open and permits a bounded retry', async () => {
  const harness = loadMain({
    lock: true,
    readyImmediate: false,
    initialOwner: true,
    stopResults: [false, true],
  });
  const first = { prevented: false, preventDefault() { this.prevented = true; } };
  harness.app.emit('before-quit', first);
  await flushMicrotasks();
  assert.equal(first.prevented, true);
  assert.equal(harness.calls.quit, 0, 'failed kill must not abandon the owned process');
  assert.equal(harness.lifecycle.owned, true);
  assert.equal(harness.calls.dialogs.length, 1, 'failed cleanup gives a short retry prompt');

  const retry = { prevented: false, preventDefault() { this.prevented = true; } };
  harness.app.emit('before-quit', retry);
  await flushMicrotasks();
  assert.equal(retry.prevented, true);
  assert.equal(harness.calls.stop.length, 2);
  assert.equal(harness.calls.quit, 1, 'only the successful bounded retry may quit');
  assert.equal(harness.lifecycle.owned, false);
});

test('E1/E2: unknown port blocks startup without HTTP shutdown or child spawn', async () => {
  const harness = loadMain({ lock: true, unknownPort: true, readyImmediate: true });
  await flushMicrotasks();
  assert.equal(harness.calls.start, 0);
  assert.equal(harness.calls.spawn, 0);
  assert.equal(harness.calls.fetch.length, 0, 'foreign port must not receive /api/shutdown');
  assert.equal(harness.calls.quit, 1);
  assert.equal(harness.calls.dialogs.length, 1);
});
