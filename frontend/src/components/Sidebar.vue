<template>
  <!-- 桌面端：侧边栏 -->
  <el-aside v-if="!isMobile" width="150px" class="sidebar">
    <div class="sidebar-header">
      <el-icon :size="22" color="#409eff"><VideoCamera /></el-icon>
      <span class="title">直播控制</span>
    </div>

    <el-menu
      :default-active="activePath"
      router
      class="sidebar-menu"
      background-color="#fafbfc"
      text-color="#606266"
      active-text-color="#409eff"
    >
      <el-menu-item
        v-for="item in menuItems"
        :key="item.path"
        :index="item.path"
      >
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </el-menu-item>
    </el-menu>

    <div class="sidebar-footer">
      <el-dropdown trigger="click" @command="onCommand">
        <span class="user-info">
          <el-avatar :size="28" :src="userAvatar" />
          <span class="username">{{ displayName }}</span>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="logout">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </el-aside>

  <!-- 手机端：顶部标签栏 -->
  <div v-else class="mobile-topbar">
    <el-tabs
      :model-value="activePath"
      class="mobile-tabs"
      @tab-change="onTabChange"
    >
      <el-tab-pane
        v-for="item in menuItems"
        :key="item.path"
        :label="item.label"
        :name="item.path"
      >
        <template #label>
          <el-icon :size="16"><component :is="item.icon" /></el-icon>
          <span class="tab-label">{{ item.label }}</span>
        </template>
      </el-tab-pane>
    </el-tabs>
    <el-dropdown trigger="click" @command="onCommand" class="mobile-user">
      <el-avatar :size="28" :src="userAvatar" />
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="logout">退出登录</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<script setup lang="ts">
/**
 * Sidebar — 通用侧边栏组件
 *
 * Props:
 *   userName   登录用户名（空则显示"未登录"）
 *   userAvatar 用户头像 URL
 *   activePath 当前激活的菜单路径
 *   menuItems  菜单项列表
 *
 * Emits:
 *   logout  用户点击退出登录
 */
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// ==================== Types ====================
export interface MenuItem {
  path: string
  icon: string
  label: string
}

// ==================== Props & Emits ====================
const props = withDefaults(
  defineProps<{
    userName?: string
    userAvatar?: string
    activePath?: string
    menuItems?: MenuItem[]
  }>(),
  {
    userName: '',
    userAvatar: '',
    activePath: '',
    menuItems: () => [
      { path: '/', icon: 'Monitor', label: '控制台' },
      { path: '/tasks', icon: 'List', label: '任务管理' },
      { path: '/settings', icon: 'Setting', label: '设置' },
    ],
  },
)

const emit = defineEmits<{
  logout: []
}>()

// ==================== Computed ====================
const route = useRoute()
const router = useRouter()
const displayName = computed(() => props.userName || '未登录')
const activePath = computed(() => props.activePath || route.path)

// ==================== 响应式断点 ====================
const isMobile = ref(false)
let mql: MediaQueryList | null = null

function onMqlChange(e: MediaQueryListEvent) {
  isMobile.value = e.matches
}

onMounted(() => {
  mql = window.matchMedia('(max-width: 768px)')
  isMobile.value = mql.matches
  mql.addEventListener('change', onMqlChange)
})

onUnmounted(() => {
  mql?.removeEventListener('change', onMqlChange)
})

// ==================== Methods ====================
// el-tabs 的 tab-change 参数类型是 TabPaneName（string | number），
// 这里收窄为两者并转字符串，避免类型不匹配。
function onTabChange(name: string | number) {
  router.push(String(name))
}

function onCommand(command: string) {
  if (command === 'logout') {
    emit('logout')
  }
}
</script>

<style scoped>
/* ===== 桌面端侧边栏 ===== */
.sidebar {
  background: #ecf5ff;
  color: #303133;
  display: flex;
  flex-direction: column;
  height: 100vh;
  border-right: 1px solid #e8e8e8;
}

.sidebar-header {
  padding: 16px 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid #e8e8e8;
}

.sidebar-header .title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.sidebar-menu {
  border-right: none;
  flex: 1;
}

.sidebar-footer {
  padding: 12px 14px;
  border-top: 1px solid #e8e8e8;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: #606266;
}

.username {
  font-size: 12px;
}

/* ===== 手机端顶部标签栏 ===== */
.mobile-topbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  height: 52px;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  display: flex;
  align-items: center;
  padding: 0 12px;
}

.mobile-tabs {
  flex: 1;
  min-width: 0;
}

.mobile-tabs :deep(.el-tabs__header) {
  margin: 0;
  height: 52px;
}

.mobile-tabs :deep(.el-tabs__nav) {
  display: flex;
  width: 100%;
}

.mobile-tabs :deep(.el-tabs__nav-wrap::after) {
  display: none;
}

.mobile-tabs :deep(.el-tabs__item) {
  flex: 1;
  min-width: 0;
  height: 52px;
  line-height: 52px;
  padding: 0 3px;
  font-size: 13px;
  white-space: nowrap;
}

.mobile-tabs :deep(.el-tabs__active-bar) {
  height: 3px;
}

.tab-label {
  margin-left: 6px;
  white-space: nowrap;
}

.mobile-user {
  flex-shrink: 0;
  cursor: pointer;
  margin-left: 8px;
}
</style>
