<template>
  <div class="stream-configurator">
    <el-form label-width="100px" label-position="right">
      <!-- 分区选择（带模糊搜索） -->
      <el-form-item label="直播分区">
        <div style="display:flex;gap:6px;width:100%">
          <el-select
            v-model="selectedZone"
            filterable
            remote
            reserve-keyword
            allow-create
            default-first-option
            :remote-method="searchAreas"
            :loading="searchingAreas"
            placeholder="输入分区名搜索"
            :disabled="disabled"
            clearable
            style="flex:1;min-width:200px"
          >
            <el-option
              v-for="item in areaOptions"
              :key="item.id"
              :label="item.name"
              :value="item.name"
            >
              <span>{{ item.name }}</span>
              <span v-if="item.parent_name" class="option-parent">
                — {{ item.parent_name }}
              </span>
            </el-option>
          </el-select>
          <el-button
            :disabled="!selectedZone || disabled"
            @click="copyZoneName"
            size="default"
          >
            <el-icon><CopyDocument /></el-icon>
          </el-button>
        </div>
      </el-form-item>

      <!-- 直播时长 -->
      <el-form-item label="直播时长">
        <el-input-number
          v-model="durationMinutes"
          :min="0"
          :max="1440"
          :step="10"
          :disabled="disabled"
        />
        <span class="unit-label">{{ durationMinutes === 0 ? '不限时' : '分钟' }}</span>
        <el-text size="small" type="info" style="margin-left:8px">（0 = 不限时）</el-text>
      </el-form-item>

      <!-- 操作按钮 -->
      <el-form-item>
        <el-button
          type="primary"
          :disabled="!canStart || disabled"
          :loading="isStarting"
          @click="handleStart"
        >
          <el-icon><VideoPlay /></el-icon>
          开始直播
        </el-button>
        <el-button
          v-if="showStop"
          type="danger"
          :loading="isStopping"
          :disabled="disabled"
          @click="handleStop"
        >
          <el-icon><VideoPause /></el-icon>
          停止直播
        </el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<script setup lang="ts">
/**
 * StreamConfigurator — 可复用的直播推流配置组件
 *
 * Props:
 *   showStop       是否显示"停止直播"按钮（直播中状态）
 *   disabled       是否禁用所有操作
 *   defaultDurationMinutes 默认时长（分钟），默认 120
 *
 * Emits:
 *   start  点击开始直播，payload: { zoneName: string, durationSeconds: number }
 *   stop   点击停止直播
 */
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRequest } from '@/api/request'
import type { AreaSearchResult, AreaSearchResponse } from '@/types/api'

// ==================== Props & Emits ====================
const props = withDefaults(
  defineProps<{
    showStop?: boolean
    disabled?: boolean
    isStarting?: boolean
    isStopping?: boolean
    defaultDurationMinutes?: number
    defaultZone?: string
  }>(),
  {
    showStop: false,
    disabled: false,
    isStarting: false,
    isStopping: false,
    defaultDurationMinutes: 120,
    defaultZone: '',
  },
)

const emit = defineEmits<{
  start: [payload: { zoneName: string; durationSeconds: number }]
  stop: []
}>()

// ==================== State ====================
const request = useRequest()
const selectedZone = ref(props.defaultZone)
const durationMinutes = ref(props.defaultDurationMinutes)
const areaOptions = ref<AreaSearchResult[]>([])
const searchingAreas = ref(false)

// 外部修改默认值时同步
watch(() => props.defaultZone, (v) => { if (v) selectedZone.value = v }, { immediate: true })
watch(() => props.defaultDurationMinutes, (v) => { if (v) durationMinutes.value = v }, { immediate: true })

// ==================== Computed ====================
const canStart = computed(() => selectedZone.value.trim().length > 0)

// ==================== Methods ====================
async function searchAreas(keyword: string) {
  if (!keyword || keyword.trim().length === 0) {
    areaOptions.value = []
    return
  }
  searchingAreas.value = true
  try {
    const res = await request.get<AreaSearchResponse>('/api/areas/search', {
      keyword,
    } as Record<string, unknown>)
    areaOptions.value = res.results || []
  } catch {
    areaOptions.value = []
  } finally {
    searchingAreas.value = false
  }
}

function handleStart() {
  if (!canStart.value) return
  emit('start', {
    zoneName: selectedZone.value,
    durationSeconds: durationMinutes.value * 60,
  })
}

function handleStop() {
  emit('stop')
}

function copyZoneName() {
  if (!selectedZone.value) return
  navigator.clipboard.writeText(selectedZone.value).then(() => {
    ElMessage.success(`已复制「${selectedZone.value}」`)
  }).catch(() => {
    ElMessage.warning('复制失败，请手动复制')
  })
}

// 组件挂载时预加载分区列表（可选，展示默认推荐分区）
onMounted(async () => {
  try {
    await request.get<AreaSearchResponse>('/api/areas/search', {
      keyword: '',
    } as Record<string, unknown>)
  } catch {
    // 静默失败
  }
})
</script>

<style scoped>
.stream-configurator {
  padding: 8px 0;
}
.unit-label {
  margin-left: 8px;
  color: #909399;
  font-size: 13px;
}
.option-parent {
  color: #c0c4cc;
  font-size: 12px;
  margin-left: 4px;
}
</style>
