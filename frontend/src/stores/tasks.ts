import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useRequest } from '@/api/request'
import cache from '@/composables/useCache'
import type {
  TaskItem,
  TaskDetail,
  TaskStats,
  TaskListResponse,
  TaskDetailResponse,
  TaskCreate,
  TaskUpdate,
  MarkDoneResponse,
  ReloadResponse,
  ImportResult,
} from '@/types/api'

export const useTaskStore = defineStore('tasks', () => {
  const tasks = ref<TaskItem[]>([])
  const taskDetails = ref<TaskDetail[]>([])
  const stats = ref<TaskStats>({ total: 0, pending_total: 0, completed: 0, today_done: 0, today_pending: 0, remaining_time: 0, avg_remaining: 0, urgency: 0 })
  const isLoading = ref(false)

  const request = useRequest()

  // 计算当前应该执行的任务
  const nextTask = computed(() => {
    return tasks.value.find(t => t.needs_execution)
  })

  // 获取任务列表
  async function fetchTasks() {
    isLoading.value = true
    try {
      const res = await request.get<TaskListResponse>('/api/tasks')
      tasks.value = res.tasks
      stats.value = {
        total: res.total,
        pending_total: res.pending_total,
        completed: res.completed,
        today_done: res.today_done,
        today_pending: res.today_pending,
        remaining_time: res.remaining_time,
        avg_remaining: res.avg_remaining,
        urgency: res.urgency,
      }
    } catch (error) {
      console.error('获取任务列表失败:', error)
      const cached = cache.get<{ tasks: TaskItem[]; stats: TaskStats }>('tasks_cache')
      if (cached) {
        tasks.value = cached.tasks
        stats.value = cached.stats
      }
    } finally {
      isLoading.value = false
    }
  }

  // 获取任务详情（含 ID 和计算列）
  async function fetchTaskDetails() {
    isLoading.value = true
    try {
      const res = await request.get<TaskDetailResponse>('/api/tasks/detail')
      taskDetails.value = res.tasks
      stats.value = {
        total: res.total,
        pending_total: res.pending_total,
        completed: res.completed,
        today_done: res.today_done,
        today_pending: res.today_pending,
        remaining_time: res.remaining_time,
        avg_remaining: res.avg_remaining,
        urgency: res.urgency,
      }
    } catch (error) {
      console.error('获取任务详情失败:', error)
    } finally {
      isLoading.value = false
    }
  }

  // 创建任务
  async function createTask(data: TaskCreate, overwrite: boolean = false) {
    try {
      const params = new URLSearchParams()
      if (overwrite) params.set('overwrite', 'true')
      const qs = params.toString()
      const url = qs ? `/api/tasks?${qs}` : '/api/tasks'
      await request.post(url, data)
      await fetchTasks()
      return true
    } catch (error: any) {
      console.error('创建任务失败:', error)
      // 返回错误信息供调用方判断是否为重名
      throw error
    }
  }

  // 更新任务
  async function updateTask(zoneName: string, data: TaskUpdate) {
    try {
      await request.put(`/api/tasks/${encodeURIComponent(zoneName)}`, data)
      await fetchTasks()
      return true
    } catch (error) {
      console.error('更新任务失败:', error)
      return false
    }
  }

  // 删除任务
  async function deleteTask(zoneName: string) {
    try {
      await request.delete(`/api/tasks/${encodeURIComponent(zoneName)}`)
      await fetchTasks()
      return true
    } catch (error) {
      console.error('删除任务失败:', error)
      return false
    }
  }

  // 标记任务完成
  async function markTaskDone(zoneName: string) {
    try {
      await request.post<MarkDoneResponse>(
        `/api/tasks/mark-done/${encodeURIComponent(zoneName)}`
      )
      await fetchTasks()
      cache.set('tasks_cache', {
        tasks: tasks.value,
        stats: stats.value,
      })
      return true
    } catch (error) {
      console.error('标记任务失败:', error)
      return false
    }
  }

  // 标记任务全部完成
  async function markTaskAllDone(zoneName: string) {
    try {
      await request.post<MarkDoneResponse>(
        `/api/tasks/mark-all-done/${encodeURIComponent(zoneName)}`
      )
      await fetchTasks()
      return true
    } catch (error) {
      console.error('标记全部完成失败:', error)
      return false
    }
  }

  // 重新加载任务
  async function reloadTasks() {
    try {
      await request.post<ReloadResponse>('/api/tasks/reload')
      await fetchTasks()
      return true
    } catch (error) {
      console.error('重新加载任务失败:', error)
      return false
    }
  }

  // 从 Excel 导入（支持分区校验确认流程 + 合并/全覆盖模式）
  async function importFromExcel(file: File, force: boolean = false, mode: string = 'merge'): Promise<ImportResult> {
    try {
      const formData = new FormData()
      formData.append('file', file)
      const params = new URLSearchParams()
      if (force) params.set('force', 'true')
      if (mode !== 'merge') params.set('mode', mode)
      const qs = params.toString()
      const baseUrl = (window as any).electronAPI ? 'http://127.0.0.1:8000' : ''
      const url = qs ? `${baseUrl}/api/tasks/import?${qs}` : `${baseUrl}/api/tasks/import`
      const response = await fetch(url, {
        method: 'POST',
        body: formData,
      })
      if (!response.ok) throw new Error('导入失败')
      const res = await response.json() as ImportResult
      if (res.success) {
        await fetchTasks()
        cache.set('tasks_cache', { tasks: tasks.value, stats: stats.value })
      }
      return res
    } catch (error) {
      console.error('导入失败:', error)
      return { success: false, imported_count: 0, message: '导入失败', errors: [] }
    }
  }

  // 导出到 Excel
  async function exportToExcel() {
    try {
      const baseUrl = (window as any).electronAPI ? 'http://127.0.0.1:8000' : ''
      const response = await fetch(`${baseUrl}/api/tasks/export`)
      if (!response.ok) throw new Error('导出失败')
      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // 使用本地时区时间，格式: live_tasks_2026-05-11_12-03-16.xlsx
      const now = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`
      a.download = `live_tasks_${ts}.xlsx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      window.URL.revokeObjectURL(url)
      return true
    } catch (error) {
      console.error('导出失败:', error)
      return false
    }
  }

  return {
    tasks,
    taskDetails,
    stats,
    isLoading,
    nextTask,
    fetchTasks,
    fetchTaskDetails,
    createTask,
    updateTask,
    deleteTask,
    markTaskDone,
    markTaskAllDone,
    reloadTasks,
    importFromExcel,
    exportToExcel,
  }
})