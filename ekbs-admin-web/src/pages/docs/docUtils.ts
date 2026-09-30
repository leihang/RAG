/**
 * 文档管理模块的**纯函数**工具（方案 05 §4.4 / §5.4 / §5.5 / §6.5）。
 *
 * 为什么单独一个文件：列表、上传、详情三个页面共用同一套"真实报文整形"逻辑，
 * 而这些逻辑（报文形状适配、上传分片规划、批量结果归一化）必须能被 `npx vitest run` 覆盖。
 *
 * ⚠️ 本文件里每个"整形"函数都对应一处**契约与后端实现不一致**（README §3 记的实测口径），
 * 不是防御性编程的洁癖 —— 具体差异见各函数注释。共享层（`src/api`、`src/utils`）不归本模块改，
 * 所以适配放在这里，页面只消费整形后的结果。
 */
import type { DocumentItem } from '@/api/types'
import { canAccessLevel, normalizeLevel } from '@/utils/permission'

/* ------------------------------------------------------------------ *
 * 上传准入（§4.4 边界 + §6.5）
 * ------------------------------------------------------------------ */

/**
 * 允许上传的后缀。
 * 取值 = `ekbs-api` 的 `limits.file_ext_whitelist` ∩ 解析器 `SUFFIX_ENGINES`
 * （`ekbs/api/docs.py::upload_init` 的准入就是这两个集合的交集）。
 */
export const UPLOAD_ACCEPT_EXTS = [
  'pdf', 'docx', 'doc', 'pptx', 'xlsx', 'xls', 'txt', 'md', 'png', 'jpg', 'jpeg',
] as const

/** `<Upload accept>` 用的属性值。 */
export const UPLOAD_ACCEPT_ATTR = UPLOAD_ACCEPT_EXTS.map((e) => `.${e}`).join(',')

/**
 * 单文件上限（方案 §6.5 / 后端 ``limits.max_file_size_mb``，现已对齐为 500）。
 */
export const MAX_FILE_SIZE_MB = 500
export const MAX_FILE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

/** 取小写后缀（不含点）；没有后缀返回空串。 */
export function fileExtension(name: string): string {
  const idx = name.lastIndexOf('.')
  return idx >= 0 ? name.slice(idx + 1).toLowerCase() : ''
}

/**
 * 把「保存到文件夹」前缀与文件相对路径拼成上传用 filename。
 * 空前缀 → 原样；两侧斜杠归一，避免 `制度//a.md`。
 */
export function joinUploadDest(destFolder: string | undefined | null, fileRel: string): string {
  const dest = String(destFolder ?? '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '')
  const rel = String(fileRel ?? '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\/+/, '')
  if (!rel) return dest
  return dest ? `${dest}/${rel}` : rel
}

export function isAllowedFile(name: string): boolean {
  return (UPLOAD_ACCEPT_EXTS as readonly string[]).includes(fileExtension(name))
}

/**
 * 上传前置校验（§4.4 边界：类型/大小在**本地**先拦，别等后端 422/413）。
 * 返回给用户看的原因；通过则返回 null。
 */
export function rejectReason(file: { name: string; size: number }): string | null {
  if (file.size <= 0) return '空文件（0 字节）不上传'
  if (!isAllowedFile(file.name)) {
    const ext = fileExtension(file.name)
    return `不支持 .${ext || '(无后缀)'} 类型，允许的类型：${UPLOAD_ACCEPT_EXTS.join(' / ').toUpperCase()}`
  }
  if (file.size > MAX_FILE_BYTES) {
    return `超出单文件限制 ${MAX_FILE_SIZE_MB} MB（当前 ${(file.size / 1024 / 1024).toFixed(1)} MB），请拆分后重传`
  }
  return null
}

/**
 * 浏览器端 SHA-256（秒传用，§6.5）。
 *
 * `crypto.subtle` **只在 https 或 localhost 可用**：内网 http 访问时它是 undefined。
 * 这时返回 null（调用方按"不算摘要"继续上传），绝不抛错、更不阻断上传 ——
 * 与 `ekbs/api/admin_ui.py` 里旧管理台的降级行为一致。
 *
 * 注：方案 §6.5 建议用 `hash-wasm` 做增量摘要以省内存；该依赖未安装，
 * 这里按任务要求用 `crypto.subtle.digest`（单文件 ≤ 100 MB，一次性读入可接受）。
 */
export async function sha256Hex(file: File): Promise<string | null> {
  try {
    const subtle =
      typeof crypto !== 'undefined'
        ? (crypto as Crypto & { subtle?: SubtleCrypto }).subtle
        : undefined
    if (!subtle || typeof subtle.digest !== 'function') return null
    const buffer = await file.arrayBuffer()
    const digest = await subtle.digest('SHA-256', buffer)
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
  } catch {
    // http 环境、隐私模式、或文件读取失败：降级为"没有摘要"
    return null
  }
}

/* ------------------------------------------------------------------ *
 * 上传目标 / 分片
 * ------------------------------------------------------------------ */

export interface KbCandidate {
  kb_id: number
  kb_code?: string | null
}

/**
 * 解析「目标知识库」输入。
 *
 * ⚠️ **本函数当前没有生产调用方**（只有本文件的单测引用它）。它诞生于
 * `GET /kb/knowledge-bases` 还不存在、界面拿不到全量知识库清单的时候；
 * 该端点已补齐（`src/api/doc.ts::listKnowledgeBases`），上传页改成真正的下拉选择，
 * 于是"从已见文档猜候选 + 允许手填"这条路整体作废。留着它是为了不删测试覆盖，
 * 但如果继续这样挂着，它那句"后端没有这个端点"的旧结论会误导后来人 —— 已改掉。
 */
export function resolveKbTarget(
  input: string,
  options: KbCandidate[],
): { kb_id?: number; kb_code?: string } | null {
  const value = input.trim()
  if (!value) return null
  const hit = options.find((o) => o.kb_code != null && o.kb_code === value)
  if (hit) return { kb_id: hit.kb_id, kb_code: hit.kb_code ?? undefined }
  if (/^\d+$/.test(value)) return { kb_id: Number(value) }
  return { kb_code: value }
}

/** 知识库候选项：从文档列表里出现过的 kb 去重（无生产调用方，见 `resolveKbTarget`）。 */
export function kbOptionsFrom(items: DocumentItem[]): {
  value: string
  label: string
  kb_id: number
  kb_code?: string
}[] {
  const seen = new Map<number, { value: string; label: string; kb_id: number; kb_code?: string }>()
  for (const item of items) {
    if (item?.kb_id == null || seen.has(item.kb_id)) continue
    const code = item.kb_code ?? undefined
    seen.set(item.kb_id, {
      value: code ?? String(item.kb_id),
      label: code ? `${code}（kb_id=${item.kb_id}）` : `kb_id=${item.kb_id}`,
      kb_id: item.kb_id,
      kb_code: code,
    })
  }
  return [...seen.values()]
}

/** 可见部门输入：`tech, market` / `tech，market` → `['tech','market']`（去空、去重）。 */
export function parseDeptIds(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[,，;；\s]+/)
        .map((s) => s.trim())
        .filter((s) => s.length > 0),
    ),
  ]
}

/** 后端未给 `chunk_size_hint` 时的兜底分片大小（实测后端硬编码 5 MB）。 */
export const DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024

/** 分片规划：最后一片允许小于分片大小（§6.5）。size=0 视为 1 片，由 rejectReason 先拦掉。 */
export function planChunks(size: number, hint?: number | null): { chunkSize: number; total: number } {
  const chunkSize = hint != null && hint > 0 ? Math.floor(hint) : DEFAULT_CHUNK_SIZE
  const total = Math.max(1, Math.ceil(Math.max(0, size) / chunkSize))
  return { chunkSize, total }
}

/** 上传队列行的状态（对应界面上的 等待/摘要计算中/上传中/成功/失败/秒传）。 */
export type UploadStatus = 'pending' | 'hashing' | 'uploading' | 'success' | 'failed' | 'instant'

export interface UploadQueueLike {
  status: UploadStatus
  uploaded: number
  total: number
}

export function isTerminal(status: UploadStatus): boolean {
  return status === 'success' || status === 'failed' || status === 'instant'
}

/** 队列汇总（上传页底部"合计/成功/失败/进行中/等待"与"全部完成"判定都用它）。 */
export function summarizeQueue(items: UploadQueueLike[]): {
  total: number
  pending: number
  running: number
  success: number
  instant: number
  failed: number
  bytesTotal: number
  bytesUploaded: number
} {
  let pending = 0
  let running = 0
  let success = 0
  let instant = 0
  let failed = 0
  let bytesTotal = 0
  let bytesUploaded = 0
  for (const item of items) {
    bytesTotal += item.total
    bytesUploaded += item.status === 'instant' || item.status === 'success' ? item.total : item.uploaded
    if (item.status === 'pending') pending += 1
    else if (item.status === 'hashing' || item.status === 'uploading') running += 1
    else if (item.status === 'success') success += 1
    else if (item.status === 'instant') instant += 1
    else failed += 1
  }
  return { total: items.length, pending, running, success, instant, failed, bytesTotal, bytesUploaded }
}

/** 上传队列行需要一个稳定 key；`crypto.randomUUID` 在 http/老浏览器可能没有。 */
let uidSeq = 0
export function newUid(): string {
  try {
    const c = typeof crypto !== 'undefined' ? (crypto as Crypto) : undefined
    if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  } catch {
    /* 降级到计数器 */
  }
  uidSeq += 1
  return `up-${Date.now()}-${uidSeq}`
}

/* ------------------------------------------------------------------ *
 * 批量操作（§5.5 L-B / §5.6）
 * ------------------------------------------------------------------ */

export interface BatchOutcome {
  succeeded: number
  failed: { doc_id: number; error: string }[]
}

/**
 * 批量结果归一化。
 * ⚠️ **契约与后端不一致**：`src/api/doc.ts::batchDocuments` 声明的是
 * `{succeeded?: number[]; failed?: {doc_id, error}[]}`，而后端
 * （`ekbs/api/docs.py::batch_documents`）真实返回的是
 * `{action, succeeded: number, results: [{doc_id, ok, status?|error?|changes?}]}`。
 * 两种都认：优先按 `results` 逐条判定，退化为读 `succeeded`/`failed`。
 */
export function normalizeBatchResult(raw: unknown): BatchOutcome {
  const obj = (raw ?? {}) as Record<string, unknown>
  const failed: { doc_id: number; error: string }[] = []
  const pushFailed = (docId: unknown, error: unknown) => {
    failed.push({ doc_id: Number(docId), error: String(error ?? '未知错误') })
  }

  if (Array.isArray(obj.results)) {
    let ok = 0
    for (const row of obj.results as Record<string, unknown>[]) {
      if (row && row.ok === true) ok += 1
      else if (row) pushFailed(row.doc_id, row.error)
    }
    return { succeeded: ok, failed }
  }

  if (Array.isArray(obj.failed)) {
    for (const row of obj.failed as Record<string, unknown>[]) {
      if (row) pushFailed(row.doc_id, row.error)
    }
  }
  let succeeded = 0
  if (typeof obj.succeeded === 'number') succeeded = obj.succeeded
  else if (Array.isArray(obj.succeeded)) succeeded = obj.succeeded.length
  return { succeeded, failed }
}

/* ------------------------------------------------------------------ *
 * 密级：知识库默认值 vs 文档实际值
 * ------------------------------------------------------------------ */

export interface KbLevelSource {
  kb_id: number
  default_level?: number | null
}

/**
 * 选了知识库之后，上传页「本文档密级」的**初值**。
 *
 * 界面上有两个密级，它们不是一回事，这里就是两者唯一的衔接点：
 *
 * * 知识库的 `default_level`（新建知识库时填的「该库默认密级」）——
 *   只是这一批上传的初始值，省一次手选，**不决定任何一份文档的真实密级**；
 * * 文档的 `level`（上传页的「本文档密级」）—— 真实入库、进权限判定的那个，
 *   可以在上传页随意改，改了也**不影响知识库设置**。
 *
 * 知识库没给该字段（老库 / 值为空 / 越界）时返回 `undefined` = **不要改动**用户
 * 当前的选择 —— 返回 2 会凭空把用户选的 L1 顶掉，那是另一种 bug。
 */
export function initialLevelForKb(
  kbs: readonly KbLevelSource[],
  kbId?: number | null,
): number | undefined {
  if (kbId == null) return undefined
  const hit = kbs.find((k) => k.kb_id === kbId)
  const level = Number(hit?.default_level)
  return Number.isInteger(level) && level >= 1 && level <= 4 ? level : undefined
}

/** 密级中文名（ClearanceTag 未导出它的 META，这里只用于"影响清单"的文字摘要）。 */
export const LEVEL_NAMES: Record<string, string> = {
  L1: '公开',
  L2: '内部',
  L3: '秘密',
  L4: '机密',
}

export function levelLabel(level: number | string | null | undefined): string {
  const lv = normalizeLevel(level)
  if (!lv) return '未知'
  return `${lv} ${LEVEL_NAMES[lv]}`
}

export interface BatchImpact {
  count: number
  ids: number[]
  titles: string[]
  /** 按密级统计，供 L-B 影响清单展示（数字全部来自当前选中行，不写死）。 */
  byLevel: { level: string; count: number }[]
  /** 高于当前用户密级的份数（发布这类管理动作允许，但发布后自己检索不到）。 */
  aboveClearance: number
}

/** L-B 影响清单摘要（§5.5「影响面数字必须真实」）。 */
export function summarizeBatch(
  items: DocumentItem[],
  clearance?: number | string | null,
): BatchImpact {
  const counter = new Map<string, number>()
  let aboveClearance = 0
  for (const item of items) {
    const lv = normalizeLevel(item.level) ?? '未知'
    counter.set(lv, (counter.get(lv) ?? 0) + 1)
    if (!canAccessLevel(clearance, item.level)) aboveClearance += 1
  }
  return {
    count: items.length,
    ids: items.map((i) => i.doc_id),
    titles: items.map((i) => i.title),
    byLevel: [...counter.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([level, count]) => ({ level, count })),
    aboveClearance,
  }
}

/* ------------------------------------------------------------------ *
 * 列表 / 详情报文整形
 * ------------------------------------------------------------------ */

/**
 * `visible_dept_ids` 的真实形状是**数组**（后端 `json.loads` 过），
 * 而 `DocumentItem` 类型写的是 `string`。两种都吃，展示成顿号分隔。
 */
export function deptText(value: unknown): string {
  if (value == null) return '-'
  if (Array.isArray(value)) {
    const list = value.map((v) => String(v).trim()).filter(Boolean)
    return list.length ? list.join('、') : '不限部门'
  }
  const text = String(value).trim()
  if (!text) return '不限部门'
  if (text.startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(text)
      if (Array.isArray(parsed)) return deptText(parsed)
    } catch {
      /* 不是 JSON，按下面的普通字符串处理 */
    }
  }
  return text
}

/**
 * 文档详情的字段。
 * `GET /docs/documents/{doc_id}` 走的是 `SELECT d.*`，比共享类型 `DocumentItem`
 * 多出 mime / parse_path / checksum / created_at / created_by —— 这些是**真实存在**的列
 * （见 `sql/*.sql` 的 `ekbs_document`），因此在本地类型里补上，只展示、不臆造。
 */
export interface DocumentDetailFields extends DocumentItem {
  mime?: string | null
  parse_path?: string | null
  checksum?: string | null
  created_by?: number | null
  created_at?: string
}

/**
 * 详情响应整形。
 * ⚠️ **契约与后端不一致**：`src/api/doc.ts::getDocument` 声明为 `Promise<DocumentItem>`，
 * 但后端（`ekbs/api/admin.py::get_document`）真实返回
 * `{document, chunks, shards, citation_count}`。两种都认，避免任一口径变化即白屏。
 */
export function unwrapDocument(raw: unknown): DocumentDetailFields | null {
  if (!raw || typeof raw !== 'object') return null
  const obj = raw as Record<string, unknown>
  const inner = obj.document
  if (inner && typeof inner === 'object' && 'doc_id' in (inner as Record<string, unknown>)) {
    return inner as unknown as DocumentDetailFields
  }
  if ('doc_id' in obj) return obj as unknown as DocumentDetailFields
  return null
}

/** 详情响应里随 `document` 一起返回的统计（契约类型里没有，但后端真实提供）。 */
export interface DocumentDetailExtra {
  /** `{child: n, parent: n, table: n, ...}` */
  chunkTypeCounts: Record<string, number>
  shards: string[]
  citationCount: number | null
}

export function documentExtra(raw: unknown): DocumentDetailExtra {
  const empty: DocumentDetailExtra = { chunkTypeCounts: {}, shards: [], citationCount: null }
  if (!raw || typeof raw !== 'object') return empty
  const obj = raw as Record<string, unknown>
  const counts: Record<string, number> = {}
  if (obj.chunks && typeof obj.chunks === 'object' && !Array.isArray(obj.chunks)) {
    for (const [k, v] of Object.entries(obj.chunks as Record<string, unknown>)) {
      const n = Number(v)
      if (Number.isFinite(n)) counts[k] = n
    }
  }
  const shards = Array.isArray(obj.shards) ? obj.shards.map((s) => String(s)) : []
  const citations = Number(obj.citation_count)
  return {
    chunkTypeCounts: counts,
    shards,
    citationCount: Number.isFinite(citations) ? citations : null,
  }
}

/** 筛选是否有内容（§5.4：空库与"筛选无结果"是两套文案、两个动作）。 */
export function hasActiveFilter(params: { status?: unknown; level?: unknown; q?: unknown }): boolean {
  return Boolean(
    (params.status != null && params.status !== '') ||
      (params.level != null && params.level !== '') ||
      (params.q != null && String(params.q).trim() !== ''),
  )
}
