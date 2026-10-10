/**
 * mainStartupExit.test.mjs - desktop main-process ownership contracts.
 *
 * These tests intentionally inspect the small integration seam in main.ts:
 * Electron itself is not launched here, so the test remains local and cannot
 * touch a real userData directory, platform account, SMTP, or public network.
 *
 * 阶段 2 更新：单实例锁移到模块**最前**（早于任何端口处理/后端启动/窗口创建），
 * 拿不到锁的进程在文件末尾直接 `app.quit()`；`force-quit` 改为"显式放弃等待"，
 * 仍然回收自有后端但不发送平台 Stop，也不宣称"已确认安全退出"。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const mainPath = path.resolve(process.cwd(), 'electron/main.ts');
const source = fs.readFileSync(mainPath, 'utf8');

test('E2: single-instance lock gates the whenReady startup callback', () => {
  const lockCall = source.indexOf('app.requestSingleInstanceLock()');
  const readyCall = source.indexOf('app.whenReady().then');
  assert.ok(lockCall >= 0, 'main must request the single-instance lock');
  assert.ok(readyCall >= 0, 'main must register a ready callback');
  assert.ok(
    lockCall < readyCall,
    'the lock must be acquired before registering whenReady, so a second instance cannot start a backend',
  );

  // 锁是模块里**第一件副作用**：在它之前不得出现端口处理、后端启动或窗口创建。
  const beforeLock = source.slice(0, lockCall);
  assert.doesNotMatch(
    beforeLock,
    /ensurePortAvailable\(\)|lifecycle\.start\(\)|createWindow\(\)|createTray\(\)/,
    'nothing may claim ports, spawn the backend or open a window before the lock',
  );

  // 拿不到锁的分支只退出：不得顺路把初始化跑完（那会产生第二份后端）。
  const noLockBranch = source.slice(
    source.indexOf('if (!gotSingleInstanceLock)'),
    readyCall,
  );
  assert.match(noLockBranch, /app\.quit\(\)/);
  assert.doesNotMatch(
    noLockBranch,
    /lifecycle\.start\(\)|createWindow\(\)|createTray\(\)|ensurePortAvailable\(\)/,
    'the no-lock path must not reach backend or window creation',
  );
});

test('E2/阶段2: a second instance only raises the existing window', () => {
  const handler = source.slice(
    source.indexOf('function handleSecondInstance'),
    source.indexOf("app.on('second-instance'"),
  );
  assert.match(handler, /isQuitting\(\)|_forceQuit/,
    'a second launch during exit must not try to raise a window');
  assert.match(handler, /mainWindow === null/,
    'a second launch before startup finished must not initialise anything');
  assert.doesNotMatch(handler, /lifecycle\.start\(\)|createWindow\(\)|spawn\(/,
    'the second-instance handler must never create a second backend or window');
});

test('E2/D2: every managed exit waits for owned backend cleanup', () => {
  const beforeQuit = source.slice(source.indexOf("app.on('before-quit'"));
  assert.match(beforeQuit, /event\.preventDefault\(\)/,
    'before-quit must hold Electron while cleanup runs');
  // 用户显式放弃等待是唯一的例外，且必须独立成闸、不得伪装成成功收尾。
  assert.match(beforeQuit, /_abandonBackendWait/,
    'the explicit abandon-wait path must be visible in before-quit');
  assert.match(
    source,
    /force-quit[\s\S]*lifecycle\.stop\(false,\s*\d+,\s*false\)/,
    'force-quit must reclaim the local backend directly and without a platform Stop',
  );
});

test('E2/阶段2: an unconfirmed graceful quit reports failure and stays alive', () => {
  const confirmQuit = source.slice(
    source.indexOf("ipcMain.handle('confirm-quit'"),
    source.indexOf("ipcMain.handle('force-quit'"),
  );
  // 未确认收齐：不销毁 owner、不释放账号占用，把原因如实回报给页面重试。
  assert.match(confirmQuit, /success: false/, 'must report failure to the renderer');
  assert.match(confirmQuit, /reason/, 'must report the reason to the renderer');
  assert.match(confirmQuit, /resetFailedQuitAttempt\(\)/,
    'a failed attempt must release the quitting flag so a retry is possible');
  assert.match(confirmQuit, /showQuitFailure\(/,
    'the main process must surface the failure, not swallow it');
  assert.doesNotMatch(confirmQuit, /killTree\(/,
    'an unconfirmed graceful quit must not silently kill the backend');
});
