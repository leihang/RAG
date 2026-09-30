/**
 * 对话详情（05 §4.12）—— 路由 `/logs/qa/:sessionId`，权限点 `audit:qa:read`。
 *
 * **只读**：没有任何写操作按钮。
 *
 * 响应结构按 `ekbs/api/admin.py:125-171` 的 handler 实测确定，**不是**猜的：
 * `{"session": {...ekbs_qa_session 的行...}, "messages": [...ekbs_qa_message 的行...],
 *   "citations": [...ekbs_qa_citation 的行...]}`（注意 `src/api/log.ts` 里该函数的
 * 返回类型仍是 `Record<string, unknown>`，本页在此就地收窄）。
 *
 * 会话不存在时后端返回 `404 {"detail": "not_found"}`，此处转成明确的中文说明。
 */
import { useMemo } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Result,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ArrowLeftOutlined, WarningOutlined } from '@ant-design/icons'
import { getQaSession } from '@/api/log'
import { ApiError } from '@/api/client'
import EmptyState from '@/components/EmptyState'
import { formatNumber, formatTime } from '@/utils/format'
import QaTurnItem from './QaTurnItem'
import {
  buildQaTurns,
  sessionDetailErrorText,
  sessionMetaOf,
  textOrDash,
  type QaSessionRaw,
} from './qaLogHelpers'

export default function QaSessionDetail() {
  const { sessionId = '' } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()

  const query = useQuery({
    queryKey: ['qa-session', sessionId],
    queryFn: async () => (await getQaSession(sessionId)) as unknown as QaSessionRaw,
    enabled: Boolean(sessionId),
    retry: false,
  })

  const raw = query.data
  const meta = useMemo(() => sessionMetaOf(raw?.session), [raw])
  const turns = useMemo(
    () => buildQaTurns(raw?.messages, raw?.citations),
    [raw?.messages, raw?.citations],
  )

  const refusedTurns = turns.filter((t) => t.refused).length
  const citationCount = turns.reduce((sum, t) => sum + t.citations.length, 0)

  const backButton = (
    <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/logs/qa')}>
      返回列表
    </Button>
  )

  if (!sessionId) {
    return (
      <Result
        status="404"
        title="缺少会话 id"
        subTitle="请从问答日志列表进入对话详情"
        extra={backButton}
      />
    )
  }

  if (query.isLoading) {
    return (
      <Card>
        <Skeleton active paragraph={{ rows: 6 }} />
      </Card>
    )
  }

  if (query.isError) {
    const err = query.error as ApiError
    return (
      <Result
        status={err?.status === 404 ? '404' : 'error'}
        title={sessionDetailErrorText(err?.status, err?.message ?? '')}
        subTitle={`会话 id：${sessionId}`}
        extra={backButton}
      />
    )
  }

  return (
    <>
      <Card title="会话概览" extra={backButton} style={{ marginBottom: 16 }}>
        <Descriptions column={{ xs: 1, sm: 2, lg: 3 }} size="small">
          <Descriptions.Item label="会话 id">
            <Typography.Text copyable code>
              {textOrDash(meta.session_id || sessionId)}
            </Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="用户 id">{textOrDash(meta.user_id)}</Descriptions.Item>
          <Descriptions.Item label="钉钉 userid">
            {textOrDash(meta.dt_userid)}
          </Descriptions.Item>
          <Descriptions.Item label="渠道">{textOrDash(meta.channel)}</Descriptions.Item>
          <Descriptions.Item label="对话 id">
            {textOrDash(meta.conversation_id)}
          </Descriptions.Item>
          <Descriptions.Item label="会话密级快照">
            {meta.clearance_at != null ? <Tag color="blue">L{meta.clearance_at}</Tag> : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="开始时间">{formatTime(meta.started_at)}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{formatTime(meta.ended_at)}</Descriptions.Item>
          <Descriptions.Item label="权限指纹">
            {meta.perm_fingerprint ? (
              <Tooltip title="后端只回前 12 位（admin.py 截断），用于关联同一权限上下文">
                <Typography.Text code>{meta.perm_fingerprint}</Typography.Text>
              </Tooltip>
            ) : (
              '-'
            )}
          </Descriptions.Item>
        </Descriptions>

        <Divider style={{ margin: '12px 0' }} />

        <Space size={48} wrap>
          <Statistic title="后端记录消息数" value={meta.message_count ?? 0} />
          <Statistic title="应答轮次" value={formatNumber(turns.length)} />
          <Statistic
            title="本轮拒答"
            value={formatNumber(refusedTurns)}
            valueStyle={refusedTurns > 0 ? { color: '#cf1322' } : undefined}
          />
          <Statistic title="引用条数" value={formatNumber(citationCount)} />
        </Space>

        {meta.message_count != null && meta.message_count !== turns.length ? (
          <Alert
            style={{ marginTop: 16 }}
            type="info"
            showIcon
            message="消息数与轮次数不一致"
            description={`会话表的 message_count 为 ${meta.message_count}（写入时每轮 +2，见 ekbs/audit.py），而本轮次聚合计 ${turns.length} 轮 — 差异通常来自多轮会话被合并统计或历史数据。`}
          />
        ) : null}
      </Card>

      {turns.length ? (
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          {turns.map((turn) => (
            <QaTurnItem key={`${turn.messageId ?? turn.turn}`} turn={turn} />
          ))}
        </Space>
      ) : (
        <Card>
          <EmptyState description="该会话还没有问答消息，等机器人完成作答后这里会显示多轮对话" />
        </Card>
      )}

      {refusedTurns > 0 ? (
        <Alert
          style={{ marginTop: 8 }}
          type="warning"
          showIcon
          icon={<WarningOutlined />}
          message={`本会话有 ${refusedTurns} 轮拒答`}
          description="拒答率是管理台核心指标；按轮展示拒答原因需结合策略配置，本页只呈现后端已记录的字段。"
        />
      ) : null}
    </>
  )
}
