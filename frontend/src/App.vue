<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'

const authStore = useAuthStore()
const taskStore = useTaskStore()
const liveStore = useLiveStore()

onMounted(async () => {
  liveStore.startEventPolling()
  await Promise.all([
    authStore.checkLoginStatus(),
    taskStore.fetchTasks()
  ])
})

onUnmounted(() => {
  liveStore.stopEventPolling()
})
</script>