import assert from 'node:assert/strict'
import { getScheduleMarginPresentation } from '../src/utils/taskMargin.ts'

const legacy = getScheduleMarginPresentation({})
assert.equal(legacy.value, '未取得', 'a legacy response without schedule_margin stays compatible')
assert.equal(legacy.bottleneck, '')

const negative = getScheduleMarginPresentation({
  schedule_margin: {
    business_date: '2026-10-21',
    hours: -1.5,
    bottleneck_date: '2026-10-22',
    bottleneck_days: 2,
    sufficient: false,
    reason: null,
  },
})
assert.equal(negative.value, '-1.5 小时')
assert.equal(negative.bottleneck, '第2天 · 10-22')
assert.match(negative.tooltip, /可容错时长：-1\.5 小时/)
assert.match(negative.tooltip, /瓶颈日期：2026-10-22（第2天）/)
assert.match(negative.tooltip, /按今天尚未完成任何任务计算，今日执行进度不改变此指标/)

const decimal = getScheduleMarginPresentation({
  schedule_margin: {
    business_date: '2026-10-21',
    hours: 3.25,
    bottleneck_date: '2026-10-21',
    bottleneck_days: 1,
    sufficient: false,
    reason: null,
  },
})
assert.equal(decimal.value, '3.25 小时')
assert.equal(decimal.bottleneck, '第1天 · 10-21', 'the current day is day one')

const sufficient = getScheduleMarginPresentation({
  schedule_margin: {
    business_date: '2026-10-21',
    hours: 22.5,
    bottleneck_date: '2026-10-24',
    bottleneck_days: 4,
    sufficient: true,
    reason: null,
  },
})
assert.equal(sufficient.value, '余量充足')
assert.equal(sufficient.bottleneck, '', 'a sufficient plan keeps the metric cell compact')
assert.match(sufficient.tooltip, /可容错时长：22\.5 小时/)
assert.match(sufficient.tooltip, /2026-10-24（第4天）/)

const noPending = getScheduleMarginPresentation({
  schedule_margin: {
    business_date: '2026-10-21',
    hours: null,
    bottleneck_date: null,
    bottleneck_days: null,
    sufficient: false,
    reason: 'no_pending_tasks',
  },
})
assert.equal(noPending.value, '暂无任务')
assert.match(noPending.tooltip, /暂无待完成任务/)

const unavailable = getScheduleMarginPresentation({
  schedule_margin: {
    business_date: '2026-10-21',
    hours: null,
    bottleneck_date: null,
    bottleneck_days: null,
    sufficient: false,
    reason: 'invalid_plan',
  },
})
assert.equal(unavailable.value, '未取得')
assert.match(unavailable.tooltip, /计划信息不完整/)

console.log('schedule margin presentation checks passed')
