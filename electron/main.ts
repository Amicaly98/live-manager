/**
 * main.ts - Electron 主进程
 *
 * 功能：
 * 1. 启动/管理 Python 后端子进程（E2：单一 owner 状态机，见 backendManager.ts）
 * 2. 创建 BrowserWindow 加载前端
 * 3. 实现系统托盘
 * 4. IPC 通信（前后端桥梁）
 *
 * E1：不再为了腾出 8000 端口杀未知进程——只有能确认属于本应用上一代
 *     后端（PID 文件匹配）的监听者才被回收，未知占用直接报错。
 * E3：后端使用统一数据目录（userData/data），通过 --data-dir 传入，
 *     工作目录设为数据目录，避免在安装目录（可能只读）写入任何文件。
 */

import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell, dialog, Notification } from 'electron';
import { autoUpdater } from 'electron-updater';
import { spawn, ChildProcess, execFile } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import {
  BackendLifecycle, OwnedProcess,
  parseNetstatListeners, classifyPortConflict,
} from './backendManager';
import type { OwnedPidRecord } from './backendManager';

// 开发/生产环境判断
const isDev = process.env.NODE_ENV === 'development' || process.argv.includes('--dev');

// 去掉默认菜单栏（File, Edit, View 等）
Menu.setApplicationMenu(null);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let _forceQuit = false;
let quitCleanup: Promise<boolean> | null = null;
let quitCleanupComplete = false;
let quitCleanupSucceeded = false;

// 后端地址
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8000;

// ==================== 数据目录（E3/D4） ====================

function getDataDir(): string {
  // userData：<用户名>/AppData/Roaming/直播控制系统/ → data/
  return path.join(app.getPath('userData'), 'data');
}

function ensureDataDir(): void {
  const dir = getDataDir();
  fs.mkdirSync(dir, { recursive: true });
  // 可写性探测（探测文件保留不删除：部分环境的删除保护会拦截 unlink）
  const probe = path.join(dir, '.write_probe');
  fs.writeFileSync(probe, 'ok');
}

function getPidFilePath(): string {
  return path.join(getDataDir(), 'backend.pid');
}

/** 读取 PID 文件（JSON：pid + 拉起时命令行）。D2：数字相同不等于身份相同。 */
function readOwnedRecords(): OwnedPidRecord[] {
  try {
    const raw = fs.readFileSync(getPidFilePath(), 'utf-8').trim();
    if (!raw) return [];
    if (raw.startsWith('{')) {
      const rec = JSON.parse(raw);
      if (Number.isInteger(rec.pid) && rec.pid > 4 && typeof rec.command === 'string') {
        return [{ pid: rec.pid, command: rec.command }];
      }
      return [];
    }
    // 兼容旧格式（纯数字）：无身份证据，视为不可验证
    const pid = parseInt(raw, 10);
    if (Number.isInteger(pid) && pid > 4) return [{ pid, command: '' }];
    return [];
  } catch {
    return [];
  }
}

function writePidFile(pid: number, command: string): void {
  try {
    fs.writeFileSync(getPidFilePath(), JSON.stringify({ pid, command }), 'utf-8');
  } catch { /* 非致命 */ }
}

function clearPidFile(): void {
  try { fs.unlinkSync(getPidFilePath()); } catch { /* 非致命 */ }
}

// ==================== E1/D2：端口冲突处理 ====================

function listPortListeners(port: number): number[] {
  if (process.platform !== 'win32') return [];
  try {
    const { execSync } = require('child_process');
    const output = execSync(`netstat -ano | findstr LISTENING | findstr :${port}`, {
      encoding: 'buffer', timeout: 3000,
    });
    return parseNetstatListeners(output.toString('utf-8'), port);
  } catch {
    return []; // findstr 无匹配时非零退出 = 端口空闲
  }
}

function killTreeByPid(pid: number): Promise<boolean> {
  return new Promise(resolve => {
    execFile('taskkill', ['/F', '/T', '/PID', String(pid)], { timeout: 8000 }, err => {
      resolve(!err);
    });
  });
}

/**
 * D2：实时查询系统里 pid 的进程命令行（身份验证证据）。
 * 查询失败/不可得一律返回 null（按"无法验证"处理，绝不回收）。
 */
function queryProcessCommandLine(pid: number): string | null {
  const { execFileSync } = require('child_process');
  try {
    // PowerShell CIM：Win11/Win10 均可用
    const out = execFileSync('powershell', [
      '-NoProfile', '-Command',
      `(Get-CimInstance Win32_Process -Filter 'ProcessId=${pid}').CommandLine`,
    ], { encoding: 'utf-8', timeout: 5000 });
    const s = String(out || '').trim();
    return s || null;
  } catch { /* 回退 wmic */ }
  try {
    const out = execFileSync('wmic', [
      'process', 'where', `processid=${pid}`, 'get', 'commandline',
    ], { encoding: 'utf-8', timeout: 5000 });
    const lines = String(out || '').split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    // wmic 输出首行是表头 "CommandLine"，第二行起是值
    const value = lines.slice(1).join(' ').trim();
    return value || null;
  } catch {
    return null;
  }
}

/** 命令行归一化比较：压空白与引号差异，忽略路径大小写 */
function commandLineMatches(recorded: string, live: string | null): boolean {
  if (!recorded || !live) return false;
  const norm = (s: string) => s.replace(/\s+/g, ' ').replace(/"/g, '').toLowerCase().trim();
  return norm(recorded) === norm(live);
}

/**
 * E1/D2：确保 8000 端口可用。
 * 返回 null = 可用（或已回收经身份验证的旧后端）；返回 string = 阻断启动的错误说明。
 */
async function ensurePortAvailable(): Promise<string | null> {
  const listeners = listPortListeners(BACKEND_PORT);
  const conflict = classifyPortConflict(listeners, readOwnedRecords(), record => {
    // PID 相同 + 实时命令行与登记命令行一致才认作自家旧后端；
    // PID 复用（别的进程顶了旧 PID）命令行不匹配 → foreign。
    return commandLineMatches(record.command, queryProcessCommandLine(record.pid));
  });
  if (conflict === 'none') return null;
  if (conflict === 'own_stale') {
    console.log(`[Electron] 端口 ${BACKEND_PORT} 由本应用上一代后端占用（身份已验证），回收中…`);
    for (const pid of listeners) {
      await killTreeByPid(pid);
    }
    // 等待端口真正释放（有界）
    for (let i = 0; i < 10; i++) {
      if (listPortListeners(BACKEND_PORT).length === 0) return null;
      await new Promise(r => setTimeout(r, 300));
    }
    return '本应用旧后端占用端口但无法回收，请重启电脑或手动结束后端进程';
  }
  const detail = listeners.join(', ');
  return `端口 ${BACKEND_PORT} 被其它程序占用（PID: ${detail}），` +
    '本应用不会强制结束无法确认身份的程序。请关闭占用该端口的程序后重试，' +
    '或联系开发者调整端口。';
}

// ==================== 后端路径（E5/D4） ====================

function getBackendCommand(): { cmd: string; args: string[] } {
  const dataDir = getDataDir();
  const dataArgs = ['--data-dir', dataDir];
  if (isDev) {
    const script = path.join(__dirname, '..', 'backend', 'run.py');
    return { cmd: process.platform === 'win32' ? 'python' : 'python3', args: [script, ...dataArgs] };
  }
  // 打包后：优先内置 run.exe（PyInstaller 产物，extraResources/backend/run.exe）
  const resourcePath = process.resourcesPath || path.join(__dirname, '..', '..');
  const bundledExe = path.join(resourcePath, 'backend', 'run.exe');
  if (fs.existsSync(bundledExe)) {
    return { cmd: bundledExe, args: [...dataArgs] };
  }
  // 兜底：随包 Python 源码 + 系统 Python（须已安装并在 PATH）
  const script = path.join(resourcePath, 'backend', 'run.py');
  return { cmd: process.platform === 'win32' ? 'python' : 'python3', args: [script, ...dataArgs] };
}

/**
 * D4：旧版本（1.0.x）数据目录候选，经 --legacy-data-dir 明确传给后端。
 * 旧版 spawn 未设 cwd → 数据落在进程工作目录（安装根）；另含 resources
 * 目录作为兜底候选。只传"存在且含已知旧数据文件"的目录。
 */
function getLegacyDataDirs(): string[] {
  const knownOldFiles = ['live_state.json', 'live_tasks.db', 'bili_cookies.json', 'settings.json'];
  const candidates: string[] = [];
  try {
    candidates.push(path.dirname(app.getPath('exe'))); // 安装根
    if (process.resourcesPath) {
      candidates.push(process.resourcesPath);
      candidates.push(path.join(process.resourcesPath, 'backend'));
    }
  } catch { /* 忽略 */ }
  return candidates.filter(dir => {
    try {
      return knownOldFiles.some(f => fs.existsSync(path.join(dir, f)));
    } catch { return false; }
  });
}

// ==================== 后端生命周期（E2） ====================

function wrapChildProcess(child: ChildProcess): OwnedProcess {
  return {
    pid: child.pid ?? -1,
    isAlive: () => child.pid !== undefined && child.exitCode === null && !child.killed,
    killTree: () => new Promise<boolean>(resolve => {
      if (process.platform === 'win32' && child.pid !== undefined) {
        execFile('taskkill', ['/F', '/T', '/PID', String(child.pid)], { timeout: 8000 }, err => {
          resolve(!err);
        });
      } else {
        try { child.kill('SIGTERM'); resolve(true); } catch { resolve(false); }
      }
    }),
  };
}

const lifecycle = new BackendLifecycle({
  async spawn(): Promise<OwnedProcess> {
    const portErr = await ensurePortAvailable();
    if (portErr) throw new Error(portErr);
    const { cmd, args } = getBackendCommand();
    // D4：旧数据目录候选明确传给后端（不猜测、不全盘扫描）
    const legacyDirs = getLegacyDataDirs();
    if (legacyDirs.length > 0) {
      args.push('--legacy-data-dir', legacyDirs.join(path.delimiter));
    }
    const fullCommand = [cmd, ...args].join(' ');
    console.log(`[Electron] Starting backend: ${fullCommand}`);
    const child = spawn(cmd, args, {
      cwd: getDataDir(), // E3：工作目录=数据目录，杜绝安装目录写入
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    });
    // D2：PID 文件记录身份证据（PID + 拉起命令行）
    writePidFile(child.pid ?? -1, fullCommand);
    const generationAtSpawn = lifecycle.getGeneration(); // start() 已先自增代际
    child.stdout?.on('data', (data: Buffer) => {
      console.log(`[Python] ${data.toString('utf-8').trim()}`);
    });
    child.stderr?.on('data', (data: Buffer) => {
      console.error(`[Python] ${data.toString('utf-8').trim()}`);
    });
    child.on('close', (code: number | null) => {
      // D2：close 携带 owner 身份；只有当前代的 close 才清 PID 文件
      if (child.pid !== undefined && child.pid === lifecycle.getPid()
          && generationAtSpawn === lifecycle.getGeneration()) {
        clearPidFile();
      }
      lifecycle.handleClose(code, child.pid ?? null, generationAtSpawn);
    });
    child.on('error', (err: Error) => lifecycle.handleError(err, child.pid ?? null, generationAtSpawn));
    return wrapChildProcess(child);
  },
  async requestShutdown(stopPlatform: boolean): Promise<boolean> {
    try {
      // D2：stopPlatform=false → "不停止并退出"：后端只保存进度并回收
      // 自建本地进程，不调用平台下播 API（不碰 OBS 推流）
      const res = await fetch(
        `http://${BACKEND_HOST}:${BACKEND_PORT}/api/shutdown?stop_live=${stopPlatform}`, {
        method: 'POST', signal: AbortSignal.timeout(2500),
      });
      return res.ok;
    } catch {
      return false;
    }
  },
  async waitReady(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/health`, {
          signal: AbortSignal.timeout(2000),
        });
        if (res.ok) return true;
      } catch { /* not ready */ }
      await new Promise(r => setTimeout(r, 1000));
    }
    return false;
  },
  log: (level, message) => {
    if (level === 'error') console.error(message);
    else if (level === 'warn') console.warn(message);
    else console.log(message);
  },
});

/**
 * E2/D2：所有退出入口共用同一条本地 owner 收尾链。
 *
 * The first exit intent wins.  A later before-quit/force-quit event only waits
 * for the already-running cleanup, so the owned backend is never left behind
 * and a second stop request cannot race the first one.
 */
function requestManagedQuit(stopPlatform: boolean, timeoutMs: number): Promise<boolean> {
  if (quitCleanup !== null) return quitCleanup;

  _forceQuit = true;
  (app as any).isQuitting = true;
  lifecycle.setQuitting();
  quitCleanupComplete = false;
  quitCleanupSucceeded = false;

  quitCleanup = (async () => {
    if (stopPlatform) {
      try {
        await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/live/stop`, {
          method: 'POST', signal: AbortSignal.timeout(5000),
        });
      } catch { /* 平台停播失败仍继续回收本地 owner */ }
    }
    return lifecycle.stop(true, timeoutMs, stopPlatform);
  })().catch(err => {
    console.error('[Electron] 退出收尾失败:', err);
    return false;
  }).then(reclaimed => {
    quitCleanupComplete = true;
    quitCleanupSucceeded = reclaimed;
    return reclaimed;
  });
  return quitCleanup;
}

function resetFailedQuitAttempt(): void {
  quitCleanup = null;
  quitCleanupComplete = false;
  quitCleanupSucceeded = false;
  _forceQuit = false;
  (app as any).isQuitting = false;
}

function showQuitFailure(): void {
  dialog.showErrorBox(
    '无法退出',
    '本应用后端进程尚未确认退出，应用保持运行。请稍后重试退出。',
  );
}

// ==================== 窗口管理 ====================

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 925,
    minHeight: 765,
    title: '直播控制系统',
    icon: path.join(__dirname, '..', 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false, // 等加载完成再显示
  });

  // 加载前端
  if (isDev) {
    // 开发时使用 Vite 开发服务器
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools();
  } else {
    // 生产环境加载打包后的文件
    const frontendPath = path.join(__dirname, '..', 'frontend', 'dist', 'index.html');
    mainWindow.loadFile(frontendPath);
  }

  mainWindow.once('ready-to-show', () => {
    if (mainWindow) {
      mainWindow.show();
    }
  });

  mainWindow.on('close', (e) => {
    // 强制退出或托盘关闭 → 不阻止
    if (_forceQuit || (app as any).isQuitting) return
    // 关闭窗口 → 隐藏到托盘（后台运行）
    e.preventDefault()
    mainWindow?.hide()
  });

  // 阻止所有新窗口打开为外部窗口
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

// ==================== 系统托盘 ====================

function createTray(): void {
  // 优先使用 16×16 托盘图标，其次 32×32，最后用 SVG 生成
  const trayIcon16 = path.join(__dirname, '..', 'assets', 'tray-icon@16.png')
  const trayIcon32 = path.join(__dirname, '..', 'assets', 'tray-icon.png')
  let trayIcon: Electron.NativeImage

  if (fs.existsSync(trayIcon16)) {
    trayIcon = nativeImage.createFromPath(trayIcon16)
  } else if (fs.existsSync(trayIcon32)) {
    trayIcon = nativeImage.createFromPath(trayIcon32).resize({ width: 16, height: 16 })
  } else {
    // 编程生成 16×16 蓝色方块兜底图标
    trayIcon = nativeImage.createFromDataURL(
      'data:image/png;base64,' +
      'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAMklEQVQ4T2NkYPj/n4EBBJgYqAkwMjBQmTaYmpoMDAzffzIw/LkMwgwM/z8ToYdsGoDpAgCmGBqT4VkUvwAAAABJRU5ErkJggg=='
    )
  }

  tray = new Tray(trayIcon);
  tray.setToolTip('直播控制系统');

  const contextMenu = Menu.buildFromTemplate([
    { label: '显示窗口', click: () => { mainWindow?.show(); mainWindow?.focus(); } },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        if (mainWindow) {
          // 通知渲染进程，让用户确认是否停止直播
          mainWindow.show()
          mainWindow.focus()
          mainWindow.webContents.send('tray-quit')
        } else {
          void requestManagedQuit(false, 2000).then(reclaimed => {
            if (reclaimed) app.quit()
            else {
              resetFailedQuitAttempt()
              showQuitFailure()
            }
          })
        }
      }
    }
  ]);

  tray.setContextMenu(contextMenu);

  // 双击托盘显示窗口
  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show();
    }
  });
}

// ==================== IPC 通信 ====================

function setupIPC(): void {
  // 获取后端地址
  ipcMain.handle('get-backend-url', () => {
    return `http://${BACKEND_HOST}:${BACKEND_PORT}`;
  });

  // 重启后端（E2：等待旧进程回收后再拉起；旧重启定时器被取消）
  ipcMain.handle('restart-backend', async () => {
    const ok = await lifecycle.restart();
    return { success: ok };
  });

  // 获取后端状态
  ipcMain.handle('get-backend-status', () => {
    return {
      running: lifecycle.getState() === 'ready' || lifecycle.getState() === 'starting',
      state: lifecycle.getState(),
      pid: lifecycle.getPid(),
    };
  });

  // 确认关闭（E2/D2：退出收尾幂等且有总时限；停止→回收→退出）
  // D2：stopLive=false（"不停止并退出"）绝不能触发平台下播——
  // 优雅关闭带 stop_live=false，后端只保存进度并回收自建本地进程；
  // 外部 OBS 推流不受影响。
  ipcMain.handle('confirm-quit', async (_event, stopLive: boolean) => {
    const reclaimed = await requestManagedQuit(stopLive, 10000);
    if (reclaimed) app.quit();
    else {
      resetFailedQuitAttempt();
      showQuitFailure();
    }
    return { success: reclaimed };
  });

  // 强制退出（不弹确认，但仍回收本应用自建后端；不发送平台停播）
  ipcMain.on('force-quit', () => {
    void requestManagedQuit(false, 10000).then(reclaimed => {
      if (reclaimed) app.quit()
      else {
        resetFailedQuitAttempt()
        showQuitFailure()
      }
    });
  });

  // 选择文件/目录
  ipcMain.handle('select-file', async (event, options: { filters?: { name: string; extensions: string[] }[] }) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
      filters: options?.filters || [{ name: '所有文件', extensions: ['*'] }]
    });
    return result.filePaths[0] || null;
  });

  ipcMain.handle('select-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory']
    });
    return result.filePaths[0] || null;
  });

  // 通知渲染进程
  ipcMain.handle('show-notification', (event, { title, body }: { title: string; body: string }) => {
    new Notification({ title, body }).show();
  });

  // ==================== 自动更新 ====================
  ipcMain.handle('check-for-updates', async () => {
    if (isDev) return { updateAvailable: false, message: '开发环境不检查更新' }
    try {
      const result = await autoUpdater.checkForUpdatesAndNotify()
      return {
        updateAvailable: !!result?.updateInfo,
        currentVersion: app.getVersion(),
        latestVersion: result?.updateInfo?.version || app.getVersion(),
      }
    } catch (err) {
      return { updateAvailable: false, message: '检查更新失败' }
    }
  })

  ipcMain.handle('get-app-version', () => app.getVersion())
}

// ==================== 应用生命周期 ====================

async function initializeAfterReady(): Promise<void> {
  // A quit can arrive before Electron resolves whenReady().  Do not let a
  // late ready callback create a window or start a backend after ownership has
  // already moved to the exit path.
  if (_forceQuit || lifecycle.isQuitting()) return;

  setupIPC();

  // E3：初始化数据目录（失败则提示并继续——后端会给健康检查错误）
  try {
    ensureDataDir();
  } catch (err) {
    dialog.showErrorBox('数据目录不可用', `无法创建或写入数据目录：\n${getDataDir()}\n\n${err}`);
  }

  if (_forceQuit || lifecycle.isQuitting()) return;

  // E1/E2：端口可用性预检（未知占用 → 明确报错，不杀进程）
  const portErr = await ensurePortAvailable();
  if (portErr) {
    dialog.showErrorBox('端口被占用', portErr);
    // A foreign listener blocks this instance.  Do not continue into a
    // second spawn attempt or create a misleading window after the error.
    const reclaimed = await requestManagedQuit(false, 2000);
    if (reclaimed) app.quit();
    else {
      resetFailedQuitAttempt();
      showQuitFailure();
    }
    return;
  }

  if (_forceQuit || lifecycle.isQuitting()) return;

  await lifecycle.start();

  if (_forceQuit || lifecycle.isQuitting()) return;

  createWindow();
  createTray();

  // 配置自动更新（仅生产环境）
  if (!isDev) {
    autoUpdater.autoDownload = true
    autoUpdater.autoInstallOnAppQuit = true

    // 更新事件日志
    autoUpdater.on('checking-for-update', () => console.log('[Update] 检查更新中...'))
    autoUpdater.on('update-available', (info) => console.log('[Update] 发现新版本:', info.version))
    autoUpdater.on('update-not-available', () => console.log('[Update] 已是最新版本'))
    autoUpdater.on('download-progress', (p) => console.log(`[Update] 下载进度: ${Math.round(p.percent)}%`))
    autoUpdater.on('update-downloaded', () => {
      console.log('[Update] 更新已下载，将在退出时安装')
      if (mainWindow) {
        mainWindow.webContents.send('update-downloaded', { version: autoUpdater.currentVersion })
      }
    })
    autoUpdater.on('error', (err) => console.error('[Update] 更新错误:', err.message))
  }

  app.on('activate', () => {
    if (mainWindow === null) {
      createWindow();
    } else {
      mainWindow.show();
    }
  });
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// E2/D2：before-quit 兜底——正常退出路径（confirm-quit）已完整收尾；
// 其它退出来源（window-all-closed、更新安装）在此标记退出并尽力回收。
// 属无人值守路径：不发送平台命令（stop_live=false），只回收本地进程。
app.on('before-quit', (event) => {
  // A second before-quit can arrive while the first cleanup is still
  // pending.  Keep Electron blocked until that same owner chain completes;
  // only the final app.quit() from the completion callback is allowed through.
  if (quitCleanup !== null) {
    if (!quitCleanupComplete || !quitCleanupSucceeded) event.preventDefault();
    return;
  }
  event.preventDefault();
  // 无人值守退出不发送平台停播，只回收本应用自建的后端进程树。
  void requestManagedQuit(false, 2000).then(reclaimed => {
    if (reclaimed) app.quit()
    else {
      resetFailedQuitAttempt()
      showQuitFailure()
    }
  });
});

// 防止多个实例
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  // The lock must be acquired before registering this callback.  In a second
  // instance, app.quit() above therefore cannot run startup/port/backend code.
  app.whenReady().then(initializeAfterReady);
}
