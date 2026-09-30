/**
 * 解析任务中心（方案 05 §4.5）。
 * 验收线：解析成功率 ≥98%（`success_rate_target` 由后端给）；`note` 字段是后端的诚实交代，原样展示。
 */
import { useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Card, Drawer, Space, Statistic, Tag, Typography, message } from 'antd'
import { Link } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import EmptyState from '@/components/EmptyState'
import ImpactConfirmModal from '@/pages/docs/ImpactConfirmModal'
import { cancelParseTask, getParseResult, listParseTasks, retryParseTask } from '@/api/parse'
import type { ParseTask, ParseTaskList as ParseTaskListResp } from '@/api/types'
import { parseTaskId } from './parseModel'
import { formatBytes, formatPercent, formatTime } from '@/utils/format'

export default function ParseTaskList() {
  const actionRef = useRef<ActionType>()
  const [summary, setSummary] = useState<ParseTaskListResp | null>(null)
  const [result, setResult] = useState<{ docId: number; preview: string; truncated?: boolean; note?: string; chunkCount?: number } | null>(null)
  const [pending, setPending] = useState<{ kind: 'retry' | 'cancel'; docId: number | null; title: string } | null>(null)

  const columns: ProColumns<ParseTask>[] = [
    // ⚠️ 主键是 `task_id`（后端 `SELECT d.doc_id AS task_id`），**不是** `doc_id`。
    // 原先写 dataIndex: 'doc_id' + rowKey="doc_id" ⇒ 每一行都取到 undefined：
    // 行 key 全撞在一起，点「查看结果」会请求 `/parse/tasks/undefined/result`（422，实测日志里有）。
    { title: 'ID', dataIndex: 'task_id', width: 64, search: false },
    { title: '文档名', dataIndex: 'title', ellipsis: true, render: (_, r) => <Link to={`/docs/${parseTaskId(r) ?? ''}`}>{r.title}</Link> },
    { title: '密级', dataIndex: 'level', width: 110, search: false, render: (_, r) => <ClearanceTag level={r.level} /> },
    { title: '状态', dataIndex: 'status', width: 110, valueType: 'select',
      valueEnum: { pending: { text: '待解析' }, parsing: { text: '解析中' }, published: { text: '已完成' }, failed: { text: '失败' }, offline: { text: '已下线' } },
      render: (_, r) => <StatusTag status={r.status} /> },
    { title: '页数', dataIndex: 'page_count', width: 80, search: false, render: (_, r) => (r.page_count == null ? '-' : String(r.page_count)) },
    { title: '分块', dataIndex: 'chunk_count', width: 80, search: false },
    { title: '可检索块', dataIndex: 'retrievable_count', width: 100, search: false,
      tooltip: '叶子块数（真正进了向量与 FTS 的块）。代码文档的「子块数」会是 0，看这一列才对' },
    { title: '大小', dataIndex: 'size_bytes', width: 100, search: false, render: (_, r) => formatBytes(r.size_bytes) },
    { title: '更新时间', dataIndex: 'updated_at', width: 170, search: false, render: (_, r) => formatTime(r.updated_at) },
    { title: '操作', valueType: 'option', width: 170, fixed: 'right', render: (_, r) => [
      <a key="view" onClick={() => void viewResult(parseTaskId(r))}>查看结果</a>,
      <Auth key="retry" code="parse:task:manage">
        <a onClick={() => setPending({ kind: 'retry', docId: parseTaskId(r), title: r.title })}>重跑</a>
      </Auth>,
      <Auth key="cancel" code="parse:task:manage">
        <a onClick={() => setPending({ kind: 'cancel', docId: parseTaskId(r), title: r.title })}>取消</a>
      </Auth>,
    ] },
  ]

  const viewResult = async (docId: number | null) => {
    if (docId == null) { message.error('这一行没有任务 ID，无法查看结果'); return }
    try {
      const res = await getParseResult(docId)
      // 后端返回的是 preview（不是 markdown）—— 早先按 markdown 读，抽屉里永远是空
      setResult({ docId, preview: res.preview ?? '', truncated: res.preview_truncated, note: res.note, chunkCount: res.chunk_count })
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const doAction = async () => {
    if (!pending) return
    if (pending.docId == null) { message.error('这一行没有任务 ID'); return }
    try {
      if (pending.kind === 'retry') await retryParseTask(pending.docId)
      else await cancelParseTask(pending.docId)
      message.success(pending.kind === 'retry' ? '已重跑' : '已取消')
      setPending(null)
      actionRef.current?.reload()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={32} wrap>
          <Statistic title="解析成功率" value={summary?.success_rate == null ? '-' : formatPercent(summary.success_rate)} />
          <Statistic title="验收线" value={summary?.success_rate_target == null ? '-' : formatPercent(summary.success_rate_target)} />
          <Statistic title="任务总数" value={summary?.total ?? '-'} />
          <div>
            <div style={{ color: 'rgba(0,0,0,.45)', fontSize: 14 }}>各状态</div>
            <Space size={6} wrap style={{ marginTop: 6 }}>
              {Object.entries(summary?.by_status ?? {}).map(([k, v]) => (
                <Tag key={k}>{k} {v}</Tag>
              ))}
              {Object.keys(summary?.by_status ?? {}).length === 0 ? <Typography.Text type="secondary">-</Typography.Text> : null}
            </Space>
          </div>
        </Space>
        {summary?.note ? (
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            {summary.note}
          </Typography.Paragraph>
        ) : null}
      </Card>

      <ProTable<ParseTask>
        headerTitle="解析任务中心"
        rowKey={(r) => String(parseTaskId(r) ?? r.title ?? '')}
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1250 }}
        options={{ density: true, fullScreen: true, setting: true, reload: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
        // §5.3 行高亮：解析失败行左侧红色竖条
        rowClassName={(r) => (r.status === 'failed' ? 'ekbs-row-failed' : '')}
        request={async (params) => {
          const res = await listParseTasks({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            status: params.status as string | undefined,
          })
          setSummary(res)
          return { data: res.items, total: res.total, success: true }
        }}
        locale={{ emptyText: <EmptyState description="还没有解析任务，先上传或导入文档" actionText="去上传文档" onAction={() => { window.location.hash = '#/docs/upload' }} /> }}
      />

      <Drawer
        title={result ? `解析结果预览（doc #${result.docId}）` : ''}
        width={720}
        open={result !== null}
        onClose={() => setResult(null)}
      >
        {result?.note ? <Typography.Paragraph type="secondary">{result.note}</Typography.Paragraph> : null}
        {result?.chunkCount != null ? <Typography.Paragraph type="secondary">共 {result.chunkCount} 个分块</Typography.Paragraph> : null}
        {result?.truncated ? <Typography.Paragraph type="warning">内容较长，以下仅为截断预览。</Typography.Paragraph> : null}
        <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
          {result?.preview || '（后端未返回内容预览）'}
        </pre>
      </Drawer>

      <ImpactConfirmModal
        open={pending !== null}
        level="A"
        title={pending ? (pending.kind === 'retry' ? '重跑解析' : '取消解析') : ''}
        consequence={pending ? <Typography.Text>确认对「{pending.title}」执行{pending.kind === 'retry' ? '重跑' : '取消'}？</Typography.Text> : null}
        onCancel={() => setPending(null)}
        onOk={() => void doAction()}
      />
    </Space>
  )
}
