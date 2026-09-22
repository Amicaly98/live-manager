<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <el-icon :size="48" color="#00a1d6"><UserFilled /></el-icon>
        <h2>直播控制系统</h2>
        <p class="subtitle">扫描二维码登录 B 站账号</p>
      </div>

      <QRCodeLogin
        :size="200"
        :expire-seconds="120"
        @login-success="onLoginSuccess"
        @expired="onExpired"
        @error="onError"
      />

      <div class="login-footer">
        <el-text type="info">
          <el-icon><InfoFilled /></el-icon>
          扫码后请在手机上确认登录
        </el-text>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import QRCodeLogin from '@/components/QRCodeLogin.vue'
import { boot } from '@/boot'
import type { UserInfo } from '@/types/api'

const router = useRouter()

/**
 * 扫码登录成功。
 *
 * `authStore.pollLoginStatus()` 成功时已经作废了启动快照
 * （`boot.invalidate('platform-login')`）。这里在导航前等一次新的启动读取，
 * 让守卫拿到"平台已登录"的新结论，否则用户会被按回本页
 * （监督复现的第二个失败）。保留 1 秒延迟只为让"登录成功！"提示可见。
 */
async function onLoginSuccess(_user: UserInfo) {
  await new Promise(resolve => setTimeout(resolve, 1000))
  await boot.ensure()
  router.push({ name: 'Dashboard' })
}

function onExpired() {
  // 过期提示已在组件内部显示，此处可扩展日志/埋点
}

function onError(_msg: string) {
  // 错误提示已在组件内部显示
}
</script>

<style scoped>
.login-page {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
}
.login-card {
  width: 420px;
  padding: 40px;
  background: white;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  text-align: center;
}
.login-header { margin-bottom: 32px; }
.login-header h2 { margin-top: 16px; font-size: 24px; color: #303133; }
.subtitle { margin-top: 8px; color: #909399; font-size: 14px; }
.login-footer { margin-top: 20px; }
</style>