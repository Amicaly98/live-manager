import { createRouter, createWebHashHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { title: '控制台', requiresAuth: true },
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录' },
  },
  {
    path: '/tasks',
    name: 'Tasks',
    component: () => import('@/views/TaskManager.vue'),
    meta: { title: '任务管理', requiresAuth: true },
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings.vue'),
    meta: { title: '设置', requiresAuth: true },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 路由守卫：检查登录状态
router.beforeEach(async (to, _from, next) => {
  document.title = to.meta.title ? `B站直播 - ${to.meta.title}` : '直播控制系统'

  if (to.meta.requiresAuth) {
    const { useAuthStore } = await import('@/stores/auth')
    const authStore = useAuthStore()
    // 未登录时先尝试从缓存恢复
    await authStore.checkLoginStatus()
    if (!authStore.isLoggedIn) {
      next({ name: 'Login' })
      return
    }
  }

  next()
})

export default router
