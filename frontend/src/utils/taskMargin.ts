import type { TaskStats } from '../types/api.ts'

export interface ScheduleMarginPresentation {
  value: string
  bottleneck: string
  tooltip: string
  sufficient: boolean
}

const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })

function formatDate(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const parsed = new Date(`${value}T00:00:00.000Z`)
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) return null
  return value
}

function formatBottleneck(date: string | null, days: number | null): string {
  const fullDate = formatDate(date)
  if (!fullDate || !Number.isInteger(days) || (days as number) < 1) return ''
  return `第${days}天 · ${fullDate.slice(5)}`
}

function unavailableValue(reason: string | null): string {
  return reason === 'no_pending_tasks' ? '暂无任务' : '未取得'
}

function unavailableTooltip(reason: string | null): string {
  if (reason === 'no_pending_tasks') return '暂无待完成任务，当前没有可计算的容错时长。'
  if (reason === 'insufficient_days') return '剩余可排期天数不足，暂未取得容错时长。'
  if (reason === 'invalid_plan') return '任务计划信息不完整，暂未取得容错时长。'
  return '当前暂未取得容错时长。'
}

/** 只格式化后端给出的容错计划，不在前端重算充足性或瓶颈。 */
export function getScheduleMarginPresentation(
  stats: Pick<TaskStats, 'schedule_margin'> | null | undefined,
): ScheduleMarginPresentation {
  const margin = stats?.schedule_margin
  if (!margin) {
    return {
      value: '未取得',
      bottleneck: '',
      tooltip: '当前服务端未提供容错时长数据。',
      sufficient: false,
    }
  }

  const hasHours = typeof margin.hours === 'number' && Number.isFinite(margin.hours)
  const value = margin.sufficient
    ? '余量充足'
    : hasHours
      ? `${numberFormat.format(margin.hours as number)} 小时`
      : unavailableValue(margin.reason)

  if (!hasHours) {
    return {
      value,
      bottleneck: margin.sufficient
        ? ''
        : formatBottleneck(margin.bottleneck_date, margin.bottleneck_days),
      tooltip: unavailableTooltip(margin.reason),
      sufficient: margin.sufficient,
    }
  }

  const bottleneckDate = formatDate(margin.bottleneck_date)
  const hasBottleneckDay = Number.isInteger(margin.bottleneck_days)
    && (margin.bottleneck_days as number) >= 1
  const bottleneckDetail = bottleneckDate && hasBottleneckDay
    ? `瓶颈日期：${bottleneckDate}（第${margin.bottleneck_days}天）。`
    : ''
  const businessDateDetail = formatDate(margin.business_date)
    ? `业务日：${margin.business_date}。`
    : ''

  return {
    value,
    bottleneck: margin.sufficient
      ? ''
      : formatBottleneck(margin.bottleneck_date, margin.bottleneck_days),
    tooltip: [
      `可容错时长：${String(margin.hours)} 小时。`,
      bottleneckDetail,
      businessDateDetail,
      '每天理论容量 22 小时；按今天尚未完成任何任务计算，今日执行进度不改变此指标。',
    ].filter(Boolean).join(' '),
    sufficient: margin.sufficient,
  }
}
