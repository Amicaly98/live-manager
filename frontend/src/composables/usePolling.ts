/**
 * usePolling — 通用轮询 composable
 *
 * 封装 setInterval 轮询逻辑，自动管理生命周期（组件卸载时清理）。
 *
 * @param fn       轮询回调，返回 true 时自动停止
 * @param interval 轮询间隔（毫秒），默认 2000
 * @returns { start, stop } 控制方法
 */
import { onUnmounted, ref } from 'vue'

export function usePolling(
  fn: () => Promise<boolean | void> | boolean | void,
  interval: number = 2000,
) {
  const isRunning = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null

  function start() {
    if (timer) return
    isRunning.value = true
    timer = setInterval(async () => {
      try {
        const shouldStop = await fn()
        if (shouldStop) stop()
      } catch {
        // 轮询异常不中断，由回调自行处理
      }
    }, interval)
  }

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    isRunning.value = false
  }

  onUnmounted(stop)

  return { start, stop, isRunning }
}
