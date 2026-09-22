<template>
  <div class="app-root">
    <div v-if="phase !== 'ready'" class="boot-screen" role="status" aria-live="polite">
      <div class="boot-card">
        <div class="boot-title">直播控制系统</div>

        <template v-if="phase === 'connecting'">
          <div class="boot-spinner" aria-hidden="true"></div>
          <div class="boot-line">{{ BOOT_MESSAGES.connecting }}</div>
          <div class="boot-hint">首次进入需要读取登录状态，请稍候</div>
        </template>

        <template v-else>
          <div class="boot-line boot-line-failed">{{ BOOT_MESSAGES.failed }}</div>
          <div class="boot-hint">{{ failureHint }}</div>
          <div class="boot-actions">
            <el-button type="primary" :loading="retrying" @click="onRetry">
              重试
            </el-button>
            <el-button @click="onReload">重新加载</el-button>
          </div>
        </template>
      </div>
    </div>

    <router-view v-else />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { boot, BOOT_MESSAGES, BOOT_FAILURE_HINTS } from '@/boot'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'

const router = useRouter()
const taskStore = useTaskStore()
const liveStore = useLiveStore()

const phase = ref(boot.phase)
const failure = ref(boot.failure)
const retrying = ref(false)
let businessActive = false
let drivenGeneration = -1

const failureHint = computed(() => {
  const kind = failure.value?.kind
  return kind ? BOOT_FAILURE_HINTS[kind] : ''
})

function syncBusinessLoads() {
  const result = boot.result
  const shouldRun = boot.phase === 'ready' && boot.isCurrent(result)
                    && !!result && result.ok && result.loggedIn
  if (shouldRun && !businessActive) {
    businessActive = true
    liveStore.startEventPolling()
    void taskStore.fetchTasks()
  } else if (!shouldRun && businessActive) {
    businessActive = false
    liveStore.stopEventPolling()
    liveStore.stopStatusReads()
    taskStore.stopTaskReads()
  }
}

async function resumeNavigation() {
  const target = boot.pendingPath
  if (!target) return
  boot.pendingPath = null
  const current = router.currentRoute.value
  if (!current.matched.length || current.fullPath !== target) {
    try {
      await router.replace(target)
    } catch {
      // A later auth generation may reject the pending navigation.
    }
  }
}

async function driveBoot(): Promise<void> {
  const snapshot = boot.snapshot
  if (boot.result !== null || snapshot.phase === 'failed') return
  if (drivenGeneration === snapshot.generation) return
  drivenGeneration = snapshot.generation
  await Promise.resolve()
  if (boot.snapshot.generation !== snapshot.generation) return
  const result = await boot.ensure()
  if (!boot.isCurrent(result)) return
  if (boot.phase === 'ready') await resumeNavigation()
  syncBusinessLoads()
}

// 应用级托盘退出确认必须保留在根组件；任务页和设置页也能安全退出。
const closeGuard = ref(false)

async function requestQuit(stopLive: boolean): Promise<void> {
  try {
    const result = await window.electronAPI?.confirmQuit(stopLive)
    // The main process keeps the window alive when local backend cleanup is
    // not confirmed.  Release the renderer guard so a later tray event can
    // retry; the main process owns the user-facing failure dialog.
    if (result && !result.success) closeGuard.value = false
  } catch {
    // An IPC rejection is also a failed quit attempt.  Keep the app usable and
    // permit a subsequent tray attempt instead of leaving the guard latched.
    closeGuard.value = false
  }
}

function handleTrayQuit() {
  if (closeGuard.value) return
  closeGuard.value = true
  if (liveStore.status.is_streaming) {
    ElMessageBox.confirm(
      '正在直播中，关闭应用将同时停止直播。\n\n是否停止直播并退出？',
      '确认退出',
      {
        confirmButtonText: '停止并退出',
        cancelButtonText: '不停止并退出',
        distinguishCancelAndClose: true,
        type: 'warning',
      },
    ).then(() => requestQuit(true)).catch((action: string) => {
      closeGuard.value = false
      if (action === 'cancel') window.electronAPI?.forceQuit()
    })
  } else {
    ElMessageBox.confirm(
      '确定要退出应用吗？',
      '确认退出',
      { confirmButtonText: '退出', cancelButtonText: '取消', type: 'info' },
    ).then(() => requestQuit(false)).catch(() => { closeGuard.value = false })
  }
}

const unsubscribe = boot.subscribe((snapshot) => {
  phase.value = snapshot.phase
  failure.value = snapshot.failure
  if (snapshot.phase !== 'connecting') retrying.value = false
  void driveBoot()
  syncBusinessLoads()
})

onMounted(async () => {
  if (window.electronAPI) window.electronAPI.onTrayQuit(handleTrayQuit)
  await driveBoot()
  syncBusinessLoads()
})

async function onRetry() {
  retrying.value = true
  try {
    await boot.retry()
  } finally {
    retrying.value = false
  }
  drivenGeneration = boot.generation
  if (boot.phase === 'ready') await resumeNavigation()
  syncBusinessLoads()
}

function onReload() {
  try {
    globalThis.location?.reload()
  } catch {
    // Ignore an unavailable location in a test shell.
  }
}

onUnmounted(() => {
  unsubscribe()
  businessActive = false
  liveStore.stopEventPolling()
  liveStore.stopStatusReads()
  taskStore.stopTaskReads()
})
</script>

<style scoped>
.app-root {
  min-height: 100vh;
}

.boot-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  box-sizing: border-box;
  background: #f5f7fa;
}

.boot-card {
  max-width: 420px;
  width: 100%;
  padding: 32px 28px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
  text-align: center;
}

.boot-title {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 20px;
}

.boot-spinner {
  width: 28px;
  height: 28px;
  margin: 0 auto 14px;
  border: 3px solid #e4e7ed;
  border-top-color: #00a1d6;
  border-radius: 50%;
  animation: boot-spin 0.9s linear infinite;
}

@keyframes boot-spin {
  to { transform: rotate(360deg); }
}

.boot-line { font-size: 15px; color: #303133; }
.boot-line-failed { color: #e6a23c; }
.boot-hint { margin-top: 8px; font-size: 13px; color: #909399; line-height: 1.6; }
.boot-actions { margin-top: 18px; display: flex; gap: 12px; justify-content: center; }
</style>
