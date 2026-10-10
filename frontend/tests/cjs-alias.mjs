/**
 * cjs-alias.mjs - 让 loadTs 型测试的 CommonJS require 也能解析 `@/...`。
 *
 * 这些测试用 `ts.transpileModule` 把 store 源码编译成 CJS 后在 vm 里执行；
 * 它们的自定义 require 以前只映射少数几个模块，兜底是 `createRequire`——
 * 而它当然不认识 `@/api/authWire` 这类别名。阶段 2 新增了 `api/authWire.ts`、
 * `api/liveControl.ts`、`api/operationToken.ts`、`types/session.ts`，
 * tasks/auth/live store 都会经由 store 间接 require 到它们。
 *
 * 这里提供一个小兜底：`@/x` → 编译并装载 `src/x.ts`（带缓存），`@/router`
 * 给无副作用桩（路由守卫/视图导入会拖进整个应用，测试不走 401 分支）。
 * 其余模块仍由调用方的映射表优先处理。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const require = createRequire(path.join(root, 'package.json'))
const ts = require('typescript')

const compiled = new Map()

/** 编译并装载 `src/<relFile>`；`relFile` 可带或不带 `.ts`。 */
export function loadSrcModule(relFile, customRequire) {
  const rel = relFile.endsWith('.ts') ? relFile : `${relFile}.ts`
  const file = path.join(root, 'src', rel)
  const source = fs.readFileSync(file, 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    esModuleInterop: true,
  } }).outputText
  const module = { exports: {} }
  const fn = vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                                 { filename: file })
  fn(customRequire, module, module.exports)
  return module.exports
}

/**
 * 兜底解析器：调用方映射表没接住的 `@/...` 在这里落到真实源码。
 *
 * `@/router` 是唯一例外：真实 router 静态 import 全部视图，会把整棵应用拖进
 * 测试进程，而这些测试从不触发 401 跳转（那是它唯一的消费点），给无副作用桩。
 */
export function makeAliasFallback(baseRequire, extra = {}) {
  const routerStub = { default: { push() {}, replace() {}, currentRoute: { value: { name: '', fullPath: '/' } } } }
  const resolved = new Map()
  // `resolve` 同时作为**递归**装载的 require 传入：`@/api/liveControl` 里的
  // `@/api/request` 必须命中同一份 extra 表，否则替身只在顶层生效。
  const resolve = (name) => {
    if (Object.prototype.hasOwnProperty.call(extra, name)) {
      const value = extra[name]
      return typeof value === 'function' ? value() : value
    }
    if (name === '@/router') return routerStub
    if (!name.startsWith('@/')) return baseRequire(name)
    if (resolved.has(name)) return resolved.get(name)
    const exports = loadSrcModule(name.slice(2), resolve)
    resolved.set(name, exports)
    return exports
  }
  return resolve
}
