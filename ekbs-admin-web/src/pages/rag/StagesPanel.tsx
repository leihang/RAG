/**
 * 检索调试台「逐跳明细」面板（方案 05 §4.7 的第 1–5 跳）。
 *
 * 只渲染**后端真的返回了的**段落（`stages` 的每个子段都是可选的：拒答会提前 return，
 * 此时只剩 `params`）。不确定的字段一律不猜、不补零 —— 见 `./playgroundData.ts` 的解析注释。
 */
import { useState } from 'react'
import { Alert, Badge, Card, Col, Row, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import EmptyState from '@/components/EmptyState'
import { formatNumber } from '@/utils/format'
import type { DensePerShard, DebugStages, StageParams } from './playgroundData'
import { stageSectionKeys } from './playgroundData'

const TOP_N = 10

/** 命中数 + 前 N 条，N 由 `expanded` 决定（逐跳明细是调试台的核心价值，但不该一次刷 500 行）。 */
function preview<T>(rows: T[], expanded: boolean, limit = TOP_N): T[] {
  return expanded ? rows : rows.slice(0, limit)
}

function ExpandHint({ total, expanded, onToggle }: { total: number; expanded: boolean; onToggle: () => void }) {
  if (total <= TOP_N) return null
  return (
    <Typography.Link onClick={onToggle} style={{ fontSize: 12 }}>
      {expanded ? '收起' : `展开全部 ${total} 条`}
    </Typography.Link>
  )
}

function ParamsCard({ params }: { params: StageParams }) {
  const rows = Object.entries(params).map(([key, value]) => ({ key, value }))
  return (
    <Card size="small" title="本次生效参数（`stages.params`）">
      <Row gutter={[8, 4]}>
        {rows.map((row) => (
          <Col xs={12} sm={8} md={6} key={row.key}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.key}
            </Typography.Text>
            <div className="ekbs-mono">{row.value === null ? '-' : String(row.value)}</div>
          </Col>
        ))}
      </Row>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        这是 `RetrievalOverrides.effective()` 算出的**本次请求**参数，不是全局配置。
      </Typography.Text>
    </Card>
  )
}

function DenseCard({ dense }: { dense: NonNullable<DebugStages['dense']> }) {
  const [expanded, setExpanded] = useState(false)
  const total = dense.perShard.reduce((sum, shard) => sum + shard.hits.length, 0)
  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>第 1 跳 · 向量召回（逐分片）</span>
          <Badge count={total} showZero color="#1677ff" />
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          requested_top_k={dense.requestedTopK ?? '-'} · 合并候选 {dense.mergedChunkIds.length} 条
        </Typography.Text>
      }
    >
      {dense.perShard.length === 0 ? (
        <EmptyState description="本次没有分片级向量命中明细" />
      ) : (
        <>
          {dense.perShard.map((shard) => (
            <ShardHits key={shard.shard_id} shard={shard} expanded={expanded} />
          ))}
          <ExpandHint total={total} expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
        </>
      )}
    </Card>
  )
}

function ShardHits({ shard, expanded }: { shard: DensePerShard; expanded: boolean }) {
  const columns: ColumnsType<{ faiss_row: number | null; score: number | null }> = [
    {
      title: '#',
      width: 48,
      render: (_v, _row, index) => index + 1,
    },
    {
      title: 'faiss_row',
      dataIndex: 'faiss_row',
      width: 120,
      render: (value: number | null) => (value == null ? '-' : String(value)),
    },
    {
      title: '相似度',
      dataIndex: 'score',
      width: 140,
      render: (value: number | null) => (value == null ? '-' : value.toFixed(6)),
    },
  ]
  return (
    <div style={{ marginBottom: 12 }}>
      <Space size={8} style={{ marginBottom: 4 }}>
        <Tag color="blue">{shard.shard_id}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          命中 {shard.hits.length} 条
        </Typography.Text>
      </Space>
      <Table
        size="small"
        rowKey={(_row, index) => `${shard.shard_id}-${index ?? 0}`}
        columns={columns}
        dataSource={preview(shard.hits, expanded)}
        pagination={false}
        locale={{ emptyText: <EmptyState description="该分片本次没有向量命中" /> }}
      />
    </div>
  )
}

function ChunkLink({
  value,
  onOpen,
}: {
  value: number | null
  onOpen?: (chunkId: number) => void
}) {
  if (value == null) return <>-</>
  if (!onOpen) return <>{String(value)}</>
  return (
    <Typography.Link onClick={() => onOpen(value)} className="ekbs-mono">
      {value}
    </Typography.Link>
  )
}

function SparseCard({
  sparse,
  onOpenChunk,
}: {
  sparse: NonNullable<DebugStages['sparse']>
  onOpenChunk?: (chunkId: number) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const columns: ColumnsType<NonNullable<DebugStages['sparse']>['hits'][number]> = [
    { title: '#', width: 48, render: (_v, _row, index) => index + 1 },
    {
      title: 'chunk_id',
      dataIndex: 'chunk_id',
      width: 120,
      render: (value: number | null) => <ChunkLink value={value} onOpen={onOpenChunk} />,
    },
    {
      title: 'BM25 分数',
      dataIndex: 'bm25',
      width: 140,
      render: (value: number | null) => (value == null ? '-' : value.toFixed(4)),
    },
    { title: '分片', dataIndex: 'shard_id', render: (value: string | null) => value ?? '-' },
  ]
  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>第 2 跳 · BM25 召回（SQLite FTS5）</span>
          <Badge count={sparse.hits.length} showZero color="geekblue" />
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          requested_top_k={sparse.requestedTopK ?? '-'}
        </Typography.Text>
      }
    >
      <Table
        size="small"
        rowKey={(_row, index) => `sparse-${index ?? 0}`}
        columns={columns}
        dataSource={preview(sparse.hits, expanded)}
        pagination={false}
        locale={{ emptyText: <EmptyState description="本次没有 BM25 命中" /> }}
      />
      <ExpandHint total={sparse.hits.length} expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
    </Card>
  )
}

function RrfCard({
  rrf,
  onOpenChunk,
}: {
  rrf: NonNullable<DebugStages['rrf']>
  onOpenChunk?: (chunkId: number) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const columns: ColumnsType<NonNullable<DebugStages['rrf']>['fused'][number]> = [
    { title: '排名', width: 64, render: (_v, _row, index) => index + 1 },
    {
      title: 'chunk_id',
      dataIndex: 'chunk_id',
      width: 120,
      render: (value: number | null) => <ChunkLink value={value} onOpen={onOpenChunk} />,
    },
    {
      title: '融合分',
      dataIndex: 'score',
      render: (value: number | null) => (value == null ? '-' : value.toFixed(8)),
    },
  ]
  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>第 3 跳 · RRF 融合</span>
          <Tag>k={rrf.k ?? '-'}</Tag>
          <Badge count={rrf.fused.length} showZero color="purple" />
        </Space>
      }
    >
      <Table
        size="small"
        rowKey={(_row, index) => `rrf-${index ?? 0}`}
        columns={columns}
        dataSource={preview(rrf.fused, expanded)}
        pagination={false}
        locale={{ emptyText: <EmptyState description="没有融合结果（本次召回应为空）" /> }}
      />
      <ExpandHint total={rrf.fused.length} expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
    </Card>
  )
}

function RerankCard({
  rerank,
  onOpenChunk,
}: {
  rerank: NonNullable<DebugStages['rerank']>
  onOpenChunk?: (chunkId: number) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const { minScore, keep } = rerank
  const columns: ColumnsType<NonNullable<DebugStages['rerank']>['scores'][number] & { decision: string }> = [
    { title: '排名', width: 64, render: (_v, _row, index) => index + 1 },
    {
      title: 'chunk_id',
      dataIndex: 'chunk_id',
      width: 120,
      render: (value: number | null) => <ChunkLink value={value} onOpen={onOpenChunk} />,
    },
    {
      title: 'rerank 分数',
      dataIndex: 'rerank_score',
      width: 140,
      render: (value: number | null) => (value == null ? '-' : value.toFixed(4)),
    },
    {
      // 判定只用后端真的给了的 min_score / 排名，不臆造规则
      title: '判定',
      dataIndex: 'decision',
      render: (value: string) => <span>{value}</span>,
    },
  ]
  const rows = preview(rerank.scores, expanded).map((row, index) => {
    let decision = '—'
    if (row.rerank_score != null && minScore != null && row.rerank_score < minScore) decision = '✖ 低于阈值'
    else if (keep != null && index >= keep) decision = '— 超 Rerank 输出'
    else if (row.rerank_score != null) decision = '✔ 保留'
    return { ...row, decision }
  })

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>第 4 跳 · Rerank 精排</span>
          <Badge
            status={rerank.enabled === false ? 'default' : 'success'}
            text={rerank.enabled === false ? '本次未启用' : '已启用'}
          />
          <Badge count={rerank.scores.length} showZero color="cyan" />
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          requested_top_n={rerank.requestedTopN ?? '-'} · keep={rerank.keep ?? '-'} · min_score=
          {rerank.minScore ?? '-'}
        </Typography.Text>
      }
    >
      <Table
        size="small"
        rowKey={(_row, index) => `rerank-${index ?? 0}`}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <EmptyState description="没有精排分数（可能未启用或候选为空）" /> }}
      />
      <ExpandHint total={rerank.scores.length} expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
    </Card>
  )
}

function C5Card({
  c5,
  onOpenChunk,
}: {
  c5: NonNullable<DebugStages['c5']>
  onOpenChunk?: (chunkId: number) => void
}) {
  const renderIds = (ids: number[]) => {
    if (!ids.length) return '—'
    if (!onOpenChunk) return ids.join(', ')
    return (
      <Space size={[4, 4]} wrap>
        {ids.map((id) => (
          <Typography.Link key={id} onClick={() => onOpenChunk(id)} className="ekbs-mono">
            {id}
          </Typography.Link>
        ))}
      </Space>
    )
  }
  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>第 4.5 跳 · C5 密级闸门</span>
          <Tag color="green">保留 {c5.kept.length}</Tag>
          <Tag color={c5.dropped.length ? 'red' : 'default'}>拦截 {c5.dropped.length}</Tag>
        </Space>
      }
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          被拦截的分块不得进入任何下游
        </Typography.Text>
      }
    >
      <Row gutter={[16, 8]}>
        <Col xs={24} md={12}>
          <Typography.Text type="secondary">kept（进入上下文）</Typography.Text>
          <div className="ekbs-mono">{renderIds(c5.kept)}</div>
        </Col>
        <Col xs={24} md={12}>
          <Typography.Text type="secondary">dropped（被 C5 拦截）</Typography.Text>
          <div className="ekbs-mono" style={{ color: c5.dropped.length ? '#ff4d4f' : undefined }}>
            {renderIds(c5.dropped)}
          </div>
        </Col>
      </Row>
    </Card>
  )
}

export default function StagesPanel({
  stages,
  onOpenChunk,
}: {
  stages: DebugStages
  onOpenChunk?: (chunkId: number) => void
}) {
  const keys = stageSectionKeys(stages)
  if (keys.length === 0) {
    return (
      <Alert
        type="info"
        showIcon
        message="本次响应没有逐跳明细"
        description="后端只有在 `collect_debug=True` 时才回 `stages`；拒答路径会提前返回，因此可能只剩 `params`。"
      />
    )
  }
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {stages.params ? <ParamsCard params={stages.params} /> : null}
      {stages.dense ? <DenseCard dense={stages.dense} /> : null}
      {stages.sparse ? <SparseCard sparse={stages.sparse} onOpenChunk={onOpenChunk} /> : null}
      {stages.rrf ? <RrfCard rrf={stages.rrf} onOpenChunk={onOpenChunk} /> : null}
      {stages.rerank ? <RerankCard rerank={stages.rerank} onOpenChunk={onOpenChunk} /> : null}
      {stages.c5 ? <C5Card c5={stages.c5} onOpenChunk={onOpenChunk} /> : null}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        共 {keys.length} 段明细（{keys.join(' / ')}）；未出现的段落代表后端本次没有写入，不是 0。
        {onOpenChunk ? ' 点击 chunk_id 可查看块内容。' : ''}
      </Typography.Text>
      {formatNumber(keys.length) ? null : null}
    </Space>
  )
}
