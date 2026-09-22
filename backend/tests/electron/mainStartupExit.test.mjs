/**
 * mainStartupExit.test.mjs - desktop main-process ownership contracts.
 *
 * These tests intentionally inspect the small integration seam in main.ts:
 * Electron itself is not launched here, so the test remains local and cannot
 * touch a real userData directory, platform account, SMTP, or public network.
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

  const noLockBranch = source.slice(lockCall, readyCall);
  assert.match(noLockBranch, /if\s*\(!gotLock\)[\s\S]*app\.quit\(\)/);
  assert.doesNotMatch(
    noLockBranch,
    /lifecycle\.start\(\)|createWindow\(\)|createTray\(\)/,
    'the no-lock path must not reach backend or window creation',
  );
});

test('E2/D2: every managed exit waits for owned backend cleanup', () => {
  const beforeQuit = source.slice(source.indexOf("app.on('before-quit'"));
  assert.match(beforeQuit, /event\.preventDefault\(\)/, 'before-quit must hold Electron while cleanup runs');
  assert.match(
    source,
    /force-quit[\s\S]*requestManagedQuit\(false,\s*\d+\)/,
    'force-quit must reclaim the local backend without stopping the platform stream',
  );
});
