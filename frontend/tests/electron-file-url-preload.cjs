const { contextBridge, ipcRenderer } = require('electron')

// Isolated smoke harness only: no product IPC or user profile is opened.
contextBridge.exposeInMainWorld('electronAPI', {
  isElectron: true,
  platform: process.platform,
  getBackendUrl: async () => 'http://127.0.0.1:8000',
  getBackendStatus: async () => ({ running: true, pid: null }),
  restartBackend: async () => ({ success: true }),
  selectFile: async () => null,
  selectDirectory: async () => null,
  showNotification: async () => {},
  checkForUpdates: async () => ({ updateAvailable: false, message: 'stub' }),
  getAppVersion: async () => '1.1.0-smoke',
  onUpdateDownloaded: () => {},
  onConfirmClose: () => {},
  onTrayQuit: (callback) => {
    ipcRenderer.removeAllListeners('smoke-tray-quit')
    ipcRenderer.on('smoke-tray-quit', () => callback())
    ipcRenderer.send('smoke-tray-ready')
  },
  confirmQuit: (stopLive) => ipcRenderer.invoke('smoke-confirm-quit', stopLive),
  forceQuit: () => ipcRenderer.send('smoke-force-quit'),
})
