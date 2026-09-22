/**
 * alias-register.mjs - 注册 `@/` 别名解析 hook（在应用模块加载之前执行）。
 */
import { register } from 'node:module'
// Desktop request.ts intentionally branches on window.electronAPI to keep
// Electron file:// requests on an absolute localhost URL. Node store tests
// model the browser side of that boundary with an empty window object.
globalThis.window = globalThis.window || {}
register(new URL('./alias-loader.mjs', import.meta.url))
