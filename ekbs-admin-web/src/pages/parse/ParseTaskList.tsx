/**
 * 清洗解析（原解析任务中心）。
 * 扫描件 review → 跳转双栏复核页；已完成 → 跳转分块预览。
 * **禁止**再弹 Drawer / alert 倾倒全文。
 */
import { useEffect, useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Card, Space, Statistic, Tag, Typography, message } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import EmptyState from '@/components/EmptyState'
import ResizableHeader from '@/components/ResizableHeader'
import { useResizableColumns } from '@/components/useResizableColumns'
import ImpactConfirmModal from '@/pages/docs/ImpactConfirmModal'
import {
  approveParseTask,
  approveParseTasksBatch,
  cancelParseTask,
  getParseConfig,
  getParseTask,
  listParseTasks,
  rejectParseTask,
  retryParseTask,
  vectorizeParseTask,
} from '@/api/parse'
import type { ParseTask, ParseTaskList as ParseTaskListResp } from '@/api/types'
import { canReviewTask, parseTaskId } from './parseModel'
import { formatBytes, formatDurationMs, formatPercent, formatTime } from '@/utils/format'

export default function ParseTaskList() {
  const navigate = useNavigate()
  const actionRef = useRef<ActionType>()
  const [summary, setSummary] = useState<ParseTaskListResp | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [selectedMap, setSelectedMap] = useState<Record<string, ParseTask>>({})
  const [approvingId, setApprovingId] = useState<number | null>(null)
  const [vectorizingId, setVectorizingId] = useState<number | null>(null)
  const [indexLayout, setIndexLayout] = useState<string>('single')
  const [batchRunning, setBatchRunning] = useState(false)
  const [pending, setPending] = useState<{
    kind: 'retry' | 'cancel' | 'reject'
    docId: number | null
    title: string
  } | null>(null)

  useEffect(() => {
    getParseConfig()
      .then((cfg) => setIndexLayout(String(cfg.index_layout ?? 'single')))
      .catch(() => { /* 读不到就按 single 逐篇处理 */ })
  }, [])

  const openTask = (r: ParseTask) => {
    const id = parseTaskId(r)
    if (id == null) {
      message.error('这一行没有任务 ID')
      return
    }
    if (canReviewTask(r.status)) {
      navigate(`/parse/review/${id}`)
      return
    }
    navigate(`/chunks/${id}`)
  }

  const columns: ProColumns<ParseTask>[] = [
    { title: 'ID', dataIndex: 'task_id', width: 64, search: false },
    {
      title: '文档名',
      dataIndex: 'title',
      // 默认 420，可拖拽调宽（useResizableColumns）；ellipsis 只做兜底，hover 有 tooltip。
      ellipsis: true,
      render: (_, r) => <Link to={`/docs/${parseTaskId(r) ?? ''}`}>{r.title}</Link>,
    },
    {
      title: '密级',
      dataIndex: 'level',
      width: 110,
      search: false,
      render: (_, r) => <ClearanceTag level={r.level} />,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      valueType: 'select',
      valueEnum: {
        pending: { text: '待解析' },
        parsing: { text: '解析中' },
        review: { text: '待复核' },
        chunked: { text: '已分块待入库' },
        pending_index: { text: '待向量入库' },
        indexing: { text: '索引中' },
        failed: { text: '失败' },
      },
      render: (_, r) => <StatusTag status={r.status} />,
    },
    {
      title: '页数',
      dataIndex: 'page_count',
      width: 80,
      search: false,
      render: (_, r) => (r.page_count == null ? '-' : String(r.page_count)),
    },
    { title: '分块', dataIndex: 'chunk_count', width: 80, search: false },
    {
      title: '解析耗时',
      dataIndex: 'parse_ms',
      width: 110,
      search: false,
      render: (_, r) => formatDurationMs(r.parse_ms),
    },
    {
      title: '可检索块',
      dataIndex: 'retrievable_count',
      width: 100,
      search: false,
      tooltip: '叶子块数（真正进了向量与 FTS 的块）。代码文档的「子块数」会是 0，看这一列才对',
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 100,
      search: false,
      render: (_, r) => formatBytes(r.size_bytes),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      search: false,
      render: (_, r) => formatTime(r.updated_at),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 280,
      fixed: 'right',
      render: (_, r) => {
        const id = parseTaskId(r)
        const items = [
          <a key="view" onClick={() => openTask(r)}>
            {canReviewTask(r.status) ? '查看清洗稿' : '打开分块'}
          </a>,
        ]
        if (r.status === 'published' && id != null) {
          items.push(
            <a key="compare" onClick={() => navigate(`/parse/review/${id}`)}>
              原件对照
            </a>,
          )
        }
        if (canReviewTask(r.status) && id != null) {
          items.push(
            <Auth key="approve" code="parse:task:manage">
              <a
                onClick={() => approveOne(id)}
                aria-disabled={approvingId === id}
                style={{ pointerEvents: approvingId === id ? 'none' : undefined, opacity: approvingId === id ? 0.5 : 1 }}
              >
                {approvingId === id ? '分块中…' : '确认合格并分块'}
              </a>
            </Auth>,
            <Link key="doc" to={`/docs/${id}`}>
              文档详情
            </Link>,
            <Auth key="reject" code="parse:task:manage">
              <a onClick={() => setPending({ kind: 'reject', docId: id, title: r.title })}>驳回</a>
            </Auth>,
          )
        }
        if (r.status === 'chunked' && id != null) {
          items.push(
            <Auth key="vectorize" code="parse:task:manage">
              <a
                onClick={() => vectorizeOne(id)}
                aria-disabled={vectorizingId === id}
                style={{ pointerEvents: vectorizingId === id ? 'none' : undefined, opacity: vectorizingId === id ? 0.5 : 1 }}
              >
                {vectorizingId === id ? '入库中…' : '向量入库'}
              </a>
            </Auth>,
            <Link key="doc2" to={`/docs/${id}`}>
              文档详情
            </Link>,
          )
        }
        items.push(
          <Auth key="retry" code="parse:task:manage">
            <a onClick={() => setPending({ kind: 'retry', docId: id, title: r.title })}>重跑</a>
          </Auth>,
          <Auth key="cancel" code="parse:task:manage">
            <a onClick={() => setPending({ kind: 'cancel', docId: id, title: r.title })}>取消</a>
          </Auth>,
        )
        return items
      },
    },
  ]

  const selectedRows = Object.values(selectedMap)
  const readyIds = selectedRows
    .filter((r) => r.status === 'chunked')
    .map((r) => parseTaskId(r))
    .filter((id): id is number => id != null)

  const approveOne = async (id: number) => {
    setApprovingId(id)
    try {
      await approveParseTask(id)
      message.success('已确认合格并完成分块；请勾选后做向量入库')
      actionRef.current?.reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setApprovingId(null)
    }
  }

  const vectorizeOne = async (id: number) => {
    setVectorizingId(id)
    try {
      const res = await vectorizeParseTask(id)
      message.success(
        res.status === 'pending_index'
          ? '已提交向量入库，worker 处理中'
          : `已完成向量入库（${res.vector_count ?? 0} 个向量）`,
      )
      actionRef.current?.reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setVectorizingId(null)
    }
  }

  const batchApprove = async () => {
    if (!readyIds.length) {
      message.warning(selectedRows.length ? '勾选的行里没有“已分块待入库”任务' : '请先勾选“已分块待入库”的任务')
      return
    }
    if (readyIds.length > 200) {
      message.warning('单次最多批量 200 篇，请分批提交')
      return
    }
    if (indexLayout === 'segmented') {
      try {
        const res = await approveParseTasksBatch(readyIds)
        message.success(
          `已提交向量入库 ${res.queued.length} 篇` +
            (res.skipped.length ? `，跳过 ${res.skipped.length} 篇` : ''),
        )
        setSelectedKeys([])
        setSelectedMap({})
        actionRef.current?.reload()
      } catch (err) {
        message.error(err instanceof Error ? err.message : String(err))
      }
      return
    }
    // single 布局没有批量队列：直接逐篇同步向量入库，避免先打批量接口拿 409
    setBatchRunning(true)
    let ok = 0
    const skipped: string[] = []
    for (const [i, id] of readyIds.entries()) {
      const tip = message.loading(`正在逐篇向量入库（${i + 1}/${readyIds.length}）…`, 0)
      try {
        // 选中后状态可能已变化（例如刚被单篇入库）：先查一次，非 chunked 直接跳过，
        // 避免对 published 行发 vectorize 拿 409、弹无意义的错误。
        const detail = await getParseTask(id)
        if (String(detail.task.status ?? '') !== 'chunked') {
          skipped.push(`doc ${id} 状态 ${detail.task.status}`)
          continue
        }
        await vectorizeParseTask(id)
        ok += 1
      } catch (err) {
        const text = err instanceof Error ? err.message : String(err)
        skipped.push(text)
      } finally {
        tip()
      }
    }
    setBatchRunning(false)
    if (ok || skipped.length) {
      message.success(
        `已完成 ${ok} 篇向量入库` + (skipped.length ? `，跳过 ${skipped.length} 篇（状态已变化）` : ''),
      )
    }
    setSelectedKeys([])
    setSelectedMap({})
    actionRef.current?.reload()
  }

  const doAction = async () => {
    if (!pending) return
    if (pending.docId == null) {
      message.error('这一行没有任务 ID')
      return
    }
    try {
      if (pending.kind === 'retry') await retryParseTask(pending.docId)
      else if (pending.kind === 'cancel') await cancelParseTask(pending.docId)
      else await rejectParseTask(pending.docId)
      message.success(
        pending.kind === 'retry' ? '已重跑' : pending.kind === 'cancel' ? '已取消' : '已驳回',
      )
      setPending(null)
      actionRef.current?.reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    }
  }

  // 列宽可拖拽调整并记忆：文档名默认 420，拖宽看全、拖窄给别的列让位
  const resizableColumns = useResizableColumns(columns, {
    storageKey: 'parse-tasks',
    defaults: { title: 420 },
  })

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={32} wrap>
          <Statistic
            title="解析成功率"
            value={summary?.success_rate == null ? '-' : formatPercent(summary.success_rate)}
          />
          <Statistic
            title="验收线"
            value={
              summary?.success_rate_target == null
                ? '-'
                : formatPercent(summary.success_rate_target)
            }
          />
          <Statistic title="任务总数" value={summary?.total ?? '-'} />
          <div>
            <div style={{ color: 'rgba(0,0,0,.45)', fontSize: 14 }}>各状态</div>
            <Space size={6} wrap style={{ marginTop: 6 }}>
              {Object.entries(summary?.by_status ?? {}).map(([k, v]) => (
                <Tag key={k} color={k === 'review' ? 'gold' : undefined}>
                  {k} {v}
                </Tag>
              ))}
            </Space>
          </div>
        </Space>
        {summary?.note ? (
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            {summary.note}
          </Typography.Paragraph>
        ) : null}
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          扫描件待复核：点「查看清洗稿」进入原件↔清洗稿双栏页。已完成（已发布/已下线）的文档不再出现在这里，请到「文档管理 / 分块预览」查看。
        </Typography.Paragraph>
      </Card>

      <ProTable<ParseTask>
        headerTitle="清洗解析"
        rowKey={(r) => String(parseTaskId(r) ?? r.title)}
        actionRef={actionRef}
        columns={resizableColumns}
        // 表头右边界可拖拽调宽（ResizableHeader），宽度记忆在 localStorage
        components={{ header: { cell: ResizableHeader } }}
        scroll={{ x: 'max-content' }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        rowSelection={{
          fixed: true,
          columnWidth: 48,
          preserveSelectedRowKeys: true,
          selectedRowKeys: selectedKeys,
          onChange: (keys, rows) => {
            const nextKeys = keys.map(String)
            setSelectedKeys(nextKeys)
            setSelectedMap((prev) => {
              const next = { ...prev }
              for (const row of rows) {
                const id = parseTaskId(row)
                if (id != null) next[String(id)] = row
              }
              for (const key of Object.keys(next)) {
                if (!nextKeys.includes(key)) delete next[key]
              }
              return next
            })
          },
        }}
        toolBarRender={() => [
          <Auth key="batch-approve" code="parse:task:manage">
            <Button
              type="primary"
              loading={batchRunning}
              disabled={readyIds.length === 0 || batchRunning}
              onClick={batchApprove}
            >
              批量向量入库{readyIds.length ? `（${readyIds.length}）` : ''}
            </Button>
          </Auth>,
        ]}
        request={async (params) => {
          const res = await listParseTasks({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            status: params.status as string | undefined,
          })
          setSummary(res)
          setSelectedMap((prev) => {
            let changed = false
            const next = { ...prev }
            for (const item of res.items) {
              const key = String(parseTaskId(item) ?? '')
              if (key && next[key] && next[key].status !== item.status) {
                next[key] = item
                changed = true
              }
            }
            return changed ? next : prev
          })
          return { data: res.items, total: res.total, success: true }
        }}
        rowClassName={(r) =>
          r.status === 'failed' ? 'ekbs-row-failed' : r.status === 'review' ? 'ekbs-row-review' : ''
        }
        locale={{
          emptyText: (
            <EmptyState
              description="还没有解析任务，先上传文档"
              actionText="去上传文档"
              onAction={() => navigate('/docs/upload')}
            />
          ),
        }}
      />

      <ImpactConfirmModal
        open={pending !== null}
        level="A"
        title={
          pending
            ? pending.kind === 'retry'
              ? '重跑解析'
              : pending.kind === 'cancel'
                ? '取消解析'
                : '驳回复核'
            : ''
        }
        consequence={
          pending ? (
            <Typography.Text>
              确认对「{pending.title}」执行
              {pending.kind === 'retry' ? '重跑' : pending.kind === 'cancel' ? '取消' : '驳回'}？
            </Typography.Text>
          ) : null
        }
        onCancel={() => setPending(null)}
        onOk={() => void doAction()}
      />
    </Space>
  )
}
