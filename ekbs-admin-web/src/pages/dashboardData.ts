/**
 * 概览看板的**响应整形纯函数**（无 React 依赖，便于单测）。
 *
 * 为什么需要这一层：`src/api/types.ts` 的 `Overview` 与实际后端报文不一致
 * （`ekbs/api/admin.py::dashboard_overview` 是权威口径）：
 *
 * | 字段 | `types.ts` 声明 | 后端实际返回 |
 * |---|---|---|
 * | `documents` | `number` | `{published, failed, pending}` |
 * | `shards` | `number` | `{active, vectors}` |
 * | `active_alerts` | `number` | `AlertHit[]`（`{metric,kind,value,threshold,comparison,severity,window_seconds,samples,description}`） |
 * | `recent_alert_events` | `Record<string, unknown>[]` | 同（`{event_id,event_type,severity,detail,created_at}`） |
 * | `metrics` | `Record<string, number>` | `{name: value}`（来自 `ekbs_metric` 最新值） |
 *
 * 共享层 `types.ts` 不允许在本任务里改，所以页面按**运行时形状**解析：
 * 数字就按数字用，对象就取对象里的字段，取不到就不显示 —— 不造假数据。
 */

/** 只保留可 JSON 序列化的叶子值，避免把 live 对象塞进表格行。 */
export function toRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** 严格取数字：`null`/`undefined`/`NaN`/非数字类型一律返回 null（页面显示 `-`）。 */
export function numberOf(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** 从对象里按 key 取数字；对象不是对象就返回 null。 */
export function numberAt(value: unknown, key: string): number | null {
  const record = toRecord(value)
  return record ? numberOf(record[key]) : null
}

/** 字符串叶子值；其它类型返回 null。 */
export function stringOf(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

/** 字符串数组（用于 `dept_chain` / `visible_shards` / `warnings`）。 */
export function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => stringOf(item)).filter((item): item is string => item !== null)
}

/** `Record<string, number>` 的健壮解析（`metrics`、`timings_ms`）。 */
export function numberRecord(value: unknown): Record<string, number> {
  const record = toRecord(value)
  if (!record) return {}
  const out: Record<string, number> = {}
  for (const [key, raw] of Object.entries(record)) {
    const n = numberOf(raw)
    if (n !== null) out[key] = n
  }
  return out
}

/** 把 unknown 数组压成「只有叶子的对象数组」，非对象项直接丢掉。 */
export function recordList(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  const rows: Record<string, unknown>[] = []
  for (const item of value) {
    const record = toRecord(item)
    if (record) rows.push(record)
  }
  return rows
}

/** 列表端点兼容：`{items:[…]}` 或裸数组，两种都认（`/system/alert-rules` 两种都可能）。 */
export function itemsOf(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return recordList(value)
  const record = toRecord(value)
  if (record && 'items' in record) return recordList(record.items)
  return []
}

export interface OverviewNumbers {
  qaTotal: number | null
  refusedTotal: number | null
  /** 后端给的比例（0–1）；缺失时由 refused/qa 现算，两者都没有则为 null。 */
  refusalRate: number | null
  documents: number | null
  documentsPublished: number | null
  documentsFailed: number | null
  documentsPending: number | null
  shards: number | null
  shardVectors: number | null
  diskUsedPct: number | null
  metrics: Record<string, number>
  activeAlerts: number | null
  /** `active_alerts` 是数组口径时，这里保留逐条命中（用于明细表）。 */
  activeAlertRows: Record<string, unknown>[]
  recentAlertEvents: Record<string, unknown>[]
}

/**
 * 概览接口的运行时整形。
 *
 * `documents` / `shards` / `active_alerts` 三种口径都兼容：
 * 后端给数字就用数字，给 `{published,failed,pending}` / `{active,vectors}` / 数组就展开。
 */
export function readOverview(raw: unknown): OverviewNumbers {
  const record = toRecord(raw) ?? {}

  const qaTotal = numberOf(record.qa_total)
  const refusedTotal = numberOf(record.refused_total)
  const explicitRate = numberOf(record.refusal_rate)
  // 后端 qa_total=0 时 refusal_rate 给 0.0；这里不额外发明口径，只在缺失时按 refused/qa 现算
  const refusalRate =
    explicitRate ?? (qaTotal && qaTotal > 0 ? (refusedTotal ?? 0) / qaTotal : null)

  const documentsDirect = numberOf(record.documents)
  const shardsDirect = numberOf(record.shards)
  const activeAlertsRaw = record.active_alerts
  const activeAlertsDirect = numberOf(activeAlertsRaw)
  const activeAlertRows = recordList(activeAlertsRaw)

  return {
    qaTotal,
    refusedTotal,
    refusalRate,
    documents: documentsDirect ?? numberAt(record.documents, 'published'),
    documentsPublished: numberAt(record.documents, 'published'),
    documentsFailed: numberAt(record.documents, 'failed'),
    documentsPending: numberAt(record.documents, 'pending'),
    shards: shardsDirect ?? numberAt(record.shards, 'active'),
    shardVectors: numberAt(record.shards, 'vectors'),
    diskUsedPct: numberOf(record.disk_used_pct),
    metrics: numberRecord(record.metrics),
    activeAlerts: activeAlertsDirect ?? (Array.isArray(activeAlertsRaw) ? activeAlertRows.length : null),
    activeAlertRows,
    recentAlertEvents: recordList(record.recent_alert_events),
  }
}

export type DiskLevel = 'normal' | 'warning' | 'danger'

/**
 * 磁盘水位分级（方案 05 §4.2「存储占用」：超 80% 标黄、超 90% 标红）。
 * 入参是后端 `disk_used_pct` 的口径：**百分数**（如 `37.5` 表示 37.5%）。
 */
export function diskLevel(pct: number | null | undefined): DiskLevel {
  if (pct == null || !Number.isFinite(pct)) return 'normal'
  if (pct >= 90) return 'danger'
  if (pct >= 80) return 'warning'
  return 'normal'
}

/** 磁盘占用展示文案；缺失显示 `-`。 */
export function diskPercentText(pct: number | null | undefined, digits = 1): string {
  if (pct == null || !Number.isFinite(pct)) return '-'
  return `${pct.toFixed(digits)}%`
}

/** `metrics` → ECharts 柱状图数据（按值降序，便于看头部指标）。 */
export function metricsToBars(metrics: Record<string, number>): { name: string; value: number }[] {
  return Object.entries(metrics)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
}

/** 告警严重度 → Ant Design 色值（`P1/P2/P3` 与 `high/medium/low` 两种口径都出现）。 */
export function severityColor(severity: unknown): string {
  const key = String(severity ?? '').toLowerCase()
  if (key === 'p1' || key === 'high' || key === 'critical') return 'red'
  if (key === 'p2' || key === 'medium' || key === 'warning') return 'orange'
  if (key === 'p3' || key === 'low') return 'blue'
  return 'default'
}

/** 告警严重度中文（不做映射时回落到原值）。 */
export function severityText(severity: unknown): string {
  const raw = stringOf(severity)
  if (raw == null) return '-'
  const key = raw.toLowerCase()
  const map: Record<string, string> = {
    p1: 'P1 严重',
    p2: 'P2 警告',
    p3: 'P3 提示',
    high: '高',
    medium: '中',
    low: '低',
  }
  return map[key] ?? raw
}

/** 最近告警事件 `detail` 是 JSON 字符串，尽量取一句人话（不解析整对象）。 */
export function alertDetailText(detail: unknown, maxLength = 160): string {
  const raw = stringOf(detail)
  if (raw == null) return '-'
  if (raw.startsWith('{') || raw.startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(raw)
      const record = toRecord(parsed)
      if (record) {
        const parts = Object.entries(record)
          .slice(0, 6)
          .map(([key, value]) => `${key}=${stringOf(value) ?? typeof value}`)
        return truncate(parts.join(' '), maxLength)
      }
    } catch {
      // 不是合法 JSON 就按纯文本展示（后端有的是纯文本 detail）
    }
  }
  return truncate(raw, maxLength)
}

function truncate(text: string, maxLength: number): string {
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
}
