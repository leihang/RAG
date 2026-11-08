import { http } from './client'
import type { ChunkNode, ChunkTree, ParseTaskList } from './types'

export interface ParseTaskQuery {
  limit?: number
  offset?: number
  status?: string
  kb_id?: number
  /** 按文档名/导入路径搜索（LIKE） */
  q?: string
}

export const listParseTasks = (params: ParseTaskQuery = {}) =>
  http.get('/parse/tasks', { params }) as Promise<ParseTaskList>

/** 任务详情是嵌套的：`{task, state_machine}`（状态机是允许的状态序列）。 */
export const getParseTask = (taskId: number) =>
  http.get(`/parse/tasks/${taskId}`) as Promise<{
    task: Record<string, unknown>
    state_machine?: string[]
  }>

/** 解析结果预览：后端返回的是 `preview`（不是 `markdown`），另有分块结构 `structure`。 */
export const getParseResult = (taskId: number) =>
  http.get(`/parse/tasks/${taskId}/result`) as Promise<{
    task_id: number
    structure?: ChunkNode[]
    chunk_count?: number
    preview?: string
    preview_truncated?: boolean
    note?: string
    needs_review?: boolean
    markdown?: string
  }>

export const retryParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/retry`) as Promise<unknown>

export const cancelParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/cancel`) as Promise<unknown>

export interface ParseDraftTable {
  table_id: string
  caption?: string
  page?: number | null
  body_html: string
  chars?: number
  /** 原生 Word 版式（colspan/rowspan）：回存时带回去，避免编辑一次就丢合并结构 */
  preserve_layout?: boolean
}

export interface ParseDraftFigure {
  figure_id: string
  caption?: string
  ocr_text?: string
  page?: number | null
  image_path?: string
  has_image?: boolean
  /** normal | faint | blank —— 淡色附图多为 faint；印章在清洗时已丢弃 */
  kind?: string
  ink_frac?: number | null
  display_hint?: string
}

/** 扫描件复核草稿（全文 + 表格 HTML + 图片清单） */
export const getParseDraft = (taskId: number) =>
  http.get(`/parse/tasks/${taskId}/draft`) as Promise<{
    task_id: number
    status: string
    markdown: string
    chars?: number
    engine?: string
    parse_ms?: number | null
    figure_count?: number
    table_count?: number
    tables?: ParseDraftTable[]
    figures?: ParseDraftFigure[]
    clean_stats?: Record<string, unknown>
    note?: string
  }>

/** 复核期取图（默认 enhance=1：淡色附图抬对比；入库后改用 /assets/figure/{chunk_id}） */
export const fetchParseDraftFigure = (
  taskId: number,
  figureId: string,
  opts: { enhance?: boolean } = {},
) =>
  http.get(`/parse/tasks/${taskId}/figures/${figureId}`, {
    params: { enhance: opts.enhance === false ? 0 : 1 },
    responseType: 'blob',
    // 清洗后图号漂移/缺图时缩略图自己显示「图不可用」，不要弹全局异常
    skipErrorToast: true,
  }) as Promise<Blob>

export interface DraftAssetSave {
  markdown: string
  tables?: Array<{
    table_id: string
    caption?: string
    body_html: string
    page?: number | null
  }>
  figures?: Array<{
    figure_id: string
    caption?: string
    ocr_text?: string
  }>
}

export const saveParseDraft = (taskId: number, body: string | DraftAssetSave) =>
  http.put(
    `/parse/tasks/${taskId}/draft`,
    typeof body === 'string' ? { markdown: body } : body,
  ) as Promise<{
    task_id: number
    status: string
    chars: number
  }>

/** 复核通过 → 分块入库；可顺带提交最终正文（如粘贴 WPS 结果） */
export const approveParseTask = (taskId: number, markdown?: string) =>
  http.post(
    `/parse/tasks/${taskId}/approve`,
    markdown == null ? {} : { markdown },
  ) as Promise<{
    task_id: number
    status: string
    parent_count?: number
    child_count?: number
  }>

/** 批量“手动向量入库”：把 review 文档置 pending_index，由 worker 统一处理（segmented 布局） */
export const approveParseTasksBatch = (taskIds: number[]) =>
  http.post('/parse/tasks/approve-batch', { task_ids: taskIds }) as Promise<{
    queued: number[]
    skipped: Array<{ task_id: number; reason: string }>
    status: string
  }>

/** 对已分块待入库（chunked）文档做向量入库 */
export const vectorizeParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/vectorize`) as Promise<{
    task_id: number
    status: string
    parent_count?: number
    child_count?: number
    vector_count?: number
  }>

export const rejectParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/reject`) as Promise<unknown>

/** 生成/确认文档「代表问句」（推荐问答 tier-3）：缺省 LLM 生成，带 question 则人工确认 */
export const generateDocRecommend = (taskId: number, question?: string) =>
  http.post(
    `/parse/tasks/${taskId}/recommend`,
    question == null ? {} : { question },
  ) as Promise<{ doc_id: number; question: string; status: string }>

/** rerank 验证代表问句能否命中本文档（需已向量入库） */
export const verifyDocRecommend = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/recommend/verify`) as Promise<{
    doc_id: number
    question: string
    rerank_score: number | null
    verified: boolean
    status: string
  }>

export const getParseConfig = () =>
  http.get('/parse/config') as Promise<Record<string, unknown>>

export const getChunkTree = (docId: number) =>
  http.get(`/chunks/documents/${docId}/tree`) as Promise<ChunkTree>

export const getChunk = (chunkId: number) =>
  http.get(`/chunks/${chunkId}`) as Promise<{ chunk: ChunkNode & Record<string, unknown> }>

/** 文档内块检索（不走全库 RAG；空 q 按 seq 浏览） */
export const searchDocumentChunks = (
  docId: number,
  params: { q?: string; limit?: number } = {},
) =>
  http.get(`/chunks/documents/${docId}/search`, { params }) as Promise<{
    document: { doc_id: number; title?: string; status?: string; level?: number }
    query: string
    total: number
    chunks: ChunkNode[]
  }>

export const updateChunk = (chunkId: number, body: { content: string; reason?: string }) =>
  http.put(`/chunks/${chunkId}`, body) as Promise<unknown>

export const splitChunk = (chunkId: number, body: { offset?: number; reason?: string } = {}) =>
  http.post(`/chunks/${chunkId}/split`, body) as Promise<unknown>

export const mergeChunk = (chunkId: number, body: { reason?: string } = {}) =>
  http.post(`/chunks/${chunkId}/merge`, body) as Promise<unknown>

export const reembedChunk = (chunkId: number) =>
  http.post(`/chunks/${chunkId}/reembed`) as Promise<unknown>

export const revectorizeDocument = (docId: number) =>
  http.post(`/chunks/documents/${docId}/revectorize`) as Promise<unknown>
