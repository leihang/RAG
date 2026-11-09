/**
 * 会话详情里的**一轮问答**（05 §4.12「问答日志与审计」的对话详情）。
 *
 * 全过程按条目折叠：用户问题 → 提示词改写 → Rerank/引用块 → 输出回答。
 * 数据来自 `ekbs_qa_message` + `ekbs_qa_citation`（审计不存完整精排候选，
 * ③ 用引用 + 模型上下文块 id 还原可查条目）。
 */
import { Alert, Card, Collapse, Divider, Flex, Space, Tag, Tooltip, Typography } from 'antd'
import { FileSearchOutlined, WarningOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import AnswerMarkdown from '@/components/AnswerMarkdown'
import FigureThumb from '@/components/FigureThumb'
import { formatMs, formatTime } from '@/utils/format'
import { citationSourceOf, textOrDash, turnRefusedOf, type QaCitation, type QaTurn } from './qaLogHelpers'

function CitationBody({ citation }: { citation: QaCitation }) {
  const aclBad = citation.aclResult && citation.aclResult !== 'pass'
  const pageBad = citation.pageCheck && citation.pageCheck !== 'pass'
  const sourceChunkId = citationSourceOf(citation)
  const page =
    citation.pageFrom != null && citation.pageTo != null
      ? `p.${citation.pageFrom}-${citation.pageTo}`
      : citation.pageFrom != null
        ? `p.${citation.pageFrom}`
        : ''

  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Space size={8} wrap>
        {sourceChunkId != null ? (
          <Tooltip title={`上下文块（由精排块 #${sourceChunkId} 扩展）`}>
            <Typography.Text type="secondary" style={{ cursor: 'help' }}>
              分块 id：
              <span className="ekbs-mono">{`#${textOrDash(citation.chunkId)} ← #${sourceChunkId}`}</span>
            </Typography.Text>
          </Tooltip>
        ) : (
          <Typography.Text type="secondary">
            分块 id：<span className="ekbs-mono">#{textOrDash(citation.chunkId)}</span>
          </Typography.Text>
        )}
        {citation.docVersion ? (
          <Typography.Text type="secondary">版本 {citation.docVersion}</Typography.Text>
        ) : null}
        {page ? <Typography.Text type="secondary">{page}</Typography.Text> : null}
        {citation.headingPath ? (
          <Typography.Text type="secondary" ellipsis={{ tooltip: citation.headingPath }}>
            {citation.headingPath}
          </Typography.Text>
        ) : null}
        {aclBad ? (
          <Tooltip title="该引用在生成时未通过 ACL 校验，可能已被回收或越权">
            <Tag color="red" icon={<WarningOutlined />}>
              ACL {citation.aclResult}
            </Tag>
          </Tooltip>
        ) : null}
        {pageBad ? (
          <Tooltip title="引用页码超出文档页数范围">
            <Tag color="orange" icon={<WarningOutlined />}>
              页码 {citation.pageCheck}
            </Tag>
          </Tooltip>
        ) : null}
        {citation.docId != null ? (
          <Link to={`/docs/${citation.docId}`}>
            <FileSearchOutlined /> 查看文档
          </Link>
        ) : null}
      </Space>
      {citation.figureId ? (
        <div>
          <Typography.Text type="secondary">图 {citation.figureId}</Typography.Text>
          <div style={{ marginTop: 4 }}>
            <FigureThumb chunkId={citation.chunkId} title={`图 ${citation.figureId}`} />
          </div>
        </div>
      ) : null}
    </Space>
  )
}

export interface QaTurnItemProps {
  turn: QaTurn
}

export default function QaTurnItem({ turn }: QaTurnItemProps) {
  const { timing } = turn
  const hasTiming =
    timing.ttft_ms != null ||
    timing.total_ms != null ||
    timing.retrieve_ms != null ||
    timing.rerank_ms != null ||
    timing.generate_ms != null

  const citedChunkIds = new Set(
    turn.citations.map((citation) => citation.chunkId).filter((id): id is number => id != null),
  )
  const seenContextIds = new Set<number>()
  const contextChunkIds = turn.contextChunkIds.filter((id) => {
    if (seenContextIds.has(id)) return false
    seenContextIds.add(id)
    return true
  })
  const contextOnlyIds = contextChunkIds.filter((id) => !citedChunkIds.has(id))
  const rewritten = (turn.rewrittenQuery || '').trim()
  const question = (turn.question || '').trim()
  const rewriteChanged = Boolean(rewritten && rewritten !== question)
  const refused = turnRefusedOf(turn)

  const flowItems = [
    {
      key: 'question',
      label: (
        <Space size={8} wrap>
          <span>① 用户问题</span>
          {question ? (
            <Typography.Text type="secondary" ellipsis style={{ maxWidth: 420 }}>
              {question}
            </Typography.Text>
          ) : null}
        </Space>
      ),
      children: question ? (
        <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
          {question}
        </Typography.Paragraph>
      ) : (
        <Typography.Text type="secondary">该轮未记录问题文本</Typography.Text>
      ),
    },
    {
      key: 'rewrite',
      label: (
        <Space size={8} wrap>
          <span>② 提示词改写</span>
          {rewriteChanged ? (
            <Typography.Text type="secondary" ellipsis style={{ maxWidth: 360 }}>
              {rewritten}
            </Typography.Text>
          ) : (
            <Tag>未改写 / 同原文</Tag>
          )}
        </Space>
      ),
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <div>
            <Typography.Text type="secondary">原始问题</Typography.Text>
            <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
              {question || '—'}
            </Typography.Paragraph>
          </div>
          <div>
            <Typography.Text type="secondary">检索用改写</Typography.Text>
            <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
              {rewritten || question || '—'}
            </Typography.Paragraph>
          </div>
        </Space>
      ),
    },
    {
      key: 'rerank',
      label: (
        <Space size={8} wrap>
          <span>③ Rerank / 引用块</span>
          <Tag color="cyan">引用 {turn.citations.length}</Tag>
          {contextChunkIds.length ? (
            <Tag>上下文 {contextChunkIds.length}</Tag>
          ) : null}
          {turn.rerankTopScore != null ? (
            <Typography.Text type="secondary">
              最高分 {turn.rerankTopScore.toFixed(3)}
            </Typography.Text>
          ) : null}
          {turn.candidateCount != null ? (
            <Typography.Text type="secondary">候选 {turn.candidateCount}</Typography.Text>
          ) : null}
        </Space>
      ),
      children: (
        <div>
          {turn.citations.length ? (
            <Collapse
              size="small"
              defaultActiveKey={turn.citations.map(
                (c, idx) => `${c.citationId ?? c.chunkId ?? idx}-${c.sourceChunkId ?? ''}`,
              )}
              items={turn.citations.map((c, idx) => {
                const sourceId = citationSourceOf(c)
                const chunkLabel =
                  sourceId != null
                    ? `#${textOrDash(c.chunkId)} ← #${sourceId}`
                    : `#${textOrDash(c.chunkId)}`
                return {
                  key: `${c.citationId ?? c.chunkId ?? idx}-${c.sourceChunkId ?? ''}`,
                  label: (
                    <Space size={8} wrap>
                      <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                        {c.citeIndex != null ? `[${c.citeIndex}] ` : ''}doc{textOrDash(c.docId)}
                      </Tag>
                      <span className="ekbs-mono">{chunkLabel}</span>
                      {c.rerankScore != null ? (
                        <Typography.Text type="secondary">
                          重排分 {c.rerankScore.toFixed(3)}
                        </Typography.Text>
                      ) : null}
                      {c.headingPath ? (
                        <Typography.Text type="secondary" ellipsis style={{ maxWidth: 240 }}>
                          {c.headingPath}
                        </Typography.Text>
                      ) : null}
                    </Space>
                  ),
                  children: <CitationBody citation={c} />,
                }
              })}
            />
          ) : (
            <Typography.Text type="secondary">
              该轮没有引用（拒答或未检索到可引用分块）
            </Typography.Text>
          )}
          {contextOnlyIds.length ? (
            <div style={{ marginTop: 12 }}>
              <Typography.Text type="secondary">
                仅进模型上下文、未写入引用（{contextOnlyIds.length}）：
              </Typography.Text>
              <div style={{ marginTop: 6 }}>
                {contextOnlyIds.map((chunkId) => (
                  <Tag key={chunkId} className="ekbs-mono">
                    #{chunkId}
                  </Tag>
                ))}
              </div>
            </div>
          ) : null}
          {!turn.citations.length && !contextChunkIds.length ? null : contextChunkIds.length && !contextOnlyIds.length ? (
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              模型上下文 {contextChunkIds.length} 块均已出现在上方引用中。
            </Typography.Paragraph>
          ) : null}
        </div>
      ),
    },
    {
      key: 'answer',
      label: (
        <Space size={8} wrap>
          <span>④ 输出回答</span>
          {refused ? (
            <Tag color="red" icon={<WarningOutlined />}>拒答</Tag>
          ) : (
            <Tag color="green">已作答</Tag>
          )}
          {turn.model ? <Tag>{turn.model}</Tag> : null}
        </Space>
      ),
      children: turn.answer ? (
        <AnswerMarkdown text={turn.answer} style={{ marginBottom: 0 }} />
      ) : (
        <Typography.Text type="secondary">该轮无答案文本（拒答或未生成）</Typography.Text>
      ),
    },
  ]

  return (
    <Card
      size="small"
      title={
        <Space size={8} wrap>
          <Typography.Text strong>第 {textOrDash(turn.turn)} 轮</Typography.Text>
          {refused ? (
            <Tag color="red" icon={<WarningOutlined />}>拒答</Tag>
          ) : (
            <Tag color="green">已作答</Tag>
          )}
          {turn.model ? <Tag>{turn.model}</Tag> : null}
        </Space>
      }
      extra={<Typography.Text type="secondary">{formatTime(turn.createdAt)}</Typography.Text>}
      style={{ marginBottom: 16 }}
    >
      <Collapse defaultActiveKey={['question', 'rewrite', 'rerank', 'answer']} items={flowItems} />

      <Divider style={{ margin: '12px 0' }} />

      <Flex wrap gap={16}>
        <Typography.Text type="secondary">
          trace_id：
          {turn.traceId ? (
            <Typography.Text copyable code>
              {turn.traceId}
            </Typography.Text>
          ) : (
            ' -'
          )}
        </Typography.Text>
        <Typography.Text type="secondary">TTFT {formatMs(timing.ttft_ms)}</Typography.Text>
        <Typography.Text type="secondary">总耗时 {formatMs(timing.total_ms)}</Typography.Text>
        <Typography.Text type="secondary">检索 {formatMs(timing.retrieve_ms)}</Typography.Text>
        <Typography.Text type="secondary">重排 {formatMs(timing.rerank_ms)}</Typography.Text>
        <Typography.Text type="secondary">生成 {formatMs(timing.generate_ms)}</Typography.Text>
        {turn.promptTokens != null || turn.completionTokens != null ? (
          <Typography.Text type="secondary">
            token {textOrDash(turn.promptTokens)} / {textOrDash(turn.completionTokens)}
          </Typography.Text>
        ) : null}
      </Flex>
      {!hasTiming ? (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message="该轮没有分阶段耗时记录"
          description="ekbs_qa_message 的 ttft_ms / total_ms / retrieve_ms / rerank_ms / generate_ms 均为空，可能来自早期版本或纯检索答复。"
        />
      ) : null}
    </Card>
  )
}
