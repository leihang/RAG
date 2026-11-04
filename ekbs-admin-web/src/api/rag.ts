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
  /** 实际喂给 LLM 的**上下文块** id（可能是父块/整表容器，如 35274） */
  chunk_id: number
  /**
   * 该上下文块由哪条**精排检索单元**扩展而来（如 35277）；null/缺省 = 引用本身就是精排单元。
   * 精排只给检索单元打分，父块容器不在精排候选里 —— 两个 id 分开暴露才能解释「这块从哪来的」。
   */
  source_chunk_id?: number | null
  doc_id?: number
  doc_title?: string
  page_from?: number | null
  heading_path?: string | null
  acl_result?: string
  page_check?: string
  /** 图片块才有：图 id 与相对 data_dir 的图片路径（`06` §4.11 的"可回源"） */
  figure_id?: string | null
  image_path?: string | null
  /** 图片块是否已入多模态旁路索引 */
  figure_mm_indexed?: boolean
}

/**
 * 真正喂给 LLM 的**一块上下文**（`POST /rag/debug/query` 的 `context_chunks[]`，task-5 新增）。
 *
 * 必须和「引用（citations）」分开看：引用只包含答案里真的标了 `[n]` 的块，
 * 而上下文还包含没被引用、但同样进了 prompt 的块（实测 doc 205：3 块进 prompt、2 块被引用）。
 * `index` 为 1-based，与答案里的 `[n]` 编号一致；`cited` = 该编号是否被答案引用。
 */
export interface ContextChunk {
  /** 1-based，与答案中的 `[n]` 编号一致 */
  index: number
  /** 上下文块 id（可能是父块/整表容器） */
  chunk_id: number
  /** parent | child | table | figure */
  chunk_type: string
  doc_title?: string | null
  heading_path?: string | null
  /** 该块内容的 token 数（后端按生成模型分词口径统计） */
  token_count: number
  rerank_score?: number | null
  /** 非空 = 该上下文块由这条精排检索单元扩展而来（父块/整表） */
  source_chunk_id?: number | null
  /** 精排名次（1-based）：扩展块取来源块的名次，未扩展块取自身名次 */
  source_rerank_rank?: number | null
  /** 答案里是否真的标了 `[index]`（false = 仅进入上下文，未进引用/审计） */
  cited: boolean
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
  /** D22：拒答时附带的相关示例问 */
  suggested_questions?: string[] | null
  model?: string
  citations?: Citation[]
  /**
   * 最终喂给 LLM 的上下文块（按 `[n]` 顺序）。旧后端没有该字段 ——
   * 前端据此**整卡不渲染**，而不是显示一张空表。
   */
  context_chunks?: ContextChunk[]
  /** 上下文总 token（后端给出的合计，等于 context_chunks 各 token_count 之和） */
  context_tokens?: number
  /** 找图意图或引用含图时：后端汇总的可回源图片（调试台单独展示原图） */
  figure_attachments?: {
    chunk_id: number
    figure_id?: string
    image_path?: string
    doc_title?: string
  }[] | null
  /** 降级标记（如 `unsupported_claim:9`）：答案已返回，但有一项检查被降级了 */
  degraded?: string[] | null
  /** 作答模式：prose（业务问答，默认）| numeric（取数：值/单位/页码 + 抄写校验） */
  mode?: 'prose' | 'numeric' | null
  /** 实际生效模式（含 auto→numeric） */
  mode_used?: string | null
  /** 查询意图：factual / numeric / figure / procedure */
  intent?: string | null
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

export interface SimulatedChunk {
  chunk_id: number
  doc_id: number
  shard_id: string
  doc_title: string
  chunk_type: string
  heading_path: string
  dense_rank: number | null
  sparse_rank: number | null
  rrf_score: number
  rerank_score: number | null
  /** 非空 = 本块是精排后扩展出的父块/整表上下文，来源块 id（=`_chunk_view` 的 expanded_from） */
  expanded_from?: number | null
  /** 精排名次（1-based）：未扩展时是本块自己的名次，无精排分数时为 null */
  source_rerank_rank?: number | null
}

/**
 * 权限模拟**问答**（`POST /rag/debug/simulate`）。
 *
 * 后端走完整 `answer()`（检索 + 生成），响应与 `/rag/debug/query` 同形，
 * 额外带 operator / simulated_user / simulated_perm，前端复用同一套答案渲染。
 */
export interface SimulateResp extends DebugQueryResp {
  operator: number
  simulated_user: number
  reason: string
  simulated_perm: {
    clearance: number
    dept_chain: string[]
    visible_shards: string[]
    fingerprint: string
  }
  /** 命中的检索块（给"看证据"用） */
  chunks: SimulatedChunk[]
}

export const debugSimulate = (body: {
  query: string
  user_id: number
  reason: string
  mode?: string
}) =>
  http.post('/rag/debug/simulate', body) as Promise<SimulateResp>

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
