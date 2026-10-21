/**
 * "点哪条开哪条"的确认流程（桌面移植阶段 2 迁移到新协议）。
 *
 * 旧版断言的是旧协议的凭证（`old_*` 五元组 + `confirm_id` + `stop_epoch`）与
 * `conflictInfoFromState`；新链上放弃与新开是**一项**请求
 * （`/api/live/restart-after-abandon`），冻结的是原 Run/Goal/选择三件套。
 *
 * 被验证的规则（所有权规则只有一条，且必须在第一次确认 UI 之前完成）：
 *   1) **冻结在弹框之前**：`plan()` 由调用方在任何确认 UI 之前构建好整份
 *      `FrozenOperation`（含客户端令牌）；流程只负责"确认 → 派发这一份"，
 *      不再回读任何 store 值；
 *   2) 有原场才多一次"放弃"确认，确认的**就是已冻结的那一份**（文案与载荷同源）；
 *   3) 用户取消 → 零控制提交，旧进度零改动；
 *   4) **没有"再取一张票"这一步**：令牌在冻结那一刻已生成；
 *   5) 被拒绝不自动重来：版本过期只会得到"请刷新后重新确认"；
 *   6) 结果属于别的账号代次（superseded）→ 不提示、不改任何状态。
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

const { runSpecifiedTaskStart } = load('utils/startTaskFlow.ts')

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

const TARGET = { taskId: 41, expectedRevision: 12, businessDate: '2026-10-10' }
const ABANDON = {
  runId: 'run-old', goalRevision: 4, zoneName: '学习区',
  businessDate: '2026-10-10', selection: { zone_name: '学习区' },
}

/** 一份"已冻结"的计划（真实页面在第一个确认 UI 之前构建的形状）。 */
function makePlan({ withAbandon = false } = {}) {
  const startBody = {
    mode: 'task', task_id: TARGET.taskId,
    expected_revision: TARGET.expectedRevision,
    business_date: TARGET.businessDate, push_source: 'ffmpeg',
  }
  const operation = withAbandon
    ? { url: '/api/live/restart-after-abandon', token: 'frozen-token-1',
        accountGeneration: 0, label: '放弃并重新开播',
        body: { ...startBody, abandon: { ...ABANDON.selection, run_id: ABANDON.runId,
                 goal_revision: ABANDON.goalRevision } } }
    : { url: '/api/live/start', token: 'frozen-token-1', accountGeneration: 0,
        label: '指定任务开播', body: { ...startBody } }
  return {
    operation,
    abandon: withAbandon ? { ...ABANDON } : null,
    target: { ...TARGET },
    pushSource: 'ffmpeg',
  }
}

/** 可脚本化的依赖注入（记录调用轨迹）。 */
function makeDeps({ plan = makePlan(), confirmAnswer = true,
                    results: startResults = null } = {}) {
  const calls = { plan: 0, start: [], confirm: [], notify: [] }
  let startCall = 0
  return {
    calls,
    deps: {
      plan: () => {
        calls.plan += 1
        return plan
      },
      start: async (operation) => {
        calls.start.push(operation)
        const result = (startResults
          || [{ superseded: false, notice: null, status: 'accepted' }])[
          Math.min(startCall, (startResults || [null]).length - 1)]
        startCall += 1
        return result
      },
      confirmAbandon: async (info) => {
        calls.confirm.push(info)
        return confirmAnswer
      },
      notify: (kind, message) => calls.notify.push([kind, message]),
    },
  }
}

await test('无原场：确认一次、派发冻结的那一份，不带 abandon', async () => {
  const plan = makePlan()
  const { deps, calls } = makeDeps({ plan })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, plan.operation, '返回的就是冻结操作本身')
  assert.equal(calls.plan, 1, '流程只取一次已冻结的计划')
  assert.equal(calls.confirm.length, 0, '没有原场就不弹放弃确认')
  assert.equal(calls.start.length, 1)
  assert.equal(calls.start[0], plan.operation, '派发的就是冻结的那一份')
  assert.equal(calls.start[0].body.abandon, undefined, '不带放弃上下文')
})

await test('有原场：先确认放弃，再派发同一份冻结操作', async () => {
  const plan = makePlan({ withAbandon: true })
  const { deps, calls } = makeDeps({ plan })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, plan.operation)
  assert.equal(calls.confirm.length, 1)
  assert.deepEqual(calls.confirm[0],
    { zone: '学习区', day: '2026-10-10', targetTaskId: 41 },
    '确认文案取自已冻结的原场上下文')
  assert.equal(calls.start.length, 1)
  assert.equal(calls.start[0], plan.operation, '确认之后不重建载荷')
  assert.ok(calls.start[0].body.abandon, '请求携带冻结的原场三件套')
  assert.equal(calls.start[0].token, 'frozen-token-1', '令牌在冻结那一刻已生成')
})

await test('用户取消放弃：零控制提交，旧进度零改动', async () => {
  const plan = makePlan({ withAbandon: true })
  const { deps, calls } = makeDeps({ plan, confirmAnswer: false })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, null, '取消后不返回任何已派发操作')
  assert.equal(calls.start.length, 0, '取消后不得发开播请求')
  assert.deepEqual(calls.notify.at(-1), ['info', '已取消，旧进度未做任何改动'])
})

await test('无法冻结（目标或原场上下文缺失）：不发请求并如实提示', async () => {
  const { deps, calls } = makeDeps({ plan: null })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, null)
  assert.equal(calls.start.length, 0)
  assert.equal(calls.notify[0][0], 'error')
})

await test('被拒绝（版本过期）不自动重发：提示用户重新确认', async () => {
  const plan = makePlan()
  const { deps, calls } = makeDeps({
    plan,
    results: [{ superseded: false, notice: null, status: 'rejected' }],
  })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, plan.operation)
  assert.equal(calls.start.length, 1, '被拒绝不得自动换版本重发')
  assert.deepEqual(calls.notify.at(-1)[0], 'warning')
  assert.match(calls.notify.at(-1)[1], /过期/)
})

await test('结果属于别的账号代次：不提示、不改状态', async () => {
  const plan = makePlan()
  const { deps, calls } = makeDeps({
    plan,
    results: [{ superseded: true, notice: null, status: 'unknown' }],
  })
  const outcome = await runSpecifiedTaskStart(deps)
  assert.equal(outcome, plan.operation)
  assert.deepEqual(calls.notify, [], 'superseded 的结果不得写进当前界面')
})

await test('未受理但有明确原因：按回执语义如实转述并给出下一步', async () => {
  const plan = makePlan()
  const { deps, calls } = makeDeps({
    plan,
    results: [{ superseded: false,
                notice: { level: 'error', message: '未被受理：目标已变化' },
                status: 'rejected' }],
  })
  await runSpecifiedTaskStart(deps)
  // 具体原因不丢，且只发一条（不另发一条互相矛盾的通用"已过期"）。
  assert.deepEqual(calls.notify,
    [['warning', '未被受理：目标已变化。请刷新任务列表后重新确认']])
})

await test('分区不可用被拒：保留明确原因，不追加确认过期', async () => {
  const plan = makePlan()
  const message = '未被受理：该分区已不存在或已下架'
  const { deps, calls } = makeDeps({
    plan,
    results: [{ superseded: false, notice: { level: 'error', message }, status: 'rejected' }],
  })
  await runSpecifiedTaskStart(deps)
  assert.equal(calls.start.length, 1, '不自动重发被拒的操作')
  assert.deepEqual(calls.notify, [['error', message]], '一条明确原因足以说明分区不可用')
})

const failed = results.filter(r => r.status === 'FAIL').length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
