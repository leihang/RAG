/**
 * 后端 DTO 类型（手写；`ekbs-api` 多数端点返回裸 JSONResponse，OpenAPI 无 response_model，
 * 因此这些类型是**按实测响应**写下的，字段名与后端逐字对应）。
 */

export interface Page<T> {
  items: T[]
  total: number
  limit?: number
  offset?: number
}

export interface AuthMe {
  user_id: number
  nick?: string
  dingtalk_userid?: string
  roles?: string[]
  permissions?: string[]
}

export interface PermContext {
  user_id: number
  clearance: number
  visible_shard_count?: number
  visible_shards: string[]
  policy_version: number
  fingerprint: string
  visible_kb_ids?: number[]
  warnings?: string[]
}

export interface KnowledgeBase {
  kb_id: number
  code: string
  name: string
  description?: string
  default_level?: number
  status?: number
}

export interface DocumentItem {
  doc_id: number
  kb_id: number
  kb_code?: string
  title: string
  level: number
  status: string
  visible_dept_ids: string
  page_count?: number | null
  doc_version?: string
  effective_date?: string | null
  expire_date?: string | null
  updated_at?: string
  /** 软删除进回收站的时间；仅 status=deleted 时有值 */
  deleted_at?: string | null
  shard_id?: string
  chunk_count?: number
  size_bytes?: number | null
  source_path?: string
}

export interface ParseTask extends DocumentItem {
  parse_path?: string | null
  checksum?: string | null
  created_at?: string
  child_count?: number
  retrievable_count?: number
}

export interface ParseTaskList extends Page<ParseTask> {
  by_status?: Record<string, number>
  success_rate?: number | null
  success_rate_target?: number
  note?: string
}

export interface ChunkNode {
  chunk_id: number
  doc_id: number
  parent_id?: number | null
  chunk_type: string
  seq: number
  content: string
  token_count?: number | null
  heading_path?: string | null
  page_from?: number | null
  page_to?: number | null
  figure_id?: string | null
  image_path?: string | null
  shard_id?: string
  faiss_row?: number | null
}

/**
 * 分块树（`GET /chunks/documents/{id}/tree` 的**真实**结构）：
 * `parents` / `children` 是**计数**（不是数组！），真正的层级在 `tree` 里，
 * 每个父节点把自己那一层子块内嵌在 `children`。
 */
export interface ChunkTreeNode extends ChunkNode {
  children?: ChunkNode[]
  content_head?: string
}

export interface ChunkTree {
  document: { doc_id: number; title?: string; status?: string; level?: number }
  parents: number
  children: number
  tree: ChunkTreeNode[]
  orphan_children?: ChunkNode[]
}

export interface ShardInfo {
  shard_id: string
  dept_code?: string
  level?: number
  dim?: number
  vector_count?: number
  status?: string
  embedding_fingerprint?: string
}

export interface Overview {
  qa_total: number
  refused_total: number
  refusal_rate: number | null
  documents: number
  shards: number
  disk_used_pct: number
  metrics?: Record<string, number>
  active_alerts: number
  recent_alert_events?: Record<string, unknown>[]
}

export interface UserItem {
  user_id: number
  dingtalk_userid: string
  nick?: string
  clearance: number
  status?: string
  dept_ids?: string
  roles?: string[]
}

export interface RoleItem {
  role_id: number
  code: string
  name: string
  is_builtin?: number
  permissions?: string[]
  user_count?: number
}

export interface AclItem {
  acl_id: number
  subject_type: string
  subject_id: string
  kb_id?: number | null
  doc_id?: number | null
  action: string
  effect: string
  created_at?: string
}

export interface PermChangeItem {
  change_id: number
  operator_id?: number
  target_type?: string
  target_id?: string
  action?: string
  reason?: string
  created_at?: string
  before?: string
  after?: string
}

export interface QaSessionItem {
  session_id: string
  user_id?: number
  conversation_id?: string
  question?: string
  answer?: string
  refused?: boolean
  created_at?: string
  message_count?: number
}

export interface SecurityEventItem {
  event_id: number
  event_type?: string
  severity?: string
  user_id?: number
  detail?: string
  created_at?: string
}

export interface DocAccessItem {
  log_id: number
  user_id?: number
  doc_id?: number
  action?: string
  result?: string
  created_at?: string
}

/* ---------------------------------------------------------------------------
 * 服务器目录导入（`ekbs/api/imports.py`，字段按实测报文逐字对齐）
 * ------------------------------------------------------------------------- */

/** `GET /import/meta` 的 `knowledge_bases[]`。 */
export interface ImportKbItem {
  kb_id: number
  code: string
  name: string
  document_count?: number
  /** 实测该端点**当前不返回**这个字段；页面按「有就优先用」处理（见 `ImportMeta` 说明） */
  default_level?: number
}

export interface ImportMeta {
  raw_root?: string
  /** 目录导入放行的后缀（= 解析器 `SUFFIX_ENGINES` 全量） */
  dir_extensions?: string[]
  /** 浏览器上传放行的后缀（= 配置白名单 ∩ 解析器），本页只做提示 */
  upload_extensions?: string[]
  upload_blocked?: string[]
  max_file_size_mb?: number
  upload?: { chunk_size_mb?: number; concurrent?: number; max_retry?: number }
  enqueue_limit?: number
  knowledge_bases?: ImportKbItem[]
  queue?: Record<string, number>
}

/** `GET /import/dirs` 的一层子目录。**注意**没有 `rel_under`（那个只出现在 `recursive=true` 的扁平列表里）。 */
export interface ImportDirEntry {
  name: string
  rel_path: string
  has_children?: boolean
  importable_files?: number
}

export interface ImportDirsResp {
  rel_path: string
  /** raw 根的上级是 `null`；逐层返回上级路径（「回上级」用它，不要自己切字符串） */
  parent: string | null
  dirs: ImportDirEntry[]
  root?: string
  recursive?: boolean
}

/** 扫描预览里一行的动作（`ekbs/ingest/dir_import.py` 的 ACTION_*）。 */
export type ImportAction = 'new' | 'duplicate' | 'too_large' | 'unsupported'

export interface ImportPreviewItem {
  rel_path: string
  size_bytes: number
  action: ImportAction | string
  reason: string
  doc_id: number | null
  importable: boolean
}

export interface ImportScanResp {
  rel_dir: string
  total_files: number
  importable: number
  counts: Partial<Record<ImportAction | string, number>>
  /** 有值即**整批不能导入**（合规闸 / L3-L4 闸 / 密级-部门），后端 `enqueue_files` 会 409 */
  blockers: string[]
  warn: string[]
  /** 最多 200 条；执行导入时**不能**拿它当全量（见 `preview_truncated`） */
  preview: ImportPreviewItem[]
  preview_truncated?: boolean
  preview_limit?: number
  extensions?: number
}

export interface ImportScanBody {
  path: string
  level: number
  dept_ids: string[]
  recursive: boolean
  limit?: number | null
}

export interface ImportRunBody extends ImportScanBody {
  kb_id?: number | null
  kb_code?: string | null
  /** 勾选导入的相对路径；**空数组 = 按目录全量**（后端会自己重扫） */
  files: string[]
}

export interface ImportEnqueuedItem {
  rel_path: string
  doc_id: number
  size_bytes: number
}

export interface ImportRunResp {
  enqueued: ImportEnqueuedItem[]
  skipped: { rel_path: string; doc_id?: number; reason: string }[]
  failed: { rel_path: string; error: string }[]
  kb_id: number
  level: number
  /** = `enqueued.length` */
  queued: number
  /** 后端给的诚实交代：解析由 worker 异步进行，进度看「解析任务」页 */
  hint?: string
}

export interface ImportQueueResp {
  counts: Record<string, number>
  recent: {
    doc_id: number
    title: string
    status: string
    level: number
    source_path: string
    updated_at: string
  }[]
}
