/**
 * 指定任务开播确认流程（2026-09-28 收口，A01/A02 前端半场 + B01 顺序约束）。
 *
 * 被验证的规则：
 *   1) 目标在任何弹框之前**冻结一次**；确认后的重发**复用同一 target**，
 *      绝不重新冻结（那等于把用户确认的内容悄悄换掉）；
 *   2) 覆盖确认的事实来自服务端状态快照；确认后凭证完整
 *      （old_* 五元组 + confirm_id + stop_epoch）；
 *   3) 用户取消/凭证事实不完整 → 不发请求，旧进度保留；
 *   4) 服务端 409（已有直播进行中）→ 重取快照 → 重新确认 → 复用同一 target 重发。
 *
 * 运行：node tests/start-task-flow.test.mjs（纯逻辑模块，依赖全部注入）。
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

const {
  runSpecifiedTaskStart, conflictInfoFromState,
} = load('utils/startTaskFlow.ts')

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

const TARGET = { id: 41, expectedRevision: 12, businessDate: '2026-09-28' }
const SAVED = {
  has_active: true, run_id: 'run-old', task_id: 17, current_zone: '学习区',
  execution_date: '2026-09-28', elapsed_seconds: 600,
}

/** 可脚本化的依赖注入（记录调用轨迹）。 */
function makeDeps({
  savedState = null, confirmAnswer = true, startResults = null, epoch = 7,
} = {}) {
  const calls = { freeze: 0, start: [], confirm: [], stateFetch: 0 }
  let startCall = 0
  return {
    calls,
    deps: {
      freezeStartTarget: () => {
        calls.freeze += 1
        return { ...TARGET }
      },
      fetchSavedState: async () => {
        calls.stateFetch += 1
        return conflictInfoFromState(savedState)
      },
      start: async (target, abandonSaved) => {
        calls.start.push({ target: { ...target }, abandonSaved: abandonSaved || null })
        const result = (startResults || [{ success: true, message: '直播已开始' }])[
          Math.min(startCall, (startResults || [{ success: true }]).length - 1)]
        startCall += 1
        return result
      },
      confirmAbandon: async (info) => {
        calls.confirm.push(info)
        return confirmAnswer
      },
      fetchControlEpoch: async () => epoch,
      newConfirmId: () => `confirm-${calls.start.length + 1}`,
      notify: () => {},
    },
  }
}

await test('无保存进度：冻结一次、不带凭证直接开播', async () => {
  const { deps, calls } = makeDeps({ savedState: null })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, true)
  assert.equal(calls.freeze, 1, '目标只冻结一次')
  assert.equal(calls.start.length, 1)
  assert.equal(calls.start[0].abandonSaved, null, '无保存进度不带凭证')
  assert.deepEqual(calls.start[0].target, TARGET)
})

await test('有保存进度：确认后携带完整凭证开播（同一 target）', async () => {
  const { deps, calls } = makeDeps({ savedState: SAVED })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, true)
  assert.equal(calls.confirm.length, 1)
  const claim = calls.start[0].abandonSaved
  assert.ok(claim, '必须携带凭证')
  assert.equal(claim.old_run_id, 'run-old')
  assert.equal(claim.old_task_id, 17)
  assert.equal(claim.old_zone_name, '学习区')
  assert.equal(claim.old_execution_date, '2026-09-28')
  assert.equal(claim.old_elapsed_seconds, 600)
  assert.equal(claim.stop_epoch, 7)
  assert.ok(claim.confirm_id, '必须有一次性确认 id')
  assert.deepEqual(calls.start[0].target, TARGET, '开播 target 与冻结一致')
})

await test('用户取消覆盖：不发请求（旧进度保留）', async () => {
  const { deps, calls } = makeDeps({ savedState: SAVED, confirmAnswer: false })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, false)
  assert.equal(calls.start.length, 0, '取消后不得发开播请求')
})

await test('冲突事实不完整（缺已播秒数）：拒绝发凭证', async () => {
  const broken = { ...SAVED, elapsed_seconds: undefined }
  const { deps, calls } = makeDeps({ savedState: broken })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, false)
  assert.equal(calls.start.length, 0, '凭证不完整不得开播')
})

await test('服务端 409（已有直播进行中）：重取快照、重新确认、复用同一 target', async () => {
  const { deps, calls } = makeDeps({
    savedState: SAVED,
    startResults: [
      { success: false, message: '已有直播进行中，不能重复开播', refusal: 'already_streaming' },
      { success: true, message: '直播已开始' },
    ],
  })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, true)
  assert.equal(calls.stateFetch, 2, '首查 + 409 后重取')
  assert.equal(calls.confirm.length, 2, '首次覆盖确认 + 409 后重新确认')
  assert.equal(calls.start.length, 2)
  assert.deepEqual(calls.start[0].target, calls.start[1].target,
    '重发复用同一个冻结 target，绝不重新冻结')
  assert.ok(calls.start[0].abandonSaved, '首次（有保存进度）已携带凭证')
  assert.ok(calls.start[1].abandonSaved, '重发必须携带重新确认的新凭证')
  assert.notEqual(calls.start[0].abandonSaved.confirm_id,
                  calls.start[1].abandonSaved.confirm_id,
                  '重试是一次新的确认（新 confirm_id），不是重放旧凭证')
})

await test('目标不明确（行已被删除重建找不到 id）：不发请求', async () => {
  const { deps, calls } = makeDeps({})
  deps.freezeStartTarget = () => null
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome.success, false)
  assert.equal(calls.start.length, 0)
})

await test('conflictInfoFromState：非活动状态/缺字段按未知处理，不虚构数字', async () => {
  assert.equal(conflictInfoFromState({ has_active: false }), null)
  assert.equal(conflictInfoFromState(null), null)
  const info = conflictInfoFromState({ has_active: true })
  assert.equal(info.hasSaved, true)
  assert.equal(info.oldTaskId, null, '缺 task_id 按未知，不编造')
  assert.equal(info.oldElapsedSeconds, undefined)
})

const failed = results.filter(r => r.status === 'FAIL').length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
