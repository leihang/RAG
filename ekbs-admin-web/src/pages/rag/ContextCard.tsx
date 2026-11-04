/**
 * 检索调试台「模型上下文」卡（方案 05 §4.7，task-5 新增 payload）。
 *
 * 用户最容易把「引用（citations）」当成「模型实际收到的上下文」：引用只是答案里真的
 * 标了 `[n]` 的那几块；父块/整表扩展出来的上下文同样进了 prompt，但答案没引用它时
 * 不会出现在引用列表里（实测 doc 205：prompt 有 3 块，引用只有 2 块，用户因此以为
 * 第 3 块「根本没推给模型」）。这张卡把**最终喂给 LLM 的每一块**都摆出来，并区分
 * 「引用」/「仅上下文」。
 *
 * 优雅降级：旧后端没有 `context_chunks` 时**整张卡不渲染**（不显示假数据）；
 * 只有后端明确给了空数组 `[]` 才显示空态。
 */
import { Card, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import EmptyState from '@/components/EmptyState'
import { formatNumber } from '@/utils/format'
import type { ContextChunk } from '@/api/rag'

/** 块类型 → 中文标签（后端取值见 `ekbs/ingest/chunker.py`：parent | child | table | figure）。 */
export const CONTEXT_CHUNK_TYPE_LABELS: Record<string, string> = {
  parent: '父容器',
  child: '子块',
  table: '表格容器',
  figure: '图片块',
}

/** 不认识的类型**原样显示**，别把后端新类型吞成 `-`。 */
export function contextChunkTypeLabel(chunkType?: string | null): string {
  const raw = String(chunkType ?? '').trim()
  if (!raw) return '-'
  return CONTEXT_CHUNK_TYPE_LABELS[raw] ?? raw
}

/** 与 Playground / 逐环节明细面板一致的 chunk 链接：有回调才可点，没有就只显示 id。 */
function ChunkLink({
  chunkId,
  onOpen,
  prefix = '',
}: {
  chunkId: number
  onOpen?: (chunkId: number) => void
  prefix?: string
}) {
  const text = prefix ? `${prefix}#${chunkId}` : String(chunkId)
  if (!onOpen) return <span className="ekbs-mono">{text}</span>
  return (
    <Typography.Link onClick={() => onOpen(chunkId)} className="ekbs-mono">
      {text}
    </Typography.Link>
  )
}

export interface ContextCardProps {
  /**
   * 后端 `context_chunks`；`undefined`/`null` = 旧后端，整卡不渲染。
   * 显式 `[]` = 后端确认本次没有上下文，给空态。
   */
  contextChunks?: ContextChunk[] | null
  /** 后端 `context_tokens`；缺省时用各块 token_count 求和兜底 */
  contextTokens?: number | null
  onOpenChunk?: (chunkId: number) => void
}

export default function ContextCard({ contextChunks, contextTokens, onOpenChunk }: ContextCardProps) {
  // 旧后端没有该字段：不渲染一张空卡，避免让用户以为「后端说没有上下文」
  if (contextChunks == null) return null

  const chunks = contextChunks
  const citedCount = chunks.filter((chunk) => chunk.cited).length
  const totalTokens =
    contextTokens ??
    chunks.reduce(
      (sum, chunk) => sum + (Number.isFinite(Number(chunk.token_count)) ? Number(chunk.token_count) : 0),
      0,
    )

  const columns: ColumnsType<ContextChunk> = [
    { title: '#', dataIndex: 'index', width: 50 },
    {
      title: 'chunk_id',
      dataIndex: 'chunk_id',
      width: 100,
      render: (id: number) => <ChunkLink chunkId={id} onOpen={onOpenChunk} />,
    },
    {
      title: '类型',
      dataIndex: 'chunk_type',
      width: 100,
      render: (value: string | null | undefined) => contextChunkTypeLabel(value),
    },
    {
      title: '来源',
      dataIndex: 'source_chunk_id',
      width: 230,
      render: (source: number | null | undefined, row: ContextChunk) => {
        const rank =
          row.source_rerank_rank == null ? null : (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              精排#{row.source_rerank_rank}
            </Typography.Text>
          )
        // 旧后端/未扩展：没有来源块，只显示它自己的精排名次（没有就给 -）
        if (source == null || source === row.chunk_id) return rank ?? '-'
        return (
          <Space size={4} wrap>
            {/* 父块不在精排候选里是设计使然：精排只给检索单元打分，扩展发生在精排之后 */}
            <Tooltip title={`该上下文块是精排后扩展出的父块/整表上下文，由精排块 #${source} 扩展而来`}>
              <Tag color="orange">父块上下文</Tag>
            </Tooltip>
            <ChunkLink chunkId={source} onOpen={onOpenChunk} prefix="← " />
            {rank}
          </Space>
        )
      },
    },
    {
      title: 'token',
      dataIndex: 'token_count',
      width: 90,
      render: (value: number | null | undefined) => formatNumber(value),
    },
    {
      title: '引用',
      dataIndex: 'cited',
      width: 110,
      render: (cited: boolean, row: ContextChunk) =>
        cited ? <Tag color="green">引用 [{row.index}]</Tag> : <Tag>仅上下文</Tag>,
    },
  ]

  return (
    <Card
      title={`模型上下文（${chunks.length} 块 · 被引用 ${citedCount}）`}
      extra={
        <Space size={6}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            合计 {formatNumber(totalTokens)} tok
          </Typography.Text>
          {/* 这张卡存在的唯一理由：把「引用」和「模型实际收到的上下文」讲清楚 */}
          <Tooltip title="引用≠上下文：只有答案里标了 [n] 的块才进引用/审计；其余块同样推给了模型">
            <QuestionCircleOutlined style={{ color: '#8c8c8c' }} />
          </Tooltip>
        </Space>
      }
    >
      {chunks.length === 0 ? (
        <EmptyState description="本次请求没有记录模型上下文（后端可能未开启上下文记录）" />
      ) : (
        <Table<ContextChunk>
          size="small"
          rowKey="index"
          pagination={false}
          dataSource={chunks}
          columns={columns}
        />
      )}
    </Card>
  )
}
