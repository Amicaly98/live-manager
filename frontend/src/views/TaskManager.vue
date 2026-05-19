<template>
  <el-container class="task-manager">
    <!-- 复用侧边栏 -->
    <Sidebar
      :user-name="authStore.userInfo?.uname"
      :user-avatar="authStore.userInfo?.face"
      @logout="onLogout"
    />

    <el-main class="main-content">
      <el-page-header @back="goBack" title="任务管理">
        <template #content>
          <span>管理直播任务列表</span>
        </template>
      </el-page-header>

      <!-- 工具栏 -->
      <div class="toolbar">
        <el-button type="primary" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>
          新建任务
        </el-button>
        <el-button @click="reloadTasks" :loading="taskStore.isLoading">
          <el-icon><Refresh /></el-icon>
          重新加载
        </el-button>
        <el-button @click="refreshAreas" :loading="refreshingAreas">
          <el-icon><RefreshRight /></el-icon>
          刷新分区
        </el-button>
        <input
          ref="fileInputRef"
          type="file"
          accept=".xlsx"
          style="display:none"
          @change="onFileSelected"
        />
        <el-button @click="fileInputRef?.click()">
          <el-icon><Upload /></el-icon>
          导入 Excel
        </el-button>
        <el-button @click="handleExport" :loading="exporting">
          <el-icon><Download /></el-icon>
          导出 Excel
        </el-button>
        <span class="stats-info">
          共 {{ taskStore.stats.total }} 个任务，{{ taskStore.stats.today_pending }} 个今日待执行
        </span>
      </div>

      <!-- 任务列表 -->
      <el-table
        :data="taskStore.tasks"
        stripe
        style="width: 100%"
        max-height="calc(100vh - 139px)"
        v-loading="taskStore.isLoading"
        empty-text="暂无任务数据"
      >
        <el-table-column type="index" width="44" fixed="left" align="center" />

        <el-table-column prop="zone_name" label="分区" width="140" fixed="left">
          <template #default="{ row }">
            <div class="zone-cell">
              <span>{{ row.zone_name }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="priority" label="优先度" min-width="70" align="center" />

        <el-table-column label="进度" min-width="150" align="center">
          <template #default="{ row }">
            <el-progress
              :percentage="Math.round((row.days_done / row.actual_days) * 100)"
              :stroke-width="16"
              :text-inside="true"
              :status="row.is_completed ? 'success' : ''"
            >
              {{ row.days_done }}/{{ row.actual_days }}
            </el-progress>
          </template>
        </el-table-column>

        <el-table-column label="今日状态" min-width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_completed" type="success" size="small">全部完成</el-tag>
            <el-tag v-else-if="streamingZone === row.zone_name" type="warning" size="small">正在执行</el-tag>
            <el-tag v-else-if="row.today_done === 1" type="success" size="small">今日完成</el-tag>
            <el-tag v-else type="info" size="small">待执行</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" min-width="400" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.needs_execution"
              type="primary"
              size="small"
              :disabled="liveStore.status.is_streaming"
              @click="confirmStartLive(row.zone_name)"
            >
              {{ liveStore.status.is_streaming ? '直播中' : '立即开播' }}
            </el-button>
            <el-button
              v-if="row.needs_execution"
              size="small"
              :disabled="streamingZone === row.zone_name"
              @click="confirmMarkDone(row.zone_name)"
            >
              标记完成
            </el-button>
            <el-button
              v-if="row.needs_execution"
              type="success"
              size="small"
              :disabled="streamingZone === row.zone_name"
              @click="confirmMarkAllDone(row.zone_name)"
            >
              全部完成
            </el-button>
            <el-button
              size="small"
              :disabled="streamingZone === row.zone_name"
              @click="openEditDialog(row)"
            >
              编辑
            </el-button>
            <el-button
              type="danger"
              size="small"
              :disabled="streamingZone === row.zone_name"
              @click="confirmDelete(row.zone_name)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 新建/编辑任务对话框 -->
      <el-dialog
        v-model="showCreateDialog"
        :title="editingZone ? '编辑任务' : '新建任务'"
        width="520px"
        @closed="resetForm"
      >
        <el-form :model="taskForm" label-width="110px">
          <el-form-item label="分区名" required>
            <el-select
              v-model="taskForm.zone_name"
              filterable
              remote
              reserve-keyword
              :remote-method="searchTaskAreas"
              :loading="searchingTaskAreas"
              placeholder="输入分区名搜索"
              :disabled="!!editingZone"
              clearable
              style="width:100%"
            >
              <el-option
                v-for="item in taskAreaOptions"
                :key="item.id"
                :label="item.name"
                :value="item.name"
              >
                <span>{{ item.name }}</span>
                <span v-if="item.parent_name" style="color:#c0c4cc;font-size:12px;margin-left:4px">
                  — {{ item.parent_name }}
                </span>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="状态" required>
            <el-radio-group v-model="taskStatus">
              <el-radio value="pending">待完成</el-radio>
              <el-radio value="completed">已完成</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="taskStatus === 'pending' && !!editingZone" label=" ">
            <el-checkbox v-model="taskTodayDone" size="small">
              今日已完成
            </el-checkbox>
            <el-text size="small" type="info" style="margin-left:8px">取消勾选将清除今日完成状态，改回待执行</el-text>
          </el-form-item>
          <el-form-item v-if="taskStatus === 'pending'" label="每天直播时长" required>
            <el-input-number v-model="taskHours" :min="1" :max="24" />
            <span style="margin-left:8px;color:#909399">小时/天</span>
          </el-form-item>
          <el-form-item label="需要完成天数" required>
            <el-input-number v-model="taskForm.total_days" :min="1" :max="999" />
          </el-form-item>
          <el-form-item label="已完成天数">
            <el-input-number v-model="taskForm.days_done" :min="0" :max="999" />
          </el-form-item>
          <el-form-item label="截止日期" required>
            <el-date-picker
              v-model="deadlineDate"
              type="date"
              placeholder="选择日期"
              format="YYYY/MM/DD"
              value-format="YYYY-MM-DD"
              style="width:100%"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="showCreateDialog = false">取消</el-button>
          <el-button type="primary" @click="submitTask" :loading="submitting">
            {{ editingZone ? '保存修改' : '创建任务' }}
          </el-button>
        </template>
      </el-dialog>
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'
import { useAuthStore } from '@/stores/auth'
import Sidebar from '@/components/Sidebar.vue'
import type { TaskItem, TaskCreate, TaskUpdate } from '@/types/api'

const router = useRouter()
const taskStore = useTaskStore()
const liveStore = useLiveStore()
const authStore = useAuthStore()

// 任务模式直播中时禁用管理操作
const isTaskModeStreaming = computed(() =>
  liveStore.status.is_streaming && liveStore.status.stream_mode === 'task'
)
// 当前正在直播的分区名
const streamingZone = computed(() =>
  isTaskModeStreaming.value ? liveStore.status.current_zone : ''
)

function formatTime(d?: Date) {
  const t = d || new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${pad(t.getMonth()+1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`
}

// 简易事件记录（供跨页面展示）
function addEvent(tag: string, type: 'success' | 'danger' | 'warning' | 'info', message: string) {
  try {
    const stored = localStorage.getItem('app_events')
    const events = stored ? JSON.parse(stored) : []
    events.push({ tag, type, message, time: formatTime() })
    localStorage.setItem('app_events', JSON.stringify(events.slice(-50)))
  } catch { /* ignore */ }
}

const refreshingAreas = ref(false)
const exporting = ref(false)
const submitting = ref(false)

// 对话框状态
const showCreateDialog = ref(false)
const editingZone = ref<string | null>(null)
const taskStatus = ref<'pending' | 'completed'>('pending')
const taskHours = ref(2)
const taskTodayDone = ref(false)  // 编辑时：今日是否已完成
const deadlineDate = ref<string | null>(null)

// 分区搜索
const searchingTaskAreas = ref(false)
const taskAreaOptions = ref<{ id: number; name: string; parent_name?: string }[]>([])

const taskForm = reactive<TaskCreate>({
  zone_name: '',
  category: 2,
  total_days: 1,
  days_done: 0,
  deadline_raw: '',
})

function resetForm() {
  editingZone.value = null
  taskForm.zone_name = ''
  taskStatus.value = 'pending'
  taskHours.value = 2
  taskTodayDone.value = false
  taskForm.total_days = 1
  taskForm.days_done = 0
  deadlineDate.value = null
  taskForm.deadline_raw = ''
}

async function searchTaskAreas(keyword: string) {
  if (!keyword || !keyword.trim()) { taskAreaOptions.value = []; return }
  searchingTaskAreas.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const res = await req.get<{ results: { id: number; name: string; parent_name?: string }[] }>(
      `/api/live/areas/search?keyword=${encodeURIComponent(keyword)}`
    )
    taskAreaOptions.value = res.results || []
  } catch { taskAreaOptions.value = [] }
  finally { searchingTaskAreas.value = false }
}

function openEditDialog(row: TaskItem) {
  editingZone.value = row.zone_name
  taskForm.zone_name = row.zone_name
  if (row.category === 0) {
    taskStatus.value = 'completed'
    taskHours.value = 2
    taskTodayDone.value = false
  } else {
    taskStatus.value = 'pending'
    taskHours.value = row.category
    taskTodayDone.value = row.today_done === 1
  }
  taskForm.total_days = row.total_days
  taskForm.days_done = row.days_done
  deadlineDate.value = parseDeadlineToPicker(row.deadline_raw)
  taskForm.deadline_raw = ''
  showCreateDialog.value = true
}

function parseDeadlineToPicker(raw: string | undefined | null): string | null {
  if (!raw) return null
  const s = raw.trim()
  // =DATE(2026,12,31)
  const m = s.match(/=DATE\((\d+),\s*(\d+),\s*(\d+)\)/i)
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`
  // 2026-12-31 或 2026/12/31
  const d = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/)
  if (d) return `${d[1]}-${d[2].padStart(2, '0')}-${d[3].padStart(2, '0')}`
  return null
}

async function submitTask() {
  if (!taskForm.zone_name.trim()) {
    ElMessage.warning('分区名不能为空')
    return
  }
  if (!deadlineDate.value) {
    ElMessage.warning('请选择截止日期')
    return
  }

  // 分区名校验（完全匹配）
  try {
    const { useRequest } = await import('@/api/request')
    const req = useRequest()
    const searchRes = await req.get<{ results: { name: string }[] }>(
      '/api/areas/search', { keyword: taskForm.zone_name.trim() })
    const exactMatch = searchRes.results?.some(
      r => r.name === taskForm.zone_name.trim()
    )
    if (!exactMatch) {
      try {
        await ElMessageBox.confirm(
          `分区「${taskForm.zone_name.trim()}」不存在或已下架，请检查拼写或符号错误。\n\n是否仍要继续${editingZone.value ? '修改' : '创建'}？`,
          '分区不存在',
          { confirmButtonText: '继续', cancelButtonText: '返回修改', type: 'warning' },
        )
      } catch {
        return // 用户选择返回修改
      }
    }
  } catch {
    // 搜索失败（网络问题等），允许继续
  }

  submitting.value = true
  try {
    const category = taskStatus.value === 'completed' ? 0 : taskHours.value
    const data: TaskCreate = {
      zone_name: taskForm.zone_name.trim(),
      category,
      total_days: taskForm.total_days,
      days_done: taskForm.days_done,
      deadline_raw: deadlineDate.value,
    }
    if (editingZone.value) {
      const updateData: TaskUpdate = { ...data, today_done: taskTodayDone.value ? 1 : 0 }
      delete (updateData as any).zone_name
      const success = await taskStore.updateTask(editingZone.value, updateData)
      if (success) {
        ElMessage.success('任务已更新')
        showCreateDialog.value = false
      } else {
        ElMessage.error('更新失败')
      }
    } else {
      await doCreateTask(data)
    }
  } finally {
    submitting.value = false
  }
}

async function doCreateTask(data: TaskCreate, overwrite: boolean = false) {
  // 新建模式下先检查是否已存在同名任务（从已加载的任务列表中）
  if (!overwrite) {
    const existing = taskStore.tasks.find(t => t.zone_name === data.zone_name)
    if (existing) {
      try {
        await ElMessageBox.confirm(
          `任务「${data.zone_name}」已存在，是否覆盖原有任务？`,
          '任务已存在',
          {
            confirmButtonText: '覆盖',
            cancelButtonText: '取消',
            type: 'warning',
            distinguishCancelAndClose: true,
          },
        )
        // 用户选择覆盖
        return await doCreateTask(data, true)
      } catch {
        // 用户取消或关闭
        ElMessage.info('已取消创建')
        return false
      }
    }
  }

  try {
    await taskStore.createTask(data, overwrite)
    ElMessage.success(overwrite ? '任务已覆盖' : '任务已创建')
    showCreateDialog.value = false
    return true
  } catch (error: any) {
    // 如果本地列表未及时同步导致后端仍报重名，兜底处理
    const detail = error?.response?.data?.detail || error?.message || ''
    if (!overwrite && (detail.includes('已存在同名') || detail.includes('UNIQUE constraint'))) {
      try {
        await ElMessageBox.confirm(
          `任务「${data.zone_name}」已存在，是否覆盖原有任务？`,
          '任务已存在',
          {
            confirmButtonText: '覆盖',
            cancelButtonText: '取消',
            type: 'warning',
            distinguishCancelAndClose: true,
          },
        )
        return await doCreateTask(data, true)
      } catch {
        ElMessage.info('已取消创建')
        return false
      }
    }
    ElMessage.error(overwrite ? '覆盖失败' : '创建失败')
    return false
  }
}

async function confirmDelete(zoneName: string) {
  try {
    await ElMessageBox.confirm(
      `确定要删除任务「${zoneName}」吗？此操作不可撤销。`,
      '确认删除',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  const success = await taskStore.deleteTask(zoneName)
  if (success) {
    ElMessage.success(`任务「${zoneName}」已删除`)
  } else {
    ElMessage.error('删除失败')
  }
}

// Excel 导入导出
const fileInputRef = ref<HTMLInputElement | null>(null)
const importFileRef = ref<File | null>(null)

function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  handleImport(file)
  // 清空 input 以允许重复选同一文件
  input.value = ''
}

async function handleImport(file: File) {
  importFileRef.value = file

  // 第一步：选择导入模式
  let mode: 'merge' | 'replace' = 'merge'
  try {
    await ElMessageBox.confirm(
      '请选择导入模式：\n\n【合并】新增不存在的分区 + 更新同名分区\n【全覆盖】清空全部现有任务后导入（不可撤销）',
      '导入模式',
      {
        confirmButtonText: '全覆盖',
        cancelButtonText: '合并',
        distinguishCancelAndClose: true,
        type: 'info',
      },
    )
    mode = 'replace'
  } catch (action: any) {
    if (action === 'cancel') {
      mode = 'merge'
    } else {
      ElMessage.info('已取消导入')
      importFileRef.value = null
      return
    }
  }

  // 全覆盖模式 → 二次确认
  if (mode === 'replace') {
    try {
      await ElMessageBox.confirm(
        '【全覆盖模式】将清空所有现有任务，然后导入 Excel 中的全部任务。此操作不可撤销，确定继续？',
        '危险操作',
        { confirmButtonText: '确认清空并导入', cancelButtonText: '取消', type: 'error',
          confirmButtonClass: 'el-button--danger' },
      )
    } catch {
      ElMessage.info('已取消导入')
      importFileRef.value = null
      return
    }
  }

  // 执行导入
  const result = await taskStore.importFromExcel(file, false, mode)

  // 分区不存在 → 确认是否跳过
  if (result.needs_confirmation && result.invalid_zones && result.invalid_zones.length > 0) {
    try {
      await ElMessageBox.confirm(
        `${result.message}：\n\n${result.invalid_zones.join('、')}\n\n是否跳过以上分区继续导入？`,
        '分区不存在',
        { confirmButtonText: '继续导入（跳过无效）', cancelButtonText: '取消', type: 'warning' },
      )
      const forceResult = await taskStore.importFromExcel(file, true, mode)
      if (forceResult.success) {
        ElMessage.success(forceResult.message)
      } else {
        ElMessage.error(forceResult.message)
      }
    } catch {
      ElMessage.info('已取消导入')
    }
    importFileRef.value = null
    return
  }

  if (result.success) {
    let msg = result.message
    if (result.errors && result.errors.length > 0) {
      msg += `；跳过原因：${result.errors.join('；')}`
    }
    ElMessage.success(msg)
    addEvent('导入', 'success', `Excel 导入完成 — ${msg}`)
  } else {
    ElMessage.error(result.message || '导入失败')
  }
  importFileRef.value = null
}

async function handleExport() {
  exporting.value = true
  try {
    const ok = await taskStore.exportToExcel()
    if (ok) {
      ElMessage.success('导出成功')
    } else {
      ElMessage.error('导出失败')
    }
  } finally {
    exporting.value = false
  }
}

function goBack() {
  router.push({ name: 'Dashboard' })
}

function onLogout() {
  authStore.logout()
  router.push({ name: 'Login' })
}

async function reloadTasks() {
  const success = await taskStore.reloadTasks()
  if (success) {
    ElMessage.success('任务已重新加载')
  } else {
    ElMessage.error('加载失败')
  }
}

async function startLiveForZone(zoneName: string) {
  const result = await liveStore.startLive(zoneName)
  if (result.success) {
    ElMessage.success(result.message)
  } else {
    ElMessage.error(result.message)
  }
}

async function confirmStartLive(zoneName: string) {
  // 选择模式
  let mode: 'task' | 'manual' = 'manual'
  try {
    await ElMessageBox.confirm(
      `在分区「${zoneName}」开播：\n\n【单任务模式】自动执行下一个待完成任务\n【手动模式】手动指定时长，单次开播`,
      '选择开播模式',
      {
        confirmButtonText: '手动模式',
        cancelButtonText: '单任务模式',
        distinguishCancelAndClose: true,
        type: 'info',
      },
    )
    mode = 'manual'
  } catch (action: any) {
    if (action === 'cancel') {
      mode = 'task'
    } else {
      return
    }
  }

  if (mode === 'task') {
    // 单任务模式：检查是否有进行中的任务
    try {
      const { useRequest } = await import('@/api/request')
      const req = useRequest()
      const stateRes = await req.get<{ has_active: boolean; current_zone?: string }>('/api/live/state')
      if (stateRes.has_active && stateRes.current_zone) {
        try {
          await ElMessageBox.confirm(
            `存在进行中的任务「${stateRes.current_zone}」，是否覆盖并开始新任务？`,
            '进行中的任务',
            { confirmButtonText: '覆盖', cancelButtonText: '取消', type: 'warning' },
          )
        } catch { return }
      }
    } catch { /* state check failed, proceed anyway */ }
    // 单任务模式：不指定分区，后端自动取下一个任务
    const result = await liveStore.startLive()
    if (result.success) { ElMessage.success(result.message) }
    else { ElMessage.error(result.message) }
  } else {
    await startLiveForZone(zoneName)
  }
}

async function confirmMarkDone(zoneName: string) {
  try {
    await ElMessageBox.confirm(`确定将「${zoneName}」标记为今日完成吗？`, '确认标记', {
      confirmButtonText: '确认标记', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  await markDone(zoneName)
}

async function confirmMarkAllDone(zoneName: string) {
  try {
    await ElMessageBox.confirm(
      `确定将「${zoneName}」标记为全部完成吗？此操作不可撤销。`,
      '确认全部完成',
      { confirmButtonText: '确认全部完成', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  const success = await taskStore.markTaskAllDone(zoneName)
  if (success) {
    ElMessage.success(`「${zoneName}」已标记为全部完成`)
  } else {
    ElMessage.error('标记全部完成失败')
  }
}

async function markDone(zoneName: string) {
  const success = await taskStore.markTaskDone(zoneName)
  if (success) {
    ElMessage.success(`任务 ${zoneName} 已标记完成`)
  } else {
    ElMessage.error('标记失败')
  }
}

async function refreshAreas() {
  refreshingAreas.value = true
  try {
    const { useRequest } = await import('@/api/request')
    const request = useRequest()
    const res = await request.post<{ success: boolean; message: string }>('/api/areas/refresh')
    ElMessage.success(res.message)
  } catch (error) {
    ElMessage.error('刷新分区失败')
  } finally {
    refreshingAreas.value = false
  }
}
</script>

<style scoped>
.task-manager {
  height: 100vh;
}

.main-content {
  background: #f5f7fa;
  padding: 20px;
  overflow: auto;
  height: 100vh;
  box-sizing: border-box;
}

.toolbar {
  margin: 20px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.stats-info {
  margin-left: auto;
  color: #909399;
  font-size: 13px;
}

.zone-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

:deep(.el-progress-bar__innerText) { color: #303133 !important; font-weight: bold; }

/* 优先度列紧凑 */
:deep(.el-table__body-wrapper) { overflow-x: auto; }
</style>