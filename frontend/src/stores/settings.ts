import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useRequest } from '@/api/request'
import type { AppSettings } from '@/types/api'

const defaultSettings: AppSettings = {
  video_path: 'F:/videosforlive',
  excel_path: 'live_tasks.xlsx',
  db_path: 'live_tasks.db',
  scan_interval_seconds: 30,
  max_reconnect: 3,
  live_retry_cooldown_minutes: 60,
  stream_mode: 'manual',
  auto_open_video: true,
  ffmpeg_path: 'ffmpeg',
  ffmpeg_reencode: true,
  // 邮箱推送
  notification_channel: 'email',
  email_enabled: false,
  email_smtp_host: 'smtp.qq.com',
  email_smtp_port: 587,
  email_smtp_user: '',
  email_smtp_pass: '',
  email_recipients: '',
  email_notify_start: true,
  email_notify_stop: true,
  email_notify_error: true,
  email_notify_complete: true,
  email_daily_summary: true,
  email_face_verify_port: 19080,
  // Server酱
  serverchan_sendkey: '',
  duration_distribution: 'beta',
  duration_multiplier_min: 1.05,
  duration_multiplier_max: 1.25,
}

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AppSettings>({ ...defaultSettings })
  const isLoading = ref(false)
  const isSaving = ref(false)
  let _saveGate = false  // 防止保存回写触发 watcher 再次保存

  const request = useRequest()

  /** 从后端加载设置 */
  async function fetchSettings() {
    isLoading.value = true
    try {
      const res = await request.get<AppSettings>('/api/settings')
      settings.value = res
    } catch {
      // 加载失败使用默认值
    } finally {
      isLoading.value = false
    }
  }

  /** 保存设置到后端 */
  async function saveSettings(): Promise<boolean> {
    isSaving.value = true
    _saveGate = true
    try {
      const res = await request.put<AppSettings>('/api/settings', settings.value)
      // 仅回写可能被后端修正的关键字段
      const corrected = res as unknown as Record<string, unknown>
      if (typeof corrected.duration_multiplier_min === 'number') {
        settings.value.duration_multiplier_min = corrected.duration_multiplier_min as number
      }
      if (typeof corrected.duration_multiplier_max === 'number') {
        settings.value.duration_multiplier_max = corrected.duration_multiplier_max as number
      }
      return true
    } catch {
      return false
    } finally {
      isSaving.value = false
      // 延迟解除 gate，防止回写触发 watcher
      setTimeout(() => { _saveGate = false }, 200)
    }
  }

  /** 更新单个设置项 */
  function updateField<K extends keyof AppSettings>(key: K, value: AppSettings[K] | any) {
    settings.value[key] = value as AppSettings[K]
  }

  function isSaveGate() { return _saveGate }

  return { settings, isLoading, isSaving, fetchSettings, saveSettings, updateField, isSaveGate }
})
