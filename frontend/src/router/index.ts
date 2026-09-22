import { createRouter, createWebHashHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { boot } from '@/boot'

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

  const result = await boot.ensure()
  if (!result.ok || !boot.isCurrent(result)) {
    // App 的根层启动页会显示可恢复状态，并在下一代完成后恢复原导航。
    boot.pendingPath = to.fullPath
    next(false)
    return
  }

  if (to.meta.requiresAuth && !result.loggedIn) {
    next({ name: 'Login' })
    return
  }

  next()
})

export default router
