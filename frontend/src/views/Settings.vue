<template>
  <el-container class="settings-page">
    <Sidebar
      :user-name="authStore.userInfo?.uname"
      :user-avatar="authStore.userInfo?.face"
      @logout="onLogout"
    />
    <el-main class="main-content">
      <el-page-header @back="goBack" title="设置">
        <template #content>
          <span>直播参数配置</span>
        </template>
      </el-page-header>

      <div class="settings-form">
        <el-card shadow="never" v-loading="settingsStore.isLoading">
          <template #header><span>基本配置</span></template>

          <el-form label-width="140px" label-position="right">
            <el-form-item label="视频文件夹路径">
              <el-input
                :model-value="settingsStore.settings.video_path"
                @update:model-value="(v: string) => settingsStore.updateField('video_path', v)"
                placeholder="F:/videosforlive"
              >
                <template #append>
                  <el-button @click="selectDirectory">选择</el-button>
                </template>
              </el-input>
              <div class="form-tip">直播视频文件存放目录，按分区名分文件夹</div>
            </el-form-item>

            <el-form-item label="扫描间隔">
              <el-input-number
                :model-value="settingsStore.settings.scan_interval_seconds"
                @update:model-value="(v: number | undefined) => settingsStore.updateField('scan_interval_seconds', v ?? 30)"
                :min="5"
                :max="120"
                :step="5"
              />
              <span class="unit">秒</span>
              <div class="form-tip">每隔多少秒检查一次直播状态</div>
            </el-form-item>

            <el-form-item label="直播间异常重试">
              <el-input-number
                :model-value="settingsStore.settings.max_reconnect"
                @update:model-value="(v: number | undefined) => settingsStore.updateField('max_reconnect', v ?? 3)"
                :min="1"
                :max="10"
              />
              <span class="unit">次/小时</span>
              <div class="form-tip">直播间状态异常时 1 小时内最多重试次数</div>
            </el-form-item>
            <el-form-item label="重试冷却时间">
              <el-input-number
                :model-value="settingsStore.settings.live_retry_cooldown_minutes"
                @update:model-value="(v: number | undefined) => settingsStore.updateField('live_retry_cooldown_minutes', v ?? 60)"
                :min="10"
                :max="360"
                :step="10"
              />
              <span class="unit">分钟</span>
              <div class="form-tip">重试次数耗尽后冷却时间</div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 推流方式 -->
        <el-card shadow="never" style="margin-top: 16px;">
          <template #header><span>推流方式</span></template>
          <el-form label-width="140px" label-position="right">
            <el-form-item label="推流模式">
              <el-radio-group
                :model-value="settingsStore.settings.stream_mode"
                @update:model-value="(v: any) => settingsStore.updateField('stream_mode', v)"
              >
                <el-radio value="manual">自行推流（OBS）</el-radio>
                <el-radio value="ffmpeg" :disabled="!hasVideos">FFmpeg 推流</el-radio>
              </el-radio-group>
              <div v-if="!hasVideos" class="form-tip" style="color:#e6a23c">⚠ 视频文件夹中未检测到视频文件，FFmpeg 推流不可用</div>
            </el-form-item>
            <!-- 自行推流选项 -->
            <el-form-item v-if="settingsStore.settings.stream_mode === 'manual'" label="自动打开视频">
              <el-switch
                :model-value="settingsStore.settings.auto_open_video"
                @update:model-value="(v: any) => settingsStore.updateField('auto_open_video', v)"
              />
              <span style="margin-left:8px;color:#909399">开播时用系统默认播放器打开视频文件</span>
            </el-form-item>
            <!-- FFmpeg 推流选项 -->
            <template v-if="settingsStore.settings.stream_mode === 'ffmpeg'">
              <el-form-item label="ffmpeg 路径">
                <el-input
                  :model-value="settingsStore.settings.ffmpeg_path"
                  @update:model-value="(v: any) => settingsStore.updateField('ffmpeg_path', v)"
                  placeholder="ffmpeg（默认从 PATH 查找）"
                />
                <div class="form-tip">可填完整路径，如 D:\ffmpeg\bin\ffmpeg.exe，留空则从系统 PATH 查找</div>
              </el-form-item>
              <el-form-item label="编码不一致时">
                <el-radio-group
                  :model-value="settingsStore.settings.ffmpeg_reencode"
                  @update:model-value="(v: any) => settingsStore.updateField('ffmpeg_reencode', v)"
                >
                  <el-radio :value="true">自动重编码（稳定，CPU占用）</el-radio>
                  <el-radio :value="false">跳过不兼容视频</el-radio>
                </el-radio-group>
                <div class="form-tip">concat 模式下视频编码不一致时：重编码可保稳定但耗CPU，跳过则只推兼容视频</div>
              </el-form-item>
            </template>
          </el-form>
        </el-card>

        <!-- 推流码 -->
        <el-card shadow="never" style="margin-top: 16px;">
          <template #header>
            <div class="form-header">
              <span>推流码</span>
              <el-button size="small" text @click="fetchRtmpCode" :loading="loadingRtmp">刷新</el-button>
            </div>
          </template>
          <el-form label-width="100px" label-position="right">
            <el-form-item label="服务器地址">
              <el-input :model-value="rtmpAddr || '暂无'" readonly>
                <template #append>
                  <el-button @click="copyText(rtmpAddr)" :disabled="!rtmpAddr">
                    <el-icon><CopyDocument /></el-icon>
                  </el-button>
                </template>
              </el-input>
            </el-form-item>
            <el-form-item label="串流密钥">
              <el-input
                :model-value="rtmpMasked ? maskStreamKey(rtmpCode) : rtmpCode"
                readonly
                :type="rtmpMasked ? 'password' : 'text'"
              >
                <template #append>
                  <el-button @click="rtmpMasked = !rtmpMasked">
                    {{ rtmpMasked ? '显示' : '隐藏' }}
                  </el-button>
                  <el-button @click="copyText(rtmpCode)" :disabled="!rtmpCode">
                    <el-icon><CopyDocument /></el-icon>
                  </el-button>
                </template>
              </el-input>
              <div class="form-tip">OBS 设置 → 推流 → 服务选"自定义"，粘贴上方服务器地址和串流密钥</div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 随机时长配置 -->
        <el-card shadow="never" style="margin-top: 16px;">
          <template #header><span>随机额外时长</span></template>
          <el-form label-width="140px" label-position="right">
            <el-form-item label="分布方式">
              <el-select
                :model-value="settingsStore.settings.duration_distribution"
                @update:model-value="(v: string) => settingsStore.updateField('duration_distribution', v as AppSettings['duration_distribution'])"
                style="width: 180px"
              >
                <el-option label="Beta 分布（左偏弧线）" value="beta" />
                <el-option label="正态分布" value="normal" />
                <el-option label="均匀分布" value="uniform" />
              </el-select>
              <div class="form-tip">Beta 期望 ~1.1×，范围集中；正态对称；均匀随机</div>
            </el-form-item>
            <el-form-item label="倍率下限">
              <el-input-number
                :model-value="settingsStore.settings.duration_multiplier_min"
                @update:model-value="(v: number | undefined) => { const val = v ?? 1.00; settingsStore.updateField('duration_multiplier_min', val); if (settingsStore.settings.duration_multiplier_max < val) { settingsStore.updateField('duration_multiplier_max', val) } }"
                :min="1.00" :max="2" :step="0.001"
              />
              <span class="unit">× 基础时长</span>
            </el-form-item>
            <el-form-item label="倍率上限">
              <el-input-number
                :model-value="settingsStore.settings.duration_multiplier_max"
                @update:model-value="(v: number | undefined) => { const val = v ?? settingsStore.settings.duration_multiplier_min; const lo = settingsStore.settings.duration_multiplier_min; settingsStore.updateField('duration_multiplier_max', val < lo ? lo : val) }"
                :min="1.00" :max="2" :step="0.001"
              />
              <span class="unit">× 基础时长</span>
              <div class="form-tip">上限必须大于等于下限，输入低于下限时自动修正</div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 推送通知 -->
        <el-card shadow="never" style="margin-top: 16px;">
          <template #header>
            <div class="form-header">
              <span>推送通知</span>
              <el-switch
                :model-value="settingsStore.settings.email_enabled"
                @update:model-value="(v: any) => settingsStore.updateField('email_enabled', v)"
                size="small"
              />
            </div>
          </template>
          <template v-if="settingsStore.settings.email_enabled">
            <el-form label-width="120px" label-position="right">
              <el-form-item label="推送渠道">
                <el-radio-group
                  :model-value="settingsStore.settings.notification_channel"
                  @update:model-value="(v: any) => settingsStore.updateField('notification_channel', v)"
                >
                  <el-radio value="email">📧 邮箱</el-radio>
                  <el-radio value="serverchan">📡 Server酱（微信）</el-radio>
                </el-radio-group>
              </el-form-item>

              <!-- 邮箱配置 -->
              <template v-if="settingsStore.settings.notification_channel === 'email'">
                <el-form-item label="SMTP 服务器">
                  <el-input :model-value="settingsStore.settings.email_smtp_host"
                    @update:model-value="(v: any) => settingsStore.updateField('email_smtp_host', v)" />
                </el-form-item>
                <el-form-item label="端口">
                  <el-input-number :model-value="settingsStore.settings.email_smtp_port"
                    @update:model-value="(v: any) => settingsStore.updateField('email_smtp_port', v ?? 587)"
                    :min="1" :max="65535" />
                </el-form-item>
                <el-form-item label="发件邮箱">
                  <el-input :model-value="settingsStore.settings.email_smtp_user"
                    @update:model-value="(v: any) => settingsStore.updateField('email_smtp_user', v)"
                    placeholder="your@qq.com" />
                </el-form-item>
                <el-form-item label="授权码">
                  <el-input :model-value="settingsStore.settings.email_smtp_pass"
                    @update:model-value="(v: any) => settingsStore.updateField('email_smtp_pass', v)"
                    type="password" show-password placeholder="QQ邮箱→设置→账户→POP3/SMTP服务" />
                </el-form-item>
                <el-form-item label="收件邮箱">
                  <el-input :model-value="settingsStore.settings.email_recipients"
                    @update:model-value="(v: any) => settingsStore.updateField('email_recipients', v)"
                    placeholder="receiver@qq.com" />
                  <div class="form-tip">多个收件人用英文逗号分隔</div>
                </el-form-item>
              </template>

              <!-- Server酱 配置 -->
              <template v-if="settingsStore.settings.notification_channel === 'serverchan'">
                <el-form-item label="SendKey">
                  <el-input :model-value="settingsStore.settings.serverchan_sendkey"
                    @update:model-value="(v: any) => settingsStore.updateField('serverchan_sendkey', v)"
                    type="password" show-password placeholder="SCTxxxxx...（从 sct.ftqq.com 获取）" />
                  <div class="form-tip">注册 <a href="https://sct.ftqq.com" target="_blank">Server酱 Turbo</a> 获取 SendKey</div>
                </el-form-item>
              </template>

              <el-form-item label="通知事件">
                <el-checkbox :model-value="settingsStore.settings.email_notify_start"
                  @update:model-value="(v: any) => settingsStore.updateField('email_notify_start', v)">开播</el-checkbox>
                <el-checkbox :model-value="settingsStore.settings.email_notify_stop"
                  @update:model-value="(v: any) => settingsStore.updateField('email_notify_stop', v)">停播</el-checkbox>
                <el-checkbox :model-value="settingsStore.settings.email_notify_complete"
                  @update:model-value="(v: any) => settingsStore.updateField('email_notify_complete', v)">任务完成</el-checkbox>
                <el-checkbox :model-value="settingsStore.settings.email_notify_error"
                  @update:model-value="(v: any) => settingsStore.updateField('email_notify_error', v)">异常告警</el-checkbox>
                <el-checkbox :model-value="settingsStore.settings.email_daily_summary"
                  @update:model-value="(v: any) => settingsStore.updateField('email_daily_summary', v)">每日简报</el-checkbox>
              </el-form-item>
              <el-form-item label="远程确认端口">
                <el-input-number :model-value="settingsStore.settings.email_face_verify_port"
                  @update:model-value="(v: any) => settingsStore.updateField('email_face_verify_port', v ?? 19080)"
                  :min="1024" :max="65535" />
                <div class="form-tip">人脸验证推送中的确认链接使用此端口，默认 19080</div>
              </el-form-item>
              <el-form-item>
                <el-button @click="sendTestEmail" :loading="sendingTestEmail" type="success" plain>
                  发送测试推送
                </el-button>
              </el-form-item>
            </el-form>
          </template>
          <div v-else style="color:#909399;padding:12px 0">开启后可接收开播/停播/异常通知和每日简报</div>
        </el-card>

        <!-- 版本更新 -->
        <el-card shadow="never" style="margin-top: 16px;" v-if="isElectron">
          <template #header>
            <div class="form-header"><span>版本更新</span></div>
          </template>
          <div class="update-info">
            <p>当前版本：{{ appVersion }}</p>
            <el-button @click="checkUpdate" :loading="checkingUpdate">
              检查更新
            </el-button>
            <span v-if="updateMsg" class="update-msg">{{ updateMsg }}</span>
          </div>
        </el-card>

        <el-card shadow="never" style="margin-top: 16px;">
          <template #header><span>关于</span></template>
          <div class="about-info">
            <p>直播控制系统 v1.0.0</p>
            <p>前端：Vue 3 + Element Plus</p>
            <p>后端：Python FastAPI</p>
            <p>桌面壳：Electron</p>
          </div>
        </el-card>
      </div>

    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useSettingsStore } from '@/stores/settings'
import type { AppSettings } from '@/types/api'
import Sidebar from '@/components/Sidebar.vue'

const router = useRouter()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()
const hasVideos = ref(true)  // 视频文件夹是否有视频
const loadingRtmp = ref(false)
const rtmpFullUrl = ref('')
const rtmpAddr = ref('')
const rtmpCode = ref('')
const rtmpMasked = ref(true)

function maskStreamKey(code: string): string {
  if (!code) return '暂无'
  // 掩码处理：保留前4个字符 + **** + 后4个字符
  if (code.length <= 8) return '****'
  return code.substring(0, 4) + '****' + code.substring(code.length - 4)
}

// 检查视频文件夹
async function checkVideos() {
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.get<{ has_videos: boolean }>('/api/settings/check-videos')
    hasVideos.value = res.has_videos
  } catch { hasVideos.value = false }
}
onMounted(() => { checkVideos(); fetchRtmpCode() })

async function fetchRtmpCode() {
  loadingRtmp.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.get<{ rtmp_addr: string; rtmp_code: string; full_url: string; room_id: string }>('/api/settings/rtmp-code')
    rtmpFullUrl.value = res.full_url || ''
    rtmpAddr.value = res.rtmp_addr || ''
    rtmpCode.value = res.rtmp_code || ''
  } catch { /* ignore */ }
  finally { loadingRtmp.value = false }
}

async function copyText(text: string) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动复制')
  }
}

// 测试邮件发送
const sendingTestEmail = ref(false)
async function sendTestEmail() {
  sendingTestEmail.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.post<{ success: boolean; message: string }>('/api/email/test')
    if (res.success) {
      ElMessage.success(res.message)
    } else {
      ElMessage.error(res.message)
    }
  } catch {
    ElMessage.error('发送失败，请检查邮箱配置')
  } finally {
    sendingTestEmail.value = false
  }
}

// 自动保存：监听设置变更，debounce 1.5s（首次加载不触发，保存回写不触发）
let _saveTimer: ReturnType<typeof setTimeout> | null = null
let _initialized = false
watch(() => settingsStore.settings, () => {
  if (!_initialized) { _initialized = true; return }
  if (settingsStore.isSaveGate()) return
  if (_saveTimer) clearTimeout(_saveTimer)
  _saveTimer = setTimeout(async () => {
    const ok = await settingsStore.saveSettings()
    if (ok) ElMessage.success('设置已自动保存')
  }, 1500)
}, { deep: true })

function goBack() {
  router.push({ name: 'Dashboard' })
}

function onLogout() {
  authStore.logout()
  router.push({ name: 'Login' })
}

async function selectDirectory() {
  if (window.electronAPI) {
    const dir = await window.electronAPI.selectDirectory()
    if (dir) settingsStore.updateField('video_path', dir)
  } else {
    ElMessage.info('文件选择功能仅在 Electron 桌面版中可用')
  }
}

// ==================== 版本更新 ====================
const isElectron = computed(() => !!window.electronAPI)
const appVersion = ref('1.0.0')
const checkingUpdate = ref(false)
const updateMsg = ref('')

async function checkUpdate() {
  if (!window.electronAPI) return
  checkingUpdate.value = true
  updateMsg.value = ''
  try {
    const result = await window.electronAPI.checkForUpdates()
    if (result.updateAvailable) {
      updateMsg.value = `发现新版本 ${result.latestVersion}，正在后台下载...`
    } else {
      updateMsg.value = result.message || '已是最新版本'
    }
  } catch {
    updateMsg.value = '检查更新失败'
  } finally {
    checkingUpdate.value = false
  }
}

onMounted(async () => {
  settingsStore.fetchSettings()
  if (window.electronAPI) {
    try {
      appVersion.value = await window.electronAPI.getAppVersion()
    } catch { /* ignore */ }
  }
})
</script>

<style scoped>
.settings-page {
  height: 100vh;
  position: relative;
}

.main-content {
  background: #f5f7fa;
  padding: 20px;
  padding-left: 30px;;
  padding-bottom: 80px;
}

.save-fab {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 1000;
}

.settings-form {
  margin: 20px auto 0;
  width: clamp(480px, 90%, 800px);
}

.settings-form :deep(.el-card__body) {
  padding: clamp(16px, 2.5vw, 28px);
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.unit {
  margin-left: 8px;
  color: #909399;
}

.about-info p {
  margin: 4px 0;
  color: #606266;
}

.form-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.update-info {
  display: flex;
  align-items: center;
  gap: 16px;
}
.update-info p { color: #606266; }
.update-msg { color: #00a1d6; font-size: 13px; }
</style>