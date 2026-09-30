import { http } from './client'
import type { DocumentItem, Page } from './types'

export interface DocListQuery {
  limit?: number
  offset?: number
  kb_id?: number
  status?: string
  level?: number
  q?: string
}

export const listDocuments = (params: DocListQuery = {}) =>
  http.get('/docs/documents', { params }) as Promise<Page<DocumentItem>>

/** 详情是**嵌套**的：`{document, chunks:{child,parent}, shards, citation_count}`。
 *  注意 `document` 里**没有** `chunk_count`（那是列表接口才有的字段），
 *  分块数要从 `chunks` 求和 —— 早先把详情当扁平对象读，页面直接白屏。 */
export interface DocumentDetail {
  document: DocumentItem
  chunks?: { child?: number; parent?: number; table?: number; figure?: number }
  shards?: string[]
  citation_count?: number
}

export const getDocumentDetail = (docId: number) =>
  http.get(`/docs/documents/${docId}`) as Promise<DocumentDetail>

/** 只要文档本体时的便捷封装。 */
export const getDocument = async (docId: number): Promise<DocumentItem> =>
  (await getDocumentDetail(docId)).document

export const updateDocument = (docId: number, body: Record<string, unknown>) =>
  http.put(`/docs/documents/${docId}`, body) as Promise<unknown>

export const publishDocument = (docId: number, reason?: string) =>
  http.post(`/docs/documents/${docId}/publish`, { reason }) as Promise<unknown>

export const unpublishDocument = (docId: number, reason?: string) =>
  http.post(`/docs/documents/${docId}/unpublish`, { reason }) as Promise<unknown>

export const offlineDocument = (docId: number, reason?: string) =>
  http.post(`/docs/documents/${docId}/offline`, { reason }) as Promise<unknown>

export const deleteDocument = (docId: number) =>
  http.delete(`/docs/documents/${docId}`) as Promise<unknown>

/** 批量动作（后端按动作分别鉴权）。 */
/**
 * 批量发布/下线/改密级/删除。
 *
 * ⚠️ 返回类型按**后端真实报文**写（`ekbs/api/docs.py::batch_documents`）：
 * `{action, succeeded: <数字>, results: [{doc_id, ok, status|error}]}`。
 * 原先这里声明成 `{succeeded?: number[]; failed?: [...]}` —— 两个字段后端都不发，
 * 于是调用方 `res.succeeded.length` 恒为 undefined、`res.failed` 恒为空数组，
 * **部分失败会被显示成"全部已提交"**。消费方请一律过 `normalizeBatchResult`。
 */
export interface BatchRawResult {
  action: string
  succeeded: number
  results: { doc_id: number; ok: boolean; status?: string; error?: string }[]
}

export const batchDocuments = (docIds: number[], action: string, reason?: string) =>
  http.post('/docs/documents/batch', { doc_ids: docIds, action, reason }) as Promise<BatchRawResult>

/** 上传三段式（方案 §4.4 / §6.5）。 */
export interface UploadInitBody {
  /** 相对路径（上传文件夹时是 `制度/2026/预算表.xlsx`），后端会保留层级 */
  filename: string
  /** 可选标题；不传则后端用文件名的最后一段 */
  title?: string | null
  size_bytes: number
  checksum?: string | null
  kb_id?: number | null
  kb_code?: string | null
  level?: number
  visible_dept_ids?: string[]
}

export type UploadInitResp =
  | { instant: true; doc_id: number; message?: string }
  | { instant: false; upload_id: string; chunk_size_hint: number; uploaded_chunks?: number[] }

export const uploadCheckHash = (checksum: string, filename?: string, sizeBytes?: number) =>
  http.post('/docs/upload/check-hash', {
    checksum,
    filename: filename ?? null,
    size_bytes: sizeBytes ?? null,
  }) as Promise<{ instant: boolean; doc_id?: number; message?: string }>

export const uploadStatus = (uploadId: string) =>
  http.get(`/docs/upload/${uploadId}/status`) as Promise<{
    upload_id: string
    uploaded_chunks: number[]
    chunk_size_hint: number
    size_bytes: number
  }>

export const uploadInit = (body: UploadInitBody) =>
  http.post('/docs/upload/init', body) as Promise<UploadInitResp>

export const uploadChunk = (uploadId: string, index: number, blob: Blob, filename: string) => {
  const form = new FormData()
  // 方案 §6.5 字段名 + 现有别名一并提交，后端两者都认
  form.append('chunk', blob, filename)
  form.append('file', blob, filename)
  form.append('chunk_index', String(index))
  form.append('index', String(index))
  return http.post(`/docs/upload/${uploadId}/chunk`, form) as Promise<{
    upload_id: string
    received: number
    index: number
    uploaded_chunks?: number[]
  }>
}

export const uploadComplete = (uploadId: string) =>
  http.post(`/docs/upload/${uploadId}/complete`) as Promise<{
    doc_id: number
    status: string
    checksum: string
    path: string
  }>


// ---------------------------------------------------------------------------
// 知识库管理（方案 05 §8.1 的 /kb/knowledge-bases；本轮补齐）
// ---------------------------------------------------------------------------

export interface KnowledgeBaseItem {
  kb_id: number
  code: string
  name: string
  description?: string | null
  default_level: number
  status?: number
  document_count?: number
}

export const listKnowledgeBases = () =>
  http.get('/kb/knowledge-bases') as Promise<{ items: KnowledgeBaseItem[]; total: number }>

export const createKnowledgeBase = (body: {
  code: string
  name: string
  description?: string
  default_level?: number
}) => http.post('/kb/knowledge-bases', body) as Promise<KnowledgeBaseItem>

export const updateKnowledgeBase = (kbId: number, body: { name?: string; description?: string; default_level?: number }) =>
  http.put(`/kb/knowledge-bases/${kbId}`, body) as Promise<KnowledgeBaseItem>

export const deleteKnowledgeBase = (kbId: number) =>
  http.delete(`/kb/knowledge-bases/${kbId}`) as Promise<{ kb_id: number; deleted: boolean }>
