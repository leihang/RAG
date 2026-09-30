/**
 * 数据访问日志（05 §4.12）—— 路由 `/logs/doc-access`，权限点 `audit:trace:read`。
 *
 * **只读**：审计员是纯只读角色（方案 §1.2），本页没有任何写操作按钮。
 *
 * 按后端实测（`ekbs/api/admin.py:218-255`）：
 *
 * * 返回体是 `{"items": [...], "limit", "offset"}` —— **没有 `total`**。
 * * 行字段是 `log_id / user_id / doc_id / action / channel / ip / ua / result / created_at`。
 * * `action` 实际取值 `view | download | cite_click | upload`；`result` 实际取值
 *   `ok | denied`（`ekbs/audit.py:157` 的唯一写入口，`assets.py` 只写这两个值）。
 *   设计稿把结果写成 `allow | deny`，本页两种取值都按「allow=成功色 / deny=红色」上色。
 * * 过滤参数只有 `doc_id`，「结果」与「用户 ID」后端都不支持 —— 结果做本地过滤并明示，
 *   用户 ID 不提供控件（宁可不给，也不给一个点了没用的框）。
 */
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Alert, Space, Tag, Tooltip, Typography } from 'antd'
import { WarningOutlined } from '@ant-design/icons'
import type { ProColumns } from '@ant-design/pro-components'
import { ProTable } from '@ant-design/pro-components'
import { listDocAccess } from '@/api/log'
import EmptyState from '@/components/EmptyState'
import { formatTime } from '@/utils/format'
import {
  DEFAULT_PAGE_SIZE,
  DOC_ACCESS_FILTER_NOTE,
  DOC_ACCESS_RESULTS,
  PAGE_SIZE_OPTIONS,
  accessResultOfDoc,
  actionLabel,
  matchDocAccessResult,
  textOrDash,
  type DocAccessRow,
} from './qaLogHelpers'

interface DocAccessParams {
  doc_id?: number
  _result?: string
  [key: string]: unknown
}

/** 结果标签：allow=成功色、deny=红色（方案 §5.1）。 */
function ResultTag({ result }: { result?: string }) {
  const meta = accessResultOfDoc(result)
  if (meta.tone === 'error') {
    return (
      <Tag color="red" icon={<WarningOutlined />}>
        {meta.text}
      </Tag>
    )
  }
  if (meta.tone === 'success') return <Tag color="green">{meta.text}</Tag>
  if (meta.tone === 'warning') return <Tag color="orange">{meta.text}</Tag>
  return <Tag>{meta.text}</Tag>
}

const columns: ProColumns<DocAccessRow>[] = [
  {
    title: '时间',
    dataIndex: 'created_at',
    width: 180,
    hideInSearch: true,
    render: (_, row) => formatTime(row.created_at),
  },
  {
    title: '用户 id',
    dataIndex: 'user_id',
    width: 100,
    hideInSearch: true, // 后端只接受 doc_id 过滤，不给一个点了没用的输入框
    render: (_, row) => textOrDash(row.user_id),
  },
  {
    title: '文档 id',
    dataIndex: 'doc_id',
    width: 110,
    valueType: 'digit',
    render: (_, row) =>
      row.doc_id != null ? <Link to={`/docs/${row.doc_id}`}>{row.doc_id}</Link> : '-',
  },
  {
    title: '动作',
    dataIndex: 'action',
    width: 120,
    hideInSearch: true,
    render: (_, row) => actionLabel(row.action),
  },
  {
    title: '结果',
    dataIndex: 'result',
    width: 150,
    valueEnum: Object.fromEntries(DOC_ACCESS_RESULTS.map((o) => [o.value, { text: o.label }])),
    render: (_, row) => <ResultTag result={row.result} />,
  },
  {
    title: '渠道',
    dataIndex: 'channel',
    width: 120,
    hideInSearch: true,
    render: (_, row) => textOrDash(row.channel),
  },
  {
    title: '来源 IP',
    dataIndex: 'ip',
    width: 150,
    hideInSearch: true,
    render: (_, row) => textOrDash(row.ip),
  },
  {
    title: 'UA',
    dataIndex: 'ua',
    width: 220,
    ellipsis: true,
    hideInSearch: true,
    render: (_, row) => (
      <Tooltip title={row.ua ? String(row.ua) : undefined}>
        <Typography.Text className={row.ua ? 'ekbs-ellipsis-2' : undefined}>
          {textOrDash(row.ua)}
        </Typography.Text>
      </Tooltip>
    ),
  },
]

export default function DocAccessList() {
  const local = useRef<{ result: string }>({ result: '' })
  const [localResult, setLocalResult] = useState(false)

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="关于筛选与分页总数"
        description={
          <Space direction="vertical" size={0}>
            <span>{DOC_ACCESS_FILTER_NOTE}</span>
            <span>
              该接口不返回 total（只回 items / limit / offset），故「本页 N 条」是按当前页条数显示的
              下限，不是真实总数。
            </span>
          </Space>
        }
      />
      <ProTable<DocAccessRow, DocAccessParams>
        rowKey={(row) => String(row.log_id ?? `${row.user_id}-${row.doc_id}-${row.created_at}`)}
        headerTitle="数据访问日志"
        columns={columns}
        cardBordered
        scroll={{ x: 'max-content' }}
        options={{ density: false, fullScreen: true, reload: true, setting: true }}
        search={{ labelWidth: 'auto', defaultCollapsed: false, searchText: '查询', resetText: '重置' }}
        form={{
          onFinish: (values) => {
            const v = values as { _result?: string }
            local.current = { result: v._result ?? '' }
            setLocalResult(Boolean(v._result))
          },
          onReset: () => {
            local.current = { result: '' }
            setLocalResult(false)
          },
        }}
        pagination={{
          defaultPageSize: DEFAULT_PAGE_SIZE,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showSizeChanger: true,
          showQuickJumper: true,
          showTotal: (total) => `本页 ${total} 条`,
        }}
        request={async (params) => {
          // ⚠️ src/api/log.ts 里 listDocAccess 的入参类型只声明了 limit/offset，
          // 但后端 handler（admin.py:218-227）确实接受 doc_id 查询参数。
          // 共享层的类型缺口，本页不擅自改共享层，就地补声明并留档。
          const res = await listDocAccess({
            limit: params.pageSize ?? DEFAULT_PAGE_SIZE,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? DEFAULT_PAGE_SIZE),
            doc_id: params.doc_id,
          } as { limit?: number; offset?: number; doc_id?: number })
          const items = (res.items ?? []) as unknown as DocAccessRow[]
          return { data: items, total: res.total ?? items.length, success: true }
        }}
        postData={(data: DocAccessRow[]) => {
          const { result } = local.current
          if (!result) return data
          return data.filter((row) => matchDocAccessResult(row, result))
        }}
        toolBarRender={false}
        locale={{
          emptyText: <EmptyState description="还没有数据访问记录，被查看或下载的文档会记录在这里" />,
        }}
      />
      {localResult ? (
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
          已在当前页按所选结果做本地过滤，未影响后端查询。
        </Typography.Text>
      ) : null}
    </>
  )
}
