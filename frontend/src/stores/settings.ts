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
  notification_enabled: true,
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

export type SaveFailureKind = 'definitive' | 'transient' | 'cancelled'

/**
 * 把一次保存失败分类。
 *
 * - definitive：4xx（含 409 并发冲突 / 422 校验失败）——重试同样的内容还是会被
 *   拒，必须停下来等用户改内容或显式重试，**绝不能自动无限排队**；
 * - transient：网络中断 / 5xx——允许有界退避重试；
 * - cancelled：请求被取消——不重试，也不算失败。
 */
export function classifySaveFailure(error: unknown): SaveFailureKind {
  const err = error as { response?: { status?: number }, code?: string, message?: string }
  if (err?.code === 'ERR_CANCELED') return 'cancelled'
  const status = err?.response?.status
  if (typeof status === 'number' && status > 0) {
    return status >= 400 && status < 500 ? 'definitive' : 'transient'
  }
  const matched = /\b(\d{3})\b/.exec(String(err?.message || ''))
  if (matched) {
    const code = Number(matched[1])
    if (code >= 400 && code < 500) return 'definitive'
    if (code >= 500) return 'transient'
  }
  return 'transient'
}

/** transient 失败的有界自动重试上限（超过后停手，等用户或新编辑）。 */
export const MAX_SAVE_AUTO_RETRIES = 2

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AppSettings>({ ...defaultSettings })
  const isLoading = ref(false)
  const isSaving = ref(false)
  /** 最后一个成功的保存是否覆盖了当前全部修改（未保存提示据此显示）。 */
  const hasUnsavedChanges = ref(false)
  /** 保存已被明确失败阻塞：不再自动重试，等新编辑或显式重试。 */
  const saveBlocked = ref(false)
  /** 最近一次保存失败的可操作说明（面板据此提示）。 */
  const lastSaveError = ref('')
  /** 后端配置的磁盘版本：用于发现其他客户端刚改过配置。 */
  const settingsRevision = ref(0)

  // 串行保存 + 脏版本号：
  // - 旧实现保存期间 `_saveGate=true`，用户在窗口内的真实修改被 watcher 直接
  //   忽略（界面显示新值、后端仍是旧值）；
  // - 现在每次修改都会推进 `_dirtyRevision`，保存前记录当时版本，保存成功后若
  //   版本又变了就继续保存后一次，直到追平。
  // - **失败不自动重入队**：明确拒绝（4xx）直接停手；瞬态失败只允许有限几次
  //   退避重试。旧实现在 finally 里只看 dirty != saved，失败也会立刻再排一次，
  //   于是一次 422 会变成无限请求风暴。
  let _dirtyRevision = 0
  let _savedRevision = 0
  let _blockedRevision = -1
  let _autoRetries = 0
  let _applyingRemote = false
  let _saveChain: Promise<boolean> = Promise.resolve(true)
  // A settings page can unmount while its read is in flight.  Generation and
  // abort together prevent a late response from replacing a newer page's
  // edits or changing the save baseline behind its back.
  let _readGeneration = 0
  let _readController: AbortController | null = null

  const request = useRequest()

  /** 从后端加载设置（含磁盘版本号；版本号不进入业务字段） */
  async function fetchSettings() {
    const generation = ++_readGeneration
    _readController?.abort()
    const controller = new AbortController()
    _readController = controller
    const readDirtyRevision = _dirtyRevision
    isLoading.value = true
    try {
      const res = await request.get<AppSettings & {
        _revision?: number
      }>('/api/settings', undefined, { signal: controller.signal })
      // Ignore responses belonging to an old page/read, and any response
      // that crossed a user edit.  A dirty store is the user's working copy;
      // reloading it from disk would silently discard that copy and its
      // revision precondition.
      if (generation !== _readGeneration || controller.signal.aborted
          || _dirtyRevision !== readDirtyRevision
          || _dirtyRevision !== _savedRevision) return
      const raw = res as unknown as Record<string, unknown>
      const revision = Number(raw._revision)
      const next = { ...(res as unknown as AppSettings) } as Record<string, unknown>
      delete next._revision
      delete next._trust
      settings.value = next as unknown as AppSettings
      settingsRevision.value = Number.isFinite(revision) ? revision : 0
      // 重新载入 = 编辑基线重置：未保存提示随之清零。
      _savedRevision = _dirtyRevision
      _blockedRevision = -1
      _autoRetries = 0
      saveBlocked.value = false
      lastSaveError.value = ''
      hasUnsavedChanges.value = false
    } catch {
      // 加载失败或页面离开时保持当前工作副本；不要用默认值覆盖编辑。
    } finally {
      if (generation === _readGeneration) {
        isLoading.value = false
        if (_readController === controller) _readController = null
      }
    }
  }

  /** 页面离开时取消仍在途的设置读取；保存请求继续由 store 完成。 */
  function stopSettingsReads() {
    _readGeneration += 1
    _readController?.abort()
    _readController = null
    isLoading.value = false
  }

  /** 保存设置到后端（串行；保存期间的新修改会在其后继续保存） */
  function saveSettings(): Promise<boolean> {
    const chained = _saveChain.then(() => _saveOnce(), () => _saveOnce())
    _saveChain = chained
    return chained
  }

  /** 用户显式重试：清掉"已阻塞"标记后立刻提交当前内容。 */
  function retrySave(): Promise<boolean> {
    saveBlocked.value = false
    lastSaveError.value = ''
    _blockedRevision = -1
    _autoRetries = 0
    return saveSettings()
  }

  function _describeFailure(error: unknown, kind: SaveFailureKind): string {
    const err = error as { response?: { status?: number, data?: { detail?: string } }, message?: string }
    const detail = err?.response?.data?.detail
    if (detail) return String(detail)
    const status = err?.response?.status
    if (kind === 'definitive') {
      return status === 409
        ? '配置已被其他客户端修改，请刷新后重试（编辑内容已保留）'
        : `设置被服务端拒绝${status ? `（${status}）` : ''}，编辑内容已保留`
    }
    return '网络异常，设置未保存（编辑内容已保留）'
  }

  async function _saveOnce(): Promise<boolean> {
    const revision = _dirtyRevision
    if (revision === _savedRevision) return true  // 没有新改动，不发请求
    // 该版本已被明确失败阻塞：不自动重发，等新编辑或显式重试。
    if (saveBlocked.value && revision <= _blockedRevision) return false
    const snapshot = JSON.parse(JSON.stringify(settings.value)) as AppSettings
    isSaving.value = true
    try {
      const options = settingsRevision.value > 0
        ? { headers: { 'X-Settings-Revision': String(settingsRevision.value) } }
        : undefined
      const res = await request.put<AppSettings & {
        _revision?: number
      }>('/api/settings', snapshot, options)
      const raw = res as unknown as Record<string, unknown>
      const newRevision = Number(raw._revision)
      if (Number.isFinite(newRevision)) settingsRevision.value = newRevision
      _savedRevision = Math.max(_savedRevision, revision)
      _blockedRevision = -1
      _autoRetries = 0
      saveBlocked.value = false
      lastSaveError.value = ''
      hasUnsavedChanges.value = _dirtyRevision !== _savedRevision
      // 只在"这次请求就是最新的修改"时才回写后端修正值，
      // 否则会用旧响应覆盖用户在保存期间刚改的新值。
      if (_dirtyRevision === revision) {
        const corrected = raw
        _applyingRemote = true
        try {
          if (typeof corrected.duration_multiplier_min === 'number'
              && corrected.duration_multiplier_min !== settings.value.duration_multiplier_min) {
            settings.value.duration_multiplier_min = corrected.duration_multiplier_min as number
          }
          if (typeof corrected.duration_multiplier_max === 'number'
              && corrected.duration_multiplier_max !== settings.value.duration_multiplier_max) {
            settings.value.duration_multiplier_max = corrected.duration_multiplier_max as number
          }
        } finally {
          _applyingRemote = false
        }
      }
      return true
    } catch (error) {
      // 保存失败：保留编辑内容，明确标记"未保存"；**不**在这里自动重入队。
      hasUnsavedChanges.value = true
      const kind = classifySaveFailure(error)
      if (kind === 'cancelled') return false
      // A late response for an older snapshot must not block or describe the
      // newer edit.  The finally block will enqueue that newer revision.
      if (_dirtyRevision !== revision) {
        _autoRetries = 0
        saveBlocked.value = false
        lastSaveError.value = ''
        return false
      }
      if (kind === 'transient' && _autoRetries < MAX_SAVE_AUTO_RETRIES) {
        _autoRetries += 1
        const attempt = _autoRetries
        // 有界退避：不阻塞调用方，也不会无限循环。
        setTimeout(() => { void saveSettings() }, 1000 * attempt)
        return false
      }
      saveBlocked.value = true
      _blockedRevision = revision
      lastSaveError.value = _describeFailure(error, kind)
      return false
    } finally {
      isSaving.value = false
      // 保存期间又有修改：继续把后一次也落盘（不丢用户输入）。
      // 只有**本次成功**才继续链式保存——失败路径由上面的有界重试/阻塞负责。
      if (_dirtyRevision !== _savedRevision && !saveBlocked.value) {
        void saveSettings()
      }
    }
  }

  /** 更新单个设置项（记录脏版本） */
  function updateField<K extends keyof AppSettings>(key: K, value: AppSettings[K] | any) {
    settings.value[key] = value as AppSettings[K]
    if (!_applyingRemote) {
      _dirtyRevision += 1
      hasUnsavedChanges.value = true
      // 新的编辑意味着"失败的内容已经变了"：解除阻塞，允许再次提交。
      if (saveBlocked.value && _dirtyRevision > _blockedRevision) {
        saveBlocked.value = false
        lastSaveError.value = ''
        _autoRetries = 0
      }
    }
  }

  /** 兼容旧调用：现在不再需要用 gate 阻止 watcher 递归。 */
  function isSaveGate() { return false }

  return {
    settings, isLoading, isSaving, hasUnsavedChanges,
    saveBlocked, lastSaveError, settingsRevision,
    fetchSettings, stopSettingsReads, saveSettings, retrySave, updateField, isSaveGate,
  }
})
