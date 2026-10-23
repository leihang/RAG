/**
 * 分块内容抽屉：检索控制台 / 引用表点击 chunk_id 直达查看正文。
 */
import { useEffect, useState } from 'react'
import { Button, Descriptions, Drawer, Space, Spin, Tag, Typography, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import FigureThumb from '@/components/FigureThumb'
import { getChunk } from '@/api/parse'
import type { ChunkNode } from '@/api/types'

const TYPE_TEXT: Record<string, string> = {
  parent: '父容器', child: '子块', table: '表格', figure: '图片', summary: '摘要',
}

export interface ChunkPeekDrawerProps {
  chunkId: number | null
  open: boolean
  onClose: () => void
}

export default function ChunkPeekDrawer({ chunkId, open, onClose }: ChunkPeekDrawerProps) {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [chunk, setChunk] = useState<(ChunkNode & Record<string, unknown>) | null>(null)

  useEffect(() => {
    if (!open || chunkId == null) {
      setChunk(null)
      return
    }
    let cancelled = false
    setLoading(true)
    void getChunk(chunkId)
      .then((resp) => {
        if (!cancelled) setChunk(resp.chunk)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setChunk(null)
          message.error(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [open, chunkId])

  const isFigure = chunk?.chunk_type === 'figure' || Boolean(chunk?.figure_id)

  return (
    <Drawer
      title={chunkId != null ? `分块 #${chunkId}` : '分块详情'}
      width={560}
      open={open}
      onClose={onClose}
      destroyOnClose
      extra={
        chunk?.doc_id != null ? (
          <Button
            type="link"
            onClick={() => {
              onClose()
              navigate(`/chunks/${chunk.doc_id}?chunk=${chunk.chunk_id}`)
            }}
          >
            在分块预览打开
          </Button>
        ) : null
      }
    >
      {loading ? <Spin /> : null}
      {!loading && chunk ? (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="文档">
              {String(chunk.doc_title ?? `doc #${chunk.doc_id}`)}
            </Descriptions.Item>
            <Descriptions.Item label="类型">
              <Space size={6} wrap>
                <Tag>{TYPE_TEXT[chunk.chunk_type] ?? chunk.chunk_type}</Tag>
                {isFigure ? (
                  chunk.figure_mm_indexed ? (
                    <Tag color="purple">多模态已索引</Tag>
                  ) : (
                    <Tag color="orange">多模态未索引</Tag>
                  )
                ) : null}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="标题路径">{chunk.heading_path || '-'}</Descriptions.Item>
            <Descriptions.Item label="页码">
              {chunk.page_from != null
                ? `${chunk.page_from}${chunk.page_to != null && chunk.page_to !== chunk.page_from ? ` - ${chunk.page_to}` : ''}`
                : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="分片 / 向量行">
              {chunk.shard_id ?? '-'} / {chunk.faiss_row ?? '-'}
            </Descriptions.Item>
          </Descriptions>
          {isFigure ? (
            <FigureThumb chunkId={chunk.chunk_id} title={chunk.figure_id ?? undefined} size={200} />
          ) : null}
          <Typography.Text type="secondary">正文</Typography.Text>
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, margin: 0 }}>
            {chunk.content || '（空）'}
          </pre>
        </Space>
      ) : null}
      {!loading && !chunk && chunkId != null ? (
        <Typography.Text type="secondary">未加载到分块</Typography.Text>
      ) : null}
    </Drawer>
  )
}
