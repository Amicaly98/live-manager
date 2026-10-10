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
      /** 优雅退出；失败时给出原因（页面据此显示失败/待核验并允许重试）。 */
      confirmQuit: (stopLive: boolean) => Promise<{ success: boolean; reason?: string }>
      /**
       * 显式放弃等待后端确认并结束应用。**不是**已确认的安全退出：
       * `reclaimed=false` 表示自有后端进程树也没能确认回收。
       */
      forceQuit: () => Promise<{ success: boolean; confirmed: boolean; reclaimed?: boolean }>
    }
  }
}

export {}
