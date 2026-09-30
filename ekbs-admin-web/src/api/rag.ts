import { http } from './client'

export interface DebugQueryBody {
  query: string
  rerank_enabled?: boolean
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
  answer?: string | null
  refused?: boolean | null
  refusal_reason?: string | null
  model?: string
  citations?: Citation[]
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
