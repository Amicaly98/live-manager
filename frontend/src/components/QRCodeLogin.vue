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
 * QRCodeLogin — 可复用的 B 站扫码登录组件
 *
 * Props:
 *   size          二维码图片尺寸（默认 200px）
 *   expireSeconds 二维码有效期（默认 120s，超过自动 emit expired）
 *
 * Emits:
 *   login-success  扫码登录成功，payload: UserInfo
 *   expired        二维码过期
 *   error          获取/轮询异常，payload: string
 */
import { ref, computed, watch, onMounted } from 'vue'
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

  if (result.expired) {
    stopProgress()
    message.value = '二维码已过期，请重新获取'
    messageType.value = 'warning'
    qrImageUrl.value = ''
    emit('expired')
    return true
  }
}, 2000)

// ==================== Methods ====================
function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
  progress.value = 0
}

async function refresh() {
  stopPoll()
  stopProgress()
  isLoading.value = true
  message.value = ''

  try {
    const data = await authStore.fetchQRCode()
    qrcodeKey.value = data.qrcode_key

    qrImageUrl.value = await QRCode.toDataURL(data.qrcode_url, {
      width: props.size,
      margin: 2,
      color: { dark: '#00a1d6', light: '#ffffff' },
    })

    // 启动过期倒计时
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

    message.value = '等待扫码...'
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
  refresh()
})

// ==================== Expose ====================
defineExpose({ refresh })
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
