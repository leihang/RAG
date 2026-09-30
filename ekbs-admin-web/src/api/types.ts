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
