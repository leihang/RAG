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
  /** 用户昵称（列表接口 JOIN ekbs_user 带出；缺则只显示数字 id） */
  user_nick?: string | null
  /** dingtalk（钉钉同步）/ local（管理台本地账号）/ seed（演示数据） */
  auth_source?: string | null
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
  /** 最新一轮是否拒答（1/0，按答案内容判定）；旧后端无此字段 */
  last_refused?: number
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
  /**
   * 该轮**最终喂给 LLM 的上下文块 id**（消息行 `context_chunk_ids` 的 JSON 字符串解析结果）。
   *
   * 与 `citations` 不是一回事：引用只含答案里标了 [n] 的块，而上下文还包含没被引用、
   * 但同样进了 prompt 的块。历史行（旧后端/旧数据）为 `[]`，界面据此不渲染该行。
   */
  contextChunkIds: number[]
}

/** `ekbs_qa_citation` 的真实列（sql/001 里该表 SELECT c.*）。 */
export interface QaCitation {
  citationId?: number
  citeIndex?: number
  /** 实际喂给 LLM 的上下文块 id（可能是父块/整表容器） */
  chunkId?: number
  /**
   * 上下文块由哪条精排检索单元扩展而来（admin.py 的 `c.*` 原样带出 `source_chunk_id`）；
   * 缺省 = 引用本身就是精排单元（旧数据没有这一列）。
   */
  sourceChunkId?: number | null
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

/**
 * 引用的「精排来源块」：上下文块是精排后才扩展出来的父块/整表容器时，返回来源块 id；
 * 引用本身即精排单元、或旧数据没有该列时返回 undefined（渲染保持原样，不编造来源）。
 */
export function citationSourceOf(citation: QaCitation): number | undefined {
  const source = citation.sourceChunkId
  if (source == null || source === citation.chunkId) return undefined
  return source
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

export type RefusedState = 'yes' | 'partial' | 'no' | 'unknown'

/**
 * 会话行上的拒答状态。
 *
 * 列表一行的「最新问题」与「是否拒答」是**同一轮**的语义，所以优先用列表接口给的
 * `last_refused`（最新一轮是否拒答）。实测：22 轮里第 9 轮因问题太模糊（「定义是什么」）
 * 被拒，最新一问「工程经理职责是什么」回答完全正常，整行却显示「部分拒答 1/22」，
 * 用户以为最新回答被拒。
 *
 * 旧后端没有 `last_refused` 时退回会话累计口径（`refused_count` / `turn_count`）：
 *
 * * `no`      —— 一轮都没拒答；
 * * `partial` —— 部分轮次拒答（界面显示 `部分拒答 3/18`）；
 * * `yes`     —— 全部轮次都拒答。
 */
export function refusedStateOf(row: QaSessionRow): RefusedState {
  if (typeof row.last_refused === 'number' && Number.isFinite(row.last_refused)) {
    return row.last_refused > 0 ? 'yes' : 'no'
  }
  const refused = row.refused_count
  if (typeof refused !== 'number' || !Number.isFinite(refused)) {
    return 'unknown'
  }
  if (refused <= 0) return 'no'
  const turns = typeof row.turn_count === 'number' && Number.isFinite(row.turn_count) ? row.turn_count : 0
  if (turns > 0 && refused >= turns) return 'yes'
  return 'partial'
}

export const REFUSED_META: Record<
  RefusedState,
  { text: string; tone: 'error' | 'warning' | 'success' | 'default' }
> = {
  yes: { text: '全部拒答', tone: 'error' },
  partial: { text: '部分拒答', tone: 'warning' },
  no: { text: '已作答', tone: 'success' },
  unknown: { text: '接口未提供', tone: 'default' },
}

/** 拒答列文案：最新一轮口径只答「拒答/已作答」；旧后端累计口径下部分拒答附比例。 */
export function refusedLabelOf(row: QaSessionRow): string {
  if (typeof row.last_refused === 'number' && Number.isFinite(row.last_refused)) {
    return row.last_refused > 0 ? '拒答' : '已作答'
  }
  const state = refusedStateOf(row)
  if (state === 'partial') {
    return `部分拒答 ${row.refused_count ?? 0}/${row.turn_count ?? 0}`
  }
  return REFUSED_META[state].text
}

/** 拒答列 tooltip：说明本会话累计情况（最新一轮之外的历史拒答也能看到）。 */
export function refusedHintOf(row: QaSessionRow): string {
  const turns = typeof row.turn_count === 'number' && Number.isFinite(row.turn_count) ? row.turn_count : null
  const refused = typeof row.refused_count === 'number' && Number.isFinite(row.refused_count) ? row.refused_count : null
  if (turns == null || refused == null) {
    return '后端未返回会话轮次/拒答统计'
  }
  const latest = typeof row.last_refused === 'number' ? (row.last_refused > 0 ? '拒答' : '已作答') : ''
  return `最新一轮：${latest || '未知'}；本会话共 ${turns} 轮，累计拒答 ${refused} 轮`
}

/** 会话问题列：列表接口汇总的是**最新一轮**的问题原文（`last_question`）。 */
export function questionTextOf(row: QaSessionRow): string {
  const text = String(row.last_question ?? row.question ?? '').trim()
  return text
}

/**
 * 用户列展示：「昵称 #id」。
 *
 * 列表里只给数字 user_id 时无法区分来源 —— 钉钉同步用户（如雷航 #5）与管理台本地
 * 管理员（local:admin #365）都是普通自增 id；带上昵称后运维一眼能认。
 */
export function userLabelOf(row: QaSessionRow): string {
  const nick = String(row.user_nick ?? '').trim()
  const id = row.user_id == null ? '' : String(row.user_id)
  if (!nick) return id || '-'
  return `${nick} #${id}`
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

/**
 * 消息行的 `context_chunk_ids` → 上下文块 id 数组。
 *
 * 后端存的是 **JSON 字符串**（如 `"[35280,35274,35281]"`），历史行是 NULL；
 * 这里做防御解析：NULL / 空串 / 非法 JSON / 非数组 / 非法元素统统降级，
 * 解析失败返回 `[]`（界面据此不渲染「模型上下文」行，而不是炸掉整个详情页）。
 * 允许传入已解析的数组，兼容将来后端直接给 JSON 数组的写法。
 */
export function parseContextChunkIds(value: unknown): number[] {
  if (value == null || value === '') return []
  let parsed: unknown = value
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value)
    } catch {
      return []
    }
  }
  if (!Array.isArray(parsed)) return []
  const ids: number[] = []
  for (const item of parsed) {
    const id = num(item)
    // chunk id 是正整数；小数/负数/字符串都当脏数据丢弃
    if (id != null && Number.isInteger(id) && id > 0) ids.push(id)
  }
  return ids
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
    sourceChunkId: num(c.source_chunk_id),
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
      // 旧后端没有该列 → []（QaTurnItem 据此隐藏「模型上下文」行）
      contextChunkIds: parseContextChunkIds(m.context_chunk_ids),
    })
  }
  return turns.sort((a, b) => (a.turn ?? 0) - (b.turn ?? 0) || (a.messageId ?? 0) - (b.messageId ?? 0))
}

/** 详情页错误文案：404 是「会话不存在」，其余沿用请求层归一化后的中文。 */
export function sessionDetailErrorText(status: number | undefined, fallback: string): string {
  if (status === 404) return '没有找到该会话（会话可能已归档或 id 有误）'
  return fallback || '加载失败'
}

/**
 * 答案是不是「拒答话术」（而不是真的答了）。
 *
 * 后端拒答文案以这些前缀开头（`ekbs/retrieval/pipeline.py: refusal_answer_text` /
 * `NO_BASIS_MARKER`）。
 */
const REFUSAL_PREFIXES = ['未找到可靠依据', '对不起', '没有找到']

export function answerLooksRefused(answer: string | null | undefined): boolean {
  const text = String(answer ?? '').trim()
  if (!text) return true
  const head = text.replace(/^[*#>\-—・\s「『"'《]+/, '')
  return REFUSAL_PREFIXES.some((prefix) => head.startsWith(prefix))
}

/**
 * 某轮是否应显示为「拒答」——**以答案实际内容为准**，不盲信 `refused` 标记。
 *
 * 历史记录里有一批 `refused=1` 但答案正常的误判（旧版 `missing_citation` 硬拒，
 * 已在修复漏标引用后纠正）。详情页若照搬标记，会把正常回答标成"拒答"。
 */
export function turnRefusedOf(turn: { refused?: boolean; answer?: string }): boolean {
  if (!String(turn.answer ?? '').trim()) return Boolean(turn.refused)
  return answerLooksRefused(turn.answer)
}
