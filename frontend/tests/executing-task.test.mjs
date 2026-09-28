/**
 * "正在执行"按稳定 task_id 判定（2026-09-28 收口，B03，工作包 C 验收）。
 *
 * 被验证的规则：
 *   1) 手动模式零任务标签——任何行都不得因同名分区被打上"正在执行"；
 *   2) 只有当前稳定 task_id 的那一行拿到标签，且阶段标签按状态优先级；
 *   3) 不在播/无 id/已结束任务 → 无标签；
 *   4) 推迟接管窗口（takeover_pending）显示"开播中"，不冒称"正在执行"。
 *
 * 运行：node tests/executing-task.test.mjs（纯函数模块，无外部依赖）。
 */
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const req = createRequire(path.join(root, 'package.json'))
const ts = req('typescript')

/** 转载一个 TS 模块（CommonJS 包装，无 @/ 依赖的纯函数模块）。 */
function load(relative) {
  const file = path.join(root, 'src', relative)
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
    { filename: file })(req, module, module.exports)
  return module.exports
}

const { resolveExecutingTaskId, resolveExecutingLabel, resolveExecutingTagType } =
  load('utils/executingTask.ts')

const results = []
async function test(name, body) {
  try {
    await body()
    results.push({ name, status: 'PASS' })
    console.log(`PASS  ${name}`)
  } catch (error) {
    results.push({ name, status: 'FAIL', message: error.message })
    console.log(`FAIL  ${name} — ${error.message}`)
  }
}

const base = {
  is_streaming: true, is_starting: false, is_cancelling: false,
  pending_face_verify: false, stream_mode: 'task', current_task_id: 7,
  takeover_pending: false,
}

await test('任务模式在播：只有当前 task_id 的行拿到"正在执行"', async () => {
  assert.equal(resolveExecutingTaskId(base), 7)
  assert.equal(resolveExecutingLabel(base, 7), '正在执行')
  assert.equal(resolveExecutingLabel(base, 8), '', '其它行不得误标')
  assert.equal(resolveExecutingTagType(base, 7), 'warning')
})

await test('手动模式零任务标签（同名分区也不冒充）', async () => {
  const manual = { ...base, stream_mode: 'manual' }
  assert.equal(resolveExecutingTaskId(manual), null)
  assert.equal(resolveExecutingLabel(manual, 7), '')
})

await test('不在播/受理前无 id/已结束 → 无标签', async () => {
  assert.equal(resolveExecutingTaskId({ ...base, is_streaming: false,
    is_starting: false, is_cancelling: false }), null)
  assert.equal(resolveExecutingTaskId({ ...base, current_task_id: null }), null)
  assert.equal(resolveExecutingTaskId({ ...base, current_task_id: undefined }), null)
  assert.equal(resolveExecutingTaskId(null), null)
  assert.equal(resolveExecutingLabel({ ...base, current_task_id: null }, 7), '')
})

await test('待验证 > 正在停止 > 开播中 > 恢复中 > 正在执行 的标签优先级', async () => {
  assert.equal(resolveExecutingLabel({ ...base, pending_face_verify: true }, 7), '待验证')
  assert.equal(resolveExecutingTagType({ ...base, pending_face_verify: true }, 7), 'danger')
  assert.equal(resolveExecutingLabel({ ...base, is_cancelling: true }, 7), '正在停止')
  assert.equal(resolveExecutingLabel({ ...base, is_starting: true }, 7), '开播中')
  assert.equal(resolveExecutingLabel({ ...base, takeover_pending: true }, 7), '开播中')
  assert.equal(resolveExecutingLabel({ ...base, recovery_blocked: 'x' }, 7), '恢复中')
})

await test('受理中（is_starting）也算活动会话，按 id 打"开播中"', async () => {
  const starting = { ...base, is_streaming: false, is_starting: true }
  assert.equal(resolveExecutingTaskId(starting), 7)
  assert.equal(resolveExecutingLabel(starting, 7), '开播中')
})

const failed = results.filter(r => r.status === 'FAIL').length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
