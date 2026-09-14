<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'

const authStore = useAuthStore()
const taskStore = useTaskStore()
const liveStore = useLiveStore()

// A7(5)：托盘退出确认注册在根组件——不再由 Dashboard.vue 独占应用级
// IPC 事件，用户在任务页/设置页同样可以完成退出。
const _closeGuard = ref(false)

function handleTrayQuit() {
  if (_closeGuard.value) return
  _closeGuard.value = true
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
    ).then(() => {
      window.electronAPI?.confirmQuit(true)
    }).catch((action: string) => {
      _closeGuard.value = false
      if (action === 'cancel') {
        window.electronAPI?.forceQuit()
      }
    })
  } else {
    ElMessageBox.confirm(
      '确定要退出应用吗？',
      '确认退出',
      { confirmButtonText: '退出', cancelButtonText: '取消', type: 'info' },
    ).then(() => {
      window.electronAPI?.confirmQuit(false)
    }).catch(() => { _closeGuard.value = false })
  }
}

onMounted(async () => {
  liveStore.startEventPolling()
  await Promise.all([
    authStore.checkLoginStatus(),
    taskStore.fetchTasks()
  ])
  if (window.electronAPI) {
    window.electronAPI.onTrayQuit(handleTrayQuit)
  }
})

onUnmounted(() => {
  liveStore.stopEventPolling()
})
</script>
