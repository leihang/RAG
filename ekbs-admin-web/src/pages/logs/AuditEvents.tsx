/**
 * 安全事件（05 §4.12）—— 路由 `/logs/audit`，权限点 `audit:qa:read_all`。
 *
 * **只读**：审计员是纯只读角色（方案 §1.2），本页没有任何写操作按钮 ——
 * 越权尝试 / 注入尝试 / 幻觉引用 / 内容拦截的记录只能看，不能改也不能删。
 *
 * 按后端实测（`ekbs/api/admin.py:174-215`）：
 *
 * * 返回体是 `{"items": [...], "limit", "offset"}` —— **没有 `total`**，因此分页
 *   「共 N 条」只能按当前页条数显示，页面顶部据此说明（不编造总数）。
 * * 行字段是 `event_id / event_type / user_id / severity / detail / created_at`。
 * * `severity` 的**实际取值是 `high | medium | low`**（观察 `ekbs/permission/acl.py`、
 *   `ekbs/api/qa.py`、`ekbs/retrieval/pipeline.py`、`ekbs/observability/alerts.py` 的写入点），
 *   `P1/P2` 是告警阈值表的口径、入库时被压成 high/medium。两种取值都按级别上色。
 * * 只有 `event_type` 一个过滤参数，「级别」只能本地过滤，页面已明示。
 */
import { useRef, useState } from 'react'
import { Alert, Popover, Space, Tag, Typography } from 'antd'
import {
  ExclamationCircleOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ProColumns } from '@ant-design/pro-components'
import { ProTable } from '@ant-design/pro-components'
import { listSecurityEvents } from '@/api/log'
import EmptyState from '@/components/EmptyState'
import { formatTime } from '@/utils/format'
import {
  AUDIT_SEVERITY_UNSUPPORTED_NOTE,
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  SECURITY_EVENT_TYPES,
  SECURITY_SEVERITIES,
  eventTypeLabel,
  parseDetail,
  severityOfDoc,
  textOrDash,
  type SecurityEventRow,
  type Tone,
} from './qaLogHelpers'

interface AuditListParams {
  event_type?: string
  _severity?: string
  [key: string]: unknown
}

const TONE_COLOR: Record<Tone, string | undefined> = {
  error: 'red',
  warning: 'orange',
  success: 'green',
  processing: 'blue',
  default: undefined,
}

/** 级别标签：P1/严重=红，P2/警告=橙，其他=默认（方案 §5.1 状态语义色）。 */
function SeverityTag({ severity }: { severity?: string }) {
  const meta = severityOfDoc(severity)
  if (meta.tone === 'error') {
    return (
      <Tag color={TONE_COLOR.error} icon={<ExclamationCircleOutlined />}>
        {meta.text}
      </Tag>
    )
  }
  if (meta.tone === 'warning') {
    return (
      <Tag color={TONE_COLOR.warning} icon={<WarningOutlined />}>
        {meta.text}
      </Tag>
    )
  }
  return <Tag color={TONE_COLOR[meta.tone]}>{meta.text}</Tag>
}

/** 详情可能很长：两行省略 + 「展开」Popover 看完整 JSON。 */
function DetailCell({ detail }: { detail?: string }) {
  const { text } = parseDetail(detail)
  if (text === '-') return <Typography.Text type="secondary">-</Typography.Text>
  const single = text.replace(/\s+/g, ' ').trim()
  return (
    <Popover
      trigger="click"
      placement="left"
      title="事件详情"
      overlayStyle={{ maxWidth: 560 }}
      content={
        <Typography.Paragraph
          style={{ marginBottom: 0, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap' }}
        >
          {text}
        </Typography.Paragraph>
      }
    >
      <Typography.Text className="ekbs-ellipsis-2" style={{ cursor: 'pointer' }}>
        {single}
      </Typography.Text>
    </Popover>
  )
}

const columns: ProColumns<SecurityEventRow>[] = [
  {
    title: '时间',
    dataIndex: 'created_at',
    width: 180,
    hideInSearch: true,
    render: (_, row) => formatTime(row.created_at),
  },
  {
    title: '事件类型',
    dataIndex: 'event_type',
    width: 200,
    valueType: 'select',
    valueEnum: Object.fromEntries(SECURITY_EVENT_TYPES.map((o) => [o.value, { text: o.label }])),
    render: (_, row) => <Tag>{eventTypeLabel(row.event_type)}</Tag>,
  },
  {
    title: '级别',
    dataIndex: 'severity',
    width: 140,
    valueEnum: Object.fromEntries(SECURITY_SEVERITIES.map((o) => [o.value, { text: o.label }])),
    render: (_, row) => <SeverityTag severity={row.severity} />,
  },
  {
    title: '用户 id',
    dataIndex: 'user_id',
    width: 100,
    hideInSearch: true,
    render: (_, row) => textOrDash(row.user_id),
  },
  {
    title: '详情',
    dataIndex: 'detail',
    ellipsis: true,
    hideInSearch: true,
    render: (_, row) => <DetailCell detail={row.detail} />,
  },
  {
    title: '事件 id',
    dataIndex: 'event_id',
    width: 90,
    hideInSearch: true,
    render: (_, row) => textOrDash(row.event_id),
  },
]

export default function AuditEvents() {
  const local = useRef<{ severity: string }>({ severity: '' })
  const [localSeverity, setLocalSeverity] = useState(false)

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="关于「级别」筛选与分页总数"
        description={
          <Space direction="vertical" size={0}>
            <span>{AUDIT_SEVERITY_UNSUPPORTED_NOTE}</span>
            <span>
              该接口不返回 total（只回 items / limit / offset），故「本页 N 条」是按当前页条数显示的
              下限，不是真实总数。
            </span>
          </Space>
        }
      />
      <ProTable<SecurityEventRow, AuditListParams>
        rowKey={(row) => String(row.event_id ?? `${row.event_type}-${row.created_at}-${row.user_id}`)}
        headerTitle="安全事件"
        columns={columns}
        cardBordered
        scroll={{ x: 'max-content' }}
        options={{ density: false, fullScreen: true, reload: true, setting: true }}
        search={{ labelWidth: 'auto', defaultCollapsed: false, searchText: '查询', resetText: '重置' }}
        form={{
          onFinish: (values) => {
            const v = values as { _severity?: string }
            local.current = { severity: v._severity ?? '' }
            setLocalSeverity(Boolean(v._severity))
          },
          onReset: () => {
            local.current = { severity: '' }
            setLocalSeverity(false)
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
          // ⚠️ src/api/log.ts 里 listSecurityEvents 的入参类型只声明了 limit/offset，
          // 但后端 handler（admin.py:174-183）确实接受 event_type 查询参数。
          // 这是共享层的类型缺口，本页不擅自改共享层，就地补声明并留档。
          const res = await listSecurityEvents({
            limit: params.pageSize ?? DEFAULT_PAGE_SIZE,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? DEFAULT_PAGE_SIZE),
            event_type: params.event_type,
          } as { limit?: number; offset?: number; event_type?: string })
          const items = (res.items ?? []) as unknown as SecurityEventRow[]
          return { data: items, total: res.total ?? items.length, success: true }
        }}
        postData={(data: SecurityEventRow[]) => {
          const { severity } = local.current
          if (!severity) return data
          return data.filter((row) => String(row.severity ?? '') === severity)
        }}
        toolBarRender={false}
        locale={{
          emptyText: <EmptyState description="还没有安全事件，越权尝试、注入尝试与内容拦截会记录在这里" />,
        }}
      />
      {localSeverity ? (
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
          已在当前页按所选级别做本地过滤，未影响后端查询。
        </Typography.Text>
      ) : null}
    </>
  )
}
