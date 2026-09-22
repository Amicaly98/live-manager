import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'
import { boot } from './boot'

import './style.css'

const app = createApp(App)

// 注册所有图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.config.errorHandler = (error, _instance, info) => {
  console.error('[app] 初始化/渲染异常:', info, error)
  if (boot.phase === 'connecting') {
    boot.fail('init', String((error as Error)?.message || error))
  }
}

globalThis.addEventListener?.('vite:preloadError', (event) => {
  const payload = (event as VitePreloadErrorEvent)?.payload
  boot.fail('resource', String(payload?.message || 'preload failed'))
})

app.mount('#app')
