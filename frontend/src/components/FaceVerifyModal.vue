<template>
  <el-dialog
    :model-value="visible"
    title="⚠️ 需要人脸验证"
    width="420px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    center
  >
    <div class="verify-content">
      <el-icon :size="48" color="#e6a23c"><WarningFilled /></el-icon>
      <h3>B 站要求进行人脸验证</h3>
      <p class="verify-tip">
        请使用 B 站 App 扫描下方二维码完成人脸验证，完成后点击「重试开播」。
      </p>

      <!-- QR 二维码 -->
      <div v-if="verifyUrl" class="qr-section">
        <img
          :src="qrImageUrl"
          alt="人脸验证二维码"
          class="qr-image"
          @error="onQrError"
        />
        <p class="qr-url">{{ verifyUrl }}</p>
      </div>
      <el-alert v-else title="未获取到验证链接" type="error" :closable="false" />

      <div class="verify-actions">
        <el-button type="primary" @click="$emit('retry')" :loading="isChecking">
          已完成验证，重试开播
        </el-button>
        <el-button @click="$emit('cancel')">取消</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  visible: boolean
  verifyUrl?: string
  isChecking?: boolean
}>()

defineEmits<{
  retry: []
  cancel: []
}>()

const qrImageUrl = computed(() => {
  if (!props.verifyUrl) return ''
  return `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(props.verifyUrl)}`
})

function onQrError(e: Event) {
  const img = e.target as HTMLImageElement
  img.style.display = 'none'
}
</script>

<style scoped>
.verify-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  text-align: center;
}
.verify-tip {
  color: #909399;
  font-size: 14px;
  max-width: 320px;
}
.qr-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.qr-image {
  width: 220px;
  height: 220px;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
}
.qr-url {
  font-size: 11px;
  color: #c0c4cc;
  word-break: break-all;
  max-width: 300px;
}
.verify-actions {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}
</style>
