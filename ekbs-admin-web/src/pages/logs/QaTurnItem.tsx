/**
 * 会话详情里的**一轮问答**（05 §4.12「问答日志与审计」的对话详情）。
 *
 * 只渲染后端真实返回的列（`ekbs_qa_message` + `ekbs_qa_citation`，见 admin.py:150-168）：
 * 问题 / 改写后查询 / 答案 / 模型 / 拒答 / 分阶段耗时 / token / 候选数与最高分 /
 * `trace_id` / 引用（文档 id + 分块 id + 标题路径 + 页码 + 重排分 + ACL 与页码校验）。
 *
 * 方案 v3.0 说明：不部署 Langfuse，`trace_id` 与分阶段耗时列就是可观测性载体，
 * 所以这两项在详情里必须可见（不可省略）。
 */
import { Alert, Card, Descriptions, Divider, Flex, Space, Tag, Tooltip, Typography } from 'antd'
import { FileSearchOutlined, WarningOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import FigureThumb from '@/components/FigureThumb'
import { formatMs, formatTime } from '@/utils/format'
import { citationSourceOf, textOrDash, turnRefusedOf, type QaCitation, type QaTurn } from './qaLogHelpers'

/** 引用项：文档名（此处只有 doc_id，方案未要求回查标题）与分块 id。 */
function CitationItem({ citation }: { citation: QaCitation }) {
  const aclBad = citation.aclResult && citation.aclResult !== 'pass'
  const pageBad = citation.pageCheck && citation.pageCheck !== 'pass'
  // 上下文块（喂给 LLM 的 chunkId）可能是精排后扩展出来的父块/整表容器；
  // 有来源时显示 `#父块 ← #精排块`，让「这块从哪来的」一眼可见（旧数据无该列时保持原样）
  const sourceChunkId = citationSourceOf(citation)
  const chunkLabel = `#${textOrDash(citation.chunkId)}`
  const page =
    citation.pageFrom != null && citation.pageTo != null
      ? `p.${citation.pageFrom}-${citation.pageTo}`
      : citation.pageFrom != null
        ? `p.${citation.pageFrom}`
        : ''

  return (
    <Space size={8} wrap>
      <Tag color="blue" style={{ marginInlineEnd: 0 }}>
        {citation.citeIndex != null ? `[${citation.citeIndex}] ` : ''}doc
        {textOrDash(citation.docId)}
      </Tag>
      {sourceChunkId != null ? (
        <Tooltip title={`上下文块（由精排块 #${sourceChunkId} 扩展）`}>
          <Typography.Text type="secondary" style={{ cursor: 'help' }}>
            分块 id：
            <span className="ekbs-mono">{`${chunkLabel} ← #${sourceChunkId}`}</span>
          </Typography.Text>
        </Tooltip>
      ) : (
        <Typography.Text type="secondary">分块 id：{chunkLabel}</Typography.Text>
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
      {citation.rerankScore != null ? (
        <Typography.Text type="secondary">重排分 {citation.rerankScore.toFixed(3)}</Typography.Text>
      ) : null}
      {citation.figureId ? (
        // 原先这里只渲染一行文字「图 img-xxx」—— 用户的原话是"无法收到图片"。
        // 现在真的把图取回来显示（端点要鉴权，`<img src>` 带不了 header，见组件注释）。
        <span>
          <Typography.Text type="secondary">图 {citation.figureId}</Typography.Text>
          <div style={{ marginTop: 4 }}>
            <FigureThumb chunkId={citation.chunkId} title={`图 ${citation.figureId}`} />
          </div>
        </span>
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

  // 「模型上下文」行：答案实际引用到的块（citations 的 chunkId 集合）才标 [引用]，
  // 其余同样喂给模型的块标 [仅上下文]。顺序按后端存进去的顺序，重复 id 只显示一次。
  const citedChunkIds = new Set(
    turn.citations.map((citation) => citation.chunkId).filter((id): id is number => id != null),
  )
  const seenContextIds = new Set<number>()
  const contextChunkIds = turn.contextChunkIds.filter((id) => {
    if (seenContextIds.has(id)) return false
    seenContextIds.add(id)
    return true
  })

  return (
    <Card
      size="small"
      title={
        <Space size={8} wrap>
          <Typography.Text strong>第 {textOrDash(turn.turn)} 轮</Typography.Text>
          {/* 按**答案实际内容**判定拒答：历史记录里存在 refused=1 但答案正常的误判
              （旧版 missing_citation 硬拒），照搬标记会把正常回答标成"拒答" */}
          {turnRefusedOf(turn) ? (
            <Tag color="red" icon={<WarningOutlined />}>
              拒答
            </Tag>
          ) : (
            <Tag color="green">已作答</Tag>
          )}
          {turn.model ? <Tag>{turn.model}</Tag> : null}
        </Space>
      }
      extra={<Typography.Text type="secondary">{formatTime(turn.createdAt)}</Typography.Text>}
      style={{ marginBottom: 16 }}
    >
      <Descriptions column={1} size="small" colon={false}>
        <Descriptions.Item label="问题">
          {turn.question ? (
            <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
              {turn.question}
            </Typography.Paragraph>
          ) : (
            <Typography.Text type="secondary">该轮未记录问题文本</Typography.Text>
          )}
        </Descriptions.Item>
        {turn.rewrittenQuery && turn.rewrittenQuery !== turn.question ? (
          <Descriptions.Item label="改写后查询">
            <Typography.Text type="secondary">{turn.rewrittenQuery}</Typography.Text>
          </Descriptions.Item>
        ) : null}
        <Descriptions.Item label="答案">
          {turn.answer ? (
            <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
              {turn.answer}
            </Typography.Paragraph>
          ) : (
            <Typography.Text type="secondary">该轮无答案文本（拒答或未生成）</Typography.Text>
          )}
        </Descriptions.Item>
      </Descriptions>

      <Divider style={{ margin: '12px 0' }} />

      {/* 方案 v3.0：不部署 Langfuse，trace_id + 分阶段耗时承担可观测性 —— 必须展示 */}
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
        {turn.candidateCount != null ? (
          <Typography.Text type="secondary">候选 {turn.candidateCount}</Typography.Text>
        ) : null}
        {turn.rerankTopScore != null ? (
          <Typography.Text type="secondary">
            最高重排分 {turn.rerankTopScore.toFixed(3)}
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

      <Divider style={{ margin: '12px 0' }} />

      <Typography.Text strong>引用（{turn.citations.length}）</Typography.Text>
      {turn.citations.length ? (
        <Flex vertical gap={8} style={{ marginTop: 8 }}>
          {turn.citations.map((c, idx) => (
            <CitationItem key={`${c.citationId ?? c.chunkId ?? idx}-${c.sourceChunkId ?? ''}`} citation={c} />
          ))}
        </Flex>
      ) : (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary">
            该轮没有引用（拒答或未检索到可引用分块）
          </Typography.Text>
        </div>
      )}

      {/*
        引用 ≠ 上下文：只有答案里标了 [n] 的块才进引用/审计，其余块同样进了 prompt。
        实测 doc 205 的这一轮 prompt 有 3 块（35280/35274/35281），引用只有前 2 块，
        用户因此以为 35281「根本没推给模型」—— 这一行就是用来消除这个误解的。
        历史行没有 context_chunk_ids（解析为 []），整行不渲染，旧页面行为不变。
      */}
      {contextChunkIds.length ? (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary">
            <span>模型上下文（{contextChunkIds.length} 块）：</span>
            {contextChunkIds.map((chunkId) => {
              const cited = citedChunkIds.has(chunkId)
              return (
                <span
                  key={chunkId}
                  className="ekbs-mono"
                  style={{ marginLeft: 8, color: cited ? '#389e0d' : undefined }}
                >
                  #{chunkId}[{cited ? '引用' : '仅上下文'}]
                </span>
              )
            })}
          </Typography.Text>
        </div>
      ) : null}
    </Card>
  )
}
