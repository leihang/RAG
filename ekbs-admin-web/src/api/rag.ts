import { http } from './client'

export interface NumericAnswer {
  value?: number | null
  unit?: string
  page?: number | null
  quote?: string
  indicator?: string
  reason?: string
  /** 是否通过「抄写校验」（值必须逐字来自 quote 所在原文片段） */
  verified?: boolean
  cite_index?: number | null
  /** 口径限定词（年份/期间/表名…）：必须能从引用片段里逐字核到 */
  scope?: string
  /** 同一行标签下的**其它数值**（如 2024 年那一行）——有就说明存在口径歧义，需人工确认 */
  alternatives?: { value?: number; label?: string; chunk_id?: number; doc_title?: string; page?: number | null }[]
}

export interface DebugQueryBody {
  query: string
  /** 多轮会话 id；同一 id 连续提问会走查询改写与上下文 */
  conversation_id?: string
  /** 作答模式：prose（默认，业务问答）| numeric（取数） */
  mode?: 'prose' | 'numeric'
  rerank_enabled?: boolean
  dense_top_k?: number
  sparse_top_k?: number
  rerank_keep?: number
  rerank_min_score?: number
  /** @deprecated 用 dense_top_k；保留兼容旧调用 */
  top_k?: number
  keep?: number
  min_score?: number
  simulate_user_id?: number
}

export interface Citation {
  cite_index: number
  chunk_id: number
  doc_id?: number
  doc_title?: string
  page_from?: number | null
  heading_path?: string | null
  acl_result?: string
  page_check?: string
  /** 图片块才有：图 id 与相对 data_dir 的图片路径（`06` §4.11 的"可回源"） */
  figure_id?: string | null
  image_path?: string | null
}

export interface FaithfulnessClaim {
  text: string
  supported: boolean
}

export interface FaithfulnessReport {
  ok: boolean
  cited_indices?: number[]
  claims?: FaithfulnessClaim[]
  unsupported?: string[]
  reason?: string
}

export interface DebugQueryResp {
  query: string
  rewritten_query?: string | null
  history_used?: boolean | null
  conversation_id?: string | null
  answer?: string | null
  refused?: boolean | null
  refusal_reason?: string | null
  model?: string
  citations?: Citation[]
  /** 降级标记（如 `unsupported_claim:9`）：答案已返回，但有一项检查被降级了 */
  degraded?: string[] | null
  /** 作答模式：prose（业务问答，默认）| numeric（取数：值/单位/页码 + 抄写校验） */
  mode?: 'prose' | 'numeric' | null
  /** 取数模式的结构化结果（prose 模式下为 null） */
  numeric?: NumericAnswer | null
  faithfulness?: FaithfulnessReport | null
  stages?: unknown
  timings_ms?: Record<string, number>
}

export const debugQuery = (body: DebugQueryBody) =>
  http.post('/rag/debug/query', body) as Promise<DebugQueryResp>

export const debugRetrieve = (body: DebugQueryBody) =>
  http.post('/rag/debug/retrieve', body) as Promise<Record<string, unknown>>

export const debugRewrite = (body: { query: string; history?: string[] }) =>
  http.post('/rag/debug/rewrite', body) as Promise<Record<string, unknown>>

export const debugSimulate = (body: { query: string; user_id: number }) =>
  http.post('/rag/debug/simulate', body) as Promise<Record<string, unknown>>

export const debugShards = () =>
  http.get('/rag/debug/shards') as Promise<{
    user_id: number
    clearance: number
    dept_chain: string[]
    visible_shards: string[]
    acl_allow_shards: string[]
    acl_deny_shards: string[]
    matched_dept_codes: string[]
    registered_shards: string[]
    warnings: string[]
  }>

export const debugHistory = () => http.get('/rag/debug/history') as Promise<unknown>
