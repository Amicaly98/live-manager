/**
 * "正在执行"按稳定 task_id 判定（桌面移植阶段 2 迁移到新投影）。
 *
 * 旧版断言的是旧控制器状态（`is_streaming` / `is_starting` / `takeover_pending` /
 * `pending_face_verify`），新链上这些字段已不存在。判定职责不变，判据换成投影：
 *
 *   1) 只认**稳定身份** `run.task_id`——同名分区不冒充（分区名可被删除重建）；
 *   2) 单次场次（`mode='single'`）没有任务身份，任何行都不得被打上"正在执行"；
 *   3) `step='wait_tasks'`（用户在跑但暂时没有可执行任务）**不是**某条任务在跑；
 *   4) 阶段标签只描述**已确认事实**：停止收尾中 / 开播确认中 / 恢复中 / 正在执行 /
 *      连接中；未知返回空串，绝不把未知伪装成"待执行"或"停播"。
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

/** 一份最小投影：与后端 projection.build_projection 的字段对齐。 */
function projection({ step = 'live', mode = 'task', taskId = 7,
                      checkpoint = 0, run = 'run-A' } = {}) {
  return {
    step, run_id: run, has_run: true,
    run: { id: run, mode, task_id: taskId,
           clock: { checkpoint_seconds: checkpoint } },
    goal: { wanted: 'running' },
  }
}

const live = projection()

await test('任务模式在播：只有当前 task_id 的行拿到"正在执行"', async () => {
  assert.equal(resolveExecutingTaskId(live), 7)
  assert.equal(resolveExecutingLabel(live, false, 7), '正在执行')
  assert.equal(resolveExecutingLabel(live, false, 8), '', '其它行不得误标')
  assert.equal(resolveExecutingTagType('正在执行'), 'warning')
})

await test('单次场次零任务标签（同名分区也不冒充）', async () => {
  const manual = projection({ mode: 'single' })
  assert.equal(resolveExecutingTaskId(manual), null)
  assert.equal(resolveExecutingLabel(manual, false, 7), '')
})

await test('没有场次 / 空闲 / 等待任务 / 未知 step → 无标签', async () => {
  assert.equal(resolveExecutingTaskId(null), null)
  assert.equal(resolveExecutingTaskId(projection({ step: 'idle' })), null)
  assert.equal(resolveExecutingTaskId(projection({ step: 'wait_tasks' })), null)
  assert.equal(resolveExecutingTaskId(projection({ step: '' })), null)
  assert.equal(resolveExecutingTaskId(projection({ taskId: null })), null)
  // 任务身份缺失（对象上没有这个键）同样不算"正在执行"。
  const noTaskId = projection()
  delete noTaskId.run.task_id
  assert.equal(resolveExecutingTaskId(noTaskId), null)
  assert.equal(resolveExecutingLabel(projection({ step: 'idle' }), false, 7), '')
})

await test('停止收尾 > 恢复中 > 开播确认中 > 正在执行 的标签优先级', async () => {
  assert.equal(resolveExecutingLabel(projection({ step: 'close_check' }), false, 7),
               '停止收尾中')
  assert.equal(resolveExecutingLabel(projection({ step: 'close_stop' }), false, 7),
               '停止收尾中')
  assert.equal(resolveExecutingLabel(projection({ step: 'resume_query',
                                                  checkpoint: 1234 }), false, 7),
               '恢复中')
  assert.equal(resolveExecutingTagType('恢复中'), 'danger')
  assert.equal(resolveExecutingLabel(projection({ step: 'open' }), false, 7),
               '开播确认中')
  assert.equal(resolveExecutingLabel(projection({ step: 'choose' }), false, 7),
               '开播确认中')
})

await test('恢复等待首次确认：有检查点是"恢复中"，全新场是"开播确认中"', async () => {
  assert.equal(resolveExecutingLabel(projection({ step: 'resume_query',
                                                  checkpoint: 1234 }), false, 7),
               '恢复中')
  assert.equal(resolveExecutingLabel(projection({ step: 'resume_query',
                                                  checkpoint: 0 }), false, 7),
               '开播确认中')
})

await test('失联只说"连接中"，不把未知当成停播', async () => {
  assert.equal(resolveExecutingLabel(live, true, 7), '连接中')
  assert.equal(resolveExecutingTagType('连接中'), 'info')
  // 没有可执行任务时不产生标签：失联标签也不能凭空落到某一行上。
  assert.equal(resolveExecutingLabel(projection({ step: 'wait_tasks' }), true, 7), '')
})

const failed = results.filter(r => r.status === 'FAIL').length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
