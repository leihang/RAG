/**
 * 文档管理列表（方案 05 §4.4）。
 * 密级徽标强制显示（§5.1/P4）；批量走 §5.5 分级确认且上限 500 条（§5.6）。
 */
import { useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Space, Tag, Typography, message } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import EmptyState from '@/components/EmptyState'
import ImpactConfirmModal from './ImpactConfirmModal'
import { normalizeBatchResult } from './docUtils'
import { batchDocuments, listDocuments, offlineDocument, publishDocument, type DocListQuery } from '@/api/doc'
import type { DocumentItem } from '@/api/types'
import { formatTime } from '@/utils/format'

const BATCH_LIMIT = 500
type BatchAction = 'publish' | 'offline' | 'delete'
const ACTION_TEXT: Record<BatchAction, string> = { publish: '批量发布', offline: '批量下线', delete: '批量删除' }

export default function DocumentList() {
  const actionRef = useRef<ActionType>()
  const navigate = useNavigate()
  const [selected, setSelected] = useState<DocumentItem[]>([])
  const [pending, setPending] = useState<{ action: BatchAction; ids: number[]; titles: string[] } | null>(null)
  const [loading, setLoading] = useState(false)

  const runConfirm = async (payload: { reason?: string }) => {
    if (!pending) return
    setLoading(true)
    try {
      const res = await batchDocuments(pending.ids, pending.action, payload.reason)
      // 后端给的是 {succeeded: 数字, results: [...]}，不是 {succeeded: [], failed: []}。
      // 以前直接读 `res.succeeded.length` ⇒ 部分失败被报成"全部已提交"。
      const outcome = normalizeBatchResult(res)
      const ok = outcome.succeeded
      const failed = outcome.failed
      if (failed.length) {
        // §5.6：部分失败必须给明细，不能只说"已提交"
        message.warning(`${ok} 项成功，${failed.length} 项失败：` + failed.slice(0, 3).map((f) => `#${f.doc_id} ${f.error}`).join('；'))
      } else message.success(`${ok} 项已提交`)
      setPending(null); setSelected([]); actionRef.current?.reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally { setLoading(false) }
  }

  const single = async (docId: number, action: 'publish' | 'offline') => {
    try {
      if (action === 'publish') await publishDocument(docId)
      else await offlineDocument(docId)
      message.success(action === 'publish' ? '已发布' : '已下线')
      actionRef.current?.reload()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const columns: ProColumns<DocumentItem>[] = [
    { title: 'ID', dataIndex: 'doc_id', width: 64, search: false },
    { title: '文档名', dataIndex: 'title', ellipsis: true,
      render: (_, row) => <Link to={`/docs/${row.doc_id}`}>{row.title}</Link> },
    // §5.1「强制」+ 原则 P4：列表里必须能看到密级
    { title: '密级', dataIndex: 'level', width: 110, valueType: 'select',
      valueEnum: { 1: { text: 'L1' }, 2: { text: 'L2' }, 3: { text: 'L3' }, 4: { text: 'L4' } },
      render: (_, row) => <ClearanceTag level={row.level} /> },
    { title: '状态', dataIndex: 'status', width: 110, valueType: 'select',
      valueEnum: { pending: { text: '待解析' }, parsing: { text: '解析中' }, published: { text: '已发布' }, failed: { text: '失败' }, offline: { text: '已下线' } },
      render: (_, row) => <StatusTag status={row.status} /> },
    { title: '页数', dataIndex: 'page_count', width: 80, search: false, render: (_, r) => (r.page_count == null ? '-' : String(r.page_count)) },
    { title: '分块', dataIndex: 'chunk_count', width: 80, search: false,
      render: (_, row) => <Link to={`/chunks/${row.doc_id}`}>{row.chunk_count ?? 0}</Link> },
    { title: '版本', dataIndex: 'doc_version', width: 70, search: false },
    { title: '知识库', dataIndex: 'kb_code', width: 100, search: false },
    { title: '更新时间', dataIndex: 'updated_at', width: 170, search: false, render: (_, r) => formatTime(r.updated_at) },
    { title: '操作', valueType: 'option', width: 170, fixed: 'right', render: (_, row) => [
      <Auth key="pub" code="doc:document:publish">{row.status === 'published' ? null : <a onClick={() => void single(row.doc_id, 'publish')}>发布</a>}</Auth>,
      <Auth key="off" code="doc:document:offline">{row.status === 'offline' ? null : <a onClick={() => void single(row.doc_id, 'offline')}>下线</a>}</Auth>,
      <a key="tree" onClick={() => navigate(`/chunks/${row.doc_id}`)}>分块</a>,
      <a key="detail" onClick={() => navigate(`/docs/${row.doc_id}`)}>详情</a>,
      // 删除是 §5.5 的 L-C 高危操作：收在最后、红色、要输入 DELETE 并填原因
      <Auth key="del" code="doc:document:delete">
        <a
          style={{ color: '#ff4d4f' }}
          onClick={() => setPending({ action: 'delete', ids: [row.doc_id], titles: [row.title] })}
        >
          删除
        </a>
      </Auth>,
    ] },
  ]

  const over = selected.length > BATCH_LIMIT
  const ask = (action: BatchAction) => setPending({ action, ids: selected.map((d) => d.doc_id), titles: selected.map((d) => d.title) })

  return (
    <>
      <ProTable<DocumentItem, DocListQuery>
        headerTitle="文档管理"
        rowKey="doc_id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1180 }}
        options={{ density: true, fullScreen: true, setting: true, reload: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
        request={async (params) => {
          const res = await listDocuments({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            status: params.status as string | undefined,
            level: params.level as number | undefined,
            q: (params as Record<string, unknown>).title as string | undefined,
          })
          return { data: res.items, total: res.total, success: true }
        }}
        rowSelection={{ selectedRowKeys: selected.map((d) => d.doc_id), onChange: (_k, rows) => setSelected(rows) }}
        tableAlertRender={({ selectedRowKeys }) => (
          <Space size={12}>
            <span>已选 {selectedRowKeys.length} 项</span>
            {over ? <Tag color="red">超过单次 {BATCH_LIMIT} 条上限，请分批</Tag> : null}
          </Space>
        )}
        tableAlertOptionRender={() => (
          <Space>
            <Auth code="doc:document:publish"><Button size="small" disabled={over} onClick={() => ask('publish')}>批量发布</Button></Auth>
            <Auth code="doc:document:offline"><Button size="small" disabled={over} onClick={() => ask('offline')}>批量下线</Button></Auth>
            <Auth code="doc:document:delete"><Button size="small" danger disabled={over} onClick={() => ask('delete')}>批量删除</Button></Auth>
          </Space>
        )}
        locale={{ emptyText: <EmptyState description="还没有文档，点击下方按钮上传第一份" actionText="去上传文档" onAction={() => navigate('/docs/upload')} /> }}
      />
      <ImpactConfirmModal
        open={pending !== null}
        // §5.5 分级：≤10 条 L-A；11–200 条 L-B；>200 条或删除 → L-C
        level={pending ? (pending.action === 'delete' || pending.ids.length > 200 ? 'C' : pending.ids.length > 10 ? 'B' : 'A') : 'A'}
        title={
          pending
            ? pending.action === 'delete' && pending.ids.length === 1
              ? `删除文档「${pending.titles[0]}」`
              : `${ACTION_TEXT[pending.action]}（${pending.ids.length} 篇）`
            : ''
        }
        consequence={pending ? (
          <Typography.Text>
            {pending.action === 'delete' ? '删除后立即停止被检索：分块、全文索引、向量、文档记录，以及磁盘上的原件与解析产物都会被删掉，需重新导入才能恢复。' : `确认${ACTION_TEXT[pending.action]}这 ${pending.ids.length} 篇文档？`}
          </Typography.Text>
        ) : null}
        impact={pending ? (
          <div>
            <div>影响文档：{pending.ids.length} 篇</div>
            <div style={{ maxHeight: 120, overflow: 'auto', marginTop: 4 }}>
              {pending.titles.slice(0, 20).map((t, i) => <div key={pending.ids[i]}>#{pending.ids[i]} {t}</div>)}
              {pending.titles.length > 20 ? <div>…等共 {pending.titles.length} 篇</div> : null}
            </div>
          </div>
        ) : null}
        confirmWord={pending?.action === 'delete' ? 'DELETE' : undefined}
        // 单篇删除时把标题也作为可接受输入（方案 §5.5「输入对象名或指定词」）；
        // 批量删除标题太多，只认 DELETE
        confirmNames={
          pending?.action === 'delete' && pending.ids.length === 1 ? pending.titles : undefined
        }
        requireReason={pending?.action === 'delete'}
        loading={loading}
        onCancel={() => setPending(null)}
        onOk={(p) => void runConfirm(p)}
      />
    </>
  )
}
