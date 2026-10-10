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
        <el-button type="primary" @click="openCreateDialog">
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

      <!-- 回包丢失：数据库里可能已经建好了。每一项已发出、结果可能未知的创建
           各自保留原 UID + 原令牌 + 原冻结载荷（逐项展示、逐项核对/重试），
           复用同一个令牌与同一份冻结命令，不新增后台重试任务。 -->
      <el-alert
        v-for="item in pendingList"
        :key="item.token"
        class="pending-create"
        type="warning"
        :closable="false"
        show-icon
        title="上一次创建的结果待核对"
      >
        <template #default>
          <div class="pending-create-body">
            <span>
              分区「{{ item.zoneName }}」的创建请求没有得到回包，
              可能已经生效，也可能没有。
              <template v-if="item.scope.uid != null">
                这项待核对按<strong>发起时的账号（UID {{ item.scope.uid }}）</strong>
                与原令牌保留：刷新页面后仍在这里，换号后**不会**在新账号下重放。
              </template>
              <template v-else>
                发起时尚未取得账号身份，这项待核对只能按当前账号核对。
              </template>
              <span v-if="item.scope.uid != null && !pendingOwnedByCurrentAccount(item)"
                    class="muted">
                当前登录的是其他账号：核对与重发已停用（不会清除原命令）；
                切回 UID {{ item.scope.uid }} 后可继续。
              </span>
            </span>
            <span class="pending-create-actions">
              <el-button size="small" :loading="isPendingBusy(item.token)"
                         :disabled="!pendingOwnedByCurrentAccount(item)"
                         @click="checkPending(item)">
                核对结果
              </el-button>
              <el-button size="small" type="primary" :loading="isPendingBusy(item.token)"
                         :disabled="!pendingOwnedByCurrentAccount(item)"
                         @click="retryPending(item)">
                重试提交
              </el-button>
            </span>
          </div>
        </template>
      </el-alert>

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
            <el-tag v-else-if="executingLabel(row)" :type="executingTagType(row)" size="small">
              {{ executingLabel(row) }}
            </el-tag>
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
              :disabled="!canStartNew"
              @click="confirmStartLive(row)"
            >
              {{ canStartNew ? '立即开播' : startBlockedLabel }}
            </el-button>
            <el-button
              v-if="row.needs_execution"
              size="small"
              :disabled="isExecuting(row)"
              @click="confirmMarkDone(row.zone_name)"
            >
              标记完成
            </el-button>
            <el-button
              v-if="row.needs_execution"
              type="success"
              size="small"
              :disabled="isExecuting(row)"
              @click="confirmMarkAllDone(row.zone_name)"
            >
              全部完成
            </el-button>
            <el-button
              size="small"
              @click="openHistory(row.zone_name)"
            >
              历史
            </el-button>
            <el-button
              size="small"
              :disabled="isExecuting(row)"
              @click="openEditDialog(row as TaskItem)"
            >
              编辑
            </el-button>
            <el-button
              type="danger"
              size="small"
              :disabled="isExecuting(row)"
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
          <el-form-item v-if="editingZone" label="已完成天数">
            <el-input-number v-model="taskForm.days_done" :min="0" :max="999" />
            <el-text size="small" type="info" style="margin-left:8px">
              仅编辑时可见；新建一律从 0 天开始
            </el-text>
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

      <!-- 桌面：分区历史只服务**当前绑定账号**。没有多实例操作台，也就不提供
           "按其他账号筛选"——远端聚合与账号目录读取都不在桌面产品范围里。 -->
      <ZoneHistoryDialog
        v-model="historyVisible"
        :zone-name="historyZone"
        :scope="historyScope"
      />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
/**
 * TaskManager（桌面移植阶段 2）。
 *
 * 桌面边界：**没有**多实例操作台，因此不迁入
 *  - `/api/instances/*` 的聚合与转交（勾选同步创建、账号目录、跨实例历史筛选）；
 *  - `utils/syncCreate` 的远端批次执行器。
 *
 * 保留并要求做实的是本地那一份完整行为：
 *  - 写请求携带**发起时冻结**的 `X-Account-UID`；
 *  - 创建走"一致预检 → 冻结预检 → 唯一的替换确认框 → 一份不可变命令（含客户端
 *    令牌）"；
 *  - 回包丢失时**持有**原 UID + 原令牌 + 原冻结载荷，只提供"核对 / 重试"两个
 *    动作；`found=false` 只表示尚未查到，不倒推成创建失败；
 *  - **待核对项跨页面刷新仍然存在**：按发起时的账号落在会话存储里；换号**不清除**
 *    原命令，只是停用当前账号的核对/重发并如实说明——**绝不**在新账号下重放，
 *    也绝不因换号销毁回到原账号后的核对依据（R1）；
 *  - 分区历史只查请求所属账号的库（不联网聚合、不接受任意 UID 参数）。
 */
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useTaskStore } from '@/stores/tasks'
import type { FrozenWriteScope } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'
import { useAuthStore } from '@/stores/auth'
import { useSettingsStore } from '@/stores/settings'
import { operationEvents } from '@/stores/operationEvents'
import Sidebar from '@/components/Sidebar.vue'
import ZoneHistoryDialog from '@/components/ZoneHistoryDialog.vue'
import type {
  TaskItem, TaskCreate, TaskUpdate, CreationPreview, FrozenCreationPreview,
  CreateTaskResponse,
} from '@/types/api'
import {
  resolveExecutingTaskId,
  resolveExecutingLabel,
  resolveExecutingTagType,
  asExecutingLike,
} from '@/utils/executingTask'
import { runSpecifiedTaskStart } from '@/utils/startTaskFlow'
import type { FrozenStartPlan } from '@/utils/startTaskFlow'

const router = useRouter()
const taskStore = useTaskStore()
const liveStore = useLiveStore()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()

/** 表格行的最小身份：模板里 Element Plus 给的 row 类型不保证是完整 TaskItem，
 *  而这里只用到稳定 id —— 按最小契约取用，不做类型断言。"
 */
type TaskIdentityRow = { id?: number }

/**
 * 正在执行的任务与阶段标签：判定**只在** utils/executingTask 里实现一份，
 * 页面不另判一份（两边判据分叉就必然出现"这边标着在播、那边按钮能点"）。
 */
const executingTaskId = computed<number | null>(
  () => resolveExecutingTaskId(asExecutingLike(liveStore.projection)))

/** 该行是否正在被执行（按 id，同名不同 id 不误标）。 */
function isExecuting(row: TaskIdentityRow): boolean {
  return executingTaskId.value !== null && row.id === executingTaskId.value
}

/** 正在执行这一阶段的标签；未在执行时返回空串。 */
function executingLabel(row: TaskIdentityRow): string {
  return resolveExecutingLabel(asExecutingLike(liveStore.projection),
                               Boolean(liveStore.statusStale),
                               typeof row.id === 'number' ? row.id : null)
}

function executingTagType(row: TaskIdentityRow): 'warning' | 'danger' | 'info' {
  return resolveExecutingTagType(executingLabel(row))
}

// 本地操作意图统一走 operationEvents（唯一来源/命名空间；不再有第二条写
// localStorage 的路径）。
function addEvent(tag: string, type: 'success' | 'danger' | 'warning' | 'info', message: string) {
  operationEvents.addLocal(tag, type, message)
}

/**
 * 推流方式只由**设置页**决定（manual→obs，其余→ffmpeg）：任务页不再另选。
 */
const pushSource = computed<'ffmpeg' | 'obs'>(
  () => (settingsStore.settings.stream_mode === 'manual' ? 'obs' : 'ffmpeg'))
/** 能不能开新场：由投影事实决定，不由页面自己反推。 */
const canStartNew = computed(
  () => liveStore.statusLoaded && !liveStore.resumable && !liveStore.running)
const startBlockedLabel = computed(() => {
  if (!liveStore.statusLoaded) return '读取中'
  if (liveStore.running) return '运行中'
  if (liveStore.resumable) return '有未完成场次'
  return '立即开播'
})

const refreshingAreas = ref(false)
const exporting = ref(false)
const submitting = ref(false)

/**
 * 本页是否仍在（`onUnmounted` 落闸）。
 *
 * 页面一旦离开，本页发起的操作结果不再写进任何视图、不弹提示。默认 `true`
 * （一个新实例就是活的），由卸载钩子翻成 `false`。
 */
const pageAlive = ref(true)

// 版本驱动的列表刷新订阅。
//
// 任务页原先只在挂载/用户操作后刷新：用户一直停在这一页时，后台
// 「任务完成 → 重算优先度 → 重排」不会反映到界面上。这里只轮询轻量统计，
// revision 前进才重取完整列表（不把整个列表改成高频轮询）。
let _revisionTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  // 直接进 /tasks（从未访问控制台）也要立刻知道谁在执行：走 store 的单飞
  // 状态读，不新增常驻轮询；迟到的响应由 store 的序号/代际门控兜住。
  void liveStore.pollStatus()
  // 推流方式来自设置页：带上当前值。
  void settingsStore.fetchSettings()
  // 刷新之前那一次"结果未知"的创建：按发起时的账号恢复；换号后丢弃。
  restorePendingCreate()
  await taskStore.fetchTasks()
  _revisionTimer = setInterval(() => { void taskStore.refreshTasksIfStale() }, 5000)
})

onUnmounted(() => {
  pageAlive.value = false
  if (_revisionTimer) { clearInterval(_revisionTimer); _revisionTimer = null }
})

// 对话框状态
const showCreateDialog = ref(false)
const editingZone = ref<string | null>(null)
//: 打开编辑对话框时**冻结**的那条记录的稳定身份。名字会在改名/删除重建后指向
//: 另一条记录，写请求必须按 id 定位（后端也按 id 校验）。
const editingTaskId = ref<number | undefined>(undefined)
//: 打开编辑对话框时**冻结**的账号上下文。弹框期间换号时，这次编辑仍然只属于打开
//: 它的那个账号：请求带上这份 UID，服务端据此刻的绑定决定受理或明确拒绝——
//: 绝不用"最新账号"把它补成一次有效操作。
const editingScope = ref<FrozenWriteScope | undefined>(undefined)

// ==================== 创建结果未知的待核对项 ====================

/**
 * 待核对项（原 UID + 原令牌 + 原冻结载荷），**按操作逐项保存**。
 *
 * 每一项已发出、结果可能未知的创建都保留**自己的**核对依据（U1/U2）：
 * 同一令牌（同一操作的重试/再持有）原位更新，不同令牌（另一项创建）各自
 * 追加——谁也不覆盖谁、谁也不销掉谁。只有**这一项自己的**确定终态才消费它
 * （按令牌作用域）：查到自己的持久回执（确定已提交）、或无前序未知的首次
 * 明确拒绝（结果确定为"没受理"）。下列情况一律**继续持有**：
 *  - 回执 `found=false`（只表示尚未查到）；
 *  - 查询本身暂时失败；
 *  - 后一次重试被拒（那只说明"这一次没受理"，R2）；
 *  - 回包落点发现页面已卸载或账号已换（R4/R1：死页面不清账，换号不清账）。
 */
interface PendingCreateEntry {
  zoneName: string
  data: TaskCreate
  preview: FrozenCreationPreview
  token: string
  scope: FrozenWriteScope
}
const pendingList = ref<PendingCreateEntry[]>([])
/** 单项视图：集合里最早的一项（旧的单项语义；模板逐项展示全部）。 */
const pendingCreate = computed<PendingCreateEntry | null>(
  () => pendingList.value[0] ?? null)

/** 某一项的核对/重发是否正在进行（按令牌逐项标记，互不卡住）。 */
const pendingBusyTokens = ref<string[]>([])
function isPendingBusy(token: string): boolean {
  return pendingBusyTokens.value.includes(token)
}

const PENDING_CREATE_KEY = 'pending_task_create_v1'
const PENDING_LIST_KEY = 'pending_task_create_v2'

/**
 * 把当前集合写进会话存储。
 *
 * - 空集合：两个键都不存在；
 * - 恰一条：`v2` 存数组，`v1` **镜像**这一条的单对象旧形态——旧版本写入的
 *   `v1` 记录、以及只认 `v1` 的旧读取方都继续可读（读写双向兼容）；
 * - 两条及以上：只存 `v2` 数组，清掉 `v1`（它只能表达一条，留着会误导）。
 */
function persistPendingList(): void {
  try {
    const items = pendingList.value
    if (items.length === 0) {
      sessionStorage.removeItem(PENDING_LIST_KEY)
      sessionStorage.removeItem(PENDING_CREATE_KEY)
    } else {
      sessionStorage.setItem(PENDING_LIST_KEY, JSON.stringify(items))
      if (items.length === 1) {
        sessionStorage.setItem(PENDING_CREATE_KEY, JSON.stringify(items[0]))
      } else {
        sessionStorage.removeItem(PENDING_CREATE_KEY)
      }
    }
  } catch {
    /* 会话存储不可用时只在内存里保留，不影响本次会话的核对能力 */
  }
}

/**
 * 持有/刷新一条待核对命令。
 *
 * 集合语义：这条记录属于**一个具体操作**（按原令牌核对）——同令牌原位更新
 * （同一操作的重试/再持有），新令牌追加（另一项创建有它自己的核对依据，
 * U1/U2）。`null` 清空整个集合（保留给"全部重来"的显式语义；常规消费一律
 * 走 `releasePendingCreate(token)` 的按项作用域）。
 */
function holdPendingCreate(pending: PendingCreateEntry | null): void {
  if (pending === null) {
    pendingList.value = []
    persistPendingList()
    return
  }
  const idx = pendingList.value.findIndex(p => p.token === pending.token)
  if (idx >= 0) pendingList.value.splice(idx, 1, pending)
  else pendingList.value.push(pending)
  persistPendingList()
}

/**
 * 只消费**本次操作自己的**待核对命令（按原令牌核对）。
 *
 * 该令牌不在集合里（已被自己的回执命中提前消费，或从未持有）时是 no-op——
 * 迟到的续体不得连带销掉别人的命令（S2）。
 */
function releasePendingCreate(token: string): void {
  const idx = pendingList.value.findIndex(p => p.token === token)
  if (idx < 0) return
  pendingList.value.splice(idx, 1)
  persistPendingList()
}

/** 当前账号 UID（未登录为 null），用于判定待核对项还属不属于本页面。 */
function currentUid(): number | null {
  const uid = authStore.userInfo?.uid
  return typeof uid === 'number' && uid > 0 ? uid : null
}

/**
 * 刷新页面后恢复待核对集合。
 *
 * 先读 `v2`（集合数组），没有再兜 `v1`（旧版本写入的**单条**记录，原样迁移
 * 进集合——旧单项会话记录继续可读）。逐项校验：**损坏的条目**（解析失败/缺
 * 关键字段）是垃圾数据，剔除它不是销掉任何操作的核对依据；账号不符**不清除**
 * （R1）：UID/令牌/载荷是"那次未知创建"仅存的核对依据，换号只能取消当前账号
 * 核对/重发的资格（见 `pendingOwnedByCurrentAccount`），不能推导出旧操作不
 * 存在。切回原账号后，这里恢复的这些条目仍然可核对。
 */
function restorePendingCreate(): void {
  let v2Raw: string | null = null
  let v1Raw: string | null = null
  try {
    v2Raw = sessionStorage.getItem(PENDING_LIST_KEY)
    v1Raw = sessionStorage.getItem(PENDING_CREATE_KEY)
  } catch {
    return
  }
  const parseOne = (raw: string): PendingCreateEntry | null => {
    let candidate: {
      zoneName?: string; data?: TaskCreate; preview?: FrozenCreationPreview
      token?: string; scope?: FrozenWriteScope
    } | null = null
    try { candidate = JSON.parse(raw) } catch { return null }
    if (!candidate || typeof candidate.token !== 'string' || !candidate.token
        || !candidate.data || typeof candidate.zoneName !== 'string') return null
    const scope: FrozenWriteScope = {
      uid: candidate.scope?.uid ?? null,
      businessDate: String(candidate.scope?.businessDate || ''),
    }
    return {
      zoneName: candidate.zoneName,
      data: candidate.data,
      preview: candidate.preview ?? { task_id: null, revision: null,
        business_day: '', existing_is_unfinished: false },
      token: candidate.token,
      scope,
    }
  }
  let list: PendingCreateEntry[] = []
  if (v2Raw) {
    try { sessionStorage.removeItem(PENDING_LIST_KEY) } catch { /* 忽略 */ }
    try {
      const arr = JSON.parse(v2Raw) as unknown
      if (Array.isArray(arr)) {
        list = arr
          .map(item => parseOne(JSON.stringify(item)))
          .filter((p): p is PendingCreateEntry => p !== null)
      }
    } catch { /* v2 整体损坏：按空处理，v1 兜底 */ }
  }
  if (list.length === 0 && v1Raw) {
    try { sessionStorage.removeItem(PENDING_CREATE_KEY) } catch { /* 忽略 */ }
    const one = parseOne(v1Raw)
    if (one) list = [one]
  }
  pendingList.value = list
  // 规范化回写：单条时同时维护 v1 镜像，保持旧形态可读。
  persistPendingList()
}

/**
 * 某条待核对项能否由**当前账号**核对/重发。
 *
 * 原命令带账号归属（UID）时必须与当前账号一致：换号后既不能核对也不能重发，
 * 更不能改用新账号重放——原命令继续保留，切回原账号后恢复可操作（R1）。
 * 发起时尚未取得账号身份（uid=null）的命令只能按当前账号核对（见模板说明）。
 */
function pendingOwnedByCurrentAccount(pending: PendingCreateEntry | null = null): boolean {
  const item = pending ?? pendingCreate.value
  if (!item) return false
  const uid = item.scope?.uid
  if (uid == null) return true
  return uid === currentUid()
}

/** 账号不符时的统一说明（不删除原命令，只取消当前账号的动作资格）。 */
function warnPendingOwnedByOtherAccount(pending: PendingCreateEntry | null = null): void {
  const item = pending ?? pendingCreate.value
  ElMessage.warning(
    `这项待核对属于发起时的账号（UID ${item?.scope?.uid ?? '未取得'}）：`
    + `当前账号不能核对或重发，也不会在新账号下重放；原命令已保留，切回原账号后可继续。`)
}

/** 按分区名找当前列表里那条记录的稳定 id（找不到就返回 undefined）。 */
function taskIdOf(zoneName: string): number | undefined {
  return taskStore.tasks.find(t => t.zone_name === zoneName)?.id
}

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
  editingTaskId.value = undefined
  editingScope.value = undefined
  taskForm.zone_name = ''
  taskStatus.value = 'pending'
  taskHours.value = 2
  taskTodayDone.value = false
  taskForm.total_days = 1
  taskForm.days_done = 0
  deadlineDate.value = null
  taskForm.deadline_raw = ''
}

/** 打开新建窗口。 */
function openCreateDialog() {
  editingZone.value = null
  editingTaskId.value = undefined
  editingScope.value = undefined
  showCreateDialog.value = true
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
  editingTaskId.value = row.id
  // 账号上下文与记录身份**一起**在弹框之前冻结：此后列表刷新、别的标签页换号
  // 都不改这一份（服务端会用请求里的 UID 与它此刻的绑定核对）。
  editingScope.value = taskStore.freezeWriteScope()
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

  // **冻结这次操作的账号上下文**：必须早于下面任何确认 UI（分区不存在确认框、
  // 替换确认框）。弹框期间别的标签页换号、后台刷新列表，都不改这一份——服务端
  // 会用它核验"这次操作仍属于当前账号"，不符即明确拒绝。
  const scope = taskStore.freezeWriteScope()

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
    if (editingZone.value) {
      // **编辑仍修改原身份**：不产生新 id、不换新进度语义。
      const updateData: TaskUpdate = {
        category,
        total_days: taskForm.total_days,
        days_done: taskForm.days_done,
        deadline_raw: deadlineDate.value,
        today_done: taskTodayDone.value ? 1 : 0,
      }
      const success = await taskStore.updateTask(editingZone.value, updateData,
                                                  editingTaskId.value,
                                                  editingScope.value ?? scope)
      if (success) {
        ElMessage.success('任务已更新')
        showCreateDialog.value = false
      } else {
        ElMessage.error('更新失败')
      }
      return
    }
    // 新建：一律产生新身份、进度 0。桌面只有本实例这一个目标，走本地完整流程
    // （一次预检、一个确认、一份命令、持久回执）。
    const data: TaskCreate = {
      zone_name: taskForm.zone_name.trim(),
      category,
      total_days: taskForm.total_days,
      deadline_raw: deadlineDate.value,
    }
    const created = await runCreateFlow(data, scope)
    if (created) showCreateDialog.value = false
  } finally {
    submitting.value = false
  }
}

/** 一次操作一个令牌：同一次操作的传输重试与手动重试都复用这**同一个**。 */
function newOperationToken(): string {
  const random = Math.random().toString(36).slice(2)
  return `create-${Date.now().toString(36)}-${random}`
}

/** 服务端"预检已过期"的明确冲突。 */
function isStaleCreate(detail: string): boolean {
  return detail.includes('create_stale_revision')
    || detail.includes('create_stale_business_day')
    || detail.includes('create_target_changed')
    || detail.includes('create_revision_unavailable')
}

function isResultUnknown(error: unknown): boolean {
  return (error as { resultUnknown?: boolean })?.resultUnknown === true
}

function detailOf(error: unknown): string {
  const e = error as { response?: { data?: { detail?: string } }; message?: string }
  return e?.response?.data?.detail || e?.message || ''
}

/**
 * 冻结并展示**唯一**的替换确认框。
 *
 * `preview` 是在此之前取到的、已冻结的那一份：弹框期间任务列表刷新、后台结算
 * 推进版本都不改它。展示的是该账号、分区、**实际** days_done/total_days 与
 * 截止日期，并说明旧进度保留在历史、新任务从 0 天开始。
 *
 * 与旧的"任务已存在，是否覆盖"弹窗的区别：旧弹窗只说"已存在"，不给当前进度、
 * 不给截止日期，也不说明旧任务去了哪里——用户无法据此判断该不该覆盖。
 */
async function confirmReplaceTask(preview: CreationPreview,
                                  frozen: FrozenCreationPreview): Promise<boolean> {
  const existing = preview.existing_task
  if (!existing) return true
  const deadline = parseDeadlineToPicker(existing.deadline_raw) || '未设置'
  try {
    await ElMessageBox.confirm(
      `分区「${preview.zone_name}」有尚未完成的任务。\n`
      + `当前进度：${existing.days_done} / ${existing.total_days} 天\n`
      + `截止日期：${deadline}\n\n`
      + `确认用本次填写的新任务替换？\n`
      + `旧任务与 ${existing.days_done} / ${existing.total_days} 天进度将保留在历史，`
      + `新任务从 0 天开始。`,
      '该分区有尚未完成的任务',
      {
        confirmButtonText: '覆盖并创建新任务',
        cancelButtonText: '取消',
        type: 'warning',
        distinguishCancelAndClose: true,
      },
    )
    // 确认框展示的事实就是**冻结那一刻**的事实：这里再核一次，保证弹框期间
    // 对象没有被别处改写（弹框文本与提交载荷同源）。
    if (frozen.task_id !== (preview.existing_task?.id ?? null)
        || frozen.revision !== preview.tasks_revision) {
      throw new Error('确认对象在弹框期间被改写')
    }
    return true
  } catch {
    return false
  }
}

/**
 * 新建流程：冻结表单与账号范围 → 取一次一致预检 → 核对回包 UID → 冻结预检 →
 * 必要时弹**唯一**的确认框 → 提交一份不可变命令。
 */
async function runCreateFlow(data: TaskCreate, scope: FrozenWriteScope): Promise<boolean> {
  let preview: CreationPreview
  try {
    preview = await taskStore.fetchCreationPreview(data.zone_name, scope)
  } catch (error: unknown) {
    if (pageAlive.value) ElMessage.error(detailOf(error) || '无法取得该分区的当前状态，请稍后重试')
    return false
  }
  // R4/S3：每个 await 之后都是页面可能已经不在、账号可能已经变化的边界——
  // 死页面不弹旧结果；冻结账号已不是当前账号时，旧意图停止投递（发送前的
  // 共同入口 submitCreation 还会再核一次）。
  if (!pageAlive.value) return false
  if (!taskStore.writeScopeIsCurrent(scope)) {
    ElMessage.warning('登录账号已变化：本次创建未发送，请按当前账号重新操作')
    return false
  }
  // 服务端以真实原因拒绝（例如该分区任务正在执行）：如实展示并结束本次创建。
  if (!preview.creation_allowed) {
    ElMessage.error(preview.reason || '当前无法在该分区创建任务')
    return false
  }
  // **在第一个确认 UI 出现之前**冻结预检：旧 id、分区、版本、业务日、进度与
  // 截止日都取此刻这一份；弹框期间列表怎么刷新都不改这个对象。
  const frozen: FrozenCreationPreview = {
    task_id: preview.existing_task?.id ?? null,
    revision: preview.tasks_revision,
    business_day: preview.business_day,
    existing_is_unfinished: preview.existing_is_unfinished,
  }
  if (frozen.existing_is_unfinished) {
    const confirmed = await confirmReplaceTask(preview, frozen)
    // R4/S3：确认框回来时页面可能已经卸载、账号可能已经变化——不放行旧确认
    // 续体，尚未发出的旧账号写请求在此停止投递。
    if (!pageAlive.value) return false
    if (!taskStore.writeScopeIsCurrent(scope)) {
      ElMessage.warning('登录账号已变化：本次创建未发送，请按当前账号重新操作')
      return false
    }
    if (!confirmed) {
      ElMessage.info('已取消创建')
      return false
    }
  }
  return submitCreation(data, frozen, newOperationToken(), scope)
}

async function submitCreation(data: TaskCreate, preview: FrozenCreationPreview,
                              token: string, scope: FrozenWriteScope): Promise<boolean> {
  // P2/S3：**共同发送入口**的发送前资格复核——页面存活 + 冻结账号仍是当前账号。
  // 换号后旧账号的意图在此停止投递（POST=0）；后端冻结 UID 防线是第二道，
  // 不是第一道。
  if (!pageAlive.value) return false
  if (!taskStore.writeScopeIsCurrent(scope)) {
    ElMessage.warning('登录账号已变化：本次创建未发送，请按当前账号重新操作')
    return false
  }
  // 这次提交是否在重放**一份已持有的未知命令**（同一个令牌）：是的话，本轮收到的
  // 任何明确拒绝都只说明"这一轮没受理"，不能否定此前那次未知尝试（R2）。
  const wasHeld = pendingList.value.some(p => p.token === token)
  // R3/U1/U2：写请求可能离手**之前**，先把**这一项自己的**原命令（原 UID +
  // 原令牌 + 原冻结载荷）落进会话存储——集合语义下每项各存各的，别的操作
  // 持有什么不影响这一项。页面在请求在途时刷新/崩溃，catch 根本来不及执行
  // ——核对依据必须已经在。只有确定终态（成功，或无前序未知时的明确拒绝）
  // 才消费这一项。
  let attemptLost = false
  holdPendingCreate({ zoneName: data.zone_name, data, preview, token, scope })
  try {
    const res = await taskStore.createNewTask(data, preview, token, scope, {
      // R2：拦截器内部有物理尝试在途丢失时置位——后续收到的拒绝只是那一轮的
      // 结果，不能当作原操作的终态。
      onAttemptLost: () => { attemptLost = true },
    })
    // 回包落点（R4）：页面已卸载或账号已换 → 不在当前上下文弹结果、清账或关框。
    // 请求已发出、不回滚；原命令保持持有（发送前已落），由回到原账号后的
    // "待核对"条收敛。
    if (!pageAlive.value || !taskStore.writeScopeIsCurrent(scope)) return false
    reportCreated(res)
    releasePendingCreate(token)
    return true
  } catch (error: unknown) {
    if (!pageAlive.value || !taskStore.writeScopeIsCurrent(scope)) {
      // R4：死页面/换号不处理这次尝试的结果——原命令保持持有，不弹任何提示。
      return false
    }
    if (isResultUnknown(error) || attemptLost) {
      // 回包丢失（或本轮有物理尝试在途丢失）：先按原 UID + 原令牌查已提交回执；
      // 查不到就是"结果待核对"，**不**重取预检、**不**换令牌、**不**自动重新覆盖。
      const resolved = await lookupReceipt(token, scope)
      if (!pageAlive.value || !taskStore.writeScopeIsCurrent(scope)) return false
      if (resolved) {
        releasePendingCreate(token)
        return true
      }
      // 同一令牌再持有：允许刷新自己的那一项；别的操作的命令不会被覆盖（S1/S2）。
      holdPendingCreate({ zoneName: data.zone_name, data, preview, token, scope })
      // 关掉新建框：这次操作已经离开表单，结果由"待核对"条统一跟进——
      // 否则模态遮罩会把那条提示压在下面，用户根本看不到。
      showCreateDialog.value = false
      ElMessage.warning('本次创建仍未取得确定结果：原命令已保留，请用「核对结果」确认')
      return false
    }
    const detail = detailOf(error)
    if (isStaleCreate(detail)) {
      if (wasHeld) {
        // 这是对**已持有未知命令**的重试被拒（R2）：只说明这一轮没受理，此前那
        // 次尝试的效果仍然未知——原命令继续保留，交给"核对结果"收敛。
        ElMessage.warning(`重试被拒（${detail}）：此前一次尝试的效果仍未知，原命令继续保留`)
        await taskStore.fetchTasks()
        return false
      }
      // 原请求自己被明确拒绝（预检前置条件已经变了），因此可以销掉**这一次
      // 操作**的待核对；与"后一次重试被拒"不是一回事（G1 钉住这一点）。
      releasePendingCreate(token)
      await taskStore.fetchTasks()
      ElMessage.warning(detail)
      return false
    }
    if (wasHeld) {
      // R2：重试的明确拒绝同样只覆盖这一轮。
      ElMessage.error(`重试被拒（${detail || '原因未说明'}）：此前一次尝试的效果仍未知，原命令继续保留`)
      return false
    }
    // 无前序未知的明确拒绝（响应已收到、结果确定）：发送前持有的这一项现在消费掉
    // （R3 的对称面——不留下一条已知被拒、永远核对不到的僵尸命令）。
    releasePendingCreate(token)
    ElMessage.error(detail || '创建失败（数据未变更）')
    return false
  }
}

function reportCreated(res: CreateTaskResponse) {
  if (res.replaced_task_id !== null && res.replaced_task_id !== undefined) {
    ElMessage.success(`已建立新任务（${res.task_id}），旧任务 ${res.replaced_task_id} 保留在历史`)
  } else {
    ElMessage.success(`任务已创建（${res.task_id}）`)
  }
  if (res.wakeup && res.wakeup.delivered === false) {
    // 数据库已经写进去了，只是唤醒没送出去——两件事分开说。
    ElMessage.warning('任务已创建，但本次未能通知运行中的调度（唤醒未送达）')
  }
}

/**
 * 只读核对同一令牌的已提交回执；命中即视为这次操作已经成功。
 *
 * 持久回执证明的是**数据库已提交**，它**不包含**唤醒投递结果：这里只能如实说
 * "创建已提交、唤醒结果未取得"，不能硬填 `delivered: true`。
 *
 * 核对失败（网络/被拒/换号）一律返回 false：**不**据此抹掉待核对项。
 */
async function lookupReceipt(token: string, scope: FrozenWriteScope): Promise<boolean> {
  try {
    const receipt = await taskStore.lookupCreationReceipt(token, scope)
    // 回包落点（R4）：页面已卸载或账号已换 → 这份回执不在当前上下文消费，
    // 不弹结果、不刷新展示；原命令保持持有，由回到原账号后的待核对条收敛。
    if (!pageAlive.value || !taskStore.writeScopeIsCurrent(scope)) return false
    if (!receipt.found) return false
    // 回执命中即**本次操作**的确定终态：在列表刷新等待之前就消费自己的待核对
    // 命令（按令牌作用域）——迟到的续体（刷新、提示）不再持有销账权，别的
    // 操作持有的命令也不受影响（S2）。
    releasePendingCreate(token)
    const result = receipt.result || {}
    reportReceipt(result)
    await taskStore.fetchTasks({ fresh: true })
    return true
  } catch {
    return false
  }
}

function reportReceipt(result: Record<string, unknown>) {
  const replaced = result.replaced_task_id
  if (replaced !== null && replaced !== undefined) {
    ElMessage.success(`创建已提交（任务 ${result.task_id}），旧任务 ${replaced} 保留在历史`)
  } else {
    ElMessage.success(`创建已提交（任务 ${result.task_id}）`)
  }
  ElMessage.info('这是已提交的持久回执：本次未取得唤醒投递结果')
}

async function checkPending(pending: PendingCreateEntry | null = null) {
  const item = pending ?? pendingCreate.value
  if (!item) return
  if (!pendingOwnedByCurrentAccount(item)) {
    // R1：换号只取消资格，不删除原命令。
    warnPendingOwnedByOtherAccount(item)
    return
  }
  pendingBusyTokens.value = [...pendingBusyTokens.value, item.token]
  try {
    if (await lookupReceipt(item.token, item.scope)) {
      // 兜底（通常已在回执命中时消费）：只销**自己**的令牌，不连带别的操作。
      releasePendingCreate(item.token)
    } else if (pageAlive.value && taskStore.writeScopeIsCurrent(item.scope)) {
      ElMessage.warning('还没有查到这次创建的成功回执：不能据此认为没有执行，原命令继续保留')
    }
  } finally {
    pendingBusyTokens.value = pendingBusyTokens.value.filter(t => t !== item.token)
  }
}

async function retryPending(pending: PendingCreateEntry | null = null) {
  const item = pending ?? pendingCreate.value
  if (!item) return
  if (!pendingOwnedByCurrentAccount(item)) {
    // R1：换号只取消资格，不删除原命令，更不改用新账号重发。
    warnPendingOwnedByOtherAccount(item)
    return
  }
  pendingBusyTokens.value = [...pendingBusyTokens.value, item.token]
  try {
    // 同一令牌、同一份冻结命令：服务端认得出来是同一次操作。
    // 原命令的持有/消费与结果呈现都由 submitCreation 统一负责——包括
    // "后一次被拒不否定前一次未知"（R2）与"发送前先落命令"（R3）。
    await submitCreation(item.data, item.preview, item.token, item.scope)
  } finally {
    pendingBusyTokens.value = pendingBusyTokens.value.filter(t => t !== item.token)
  }
}

// ==================== 分区历史窗口 ====================

const historyVisible = ref(false)
const historyZone = ref('')
const historyScope = ref<FrozenWriteScope | undefined>(undefined)

function openHistory(zoneName: string) {
  // 只读入口：冻结账号范围、不发 start/stop、不改任务与业务日。
  historyScope.value = taskStore.freezeWriteScope()
  historyZone.value = zoneName
  historyVisible.value = true
}

async function confirmDelete(zoneName: string) {
  // **先在确认框之前冻结**这次操作的身份与账号上下文：确认框弹出后列表可能刷新
  // （id 会变），别的标签页也可能换了账号——用户认可的删除对象就是**此刻这一行**，
  // 服务端再用这份 UID 判断它还属不属于当前账号（不符即拒绝，不会"顺手删掉同名
  // 的新账号任务"）。
  const scope = taskStore.freezeWriteScope()
  const taskId = taskIdOf(zoneName)
  try {
    await ElMessageBox.confirm(
      `确定要删除任务「${zoneName}」吗？此操作不可撤销。`,
      '确认删除',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  const success = await taskStore.deleteTask(zoneName, taskId, scope)
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
  // 账号上下文在**第一个确认 UI 之前**冻结：两次确认框（模式、全覆盖警告）
  // 期间换号时，这次导入仍然只属于发起它的账号；服务端据请求里的 UID 决定受理
  // 或明确拒绝，绝不把整批导入落到新账号上。
  const scope = taskStore.freezeWriteScope()

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
  const result = await taskStore.importFromExcel(file, false, mode, scope)

  // 分区不存在 → 确认是否跳过
  if (result.needs_confirmation && result.invalid_zones && result.invalid_zones.length > 0) {
    try {
      await ElMessageBox.confirm(
        `${result.message}：\n\n${result.invalid_zones.join('、')}\n\n是否跳过以上分区继续导入？`,
        '分区不存在',
        { confirmButtonText: '继续导入（跳过无效）', cancelButtonText: '取消', type: 'warning' },
      )
      const forceResult = await taskStore.importFromExcel(file, true, mode, scope)
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

async function onLogout() {
  const result = await authStore.logout()
  if (!result.blocked && !result.superseded) {
    router.push({ name: 'Login' })
  }
}

async function reloadTasks() {
  const success = await taskStore.reloadTasks()
  if (success) {
    ElMessage.success('任务已重新加载')
  } else {
    ElMessage.error('加载失败')
  }
}

/**
 * 冻结"用户此刻点中的那一条"的身份与前置条件。
 *
 * 三样必须在**第一次确认 UI 之前**一起取：确认框弹出后列表随时可能刷新，只有 id
 * 代表用户看到的那一行；而版本与业务日保证"确认的那一刻"的状态没有被人改过。
 */
function freezeStartTarget(row: TaskIdentityRow) {
  if (typeof row.id !== 'number') return null
  return {
    taskId: row.id,
    expectedRevision: taskStore.tasksRevision,
    businessDate: taskStore.businessDate,
  }
}

/**
 * 从**这一行**开始任务模式。
 *
 * `plan` 由调用方在**模式选择弹窗之前**构建好（见 `confirmStartLive`）：这里只
 * 负责"确认 → 派发这一份"，不再回读任何 store 值。
 */
function startTaskFromRow(plan: FrozenStartPlan) {
  return runSpecifiedTaskStart({
    plan: () => plan,
    start: (operation) => liveStore.dispatch(operation).then((result) => ({
      superseded: result.superseded,
      notice: result.notice,
      status: result.outcome.kind === 'receipt'
        ? String(result.outcome.receipt.status) : 'unknown',
    })),
    notify: (kind, message) => {
      if (kind === 'success') ElMessage.success(message)
      else if (kind === 'warning') ElMessage.warning(message)
      else if (kind === 'error') ElMessage.error(message)
      else ElMessage.info(message)
    },
    confirmAbandon: async (info) => {
      try {
        await ElMessageBox.confirm(
          `旧场「${info.zone}」还有本次未结算的已播进度（执行日 ${info.day}）。\n\n`
          + `确认后将在**一项**请求里放弃旧场并从这一条任务开始新的一场；`
          + `此前已完成的天数不受影响。`,
          '放弃本次未结算的已播进度',
          {
            confirmButtonText: '放弃并改播',
            cancelButtonText: '取消',
            type: 'warning',
            distinguishCancelAndClose: true,
            confirmButtonClass: 'el-button--danger',
          },
        )
        return true
      } catch {
        return false
      }
    },
  })
}


async function confirmStartLive(
  row: TaskIdentityRow & { zone_name?: string },
) {
  // ---- 冻结整份操作：**必须在第一个确认 UI（模式选择弹窗）之前** ----
  // 目标身份、原场三件套、推流来源、账号代次与客户端令牌一次性取好；此后弹框
  // 期间后台换场/改版本都拼不进这份已经被用户确认过的载荷。
  const target = freezeStartTarget(row)
  if (!target) {
    ElMessage.error('该任务缺少稳定身份（id）：请刷新任务列表后重试')
    return
  }
  const source = pushSource.value
  const abandonRef = liveStore.freezeAbandonRef(liveStore.projection)
  const startBody = {
    mode: 'task', task_id: target.taskId,
    expected_revision: target.expectedRevision,
    business_date: target.businessDate, push_source: source,
  } as Record<string, unknown>
  const plan: FrozenStartPlan = {
    operation: abandonRef
      ? liveStore.buildRestartAfterAbandonOp(startBody, abandonRef)
      : liveStore.buildNamedTaskOp(target, source),
    abandon: abandonRef,
    target,
    pushSource: source,
  }
  // 手动模式**同样在弹窗之前**冻结：分区按值取自这一行，来源、账号代次与令牌都
  // 取此刻的值。模式选择只决定"派发哪一份已经冻结的操作"，不再回头读
  // live/settings/row。
  const manualOperation = liveStore.buildSingleOp(
    { zoneName: row.zone_name || '', durationSeconds: 0 }, source)

  // 模式选择：两个明确按钮；关闭弹窗只是取消。
  let mode: 'task' | 'manual' | null = null
  try {
    await ElMessageBox.confirm(
      `在分区「${row.zone_name}」开播：\n\n`
      + `【从此任务开始】直接从这一条任务开始，之后照常自动轮转\n`
      + `【手动模式】手动指定时长，单次开播（不记为任务执行）`,
      '选择开播模式',
      {
        confirmButtonText: '手动模式',
        cancelButtonText: '从此任务开始',
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
  if (mode === 'manual') {
    // 单次模式没有第二个确认框：派发的就是弹窗那一刻冻结的那一份。
    const result = await liveStore.dispatch(manualOperation)
    const notice = result.notice
    if (notice) {
      if (notice.level === 'error') ElMessage.error(notice.message)
      else if (notice.level === 'warning') ElMessage.warning(notice.message)
      else if (notice.level === 'success') ElMessage.success(notice.message)
      else ElMessage.info(notice.message)
    }
    return
  }
  await startTaskFromRow(plan)
}

async function confirmMarkDone(zoneName: string) {
  const scope = taskStore.freezeWriteScope()
  const taskId = taskIdOf(zoneName)
  try {
    await ElMessageBox.confirm(`确定将「${zoneName}」标记为今日完成吗？`, '确认标记', {
      confirmButtonText: '确认标记', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  await markDone(zoneName, taskId, scope)
}

async function confirmMarkAllDone(zoneName: string) {
  const scope = taskStore.freezeWriteScope()
  const taskId = taskIdOf(zoneName)
  try {
    await ElMessageBox.confirm(
      `确定将「${zoneName}」标记为全部完成吗？此操作不可撤销。`,
      '确认全部完成',
      { confirmButtonText: '确认全部完成', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  const success = await taskStore.markTaskAllDone(zoneName, taskId, scope)
  if (success) {
    ElMessage.success(`「${zoneName}」已标记为全部完成`)
  } else {
    ElMessage.error('标记全部完成失败')
  }
}

async function markDone(zoneName: string, taskId?: number,
                        scope?: FrozenWriteScope) {
  const success = await taskStore.markTaskDone(zoneName, taskId, undefined, scope)
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

.pending-create {
  margin-bottom: 12px;
}

.pending-create-body {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.pending-create-actions {
  display: inline-flex;
  gap: 8px;
}

.zone-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 多实例同步创建（`/api/instances/*`）不在桌面产品范围里，相关样式随功能一并
   不迁入；`.muted` 仍被"待核对"提示复用。 */
.muted {
  color: #909399;
  font-size: 12px;
}

:deep(.el-progress-bar__innerText) { color: #303133 !important; font-weight: bold; }

/* 优先度列紧凑 */
:deep(.el-table__body-wrapper) { overflow-x: auto; }

/* ===== 手机端适配 ===== */
@media (max-width: 768px) {
  .main-content {
    padding: 56px 12px 12px;
    height: auto;
    min-height: 100vh;
  }
  .toolbar {
    flex-wrap: wrap;
    gap: 6px;
  }
  .stats-info {
    width: 100%;
    margin-left: 0;
    margin-top: 4px;
  }
}
</style>
