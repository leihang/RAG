/**
 * 分块预览与编辑（方案 05 §4.6）。
 *
 * 数据结构按后端**实际**返回：`{document, parents(计数), children(计数), tree(嵌套), orphan_children}`。
 * 注意 `parents`/`children` 是数字不是数组 —— 早先当数组用，页面直接崩。
 * 后端按 `parent_id`（与 `chunk_type` 无关）递归建树：`parents` = 顶层块数、
 * `children` = 树内后代总数；有子节点 = 上下文容器（如 `table` 父容器），按设计不进向量。
 *
 * 后端的真实约束（`ekbs/api/chunks.py`）：**拆分/合并/编辑只支持 `chunk_type='child'`**；
 * 改子块会**追加新向量并重写 FTS**（不是原地改），提交前必须提示。
 *
 * 大表（如能耗分析五千+行组）不能一次性展开：默认只展开「子节点不多」的容器，
 * 左栏虚拟滚动；树接口只带 content_head，右栏点选再拉全文。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Card, Descriptions, Empty, Input, List, Modal, Space, Spin, Tag, Tooltip, Typography, message } from 'antd'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import FigureThumb from '@/components/FigureThumb'
import StatusTag from '@/components/StatusTag'
import ImpactConfirmModal from '@/pages/docs/ImpactConfirmModal'
import { getChunk, getChunkTree, mergeChunk, reembedChunk, splitChunk, updateChunk } from '@/api/parse'
import type { ChunkNode, ChunkTree, ChunkTreeNode } from '@/api/types'

const TYPE_TEXT: Record<string, string> = {
  parent: '父容器', child: '子块', table: '表格', figure: '图片', summary: '摘要',
}

/** 子节点数超过此值时默认折叠，避免电子表格一次摊开几千行把浏览器卡死。 */
const AUTO_EXPAND_CHILD_LIMIT = 40

/** 容器提示：有子节点 = 上下文容器，按设计不进向量（检索命中叶子后用它扩展上下文）。 */
const CONTAINER_VECTOR_HINT = '按设计不进向量（检索命中叶子后用它扩展上下文）'

function childNodes(node: ChunkNode | ChunkTreeNode | null | undefined): ChunkTreeNode[] {
  return ((node as ChunkTreeNode | null | undefined)?.children ?? []) as ChunkTreeNode[]
}

/**
 * 是否有子节点 —— 判断"容器 vs 叶子"的**唯一依据**，与 chunk_type 无关：
 * 表格父容器（chunk_type='table' 且有行组叶子）和父块一样不进向量；
 * 叶子（含独立 figure/table）才必须入向量。
 */
function hasChildren(node: ChunkNode | ChunkTreeNode | null | undefined): boolean {
  return childNodes(node).length > 0
}

function typeLabel(node: ChunkNode, leafTop: boolean): string {
  if (node.chunk_type === 'figure') return '图片'
  if (node.chunk_type === 'table') return hasChildren(node) ? '表格父容器' : '表格'
  if (leafTop) return '叶子块（短节）'
  return TYPE_TEXT[node.chunk_type] ?? node.chunk_type
}

/** 递归找 focus 分块的路径（树可有三层：parent -> table 容器 -> table 行组叶子）。 */
function findNodePath(nodes: ChunkTreeNode[], chunkId: number): ChunkTreeNode[] | null {
  for (const node of nodes) {
    if (node.chunk_id === chunkId) return [node]
    const rest = findNodePath(childNodes(node), chunkId)
    if (rest) return [node, ...rest]
  }
  return null
}

/** 收集「子节点不多」的容器 id，作为默认展开集；大容器留给用户点开。 */
function defaultExpandedIds(nodes: ChunkTreeNode[]): Set<number> {
  const out = new Set<number>()
  const walk = (list: ChunkTreeNode[]) => {
    for (const node of list) {
      const kids = childNodes(node)
      if (!kids.length) continue
      if (kids.length <= AUTO_EXPAND_CHILD_LIMIT) {
        out.add(node.chunk_id)
        walk(kids)
      }
    }
  }
  walk(nodes)
  return out
}

interface FlatChunk {
  node: ChunkTreeNode
  depth: number
}

/** 按展开集摊平成左栏列表；未展开的容器只占一行。 */
function flattenChunks(
  nodes: ChunkTreeNode[],
  expanded: Set<number>,
  depth = 0,
): FlatChunk[] {
  return nodes.flatMap((node) => {
    const kids = childNodes(node)
    const row: FlatChunk = { node, depth }
    if (!kids.length || !expanded.has(node.chunk_id)) return [row]
    return [row, ...flattenChunks(kids, expanded, depth + 1)]
  })
}

function previewText(node: ChunkNode): string {
  return (node.content_head || node.content || '').slice(0, 160)
}

/** 向量状态标签：容器按设计不进向量（不是异常）；叶子缺 faiss_row 才是异常。 */
function VectorTag({ node }: { node: ChunkNode | ChunkTreeNode }) {
  if (hasChildren(node)) {
    return (
      <Tooltip title={CONTAINER_VECTOR_HINT}>
        <Tag>{node.chunk_type === 'table' ? '表格父容器' : '上下文块'}</Tag>
      </Tooltip>
    )
  }
  if (node.faiss_row == null) {
    return (
      <Tooltip title="叶子块本该进向量，但没有 faiss_row —— 异常，需重新向量化。">
        <Tag color="orange">未入文本向量</Tag>
      </Tooltip>
    )
  }
  return (
    <Tooltip title="叶子块是检索单元，已入向量。">
      <Tag color="success">已入文本向量</Tag>
    </Tooltip>
  )
}

function MmBadge({ node }: { node: ChunkNode }) {
  if (node.chunk_type !== 'figure') return null
  return node.figure_mm_indexed ? (
    <Tooltip title="已写入 figures.faiss，可走多模态召回">
      <Tag color="purple">多模态已索引</Tag>
    </Tooltip>
  ) : (
    <Tooltip title="图片块尚未写入多模态旁路索引（可重解析或检查 multimodal 配置）">
      <Tag color="orange">多模态未索引</Tag>
    </Tooltip>
  )
}

export default function ChunkExplorer() {
  const { docId } = useParams<{ docId: string }>()
  const id = Number(docId)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const focusChunkId = Number(searchParams.get('chunk') || '')
  const [tree, setTree] = useState<ChunkTree | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeChunk, setActiveChunk] = useState<ChunkNode | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ kind: 'split' | 'merge' | 'reembed'; chunk: ChunkNode } | null>(null)
  const [compareNodes, setCompareNodes] = useState<ChunkNode[]>([])
  const [compareOpen, setCompareOpen] = useState(false)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())

  const selectChunk = useCallback(async (node: ChunkNode) => {
    setActiveChunk(node)
    setEditing(false)
    // 树接口只有 content_head；右栏/对比/编辑再拉全文
    setDetailLoading(true)
    try {
      const { chunk } = await getChunk(node.chunk_id)
      setActiveChunk((prev) => (prev?.chunk_id === node.chunk_id ? { ...prev, ...chunk } : prev))
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const load = useCallback(async () => {
    if (!Number.isFinite(id)) return
    setLoading(true)
    try {
      const data = await getChunkTree(id)
      setTree(data)
      const roots = data.tree ?? []
      const expanded = defaultExpandedIds(roots)
      const focus = Number.isFinite(focusChunkId) && focusChunkId > 0 ? focusChunkId : null
      let focusNode: ChunkTreeNode | null = null
      if (focus != null) {
        const path = findNodePath(roots, focus)
        if (path) {
          for (const node of path.slice(0, -1)) expanded.add(node.chunk_id)
          focusNode = path[path.length - 1]
        } else {
          const orphan = (data.orphan_children ?? []).find((c) => c.chunk_id === focus)
          if (orphan) focusNode = orphan as ChunkTreeNode
        }
      }
      setExpandedIds(expanded)
      const first = focusNode ?? flattenChunks(roots, expanded)[0]?.node ?? null
      if (first) await selectChunk(first)
      else setActiveChunk(null)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally { setLoading(false) }
  }, [id, focusChunkId, selectChunk])

  useEffect(() => { void load() }, [load])

  const flatNodes = useMemo(
    () => flattenChunks(tree?.tree ?? [], expandedIds),
    [tree, expandedIds],
  )

  const toggleExpand = (chunkId: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(chunkId)) next.delete(chunkId)
      else next.add(chunkId)
      return next
    })
  }

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
  const leafParents = (tree.tree ?? []).filter((p) => !hasChildren(p)).length
  const inCompare = (chunkId: number) => compareNodes.some((n) => n.chunk_id === chunkId)
  const toggleCompare = async (node: ChunkNode) => {
    let full = node
    if ((node.content?.length ?? 0) <= 200) {
      try {
        const res = await getChunk(node.chunk_id)
        full = { ...node, ...res.chunk }
      } catch {
        /* 对比仍可用摘要 */
      }
    }
    setCompareNodes((prev) => {
      if (prev.some((n) => n.chunk_id === full.chunk_id)) {
        return prev.filter((n) => n.chunk_id !== full.chunk_id)
      }
      if (prev.length >= 2) {
        message.info('最多对比 2 个分块，已替换最早选择的一个')
        return [prev[1], full]
      }
      return [...prev, full]
    })
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={12} wrap>
          <Typography.Title level={5} style={{ margin: 0 }}>{tree.document?.title ?? `文档 #${id}`}</Typography.Title>
          <ClearanceTag level={tree.document?.level} />
          <StatusTag status={tree.document?.status} />
          <Tooltip title="顶层块数（parent_id 为空）：L1 父块、叶子父块、独立 figure/table 叶子">
            <Tag>父块 {tree.parents}</Tag>
          </Tooltip>
          <Tooltip title="树内全部后代数（含表格父容器下的行组叶子）">
            <Tag>子块 {tree.children}</Tag>
          </Tooltip>
          {leafParents > 0 ? (
            <Tooltip title="PPT/短节常见：整页短于子块下限时不建同文 child，父块本身作为检索叶子并已入向量。不是解析失败。">
              <Tag color="blue">叶子父块 {leafParents}</Tag>
            </Tooltip>
          ) : null}
          <Tag>合计 {total}</Tag>
          {(tree.orphan_children?.length ?? 0) > 0 ? <Tag color="orange">孤儿块 {tree.orphan_children?.length}</Tag> : null}
          <span />
          <Button size="small" onClick={() => navigate('/docs/list')}>返回文档列表</Button>
          <Button size="small" onClick={() => void load()}>刷新</Button>
          <Button size="small" disabled={compareNodes.length !== 2} onClick={() => setCompareOpen(true)}>
            对比已选（{compareNodes.length}/2）
          </Button>
        </Space>
      </Card>

      {tree.tree?.length ? (
        // 窄屏时上下堆叠：右栏（子块）是检索单元，不能因为窗口窄被挤成 0 宽度而"看不见"
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <Card
            title={`分块（可见 ${flatNodes.length} / 合计 ${total}）`}
            style={{ width: 400, flex: 'none' }}
            styles={{ body: { padding: 0, maxHeight: 'calc(100vh - 250px)', overflowY: 'auto' } }}
          >
            <List
              size="small"
              dataSource={flatNodes}
              renderItem={({ node, depth }) => {
                const nodeChildren = childNodes(node)
                const expanded = expandedIds.has(node.chunk_id)
                return (
                  <List.Item
                    onClick={() => { void selectChunk(node) }}
                    style={{
                      cursor: 'pointer',
                      paddingLeft: 12 + depth * 16,
                      background: activeChunk?.chunk_id === node.chunk_id ? '#e6f4ff' : undefined,
                    }}
                  >
                    <List.Item.Meta
                      title={
                        <Space size={6} wrap>
                          {nodeChildren.length > 0 ? (
                            <Button
                              type="text"
                              size="small"
                              onClick={(e) => {
                                e.stopPropagation()
                                toggleExpand(node.chunk_id)
                              }}
                            >
                              {expanded ? '▾' : '▸'}
                            </Button>
                          ) : (
                            <span style={{ display: 'inline-block', width: 24 }} />
                          )}
                          <Typography.Text ellipsis style={{ maxWidth: 200 }}>
                            #{node.chunk_id} {node.heading_path || `seq ${node.seq}`}
                          </Typography.Text>
                          <Tag color={node.chunk_type === 'table' ? 'geekblue' : node.chunk_type === 'figure' ? 'purple' : undefined}>
                            {typeLabel(node, !hasChildren(node))}
                          </Tag>
                          <VectorTag node={node} />
                          <span>{node.token_count ?? 0} tok</span>
                          {nodeChildren.length > 0 ? (
                            <Tag>{node.chunk_type === 'table' ? '行组' : '子块'} {nodeChildren.length}</Tag>
                          ) : null}
                        </Space>
                      }
                      description={
                        <Typography.Paragraph className="ekbs-ellipsis-2" style={{ marginBottom: 0, fontSize: 12 }}>
                          {previewText(node)}
                        </Typography.Paragraph>
                      }
                    />
                  </List.Item>
                )
              }}
            />
          </Card>

          <Card
            title={activeChunk ? `分块内容 #${activeChunk.chunk_id}` : '分块内容'}
            style={{ flex: '1 1 520px', minWidth: 420 }}
            styles={{ body: { maxHeight: 'calc(100vh - 250px)', overflowY: 'auto' } }}
            extra={
              activeChunk ? (
                <Space wrap>
                  <Button size="small" onClick={() => void toggleCompare(activeChunk)}>
                    {inCompare(activeChunk.chunk_id) ? '移出对比' : '加入对比'}
                  </Button>
                  {activeChunk.chunk_type === 'child' ? (
                    <>
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
                    </>
                  ) : null}
                </Space>
              ) : null
            }
          >
            {!activeChunk ? (
              <Empty description="从左侧选择一个分块查看全文" />
            ) : detailLoading && (activeChunk.content?.length ?? 0) <= 200 ? (
              <Spin />
            ) : (
              <>
                <Space size={6} wrap style={{ marginBottom: 12 }}>
                  <Tag>#{activeChunk.chunk_id}</Tag>
                  <Tag>{typeLabel(activeChunk, !hasChildren(activeChunk))}</Tag>
                  <VectorTag node={activeChunk} />
                  <span>{activeChunk.token_count ?? 0} tok</span>
                  {hasChildren(activeChunk) ? <Tag>子块 {childNodes(activeChunk).length}</Tag> : null}
                  <MmBadge node={activeChunk} />
                </Space>
                <Descriptions size="small" column={2} bordered>
                  <Descriptions.Item label="类型">{typeLabel(activeChunk, !hasChildren(activeChunk))}</Descriptions.Item>
                  <Descriptions.Item label="seq">{activeChunk.seq}</Descriptions.Item>
                  <Descriptions.Item label="token">{activeChunk.token_count ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="分片">{activeChunk.shard_id ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="向量行">
                    {activeChunk.faiss_row != null
                      ? activeChunk.faiss_row
                      : hasChildren(activeChunk)
                        ? (activeChunk.chunk_type === 'table'
                          ? '表格父容器按设计不进向量（正常）'
                          : '父容器按设计不进向量（正常）')
                        : '未入向量（异常，可点「重新向量化」）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="页码">
                    {activeChunk.page_from
                      ? `${activeChunk.page_from}${activeChunk.page_to && activeChunk.page_to !== activeChunk.page_from ? ` - ${activeChunk.page_to}` : ''}`
                      : '无（代码/文本类正常）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="标题路径" span={2}>{activeChunk.heading_path || '-'}</Descriptions.Item>
                  {activeChunk.chunk_type === 'figure' ? (
                    <Descriptions.Item label="图" span={2}>
                      <FigureThumb chunkId={activeChunk.chunk_id} title={activeChunk.figure_id ?? undefined} size={160} />
                    </Descriptions.Item>
                  ) : null}
                </Descriptions>
                {editing ? (
                  <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
                    <Input.TextArea rows={14} value={draft} onChange={(e) => setDraft(e.target.value)} />
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
              </>
            )}
          </Card>
        </div>
      ) : (
        <Card><Empty description="该文档还没有分块，请确认 worker 已解析完成" /></Card>
      )}

      <Modal
        open={compareOpen}
        width="90vw"
        footer={null}
        onCancel={() => setCompareOpen(false)}
        title="分块对比（全文）"
        destroyOnClose
      >
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {compareNodes.map((node) => (
            <div key={node.chunk_id}>
              <Space size={6} wrap style={{ marginBottom: 8 }}>
                <Tag>#{node.chunk_id}</Tag>
                <Tag>{typeLabel(node, false)}</Tag>
                <span>{node.token_count ?? 0} tok</span>
                <Typography.Text ellipsis style={{ maxWidth: 260 }}>
                  {node.heading_path || `#${node.seq}`}
                </Typography.Text>
              </Space>
              <pre
                style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxHeight: '65vh',
                  overflow: 'auto',
                  padding: 12,
                  background: '#fafafa',
                  border: '1px solid #f0f0f0',
                  fontSize: 12,
                  margin: 0,
                }}
              >
                {node.content}
              </pre>
            </div>
          ))}
        </div>
      </Modal>

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
