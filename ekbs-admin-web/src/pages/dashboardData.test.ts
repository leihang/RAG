import { describe, expect, it } from 'vitest'
import {
  alertDetailText,
  diskLevel,
  diskPercentText,
  itemsOf,
  metricsToBars,
  numberRecord,
  readOverview,
  severityColor,
  stringList,
} from './dashboardData'

/**
 * 样本取自 `ekbs/api/admin.py::dashboard_overview` 的**真实返回形状**
 * （`documents`/`shards`/`active_alerts` 是对象与数组，不是 `types.ts` 里的 number）。
 */
const REAL_OVERVIEW = {
  qa_total: 128,
  refused_total: 9,
  refusal_rate: 0.0703,
  documents: { published: 42, failed: 3, pending: 7 },
  shards: { active: 4, vectors: 182340 },
  disk_used_pct: 81.5,
  metrics: { qa_count: 128, miss_count: 12, p95_latency_ms: 1840 },
  active_alerts: [
    {
      metric: 'retrieval_p95_ms',
      kind: 'quantile',
      value: 2410.5,
      threshold: 2000,
      comparison: '>',
      severity: 'P2',
      window_seconds: 300,
      samples: 120,
      description: '检索 P95 > 2s',
    },
  ],
  recent_alert_events: [
    {
      event_id: 7,
      event_type: 'metric_alert',
      severity: 'high',
      detail: '{"metric":"retrieval_p95_ms","value":2410.5}',
      created_at: '2025-09-23 09:12:00',
    },
  ],
}

describe('readOverview 兼容后端真实形状', () => {
  it('documents / shards 是对象时逐字段展开', () => {
    const ov = readOverview(REAL_OVERVIEW)
    expect(ov.documents).toBe(42)
    expect(ov.documentsPublished).toBe(42)
    expect(ov.documentsFailed).toBe(3)
    expect(ov.documentsPending).toBe(7)
    expect(ov.shards).toBe(4)
    expect(ov.shardVectors).toBe(182340)
  })

  it('active_alerts 是数组时取长度计数', () => {
    const ov = readOverview(REAL_OVERVIEW)
    expect(ov.activeAlerts).toBe(1)
    expect(ov.recentAlertEvents).toHaveLength(1)
  })

  it('也兼容 types.ts 声明的 number 口径（后端若改成数字仍可用）', () => {
    const ov = readOverview({
      qa_total: 10,
      refused_total: 2,
      refusal_rate: 0.2,
      documents: 5,
      shards: 3,
      disk_used_pct: 12,
      active_alerts: 2,
    })
    expect(ov.documents).toBe(5)
    expect(ov.documentsPublished).toBeNull()
    expect(ov.shards).toBe(3)
    expect(ov.shardVectors).toBeNull()
    expect(ov.activeAlerts).toBe(2)
  })

  it('缺字段不伪造：null 与空数组', () => {
    const ov = readOverview({})
    expect(ov.qaTotal).toBeNull()
    expect(ov.refusedTotal).toBeNull()
    expect(ov.refusalRate).toBeNull()
    expect(ov.documents).toBeNull()
    expect(ov.shards).toBeNull()
    expect(ov.diskUsedPct).toBeNull()
    expect(ov.activeAlerts).toBeNull()
    expect(ov.metrics).toEqual({})
    expect(ov.recentAlertEvents).toEqual([])
  })

  it('refusal_rate 缺失时按 refused/qa 现算；qa_total=0 时为 null 而不是 0', () => {
    expect(readOverview({ qa_total: 200, refused_total: 50 }).refusalRate).toBe(0.25)
    expect(readOverview({ qa_total: 0, refused_total: 0 }).refusalRate).toBeNull()
  })

  it('后端显式给的 refusal_rate 优先（哪怕是 0）', () => {
    expect(readOverview({ qa_total: 200, refused_total: 50, refusal_rate: 0 }).refusalRate).toBe(0)
  })

  it('非数字 metrics 值被丢弃，metrics 不是对象时为空', () => {
    expect(numberRecord({ a: 1, b: 'x', c: null, d: 2 })).toEqual({ a: 1, d: 2 })
    expect(readOverview({ metrics: 'oops' }).metrics).toEqual({})
  })
})

describe('磁盘水位分级（≥80% 黄、≥90% 红）', () => {
  it('按阈值分级', () => {
    expect(diskLevel(10)).toBe('normal')
    expect(diskLevel(79.9)).toBe('normal')
    expect(diskLevel(80)).toBe('warning')
    expect(diskLevel(89.9)).toBe('warning')
    expect(diskLevel(90)).toBe('danger')
    expect(diskLevel(null)).toBe('normal')
    expect(diskLevel(Number.NaN)).toBe('normal')
  })

  it('文案：缺失显示 -', () => {
    expect(diskPercentText(81.5)).toBe('81.5%')
    expect(diskPercentText(null)).toBe('-')
  })
})

describe('列表端点兼容 {items:[]} 与裸数组', () => {
  it('两种形状都认', () => {
    expect(itemsOf({ items: [{ a: 1 }] })).toEqual([{ a: 1 }])
    expect(itemsOf([{ a: 1 }, { b: 2 }])).toEqual([{ a: 1 }, { b: 2 }])
    expect(itemsOf(null)).toEqual([])
    expect(itemsOf({ items: [1, 'x', { ok: true }] })).toEqual([{ ok: true }])
  })
})

describe('metricsToBars 与告警文案', () => {
  it('按值降序，不补零', () => {
    expect(metricsToBars({ a: 1, b: 9, c: 5 })).toEqual([
      { name: 'b', value: 9 },
      { name: 'c', value: 5 },
      { name: 'a', value: 1 },
    ])
    expect(metricsToBars({})).toEqual([])
  })

  it('severity 色值兼容 P1/P2/P3 与 high/medium/low', () => {
    expect(severityColor('P1')).toBe('red')
    expect(severityColor('high')).toBe('red')
    expect(severityColor('P2')).toBe('orange')
    expect(severityColor('medium')).toBe('orange')
    expect(severityColor('P3')).toBe('blue')
    expect(severityColor(undefined)).toBe('default')
  })

  it('detail 是 JSON 字符串时压成 key=value，纯文本原样返回', () => {
    expect(alertDetailText('{"metric":"p95","value":2410.5}')).toBe('metric=p95 value=2410.5')
    expect(alertDetailText('磁盘水位过高')).toBe('磁盘水位过高')
    expect(alertDetailText(null)).toBe('-')
  })

  it('stringList 只保留字符串叶子', () => {
    expect(stringList(['a', 1, null, 'b'])).toEqual(['a', '1', 'b'])
    expect(stringList('a')).toEqual([])
  })
})
