/**
 * 「问答日志与审计」纯函数测试。
 *
 * 重点覆盖**与后端实测口径相关的边界**：拒答无法判定时不许瞎猜、级别 high/medium 与
 * 设计稿 P1/P2 两套取值都要上对色、result 的 ok/denied 与 allow/deny 两种写法都要认。
 */
import { describe, expect, it } from 'vitest'
import {
  accessResultOfDoc,
  actionLabel,
  buildQaTurns,
  citationOfDoc,
  eventTypeLabel,
  matchDocAccessResult,
  matchQaKeyword,
  matchSecurityEvent,
  parseDetail,
  questionStateOf,
  refusedStateOf,
  sessionDetailErrorText,
  sessionMetaOf,
  severityOfDoc,
  textOrDash,
  toBool,
} from './qaLogHelpers'

describe('textOrDash', () => {
  it('空值统一显示 -', () => {
    expect(textOrDash(null)).toBe('-')
    expect(textOrDash(undefined)).toBe('-')
    expect(textOrDash('')).toBe('-')
    expect(textOrDash('   ')).toBe('-')
  })

  it('保留 0 这类合法值', () => {
    expect(textOrDash(0)).toBe('0')
  })
})

describe('toBool', () => {
  it('把 SQLite 的 0/1 归一成布尔', () => {
    expect(toBool(1)).toBe(true)
    expect(toBool(0)).toBe(false)
    expect(toBool(true)).toBe(true)
    expect(toBool('true')).toBe(true)
    expect(toBool(null)).toBe(false)
  })
})

describe('问答会话列表的缺字段处理', () => {
  it('拒答列在列表接口下必须是「无法判定」，不能编造', () => {
    // ekbs_qa_session 没有 refused 列，GET /logs/qa/sessions 也不返回消息
    expect(refusedStateOf({ session_id: 's1' })).toBe('unknown')
  })

  it('问题列同样标注为接口未提供', () => {
    expect(questionStateOf({ session_id: 's1' })).toBe('unknown')
  })

  it('关键字过滤：后端不返回 question/answer，故与任何关键字都不匹配', () => {
    const row = { session_id: 's1' }
    expect(matchQaKeyword(row, '报销')).toBe(false)
    // 空关键字不过滤
    expect(matchQaKeyword(row, '   ')).toBe(true)
  })

  it('关键字过滤：后端补齐 question/answer 后即可命中（大小写不敏感）', () => {
    const row = { session_id: 's1', question: 'VPN 怎么申请', answer: '见 IT 手册' }
    expect(matchQaKeyword(row, 'vpn')).toBe(true)
    expect(matchQaKeyword(row, 'it 手册')).toBe(true)
    expect(matchQaKeyword(row, '无关词')).toBe(false)
  })
})

describe('severityOfDoc（后端实际写 high/medium/low，告警表用 P1/P2）', () => {
  it('high 与 P1 都是红色（error）', () => {
    expect(severityOfDoc('high').tone).toBe('error')
    expect(severityOfDoc('P1').tone).toBe('error')
    expect(severityOfDoc('严重').tone).toBe('error')
  })

  it('medium 与 P2 都是橙色（warning）', () => {
    expect(severityOfDoc('medium').tone).toBe('warning')
    expect(severityOfDoc('P2').tone).toBe('warning')
    expect(severityOfDoc('警告').tone).toBe('warning')
  })

  it('其他取值为默认色，且原样保留取值', () => {
    expect(severityOfDoc('low').tone).toBe('default')
    expect(severityOfDoc('').tone).toBe('default')
    expect(severityOfDoc(undefined).text).toBe('未知')
  })
})

describe('事件类型与详情', () => {
  it('已知事件类型给中文，未知类型原样显示', () => {
    expect(eventTypeLabel('access_denied')).toBe('越权访问拦截')
    expect(eventTypeLabel('some_new_type')).toBe('some_new_type')
    expect(eventTypeLabel(null)).toBe('-')
  })

  it('detail 是 JSON 字符串则美化，纯文本则原样', () => {
    expect(parseDetail('{"hits":["a"],"reason":"x"}').text).toContain('"hits"')
    expect(parseDetail('plain text').text).toBe('plain text')
    expect(parseDetail('').text).toBe('-')
  })

  it('级别与事件类型的本地过滤', () => {
    const row = { event_type: 'access_denied', severity: 'high' }
    expect(matchSecurityEvent(row, {})).toBe(true)
    expect(matchSecurityEvent(row, { eventType: 'access_denied' })).toBe(true)
    expect(matchSecurityEvent(row, { eventType: 'metric_alert' })).toBe(false)
    expect(matchSecurityEvent(row, { severity: 'high' })).toBe(true)
    expect(matchSecurityEvent(row, { severity: 'low' })).toBe(false)
  })
})

describe('数据访问日志的枚举', () => {
  it('action 给中文，未知动作原样显示', () => {
    expect(actionLabel('view')).toBe('查看')
    expect(actionLabel('cite_click')).toBe('引用跳转')
    expect(actionLabel('weird')).toBe('weird')
  })

  it('ok/allow 是成功色，denied/deny 是红色', () => {
    expect(accessResultOfDoc('ok').tone).toBe('success')
    expect(accessResultOfDoc('allow').tone).toBe('success')
    expect(accessResultOfDoc('denied').tone).toBe('error')
    expect(accessResultOfDoc('deny').tone).toBe('error')
    expect(accessResultOfDoc('not_found').tone).toBe('warning')
    expect(accessResultOfDoc('').tone).toBe('default')
  })

  it('结果过滤对大小写不敏感', () => {
    expect(matchDocAccessResult({ result: 'Denied' }, 'denied')).toBe(true)
    expect(matchDocAccessResult({ result: 'ok' }, 'denied')).toBe(false)
    expect(matchDocAccessResult({ result: 'ok' }, '')).toBe(true)
  })
})

describe('sessionMetaOf（ekbs_qa_session 的真实列）', () => {
  it('逐列读取，缺失即 undefined 而不是空串', () => {
    const meta = sessionMetaOf({
      session_id: 's-1',
      user_id: 7,
      dt_userid: 'ding-7',
      channel: 'dingtalk_robot',
      conversation_id: 'cid-1',
      perm_fingerprint: 'abcdef123456',
      clearance_at: 3,
      started_at: '2026-01-01 10:00:00',
      ended_at: null,
      message_count: 4,
    })
    expect(meta).toMatchObject({
      session_id: 's-1',
      user_id: 7,
      dt_userid: 'ding-7',
      channel: 'dingtalk_robot',
      perm_fingerprint: 'abcdef123456',
      clearance_at: 3,
      message_count: 4,
    })
    expect(meta.ended_at).toBeUndefined()
  })

  it('容忍空包体', () => {
    expect(sessionMetaOf(undefined).session_id).toBe('')
    expect(sessionMetaOf(null).user_id).toBeUndefined()
  })
})

describe('citationOfDoc（ekbs_qa_citation 的 SELECT c.* 列）', () => {
  it('读页码、标题路径、ACL 与页码校验结果', () => {
    const c = citationOfDoc({
      citation_id: 9,
      cite_index: 1,
      chunk_id: 1201,
      doc_id: 33,
      doc_version: 'v2',
      heading_path: '第 3 章 > 报销',
      page_from: 4,
      page_to: 5,
      rerank_score: 0.8123,
      acl_result: 'revoked',
      page_check: 'out_of_range',
      snippet_hash: 'x',
    })
    expect(c).toMatchObject({
      citationId: 9,
      citeIndex: 1,
      chunkId: 1201,
      docId: 33,
      headingPath: '第 3 章 > 报销',
      pageFrom: 4,
      pageTo: 5,
      aclResult: 'revoked',
      pageCheck: 'out_of_range',
    })
    expect(c.rerankScore).toBeCloseTo(0.8123)
  })
})

describe('buildQaTurns', () => {
  const messages = [
    {
      message_id: 2,
      turn: 1,
      role: 'assistant',
      question: '报销标准？',
      rewritten_query: '差旅报销标准',
      answer: '见制度第 3 章',
      model: 'deepseek-chat',
      prompt_tokens: 100,
      completion_tokens: 20,
      ttft_ms: 320,
      total_ms: 1800,
      retrieve_ms: 60,
      rerank_ms: 20,
      generate_ms: 1500,
      candidate_count: 12,
      rerank_top_score: 0.91,
      refused: 0,
      trace_id: 'trace-abc',
      created_at: '2026-01-01 10:00:01',
    },
    {
      message_id: 5,
      turn: 2,
      role: 'assistant',
      question: '那出差呢？',
      answer: '',
      refused: 1,
      trace_id: 'trace-def',
      created_at: '2026-01-01 10:01:00',
    },
  ]

  it('每轮一条 assistant 消息即一轮，按 turn 升序', () => {
    const turns = buildQaTurns(messages, [])
    expect(turns).toHaveLength(2)
    expect(turns[0].question).toBe('报销标准？')
    expect(turns[0].rewrittenQuery).toBe('差旅报销标准')
    expect(turns[0].timing.ttft_ms).toBe(320)
    expect(turns[0].promptTokens).toBe(100)
    expect(turns[0].traceId).toBe('trace-abc')
    expect(turns[1].refused).toBe(true)
  })

  it('跳过 role=user 的行，避免把一轮拆成两条', () => {
    const turns = buildQaTurns([...messages, { message_id: 6, turn: 3, role: 'user', question: '?' }], [])
    expect(turns).toHaveLength(2)
  })

  it('引用按 message_id 归到对应轮次，并按 cite_index 排序', () => {
    const turns = buildQaTurns(messages, [
      { message_id: 2, cite_index: 2, chunk_id: 22, doc_id: 9 },
      { message_id: 2, cite_index: 1, chunk_id: 21, doc_id: 9 },
      { message_id: 5, cite_index: 1, chunk_id: 31, doc_id: 10 },
    ])
    expect(turns[0].citations.map((c) => c.citeIndex)).toEqual([1, 2])
    expect(turns[1].citations).toHaveLength(1)
    expect(turns[1].citations[0].docId).toBe(10)
  })

  it('容忍空数据', () => {
    expect(buildQaTurns(undefined, undefined)).toEqual([])
    expect(buildQaTurns([], [])).toEqual([])
  })
})

describe('sessionDetailErrorText', () => {
  it('404 给明确中文（会话不存在，而不是笼统的加载失败）', () => {
    expect(sessionDetailErrorText(404, 'not_found')).toContain('没有找到该会话')
  })

  it('其他状态沿用请求层文案', () => {
    expect(sessionDetailErrorText(403, '无操作权限')).toBe('无操作权限')
    expect(sessionDetailErrorText(undefined, '')).toBe('加载失败')
  })
})
