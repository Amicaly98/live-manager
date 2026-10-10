<template>
  <el-dialog
    :model-value="modelValue"
    :title="`分区「${zoneName}」的历史`"
    width="860px"
    top="6vh"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
    @closed="onClosed"
  >
    <div v-loading="loading" class="zone-history">
      <!-- 错误与"确实没有记录"必须分开显示。 -->
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        show-icon
        class="history-error"
      />
      <el-empty
        v-if="!loading && groups.length === 0 && !error"
        description="暂无记录"
      />

      <div v-for="group in groups" :key="group.task_id" class="history-group">
        <div class="group-head">
          <span class="group-title">
            第 {{ group.task_id }} 号任务
            <el-tag v-if="group.is_current" type="success" size="small">当前</el-tag>
          </span>
          <span class="group-terminal">
            <el-tag :type="terminalTagType(group.terminal.code)" size="small">
              {{ terminalLabel(group.terminal.code) }}
            </el-tag>
            <span v-if="group.terminal.at" class="muted">（{{ group.terminal.at }}）</span>
          </span>
        </div>

        <el-descriptions :column="3" size="small" border class="group-facts">
          <el-descriptions-item label="起始">
            {{ originLabel(group.origin) }}
          </el-descriptions-item>
          <el-descriptions-item label="每天直播时长">
            {{ hoursLabel(group.params.category) }}
          </el-descriptions-item>
          <el-descriptions-item label="需要完成天数">
            {{ valueOrUnrecorded(group.params.total_days) }}
          </el-descriptions-item>
          <el-descriptions-item label="截止日期">
            {{ deadlineLabel(group.params.deadline_raw) }}
          </el-descriptions-item>
          <el-descriptions-item label="当时/最终进度">
            {{ progressLabel(group.params) }}
          </el-descriptions-item>
          <el-descriptions-item label="终结时刻">
            {{ group.terminal.at || '未记录' }}
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="group.terminal.detail" class="group-detail muted">
          {{ group.terminal.detail }}
        </div>

        <!-- 每日完成表：业务日 / 达标确认时间 / 入账时间 / 目标与有效秒数 /
             计入后进度 / 来源。缺失一律写"未记录"，不用入账时间冒充达标时间。
             人工完成、自然完成、参数沿革与终结原因一律复用后端的解释，页面不重推断。 -->
        <el-table :data="group.ledger" size="small" empty-text="没有每日完成记录">
          <el-table-column prop="business_day" label="业务日" width="110" />
          <el-table-column label="达标确认时间" min-width="150">
            <template #default="{ row }">
              {{ row.completed_at || '未记录' }}
            </template>
          </el-table-column>
          <el-table-column prop="committed_at" label="入账时间" min-width="150" />
          <el-table-column label="目标/有效秒数" min-width="130">
            <template #default="{ row }">
              {{ Math.round(row.target_seconds) }} / {{ Math.round(row.effective_seconds) }}
            </template>
          </el-table-column>
          <el-table-column label="计入后进度" width="110" align="center">
            <template #default="{ row }">
              {{ row.days_done_after }}/{{ row.total_days_after }}
            </template>
          </el-table-column>
          <el-table-column label="来源" width="110">
            <template #default="{ row }">{{ ledgerSourceLabel(row.source) }}</template>
          </el-table-column>
        </el-table>

        <el-collapse class="group-events">
          <el-collapse-item
            :title="`参数与进度变更（${group.events.length} 条）`"
            :name="String(group.task_id)"
          >
            <el-table :data="group.events" size="small">
              <el-table-column label="时刻" width="150">
                <template #default="{ row }">{{ row.at }}</template>
              </el-table-column>
              <el-table-column label="事件" width="130">
                <template #default="{ row }">{{ eventLabel(row.event) }}</template>
              </el-table-column>
              <el-table-column label="变化" min-width="200">
                <template #default="{ row }">{{ changeLabel(row) }}</template>
              </el-table-column>
              <el-table-column label="来源" width="110">
                <template #default="{ row }">{{ eventSourceLabel(row.source) }}</template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </div>

      <div v-if="hasMore" class="load-more">
        <el-button :loading="loading" @click="loadMore">
          加载更早的任务（已显示 {{ groups.length }} / {{ total }}）
        </el-button>
      </div>
      <div v-else-if="groups.length > 0" class="load-more muted">
        <span>已显示全部 {{ total }} 次任务</span>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * ZoneHistoryDialog（桌面移植阶段 2）。
 *
 * 桌面边界：只读**当前绑定账号**该分区的历次任务与每日完成记录。
 * 服务端版本里的"按账号筛选"（`/api/instances/accounts` 的账号目录 +
 * `/api/instances/accounts/history` 的跨账号聚合）不迁入——桌面一次只绑定一个
 * 账号，也不装配多实例操作台。历史窗口因此只有"请求所属账号"一种范围。
 *
 * 其余语义与共享链一致：关窗/切分区即作废在途读；回包属于另一个账号时丢弃并
 * 如实说明（不显示成"暂无记录"）。
 */
import { ref, watch } from 'vue'
import { useTaskStore } from '@/stores/tasks'
import type { FrozenWriteScope } from '@/stores/tasks'
import type { TerminalCode, ZoneHistoryGroup } from '@/types/api'

const props = defineProps<{
  modelValue: boolean
  zoneName: string
  /** 打开窗口时冻结的账号范围：窗口存活期间换账号，旧响应不落地。 */
  scope?: FrozenWriteScope
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const taskStore = useTaskStore()

const PAGE_SIZE = 10
const groups = ref<ZoneHistoryGroup[]>([])
const total = ref(0)
const hasMore = ref(false)
const loading = ref(false)
const error = ref('')

function detailOf(err: unknown): string {
  const e = err as { response?: { data?: { detail?: string } }; message?: string }
  return e?.response?.data?.detail || e?.message || ''
}

/**
 * 请求代次：关窗 / 切分区 / 重新打开都前进一步。
 *
 * 只读窗口同样会被迟到响应踩到——旧账号或旧分区的回包写进新窗口会让用户看到
 * 不属于当前选择的历史。
 */
let generation = 0
let _controller: AbortController | null = null

function abortInflight(): void {
  const controller = _controller
  _controller = null
  if (controller) {
    try { controller.abort('zone-history-superseded') } catch { /* 已完成则忽略 */ }
  }
}

const TERMINAL_LABELS: Record<TerminalCode, string> = {
  ongoing: '进行中',
  natural_complete: '自然完成',
  //: 人工标记最后一天：任务走完了，但"走完"的是人工动作，不是平台播满——
  //: 不能显示成"自然完成"。
  manual_complete: '人工完成',
  manual_all_done: '人工全部完成',
  moved_out: '不可完成移出',
  replaced: '被新任务覆盖',
  deleted: '人工删除',
  unknown: '旧记录原因未取得',
}

const EVENT_LABELS: Record<string, string> = {
  baseline: '现状收录',
  created: '创建任务',
  updated: '修改参数',
  manual_progress: '人工调整进度',
  all_done: '人工全部完成',
  moved_out: '不可完成移出',
  replaced: '被新任务覆盖',
  deleted: '删除任务',
}

const SOURCE_LABELS: Record<string, string> = {
  user: '人工',
  import: '导入',
  auto: '自动结算',
  negative_priority: '规则移出',
  migration: '现状收录',
  legacy_observed: '历史迁移',
  mark_done: '人工标记',
}

function terminalLabel(code: TerminalCode): string {
  return TERMINAL_LABELS[code] || TERMINAL_LABELS.unknown
}

function terminalTagType(code: TerminalCode): 'success' | 'info' | 'warning' | 'danger' {
  if (code === 'ongoing') return 'success'
  if (code === 'natural_complete' || code === 'manual_complete'
      || code === 'manual_all_done') return 'success'
  if (code === 'replaced' || code === 'deleted') return 'warning'
  return 'info'
}

function eventLabel(event: string): string {
  return EVENT_LABELS[event] || event
}

function eventSourceLabel(source: string): string {
  return SOURCE_LABELS[source] || (source ? source : '未记录')
}

function ledgerSourceLabel(source: string): string {
  return SOURCE_LABELS[source] || (source ? source : '未记录')
}

function valueOrUnrecorded(value: number | null | undefined): string {
  return value === null || value === undefined ? '未记录' : String(value)
}

function hoursLabel(category: number | null): string {
  if (category === null || category === undefined) return '未记录'
  if (category === 0) return '已完成（不要求时长）'
  return `${category} 小时/天`
}

/** 截止日按原事实显示；无法解析时原样呈现，不猜。 */
function deadlineLabel(raw: string | null): string {
  if (!raw) return '未记录'
  const s = String(raw).trim()
  const m = s.match(/=DATE\((\d+),\s*(\d+),\s*(\d+)\)/i)
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`
  const d = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/)
  if (d) return `${d[1]}-${d[2].padStart(2, '0')}-${d[3].padStart(2, '0')}`
  return s
}

function progressLabel(params: ZoneHistoryGroup['params']): string {
  if (params.days_done === null || params.total_days === null) return '未记录'
  return `${params.days_done} / ${params.total_days} 天`
}

/**
 * 起始信息只沿可靠事实展示：新 created 事件是**创建时间**，旧 baseline 只是
 * **开始收录时间**；两者都没有就写未记录，绝不从累计天数倒推业务起始日。
 */
function originLabel(origin: ZoneHistoryGroup['origin']): string {
  if (origin.code === 'created') return `创建于 ${origin.at || '未记录'}`
  if (origin.code === 'baseline') return `现状收录于 ${origin.at || '未记录'}`
  return '起始时刻未记录'
}

function changeLabel(row: Record<string, unknown>): string {
  const parts: string[] = []
  const pairs: [string, unknown, unknown][] = [
    ['时长', row.category_before, row.category_after],
    ['总天数', row.total_days_before, row.total_days_after],
    ['进度', row.days_done_before, row.days_done_after],
    ['截止日', row.deadline_before, row.deadline_after],
  ]
  for (const [label, before, after] of pairs) {
    if (before === null && after === null) continue
    if (before === after) continue
    parts.push(`${label} ${fmt(before)} → ${fmt(after)}`)
  }
  return parts.length > 0 ? parts.join('；') : '（无参数变化）'
}

function fmt(value: unknown): string {
  return value === null || value === undefined ? '未记录' : String(value)
}

/**
 * 这一份读**是否仍属于当前窗口**：窗口/分区代次未前进，**并且**打开窗口时冻结的
 * 原账号仍是当前账号。
 *
 * 判据与任务页复用同一份 store 事实（`writeScopeIsCurrent`），不在这里另立一套
 * 账号判定。
 */
function readStillBelongs(seq: number): boolean {
  return seq === generation && taskStore.writeScopeIsCurrent(props.scope)
}

async function load(reset: boolean): Promise<void> {
  const zone = props.zoneName
  if (!zone) return
  const seq = ++generation
  abortInflight()
  const controller = new AbortController()
  _controller = controller
  if (reset) {
    groups.value = []
    total.value = 0
    hasMore.value = false
    error.value = ''
  }
  loading.value = true
  try {
    const offset = reset ? 0 : groups.value.length
    const res = await taskStore.fetchZoneHistory(
      zone, offset, PAGE_SIZE, props.scope)
    if (!readStillBelongs(seq)) return dropStaleRead(seq)
    if (res === null) return                 // 回包属于另一个账号：丢弃
    groups.value = reset ? res.groups : [...groups.value, ...res.groups]
    total.value = res.total
    hasMore.value = res.has_more
    error.value = ''
  } catch (err: unknown) {
    if (!readStillBelongs(seq)) return dropStaleRead(seq)
    if ((err as { code?: string })?.code === 'ERR_CANCELED') return
    error.value = detailOf(err) || '历史读取失败，请稍后重试'
  } finally {
    if (seq === generation) {
      loading.value = false
      _controller = null
    }
  }
}

/**
 * 丢弃一份**不属于当前窗口**的历史响应。
 *
 * 代次相符却账号不符，说明用户在窗口开着的时候换了账号——按原账号冻结的窗口
 * 就此作废，如实告诉用户，不显示成"暂无记录"。
 */
function dropStaleRead(seq: number): void {
  if (seq !== generation) return
  error.value = '账号已变化：这个历史窗口按打开时的账号作废，请关闭后重新打开'
}

function loadMore(): void {
  void load(false)
}

function onClosed(): void {
  // 关窗即作废：此后到达的响应不再写进任何窗口。
  generation += 1
  abortInflight()
  groups.value = []
  total.value = 0
  hasMore.value = false
  error.value = ''
  loading.value = false
}

watch(
  () => [props.modelValue, props.zoneName],
  ([visible]) => {
    if (visible) {
      void load(true)
    } else {
      generation += 1
      abortInflight()
    }
  },
)
</script>

<style scoped>
.zone-history {
  max-height: 68vh;
  overflow: auto;
}

.history-error {
  margin-bottom: 12px;
}

.history-group {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  margin-bottom: 14px;
}

.group-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.group-title {
  font-weight: bold;
  color: #303133;
}

.group-facts {
  margin-bottom: 8px;
}

.group-detail {
  margin-bottom: 8px;
  font-size: 12px;
}

.group-events {
  margin-top: 8px;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.load-more {
  text-align: center;
  padding: 8px 0 4px;
}
</style>
