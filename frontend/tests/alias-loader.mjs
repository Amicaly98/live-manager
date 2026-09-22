/**
 * alias-loader.mjs - 让 node 原生运行时解析 `@/` 别名与无扩展名相对导入。
 *
 * 生产代码（vite 环境）允许 `import x from './operationToken'` 这类
 * 无扩展名相对导入；node --experimental-strip-types 不会自动补 `.ts`。
 * 这里在模块解析层统一处理：`@/x` → `src/x(.ts)`，`./x` 找不到时补 `.ts`。
 * element-plus 只在 UI 提示路径使用，node 环境替换为无副作用的 stub
 * （不影响请求/票据协议的真实性）。
 */
import { existsSync, statSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = pathToFileURL(path.resolve(HERE, '../src') + path.sep).href

const ELEMENT_PLUS_STUB = 'data:text/javascript,' + encodeURIComponent(`
export const ElMessage = { error() {}, warning() {}, success() {}, info() {}, closeAll() {} }
export const ElMessageBox = { confirm: async () => 'confirm' }
export default { ElMessage }
`)

// axios 的类型导出（AxiosInstance 等）在运行时不存在；strip-types 无法
// 从混合值导入中剥离它们。此 shim 原样转出全部运行时导出，并为纯类型
// 名提供占位（仅命名绑定，不参与运行时行为）。
// 注意：shim 内直接引用真实包文件 URL——data URL 模块里再写 'axios'
// 会被本 hook 再次拦截造成自引用。
const AXIOS_ENTRY = pathToFileURL(
  path.resolve(HERE, '../node_modules/axios/index.js')).href
const AXIOS_SHIM = 'data:text/javascript,' + encodeURIComponent(`
import axios from '${AXIOS_ENTRY}'
export default axios
export const AxiosError = axios.AxiosError
export const CanceledError = axios.CanceledError
export const isCancel = axios.isCancel
export const all = axios.all
export const spread = axios.spread
export const AxiosInstance = undefined
export const InternalAxiosRequestConfig = undefined
export const GenericAbortSignal = undefined
export const AxiosRequestConfig = undefined
export const AxiosResponse = undefined
export const AxiosHeaders = axios.AxiosHeaders
`)

function fileExists(url) {
  try {
    return statSync(fileURLToPath(url)).isFile()
  } catch {
    return false
  }
}

export async function resolve(specifier, context, next) {
  if (specifier === 'element-plus') {
    return { shortCircuit: true, url: ELEMENT_PLUS_STUB }
  }
  if (specifier === 'axios') {
    return { shortCircuit: true, url: AXIOS_SHIM }
  }
  if (specifier.startsWith('@/')) {
    const rel = specifier.slice(2)
    for (const candidate of [rel, rel + '.ts', rel + '/index.ts']) {
      const url = new URL(candidate, SRC).href
      if (fileExists(url)) {
        return next(url, context)
      }
    }
  }
  // 相对导入无扩展名：尝试补 .ts（仅当原样解析会失败时）
  if ((specifier.startsWith('./') || specifier.startsWith('../'))
      && context.parentURL && !/\.(mjs|cjs|js|json|ts)$/.test(specifier)) {
    try {
      return await next(specifier, context)
    } catch (error) {
      const base = new URL(specifier, context.parentURL).href
      for (const candidate of [base + '.ts', base + '/index.ts']) {
        if (fileExists(candidate)) {
          return next(candidate, context)
        }
      }
      throw error
    }
  }
  return next(specifier, context)
}
