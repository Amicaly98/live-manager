/**
 * main.ts - Electron 主进程
 *
 * 功能：
 * 1. 启动/管理 Python 后端子进程
 * 2. 创建 BrowserWindow 加载前端
 * 3. 实现系统托盘
 * 4. IPC 通信（前后端桥梁）
 */

import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell } from 'electron';
import { autoUpdater } from 'electron-updater';
import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

// 开发/生产环境判断
const isDev = process.env.NODE_ENV === 'development' || process.argv.includes('--dev');
const isPackaged = app.isPackaged;

// 去掉默认菜单栏（File, Edit, View 等）
Menu.setApplicationMenu(null);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let pythonProcess: ChildProcess | null = null;
let _forceQuit = false;

// 后端地址
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8000;

// ==================== 后端管理 ====================

/** 释放被占用的端口（仅处理 LISTENING 状态的 Python 进程） */
function freePort(port: number): void {
  if (process.platform !== 'win32') return
  try {
    const { execSync } = require('child_process')
    // 只查找 LISTENING 状态的连接
    const output = execSync(`netstat -ano | findstr LISTENING | findstr :${port}`, {
      encoding: 'buffer', timeout: 3000
    })
    const text = output.toString('utf-8')
    const lines = text.trim().split('\n').filter(Boolean)
    for (const line of lines) {
      const parts = line.trim().split(/\s+/)
      const pid = parseInt(parts[parts.length - 1], 10)
      // 跳过系统 PID
      if (!pid || pid <= 4) continue
      console.log(`[Electron] Releasing port ${port} (PID ${pid})...`)
      try { execSync(`taskkill /PID ${pid} /F`, { timeout: 3000, encoding: 'buffer' }) } catch { /* ignore */ }
    }
  } catch { /* 端口空闲 */ }
}

function getBackendPath(): string {
  // 开发时在 backend 目录，打包后可能内嵌 python
  if (isDev) {
    return path.join(__dirname, '..', 'backend', 'run.py');
  }
  // 打包后，假设 python 解压到 resources 下
  const resourcePath = process.resourcesPath || path.join(__dirname, '..', '..');
  return path.join(resourcePath, 'backend', 'run.py');
}

function startBackend(): void {
  const backendScript = getBackendPath();
  console.log(`[Electron] Starting backend: ${backendScript}`);

  // 释放可能被占用的端口
  freePort(BACKEND_PORT);

  // 如果后端脚本不存在，可能已经打包成 exe
  if (!fs.existsSync(backendScript)) {
    console.log('[Electron] 后端脚本不存在，尝试直接启动打包后的后端服务');
    // 假设打包时已内置后端
    const bundledBackend = path.join(process.resourcesPath || '', 'backend', 'run.exe');
    if (fs.existsSync(bundledBackend)) {
      pythonProcess = spawn(bundledBackend, ['--mode', 'service'], {
        stdio: ['pipe', 'pipe', 'pipe']
      });
    } else {
      console.error('[Electron] 后端程序不存在，请先打包后端');
      return;
    }
  } else {
    // 使用系统 python 启动
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
    pythonProcess = spawn(pythonCmd, [backendScript, '--mode', 'service'], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
    });
  }

  if (!pythonProcess) return;

  pythonProcess.stdout?.on('data', (data: Buffer) => {
    console.log(`[Python] ${data.toString('utf-8').trim()}`);
  });

  pythonProcess.stderr?.on('data', (data: Buffer) => {
    console.error(`[Python] ${data.toString('utf-8').trim()}`);
  });

  pythonProcess.on('close', (code: number | null) => {
    console.log(`[Python] 后端进程退出，code=${code}`);
    pythonProcess = null;
    // 如果是意外退出，可以尝试重启
    if (code !== 0 && !(app as any).isQuitting) {
      console.log('[Electron] 后端异常退出，5秒后重启...');
      setTimeout(startBackend, 5000);
    }
  });

  pythonProcess.on('error', (err: Error) => {
    console.error('[Electron] 启动后端失败:', err.message);
  });
}

async function stopBackend(graceful: boolean = true): Promise<void> {
  if (pythonProcess) {
    if (graceful) {
      console.log('[Electron] 通知后端正常退出...');
      try {
        await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/shutdown`, { method: 'POST' }).catch(() => {});
        await new Promise(r => setTimeout(r, 1000));
      } catch { /* 后端可能已退出 */ }
    }

    console.log('[Electron] 停止后端进程...');
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(pythonProcess.pid), '/f', '/t']);
    } else {
      pythonProcess.kill('SIGTERM');
    }
    pythonProcess = null;
  }
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

  // 重启后端
  ipcMain.handle('restart-backend', () => {
    stopBackend();
    setTimeout(startBackend, 1000);
    return { success: true };
  });

  // 获取后端状态
  ipcMain.handle('get-backend-status', () => {
    return {
      running: pythonProcess !== null,
      pid: pythonProcess?.pid || null
    };
  });

  // 确认关闭
  ipcMain.handle('confirm-quit', async (_event, stopLive: boolean) => {
    _forceQuit = true
    if (stopLive) {
      try {
        await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/live/stop`, { method: 'POST' });
        await new Promise(r => setTimeout(r, 500));
      } catch { /* */ }
      await stopBackend(true);
    } else {
      await stopBackend(false);
    }
    app.quit();
  });

  // 强制退出（不弹确认，直接杀）
  ipcMain.on('force-quit', () => {
    _forceQuit = true
    app.quit()
  })

  // 选择文件/目录
  ipcMain.handle('select-file', async (event, options: { filters?: { name: string; extensions: string[] }[] }) => {
    const { dialog } = require('electron');
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
      filters: options.filters || [{ name: '所有文件', extensions: ['*'] }]
    });
    return result.filePaths[0] || null;
  });

  ipcMain.handle('select-directory', async () => {
    const { dialog } = require('electron');
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory']
    });
    return result.filePaths[0] || null;
  });

  // 通知渲染进程
  ipcMain.handle('show-notification', (event, { title, body }: { title: string; body: string }) => {
    const { Notification } = require('electron');
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

/** 轮询等待后端就绪（超时 30s） */
async function waitForBackend(maxRetries: number = 30, interval: number = 1000): Promise<boolean> {
  const http = require('http') as typeof import('http')
  for (let i = 0; i < maxRetries; i++) {
    try {
      await new Promise<void>((resolve, reject) => {
        const req = http.get(`http://${BACKEND_HOST}:${BACKEND_PORT}/api/health`, (res: any) => {
          if (res.statusCode === 200) resolve()
          else reject(new Error(`status ${res.statusCode}`))
        })
        req.on('error', reject)
        req.setTimeout(2000, () => { req.destroy(); reject(new Error('timeout')) })
      })
      console.log(`[Electron] Backend ready (attempt ${i + 1})`)
      return true
    } catch {
      if (i === 0) console.log('[Electron] Waiting for backend...')
      await new Promise(r => setTimeout(r, interval))
    }
  }
  console.error('[Electron] Backend failed to start within timeout')
  return false
}

app.whenReady().then(async () => {
  setupIPC();
  startBackend();
  
  // 等后端就绪后再加载窗口
  const backendReady = await waitForBackend()
  if (backendReady) {
    createWindow();
    createTray();
  } else {
    // 后端未就绪也显示窗口（允许查看缓存的离线数据）
    createWindow();
  }

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

app.on('before-quit', () => {
  (app as any).isQuitting = true;
  stopBackend();
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