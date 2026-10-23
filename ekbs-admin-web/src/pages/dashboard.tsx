/**
 * 概览看板 `/dashboard`（方案 05 §4.2）—— 权限点 `sys:dashboard:read`。
 *
 * 数据来源（**逐条对照后端实现，不按设计稿臆造**）：
 *
 * | 接口 | 后端 | 说明 |
 * |---|---|---|
 * | `getOverview()` | `ekbs/api/admin.py::dashboard_overview` | KPI + `metrics` + `recent_alert_events` |
 * | `getHealth()` | `ekbs/api/admin.py::system_health` | 权限点 `sys:health:read`，**可能 403** → 卡片内降级 |
 * | `listShards()` | `ekbs/api/admin.py::system_shards` | 权限点 `sys:health:read` |
 * | `listAlertRules()` | `ekbs/api/admin.py::alert_rules` | 返回 `{items:[…]}`（也兼容裸数组） |
 *
 * 三处口径提醒（见 `./dashboardData.ts`）：`documents`/`shards` 是**对象**、
 * `active_alerts` 是**数组**、`recent_alert_events` 是 `[{event_id,event_type,severity,detail,created_at}]`。
 * `metrics` 为空就显示空态，绝不造假数据。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Card,
  Col,
  Progress,
  Row,
  Space,
  Statistic,
  Tooltip,
  Typography,
} from 'antd'
import { ProCard } from '@ant-design/pro-components'
import { ProDescriptions, ProTable } from '@ant-design/pro-components'
import type { ProColumns } from '@ant-design/pro-components'
import { getHealth, getOverview, listAlertRules, listShards } from '@/api/system'
import type { ShardInfo } from '@/api/types'
import { personalOwnerText } from '@/utils/personalKb'
import EmptyState from '@/components/EmptyState'
import { formatNumber, formatPercent, formatTime } from '@/utils/format'
import MetricsChart from './MetricsChart'
import {
  alertDetailText,
  diskLevel,
  diskPercentText,
  itemsOf,
  numberOf,
  readOverview,
  severityColor,
  severityText,
  stringList,
  stringOf,
  toRecord,
  type DiskLevel,
  type OverviewNumbers,
} from './dashboardData'

type Section<T> = { loading: boolean; error: string | null; data: T | null }

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return '加载失败'
}

/** 单个数据区块的加载状态：**各区块独立降级**，单接口失败不阻塞整页（§4.2 边界处理）。 */
function useSection<T>(loader: () => Promise<T>): Section<T> & { reload: () => void } {
  const [state, setState] = useState<Section<T>>({ loading: true, error: null, data: null })
  const load = useCallback(() => {
    let alive = true
    setState((prev) => ({ ...prev, loading: true }))
    loader()
      .then((data) => {
        if (alive) setState({ loading: false, error: null, data })
      })
      .catch((err: unknown) => {
        if (alive) setState({ loading: false, error: errorText(err), data: null })
      })
    return () => {
      alive = false
    }
    // loader 是模块级稳定函数，依赖仅在显式调用时变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let alive = true
    setState({ loading: true, error: null, data: null })
    loader()
      .then((data) => {
        if (alive) setState({ loading: false, error: null, data })
      })
      .catch((err: unknown) => {
        if (alive) setState({ loading: false, error: errorText(err), data: null })
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { ...state, reload: load }
}

const DISK_COLOR: Record<DiskLevel, string> = {
  normal: '#1677ff',
  warning: '#faad14',
  danger: '#ff4d4f',
}

function KpiCard({
  title,
  value,
  suffix,
  hint,
  danger,
  loading,
}: {
  title: string
  value: React.ReactNode
  suffix?: string
  hint?: React.ReactNode
  danger?: boolean
  loading?: boolean
}) {
  return (
    <ProCard size="small" bordered style={{ height: '100%' }}>
      <Statistic
        title={title}
        value={value as number}
        suffix={suffix}
        loading={loading}
        valueStyle={danger ? { color: '#ff4d4f' } : undefined}
      />
      {hint ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {hint}
        </Typography.Text>
      ) : null}
    </ProCard>
  )
}

/** 磁盘占用：≥80% 黄、≥90% 红（§4.2「存储占用」），高水位时整卡醒目提示。 */
function DiskCard({ overview, loading }: { overview: OverviewNumbers | null; loading: boolean }) {
  const pct = overview?.diskUsedPct ?? null
  const level = diskLevel(pct)
  const warn = level !== 'normal'
  return (
    <ProCard
      size="small"
      bordered
      style={
        warn
          ? { height: '100%', borderColor: DISK_COLOR[level], background: level === 'danger' ? '#fff1f0' : '#fffbe6' }
          : { height: '100%' }
      }
    >
      <Statistic
        title={
          <Space size={4}>
            <span>磁盘占用</span>
            {warn ? (
              <Typography.Text type={level === 'danger' ? 'danger' : 'warning'} style={{ fontSize: 12 }}>
                {level === 'danger' ? '已超 90%，请立即清理' : '已超 80%，请关注'}
              </Typography.Text>
            ) : null}
          </Space>
        }
        value={diskPercentText(pct)}
        loading={loading}
        valueStyle={warn ? { color: DISK_COLOR[level] } : undefined}
      />
      <Progress
        percent={pct == null ? 0 : Math.min(pct, 100)}
        size="small"
        showInfo={false}
        strokeColor={DISK_COLOR[level]}
        status={level === 'danger' ? 'exception' : 'normal'}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        后端口径：数据目录磁盘使用率（`disk_used_pct`，百分数）
      </Typography.Text>
    </ProCard>
  )
}

interface AlertEventRow {
  event_id?: number
  event_type?: string
  severity?: string
  detail?: string
  created_at?: string
}

const ALERT_EVENT_COLUMNS: ProColumns<AlertEventRow>[] = [
  { title: '事件 ID', dataIndex: 'event_id', width: 90 },
  { title: '类型', dataIndex: 'event_type', width: 140, render: (_, row) => row.event_type ?? '-' },
  {
    title: '严重度',
    dataIndex: 'severity',
    width: 100,
    render: (_, row) =>
      row.severity ? <Badge color={severityColor(row.severity)} text={severityText(row.severity)} /> : '-',
  },
  {
    title: '详情',
    dataIndex: 'detail',
    ellipsis: true,
    render: (_, row) => <span className="ekbs-ellipsis-2">{alertDetailText(row.detail)}</span>,
  },
  { title: '时间', dataIndex: 'created_at', width: 180, render: (_, row) => formatTime(row.created_at) },
]

interface AlertHitRow {
  metric?: string
  kind?: string
  value?: number
  threshold?: number
  comparison?: string
  severity?: string
  window_seconds?: number
  samples?: number
  description?: string
}

const ALERT_HIT_COLUMNS: ProColumns<AlertHitRow>[] = [
  { title: '指标', dataIndex: 'metric', width: 200, render: (_, row) => row.metric ?? '-' },
  {
    title: '当前值',
    dataIndex: 'value',
    width: 120,
    render: (_, row) => (row.value == null ? '-' : String(row.value)),
  },
  {
    title: '阈值',
    dataIndex: 'threshold',
    width: 120,
    render: (_, row) => (row.threshold == null ? '-' : `${row.comparison ?? ''} ${row.threshold}`),
  },
  {
    title: '严重度',
    dataIndex: 'severity',
    width: 100,
    render: (_, row) =>
      row.severity ? <Badge color={severityColor(row.severity)} text={severityText(row.severity)} /> : '-',
  },
  {
    title: '窗口 / 样本',
    dataIndex: 'window_seconds',
    width: 140,
    render: (_, row) =>
      row.window_seconds == null ? '-' : `${row.window_seconds}s / ${row.samples ?? '-'} 样本`,
  },
  { title: '说明', dataIndex: 'description', ellipsis: true, render: (_, row) => row.description ?? '-' },
]

interface AlertRuleRow {
  name?: string
  kind?: string
  threshold?: number
  comparison?: string
  window_seconds?: number
  severity?: string
  description?: string
  sustained?: boolean
  business_hours_only?: boolean
}

const ALERT_RULE_COLUMNS: ProColumns<AlertRuleRow>[] = [
  { title: '规则', dataIndex: 'name', width: 220, render: (_, row) => row.name ?? '-' },
  {
    title: '阈值',
    dataIndex: 'threshold',
    width: 120,
    render: (_, row) => (row.threshold == null ? '-' : `${row.comparison ?? ''} ${row.threshold}`),
  },
  {
    title: '严重度',
    dataIndex: 'severity',
    width: 100,
    render: (_, row) =>
      row.severity ? <Badge color={severityColor(row.severity)} text={severityText(row.severity)} /> : '-',
  },
  {
    title: '窗口',
    dataIndex: 'window_seconds',
    width: 100,
    render: (_, row) => (row.window_seconds == null ? '-' : `${row.window_seconds}s`),
  },
  { title: '类型', dataIndex: 'kind', width: 110, render: (_, row) => row.kind ?? '-' },
  {
    title: '附加条件',
    dataIndex: 'sustained',
    width: 180,
    render: (_, row) => {
      const flags = [row.sustained ? '持续' : '', row.business_hours_only ? '仅工作时间' : ''].filter(Boolean)
      return flags.length ? flags.join(' / ') : '-'
    },
  },
  { title: '说明', dataIndex: 'description', ellipsis: true, render: (_, row) => row.description ?? '-' },
]

const SHARD_COLUMNS: ProColumns<ShardInfo>[] = [
  { title: '分片 ID', dataIndex: 'shard_id', width: 180, copyable: true },
  {
    title: '部门 / 归属',
    dataIndex: 'dept_code',
    width: 280,
    ellipsis: true,
    render: (_, row) => personalOwnerText(row) || row.dept_code || '-',
  },
  {
    title: '密级',
    dataIndex: 'level',
    width: 120,
    // 这里展示的是**分片自身**的密级（`ClearanceTag` 的 requirement），不是文档密级
    render: (_, row) => (row.level == null ? '-' : `L${row.level}`),
  },
  { title: '维度', dataIndex: 'dim', width: 90, render: (_, row) => (row.dim == null ? '-' : String(row.dim)) },
  {
    title: '向量数',
    dataIndex: 'vector_count',
    width: 120,
    render: (_, row) => formatNumber(row.vector_count),
  },
  {
    title: '状态',
    dataIndex: 'status',
    width: 120,
    render: (_, row) => {
      const status = stringOf(row.status)
      if (status == null) return '-'
      const ok = status.toLowerCase() === 'active'
      return <Badge status={ok ? 'success' : 'default'} text={status} />
    },
  },
]

interface ShardCheck {
  shard_id: string
  ok: boolean
  detail: string
}

const SHARD_CHECK_COLUMNS: ProColumns<ShardCheck>[] = [
  { title: '分片 ID', dataIndex: 'shard_id', width: 180 },
  {
    title: '自检',
    dataIndex: 'ok',
    width: 120,
    render: (_, row) => <Badge status={row.ok ? 'success' : 'error'} text={row.ok ? '正常' : '异常'} />,
  },
  { title: '详情', dataIndex: 'detail', ellipsis: true, render: (_, row) => row.detail || '-' },
]

export default function Dashboard() {
  const overview = useSection(getOverview)
  const health = useSection(getHealth)
  const shards = useSection(listShards)
  const alertRules = useSection(listAlertRules)

  const raw = overview.data
  const numbers = raw ? readOverview(raw) : null
  const metrics = numbers?.metrics ?? {}
  const hasMetrics = Object.keys(metrics).length > 0
  const recentEvents = numbers?.recentAlertEvents ?? []
  const activeAlertList = numbers?.activeAlertRows ?? []

  // 平台健康（`/system/health`）只在字段存在时展示，缺字段显示 `-`
  const healthRows = (() => {
    const record = toRecord(health.data)
    if (!record) return []
    const schema = toRecord(record.schema)
    const embedding = toRecord(record.embedding)
    const gates = toRecord(record.gates)
    const rows: { key: string; label: string; value: React.ReactNode }[] = []
    const push = (key: string, label: string, value: React.ReactNode) => rows.push({ key, label, value })

    push('overall', '整体', <Badge status="processing" text="已连通" />)
    if (schema) {
      push(
        'schema',
        'Schema 校验',
        <Badge status={schema.ok === true ? 'success' : 'error'} text={schema.ok === true ? '通过' : '不通过'} />,
      )
      const problems = stringList(schema.problems)
      if (problems.length) push('schema_problems', 'Schema 问题', problems.join('；'))
      const tables = numberOf(schema.tables)
      if (tables != null) push('tables', '表数量', formatNumber(tables))
      const triggers = numberOf(schema.triggers)
      if (triggers != null) push('triggers', '触发器数量', formatNumber(triggers))
      const policyVersion = numberOf(schema.policy_version)
      if (policyVersion != null) push('policy_version', '策略版本', String(policyVersion))
      const journal = stringOf(schema.journal_mode)
      if (journal) push('journal_mode', '日志模式', journal)
    }
    const dbReachable = record.db_reachable
    if (typeof dbReachable === 'boolean') {
      push('db', '数据库可达', <Badge status={dbReachable ? 'success' : 'error'} text={dbReachable ? '是' : '否'} />)
    }
    if (typeof record.data_dir_writable === 'boolean') {
      push(
        'data_dir',
        '数据目录可写',
        <Badge status={record.data_dir_writable ? 'success' : 'error'} text={record.data_dir_writable ? '是' : '否'} />,
      )
    }
    const healthDisk = numberOf(record.disk_used_pct)
    if (healthDisk != null) {
      const lv = diskLevel(healthDisk)
      push(
        'disk',
        '磁盘占用',
        <Typography.Text type={lv === 'danger' ? 'danger' : lv === 'warning' ? 'warning' : undefined}>
          {diskPercentText(healthDisk)}
        </Typography.Text>,
      )
    }
    if (embedding) {
      const model = stringOf(embedding.model)
      if (model) push('embed_model', '嵌入模型', model)
      const fingerprint = stringOf(embedding.fingerprint)
      if (fingerprint) push('embed_fp', '嵌入指纹', fingerprint)
      if (typeof embedding.fingerprint_match === 'boolean') {
        push(
          'embed_match',
          '指纹一致',
          <Badge
            status={embedding.fingerprint_match ? 'success' : 'error'}
            text={embedding.fingerprint_match ? '一致' : '不一致'}
          />,
        )
      }
    }
    if (gates) {
      const gateText = Object.entries(gates)
        .map(([key, value]) => `${key}=${value === true ? '开' : value === false ? '关' : String(value)}`)
        .join(' / ')
      if (gateText) push('gates', '权限闸门', gateText)
    }
    return rows
  })()

  const shardChecks: ShardCheck[] = (() => {
    const record = toRecord(health.data)
    if (!record || !Array.isArray(record.shards)) return []
    return record.shards
      .map((item) => toRecord(item))
      .filter((item): item is Record<string, unknown> => item !== null)
      .map((item) => ({
        shard_id: stringOf(item.shard_id) ?? '-',
        ok: item.ok === true,
        detail: stringOf(item.detail) ?? '',
      }))
  })()

  const ruleRows: AlertRuleRow[] = itemsOf(alertRules.data).map((row) => ({
    name: stringOf(row.name) ?? undefined,
    kind: stringOf(row.kind) ?? undefined,
    threshold: numberOf(row.threshold) ?? undefined,
    comparison: stringOf(row.comparison) ?? undefined,
    window_seconds: numberOf(row.window_seconds) ?? undefined,
    severity: stringOf(row.severity) ?? undefined,
    description: stringOf(row.description) ?? undefined,
    sustained: row.sustained === true,
    business_hours_only: row.business_hours_only === true,
  }))

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Row justify="space-between" align="middle">
        <Col>
          <Typography.Title level={4} style={{ margin: 0 }}>
            概览看板
          </Typography.Title>
          <Typography.Text type="secondary">
            数据来源：`/dashboard/overview`、`/system/health`、`/system/shards`、`/system/alert-rules`
          </Typography.Text>
        </Col>
        <Col>
          <Typography.Text type="secondary">
            指标口径来自 `ekbs_metric` 最新采集值 + 实时聚合，非实时流式
          </Typography.Text>
        </Col>
      </Row>

      {overview.error ? (
        <Alert
          type="error"
          showIcon
          message="概览指标加载失败"
          description={overview.error}
          action={<a onClick={overview.reload}>重试</a>}
        />
      ) : null}

      {/* KPI 指标卡 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="问答总数"
            value={formatNumber(numbers?.qaTotal)}
            loading={overview.loading}
            hint="`ekbs_qa_message` 中 answer 非空的消息数"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="拒答数"
            value={formatNumber(numbers?.refusedTotal)}
            loading={overview.loading}
            danger={(numbers?.refusedTotal ?? 0) > 0}
            hint="`refused = 1` 的问答消息数"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="拒答率"
            value={formatPercent(numbers?.refusalRate)}
            loading={overview.loading}
            hint="后端 `refusal_rate`（refused / qa_total）"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <DiskCard overview={numbers} loading={overview.loading} />
        </Col>

        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="文档数"
            value={formatNumber(numbers?.documents)}
            loading={overview.loading}
            hint={
              numbers && (numbers.documentsFailed != null || numbers.documentsPending != null)
                ? `已发布 ${formatNumber(numbers.documentsPublished)} / 失败 ${formatNumber(
                    numbers.documentsFailed,
                  )} / 待处理 ${formatNumber(numbers.documentsPending)}`
                : '按 status 聚合的文档数'
            }
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="分片数"
            value={formatNumber(numbers?.shards)}
            loading={overview.loading}
            hint={
              numbers?.shardVectors != null
                ? `活跃分片；向量总数 ${formatNumber(numbers.shardVectors)}`
                : '活跃分片数'
            }
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="活跃告警数"
            value={formatNumber(numbers?.activeAlerts)}
            loading={overview.loading}
            danger={(numbers?.activeAlerts ?? 0) > 0}
            hint="后端 `evaluate_all()` 当前命中的告警条数"
          />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <KpiCard
            title="近期告警事件"
            value={formatNumber(recentEvents.length)}
            loading={overview.loading}
            hint="`event_type='metric_alert'` 最近 10 条"
          />
        </Col>
      </Row>

      {/* 指标柱状图（ECharts）*/}
      <Card
        title="指标（`overview.metrics`）"
        extra={
          <Tooltip title="后端 `metrics` 是 `ekbs_metric` 中每个指标的最新值（name → value）">
            <Typography.Text type="secondary">口径说明</Typography.Text>
          </Tooltip>
        }
        loading={overview.loading}
      >
        {hasMetrics ? (
          <MetricsChart metrics={metrics} />
        ) : (
          <EmptyState description="还没有采集到指标，先让采集作业跑一轮（每 60s 写入一次）" />
        )}
      </Card>

      <Row gutter={[16, 16]}>
        {/* 当前活跃告警（数组口径）*/}
        <Col xs={24} xl={12}>
          <Card title="当前活跃告警" size="small">
            <ProTable<AlertHitRow>
              rowKey={(row) => `${row.metric ?? ''}-${row.kind ?? ''}`}
              columns={ALERT_HIT_COLUMNS}
              dataSource={activeAlertList as AlertHitRow[]}
              search={false}
              options={false}
              pagination={false}
              size="small"
              locale={{ emptyText: <EmptyState description="当前没有命中的告警阈值" /> }}
            />
          </Card>
        </Col>
        {/* 近期告警事件（事件表口径）*/}
        <Col xs={24} xl={12}>
          <Card title="近期告警事件" size="small">
            {recentEvents.length === 0 ? (
              <Typography.Text type="secondary">近期无告警事件</Typography.Text>
            ) : (
              <ProTable<AlertEventRow>
                rowKey={(row) => String(row.event_id ?? `${row.event_type}-${row.created_at}`)}
                columns={ALERT_EVENT_COLUMNS}
                dataSource={recentEvents as AlertEventRow[]}
                search={false}
                options={false}
                pagination={{ pageSize: 5, size: 'small' }}
                size="small"
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* 平台健康 */}
      <Card
        title="平台健康"
        size="small"
        extra={<Typography.Text type="secondary">权限点 `sys:health:read`</Typography.Text>}
        loading={health.loading}
      >
        {health.error ? (
          <Alert
            type="warning"
            showIcon
            message="健康信息不可用"
            description={`${health.error}（该接口需要 \`sys:health:read\`，当前身份可能未授予）`}
            action={<a onClick={health.reload}>重试</a>}
          />
        ) : healthRows.length === 0 ? (
          <EmptyState description="暂时没有拿到健康数据" />
        ) : (
          <>
            <ProDescriptions
              column={2}
              dataSource={Object.fromEntries(healthRows.map((row) => [row.key, row.value]))}
              columns={healthRows.map((row) => ({ title: row.label, dataIndex: row.key }))}
            />
            {shardChecks.length > 0 ? (
              <ProTable<ShardCheck>
                style={{ marginTop: 16 }}
                headerTitle="分片自检"
                rowKey="shard_id"
                columns={SHARD_CHECK_COLUMNS}
                dataSource={shardChecks}
                search={false}
                options={false}
                pagination={false}
                size="small"
              />
            ) : null}
          </>
        )}
      </Card>

      {/* 分片清单 */}
      <Card title="向量分片" size="small" extra={<Typography.Text type="secondary">权限点 `sys:health:read`</Typography.Text>}>
        <ProTable<ShardInfo>
          rowKey="shard_id"
          columns={SHARD_COLUMNS}
          loading={shards.loading}
          dataSource={shards.data?.items ?? []}
          search={false}
          options={false}
          pagination={{ pageSize: 20, pageSizeOptions: [20, 50, 100], size: 'small' }}
          size="small"
          locale={{
            emptyText: shards.error ? (
              <Alert type="warning" showIcon message="分片清单不可用" description={shards.error} />
            ) : (
              <EmptyState description="还没有注册的分片，先完成一次文档收录" />
            ),
          }}
        />
      </Card>

      {/* 告警阈值（只读）*/}
      <Card title="告警阈值（只读，权威定义在代码里）" size="small">
        <ProTable<AlertRuleRow>
          rowKey={(row) => row.name ?? '-'}
          columns={ALERT_RULE_COLUMNS}
          loading={alertRules.loading}
          dataSource={ruleRows}
          search={false}
          options={false}
          pagination={false}
          size="small"
          locale={{
            emptyText: alertRules.error ? (
              <Alert type="warning" showIcon message="告警阈值不可用" description={alertRules.error} />
            ) : (
              <EmptyState description="没有拿到告警阈值定义" />
            ),
          }}
        />
      </Card>

      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        数值口径：接口没有的字段一律显示 `-`，不补零、不估算。
      </Typography.Text>
    </Space>
  )
}
