/**
 * 「问答日志与审计」模块的纯函数与本地 DTO（05 §4.12）。
 *
 * ⚠️ 为什么这里**不**直接用 `src/api/types.ts` 的 `QaSessionItem` / `SecurityEventItem` /
 * `DocAccessItem`：那三个接口是按设计稿字段写的，与 `ekbs/api/admin.py` 的**实际 SELECT**
 * 不一致（缺字段、字段改名、枚举取值不同）。按实测见下方每个类型与映射的注释。
 * 这些类型只在本模块内使用，待 `src/api/types.ts` 按实测校正后即可合并回去。
 */

/** `GET /logs/qa/sessions` 的**实际**行字段（admin.py 的 SELECT，逐字对应）。 */
export interface QaSessionRow {
  session_id: string
  /** 后端是 NOT NULL，但前端仍按「可能缺失」处理，避免缺字段时页面炸掉 */
  user_id?: number
  /** 钉钉 userid（可空） */
  dt_userid?: string | null
  /** dingtalk_robot | dingtalk_h5 | web | api */
  channel?: string
  conversation_id?: string | null
  /** 只回前 12 位（admin.py 截断） */
  perm_fingerprint?: string
  /** 会话建立时的密级快照 */
  clearance_at?: number
  started_at?: string
  ended_at?: string | null
  message_count?: number
  /** 本会话里拒答的轮数（admin.py 从 ekbs_qa_message 汇总） */
  refused_count?: number
  /** 本会话的 assistant 轮数 */
  turn_count?: number
  /** 最新一轮的问题原文 */
  last_question?: string | null
  /**
   * 兼容字段：问题/答案逐轮存在 `ekbs_qa_message`，列表接口汇总的是
   * `last_question`；这两个字段留给本地关键字过滤在后端补齐后自动生效。
   */
  question?: string
  answer?: string
}

/** `GET /logs/qa/sessions/{id}` 的实际包体：`{session, messages, citations}`（admin.py:171）。 */
export interface QaSessionRaw {
  session: Record<string, unknown>
  messages: Record<string, unknown>[]
  citations: Record<string, unknown>[]
}

/** 会话详情头部要展示的字段（全部来自 `ekbs_qa_session` 的真实列）。 */
export interface QaSessionMeta {
  session_id: string
  user_id?: number
  dt_userid?: string
  channel?: string
  conversation_id?: string
  perm_fingerprint?: string
  clearance_at?: number
  started_at?: string
  ended_at?: string
  message_count?: number
}

/** 一轮问答：assistant 消息 + 问题 + 该轮引用。 */
export interface QaTurn {
  messageId?: number
  turn?: number
  question: string
  rewrittenQuery: string
  answer: string
  model: string
  refused: boolean
  traceId: string
  createdAt?: string
  timing: {
    ttft_ms?: number
    total_ms?: number
    retrieve_ms?: number
    rerank_ms?: number
    generate_ms?: number
  }
  promptTokens?: number
  completionTokens?: number
  candidateCount?: number
  rerankTopScore?: number
  citations: QaCitation[]
}

/** `ekbs_qa_citation` 的真实列（sql/001 里该表 SELECT c.*）。 */
export interface QaCitation {
  citationId?: number
  citeIndex?: number
  chunkId?: number
  docId?: number
  docVersion?: string
  headingPath?: string
  pageFrom?: number
  pageTo?: number
  rerankScore?: number
  /** pass | revoked | denied */
  aclResult?: string
  /** pass | out_of_range */
  pageCheck?: string
  figureId?: string
  imagePath?: string
}

/** `GET /audit/security-events` 的实际行字段（admin.py:208-213 的 SELECT）。 */
export interface SecurityEventRow {
  event_id?: number
  event_type?: string
  user_id?: number | null
  /** 后端实际取值为 high | medium | low —— **不是**设计稿的 P1/P2（见 severityOfDoc） */
  severity?: string
  /** JSON 字符串（写侧 json.dumps），可能很长 */
  detail?: string
  created_at?: string
  _eventType?: string
  _severity?: string
}

/** `GET /logs/doc-access` 的实际行字段（admin.py:248-253 的 SELECT）。 */
export interface DocAccessRow {
  log_id?: number
  user_id?: number | null
  doc_id?: number | null
  /** view | download | cite_click | upload */
  action?: string
  channel?: string
  ip?: string
  ua?: string
  /** 后端实际写入的是 ok | denied（audit.py:157 的调用点）；设计稿写的是 allow | deny */
  result?: string
  created_at?: string
  _result?: string
}

// ---------------------------------------------------------------------------
// 通用
// ---------------------------------------------------------------------------

/** 分页选项（方案 05 §5：默认 20，可选 20/50/100）。 */
export const PAGE_SIZE_OPTIONS = ['20', '50', '100']

/** 列表首屏默认页大小。 */
export const DEFAULT_PAGE_SIZE = 20

/** 后端分页护栏（admin.py:42 的 `MAX_PAGE_SIZE = 200`）。 */
export const MAX_PAGE_SIZE = 200

/** 空值统一显示 `-`。 */
export function textOrDash(value: unknown): string {
  if (value == null) return '-'
  const text = String(value).trim()
  return text ? text : '-'
}

/** 把 0/1（SQLite 的 INTEGER 布尔）或布尔值归一成 boolean。 */
export function toBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return value === '1' || value.toLowerCase() === 'true'
  return false
}

// ---------------------------------------------------------------------------
// 问答会话列表
// ---------------------------------------------------------------------------

export type RefusedState = 'yes' | 'no' | 'unknown'

/**
 * 会话是否拒答。
 *
 * `ekbs_qa_session` 本身没有拒答列，但列表接口现在从 `ekbs_qa_message`
 * 汇总了 `refused_count`（本会话拒答轮数）；有值就按「>0 = 拒答过」判定，
 * 缺字段才回退 `unknown`（旧后端）。
 */
export function refusedStateOf(row: QaSessionRow): RefusedState {
  const count = row.refused_count
  if (typeof count === 'number' && Number.isFinite(count)) {
    return count > 0 ? 'yes' : 'no'
  }
  return 'unknown'
}

export const REFUSED_META: Record<RefusedState, { text: string; tone: 'error' | 'success' | 'default' }> = {
  yes: { text: '拒答', tone: 'error' },
  no: { text: '已作答', tone: 'success' },
  unknown: { text: '接口未提供', tone: 'default' },
}

/** 会话问题列：列表接口汇总的是**最新一轮**的问题原文（`last_question`）。 */
export function questionTextOf(row: QaSessionRow): string {
  const text = String(row.last_question ?? row.question ?? '').trim()
  return text
}

export const QUESTION_UNKNOWN_TEXT = '列表接口未返回，见对话详情'

/** 关键字在最新问题与答案两侧做大小写不敏感的包含匹配。 */
export function matchQaKeyword(row: QaSessionRow, keyword: string): boolean {
  const q = keyword.trim().toLowerCase()
  if (!q) return true
  const haystack = [row.last_question, row.question, row.answer].map((v) =>
    String(v ?? '').toLowerCase(),
  )
  return haystack.some((t) => t.includes(q))
}

/** 关键字过滤是在**本页返回的行**上做的：接口不支持 q，回看上面注释。 */
export const QA_KEYWORD_UNSUPPORTED_NOTE =
  '「是否拒答」筛选项后端接口不支持（列表只接受 user_id、channel、since 三个过滤参数），' +
  '但「问题」列已展示最新一轮问题、「是否拒答」列已按本会话拒答轮数汇总，可直接肉眼判断。' +
  '此处仅对**当前页已返回的行**做关键字本地过滤，不影响 total。'

// ---------------------------------------------------------------------------
// 安全事件
// ---------------------------------------------------------------------------

export type Tone = 'error' | 'warning' | 'default' | 'success' | 'processing'

/**
 * 级别 → 展示色与文案。
 *
 * 后端实际写的是 `high | medium | low`（qa.py / retrieval/pipeline.py / acl.py / alerts.py），
 * 告警阈值表用的是 `P1 | P2`（observability/alerts.py:351），入库时被压成 high/medium
 * （alerts.py:360）。两套都认：P1/严重/高 → 红，P2/警告/中 → 橙，其余 → 默认。
 */
export function severityOfDoc(severity?: string | null): { text: string; tone: Tone } {
  const raw = String(severity ?? '').trim()
  const key = raw.toUpperCase()
  if (key === 'P1' || key === 'HIGH' || raw === '严重' || raw === '高') {
    return { text: raw ? `${raw} 严重` : '严重', tone: 'error' }
  }
  if (key === 'P2' || key === 'MEDIUM' || raw === '警告' || raw === '中') {
    return { text: raw ? `${raw} 警告` : '警告', tone: 'warning' }
  }
  if (key === 'P3' || key === 'LOW' || raw === '低') {
    return { text: raw ? `${raw} 提示` : '提示', tone: 'default' }
  }
  return { text: raw || '未知', tone: 'default' }
}

/** 事件类型标签（写入侧的实际取值 + 表注释里的取值）。 */
export const SECURITY_EVENT_TYPES: { value: string; label: string }[] = [
  { value: 'access_denied', label: '越权访问拦截' },
  { value: 'injection_suspected', label: '疑似提示词注入' },
  { value: 'hallucinated_citation', label: '幻觉引用' },
  { value: 'content_blocked', label: '内容拦截' },
  { value: 'metric_alert', label: '指标告警' },
]

/** 级别选项：后端真实取值是 high/medium/low。 */
export const SECURITY_SEVERITIES: { value: string; label: string }[] = [
  { value: 'high', label: 'high 严重' },
  { value: 'medium', label: 'medium 警告' },
  { value: 'low', label: 'low 提示' },
]

const EVENT_TYPE_LABELS = new Map(SECURITY_EVENT_TYPES.map((o) => [o.value, o.label]))

/** 事件类型展示：认识的给中文，不认识的**原样显示**（别把未知值吞掉）。 */
export function eventTypeLabel(eventType?: string | null): string {
  const raw = String(eventType ?? '').trim()
  if (!raw) return '-'
  return EVENT_TYPE_LABELS.get(raw) ?? raw
}

/** `detail` 是 JSON 字符串；解析失败就原样展示（后端也可能写纯文本）。 */
export function parseDetail(detail?: string | null): { text: string; json: unknown | null } {
  const raw = String(detail ?? '').trim()
  if (!raw) return { text: '-', json: null }
  try {
    const json = JSON.parse(raw)
    return { text: JSON.stringify(json, null, 2), json }
  } catch {
    return { text: raw, json: null }
  }
}

/** 事件类型 / 级别的本地过滤（接口只支持 event_type，级别只能本地过滤）。 */
export function matchSecurityEvent(
  row: SecurityEventRow,
  filters: { eventType?: string; severity?: string },
): boolean {
  if (filters.eventType && String(row.event_type ?? '') !== filters.eventType) return false
  if (filters.severity && String(row.severity ?? '') !== filters.severity) return false
  return true
}

export const AUDIT_SEVERITY_UNSUPPORTED_NOTE =
  '「级别」筛选后端不支持（GET /audit/security-events 只接受 event_type 一个过滤参数），' +
  '此处仅对当前页已返回的行做本地过滤，不影响 total。'

// ---------------------------------------------------------------------------
// 数据访问日志
// ---------------------------------------------------------------------------

/** `ekbs_doc_access_log.action` 的实际取值（sql 表注释 + assets.py 调用点）。 */
export const DOC_ACCESS_ACTIONS: Record<string, string> = {
  view: '查看',
  download: '下载',
  cite_click: '引用跳转',
  upload: '上传',
}

export function actionLabel(action?: string | null): string {
  const raw = String(action ?? '').trim()
  if (!raw) return '-'
  return DOC_ACCESS_ACTIONS[raw] ?? raw
}

/**
 * 结果 → 色与文案。
 *
 * 后端实际写 `ok` / `denied`（admin.py 的 SELECT 列 + audit.py:157；`not_found` 在表注释里，
 * assets.py 目前只写 denied 与 ok）。设计稿把取值写成 `allow` / `deny`，两者都认。
 */
export function accessResultOfDoc(result?: string | null): { text: string; tone: Tone } {
  const raw = String(result ?? '').trim()
  const key = raw.toLowerCase()
  if (key === 'ok' || key === 'allow' || key === 'allowed' || key === 'pass') {
    return { text: `${raw} 允许`, tone: 'success' }
  }
  if (key === 'denied' || key === 'deny' || key === 'forbidden' || key === 'blocked') {
    return { text: `${raw} 拒绝`, tone: 'error' }
  }
  if (key === 'not_found') {
    return { text: `${raw} 不存在`, tone: 'warning' }
  }
  return { text: raw || '未知', tone: 'default' }
}

/** 结果选项：后端真实取值，另留设计稿的 allow/deny 以便历史数据也能筛。 */
export const DOC_ACCESS_RESULTS: { value: string; label: string }[] = [
  { value: 'ok', label: 'ok 允许' },
  { value: 'denied', label: 'denied 拒绝' },
  { value: 'not_found', label: 'not_found 对象不存在' },
  { value: 'allow', label: 'allow 允许（设计稿取值）' },
  { value: 'deny', label: 'deny 拒绝（设计稿取值）' },
]

/** 结果本地过滤（接口只支持 doc_id）。 */
export function matchDocAccessResult(row: DocAccessRow, result?: string): boolean {
  if (!result) return true
  return String(row.result ?? '').toLowerCase() === result.toLowerCase()
}

export const DOC_ACCESS_FILTER_NOTE =
  '「结果」筛选后端不支持（GET /logs/doc-access 只接受 doc_id 一个过滤参数），' +
  '此处仅对当前页已返回的行做本地过滤，不影响 total；「用户 ID」后端同样不支持，故未提供该筛选项。'

// ---------------------------------------------------------------------------
// 会话详情：原始包体 → 可渲染结构
// ---------------------------------------------------------------------------

function str(value: unknown): string {
  return value == null ? '' : String(value)
}

function num(value: unknown): number | undefined {
  if (value == null || value === '') return undefined
  const n = Number(value)
  return Number.isFinite(n) ? n : undefined
}

/** 会话头部字段（`ekbs_qa_session` 的真实列）。 */
export function sessionMetaOf(raw: unknown): QaSessionMeta {
  const s = (raw ?? {}) as Record<string, unknown>
  return {
    session_id: str(s.session_id),
    user_id: num(s.user_id),
    dt_userid: str(s.dt_userid) || undefined,
    channel: str(s.channel) || undefined,
    conversation_id: str(s.conversation_id) || undefined,
    perm_fingerprint: str(s.perm_fingerprint) || undefined,
    clearance_at: num(s.clearance_at),
    started_at: str(s.started_at) || undefined,
    ended_at: str(s.ended_at) || undefined,
    message_count: num(s.message_count),
  }
}

/** 引用行 → 视图对象（表是 `SELECT c.*`，字段名逐字对应）。 */
export function citationOfDoc(raw: unknown): QaCitation {
  const c = (raw ?? {}) as Record<string, unknown>
  return {
    citationId: num(c.citation_id),
    citeIndex: num(c.cite_index),
    chunkId: num(c.chunk_id),
    docId: num(c.doc_id),
    docVersion: str(c.doc_version) || undefined,
    headingPath: str(c.heading_path) || undefined,
    pageFrom: num(c.page_from),
    pageTo: num(c.page_to),
    rerankScore: num(c.rerank_score),
    aclResult: str(c.acl_result) || undefined,
    pageCheck: str(c.page_check) || undefined,
    figureId: str(c.figure_id) || undefined,
    imagePath: str(c.image_path) || undefined,
  }
}

/**
 * 消息 → 轮次。
 *
 * 后端每轮只写**一条 assistant 消息**（audit.py:117-131 的 role 常量是 'assistant'，
 * 同一行的 `question` 列即该轮问题），所以按 assistant 行聚合成「一轮」，
 * 而不是按 role 拆成问答两条。
 */
export function buildQaTurns(
  messages: unknown[] | undefined,
  citations: unknown[] | undefined,
): QaTurn[] {
  const byMessage = new Map<number, QaCitation[]>()
  for (const raw of citations ?? []) {
    const c = citationOfDoc(raw)
    const rawId = num(((raw ?? {}) as Record<string, unknown>).message_id)
    if (rawId == null) continue
    const list = byMessage.get(rawId) ?? []
    list.push(c)
    byMessage.set(rawId, list)
  }
  for (const list of byMessage.values()) {
    list.sort((a, b) => (a.citeIndex ?? 0) - (b.citeIndex ?? 0))
  }

  const turns: QaTurn[] = []
  for (const raw of messages ?? []) {
    const m = (raw ?? {}) as Record<string, unknown>
    if (str(m.role) === 'user') continue
    const messageId = num(m.message_id)
    turns.push({
      messageId,
      turn: num(m.turn),
      question: str(m.question),
      rewrittenQuery: str(m.rewritten_query),
      answer: str(m.answer),
      model: str(m.model),
      refused: toBool(m.refused),
      traceId: str(m.trace_id),
      createdAt: str(m.created_at) || undefined,
      timing: {
        ttft_ms: num(m.ttft_ms),
        total_ms: num(m.total_ms),
        retrieve_ms: num(m.retrieve_ms),
        rerank_ms: num(m.rerank_ms),
        generate_ms: num(m.generate_ms),
      },
      promptTokens: num(m.prompt_tokens),
      completionTokens: num(m.completion_tokens),
      candidateCount: num(m.candidate_count),
      rerankTopScore: num(m.rerank_top_score),
      citations: (messageId != null ? byMessage.get(messageId) : undefined) ?? [],
    })
  }
  return turns.sort((a, b) => (a.turn ?? 0) - (b.turn ?? 0) || (a.messageId ?? 0) - (b.messageId ?? 0))
}

/** 详情页错误文案：404 是「会话不存在」，其余沿用请求层归一化后的中文。 */
export function sessionDetailErrorText(status: number | undefined, fallback: string): string {
  if (status === 404) return '没有找到该会话（会话可能已归档或 id 有误）'
  return fallback || '加载失败'
}
