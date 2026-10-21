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
                  <el-tag :type="statusTagType" size="small">{{ statusLabel }}</el-tag>
                </div>
              </template>

              <el-alert
                v-if="!liveStore.statusLoaded"
                title="正在读取服务端状态…"
                type="info" :closable="false" style="margin-bottom:12px"
              />
              <el-alert
                v-else-if="liveStore.statusStale"
                title="与服务端连接已中断：显示停留在最后一次可信状态"
                type="warning" :closable="false" style="margin-bottom:12px"
              />
              <el-alert
                v-else-if="blockedReason"
                :title="blockedReason"
                type="error" :closable="false" style="margin-bottom:12px"
              />

              <!-- 运行中（含等待任务） -->
              <div v-if="runningSection" class="streaming-info">
                <div class="info-row">
                  <span class="label">运行方式</span>
                  <span class="value">
                    <el-tag :type="liveStore.sessionMode === 'single' ? 'warning' : 'success'" size="small">
                      {{ liveStore.sessionMode === 'single' ? '手动模式' : '任务模式' }}
                    </el-tag>
                    <el-tag size="small" type="info" effect="plain" style="margin-left:6px">
                      {{ liveStore.pushSource === 'obs' ? 'OBS 推流' : '本机推流' }}
                    </el-tag>
                  </span>
                </div>
                <div class="info-row">
                  <span class="label">当前分区</span>
                  <span class="value">{{ liveStore.zoneName || '—' }}</span>
                </div>
                <div class="info-row">
                  <span class="label">房间号</span>
                  <span class="value">
                    <a
                      v-if="roomId"
                      :href="`https://live.bilibili.com/${roomId}`"
                      target="_blank"
                      class="room-link"
                    >
                      {{ roomId }}
                      <el-icon :size="12"><TopRight /></el-icon>
                    </a>
                    <template v-else>—</template>
                  </span>
                </div>

                <!-- 等待任务：用户要运行，但暂时没有可执行场次 -->
                <el-alert
                  v-if="liveStore.waitingTasks"
                  title="等待任务"
                  description="当前没有可执行的任务场次。添加任务或到日切后由后端自行唤醒，无需重复点击开始。"
                  type="info" :closable="false" style="margin-bottom:8px"
                />

                <div class="progress-section">
                  <div class="progress-header">
                    <span :class="liveStore.timerHint ? 'anomaly-text' : ''">
                      {{ liveStore.timerHint || '直播进度' }}
                    </span>
                    <!-- 时长标签与进度条宽度解耦：0%/不限时/未知都能完整显示 -->
                    <span class="progress-time">
                      {{ formatDuration(liveStore.displaySeconds) }}
                      <template v-if="progressRendered"> / {{ formatDuration(liveStore.targetSeconds) }}</template>
                      <template v-else-if="liveStore.hasRun"> / 不限时</template>
                      <template v-else> / 准备中</template>
                      <span v-if="liveStore.overtime" class="overtime-tag">收尾中</span>
                    </span>
                  </div>
                  <div v-if="progressRendered" class="progress-bar-wrap">
                    <el-progress
                      :percentage="liveStore.progressPercent"
                      :stroke-width="18"
                      :status="liveStore.overtime ? 'warning' : ''"
                      :text-inside="true"
                    >
                      {{ liveStore.progressPercent }}%
                    </el-progress>
                  </div>
                  <div v-else class="progress-bar-wrap">
                    <el-progress
                      :percentage="0"
                      :stroke-width="18"
                      :text-inside="true"
                      color="#909399"
                    >
                      {{ liveStore.hasRun ? '不限时' : '等待场次' }}
                    </el-progress>
                  </div>
                  <p v-if="liveStore.displayFrozen" class="task-progress">
                    状态暂未更新：显示已冻结在最后一次可信进度上。
                  </p>
                </div>

                <div class="stream-actions">
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

              <!-- 停止 / 空闲：恢复 + 双模式 -->
              <div v-else class="idle-info">
                <!-- 停止收尾中：目标已停止，房间还没核对完 -->
                <el-alert
                  v-if="liveStore.closing"
                  title="正在停止并核对房间状态"
                  type="info" :closable="false" style="margin-bottom:12px"
                />

                <!-- 有可恢复的原场：继续 / 放弃 -->
                <el-alert
                  v-if="liveStore.resumable && liveStore.statusLoaded"
                  title="存在未完成的直播场次"
                  type="warning"
                  :closable="false"
                  style="margin-bottom:12px"
                >
                  <template #default>
                    <p>
                      分区：<strong>{{ liveStore.zoneName || '—' }}</strong>
                      &nbsp;|&nbsp; 已确认 {{ formatDuration(liveStore.confirmedSeconds) }}
                      <template v-if="liveStore.durationKnown">
                        &nbsp;|&nbsp; 目标 {{ formatDuration(liveStore.targetSeconds) }}
                      </template>
                      <template v-else>&nbsp;|&nbsp; 不限时</template>
                    </p>
                    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
                      <el-button size="small" type="primary"
                        :loading="liveStore.isStarting" @click="resumeTask">继续场次</el-button>
                      <el-button size="small"
                        :loading="liveStore.isStarting" @click="abandonTask">放弃场次</el-button>
                      <el-button size="small" type="warning" plain
                        :loading="liveStore.isStarting" @click="restartAfterAbandonTask">
                        放弃并重新开播
                      </el-button>
                    </div>
                  </template>
                </el-alert>

                <el-alert
                  v-else-if="liveStore.statusLoaded && liveStore.resumeBlockedReason"
                  title="保存的场次当前不可继续"
                  type="info" :closable="false" style="margin-bottom:12px"
                >
                  <template #default><p>{{ liveStore.resumeBlockedReason }}</p></template>
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
                          :disabled="!canStartNew"
                        >
                          开始下一任务
                        </el-button>
                        <p class="task-progress">
                          推流方式：{{ pushSourceLabel }}（在设置页修改）
                        </p>
                        <p v-if="liveStore.resumable" class="task-progress">
                          请先继续或放弃未完成的场次，再开始新任务。
                        </p>
                        <p v-else-if="!liveStore.statusLoaded" class="task-progress">
                          正在读取服务端状态，请稍候。
                        </p>
                      </template>
                      <div v-else class="empty-task">
                        <el-icon :size="36" color="#c0c4cc"><CircleCheck /></el-icon>
                        <p>所有任务已完成</p>
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
                      <p class="task-progress">
                        推流方式：{{ pushSourceLabel }}（在设置页修改）
                      </p>
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
                <div class="stat-item" :title="scheduleMarginPresentation.tooltip">
                  <span class="stat-value" :class="scheduleMarginPresentation.sufficient ? 'success-value' : ''">
                    {{ scheduleMarginPresentation.value }}
                  </span>
                  <span class="stat-label">容错时长</span>
                  <small
                    v-if="scheduleMarginPresentation.bottleneck"
                    style="color:#909399;line-height:1.2;white-space:nowrap"
                  >{{ scheduleMarginPresentation.bottleneck }}</small>
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
      :verify-url="faceVerifyUrl"
      :is-checking="isCheckingVerify"
      :confirmable="faceConfirmable"
      :block-reason="faceBlockReason"
      @retry="confirmFaceVerify"
      @cancel="cancelVerifyModal"
    />
  </div>
</template>

<script setup lang="ts">
/**
 * Dashboard（阶段 4C 接线）。
 *
 * 三条硬边界：
 *  1. 运行事实全部来自 `liveStore.projection` —— 页面不再有自己的状态副本，
 *     也不再用 `resumable` 反推"能不能开新任务"；
 *  2. **手工恢复参数编辑已退役**：没有分区/时长/已播秒数的输入框，也没有
 *     `/state/update`、`/clear-state`、`/switch-area` 调用；
 *  3. 进度条只是绘制：`progressPercent` 到 100% 也不会触发任何控制请求。
 */
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'
import { useSettingsStore } from '@/stores/settings'
import { operationEvents } from '@/stores/operationEvents'
import { usePolling } from '@/composables/usePolling'
import { getScheduleMarginPresentation } from '@/utils/taskMargin'
import type { FaceConfirmRequest, PushSource } from '@/types/session'
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
/**
 * 推流方式只由**设置页**决定（manual→obs，其余→ffmpeg）：页面不再另选。
 * 这里只作兼容字段随请求带上（服务端以目标实例自己的可信设置为准），
 * 以及给只读提示用。
 */
const pushSource = computed<PushSource>(
  () => (settingsStore.settings.stream_mode === 'manual' ? 'obs' : 'ffmpeg'))
const pushSourceLabel = computed(
  () => (pushSource.value === 'obs' ? 'OBS（自行推流）' : 'FFmpeg 本机推流'))
const lastManualZone = ref(sessionStorage.getItem('lastManualZone') || '')
const lastManualDuration = ref(Number(sessionStorage.getItem('lastManualDuration')) || 120)

const roomId = computed(() => Number(liveStore.projection?.room_id ?? 0) || 0)
const scheduleMarginPresentation = computed(
  () => getScheduleMarginPresentation(taskStore.stats))
/** 有没有可画的目标进度：有场次且目标有限。 */
const progressRendered = computed(
  () => liveStore.hasRun && liveStore.targetSeconds > 0 && liveStore.durationKnown)
/** 用户在运行（含等待任务），停止收尾不算。 */
const runningSection = computed(
  () => (liveStore.running && liveStore.hasRun) || liveStore.closing)
const canStartNew = computed(
  () => liveStore.statusLoaded && !liveStore.resumable && !liveStore.running)

const statusLabel = computed(() => {
  if (!liveStore.statusLoaded) return '读取中'
  if (liveStore.closing) return '停止收尾中'
  if (liveStore.live) return '直播中'
  if (liveStore.waitingTasks) return '等待任务'
  if (liveStore.running) return '切换中'
  return '已停止'
})
const statusTagType = computed(() => {
  if (liveStore.live) return 'success'
  if (liveStore.closing || liveStore.running) return 'warning'
  return 'info'
})
/** 控制不可用时的原因（数据接管未通过等）：由 /api/health 的结论呈现。 */
const blockedReason = ref('')

// ==================== 操作日志 ====================
interface LiveEvent {
  id: string
  tag: string
  type: 'success' | 'danger' | 'warning' | 'info'
  message: string
  time: string
  source: 'server' | 'local' | 'legacy'
}
// 近期操作日志统一走响应式事件 store：服务器事件由 live store 的受控状态应用
// 路径直接摄取（唯一 ingestion 入口），本地操作意图走 addLocal（独立身份/来源）。
const events = computed<LiveEvent[]>(() =>
  operationEvents.events.value.map((e) => ({
    id: e.id,
    tag: e.tag,
    type: (e.type as LiveEvent['type']) || 'info',
    message: e.message,
    time: e.time,
    source: e.source,
  })) as LiveEvent[])

const eventListRef = ref<HTMLElement | null>(null)
let _eventListResizeOb: ResizeObserver | null = null

function scrollEventsToBottom() {
  const el = eventListRef.value
  if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
}
function setupEventListAutoScroll() {
  const el = eventListRef.value
  if (!el || _eventListResizeOb) return
  _eventListResizeOb = new ResizeObserver(() => { scrollEventsToBottom() })
  _eventListResizeOb.observe(el)
}
function teardownEventListAutoScroll() {
  if (_eventListResizeOb) { _eventListResizeOb.disconnect(); _eventListResizeOb = null }
}

// ==================== 人脸验证 ====================
const showVerifyModal = ref(false)
const isCheckingVerify = ref(false)
/**
 * 弹窗打开时**冻结**的确认上下文。提交时只用它——不再读当前投影补齐，
 * 这样"打开弹窗后又 Stop / 换了目标"就不会被旧确认改写。
 */
let frozenFace: FaceConfirmRequest | null = null
const faceVerifyUrl = ref('')
const faceConfirmable = computed(() => {
  const face = liveStore.face
  return Boolean(face && face.active && face.confirmable)
})
const faceBlockReason = computed(() => String(liveStore.face?.block_reason || ''))
let suppressFaceUntilGoalRevision = -1

watch(() => liveStore.face, (face) => {
  if (face && face.active) {
    if (suppressFaceUntilGoalRevision >= 0
        && face.goal_revision === suppressFaceUntilGoalRevision) return
    suppressFaceUntilGoalRevision = -1
    faceVerifyUrl.value = face.verify_url
    if (!showVerifyModal.value) {
      showVerifyModal.value = true
      frozenFace = {
        goal_revision: face.goal_revision,
        uid: face.uid, room_id: face.room_id,
      }
      addEvent('验证', 'warning',
               face.verify_url_available
                 ? '需要人脸验证：请完成验证后在弹窗确认'
                 : '需要人脸验证：验证链接未取得，请在 B 站完成验证后回到面板确认')
    }
    return
  }
  suppressFaceUntilGoalRevision = -1
  if (showVerifyModal.value && !liveStore.running) cancelVerifyModal(false)
}, { deep: true })

function cancelVerifyModal(suppressCurrent = true) {
  if (suppressCurrent) {
    suppressFaceUntilGoalRevision = Number(liveStore.face?.goal_revision ?? -1)
  }
  showVerifyModal.value = false
  isCheckingVerify.value = false
  frozenFace = null
  faceVerifyUrl.value = ''
}

/**
 * 面板确认：提交**冻结**的三元组；确认后只等事实，**不**另发 Start。
 */
async function confirmFaceVerify() {
  if (!showVerifyModal.value) return
  const frozen = frozenFace
  if (!frozen || !faceConfirmable.value) {
    ElMessage.warning(faceBlockReason.value
      || '本次验证无法从面板确认：请在 B 站完成验证后重试')
    return
  }
  isCheckingVerify.value = true
  try {
    const result = await liveStore.confirmFace(frozen)
    if (result.superseded) return
    if (result.outcome.kind === 'receipt'
        && result.outcome.receipt.status === 'ignored') {
      // 目标已经变了：这份确认不再授权任何东西。
      cancelVerifyModal()
      ElMessage.warning('这次确认已被更新的操作取代，请按当前状态重新操作')
      return
    }
    if (result.notice) {
      const level = result.notice.level
      if (level === 'error') ElMessage.error(result.notice.message)
      else if (level === 'warning') ElMessage.warning(result.notice.message)
      else ElMessage.success(result.notice.message)
    }
    if (!result.notice || result.notice.level !== 'error') {
      cancelVerifyModal(false)
      addEvent('验证', 'info', '已提交人工确认，等待后台按原目标重试')
    }
  } finally {
    isCheckingVerify.value = false
  }
}

// ==================== Polling ====================
const { start: startPoll } = usePolling(async () => {
  // 状态走 store 的单飞入口：慢请求不会叠加，任务查询再慢也不拖住状态应用。
  const statusPromise = liveStore.refreshStatus()
  void taskStore.fetchTasks().catch(() => { /* 任务列表刷新与状态无关 */ })
  await statusPromise
}, pollInterval.value)

onMounted(async () => {
  // 缓存的操作日志先于任何网络读取装载：慢读取期间日志栏也有内容可看，
  // 不等第一份状态回来才出现（桌面既有行为，保留）。
  operationEvents.ensureLoaded()
  scrollEventsToBottom()
  // 只读提示与兼容字段都要反映**当前**设置（推流方式由设置页唯一决定）。
  void settingsStore.fetchSettings()
  await liveStore.refreshStatus()
  await readHealth()
  startPoll()
  await nextTick()
  setTimeout(() => { scrollEventsToBottom(); setupEventListAutoScroll() }, 100)
})

onUnmounted(() => {
  teardownEventListAutoScroll()
  // 页面离开：取消在途状态读与收敛观察，避免离开后仍有请求落地。
  liveStore.stopSettleWatch()
  liveStore.stopStatusReads()
})

/** 控制不可用的原因来自 /api/health 的 data_takeover（只读）。 */
async function readHealth() {
  try {
    const { useRequest } = await import('@/api/request')
    const res = await useRequest().get<{
      control_service?: boolean
      data_takeover?: { status?: string; detail?: string; reason?: string }
    }>('/api/health')
    const takeover = res?.data_takeover || {}
    blockedReason.value = (res?.control_service && takeover.status
      && !['trusted', 'first_start', 'migrated'].includes(String(takeover.status)))
      ? `控制当前不可用：${takeover.detail || takeover.reason || takeover.status}`
      : ''
  } catch { blockedReason.value = '' }
}

function addEvent(tag: string, type: LiveEvent['type'], message: string) {
  operationEvents.addLocal(tag, type, message)
  scrollEventsToBottom()
}

/**
 * 桌面系统级通知（只走 Electron 通道，不再弹第二条应用内提示）。
 *
 * `composables/useNotification` 的 `notify` 会**同时**弹一条 ElMessage；控制结果
 * 已经在页面上有明确提示，再弹一次只会把同一件事说两遍。这里只保留"窗口可能在
 * 托盘里"这一条真实需求：失败/需人工处理的结论额外发一条系统通知。
 */
function notifyDesktop(title: string, body: string): void {
  try {
    window.electronAPI?.showNotification(title, body)
  } catch {
    /* 通知通道不可用不影响控制结果 */
  }
}

/** 控制结果的统一呈现：受理 ≠ 成功，页面只转述。 */
function showNotice(notice: { level: string; message: string } | null) {
  if (!notice) return
  const { level, message } = notice
  if (level === 'error') ElMessage.error(message)
  else if (level === 'warning') ElMessage.warning(message)
  else if (level === 'success') ElMessage.success(message)
  else ElMessage.info(message)
  if (level === 'error' || level === 'warning') {
    addEvent(level === 'error' ? '错误' : '提示', level === 'error' ? 'danger' : 'warning',
             message)
    notifyDesktop(level === 'error' ? '操作未被受理' : '需要注意', message)
  } else {
    addEvent('操作', 'info', message)
  }
}

// ==================== Actions ====================
async function startNextTask() {
  // 本地只记录**意图**；"已开播"只能来自后台确认事实。
  addEvent('开播', 'info',
           `请求自动选择并开始下一场（${pushSource.value === 'obs' ? 'OBS' : '本机'}推流）`)
  const result = await liveStore.startNextTask(pushSource.value)
  showNotice(result.notice)
}

function onManualStart(payload: { zoneName: string; durationSeconds: number }) {
  lastManualZone.value = payload.zoneName
  lastManualDuration.value = Math.round(payload.durationSeconds / 60)
  sessionStorage.setItem('lastManualZone', payload.zoneName)
  sessionStorage.setItem('lastManualDuration', String(lastManualDuration.value))
  const durLabel = payload.durationSeconds === 0 ? '不限时'
    : `${Math.round(payload.durationSeconds / 60)} 分钟`
  // 确认框**之前**冻结整份操作：确认期间的任何后台变化都拼不进这份载荷。
  const operation = liveStore.buildSingleOp(
    { zoneName: payload.zoneName, durationSeconds: payload.durationSeconds },
    pushSource.value)
  ElMessageBox.confirm(
    `将在分区「${payload.zoneName}」开一场单次直播，时长 ${durLabel}。`,
    '确认开播',
    { confirmButtonText: '开始', cancelButtonText: '取消', type: 'info' },
  ).then(async () => {
    addEvent('开播', 'info', `请求单次开播：${payload.zoneName}（${durLabel}）`)
    const result = await liveStore.dispatch(operation)
    showNotice(result.notice)
  }).catch(() => { /* 用户取消 */ })
}

async function resumeTask() {
  if (!liveStore.projection?.run || !liveStore.projection?.goal) {
    ElMessage.error('当前没有可恢复的场次'); return
  }
  // 恢复同样在**任何 await 之前**冻结 run_id/goal_revision。
  const operation = liveStore.buildResumeOp({
    runId: liveStore.runId, goalRevision: liveStore.goalRevision,
  })
  addEvent('恢复', 'info', `请求继续场次：${liveStore.zoneName}`)
  const result = await liveStore.dispatch(operation)
  showNotice(result.notice)
}

async function abandonTask() {
  // 弹框**之前**冻结原场三件套：确认之后不再读当前投影（复核 R2）。
  const ref = liveStore.freezeAbandonRef(liveStore.projection)
  if (!ref) { ElMessage.error('当前没有可放弃的场次'); return }
  const operation = liveStore.buildAbandonOp(ref)
  try {
    await ElMessageBox.confirm(
      `将放弃「${ref.zoneName || '—'}」这一场未结算的已播进度。\n\n`
      + `此前已完成的天数不受影响；放弃之后这场无法再继续。`,
      '放弃这场未结算的进度',
      { confirmButtonText: '放弃', cancelButtonText: '取消', type: 'warning',
        distinguishCancelAndClose: true, confirmButtonClass: 'el-button--danger' },
    )
  } catch { return /* 用户取消：零控制提交 */ }
  addEvent('放弃', 'warning', `请求放弃场次：${ref.zoneName}`)
  const result = await liveStore.dispatch(operation)
  showNotice(result.notice)
}

/**
 * 放弃旧场并**一步**开新场。
 *
 * 新选择与原场上下文都必须在确认框**之前**冻结：确认期间后台换场不会改写
 * 用户已经确认的这一份（`buildRestartAfterAbandonOp` 只接受冻结值）。
 */
async function restartAfterAbandonTask() {
  const ref = liveStore.freezeAbandonRef(liveStore.projection)
  if (!ref) { ElMessage.error('当前没有需要放弃的原场'); return }
  let startBody: Record<string, unknown> | null = null
  if (liveMode.value === 'manual') {
    const zoneName = String(lastManualZone.value || '').trim()
    if (!zoneName) { ElMessage.warning('请先在手动模式下填写直播分区'); return }
    startBody = { mode: 'single', zone_name: zoneName,
                  target_seconds: Math.max(0, Math.round(lastManualDuration.value * 60)),
                  push_source: pushSource.value }
  } else {
    const next = taskStore.nextTask as (typeof taskStore.nextTask & { id?: number }) | null
    if (!next || typeof next.id !== 'number') {
      ElMessage.warning('当前没有可指定的任务：请刷新任务列表后重试'); return
    }
    startBody = { mode: 'task', task_id: next.id,
                  expected_revision: taskStore.tasksRevision,
                  business_date: taskStore.businessDate,
                  push_source: pushSource.value }
  }
  const operation = liveStore.buildRestartAfterAbandonOp(startBody, ref)
  try {
    await ElMessageBox.confirm(
      `将放弃「${ref.zoneName || '—'}」这一场未结算的已播进度，并立即开始新的一场。\n\n`
      + `两项动作在同一项请求里完成，中途不会先清空状态。`,
      '放弃并重新开播',
      { confirmButtonText: '放弃并开播', cancelButtonText: '取消', type: 'warning',
        distinguishCancelAndClose: true, confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  addEvent('放弃并开播', 'warning',
           `请求放弃「${ref.zoneName}」并开始新场`)
  const result = await liveStore.dispatch(operation)
  showNotice(result.notice)
}

async function handleStopLive() {
  try {
    await ElMessageBox.confirm('确定要停止当前直播吗？', '停止直播', {
      confirmButtonText: '确定停止', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  // 用户已确认停播：撤销页面中的旧验证意图，避免旧弹窗在停止请求期间继续重试。
  cancelVerifyModal()
  addEvent('停播', 'warning', '请求停止直播')
  const result = await liveStore.stopLive()
  showNotice(result.notice)
}

async function onLogout() {
  // 退出由 auth store 统一处理：**服务确认之后**才撤本地身份。
  const result = await authStore.logout()
  if (!result.blocked && !result.superseded) {
    liveStore.teardown()
    router.push({ name: 'Login' })
  }
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
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
.overtime-tag { margin-left: 6px; color: #e6a23c; }

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
.event-msg { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 13px; color: #606266; }
.event-time { font-size: 12px; color: #c0c4cc; }
/* ===== 手机端适配 ===== */
@media (max-width: 768px) {
  /* 小屏按内容向下滚动，日志不再争用首屏剩余的几十像素。 */
  .dashboard {
    display: block;
    height: auto;
    min-height: 100vh;
    overflow: visible;
  }
  .dashboard > .el-container {
    display: block;
    width: 100%;
  }
  .main-content {
    padding: 56px 12px 12px;
    display: block;
    width: 100%;
    min-height: 0;
    overflow: visible;
  }
  .top-cards { margin-bottom: 16px; row-gap: 12px; }
  .top-cards :deep(.el-card__body) { padding: 12px 14px !important; }
  .top-cards :deep(.el-card__header) { padding-left: 14px; padding-right: 14px; }
  .info-row { gap: 12px; }
  .info-row .label { flex-shrink: 0; }
  .info-row .value { min-width: 0; overflow-wrap: anywhere; text-align: right; }
  .progress-header { gap: 6px 12px; flex-wrap: wrap; }

  .event-card {
    height: 65vh;
    height: 65svh;
    min-height: 360px;
    max-height: 640px;
  }
  .event-card :deep(.el-card__body) { min-height: 0; }
  .event-list { min-height: 0; }
  .event-item {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 6px 8px;
    padding: 12px 0;
  }
  .event-item > .el-tag { justify-self: start; }
  .event-msg { grid-column: 1 / -1; grid-row: 2; line-height: 1.6; }
  .event-src { grid-column: 2; grid-row: 1; }
  .event-time { grid-column: 3; grid-row: 1; white-space: nowrap; color: #909399; }

  /* 统计栏：3列2行紧凑排列 */
  .stats-grid {
    display: grid !important;
    grid-template-columns: repeat(3, 1fr) !important;
    grid-template-rows: repeat(2, 1fr) !important;
    gap: 6px !important;
    width: 100% !important;
    min-width: 0;
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
