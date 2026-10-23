/**
 * 解析任务中心（方案 05 §4.5）的**报文整形与能力判定** —— 纯函数，可单测。
 *
 * ## 为什么需要这一层：`src/api/parse.ts` 的返回类型与实测报文不一致
 *
 * 共享层（`src/api/**`）不在本页改动范围内，所以由本文件承担双向兼容。
 * 下表是**实测**（`curl` 打运行中的 `ekbs-api`）而不是推测：
 *
 * | 位置 | `src/api/parse.ts` 声明 | 实测后端 |
 * |---|---|---|
 * | `GET /parse/tasks` 行对象 | `doc_id`，且 `ParseTask` 带 `retrievable_count` / `size_bytes` | 主键叫 **`task_id`**；**没有** `size_bytes` / `retrievable_count` |
 * | `GET /parse/tasks/{id}/result` | `{doc_id, engine, markdown, page_count, truncated, note}` | `{task_id, structure, chunk_count, **preview**, **preview_truncated**, note}` |
 * | `GET /parse/tasks/{id}` | `Record<string, unknown>` | `{task:{…}, state_machine, failure_hint?}` |
 * | `POST …/retry` | — | `{task_id, status:'pending', previous_status}` |
 * | `POST …/cancel` | — | `{task_id, status:'failed', note, policy_version}` |
 *
 * 读取策略：**两种字段名都认，取不到就是取不到**（返回 `null` / 空串），
 * 绝不编造一个"看起来合理"的值。
 */
import type { ParseTask } from '@/api/types'

/* -------------------------------------------------------------------------- */
/* 行对象                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * `GET /parse/tasks` 的一行。
 *
 * `Omit<…,'doc_id'>` 是因为**列表端点真的不返回 `doc_id`**（只有 `task_id`），
 * 直接 `extends ParseTask` 会让 TS 认为 `doc_id` 一定存在，从而掩盖这个缺口。
 */
export type ParseTaskRow = Omit<ParseTask, 'doc_id'> & {
  doc_id?: number
  /** 实测的列表主键 = `doc_id`（本栈一个文档就是一个解析任务）。 */
  task_id?: number
}

/** 列表接口附带的汇总块。 */
export interface ParseSummary {
  byStatus: Record<string, number>
  successRate: number | null
  successRateTarget: number | null
  /** 后端用它做诚实交代，原样展示 */
  note: string
}

export const EMPTY_SUMMARY: ParseSummary = {
  byStatus: {},
  successRate: null,
  successRateTarget: null,
  note: '',
}

/** 行主键：实测取 `task_id`，兼容声明的 `doc_id`。 */
export function parseTaskId(row: ParseTaskRow | null | undefined): number | null {
  if (!row) return null
  return asNumberOrNull(row.task_id) ?? asNumberOrNull(row.doc_id)
}

/* -------------------------------------------------------------------------- */
/* 状态机与操作能力                                                            */
/* -------------------------------------------------------------------------- */

/** 后端状态机（`ekbs/api/parse_tasks.py` 的 `VALID_STATUSES`）。 */
export const PARSE_STATUSES = [
  'pending',
  'parsing',
  'review',
  'indexing',
  'published',
  'failed',
  'offline',
] as const

export const STATUS_LABEL: Record<string, string> = {
  pending: '待解析',
  parsing: '解析中',
  review: '待复核',
  indexing: '索引中',
  published: '已发布',
  failed: '失败',
  offline: '已下线',
}

export function statusLabel(status?: string | null): string {
  const s = String(status ?? '')
  return STATUS_LABEL[s] ?? (s || '-')
}

/** 按状态机顺序排列 `by_status`；后端将来加的新状态排在后面（不丢数据）。 */
export function orderedStatusEntries(
  byStatus?: Record<string, number> | null,
): Array<[string, number]> {
  if (!byStatus) return []
  const known = PARSE_STATUSES.filter((s) => s in byStatus)
  const extra = Object.keys(byStatus)
    .filter((k) => !(PARSE_STATUSES as readonly string[]).includes(k))
    .sort()
  return [...known, ...extra].map((k) => [k, Number(byStatus[k] ?? 0)] as [string, number])
}

/** 是否达到验收线（`success_rate >= success_rate_target`，方案 §4.5 的 ≥98%）。 */
export function successRateMet(
  rate?: number | null,
  target?: number | null,
): boolean | null {
  if (rate == null || target == null) return null
  const r = Number(rate)
  const t = Number(target)
  if (!Number.isFinite(r) || !Number.isFinite(t)) return null
  return r >= t
}

/** 后端 `retry` 对 `parsing` / `indexing` / `published` 一律 409，前端据此置灰。 */
const RETRY_BLOCKED = new Set(['parsing', 'indexing', 'published'])

export function canRetryTask(status?: string | null): boolean {
  const s = String(status ?? '')
  return s !== '' && !RETRY_BLOCKED.has(s)
}

export function canReviewTask(status?: string | null): boolean {
  return String(status ?? '') === 'review'
}

export function retryBlockedReason(status?: string | null): string {
  const s = String(status ?? '')
  if (s === 'parsing' || s === 'indexing') {
    return `任务正在${statusLabel(s)}，不能重排；请等它结束或先取消`
  }
  if (s === 'published') return '已发布的文档无需重排；如需更新请先下线'
  return s === '' ? '状态未知，无法判断是否可重跑' : ''
}

/** 后端只允许取消 `pending`（其余 409）。 */
export function canCancelTask(status?: string | null): boolean {
  return String(status ?? '') === 'pending'
}

export function cancelBlockedReason(status?: string | null): string {
  if (canCancelTask(status)) return ''
  return `只能取消待解析（pending）任务，当前状态为「${statusLabel(status)}」`
}

/* -------------------------------------------------------------------------- */
/* 关键字过滤                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * 关键字匹配（文档名 / 任务 ID）。
 *
 * ⚠️ `GET /parse/tasks` **没有关键字参数**（只有 status / kb_id / limit / offset），
 * 所以这个筛选只能作用于**当前页**；页面会如实提示，不假装是全库检索。
 */
export function matchesKeyword(row: ParseTaskRow, keyword: string): boolean {
  const kw = keyword.trim().toLowerCase()
  if (!kw) return true
  const id = parseTaskId(row)
  const haystack = [row.title, id == null ? '' : String(id)]
  return haystack.some((v) => String(v ?? '').toLowerCase().includes(kw))
}

/* -------------------------------------------------------------------------- */
/* `GET /parse/tasks/{id}/result`                                              */
/* -------------------------------------------------------------------------- */

export interface ParseStructureRow {
  chunkId: number
  parentId: number | null
  chunkType: string
  seq: number
  headingPath: string
  tokenCount: number | null
  pageFrom: number | null
  pageTo: number | null
  contentHead: string
}

export interface ParseResultView {
  taskId: number | null
  /** 实测字段名是 `preview`；声明里叫 `markdown` —— 两者都认。 */
  markdown: string
  /** 实测 `preview_truncated`；声明里叫 `truncated`。 */
  truncated: boolean
  chunkCount: number | null
  note: string
  /** 实测不返回 `engine`，取不到就是空串。 */
  engine: string
  structure: ParseStructureRow[]
}

export function normalizeParseResult(raw: unknown): ParseResultView {
  const r = asRecord(raw) ?? {}
  const structureRaw = Array.isArray(r.structure) ? r.structure : []
  return {
    taskId: asNumberOrNull(r.task_id) ?? asNumberOrNull(r.doc_id),
    markdown: asString(r.markdown ?? r.preview),
    truncated: asBool(r.truncated ?? r.preview_truncated),
    chunkCount: asNumberOrNull(r.chunk_count),
    note: asString(r.note),
    engine: asString(r.engine),
    structure: structureRaw.map(normalizeStructureRow),
  }
}

export function normalizeStructureRow(raw: unknown): ParseStructureRow {
  const r = asRecord(raw) ?? {}
  return {
    chunkId: asNumberOrNull(r.chunk_id) ?? 0,
    parentId: asNumberOrNull(r.parent_id),
    chunkType: asString(r.chunk_type) || '-',
    seq: asNumberOrNull(r.seq) ?? 0,
    headingPath: asString(r.heading_path),
    tokenCount: asNumberOrNull(r.token_count),
    pageFrom: asNumberOrNull(r.page_from),
    pageTo: asNumberOrNull(r.page_to),
    contentHead: asString(r.content_head),
  }
}

/* -------------------------------------------------------------------------- */
/* `GET /parse/tasks/{id}`                                                     */
/* -------------------------------------------------------------------------- */

export interface ParseTaskDetailView {
  retrievableCount: number | null
  sizeBytes: number | null
  childCount: number | null
  chunkCount: number | null
  pageCount: number | null
  shardId: string
  parsePath: string
  sourcePath: string
  /** 失败任务的诚实交代（后端 `failure_hint`，指向 worker 日志，不编造错误信息）。 */
  failureHint: string
  stateMachine: string[]
}

export function normalizeParseTaskDetail(raw: unknown): ParseTaskDetailView {
  const outer = asRecord(raw) ?? {}
  const task = asRecord(outer.task) ?? outer
  return {
    retrievableCount: asNumberOrNull(task.retrievable_count),
    sizeBytes: asNumberOrNull(task.size_bytes),
    childCount: asNumberOrNull(task.child_count),
    chunkCount: asNumberOrNull(task.chunk_count),
    pageCount: asNumberOrNull(task.page_count),
    shardId: asString(task.shard_id),
    parsePath: asString(task.parse_path),
    sourcePath: asString(task.source_path),
    failureHint: asString(outer.failure_hint),
    stateMachine: Array.isArray(outer.state_machine) ? outer.state_machine.map((s) => asString(s)) : [],
  }
}

/* -------------------------------------------------------------------------- */
/* 通用小工具                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * 接口是否**真的**返回了某字段（用于「接口没有的字段不显示」）。
 *
 * 判据是「至少一行有该自有属性且值非空」—— 全是 `null` 也视为没数据。
 */
export function rowsHaveField(rows: readonly unknown[], key: string): boolean {
  return rows.some((row) => {
    const r = asRecord(row)
    if (!r) return false
    return Object.prototype.hasOwnProperty.call(r, key) && r[key] != null
  })
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

export function asString(value: unknown): string {
  if (value == null) return ''
  return typeof value === 'string' ? value : String(value)
}

export function asNumberOrNull(value: unknown): number | null {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

export function asBool(value: unknown): boolean {
  return value === true || value === 1 || value === 'true' || value === '1'
}
