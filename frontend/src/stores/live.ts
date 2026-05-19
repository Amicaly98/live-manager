import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useRequest } from '@/api/request'
import type {
  LiveStatus,
  StartLiveResponse,
  StopLiveResponse,
  StartLiveRequest,
} from '@/types/api'

export const useLiveStore = defineStore('live', () => {
  const status = ref<LiveStatus>({
    is_streaming: false,
    current_zone: '',
    elapsed_seconds: 0,
    remaining_seconds: 0,
    room_id: 0,
    is_anomaly: false,
    ffmpeg_active: false,
    ffmpeg_current_video: '',
  })
  const isStarting = ref(false)
  const isStopping = ref(false)

  // 本地计时器状态（跨页面持久）
  const localElapsed = ref(0)
  const fixedTotal = ref(0)
  let tickTimer: ReturnType<typeof setInterval> | null = null
  let _eventPollTimer: ReturnType<typeof setInterval> | null = null

  const request = useRequest()

  // 后台事件轮询（跨页面持续运行，确保操作日志不丢失）
  function startEventPolling() {
    if (_eventPollTimer) return
    _eventPollTimer = setInterval(async () => {
      try {
        const res = await request.get<LiveStatus>('/api/live/status')
        const be = (res as any).backend_events as Array<{tag:string;type:string;message:string;time:string}> | undefined
        if (be && be.length > 0) {
          let lastTs = localStorage.getItem('lastBackendEventTs') || ''
          const stored = localStorage.getItem('app_events')
          const events = stored ? JSON.parse(stored) : []
          let added = false
          for (const e of be) {
            // 兼容旧格式：如果时间只有 HH:MM:SS，补上当天日期
            let t = e.time
            if (t && /^\d{2}:\d{2}:\d{2}$/.test(t)) {
              const d = new Date()
              const pad = (n: number) => String(n).padStart(2, '0')
              t = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${t}`
            }
            if (t > lastTs) {
              events.push({ ...e, time: t })
              lastTs = t
              added = true
            }
          }
          if (added) {
            localStorage.setItem('app_events', JSON.stringify(events.slice(-50)))
            localStorage.setItem('lastBackendEventTs', lastTs)
          }
        }
      } catch { /* silent */ }
    }, 30000)
  }

  function stopEventPolling() {
    if (_eventPollTimer) { clearInterval(_eventPollTimer); _eventPollTimer = null }
  }

  // 获取直播状态
  async function fetchStatus() {
    try {
      const res = await request.get<LiveStatus>('/api/live/status')
      status.value = res
    } catch (error) {
      console.error('获取直播状态失败:', error)
    }
  }

  // 开始直播（zoneName 非空=手动模式，空=任务模式）
  async function startLive(zoneName?: string, durationSeconds?: number) {
    isStarting.value = true
    try {
      const body: StartLiveRequest = zoneName
        ? { zone_name: zoneName, duration_seconds: durationSeconds }
        : {}
      const res = await request.post<StartLiveResponse>('/api/live/start', body)
      console.log('[startLive] API response:', { success: res.success, need_face_verification: res.need_face_verification, qr_data: res.qr_data, msg: res.message })
      if (res.success) {
        await fetchStatus()
        return { success: true, message: res.message, needFaceVerify: false, qrData: '' }
      }
      return {
        success: false,
        message: res.message,
        needFaceVerify: res.need_face_verification || false,
        qrData: res.qr_data || '',
      }
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '开播失败'
      return { success: false, message: msg, needFaceVerify: false, qrData: '' }
    } finally {
      isStarting.value = false
    }
  }

  // 停止直播
  async function stopLive() {
    isStopping.value = true
    try {
      const res = await request.post<StopLiveResponse>('/api/live/stop')
      if (res.success) {
        status.value.is_streaming = false
        status.value.current_zone = ''
        status.value.elapsed_seconds = 0
        status.value.remaining_seconds = 0
        stopLocalTick()
      }
      return res
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '停止失败'
      return { success: false, message: msg }
    } finally {
      isStopping.value = false
    }
  }

  // 本地计时器
  function startLocalTick() {
    stopLocalTick()
    localElapsed.value = status.value.elapsed_seconds || 0
    const dur = (status.value as any).duration_seconds
    // duration_seconds=0 表示不限时，fixedTotal=0 表示不显示进度百分比
    if (dur === 0) {
      fixedTotal.value = 0
    } else {
      fixedTotal.value = dur
        || (localElapsed.value + (status.value.remaining_seconds || 0))
        || 7200
    }
    tickTimer = setInterval(() => {
      if (status.value.is_streaming && !status.value.is_anomaly) {
        localElapsed.value++
      }
    }, 1000)
  }

  function stopLocalTick() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
    localElapsed.value = 0
    fixedTotal.value = 0
  }

  function syncFromServer() {
    if (status.value.is_streaming) {
      const serverElapsed = status.value.elapsed_seconds || 0
      if (Math.abs(localElapsed.value - serverElapsed) > 5) {
        localElapsed.value = serverElapsed
      }
      const dur = (status.value as any).duration_seconds
      if (dur && dur > 0) fixedTotal.value = dur
    }
  }

  return {
    status,
    isStarting,
    isStopping,
    localElapsed,
    fixedTotal,
    tickTimer,
    fetchStatus,
    startLive,
    stopLive,
    startLocalTick,
    stopLocalTick,
    syncFromServer,
    startEventPolling,
    stopEventPolling,
  }
})