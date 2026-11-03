/**
 * 问答日志（05 §4.12）—— 路由 `/logs/qa`，权限点 `audit:qa:read`。
 *
 * **只读**：审计员是纯只读角色（方案 §1.2），本页没有任何写操作按钮。
 *
 * 数据口径（与后端实测对齐）：
 *
 * 1. 「是否拒答」：`ekbs_qa_session` 没有拒答列，但列表接口已从 `ekbs_qa_message`
 *    汇总 `refused_count`（本会话拒答轮数），>0 显示「拒答」；旧后端缺字段时才回退
 *    「接口未提供」。
 * 2. 「最新问题」：列表接口汇总 `last_question`（最新一轮问题原文）。
 * 3. 排序按 `COALESCE(ended_at, started_at)` —— 同一会话可多轮追问，按会话开始时间
 *    排会把"刚问完但开得早"的记录压到底部。
 *
 * 筛选项「关键字」后端不支持服务端过滤（接口只有 user_id / channel / since），
 * 页面只做当前页本地过滤，并在页面上**明示**这一点。
 */
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Alert, Space, Tag, Tooltip, Typography } from 'antd'
import { WarningOutlined } from '@ant-design/icons'
import type { ProColumns } from '@ant-design/pro-components'
import { ProTable } from '@ant-design/pro-components'
import { listQaSessions } from '@/api/log'
import EmptyState from '@/components/EmptyState'
import { formatNumber, formatTime } from '@/utils/format'
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  QA_KEYWORD_UNSUPPORTED_NOTE,
  QUESTION_UNKNOWN_TEXT,
  REFUSED_META,
  matchQaKeyword,
  questionTextOf,
  refusedStateOf,
  textOrDash,
  type QaSessionRow,
} from './qaLogHelpers'

/** 查询表单值：后端支持的三个参数 + 两个仅本地生效的参数。 */
interface QaListParams {
  user_id?: number
  channel?: string
  since?: string
  _q?: string
  _refused?: string
  [key: string]: unknown
}

const columns: ProColumns<QaSessionRow>[] = [
  {
    title: '会话 id',
    dataIndex: 'session_id',
    width: 300,
    ellipsis: true,
    copyable: true,
    hideInSearch: true,
    render: (_, row) => <Typography.Text code>{textOrDash(row.session_id)}</Typography.Text>,
  },
  {
    title: '用户 id',
    dataIndex: 'user_id',
    width: 100,
    valueType: 'digit',
    render: (_, row) => textOrDash(row.user_id),
  },
  {
    title: '最新问题',
    dataIndex: 'last_question',
    width: 240,
    ellipsis: true,
    hideInSearch: true,
    render: (_, row) => {
      const text = questionTextOf(row)
      if (!text) {
        return <Typography.Text type="secondary">{QUESTION_UNKNOWN_TEXT}</Typography.Text>
      }
      return (
        <Tooltip title={text}>
          <Typography.Text>{text}</Typography.Text>
        </Tooltip>
      )
    },
  },
  {
    // 方案 §3.2：拒答率是核心指标 —— 真能判定时用 error 色醒目呈现。
    // 列表接口已从 ekbs_qa_message 汇总 refused_count（旧后端才回退 unknown）。
    title: '是否拒答',
    dataIndex: 'refused',
    width: 130,
    valueEnum: {
      yes: { text: '拒答' },
      no: { text: '已作答' },
      unknown: { text: '接口未提供' },
    },
    render: (_, row) => {
      const meta = REFUSED_META[refusedStateOf(row)]
      if (meta.tone === 'default') {
        return (
          <Tooltip title="后端列表接口尚未汇总 refused_count（旧版本），无法判定本会话是否拒答；升级后端后自动生效">
            <Tag icon={<WarningOutlined />}>{meta.text}</Tag>
          </Tooltip>
        )
      }
      return <Tag color={meta.tone === 'error' ? 'red' : 'green'}>{meta.text}</Tag>
    },
  },
  {
    title: '消息数',
    dataIndex: 'message_count',
    width: 90,
    hideInSearch: true,
    render: (_, row) => formatNumber(row.message_count),
  },
  {
    title: '时间',
    dataIndex: 'started_at',
    width: 180,
    hideInSearch: true,
    render: (_, row) => formatTime(row.started_at),
  },
  {
    title: '渠道',
    dataIndex: 'channel',
    width: 140,
    valueType: 'select',
    valueEnum: {
      dingtalk_robot: { text: 'dingtalk_robot' },
      dingtalk_h5: { text: 'dingtalk_h5' },
      web: { text: 'web' },
      api: { text: 'api' },
    },
  },
  {
    title: '操作',
    valueType: 'option',
    width: 140,
    fixed: 'right',
    render: (_, row) => [
      <Link key="detail" to={`/logs/qa/${encodeURIComponent(String(row.session_id ?? ''))}`}>
        查看对话详情
      </Link>,
    ],
  },
]

interface LocalSearch {
  q: string
  refused: string
}

export default function QaSessionList() {
  // 用 ref 保存过滤条件：搜索提交后立刻在 postData 生效，避免多一次渲染才过滤
  const local = useRef<LocalSearch>({ q: '', refused: '' })
  const [shownTotal, setShownTotal] = useState(0)
  const [localFiltering, setLocalFiltering] = useState(false)

  const applyLocal = (next: LocalSearch) => {
    local.current = next
    setLocalFiltering(Boolean(next.q) || Boolean(next.refused))
  }

  const localFilteringLabels = [
    local.current.q ? '关键字' : null,
    local.current.refused ? '是否拒答' : null,
  ]
    .filter(Boolean)
    .join('、')

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="关于「是否拒答」与「关键字」筛选"
        description={
          <Space direction="vertical" size={0}>
            <span>{QA_KEYWORD_UNSUPPORTED_NOTE}</span>
            {localFiltering ? (
              <Typography.Text type="warning">
                已启用本地过滤（{localFilteringLabels}），仅作用于当前页已加载的行，不影响共{' '}
                {shownTotal} 条的统计。
              </Typography.Text>
            ) : null}
          </Space>
        }
      />
      <ProTable<QaSessionRow, QaListParams>
        rowKey="session_id"
        headerTitle="问答日志"
        columns={columns}
        cardBordered
        scroll={{ x: 'max-content' }}
        options={{ density: false, fullScreen: true, reload: true, setting: true }}
        search={{ labelWidth: 'auto', defaultCollapsed: false, searchText: '查询', resetText: '重置' }}
        form={{
          // 查询/重置时同步本地过滤条件；后端不认这两个参数，故不放进 request
          onFinish: (values) => {
            const v = values as { _q?: string; _refused?: string }
            applyLocal({ q: (v._q ?? '').trim(), refused: v._refused ?? '' })
          },
          onReset: () => applyLocal({ q: '', refused: '' }),
        }}
        pagination={{
          defaultPageSize: DEFAULT_PAGE_SIZE,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showSizeChanger: true,
          showQuickJumper: true,
          showTotal: (total) => `共 ${total} 条`,
        }}
        request={async (params) => {
          // ⚠️ src/api/log.ts 里 listQaSessions 的入参类型只声明了 limit/offset，
          // 但后端 handler（admin.py:64-75）确实接受 user_id / channel / since 查询参数。
          // 共享层的类型缺口，本页不擅自改共享层，就地补声明并留档。
          const res = (await listQaSessions({
            limit: params.pageSize ?? DEFAULT_PAGE_SIZE,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? DEFAULT_PAGE_SIZE),
            user_id: params.user_id,
            channel: params.channel,
            since: params.since,
          } as { limit?: number; offset?: number; user_id?: number; channel?: string; since?: string })) as unknown as {
            items?: QaSessionRow[]
            total?: number
          }
          const items = res.items ?? []
          const total = res.total ?? items.length
          setShownTotal(total)
          return { data: items, total, success: true }
        }}
        postData={(data: QaSessionRow[]) => {
          // 本地过滤：后端不支持 refused / 关键字，见页面顶部说明
          const { q, refused } = local.current
          let rows = data
          if (refused) {
            rows = rows.filter((row) => refusedStateOf(row) === refused)
          }
          if (q) {
            rows = rows.filter((row) => matchQaKeyword(row, q))
          }
          return rows
        }}
        locale={{
          emptyText: <EmptyState description="还没有问答日志，等机器人产生问答后这里会自动出现记录" />,
        }}
      />
    </>
  )
}
