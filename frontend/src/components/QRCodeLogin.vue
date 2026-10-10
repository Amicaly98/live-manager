<template>
  <div class="qrcode-login">
    <div v-if="qrImageUrl" class="qrcode-container">
      <img :src="qrImageUrl" alt="登录二维码" class="qrcode-img" />
      <p class="qrcode-tip">请使用 B 站 App 扫描二维码登录</p>
      <el-progress
        :percentage="progress"
        :stroke-width="4"
        :show-text="false"
        :color="progressColor"
      />
    </div>

    <div v-else class="qrcode-placeholder">
      <slot name="placeholder">
        <el-button
          type="primary"
          size="large"
          :loading="isLoading"
          @click="refresh"
        >
          获取登录二维码
        </el-button>
      </slot>
    </div>

    <div v-if="message" class="qrcode-message">
      <el-alert
        :title="message"
        :type="messageType"
        :closable="false"
        show-icon
      />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * QRCodeLogin — 可复用的 B 站扫码登录组件（阶段 4C 对齐新扫码协议）
 *
 * Props:
 *   size          二维码图片尺寸（默认 200px）
 *   expireSeconds 二维码本地倒计时（默认 120s；服务端的 86038 才是权威过期）
 *
 * Emits:
 *   login-success  扫码登录成功，payload: UserInfo
 *   expired        二维码过期（本地倒计时或服务端 86038）
 *   error          获取/轮询异常，payload: string
 *
 * 与旧版的差别（`PHASE4C-DESIGN.md` §6.4）：
 * - 读 `qr_url` 与 `generation`（不再有 `qrcode_url`）；每次 `refresh()` 都是
 *   **新一代**二维码，旧代的回包一律作废；
 * - 轮询结果按服务端的 `status` 分别说明：等待扫码 / 已扫码待确认 / 已过期 /
 *   已被更新的二维码取代 / 候选被拒绝。**不是**所有 `logged_in=false` 都当作
 *   "还没扫"；
 * - 二维码的重新获取是**显式动作**（换代、有副作用），不作为普通只读 GET
 *   无限制自动重试。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import QRCode from 'qrcode'
import { useAuthStore } from '@/stores/auth'
import { usePolling } from '@/composables/usePolling'
import type { UserInfo } from '@/types/api'

// ==================== Props & Emits ====================
const props = withDefaults(
  defineProps<{
    size?: number
    expireSeconds?: number
  }>(),
  { size: 200, expireSeconds: 120 },
)

const emit = defineEmits<{
  'login-success': [user: UserInfo]
  expired: []
  error: [message: string]
}>()

// ==================== State ====================
const authStore = useAuthStore()
const qrImageUrl = ref('')
const progress = ref(0)
const message = ref('')
const messageType = ref<'info' | 'warning' | 'error'>('info')
const isLoading = ref(false)
const qrcodeKey = ref('')
const generation = ref(0)

let progressTimer: ReturnType<typeof setInterval> | null = null

const progressColor = computed(() => {
  if (progress.value > 80) return '#f56c6c'
  if (progress.value > 50) return '#e6a23c'
  return '#00a1d6'
})

// ==================== Polling ====================
const { start: startPoll, stop: stopPoll } = usePolling(async () => {
  if (!qrcodeKey.value) return

  const result = await authStore.pollLoginStatus(qrcodeKey.value)

  if (result.logged_in && result.user_info) {
    stopProgress()
    message.value = '登录成功！'
    messageType.value = 'info'
    emit('login-success', result.user_info)
    return true // stop polling
  }

  // 服务端的结论才是权威：按 status 分别说明，不把一切都当"还没扫"。
  const described = authStore.describeQrStatus(String(result.status || ''))
  if (!described) return false
  message.value = described.message
  messageType.value = described.kind === 'error' ? 'error'
    : (described.kind === 'waiting' ? 'info' : 'warning')
  if (described.kind === 'expired' || described.kind === 'stale'
      || described.kind === 'rejected') {
    stopProgress()
    qrImageUrl.value = ''
    emit('expired')
    return true
  }
  return false
}, 2000)

// ==================== Methods ====================
function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
  progress.value = 0
}

/**
 * 获取**新的一代**二维码（显式动作，带副作用：旧代次的回包从此无效）。
 */
async function refresh() {
  stopPoll()
  stopProgress()
  isLoading.value = true
  message.value = ''

  try {
    const data = await authStore.fetchQRCode()
    qrcodeKey.value = data.qrcode_key
    generation.value = data.generation

    qrImageUrl.value = await QRCode.toDataURL(data.qr_url, {
      width: props.size,
      margin: 2,
      color: { dark: '#00a1d6', light: '#ffffff' },
    })

    // 本地倒计时只是**提示**：真正的过期以服务端 86038 为准。
    let elapsed = 0
    progressTimer = setInterval(() => {
      elapsed++
      progress.value = Math.round((elapsed / props.expireSeconds) * 100)
      if (elapsed >= props.expireSeconds) {
        stopProgress()
        qrImageUrl.value = ''
        message.value = '二维码已过期，请重新获取'
        messageType.value = 'warning'
        emit('expired')
        stopPoll()
      }
    }, 1000)

    message.value = '等待扫码…'
    messageType.value = 'info'
    startPoll()
  } catch (err) {
    const msg = err instanceof Error ? err.message : '获取二维码失败'
    message.value = msg
    messageType.value = 'error'
    emit('error', msg)
  } finally {
    isLoading.value = false
  }
}

// ==================== Lifecycle ====================
onMounted(() => {
  void refresh()
})

onUnmounted(() => {
  stopPoll()
  stopProgress()
})

// ==================== Expose ====================
defineExpose({ refresh, generation })
</script>

<style scoped>
.qrcode-login {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}
.qrcode-container {
  text-align: center;
}
.qrcode-img {
  border: 2px solid #e4e7ed;
  border-radius: 8px;
  padding: 8px;
  background: #fff;
}
.qrcode-tip {
  margin-top: 12px;
  color: #606266;
  font-size: 13px;
}
.qrcode-placeholder {
  min-height: 200px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.qrcode-message {
  width: 100%;
}
</style>
