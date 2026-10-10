<template>
  <el-dialog
    :model-value="visible"
    title="需要人脸验证"
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
        请先在 B 站完成验证，然后回到面板点击「已完成验证」。面板确认只是告诉后台
        "你已经验证过"，不会另外发起一次开播。
      </p>

      <!-- 验证页地址：只在服务端确认是**合法地址**时才画二维码 -->
      <div v-if="hasUrl" class="qr-section">
        <img v-if="qrImageUrl" :src="qrImageUrl" alt="人脸验证地址二维码" class="qr-image" />
        <p class="qr-url">{{ verifyUrl }}</p>
      </div>
      <el-alert
        v-else
        title="验证链接未取得"
        description="后台没有拿到可用的验证地址（平台可能只返回了错误文案）。请在 B 站 App 内完成人脸验证后回到本面板确认。"
        type="warning"
        :closable="false"
      />

      <!--
        服务端说不能从面板确认时，绝不显示一个点了没反应的按钮：那正是
        2026-09-25 的现场——弹窗能开、"已完成验证"永远无效，用户只能叉掉
        改点开播。这里把原因摊开，并给出一条走得通的路。
      -->
      <el-alert
        v-if="confirmable === false"
        type="warning"
        :closable="false"
        class="verify-block-alert"
        title="本次验证无法从面板确认"
        :description="blockReason || '当前没有已登录账号，服务端无法校验确认归属。'"
      />

      <div class="verify-actions">
        <el-button
          v-if="confirmable !== false"
          type="primary"
          @click="$emit('retry')"
          :loading="isChecking"
        >
          已完成验证
        </el-button>
        <el-button v-else type="primary" @click="$emit('cancel')">
          知道了
        </el-button>
        <el-button v-if="confirmable !== false" @click="$emit('cancel')">
          取消
        </el-button>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * FaceVerifyModal（阶段 4C）：展示**冻结事实**里来的验证上下文。
 *
 * - 没有合法验证地址时如实显示"验证链接未取得"，**不**把登录二维码或平台错误
 *   文案冒充成验证地址；
 * - 按钮只说"已完成验证"：面板确认只是把用户的确认交给后台，后台按**原来的**
 *   目标重试，不另发 Start；
 * - 二维码在本地生成（不请求任何第三方服务）。
 */
import { computed, ref, watch } from 'vue'
import QRCode from 'qrcode'

const props = defineProps<{
  visible: boolean
  verifyUrl?: string
  isChecking?: boolean
  /**
   * 服务端下发的可确认性（投影里的 `face.confirmable`）。`false` 时本弹窗只做
   * 说明，不提供确认按钮——因为服务端一定不会受理该确认。
   */
  confirmable?: boolean
  /** 不可确认的原因，直接展示。 */
  blockReason?: string
}>()

defineEmits<{
  retry: []
  cancel: []
}>()

const qrImageUrl = ref('')
const hasUrl = computed(() => Boolean(props.verifyUrl && props.verifyUrl.trim()))

watch(() => props.verifyUrl, async (url) => {
  if (!url || !url.trim()) { qrImageUrl.value = ''; return }
  try {
    qrImageUrl.value = await QRCode.toDataURL(url, {
      width: 220, margin: 2, color: { dark: '#303133', light: '#ffffff' },
    })
  } catch {
    // 生成失败只影响二维码图形；地址文本仍然展示，用户可手工复制。
    qrImageUrl.value = ''
  }
}, { immediate: true })
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
.verify-block-alert {
  width: 100%;
  text-align: left;
}
.verify-block-alert :deep(.el-alert__description) {
  text-align: left;
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
