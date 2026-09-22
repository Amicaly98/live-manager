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
          <el-col :span="12" :xs="24">
            <el-card shadow="never" class="status-card">
              <template #header>
                <div class="card-header">
                  <span>直播状态</span>
                  <el-tag :type="liveStore.status.is_streaming ? 'success' : 'info'" size="small">
                    {{ liveStore.status.is_starting ? '后台处理中' : liveStore.status.recovery_blocked ? '恢复已暂停' : liveStore.status.is_streaming ? '直播中' : '已停止' }}
                  </el-tag>
                </div>
              </template>

              <el-alert
                v-if="liveStore.status.recovery_blocked"
                :title="'自动恢复暂停：' + liveStore.status.recovery_blocked"
                type="error" :closable="false" style="margin-bottom:12px"
              />
              <div v-if="liveStore.status.is_starting" class="idle-info">
                <el-alert
                  :title="liveStore.status.is_cancelling ? '正在停止直播并清理' : '正在后台开播，网络中断时会持续重试'"
                  description="可以关闭应用，后台会继续处理。"
                  type="info" :closable="false" style="margin-bottom:12px"
                />
                <el-button type="danger" :loading="liveStore.isStopping"
                  :disabled="liveStore.status.is_cancelling" @click="handleStopLive">
                  取消开播
                </el-button>
              </div>
              <!-- 直播中 -->
              <div v-else-if="liveStore.status.is_streaming" class="streaming-info">
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
                    <span :class="(liveStore.status.is_anomaly || liveStore.timerHint) ? 'anomaly-text' : ''">
                      {{ liveStore.timerHint || (liveStore.status.is_anomaly ? '⚠ 异常' : '直播进度') }}
                    </span>
                    <!-- 时长标签与进度条宽度解耦：0%/不限时/未知都能完整显示 -->
                    <span class="progress-time">
                      {{ formatDuration(liveStore.localElapsed) }}
                      <template v-if="liveStore.fixedTotal > 0"> / {{ formatDuration(liveStore.fixedTotal) }}</template>
                      <template v-else-if="liveStore.durationKnown"> / 不限时</template>
                      <template v-else> / 正在获取时长…</template>
                      <span v-if="overtime" class="overtime-tag">收尾中</span>
                    </span>
                  </div>
                  <div v-if="liveStore.fixedTotal > 0" class="progress-bar-wrap">
                    <el-progress
                      :percentage="progressPercent"
                      :stroke-width="18"
                      :status="liveStore.status.is_anomaly ? 'exception' : (overtime ? 'warning' : '')"
                      :text-inside="true"
                    >
                      {{ progressPercent }}%
                    </el-progress>
                  </div>
                  <div v-else class="progress-bar-wrap">
                    <el-progress
                      :percentage="0"
                      :stroke-width="18"
                      :text-inside="true"
                      color="#909399"
                    >
                      {{ liveStore.durationKnown ? '不限时' : '准备中' }}
                    </el-progress>
                  </div>
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

          <el-col :span="12" :xs="24">
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
            <div v-for="evt in events" :key="evt.id" class="event-item">
              <el-tag :type="evt.type" size="small">{{ evt.tag }}</el-tag>
              <span class="event-msg">{{ evt.message }}</span>
              <el-tag v-if="evt.source === 'local'" size="small" type="info"
                      effect="plain" class="event-src">本地</el-tag>
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
import { operationEvents } from '@/stores/operationEvents'
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
  id: string
  tag: string
  type: 'success' | 'danger' | 'warning' | 'info'
  message: string
  time: string
  source: 'server' | 'local' | 'legacy'
}
// 近期操作日志统一走响应式事件 store（2026-09-21 工作包 A）：
// 服务器事件由 live store 的受控状态应用路径直接摄取，本地操作意图走
// addLocal（独立身份/来源），页面不再经过"定时器→localStorage→定时器"中转。
const events = computed<LiveEvent[]>(() =>
  operationEvents.events.value.map((e) => ({
    id: e.id,
    tag: e.tag,
    type: (e.type as LiveEvent['type']) || 'info',
    message: e.message,
    time: e.time,
    source: e.source,
  })) as LiveEvent[])

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
// 百分比永远落在 0..100（NaN/负数/未知都不传给进度组件），
// 耗时超过目标时显示实际数值并标注"收尾中"，不让条宽超出容器。
//
// 计时语义（2026-09-20）：localElapsed = 服务端已确认有效时长 + 有界外推的
// 待确认区间。待确认部分即使推到目标也**不显示已完成**（停在 99%），完成状态
// 以服务端实际结算为准；只有服务端已确认值达到目标时才显示 100%。
const confirmedElapsed = computed(() => {
  const value = Number(liveStore.status.elapsed_seconds)
  return Number.isFinite(value) && value > 0 ? value : 0
})

const progressPercent = computed(() => {
  const total = Number(liveStore.fixedTotal)
  if (!Number.isFinite(total) || total <= 0) return 0
  if (confirmedElapsed.value >= total) return 100
  const elapsed = Number(liveStore.localElapsed)
  if (!Number.isFinite(elapsed) || elapsed <= 0) return 0
  return Math.max(0, Math.min(99, Math.round((elapsed / total) * 100)))
})

const overtime = computed(() => {
  const total = Number(liveStore.fixedTotal)
  return Number.isFinite(total) && total > 0 && confirmedElapsed.value > total
})

// ==================== Polling ====================
let _wasStreaming = false
// 轮询不重复触发 success：retryStartLive 成功时置 true，轮询消费后复位
let _faceVerifySuccessHandled = false
const { start: startPoll } = usePolling(async () => {
  // 直播状态与任务列表**并行但互不阻塞**：慢任务查询不会拖住状态应用，
  // 状态一回来就更新计时显示。
  // 状态走 store 的单飞入口：慢请求不会叠加，任务查询再慢也不拖住状态应用。
  await Promise.allSettled([
    liveStore.pollStatus(),
    taskStore.fetchTasks(),
  ])
  liveStore.syncFromServer()
  // 事件展示由响应式 store 驱动（服务器事件在状态应用路径上已直接摄取）。
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
    if (!_faceVerifySuccessHandled) {
      ElMessage.success('人脸验证已确认，直播已开始')
    }
    _faceVerifySuccessHandled = false
  }
  if (liveStore.status.is_streaming) {
    if (!liveStore.tickActive) liveStore.startLocalTick()
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
})

onUnmounted(() => {
  teardownEventListAutoScroll()
  // 页面离开：取消在途状态读与启动观察，避免离开后仍有请求落地。
  liveStore.stopStartWatch()
  liveStore.stopStatusReads()
})

function addEvent(tag: string, type: LiveEvent['type'], message: string) {
  // 本地操作意图：独立身份与来源（与后台事实分阶段，不按文案猜同一事件）。
  operationEvents.addLocal(tag, type, message)
  scrollEventsToBottom()
}

// 加载跨页面/跨启动事件：由事件 store 的缓存装载完成（含旧格式迁移），
// 页面挂载只需确保 store 已装载并滚动到底部。
function loadCrossEvents() {
  operationEvents.ensureLoaded()
  scrollEventsToBottom()
}

// ==================== Actions ====================
async function fetchStatus() {
  try {
    await Promise.all([
      liveStore.pollStatus(),
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
    // 走 store 的 resumeLive：与 start 共用同一套在途意图管理，
    // 取票等待期间点停止能真正取消这次恢复（旧实现直接 req.post 不受控）。
    const res = await liveStore.resumeLive()
    if (res.success) {
      // 后端只是“已接收”：开播在后台持续重试，只有状态轮询确认后才算真正在播。
      ElMessage.success(res.message || '恢复请求已接收，后台正在开播')
      addEvent('恢复', 'info', `恢复请求已接收：${activeTaskState.value.current_zone}`)
    } else if (res.needFaceVerify) {
      showVerifyModal.value = true
      verifyUrl.value = res.qrData || ''
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
    notify('开播请求已接收', result.message, 'success')
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
      // 验证后恢复同样走 store：受停止取消约束，且复用统一的响应形状
      result = await liveStore.resumeLive()
    } else {
      result = await liveStore.startLive(pendingStartZone, pendingStartDuration)
    }

    if (result.success) {
      showVerifyModal.value = false
      pendingRetryResume = false
      _faceVerifySuccessHandled = true
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
    ElMessage.success(result.message)
    addEvent('停播', 'warning', result.message)
    notify('停止直播', result.message, 'warning')
    await fetchStatus()
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

async function onLogout() {
  const result = await authStore.logout()
  // 业务拒绝（例如直播进行中）或已被新的登录意图取代时，留在当前页面。
  // 网络失败仍保留本地退出语义，沿用原来的登录页落点。
  if (!result.blocked && !result.superseded) {
    router.push({ name: 'Login' })
  }
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
/* ===== 手机端适配 ===== */
@media (max-width: 768px) {
  .dashboard { flex-direction: column; }
  .main-content {
    padding: 56px 12px 12px;
    min-height: auto;
    overflow: auto;
  }
  .top-cards { margin-bottom: 12px; }
  .top-cards :deep(.el-card__body) { padding: 12px 14px !important; }
  .top-cards :deep(.el-card__header) { padding-left: 14px; padding-right: 14px; }

  /* 统计栏：3列2行紧凑排列 */
  .stats-grid {
    display: grid !important;
    grid-template-columns: repeat(3, 1fr) !important;
    grid-template-rows: repeat(2, 1fr) !important;
    gap: 6px !important;
    width: 100% !important;
  }
  .stat-item {
    width: auto !important;
    height: auto !important;
    aspect-ratio: auto !important;
    padding: 8px 4px;
  }
  .stat-value { font-size: 18px !important; }
}
</style>
