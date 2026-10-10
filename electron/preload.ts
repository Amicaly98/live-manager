/**
 * preload.ts - Electron 预加载脚本
 *
 * 通过 contextBridge 安全地暴露主进程 API 到渲染进程
 */

import { contextBridge, ipcRenderer } from 'electron';

// 暴露给渲染进程的 API
const electronAPI = {
  // 后端通信
  getBackendUrl: (): Promise<string> => ipcRenderer.invoke('get-backend-url'),
  restartBackend: (): Promise<{ success: boolean }> => ipcRenderer.invoke('restart-backend'),
  getBackendStatus: (): Promise<{ running: boolean; pid: number | null }> => ipcRenderer.invoke('get-backend-status'),

  // 文件选择
  selectFile: (filters?: { name: string; extensions: string[] }[]): Promise<string | null> =>
    ipcRenderer.invoke('select-file', { filters }),
  selectDirectory: (): Promise<string | null> => ipcRenderer.invoke('select-directory'),

  // 通知
  showNotification: (title: string, body: string): Promise<void> =>
    ipcRenderer.invoke('show-notification', { title, body }),

  // 自动更新
  checkForUpdates: (): Promise<{
    updateAvailable: boolean
    currentVersion?: string
    latestVersion?: string
    message?: string
  }> => ipcRenderer.invoke('check-for-updates'),
  getAppVersion: (): Promise<string> => ipcRenderer.invoke('get-app-version'),
  onUpdateDownloaded: (callback: (info: { version: string }) => void) => {
    ipcRenderer.removeAllListeners('update-downloaded')
    ipcRenderer.on('update-downloaded', (_event, info) => callback(info))
  },

  // 应用信息
  platform: process.platform,
  isElectron: true,

  // 关闭确认（自动清理旧监听器，防止重复弹窗）
  onConfirmClose: (callback: () => void) => {
    ipcRenderer.removeAllListeners('confirm-close')
    ipcRenderer.on('confirm-close', () => callback())
  },
  // 托盘退出（右键托盘 → 退出）
  onTrayQuit: (callback: () => void) => {
    ipcRenderer.removeAllListeners('tray-quit')
    ipcRenderer.on('tray-quit', () => callback())
  },
  /**
   * 优雅退出。`stopLive=true` = "停止并退出"（请后端停播并核对关房）；
   * `stopLive=false` = "不停止并退出"（不发平台 Stop，只回收自有资源）。
   *
   * 失败时**同时**给出原因：调用方据此显示"失败/待核验"并允许重试，
   * 而不是把它当成"已安全退出"。
   */
  confirmQuit: (stopLive: boolean): Promise<{ success: boolean; reason?: string }> =>
    ipcRenderer.invoke('confirm-quit', stopLive),
  /**
   * **显式**强制退出：只有在优雅退出已经失败、用户再次明确选择之后才调用。
   *
   * 它**不是**"安全退出"：后端可能仍在收尾，本应用只是不再等它。返回
   * `confirmed=false` 表示连这次放弃等待都没能落实（调用方不得据此宣称已退出）。
   */
  forceQuit: (): Promise<{ success: boolean; confirmed: boolean; reclaimed?: boolean }> =>
    ipcRenderer.invoke('force-quit'),
};

contextBridge.exposeInMainWorld('electronAPI', electronAPI);
