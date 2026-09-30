/**
 * 分块预览与编辑（方案 05 §4.6）。
 *
 * 数据结构按后端**实际**返回：`{document, parents(计数), children(计数), tree(嵌套), orphan_children}`。
 * 注意 `parents`/`children` 是数字不是数组 —— 早先当数组用，页面直接崩。
 *
 * 后端的真实约束（`ekbs/api/chunks.py`）：**拆分/合并/编辑只支持 `chunk_type='child'`**；
 * 改子块会**追加新向量并重写 FTS**（不是原地改），提交前必须提示。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Card, Descriptions, Empty, Input, List, Space, Spin, Tag, Tooltip, Typography, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import ImpactConfirmModal from '@/pages/docs/ImpactConfirmModal'
import { getChunkTree, mergeChunk, reembedChunk, splitChunk, updateChunk } from '@/api/parse'
import type { ChunkNode, ChunkTree } from '@/api/types'

const TYPE_TEXT: Record<string, string> = {
  parent: '父容器', child: '子块', table: '表格', figure: '图片', summary: '摘要',
}

export default function ChunkExplorer() {
  const { docId } = useParams<{ docId: string }>()
  const id = Number(docId)
  const navigate = useNavigate()
  const [tree, setTree] = useState<ChunkTree | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeParentId, setActiveParentId] = useState<number | null>(null)
  const [activeChunk, setActiveChunk] = useState<ChunkNode | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ kind: 'split' | 'merge' | 'reembed'; chunk: ChunkNode } | null>(null)

  const load = useCallback(async () => {
    if (!Number.isFinite(id)) return
    setLoading(true)
    try {
      const data = await getChunkTree(id)
      setTree(data)
      const first = data.tree?.[0]
      setActiveParentId(first?.chunk_id ?? null)
      setActiveChunk(first ?? null)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally { setLoading(false) }
  }, [id])

  useEffect(() => { void load() }, [load])

  const activeParent = useMemo(
    () => (tree?.tree ?? []).find((n) => n.chunk_id === activeParentId) ?? null,
    [tree, activeParentId],
  )
  const children = activeParent?.children ?? []

  const saveEdit = async () => {
    if (!activeChunk) return
    try {
      await updateChunk(activeChunk.chunk_id, { content: draft, reason: '管理台编辑' })
      message.success('已保存（后端已追加新向量并重写 FTS）')
      setEditing(false)
      await load()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const runAction = async () => {
    if (!pending) return
    try {
      if (pending.kind === 'split') await splitChunk(pending.chunk.chunk_id)
      else if (pending.kind === 'merge') await mergeChunk(pending.chunk.chunk_id)
      else await reembedChunk(pending.chunk.chunk_id)
      message.success('已提交')
      setPending(null)
      await load()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  if (loading) return <Card><Spin /> </Card>
  if (!tree) return <Card>文档不存在或尚未产生分块</Card>

  const total = (tree.parents ?? 0) + (tree.children ?? 0)

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={12} wrap>
          <Typography.Title level={5} style={{ margin: 0 }}>{tree.document?.title ?? `文档 #${id}`}</Typography.Title>
          <ClearanceTag level={tree.document?.level} />
          <StatusTag status={tree.document?.status} />
          <Tag>父块 {tree.parents}</Tag>
          <Tag>子块 {tree.children}</Tag>
          <Tag>合计 {total}</Tag>
          {(tree.orphan_children?.length ?? 0) > 0 ? <Tag color="orange">孤儿块 {tree.orphan_children?.length}</Tag> : null}
          <span />
          <Button size="small" onClick={() => navigate('/docs/list')}>返回文档列表</Button>
          <Button size="small" onClick={() => void load()}>刷新</Button>
        </Space>
      </Card>

      {tree.tree?.length ? (
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
          <Card title={`父块（${tree.tree.length}）`} style={{ width: 400, flex: 'none' }} styles={{ body: { padding: 0 } }}>
            <List
              size="small"
              dataSource={tree.tree}
              renderItem={(p) => (
                <List.Item
                  onClick={() => { setActiveParentId(p.chunk_id); setActiveChunk(p); setEditing(false) }}
                  style={{ cursor: 'pointer', background: activeParentId === p.chunk_id ? '#e6f4ff' : undefined, paddingInline: 16 }}
                >
                  <List.Item.Meta
                    title={<Typography.Text ellipsis style={{ maxWidth: 320 }}>{p.heading_path || '(无标题路径)'}</Typography.Text>}
                    description={
                      <Space size={6} wrap>
                        <Tag>{TYPE_TEXT[p.chunk_type] ?? p.chunk_type}</Tag>
                        <span>{p.token_count ?? 0} tok</span>
                        <span>子块 {p.children?.length ?? 0}</span>
                        {/* ⚠️ 父块**按设计**不进向量（它是上下文容器，命中后用来扩展上下文）。
                            原先对所有 faiss_row 为空的块一律打橙色「未入向量」，用户看到
                            父块列表整列橙色，就以为"父子块都没向量、向量失败"了（实测反馈）。
                            只有**叶子块**没向量才是真异常，那才该报警。 */}
                        {(p.children?.length ?? 0) > 0 ? (
                          <Tooltip title="父块是上下文容器：检索命中叶子块后用它扩展上下文。按设计不进向量、不进全文索引，真正可检索的是下面的叶子块。">
                            <Tag>上下文块</Tag>
                          </Tooltip>
                        ) : p.faiss_row == null ? (
                          <Tooltip title="这是叶子块（本该进向量），但没有 faiss_row —— 属于异常，需要重新向量化。">
                            <Tag color="orange">未入向量</Tag>
                          </Tooltip>
                        ) : null}
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>

          <Card
            title={`子块（${children.length}）`}
            style={{ flex: 1, minWidth: 0 }}
            extra={
              activeChunk?.chunk_type === 'child' ? (
                <Space>
                  <Auth code="doc:chunk:update">
                    <Button size="small" onClick={() => { setDraft(activeChunk.content); setEditing(true) }}>编辑</Button>
                  </Auth>
                  <Auth code="doc:chunk:update">
                    <Button size="small" onClick={() => setPending({ kind: 'split', chunk: activeChunk })}>拆分</Button>
                  </Auth>
                  <Auth code="doc:chunk:update">
                    <Button size="small" onClick={() => setPending({ kind: 'merge', chunk: activeChunk })}>合并下一块</Button>
                  </Auth>
                  <Auth code="doc:chunk:reembed">
                    <Button size="small" onClick={() => setPending({ kind: 'reembed', chunk: activeChunk })}>重新向量化</Button>
                  </Auth>
                </Space>
              ) : (
                <Typography.Text type="secondary">
                  {activeChunk ? `${TYPE_TEXT[activeChunk.chunk_type] ?? activeChunk.chunk_type} 不支持在线编辑（后端仅支持 child）` : ''}
                </Typography.Text>
              )
            }
          >
            <List
              size="small"
              dataSource={children}
              locale={{ emptyText: <Empty description="该父块下没有子块（父块本身即检索单元）" /> }}
              renderItem={(c) => (
                <List.Item
                  onClick={() => { setActiveChunk(c); setEditing(false) }}
                  style={{ cursor: 'pointer', background: activeChunk?.chunk_id === c.chunk_id ? '#f5f7fa' : undefined }}
                >
                  <List.Item.Meta
                    title={<Typography.Text ellipsis style={{ maxWidth: 520 }}>{c.heading_path || `#${c.seq}`}</Typography.Text>}
                    description={<Typography.Paragraph className="ekbs-ellipsis-2" style={{ marginBottom: 0 }}>{c.content}</Typography.Paragraph>}
                  />
                </List.Item>
              )}
            />

            {activeChunk ? (
              <Card size="small" style={{ marginTop: 12 }} title="分块详情">
                <Descriptions size="small" column={2} bordered>
                  <Descriptions.Item label="chunk_id">{activeChunk.chunk_id}</Descriptions.Item>
                  <Descriptions.Item label="类型">{TYPE_TEXT[activeChunk.chunk_type] ?? activeChunk.chunk_type}</Descriptions.Item>
                  <Descriptions.Item label="seq">{activeChunk.seq}</Descriptions.Item>
                  <Descriptions.Item label="token">{activeChunk.token_count ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="分片">{activeChunk.shard_id ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="向量行">
                    {activeChunk.faiss_row ??
                      (activeChunk.chunk_type === 'parent'
                        ? '父块不参与向量（正常）'
                        : '未入向量（异常，可点「重新向量化」）')}
                  </Descriptions.Item>
                  <Descriptions.Item label="页码">
                    {activeChunk.page_from
                      ? `${activeChunk.page_from}${activeChunk.page_to && activeChunk.page_to !== activeChunk.page_from ? ` - ${activeChunk.page_to}` : ''}`
                      : '无（代码/文本类正常）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="标题路径" span={2}>{activeChunk.heading_path || '-'}</Descriptions.Item>
                </Descriptions>
                {editing ? (
                  <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
                    <Input.TextArea rows={8} value={draft} onChange={(e) => setDraft(e.target.value)} />
                    <Space>
                      <Button type="primary" onClick={() => void saveEdit()}>保存</Button>
                      <Button onClick={() => setEditing(false)}>取消</Button>
                      <Typography.Text type="secondary">保存会追加新向量并重写 FTS（不改历史向量）</Typography.Text>
                    </Space>
                  </Space>
                ) : (
                  <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginTop: 12, fontSize: 12 }}>
                    {activeChunk.content}
                  </pre>
                )}
              </Card>
            ) : null}
          </Card>
        </div>
      ) : (
        <Card><Empty description="该文档还没有分块，请确认 worker 已解析完成" /></Card>
      )}

      <ImpactConfirmModal
        open={pending !== null}
        level="A"
        title={pending ? ({ split: '拆分分块', merge: '合并到下一兄弟块', reembed: '重新向量化' } as const)[pending.kind] : ''}
        consequence={pending ? (
          <Typography.Text>
            {pending.kind === 'reembed'
              ? '将按当前嵌入模型重新生成该块的向量。'
              : '会改写分块并重写 FTS；原分块内容不可自动恢复。'}
          </Typography.Text>
        ) : null}
        impact={pending ? <div>目标分块：#{pending.chunk.chunk_id}（{pending.chunk.heading_path || '无标题路径'}）</div> : null}
        onCancel={() => setPending(null)}
        onOk={() => void runAction()}
      />
    </Space>
  )
}
