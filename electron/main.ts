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

// 开发/生产环境判断
const isDev = process.env.NODE_ENV === 'development' || process.argv.includes('--dev');

// 去掉默认菜单栏（File, Edit, View 等）
Menu.setApplicationMenu(null);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let _forceQuit = false;

// 后端地址
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8000;

// ==================== 数据目录（E3） ====================

function getDataDir(): string {
  // userData：<用户名>/AppData/Roaming/直播控制系统/ → data/
  return path.join(app.getPath('userData'), 'data');
}

function ensureDataDir(): void {
  const dir = getDataDir();
  fs.mkdirSync(dir, { recursive: true });
  // 可写性探测
  const probe = path.join(dir, '.write_probe');
  fs.writeFileSync(probe, 'ok');
  fs.unlinkSync(probe);
}

function getPidFilePath(): string {
  return path.join(getDataDir(), 'backend.pid');
}

function readOwnedPids(): number[] {
  try {
    const raw = fs.readFileSync(getPidFilePath(), 'utf-8');
    return raw.split(/\s+/).map(s => parseInt(s, 10)).filter(n => Number.isInteger(n) && n > 4);
  } catch {
    return [];
  }
}

function writePidFile(pid: number): void {
  try { fs.writeFileSync(getPidFilePath(), String(pid), 'utf-8'); } catch { /* 非致命 */ }
}

function clearPidFile(): void {
  try { fs.unlinkSync(getPidFilePath()); } catch { /* 非致命 */ }
}

// ==================== E1：端口冲突处理 ====================

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
 * E1：确保 8000 端口可用。
 * 返回 null = 可用（或已回收自己的旧后端）；返回 string = 阻断启动的错误说明。
 */
async function ensurePortAvailable(): Promise<string | null> {
  const listeners = listPortListeners(BACKEND_PORT);
  const conflict = classifyPortConflict(listeners, readOwnedPids());
  if (conflict === 'none') return null;
  if (conflict === 'own_stale') {
    console.log(`[Electron] 端口 ${BACKEND_PORT} 由本应用上一代后端占用，回收中…`);
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
    '本应用不会强制结束未知程序。请关闭占用该端口的程序后重试，' +
    '或联系开发者调整端口。';
}

// ==================== 后端路径（E5） ====================

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
    console.log(`[Electron] Starting backend: ${cmd} ${args.join(' ')}`);
    const child = spawn(cmd, args, {
      cwd: getDataDir(), // E3：工作目录=数据目录，杜绝安装目录写入
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    });
    writePidFile(child.pid ?? -1);
    child.stdout?.on('data', (data: Buffer) => {
      console.log(`[Python] ${data.toString('utf-8').trim()}`);
    });
    child.stderr?.on('data', (data: Buffer) => {
      console.error(`[Python] ${data.toString('utf-8').trim()}`);
    });
    child.on('close', (code: number | null) => {
      if (child.pid === (lifecycle.getPid() ?? -1)) clearPidFile();
      lifecycle.handleClose(code);
    });
    child.on('error', (err: Error) => lifecycle.handleError(err));
    return wrapChildProcess(child);
  },
  async requestShutdown(): Promise<boolean> {
    try {
      const res = await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/shutdown`, {
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
          _forceQuit = true
          app.quit()
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

  // 确认关闭（E2：退出收尾幂等且有总时限；停止→回收→退出）
  ipcMain.handle('confirm-quit', async (_event, stopLive: boolean) => {
    _forceQuit = true;
    lifecycle.setQuitting();
    if (stopLive) {
      try {
        await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/live/stop`, {
          method: 'POST', signal: AbortSignal.timeout(5000),
        });
      } catch { /* */ }
    }
    await lifecycle.stop(true, 10000);
    app.quit();
  });

  // 强制退出（不弹确认，直接退）
  ipcMain.on('force-quit', () => {
    _forceQuit = true;
    lifecycle.setQuitting();
    app.quit();
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

app.whenReady().then(async () => {
  setupIPC();

  // E3：初始化数据目录（失败则提示并继续——后端会给健康检查错误）
  try {
    ensureDataDir();
  } catch (err) {
    dialog.showErrorBox('数据目录不可用', `无法创建或写入数据目录：\n${getDataDir()}\n\n${err}`);
  }

  // E1/E2：端口可用性预检（未知占用 → 明确报错，不杀进程）
  const portErr = await ensurePortAvailable();
  if (portErr) {
    dialog.showErrorBox('端口被占用', portErr);
  }

  await lifecycle.start();

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
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// E2：before-quit 兜底——正常退出路径（confirm-quit）已完整收尾；
// 其它退出来源（window-all-closed、更新安装）在此标记退出并尽力回收。
app.on('before-quit', () => {
  (app as any).isQuitting = true;
  _forceQuit = true;
  if (!lifecycle.isQuitting()) {
    lifecycle.setQuitting();
    // 有界异步收尾：不阻塞退出，但给后端 2 秒优雅退出窗口
    void lifecycle.stop(true, 2000);
  }
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
}
