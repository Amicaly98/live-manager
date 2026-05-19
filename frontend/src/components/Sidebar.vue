<template>
  <el-aside width="150px" class="sidebar">
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
import { computed } from 'vue'
import { useRoute } from 'vue-router'

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
const displayName = computed(() => props.userName || '未登录')
const activePath = computed(() => props.activePath || route.path)

// ==================== Methods ====================
function onCommand(command: string) {
  if (command === 'logout') {
    emit('logout')
  }
}
</script>

<style scoped>
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
</style>