/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

declare module 'element-plus/dist/locale/zh-cn.mjs' {
  const locale: any
  export default locale
}

declare module 'element-plus/dist/locale/zh-cn.mjs' {
  const locale: any
  export default locale
}

// 为 qrcode 库添加类型声明
declare module 'qrcode' {
  export function toDataURL(text: string, options?: {
    width?: number
    margin?: number
    color?: { dark?: string; light?: string }
  }): Promise<string>
}
