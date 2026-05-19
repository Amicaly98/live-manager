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
  confirmQuit: (stopLive: boolean): Promise<void> =>
    ipcRenderer.invoke('confirm-quit', stopLive),
  forceQuit: (): void =>
    ipcRenderer.send('force-quit'),
};

contextBridge.exposeInMainWorld('electronAPI', electronAPI);