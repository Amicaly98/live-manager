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
 *
 * 桌面移植阶段 2（单实例与退出）：
 *   - **单实例锁必须是模块里第一件副作用**：它早于端口处理、后端启动与窗口
 *     创建。第二份进程拿不到锁就直接退出，因此不可能出现第二个 Python/run.exe、
 *     第二次账号绑定或第二份本地媒体。
 *   - 重复启动只**唤起已有窗口**：覆盖"启动尚未完成""窗口隐藏在托盘""正在退出"
 *     三种情况——没有窗口可唤起时也只是安静退出，绝不顺手把初始化跑完。
 *   - 所有退出入口（托盘退出 / 更新安装 / 系统退出 / before-quit）共用同一条
 *     `BackendLifecycle` 收尾链；"停止并退出"与"不停止并退出"只在
 *     `stop_live` 上分叉，其余回收逻辑完全一致。
 *   - 优雅退出未确认时**不**静默强杀：保留后端与账号占用，返回原因给页面重试。
 *     只有用户在失败之后**再显式**点一次"不等待后端确认"，才会放弃等待，并且
 *     这条路径不会对外宣称"安全退出"。
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

// ==================== 单实例锁（必须最先执行） ====================
//
// 放在任何副作用之前：端口探测、后端 spawn、BrowserWindow/PID 文件写入都属于
// "这一份实例已经开始占用资源"，必须在确认自己是唯一实例之后才发生。
// 应用标识（appId/userData）保持既有值不变——靠改目录绕过锁不在本轮范围里。
const gotSingleInstanceLock = app.requestSingleInstanceLock();

// 开发/生产环境判断
const isDev = process.env.NODE_ENV === 'development' || process.argv.includes('--dev');

// 去掉默认菜单栏（File, Edit, View 等）
Menu.setApplicationMenu(null);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let _forceQuit = false;
/**
 * 用户已**显式**放弃等待后端确认（阶段 2）。
 *
 * 只在优雅退出失败之后的"仍要退出"里被置位：它让 `before-quit` 直接放行，
 * 不再把应用挡在"等收尾确认"这一步。它不是"收尾已确认"，日志与页面都不得把
 * 这条路径说成安全退出。
 */
let _abandonBackendWait = false;
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

/**
 * 账号数据根（桌面移植阶段 1）：任务/进度/历史按 UID 归属，后端需要显式传入。
 * 已有明确配置（环境变量 ACCOUNT_DATA_ROOT）优先；缺省用 userData/account-data，
 * 与后端 run.py 的默认推导一致。不改 userData 路径，也不扫描或接管服务器数据。
 */
function getAccountRoot(): string {
  const configured = process.env.ACCOUNT_DATA_ROOT;
  if (configured && configured.trim()) return configured;
  return path.join(app.getPath('userData'), 'account-data');
}

function getBackendCommand(): { cmd: string; args: string[] } {
  const dataDir = getDataDir();
  const dataArgs = ['--data-dir', dataDir, '--account-root', getAccountRoot()];
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
        // Controller cleanup can wait for owned FFmpeg and persist the task.
        // A 2.5 s timeout raced that work and led the caller to killTree while
        // the backend was still finishing a valid shutdown request.
        method: 'POST', signal: AbortSignal.timeout(25000),
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

  // 注意：这里**不**置 _forceQuit。退出尝试尚未确认成功前，窗口必须保住——
  // "停止并退出"失败时页面还要显示失败/待核验并提供重试（阶段 2 契约）；
  // 窗口若在尝试一开始就被放行关闭，失败后应用只剩托盘，用户看不到任何交代。
  // 成功路径由 confirm-quit 在 reclaimed 后调用 app.quit()，届时
  // quitCleanupSucceeded 已置位，close 处理器自然放行。
  (app as any).isQuitting = true;
  lifecycle.setQuitting();
  quitCleanupComplete = false;
  quitCleanupSucceeded = false;

  quitCleanup = (async () => {
    // A separate /api/live/stop runs on a background executor.  If its HTTP
    // response times out, /api/shutdown can race its state save and kill the
    // backend mid-cleanup.  The shutdown endpoint owns the entire stop/quit
    // transaction and must report completion before Electron exits.
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
  lifecycle.setQuitting(false);
}

/**
 * 优雅退出失败的原因（给 `confirm-quit` 回报页面，也用于原生提示框）。
 *
 * 措辞区分两种退出，并且**不**暗示"已经退出成功"：本地资源未收齐时后端仍在
 * 运行，账号占用也还没释放——这正是"停止并退出"要保住的东西。
 */
function describeQuitFailure(stopLive: boolean): string {
  const base = stopLive
    ? '平台停止或本地资源收尾尚未被后端确认'
    : '本地后端进程尚未确认收尾';
  return `${base}。应用保持运行：未销毁后端、未提前释放账号占用，可稍后重试退出。`;
}

function showQuitFailure(reason?: string): void {
  // 阶段 2：失败 UI 的**唯一**权威是页面上的"退出未完成"横幅（含重试）。
  // 这里**不再**弹原生 showErrorBox——原生模态会阻塞 confirm-quit 的 IPC 回包，
  // 页面永远等不到结果、横幅永远渲染不出来，"页面保留并显示失败/待核验"
  // 就成了一句空话。主进程只负责把原因留给页面与日志。
  console.warn('[quit] 退出未确认收齐，保留后端与账号占用，原因：', reason || describeQuitFailure(false));
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
    // 只有"确认收齐后的退出"或**显式**强杀才放行窗口关闭；普通关闭进托盘，
    // 退出尝试期间也保住窗口（失败后页面要显示失败/待核验并提供重试）。
    if (_forceQuit || quitCleanupSucceeded) return
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
  //
  // 两种退出语义只在 `stopLive` 上分叉，其余回收逻辑完全一致：
  //   - stopLive=true（"停止并退出"）：`/api/shutdown?stop_live=true`，后端经同一
  //     ControlService 停播并核对关房，再收本地资源；
  //   - stopLive=false（"不停止并退出"）：`stop_live=false`，**不**发送平台 Stop，
  //     只回收自有本地媒体与后端并保留恢复依据；外部 OBS、外来 FFmpeg 与身份
  //     不明的端口占用者一个都不碰。
  //
  // 未确认收齐时**不**静默强杀：保留后端与账号占用，把原因回报给页面（页面据此
  // 显示失败/待核验并提供重试），本进程继续运行。
  ipcMain.handle('confirm-quit', async (_event, stopLive: boolean) => {
    const reclaimed = await requestManagedQuit(stopLive, 10000);
    if (reclaimed) {
      app.quit();
      return { success: true };
    }
    const reason = describeQuitFailure(stopLive === true);
    // 收尾未确认：解除"正在退出"，让**下一次**退出动作还能真实发起（否则后续
    // 重试会被上一次失败的退出态挡住，页面只剩一个永远点不动的按钮）。
    resetFailedQuitAttempt();
    showQuitFailure(reason);
    // 退出尝试期间窗口被 hide 进了托盘；失败后必须把它带回来——
    // "页面保留并显示失败/待核验"是本条退出语义的一部分。
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
    return { success: false, reason };
  });

  /**
   * **显式**放弃等待后端确认并结束本应用（阶段 2）。
   *
   * 与"已确认安全退出"严格区分：它只在 `confirm-quit` 失败之后由用户再点一次
   * 才发生；仍然尽力回收自有后端（有界、直接杀进程树），但**不等**它的确认，
   * 也绝不对外宣称这是一次成功的收尾。未回收成功时如实记日志与回报。
   */
  ipcMain.handle('force-quit', async () => {
    console.warn('[Electron] 用户选择不等待后端确认：直接结束本应用'
      + '（这不是已确认的安全退出，后端可能仍在收尾）');
    _abandonBackendWait = true;
    _forceQuit = true;
    (app as any).isQuitting = true;
    lifecycle.setQuitting();
    // graceful=false：跳过 HTTP 关房请求，直接杀本应用**自建**的进程树；
    // stopPlatform=false：这条路径同样不发送平台 Stop。
    const reclaimed = await lifecycle.stop(false, 3000, false);
    if (!reclaimed) {
      console.warn('[Electron] 放弃等待：自有后端进程树在 3 秒内未确认退出');
    }
    app.quit();
    return { success: true, confirmed: true, reclaimed };
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
  // 用户已**显式**放弃等待后端确认（"仍要退出"）：直接放行，不再把应用挡在
  // "等收尾确认"这一步。这条路径不是安全退出，别处不得把它记成成功收尾。
  if (_abandonBackendWait) return;
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

/**
 * 第二次启动：**只唤起已有窗口**，不创建第二份资源。
 *
 * 拿不到单实例锁的进程走的是 `app.quit()`（见文件末尾），根本不会执行到本
 * 模块的初始化——所以这里不存在"第二份 Python/run.exe、第二次账号绑定或第二份
 * 本地媒体"。本回调只负责把**已有**那一份实例的窗口叫出来。
 *
 * 三种情况分别处理：
 *  - 正在退出：应用马上就结束了，唤起窗口没有意义，也不该打断退出事务；
 *  - 启动尚未完成（窗口还没建出来）：已有实例仍在初始化，本进程随后退出即可，
 *    绝不能"顺手把初始化跑完"（那会真的产生第二份后端）；
 *  - 窗口存在（含最小化、隐藏在托盘）：恢复 → 显示 → 聚焦。
 */
function handleSecondInstance(): void {
  if (_forceQuit || lifecycle.isQuitting()) {
    console.log('[Electron] 收到重复启动：本实例正在退出，不唤起窗口');
    return;
  }
  if (mainWindow === null) {
    console.log('[Electron] 收到重复启动：本实例窗口尚未创建（启动未完成），'
      + '由已运行的实例继续负责');
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  if (!mainWindow.isVisible()) mainWindow.show();
  mainWindow.focus();
}

if (!gotSingleInstanceLock) {
  // 已有实例在运行：这一份立刻退出。锁在模块顶部就已经申请过，因此这里之前
  // 没有任何端口探测、后端启动或窗口创建。
  console.log('[Electron] 已有实例在运行，本进程退出（单实例锁）');
  app.quit();
} else {
  app.on('second-instance', handleSecondInstance);
  app.whenReady().then(initializeAfterReady);
}
