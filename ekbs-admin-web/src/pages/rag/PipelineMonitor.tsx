/**
 * 管理台「用户提问」全过程监控：按条目折叠查看
 * 用户问题 → 提示词改写 → Rerank 块 → 输出回答
 */
import { Collapse, Space, Tag, Typography } from 'antd'
import AnswerMarkdown from '@/components/AnswerMarkdown'
import { formatMs } from '@/utils/format'
import type { PipelineMonitorPayload, PipelineRerankChunk } from './playgroundData'

const DECISION_TAG: Record<string, { color: string; text: string }> = {
  kept: { color: 'success', text: '进扩展' },
  below_threshold: { color: 'error', text: '低于阈值' },
  over_keep: { color: 'default', text: '超 keep' },
  unknown: { color: 'default', text: '—' },
}

function RerankItem({
  row,
  onOpenChunk,
}: {
  row: PipelineRerankChunk
  onOpenChunk?: (chunkId: number) => void
}) {
  const decision = DECISION_TAG[row.decision] ?? DECISION_TAG.unknown
  const title = (
    <Space size={8} wrap>
      <Typography.Text strong>#{row.rank}</Typography.Text>
      {onOpenChunk ? (
        <Typography.Link onClick={(e) => { e.stopPropagation(); onOpenChunk(row.chunk_id) }} className="ekbs-mono">
          chunk {row.chunk_id}
        </Typography.Link>
      ) : (
        <Typography.Text className="ekbs-mono">chunk {row.chunk_id}</Typography.Text>
      )}
      <Tag color={decision.color}>{decision.text}</Tag>
      {row.rerank_score != null ? (
        <Typography.Text type="secondary">{row.rerank_score.toFixed(4)}</Typography.Text>
      ) : null}
      {row.doc_title ? (
        <Typography.Text type="secondary" ellipsis style={{ maxWidth: 280 }}>
          {row.doc_title}
        </Typography.Text>
      ) : null}
    </Space>
  )
  return (
    <Collapse
      size="small"
      style={{ marginBottom: 8 }}
      items={[
        {
          key: String(row.chunk_id),
          label: title,
          children: (
            <div>
              {row.heading_path ? (
                <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                  路径：{row.heading_path}
                  {row.chunk_type ? ` · ${row.chunk_type}` : ''}
                </Typography.Paragraph>
              ) : null}
              <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                {row.preview || '（无正文预览，可点击 chunk 打开）'}
              </Typography.Paragraph>
            </div>
          ),
        },
      ]}
    />
  )
}

export default function PipelineMonitor({
  monitor,
  onOpenChunk,
}: {
  monitor?: PipelineMonitorPayload | null
  onOpenChunk?: (chunkId: number) => void
}) {
  if (!monitor?.question && !monitor?.answer && !(monitor?.rerank_chunks?.length)) {
    return null
  }

  const rewrite = monitor.rewrite
  const variants = (rewrite?.variants ?? []).filter(Boolean)
  const rewritten = rewrite?.rewritten || monitor.question || ''
  const original = rewrite?.original || monitor.question || ''
  const rerankChunks = monitor.rerank_chunks ?? []
  const keptCount = rerankChunks.filter((r) => r.decision === 'kept').length
  const wall = monitor.timings_ms?.wall

  const items = [
    {
      key: 'question',
      label: (
        <Space size={8}>
          <span>① 用户问题</span>
          <Typography.Text type="secondary" ellipsis style={{ maxWidth: 420 }}>
            {monitor.question}
          </Typography.Text>
        </Space>
      ),
      children: (
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
          {monitor.question || '—'}
        </Typography.Paragraph>
      ),
    },
    {
      key: 'rewrite',
      label: (
        <Space size={8} wrap>
          <span>② 提示词改写</span>
          {rewrite?.history_used ? <Tag color="blue">已用会话</Tag> : <Tag>单轮</Tag>}
          {rewritten && rewritten !== original ? (
            <Typography.Text type="secondary" ellipsis style={{ maxWidth: 360 }}>
              {rewritten}
            </Typography.Text>
          ) : (
            <Tag>未改写</Tag>
          )}
        </Space>
      ),
      children: (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <Typography.Text type="secondary">原始问题</Typography.Text>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
              {original || '—'}
            </Typography.Paragraph>
          </div>
          <div>
            <Typography.Text type="secondary">检索用改写</Typography.Text>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
              {rewritten || '—'}
            </Typography.Paragraph>
          </div>
          {variants.length > 1 ? (
            <div>
              <Typography.Text type="secondary">MultiQuery 扩写变体（{variants.length}）</Typography.Text>
              <Collapse
                size="small"
                style={{ marginTop: 8 }}
                items={variants.map((v, i) => ({
                  key: `v-${i}`,
                  label: `变体 ${i + 1}`,
                  children: (
                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                      {v}
                    </Typography.Paragraph>
                  ),
                }))}
              />
            </div>
          ) : null}
        </Space>
      ),
    },
    {
      key: 'rerank',
      label: (
        <Space size={8} wrap>
          <span>③ Rerank 块</span>
          <Tag color="cyan">{rerankChunks.length} 条</Tag>
          <Tag color="green">保留 {keptCount}</Tag>
          {monitor.rerank_meta?.min_score != null ? (
            <Typography.Text type="secondary">
              min={monitor.rerank_meta.min_score} · keep={monitor.rerank_meta.keep ?? '-'}
            </Typography.Text>
          ) : null}
        </Space>
      ),
      children: rerankChunks.length ? (
        <div>
          {rerankChunks.map((row) => (
            <RerankItem key={`${row.rank}-${row.chunk_id}`} row={row} onOpenChunk={onOpenChunk} />
          ))}
        </div>
      ) : (
        <Typography.Text type="secondary">本次无精排结果（拒答或未启用）</Typography.Text>
      ),
    },
    {
      key: 'answer',
      label: (
        <Space size={8} wrap>
          <span>④ 输出回答</span>
          {monitor.refused ? <Tag color="orange">已拒答</Tag> : <Tag color="success">已作答</Tag>}
          {monitor.model ? <Tag>模型 {monitor.model}</Tag> : null}
          {monitor.intent ? <Tag>意图 {monitor.intent}</Tag> : null}
        </Space>
      ),
      children: (
        <div>
          {monitor.refused && monitor.refusal_reason ? (
            <Typography.Paragraph type="secondary">
              拒答原因：{monitor.refusal_reason}
            </Typography.Paragraph>
          ) : null}
          <AnswerMarkdown text={monitor.answer} empty="（无回答内容）" />
        </div>
      ),
    },
  ]

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          全过程监控
        </Typography.Title>
        <Space size={6} wrap>
          {monitor.cache_hit ? <Tag color="gold">答案缓存</Tag> : null}
          {monitor.mode_used ? <Tag>{monitor.mode_used}</Tag> : null}
          {wall != null ? <Tag>墙钟 {formatMs(wall)}</Tag> : null}
          {(monitor.prompt_tokens || monitor.completion_tokens) ? (
            <Tag>
              tokens {(monitor.prompt_tokens ?? 0)}+{(monitor.completion_tokens ?? 0)}
            </Tag>
          ) : null}
        </Space>
      </div>
      <Collapse defaultActiveKey={['question', 'rewrite', 'rerank', 'answer']} items={items} />
    </div>
  )
}
