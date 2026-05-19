<template>
  <div class="dashboard">
    <el-container>
      <Sidebar
        :user-name="authStore.userInfo?.uname"
        :user-avatar="authStore.userInfo?.face"
        @logout="onLogout"
      />

      <el-main class="main-content">
        <!-- 直播控制卡片 -->
        <el-row :gutter="20" class="top-cards">
          <el-col :span="12">
            <el-card shadow="never" class="status-card">
              <template #header>
                <div class="card-header">
                  <span>直播状态</span>
                  <el-tag :type="liveStore.status.is_streaming ? 'success' : 'info'" size="small">
                    {{ liveStore.status.is_streaming ? '直播中' : '已停止' }}
                  </el-tag>
                </div>
              </template>

              <!-- 直播中 -->
              <div v-if="liveStore.status.is_streaming" class="streaming-info">
                <div class="info-row">
                  <span class="label">直播模式</span>
                  <span class="value">
                    <el-tag :type="liveStore.status.stream_mode === 'manual' ? 'warning' : 'success'" size="small">
                      {{ liveStore.status.stream_mode === 'manual' ? '手动模式' : '任务模式' }}
                    </el-tag>
                  </span>
                </div>
                <!-- FFmpeg 推流状态 -->
                <div v-if="liveStore.status.ffmpeg_active" class="info-row">
                  <span class="label">推流状态</span>
                  <span class="value">
                    <el-tag type="success" size="small">FFmpeg 推流中</el-tag>
                    <span style="margin-left:6px;font-size:13px;color:#909399">{{ liveStore.status.ffmpeg_current_video }}</span>
                  </span>
                </div>
                <div class="info-row">
                  <span class="label">当前分区</span>
                  <span class="value">{{ liveStore.status.current_zone }}</span>
                </div>
                <div class="info-row">
                  <span class="label">房间号</span>
                  <span class="value">
                    <a
                      :href="`https://live.bilibili.com/${liveStore.status.room_id}`"
                      target="_blank"
                      class="room-link"
                    >
                      {{ liveStore.status.room_id }}
                      <el-icon :size="12"><TopRight /></el-icon>
                    </a>
                  </span>
                </div>
                <div class="progress-section">
                  <div class="progress-header">
                    <span :class="liveStore.status.is_anomaly ? 'anomaly-text' : ''">
                      {{ liveStore.status.is_anomaly ? '⚠ 异常' : '直播进度' }}
                    </span>
                    <span class="progress-time">
                      {{ formatDuration(liveStore.localElapsed) }}
                      <template v-if="liveStore.fixedTotal > 0"> / {{ formatDuration(liveStore.fixedTotal) }}</template>
                      <template v-else> / 不限时</template>
                    </span>
                  </div>
                  <el-progress
                    v-if="liveStore.fixedTotal > 0"
                    :percentage="progressPercent"
                    :stroke-width="18"
                    :status="liveStore.status.is_anomaly ? 'exception' : ''"
                    :text-inside="true"
                  >
                    {{ progressPercent }}%
                  </el-progress>
                  <el-progress
                    v-else
                    :percentage="0"
                    :stroke-width="18"
                    :text-inside="true"
                    color="#909399"
                  >
                    不限时
                  </el-progress>
                </div>
                <div class="stream-actions">
                  <el-button
                    v-if="liveStore.status.stream_mode === 'manual'"
                    type="warning"
                    plain
                    @click="showSwitchAreaDialog = true"
                  >
                    <el-icon><Switch /></el-icon>
                    切换分区
                  </el-button>
                  <el-button
                    type="danger"
                    :loading="liveStore.isStopping"
                    @click="handleStopLive"
                  >
                    <el-icon><VideoPause /></el-icon>
                    停止直播
                  </el-button>
                </div>
              </div>

              <!-- 已停止：双模式 + 进行中任务恢复 -->
              <div v-else class="idle-info">
                <!-- 进行中的任务提示（只要分区名不为空就显示） -->
                <el-alert
                  v-if="activeTaskState.current_zone"
                  title="存在未完成的直播任务"
                  type="warning"
                  :closable="false"
                  style="margin-bottom:12px"
                >
                  <template #default>
                    <p>
                      分区：<strong>{{ activeTaskState.current_zone }}</strong>
                      &nbsp;|&nbsp; 已播 {{ formatDuration(activeTaskState.elapsed_seconds) }}
                      &nbsp;|&nbsp; 总时长 {{ formatDuration(activeTaskState.duration_seconds) }}
                    </p>
                    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                      <el-button size="small" type="primary" @click="resumeTask">继续任务</el-button>
                      <el-button size="small" @click="clearTaskState">清空状态</el-button>
                      <el-popover trigger="click" :width="280" placement="bottom">
                        <template #reference>
                          <el-button size="small" text type="warning">修改参数</el-button>
                        </template>
                        <div style="display:flex;flex-direction:column;gap:8px">
                          <div>
                            <el-text size="small">分区名</el-text>
                            <el-input v-model="editState.zone" size="small" placeholder="分区名" />
                          </div>
                          <div>
                            <el-text size="small">总时长（秒）</el-text>
                            <el-input-number v-model="editState.duration" :min="60" :step="300" size="small" style="width:100%" />
                          </div>
                          <div>
                            <el-text size="small">已播时长（秒）</el-text>
                            <el-input-number v-model="editState.elapsed" :min="0" :step="60" size="small" style="width:100%" />
                          </div>
                          <el-button size="small" type="primary" @click="updateTaskState">保存修改</el-button>
                        </div>
                      </el-popover>
                    </div>
                  </template>
                </el-alert>
                <el-tabs v-model="liveMode" class="mode-tabs">
                  <el-tab-pane label="任务模式" name="task">
                    <div class="mode-content">
                      <template v-if="taskStore.nextTask">
                        <div class="next-task-preview">
                          <el-icon :size="36" color="#e6a23c"><Clock /></el-icon>
                          <p class="task-zone">{{ taskStore.nextTask.zone_name }}</p>
                          <p class="task-progress">
                            进度 {{ taskStore.nextTask.days_done }}/{{ taskStore.nextTask.actual_days }}
                          </p>
                        </div>
                        <el-button
                          type="primary"
                          @click="startNextTask"
                          :loading="liveStore.isStarting"
                        >
                          开始下一任务
                        </el-button>
                      </template>
                      <div v-else class="empty-task">
                        <el-icon :size="36" color="#c0c4cc"><CircleCheck /></el-icon>
                        <p>所有任务已完成 🎉</p>
                      </div>
                    </div>
                  </el-tab-pane>

                  <el-tab-pane label="手动模式" name="manual">
                    <div class="mode-content">
                      <StreamConfigurator
                        :disabled="false"
                        :is-starting="liveStore.isStarting"
                        :is-stopping="liveStore.isStopping"
                        :default-zone="lastManualZone"
                        :default-duration-minutes="lastManualDuration"
                        @start="onManualStart"
                        @stop="handleStopLive"
                      />
                    </div>
                  </el-tab-pane>
                </el-tabs>
              </div>
            </el-card>
          </el-col>

          <el-col :span="12">
            <el-card shadow="never" class="stats-card">
              <template #header>
                <div class="card-header"><span>任务统计</span></div>
              </template>
              <div class="stats-grid five-col">
                <div class="stat-item">
                  <span class="stat-value active-value">{{ taskStore.stats.pending_total }}</span>
                  <span class="stat-label">待完成</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ taskStore.stats.remaining_time }}</span>
                  <span class="stat-label">剩余时间</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value info-value">{{ taskStore.stats.avg_remaining.toFixed(2) }}</span>
                  <span class="stat-label">平均剩余</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value" :class="taskStore.stats.urgency > 1 ? 'active-value' : ''">{{ (taskStore.stats.urgency * 100).toFixed(2) }}%</span>
                  <span class="stat-label">紧迫率</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value success-value">{{ taskStore.stats.today_done }}</span>
                  <span class="stat-label">今日已执行</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value info-value">{{ taskStore.stats.today_pending }}</span>
                  <span class="stat-label">今日待执行</span>
                </div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 最近事件 -->
        <el-card shadow="never" class="event-card">
          <template #header>
            <div class="card-header"><span>操作日志</span></div>
          </template>
          <div ref="eventListRef" class="event-list">
            <div v-if="events.length === 0" class="empty-event">
              <el-text type="info">暂无操作记录</el-text>
            </div>
            <div v-for="(evt, idx) in events" :key="idx" class="event-item">
              <el-tag :type="evt.type" size="small">{{ evt.tag }}</el-tag>
              <span class="event-msg">{{ evt.message }}</span>
              <span class="event-time">{{ evt.time }}</span>
            </div>
          </div>
        </el-card>
      </el-main>
    </el-container>

    <!-- 人脸验证弹窗 -->
    <FaceVerifyModal
      :visible="showVerifyModal"
      :verify-url="verifyUrl"
      :is-checking="isCheckingVerify"
      @retry="retryStartLive"
      @cancel="showVerifyModal = false"
    />

    <!-- 手动模式切换分区弹窗 -->
    <el-dialog v-model="showSwitchAreaDialog" title="切换直播分区" width="420px">
      <el-select
        v-model="switchAreaZone"
        filterable
        remote
        reserve-keyword
        :remote-method="searchSwitchAreas"
        :loading="searchingSwitchAreas"
        placeholder="输入分区名搜索"
        clearable
        style="width:100%"
      >
        <el-option
          v-for="item in switchAreaOptions"
          :key="item.id"
          :label="item.name"
          :value="item.name"
        >
          <span>{{ item.name }}</span>
          <span v-if="item.parent_name" style="color:#c0c4cc;font-size:12px;margin-left:4px">
            — {{ item.parent_name }}
          </span>
        </el-option>
      </el-select>
      <template #footer>
        <el-button @click="showSwitchAreaDialog = false">取消</el-button>
        <el-button type="primary" @click="handleSwitchArea" :loading="isSwitchingArea">切换</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'
import { useSettingsStore } from '@/stores/settings'
import { usePolling } from '@/composables/usePolling'
import { useNotification } from '@/composables/useNotification'
import Sidebar from '@/components/Sidebar.vue'
import StreamConfigurator from '@/components/StreamConfigurator.vue'
import FaceVerifyModal from '@/components/FaceVerifyModal.vue'

// ==================== Stores ====================
const router = useRouter()
const authStore = useAuthStore()
const taskStore = useTaskStore()
const liveStore = useLiveStore()
const settingsStore = useSettingsStore()

// 轮询间隔：使用设置中的扫描间隔（秒→毫秒），默认30秒
const pollInterval = computed(() => (settingsStore.settings.scan_interval_seconds || 30) * 1000)

// ==================== State ====================
const liveMode = ref<'task' | 'manual'>('task')

// 进行中的任务状态
const activeTaskState = ref<{
  has_active: boolean
  current_zone: string
  elapsed_seconds: number
  room_id: number
  duration_seconds: number
}>({ has_active: false, current_zone: '', elapsed_seconds: 0, room_id: 0, duration_seconds: 0 })

// 编辑待恢复任务参数
const editState = reactive({ zone: '', duration: 0, elapsed: 0 })
// 监听 activeTaskState 变化，同步编辑表单初始值
watch(activeTaskState, (s) => {
  if (s.current_zone) {
    editState.zone = s.current_zone
    editState.duration = s.duration_seconds
    editState.elapsed = s.elapsed_seconds
  }
}, { immediate: true })


interface LiveEvent {
  tag: string
  type: 'success' | 'danger' | 'warning' | 'info'
  message: string
  time: string
}
const events = ref<LiveEvent[]>([])

// 事件日志容器引用（滚底用）
const eventListRef = ref<HTMLElement | null>(null)
let _eventListResizeOb: ResizeObserver | null = null

function scrollEventsToBottom() {
  const el = eventListRef.value
  if (el) {
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
  }
}

function setupEventListAutoScroll() {
  const el = eventListRef.value
  if (!el || _eventListResizeOb) return
  _eventListResizeOb = new ResizeObserver(() => {
    // 容器大小变化时保持在底部
    scrollEventsToBottom()
  })
  _eventListResizeOb.observe(el)
}

function teardownEventListAutoScroll() {
  if (_eventListResizeOb) {
    _eventListResizeOb.disconnect()
    _eventListResizeOb = null
  }
}

// 人脸验证
const showVerifyModal = ref(false)
const verifyUrl = ref('')
const isCheckingVerify = ref(false)
let pendingStartZone: string | undefined = undefined
let pendingStartDuration: number | undefined = undefined
let pendingRetryResume = false  // true=重试恢复，false=重试开播
let _closeGuard = false        // 防止退出弹窗重复
const lastManualZone = ref(sessionStorage.getItem('lastManualZone') || '')
const lastManualDuration = ref(Number(sessionStorage.getItem('lastManualDuration')) || 120)

// 手动模式切换分区
const showSwitchAreaDialog = ref(false)
const switchAreaZone = ref('')
const isSwitchingArea = ref(false)
const searchingSwitchAreas = ref(false)
const switchAreaOptions = ref<{ id: number; name: string; parent_name?: string }[]>([])

async function searchSwitchAreas(keyword: string) {
  if (!keyword || !keyword.trim()) { switchAreaOptions.value = []; return }
  searchingSwitchAreas.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.get<{ results: { id: number; name: string; parent_name?: string }[] }>(
      `/api/live/areas/search?keyword=${encodeURIComponent(keyword)}`
    )
    switchAreaOptions.value = res.results || []
  } catch { switchAreaOptions.value = [] }
  finally { searchingSwitchAreas.value = false }
}

// ==================== 实时进度 ====================
const progressPercent = computed(() => {
  const total = liveStore.fixedTotal || 7200
  if (total <= 0) return 0
  return Math.min(100, Math.round((liveStore.localElapsed / total) * 100))
})

// ==================== Polling ====================
let _wasStreaming = false
const { start: startPoll } = usePolling(async () => {
  await Promise.all([
    liveStore.fetchStatus(),
    taskStore.fetchTasks(),
  ])
  liveStore.syncFromServer()
  // 从 localStorage 同步事件（liveStore 跨页面持续写入）
  syncEventsFromStorage()
  // 人脸验证弹窗感知
  if ((liveStore.status as any).pending_face_verify && !showVerifyModal.value) {
    showVerifyModal.value = true
    verifyUrl.value = (liveStore.status as any).face_verify_url || ''
    pendingRetryResume = false
  }
  // 邮箱远程确认后开播成功，自动关闭人脸验证弹窗
  if (showVerifyModal.value && liveStore.status.is_streaming) {
    showVerifyModal.value = false
    pendingRetryResume = false
    ElMessage.success('人脸验证已确认，直播已开始')
  }
  if (liveStore.status.is_streaming) {
    if (!liveStore.tickTimer) liveStore.startLocalTick()
    _wasStreaming = true
  } else {
    if (_wasStreaming) {
      _wasStreaming = false
      // 手动模式停播：保持手动页面 + 剩余时长预填
      if (liveStore.status.stream_mode === 'manual') {
        liveMode.value = 'manual'
        const elapsedMins = Math.round(liveStore.localElapsed / 60)
        if (lastManualDuration.value > 0 && elapsedMins > 0) {
          lastManualDuration.value = Math.max(1, lastManualDuration.value - elapsedMins)
          sessionStorage.setItem('lastManualDuration', String(lastManualDuration.value))
        }
      }
    }
    liveStore.stopLocalTick()
  }
}, pollInterval.value)

const { notify } = useNotification()

onMounted(async () => {
  await fetchStatus()
  liveStore.syncFromServer()  // 立即同步服务器时间，避免切回时显示旧进度
  startPoll()
  loadCrossEvents()
  // 确保 DOM 渲染完成后滚到底部
  await nextTick()
  setTimeout(() => {
    scrollEventsToBottom()
    setupEventListAutoScroll()
  }, 100)
  // Electron 托盘退出确认
  if (window.electronAPI) {
    window.electronAPI.onTrayQuit(() => {
      if (_closeGuard) return
      _closeGuard = true
      if (liveStore.status.is_streaming) {
        ElMessageBox.confirm(
          '正在直播中，关闭应用将同时停止直播。\n\n是否停止直播并退出？',
          '确认退出',
          {
            confirmButtonText: '停止并退出',
            cancelButtonText: '不停止并退出',
            distinguishCancelAndClose: true,
            type: 'warning',
          },
        ).then(() => {
          window.electronAPI?.confirmQuit(true)
        }).catch((action: string) => {
          _closeGuard = false
          if (action === 'cancel') {
            window.electronAPI?.forceQuit()
          }
        })
      } else {
        ElMessageBox.confirm(
          '确定要退出应用吗？',
          '确认退出',
          { confirmButtonText: '退出', cancelButtonText: '取消', type: 'info' },
        ).then(() => {
          window.electronAPI?.confirmQuit(false)
        }).catch(() => { _closeGuard = false })
      }
    })
  }
})

onUnmounted(() => {
  teardownEventListAutoScroll()
})

function formatTime(d?: Date) {
  const t = d || new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${pad(t.getMonth()+1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`
}

function addEvent(tag: string, type: LiveEvent['type'], message: string) {
  events.value.push({ tag, type, message, time: formatTime() })
  if (events.value.length > 50) events.value = events.value.slice(-50)
  try { localStorage.setItem('app_events', JSON.stringify(events.value)) } catch { /* */ }
  scrollEventsToBottom()
}

// 加载跨页面/跨启动事件
function loadCrossEvents() {
  try {
    const stored = localStorage.getItem('app_events')
    if (stored) {
      const cross = JSON.parse(stored)
      const merged = [...events.value]
      const today = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const todayPrefix = `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`
      for (const e of cross) {
        // 兼容旧格式：只有 HH:MM:SS 的补上当天日期
        let t = e.time || ''
        if (t && /^\d{2}:\d{2}:\d{2}$/.test(t)) {
          t = todayPrefix + ' ' + t
        }
        if (!merged.find(x => x.time === t && x.message === e.message)) {
          merged.push({ ...e, time: t })
        }
      }
      events.value = merged.slice(-50)
      scrollEventsToBottom()
    }
  } catch { /* */ }
}

// 定期从 localStorage 增量同步新事件
function syncEventsFromStorage() {
  try {
    const stored = localStorage.getItem('app_events')
    if (!stored) return
    const cross = JSON.parse(stored) as LiveEvent[]
    const today = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const todayPrefix = `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`
    // 只追加 events 中还没有的事件
    for (const e of cross) {
      let t = e.time || ''
      if (t && /^\d{2}:\d{2}:\d{2}$/.test(t)) {
        t = todayPrefix + ' ' + t
      }
      if (!events.value.find(x => x.time === t && x.message === e.message)) {
        events.value.push({ ...e, time: t })
      }
    }
    if (events.value.length > 50) events.value = events.value.slice(-50)
    scrollEventsToBottom()
  } catch { /* */ }
}

// ==================== Actions ====================
async function fetchStatus() {
  try {
    await Promise.all([
      liveStore.fetchStatus(),
      taskStore.fetchTasks(),
      fetchActiveTaskState(),
    ])
  } catch {
    // polling silently handles errors
  }
}

async function fetchActiveTaskState() {
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.get<typeof activeTaskState.value>('/api/live/state/full')
    activeTaskState.value = res
  } catch { /* ignore */ }
}

async function updateTaskState() {
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    await req.post('/api/live/state/update', {
      current_zone: editState.zone,
      duration_seconds: editState.duration,
      elapsed_seconds: editState.elapsed,
    })
    activeTaskState.value.current_zone = editState.zone
    activeTaskState.value.duration_seconds = editState.duration
    activeTaskState.value.elapsed_seconds = editState.elapsed
    ElMessage.success('任务参数已更新')
    addEvent('状态', 'info', `修改恢复参数：${editState.zone}，时长 ${formatDuration(editState.duration)}`)
  } catch {
    ElMessage.error('更新失败')
  }
}

async function resumeTask() {
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.post<{ success: boolean; message: string; need_face_verification?: boolean; qr_data?: string }>('/api/live/resume')
    if (res.success) {
      ElMessage.success('直播已恢复')
      addEvent('恢复', 'success', `恢复直播：${activeTaskState.value.current_zone}`)
    } else if (res.need_face_verification) {
      showVerifyModal.value = true
      verifyUrl.value = res.qr_data || ''
      pendingStartZone = activeTaskState.value.current_zone
      pendingStartDuration = undefined
      pendingRetryResume = true
      addEvent('验证', 'warning', '需要人脸验证，请扫描二维码完成验证后重试')
    } else {
      ElMessage.error(res.message)
    }
    await fetchStatus()
    await fetchActiveTaskState()
  } catch {
    ElMessage.error('恢复失败')
  }
}

async function clearTaskState() {
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    await req.post('/api/live/clear-state')
    activeTaskState.value = { has_active: false, current_zone: '', elapsed_seconds: 0, room_id: 0, duration_seconds: 0 }
    ElMessage.success('任务状态已清空')
  } catch {
    ElMessage.error('清空失败')
  }
}

async function startNextTask() {
  const result = await liveStore.startLive()
  handleStartResult(result)
}

function onManualStart(payload: { zoneName: string; durationSeconds: number }) {
  lastManualZone.value = payload.zoneName
  lastManualDuration.value = Math.round(payload.durationSeconds / 60)
  sessionStorage.setItem('lastManualZone', payload.zoneName)
  sessionStorage.setItem('lastManualDuration', String(lastManualDuration.value))
  const durLabel = payload.durationSeconds === 0 ? '不限时' : `${Math.round(payload.durationSeconds / 60)} 分钟`
  ElMessageBox.confirm(
    `将在分区「${payload.zoneName}」开播，时长 ${durLabel}`,
    '确认开播',
    { confirmButtonText: '开始', cancelButtonText: '取消', type: 'info' },
  ).then(async () => {
    const result = await liveStore.startLive(payload.zoneName, payload.durationSeconds)
    handleStartResult(result, payload.zoneName, payload.durationSeconds)
  }).catch(() => { /* 用户取消 */ })
}

function handleStartResult(
  result: { success: boolean; message: string; needFaceVerify?: boolean; qrData?: string },
  zoneName?: string,
  durationSeconds?: number,
) {
  console.log('[handleStartResult]', { success: result.success, needFaceVerify: result.needFaceVerify, qrData: result.qrData, message: result.message })
  if (result.success) {
    ElMessage.success(result.message)
    notify('直播已开始', result.message, 'success')
    fetchStatus()
  } else if (result.needFaceVerify) {
    console.log('[handleStartResult] 显示人脸验证弹窗, qrData:', result.qrData)
    showVerifyModal.value = true
    verifyUrl.value = result.qrData || ''
    pendingStartZone = zoneName
    pendingStartDuration = durationSeconds
    pendingRetryResume = false
    addEvent('验证', 'warning', '需要人脸验证，请扫描二维码完成验证后重试')
  } else {
    ElMessage.error(result.message)
    addEvent('错误', 'danger', `开播失败 — ${result.message}`)
    notify('开播失败', result.message, 'error')
  }
}

async function retryStartLive() {
  isCheckingVerify.value = true
  try {
    // 先确认人脸验证完成
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    await req.post('/api/live/confirm-face-verify')

    // 根据原始操作选择重试方式
    let result: { success: boolean; message: string; needFaceVerify?: boolean; qrData?: string }
    if (pendingRetryResume) {
      const res = await req.post<{ success: boolean; message: string; need_face_verification?: boolean; qr_data?: string }>('/api/live/resume')
      result = {
        success: res.success,
        message: res.message,
        needFaceVerify: res.need_face_verification,
        qrData: res.qr_data,
      }
    } else {
      result = await liveStore.startLive(pendingStartZone, pendingStartDuration)
    }

    if (result.success) {
      showVerifyModal.value = false
      pendingRetryResume = false
      handleStartResult(result)
    } else if (result.needFaceVerify) {
      // 仍需验证，更新二维码
      verifyUrl.value = result.qrData || ''
    } else {
      showVerifyModal.value = false
      pendingRetryResume = false
      handleStartResult(result)
    }
  } finally {
    isCheckingVerify.value = false
  }
}

async function handleStopLive() {
  try {
    await ElMessageBox.confirm('确定要停止当前直播吗？', '停止直播', {
      confirmButtonText: '确定停止',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const result = await liveStore.stopLive()
  if (result.success) {
    ElMessage.success('直播已停止')
    addEvent('停播', 'warning', '直播已停止')
    notify('直播已停止', '当前直播已成功停止', 'warning')
  } else {
    ElMessage.error(result.message)
    addEvent('错误', 'danger', result.message)
  }
  await fetchStatus()
}

async function handleSwitchArea() {
  const zone = switchAreaZone.value.trim()
  if (!zone) { ElMessage.warning('请输入分区名'); return }
  isSwitchingArea.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.post<{ success: boolean; message: string }>(
      `/api/live/switch-area?zone_name=${encodeURIComponent(zone)}`
    )
    if (res.success) {
      ElMessage.success(res.message)
      addEvent('切换', 'warning', `切换分区：${zone}`)
      showSwitchAreaDialog.value = false
      switchAreaZone.value = ''
      await fetchStatus()
    } else {
      ElMessage.error(res.message)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '切换分区失败')
  } finally {
    isSwitchingArea.value = false
  }
}

function onLogout() {
  authStore.logout()
  router.push({ name: 'Login' })
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}时${m}分${s}秒`
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
}
</script>

<style scoped>
.dashboard { height: 100vh; display: flex; overflow: hidden; }
.main-content {
  background: #f5f7fa;
  padding: 10px 3%;
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
  min-height: 520px;  /* 窗口最低高度：保证日志栏至少3条且不出现双滚动条 */
}
.top-cards { flex-shrink: 1; margin-bottom: 20px; min-height: 0; }
.top-cards :deep(.el-card) { height: 100%; display: flex; flex-direction: column; }
.top-cards :deep(.el-card__body) { flex: 1; padding: 14px 8% !important; }
.card-header { display: flex; justify-content: space-between; align-items: center; font-weight: bold; }
.top-cards :deep(.el-card__header) { padding-left: 8%; padding-right: 8%; }

/* ---- 直播中 ---- */
.streaming-info { display: flex; flex-direction: column; gap: 12px; }
.info-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
.info-row .label { color: #909399; font-size: 14px; }
.info-row .value { font-size: 16px; font-weight: bold; color: #303133; }
.room-link { color: #00a1d6; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; }
.room-link:hover { text-decoration: underline; }
.stream-actions { margin-top: 12px; text-align: center; }

/* ---- 双模式 ---- */
.mode-tabs { width: 100%; }
.mode-content { min-height: 100px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; padding: 20px 0; }
.next-task-preview { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.task-zone { font-size: 20px; font-weight: bold; color: #303133; }
.task-progress { font-size: 13px; color: #909399; }
.empty-task { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.empty-task p { color: #909399; }

:deep(.el-progress-bar__innerText) { color: #303133 !important; font-weight: bold; }
.progress-section { margin: 8px 0; }
.progress-header { display: flex; justify-content: space-between; margin-bottom: 4px; }
.progress-time { font-size: 13px; color: #606266; }
.anomaly-text { color: #f56c6c; font-weight: bold; }

/* ---- 统计 ---- */
.stats-card { container-type: inline-size; }
.stats-card :deep(.el-card__body) { overflow: hidden; }

/* ≤386px：原有样式，2列起步 */
.stats-grid { display: flex; flex-wrap: wrap; gap: 12px; min-width: 212px; }
.stats-grid.five-col { justify-content: space-evenly; }
.stat-item {
  text-align: center;
  width: 100px;
  height: 100px;
  background: #f5f7fa;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* >386px：3列2行，正方形等比缩放，卡片高度不涨 */
@container (min-width: 386px) {
  .stats-card :deep(.el-card__body) {
    display: flex;
    align-items: center;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    grid-template-rows: repeat(2, 1fr);
    gap: clamp(8px, 3.5%, 20px);
    width: 100%;
  }
  .stat-item {
    width: auto;
    height: auto;
    aspect-ratio: 1 / 1;
    flex-shrink: 1;
  }
}
.stat-value { font-size: clamp(22px, 5.5cqi, 30px); font-weight: bold; color: #303133; line-height: 1.3; }
.stat-value.active-value { color: #e6a23c; }
.stat-value.success-value { color: #67c23a; }
.stat-value.info-value { color: #409eff; }
.stat-label { font-size: 11px; color: #909399; margin-top: 2px; }

/* ---- 事件日志 ---- */
.event-card {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.event-card :deep(.el-card__body) {
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  padding: 12px 3% !important;
  overflow: hidden;
  min-height: 138px;   /* event-list 114px + 上下padding 24px */
}
.event-list {
  flex: 1 1 0;
  overflow-y: auto;
  min-height: 114px;   /* 精确3条日志高度 */
}
.empty-event { text-align: center; padding: 24px; }
.event-item { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #f5f5f5; }
.event-msg { flex: 1; font-size: 13px; color: #606266; }
.event-time { font-size: 12px; color: #c0c4cc; }
</style>