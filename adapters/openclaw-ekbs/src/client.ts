/**
 * EKBS HTTP 客户端（与 Python adapters/ekbs_tool 语义对齐）。
 * 不依赖 OpenClaw，可单独 smoke / unit test。
 */

export type EkbsCitation = {
  cite_index?: number
  chunk_id?: number
  doc_id?: number
  doc_title?: string
  page_from?: number | null
}

export type EkbsAskResult = {
  answer: string
  refused: boolean
  refusal_reason?: string | null
  citations: EkbsCitation[]
  model?: string | null
  trace_id?: string | null
  suggested_questions: string[]
  raw: Record<string, unknown>
}

export type EkbsClientConfig = {
  baseUrl?: string
  internalToken?: string
  bearerToken?: string
  userId?: number
  channel?: string
  timeoutMs?: number
}

export function formatToolText(result: EkbsAskResult): string {
  if (result.refused) {
    const reason = result.refusal_reason || "unknown"
    return `[EKBS 拒答 reason=${reason}]\n${result.answer}`.trim()
  }
  const cites = result.citations.map((c) => {
    const title = c.doc_title || c.doc_id || "?"
    const page = c.page_from != null ? ` p.${c.page_from}` : ""
    return `[${c.cite_index ?? "?"}] ${title}${page}`
  })
  const footer = cites.length ? `\n引用：${cites.join("；")}` : ""
  return `${result.answer}${footer}`.trim()
}

export function configFromEnv(overrides: EkbsClientConfig = {}): Required<
  Pick<EkbsClientConfig, "baseUrl" | "channel" | "timeoutMs">
> &
  EkbsClientConfig {
  const userRaw = process.env.EKBS_TOOL_USER_ID || process.env.EKBS_INTERNAL_USER_ID
  return {
    baseUrl: (overrides.baseUrl || process.env.EKBS_API_BASE || "http://127.0.0.1:8000/api/v1").replace(
      /\/$/,
      "",
    ),
    internalToken: overrides.internalToken ?? process.env.EKBS_INTERNAL_TOKEN,
    bearerToken: overrides.bearerToken ?? process.env.EKBS_BEARER_TOKEN,
    userId:
      overrides.userId ??
      (userRaw && String(userRaw).trim() ? Number(userRaw) : undefined),
    channel: overrides.channel || process.env.EKBS_TOOL_CHANNEL || "openclaw",
    timeoutMs: overrides.timeoutMs ?? Number(process.env.EKBS_TOOL_TIMEOUT_MS || 120_000),
  }
}

export async function ekbsAsk(
  question: string,
  config: EkbsClientConfig = {},
  opts: { conversationId?: string; mode?: string; signal?: AbortSignal } = {},
): Promise<EkbsAskResult> {
  const cfg = configFromEnv(config)
  const q = String(question || "").trim()
  if (!q) throw new Error("question 不能为空")
  if (!cfg.internalToken && !cfg.bearerToken) {
    throw new Error("未配置鉴权：设 internalToken / EKBS_INTERNAL_TOKEN 或 bearerToken")
  }
  if (cfg.internalToken && (cfg.userId == null || Number.isNaN(Number(cfg.userId)))) {
    throw new Error("内部回环鉴权需要 userId（插件配置或 EKBS_TOOL_USER_ID）")
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Internal-Channel": cfg.channel || "openclaw",
  }
  if (cfg.bearerToken) headers.Authorization = `Bearer ${cfg.bearerToken}`
  if (cfg.internalToken) {
    headers["X-Internal-Token"] = cfg.internalToken
    headers["X-Internal-User-Id"] = String(cfg.userId)
  }

  const body: Record<string, unknown> = {
    question: q,
    mode: opts.mode || "auto",
    use_cache: true,
  }
  if (opts.conversationId) body.conversation_id = opts.conversationId

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), cfg.timeoutMs || 120_000)
  if (opts.signal) {
    opts.signal.addEventListener("abort", () => ctrl.abort(), { once: true })
  }
  try {
    const res = await fetch(`${cfg.baseUrl}/qa/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      throw new Error(`EKBS HTTP ${res.status}: ${text.slice(0, 200)}`)
    }
    const data = (await res.json()) as Record<string, unknown>
    return {
      answer: String(data.answer || ""),
      refused: Boolean(data.refused),
      refusal_reason: (data.refusal_reason as string | null | undefined) ?? null,
      citations: Array.isArray(data.citations) ? (data.citations as EkbsCitation[]) : [],
      model: (data.model as string | null | undefined) ?? null,
      trace_id: (data.trace_id as string | null | undefined) ?? null,
      suggested_questions: Array.isArray(data.suggested_questions)
        ? data.suggested_questions.map(String)
        : [],
      raw: data,
    }
  } finally {
    clearTimeout(timer)
  }
}
