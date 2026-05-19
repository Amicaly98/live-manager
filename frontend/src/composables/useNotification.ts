/**
 * useNotification — 跨平台通知 composable
 *
 * Electron 环境：通过 preload API 发送系统原生通知
 * 浏览器环境：降级为 Element Plus ElMessage
 */
import { ElMessage } from 'element-plus'

type NotifyType = 'success' | 'warning' | 'error' | 'info'

export function useNotification() {
  /** 发送通知 */
  function notify(title: string, body: string, type: NotifyType = 'info') {
    // Electron 环境：系统通知
    if (window.electronAPI) {
      window.electronAPI.showNotification(title, body)
    }
    // 同时显示应用内提示
    switch (type) {
      case 'success': ElMessage.success(`${title}：${body}`); break
      case 'warning': ElMessage.warning(`${title}：${body}`); break
      case 'error':   ElMessage.error(`${title}：${body}`); break
      default:        ElMessage.info(`${title}：${body}`); break
    }
  }

  return { notify }
}
