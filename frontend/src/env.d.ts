/// <reference types="vite/client" />

// 扩展 Window 类型，避免使用 `as any` 访问 electronAPI
declare global {
  interface Window {
    electronAPI?: {
      getBackendUrl: () => Promise<string>
      restartBackend: () => Promise<{ success: boolean }>
      getBackendStatus: () => Promise<{ running: boolean; pid: number | null }>
      selectFile: (filters?: { name: string; extensions: string[] }[]) => Promise<string | null>
      selectDirectory: () => Promise<string | null>
      showNotification: (title: string, body: string) => Promise<void>
      checkForUpdates: () => Promise<{
        updateAvailable: boolean
        currentVersion?: string
        latestVersion?: string
        message?: string
      }>
      getAppVersion: () => Promise<string>
      onUpdateDownloaded: (callback: (info: { version: string }) => void) => void
      platform: string
      isElectron: boolean
      onConfirmClose: (callback: () => void) => void
      onTrayQuit: (callback: () => void) => void
      confirmQuit: (stopLive: boolean) => Promise<void>
      forceQuit: () => void
    }
  }
}

export {}
