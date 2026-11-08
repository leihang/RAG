/**
 * 检索调试台的**响应整形纯函数**（无 React 依赖，便于单测）。
 *
 * `src/api/rag.ts` 里 `DebugQueryResp.stages` 是 `unknown`，因为后端确实没给它定 schema。
 * 真实结构来自 `ekbs/retrieval/pipeline.py::RetrievalPipeline.retrieve(collect_debug=True)`
 * 与 `ekbs/api/playground.py::_chunk_view`：
 *
 * ```
 * stages = {
 *   params: { dense_top_k, sparse_top_k, rrf_k, rerank_enabled, rerank_top_n,
 *             rerank_keep, rerank_min_score, parent_expand },       // 本次请求的生效参数
 *   dense:  { requested_top_k, per_shard: { shard_id: [{faiss_row, score}] },
 *             merged_chunk_ids: number[] },
 *   sparse: { requested_top_k, hits: [{chunk_id, bm25, shard_id}] },
 *   rrf:    { k, fused: [{chunk_id, score}] },
 *   rerank: { enabled, requested_top_n, keep, min_score, scores: [{chunk_id, rerank_score}] },
 *   c5:     { kept: number[], dropped: number[] },
 *   expand: { enabled, deduped, kept_leaf_ids, represented_source_ids, collapsed_kept_ids,
 *             expanded: [{context_chunk_id, source_chunk_id, chunk_type, heading_path,
 *                         rerank_score, rerank_rank}] },
 * }
 * ```
 *
 * 关键约束：**`stages` 的每个子段都是可选/不长存的**（拒答会提前 return，此时只有 `params`），
 * 所以这里全部按「有就解析、没有就不渲染」处理，绝不用零值补齐出一个假的结构。
 */

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 数字叶子：非法值返回 null（页面显示 `-`）。 */
export function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** 布尔叶子：只认真正的 true/false，别的返回 null（不把 'false' 当 false）。 */
export function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}

/** 字符串叶子。 */
export function str(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

/** 数字数组（`merged_chunk_ids` / `kept` / `dropped`）。 */
export function numList(value: unknown): number[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => num(item)).filter((item): item is number => item !== null)
}

function recordList(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  return value.filter(isRecord)
}

// ---------------------------------------------------------------------------
// stages
// ---------------------------------------------------------------------------

/** 生效参数（`stages.params`）——key 是后端 `RetrievalOverrides.effective()` 的真实键名。 */
export type StageParams = Record<string, string | number | boolean | null>

export interface DensePerShard {
  shard_id: string
  hits: { faiss_row: number | null; score: number | null }[]
}

export interface DenseStage {
  requestedTopK: number | null
  perShard: DensePerShard[]
  /** `per_shard` 的原始顺序（分片多时按命中条数降序更实用，这里保留后端顺序但附条数） */
  mergedChunkIds: number[]
}

export interface SparseStage {
  requestedTopK: number | null
  hits: { chunk_id: number | null; bm25: number | null; shard_id: string | null }[]
}

export interface RrfStage {
  k: number | null
  fused: { chunk_id: number | null; score: number | null }[]
}

export interface RerankStage {
  enabled: boolean | null
  requestedTopN: number | null
  keep: number | null
  minScore: number | null
  scores: { chunk_id: number | null; rerank_score: number | null }[]
}

export interface C5Stage {
  kept: number[]
  dropped: number[]
}

/**
 * 一条「上下文块 ← 精排检索单元」的扩展记录（`stages.expand.expanded[i]`）。
 *
 * 精排只给检索单元（叶子/行组）打分，父块/整表容器是精排**之后**才被扩展出来的上下文，
 * 所以 `context_chunk_id` 常常不在 rerank 候选里 —— 这不是异常，来源就是 `source_chunk_id`。
 */
export interface ExpandRow {
  context_chunk_id: number | null
  source_chunk_id: number | null
  chunk_type: string | null
  heading_path: string | null
  rerank_score: number | null
  rerank_rank: number | null
}

/** 第 5 跳：精排后的上下文扩展（父块回溯 / 整表展开）。 */
export interface ExpandStage {
  enabled: boolean | null
  deduped: number | null
  /** 进入扩展前的精排 keep 叶子 */
  keptLeafIds: number[]
  /** 扩展+去重后仍作为 source 出现的叶子 */
  representedSourceIds: number[]
  /**
   * 精排保留但被同父块/同表折叠掉的叶子（内容已在上下文，溯源不再指向它们）。
   * 这是「Rerank 保留 5 / 上下文只溯到 2」的常见原因，不是漏召回。
   */
  collapsedKeptIds: number[]
  expanded: ExpandRow[]
}

export interface DebugStages {
  params: StageParams | null
  dense: DenseStage | null
  sparse: SparseStage | null
  rrf: RrfStage | null
  rerank: RerankStage | null
  c5: C5Stage | null
  expand: ExpandStage | null
}

function parseParams(value: unknown): StageParams | null {
  if (!isRecord(value)) return null
  const out: StageParams = {}
  for (const [key, raw] of Object.entries(value)) {
    if (raw === null || typeof raw === 'string' || typeof raw === 'number' || typeof raw === 'boolean') {
      out[key] = raw
    }
  }
  return Object.keys(out).length ? out : null
}

function parseDense(value: unknown): DenseStage | null {
  if (!isRecord(value)) return null
  const perShardRaw = value.per_shard
  const perShard: DensePerShard[] = []
  if (isRecord(perShardRaw)) {
    for (const [shardId, hitsRaw] of Object.entries(perShardRaw)) {
      perShard.push({
        shard_id: shardId,
        hits: recordList(hitsRaw).map((hit) => ({
          faiss_row: num(hit.faiss_row),
          score: num(hit.score),
        })),
      })
    }
  }
  return {
    requestedTopK: num(value.requested_top_k),
    perShard,
    mergedChunkIds: numList(value.merged_chunk_ids),
  }
}

function parseSparse(value: unknown): SparseStage | null {
  if (!isRecord(value)) return null
  return {
    requestedTopK: num(value.requested_top_k),
    hits: recordList(value.hits).map((hit) => ({
      chunk_id: num(hit.chunk_id),
      bm25: num(hit.bm25),
      shard_id: str(hit.shard_id),
    })),
  }
}

function parseRrf(value: unknown): RrfStage | null {
  if (!isRecord(value)) return null
  return {
    k: num(value.k),
    fused: recordList(value.fused).map((item) => ({
      chunk_id: num(item.chunk_id),
      score: num(item.score),
    })),
  }
}

function parseRerank(value: unknown): RerankStage | null {
  if (!isRecord(value)) return null
  return {
    enabled: bool(value.enabled),
    requestedTopN: num(value.requested_top_n),
    keep: num(value.keep),
    minScore: num(value.min_score),
    scores: recordList(value.scores).map((item) => ({
      chunk_id: num(item.chunk_id),
      rerank_score: num(item.rerank_score),
    })),
  }
}

function parseC5(value: unknown): C5Stage | null {
  if (!isRecord(value)) return null
  return { kept: numList(value.kept), dropped: numList(value.dropped) }
}

function parseExpand(value: unknown): ExpandStage | null {
  if (!isRecord(value)) return null
  return {
    enabled: bool(value.enabled),
    deduped: num(value.deduped),
    keptLeafIds: numList(value.kept_leaf_ids),
    representedSourceIds: numList(value.represented_source_ids),
    collapsedKeptIds: numList(value.collapsed_kept_ids),
    expanded: recordList(value.expanded).map((row) => ({
      context_chunk_id: num(row.context_chunk_id),
      source_chunk_id: num(row.source_chunk_id),
      chunk_type: str(row.chunk_type),
      heading_path: str(row.heading_path),
      rerank_score: num(row.rerank_score),
      rerank_rank: num(row.rerank_rank),
    })),
  }
}

/** `stages` 总解析结果；`raw` 不是对象时全为 null（页面直接不渲染逐跳明细）。 */
export function parseStages(value: unknown): DebugStages {
  const record = isRecord(value) ? value : {}
  return {
    params: parseParams(record.params),
    dense: parseDense(record.dense),
    sparse: parseSparse(record.sparse),
    rrf: parseRrf(record.rrf),
    rerank: parseRerank(record.rerank),
    c5: parseC5(record.c5),
    expand: parseExpand(record.expand),
  }
}

/** 逐跳明细里到底有哪些段落（用于「无明细」判断与提示）。 */
export function stageSectionKeys(stages: DebugStages): string[] {
  const keys: string[] = []
  if (stages.params) keys.push('params')
  if (stages.dense) keys.push('dense')
  if (stages.sparse) keys.push('sparse')
  if (stages.rrf) keys.push('rrf')
  if (stages.rerank) keys.push('rerank')
  if (stages.c5) keys.push('c5')
  // 扩展发生在 C5 之后（精排/C5 只处理检索单元，父块是之后才回溯出来的），排最后
  if (stages.expand) keys.push('expand')
  return keys
}

/** 逐跳段落的展示名（key 与 `stageSectionKeys` 的取值一一对应）。 */
export const STAGE_TITLES: Record<string, string> = {
  params: '本次生效参数',
  dense: '向量召回',
  sparse: 'BM25 召回',
  rrf: 'RRF 融合',
  rerank: 'Rerank 精排',
  c5: 'C5 密级闸门',
  expand: '上下文扩展（父块/整表）',
}

// ---------------------------------------------------------------------------
// timings_ms
// ---------------------------------------------------------------------------

/** 后端实际写过的耗时键（`pipeline.py` + `playground.py`）。 */
const TIMING_LABELS: Record<string, string> = {
  embed: '向量化',
  recall: '双路召回',
  rerank: '精排 Rerank',
  expand: '父块回溯',
  total: '检索总耗时',
  generate_ms: '生成首字',
}

/** 阶段展示顺序（与方案 05 §4.7 的跳数一致）。 */
const TIMING_ORDER = ['embed', 'recall', 'rerank', 'expand', 'total', 'generate_ms']

export interface TimingRow {
  key: string
  label: string
  ms: number
}

/** 耗时表：已知键用中文名并排序，未知键原样附在后面（后端加了新阶段也能看见）。 */
export function timingRows(timings: Record<string, number> | undefined | null): TimingRow[] {
  if (!timings) return []
  const known = TIMING_ORDER.filter((key) => typeof timings[key] === 'number').map((key) => ({
    key,
    label: TIMING_LABELS[key],
    ms: timings[key],
  }))
  const rest = Object.keys(timings)
    .filter((key) => !TIMING_ORDER.includes(key))
    .map((key) => ({ key, label: key, ms: timings[key] }))
  return [...known, ...rest]
}

// ---------------------------------------------------------------------------
// citations / 分片可见性
// ---------------------------------------------------------------------------

export interface CitationRow {
  cite_index: number | null
  chunk_id: number | null
  /** 上下文块由哪条精排检索单元扩展而来；null = 引用本身就是精排单元 */
  source_chunk_id: number | null
  doc_title: string | null
  page_from: number | null
  acl_result: string | null
  page_check: string | null
}

/** 引用表（后端 `playground.py::debug_query` 回这 7 个字段；`doc_id`/`heading_path` 不下发）。 */
export function citationRows(value: unknown): CitationRow[] {
  return recordList(value).map((item) => ({
    cite_index: num(item.cite_index),
    chunk_id: num(item.chunk_id),
    source_chunk_id: num(item.source_chunk_id),
    doc_title: str(item.doc_title),
    page_from: num(item.page_from),
    acl_result: str(item.acl_result),
    page_check: str(item.page_check),
  }))
}

export interface ShardVisibility {
  user_id: number | null
  clearance: number | null
  deptChain: string[]
  visibleShards: string[]
  aclAllowShards: string[]
  aclDenyShards: string[]
  matchedDeptCodes: string[]
  warnings: string[]
  /** `registered_shards` 实际是对象数组（`{shard_id,dept_code,level,status}`），不是 `string[]`。 */
  registeredShards: { shard_id: string | null; dept_code: string | null; level: number | null; status: string | null }[]
}

/** `/rag/debug/shards` 的运行时解析（`registered_shards` 的对象数组口径见上）。 */
export function readShardVisibility(value: unknown): ShardVisibility | null {
  if (!isRecord(value)) return null
  return {
    user_id: num(value.user_id),
    clearance: num(value.clearance),
    deptChain: strList(value.dept_chain),
    visibleShards: strList(value.visible_shards),
    aclAllowShards: strList(value.acl_allow_shards),
    aclDenyShards: strList(value.acl_deny_shards),
    matchedDeptCodes: strList(value.matched_dept_codes),
    warnings: strList(value.warnings),
    registeredShards: recordList(value.registered_shards).map((row) => ({
      shard_id: str(row.shard_id),
      dept_code: str(row.dept_code),
      level: num(row.level),
      status: str(row.status),
    })),
  }
}

function strList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => str(item)).filter((item): item is string => item !== null)
}

// ---------------------------------------------------------------------------
// debugHistory
// ---------------------------------------------------------------------------

export interface HistoryEntry {
  at: string | null
  kind: string | null
  operator: number | null
  query: string | null
  chunks: number | null
  refused: boolean | null
}

export interface DebugHistory {
  items: HistoryEntry[]
  count: number | null
  retention: string | null
}

/**
 * `/rag/debug/history`（`playground.py::debug_history`）返回
 * `{items, count, retention}`；`items[i]` 是 `_record_history` 写入的
 * `{at(epoch 秒), kind, operator, query, chunks, refused}`。
 * 缓存里没有历史时是 `{items: [], count: 0}`。
 */
export function readDebugHistory(value: unknown): DebugHistory | null {
  if (!isRecord(value)) return null
  const items = Array.isArray(value.items) ? value.items : null
  if (!items) return null
  return {
    items: recordList(items).map((entry) => ({
      at: epochSecondsToIso(entry.at),
      kind: str(entry.kind),
      operator: num(entry.operator),
      query: str(entry.query),
      chunks: num(entry.chunks),
      refused: bool(entry.refused),
    })),
    count: num(value.count),
    retention: str(value.retention),
  }
}

/** 历史里的 `at` 是 epoch 秒（`int(time.time())`），其它形式原样返回。 */
export function epochSecondsToIso(value: unknown): string | null {
  const raw = str(value)
  if (raw == null) return null
  const seconds = num(value)
  if (seconds == null || !/^\d{9,11}$/.test(raw)) return raw
  return new Date(seconds * 1000).toISOString()
}

// ---------------------------------------------------------------------------
// debugSimulate
// ---------------------------------------------------------------------------

export interface SimulatedResult {
  operator: number | null
  simulatedUser: number | null
  reason: string | null
  query: string | null
  refused: boolean | null
  refusalReason: string | null
  clearance: number | null
  deptChain: string[]
  visibleShards: string[]
  fingerprint: string | null
  chunkCount: number | null
}

/** `/rag/debug/simulate`（`playground.py::debug_simulate`）的运行时解析。 */
export function readSimulated(value: unknown): SimulatedResult | null {
  if (!isRecord(value)) return null
  const perm = isRecord(value.simulated_perm) ? value.simulated_perm : null
  const chunks = Array.isArray(value.chunks) ? value.chunks : []
  return {
    operator: num(value.operator),
    simulatedUser: num(value.simulated_user),
    reason: str(value.reason),
    query: str(value.query),
    refused: bool(value.refused),
    refusalReason: str(value.refusal_reason),
    clearance: perm ? num(perm.clearance) : null,
    deptChain: perm ? strList(perm.dept_chain) : [],
    visibleShards: perm ? strList(perm.visible_shards) : [],
    fingerprint: perm ? str(perm.fingerprint) : null,
    chunkCount: chunks.length || null,
  }
}

// ---------------------------------------------------------------------------
// 请求体口径
// ---------------------------------------------------------------------------

export interface PlaygroundParams {
  /** `rerank_enabled` —— 后端 `DebugRequest.rerank_enabled` */
  rerankEnabled: boolean
  /** `dense_top_k`（后端把 top_k 拆成两路；方案 05 §4.7 的「top_k（向量）」） */
  denseTopK: number
  /** `sparse_top_k`（「top_k（BM25）」） */
  sparseTopK: number
  /** `rerank_keep`（方案里的「Rerank 输出 / 融合截断」） */
  rerankKeep: number
  /** `rerank_min_score`（「Rerank 阈值」） */
  rerankMinScore: number
  /** 作答模式：`prose`（业务问答）| `numeric`（取数，见 ekbs/retrieval/numeric.py） */
  mode?: 'prose' | 'numeric'
}

export const DEFAULT_PARAMS: PlaygroundParams = {
  rerankEnabled: true,
  denseTopK: 50,
  sparseTopK: 50,
  rerankKeep: 5,
  rerankMinScore: 0.35,
  mode: 'prose',
}

/**
 * 参数面板 → 请求体。
 *
 * ⚠️ 契约缺口：`src/api/rag.ts::DebugQueryBody` 声明的是 `top_k` / `keep` / `min_score`，
 * 但后端 `DebugRequest` 只认 `dense_top_k` / `sparse_top_k` / `rerank_keep` /
 * `rerank_min_score` / `rerank_top_n`（`ekbs/api/playground.py`）。Pydantic 默认忽略未知字段，
 * 所以发 `top_k`/`keep`/`min_score` 会**静默失效**（不报错、参数没生效）。
 * 这里按后端真实字段名下发，保证「参数覆盖真的生效」这条核心语义。
 */
export function buildQueryBody(
  query: string,
  params: PlaygroundParams,
): {
  query: string
  rerank_enabled: boolean
  dense_top_k: number
  sparse_top_k: number
  rerank_keep: number
  rerank_min_score: number
  mode: 'prose' | 'numeric'
} {
  return {
    query,
    mode: params.mode ?? 'prose',
    rerank_enabled: params.rerankEnabled,
    dense_top_k: params.denseTopK,
    sparse_top_k: params.sparseTopK,
    rerank_keep: params.rerankKeep,
    rerank_min_score: params.rerankMinScore,
  }
}

/**
 * 忠实度卡片的呈现口径 —— 与后端 **P0 分级策略**一一对应：
 *
 * * `ok` → 通过（绿）；
 * * 未通过但**没有拒答** → 降级放行（黄）：答案与引用都还在，只是有数字/量词
 *   没能在原文里逐字核对上（表格汇总题常因单位换算/四舍五入命中这一类）；
 * * 未通过且已拒答 → 红。
 *
 * 抽成纯函数是为了能单测：这段口径曾经写死成"未通过=红"，与后端降级策略不一致，
 * 会把"降级放行"误报成"拒答"，让人以为答案没了。
 */
export interface FaithfulnessBanner {
  type: 'success' | 'warning' | 'error'
  message: string
  /** 展示用的一句话结论 */
  detail: string
}

export function faithfulnessBanner(input: {
  ok?: boolean | null
  refused?: boolean | null
  reason?: string | null
  unsupported?: readonly string[] | null
  citedIndices?: readonly number[] | null
  claimCount?: number | null
}): FaithfulnessBanner | null {
  if (input.ok === undefined || input.ok === null) return null
  if (input.ok) {
    return {
      type: 'success',
      message: '通过（规则型支撑检查）',
      detail: `引用编号：[${(input.citedIndices ?? []).join(', ')}]；可核对 claim ${input.claimCount ?? 0} 条均落在入选资料中。`,
    }
  }
  const reason = input.reason || '（未给）'
  if (input.reason === 'missing_citation') {
    return {
      type: 'error',
      message: '未通过（已拒答）',
      detail: `原因：${reason} — 答案无 [n]，禁止挂伪溯源引用。`,
    }
  }
  if (input.refused) {
    return {
      type: 'error',
      message: '未通过（已拒答）',
      detail: `原因：${reason} — 多数数字/量词未在入选分块原文出现，按整篇不可信拒答。`,
    }
  }
  return {
    type: 'warning',
    message: '降级放行（答案与引用已保留）',
    detail:
      `原因：${reason} — 有 ${input.unsupported?.length ?? 0} 个数字/量词未能在原文中逐字核对` +
      `（可能是单位换算或四舍五入）；答案与引用仍然给出，请人工核对后再采信。`,
  }
}
