/**
 * 检索调试台（方案 05 §4.7）。
 *
 * 这是权限与质量的红线所在（§1.5 把「检索调试台」列为自研 6 之一）：
 * 逐分片稠密召回 / 稀疏召回 / RRF / rerank / 父块扩展的全过程都能看到，
 * 参数覆盖**不改全局配置**（后端把 override 放在请求作用域内，并发安全）。
 * 权限模拟是唯一允许越权读取的入口，仅 `rag:playground:simulate` 可见（§3.4 约束 3）。
 */
import { useState } from 'react'
import {
  Alert, Button, Card, Descriptions, Form, Input, InputNumber, Space, Switch, Table, Tag, Typography, message,
} from 'antd'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StagesPanel from './StagesPanel'
import FigureThumb from '@/components/FigureThumb'
import { parseStages } from './playgroundData'
import { debugQuery, debugShards, debugSimulate, type DebugQueryResp, type Citation } from '@/api/rag'
import { formatMs } from '@/utils/format'

interface ShardInfo {
  user_id: number
  clearance: number
  dept_chain: string[]
  visible_shards: string[]
  acl_allow_shards: string[]
  acl_deny_shards: string[]
  matched_dept_codes: string[]
  registered_shards: string[]
  warnings: string[]
}

export default function Playground() {
  const [form] = Form.useForm()
  const [simForm] = Form.useForm()
  const [result, setResult] = useState<DebugQueryResp | null>(null)
  const [shards, setShards] = useState<ShardInfo | null>(null)
  const [loading, setLoading] = useState(false)

  const run = async () => {
    const v = await form.validateFields()
    setLoading(true)
    try {
      setResult(await debugQuery({
        query: v.query,
        rerank_enabled: Boolean(v.rerank_enabled),
        top_k: v.top_k ?? undefined,
        keep: v.keep ?? undefined,
        min_score: v.min_score ?? undefined,
      }))
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const loadShards = async () => {
    try { setShards(await debugShards()) }
    catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const runSimulate = async () => {
    const v = await simForm.validateFields()
    setLoading(true)
    try {
      await debugSimulate({ query: v.query, user_id: Number(v.user_id) })
      message.success('已按该身份检索（已记入安全事件）')
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="检索并生成">
        <Form form={form} layout="vertical" initialValues={{ rerank_enabled: true }}>
          <Form.Item name="query" label="问题" rules={[{ required: true, message: '请输入问题' }]}>
            <Input.TextArea rows={2} placeholder="如：年假有多少天" />
          </Form.Item>
          <Space size={16} wrap align="end">
            <Form.Item name="rerank_enabled" label="启用精排" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Switch />
            </Form.Item>
            <Form.Item name="top_k" label="每路召回 top_k" style={{ marginBottom: 0 }}>
              <InputNumber min={1} max={200} placeholder="30" />
            </Form.Item>
            <Form.Item name="keep" label="精排保留 keep" style={{ marginBottom: 0 }}>
              <InputNumber min={1} max={50} placeholder="5" />
            </Form.Item>
            <Form.Item name="min_score" label="min_score" style={{ marginBottom: 0 }}>
              <InputNumber min={0} max={1} step={0.05} placeholder="0.60" />
            </Form.Item>
            <Button type="primary" loading={loading} onClick={() => void run()}>检索并生成</Button>
            <Button onClick={() => void loadShards()}>看权限路由</Button>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
            参数覆盖只作用于本次请求，<b>不改全局配置</b>（并发安全）。
          </Typography.Paragraph>
        </Form>
      </Card>

      {result ? (
        <>
          <Card title="答案">
            {result.refused ? (
              <Alert
                type="warning"
                showIcon
                message="已拒答（refused=true）"
                description={
                  <>
                    拒答原因：{result.refusal_reason || '（后端未给原因）'}
                    <br />
                    拒答时引用必然为空 —— 这是「没有可靠依据就不编」的硬约束。
                  </>
                }
              />
            ) : null}
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginTop: 12 }}>
              {result.answer ?? '（后端未返回答案）'}
            </Typography.Paragraph>
            <Space size={8} wrap>
              {result.model ? <Tag>模型 {result.model}</Tag> : null}
              {result.timings_ms ? Object.entries(result.timings_ms).map(([k, v]) => (
                <Tag key={k}>{k} {formatMs(v)}</Tag>
              )) : null}
            </Space>
          </Card>

          {result.faithfulness ? (
            <Card title="忠实度">
              <Alert
                type={result.faithfulness.ok ? 'success' : 'error'}
                showIcon
                message={result.faithfulness.ok ? '通过（规则型支撑检查）' : '未通过'}
                description={
                  result.faithfulness.ok
                    ? `引用编号：[${(result.faithfulness.cited_indices ?? []).join(', ')}]；可核对 claim ${result.faithfulness.claims?.length ?? 0} 条均落在入选资料中。`
                    : (
                      <>
                        原因：{result.faithfulness.reason || '（未给）'}
                        {result.faithfulness.reason === 'missing_citation'
                          ? ' — 答案无 [n]，禁止挂伪溯源引用。'
                          : null}
                        {result.faithfulness.reason === 'unsupported_claim'
                          ? ' — 答案中的数字/量词未在入选分块原文出现。'
                          : null}
                      </>
                    )
                }
              />
              {(result.faithfulness.claims?.length ?? 0) > 0 ? (
                <Table
                  style={{ marginTop: 12 }}
                  size="small"
                  pagination={false}
                  rowKey={(r) => `${r.text}-${String(r.supported)}`}
                  dataSource={result.faithfulness.claims ?? []}
                  columns={[
                    { title: 'Claim', dataIndex: 'text', ellipsis: true },
                    {
                      title: '支撑',
                      dataIndex: 'supported',
                      width: 100,
                      render: (ok: boolean) => (
                        ok
                          ? <Tag color="green">supported</Tag>
                          : <Tag color="red">unsupported</Tag>
                      ),
                    },
                  ]}
                />
              ) : (
                <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
                  本答未抽出可核对的数字/量词 claim（无则不算失败）。
                </Typography.Paragraph>
              )}
              {(result.faithfulness.unsupported?.length ?? 0) > 0 ? (
                <Alert
                  style={{ marginTop: 12 }}
                  type="error"
                  showIcon
                  message="未支撑片段"
                  description={
                    <Space size={[4, 4]} wrap>
                      {(result.faithfulness.unsupported ?? []).map((s) => (
                        <Tag key={s} color="red">{s}</Tag>
                      ))}
                    </Space>
                  }
                />
              ) : null}
            </Card>
          ) : null}

          <Card title={`引用（${result.citations?.length ?? 0}）`}>
            <Table<Citation>
              rowKey="cite_index"
              size="small"
              pagination={false}
              dataSource={result.citations ?? []}
              locale={{ emptyText: '无引用（拒答或未命中时正常为空）' }}
              columns={[
                { title: '#', dataIndex: 'cite_index', width: 50 },
                { title: '文档', dataIndex: 'doc_title', ellipsis: true },
                { title: 'chunk', dataIndex: 'chunk_id', width: 90 },
                { title: '页', dataIndex: 'page_from', width: 70, render: (v) => v ?? '-' },
                { title: '标题路径', dataIndex: 'heading_path', ellipsis: true },
                { title: 'ACL', dataIndex: 'acl_result', width: 90,
                  render: (v) => (v === 'pass' ? <Tag color="green">pass</Tag> : <Tag>{String(v ?? '-')}</Tag>) },
                { title: '页码校验', dataIndex: 'page_check', width: 100,
                  render: (v) => (v === 'pass' ? <Tag color="green">pass</Tag> : <Tag>{String(v ?? '-')}</Tag>) },
                // 图片块被召回时把原图显示出来（调试台里"命中了一张图"要能当场看见）
                { title: '图', dataIndex: 'figure_id', width: 110,
                  render: (_, r: Citation) => (r.figure_id ? <FigureThumb chunkId={r.chunk_id} title={r.figure_id} size={72} /> : '-') },
              ]}
            />
          </Card>

          <Card title="逐环节明细">
            {/* ⚠️ 必须过 `parseStages`：后端给的是 snake_case（`per_shard`），
                面板读的是归一化后的 `perShard`。原先这里用类型断言把不匹配压了下去，
                运行时就炸在 `dense.perShard.reduce`（用户实测：TypeError: undefined
                is not an object）。这类断言没有正当用途，已由
                `tests/test_admin_ui_wiring.py` 全局禁掉。 */}
            <StagesPanel stages={parseStages(result.stages)} />
          </Card>
        </>
      ) : null}

      {shards ? (
        <Card title="权限路由（当前身份）">
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="用户">{shards.user_id}</Descriptions.Item>
            <Descriptions.Item label="密级"><ClearanceTag level={shards.clearance} /></Descriptions.Item>
            <Descriptions.Item label="部门链" span={2}>{shards.dept_chain.join(' / ') || '-'}</Descriptions.Item>
            <Descriptions.Item label="可见分片" span={2}>
              {shards.visible_shards.map((s) => <Tag key={s} color="blue">{s}</Tag>)}
            </Descriptions.Item>
            <Descriptions.Item label="ACL allow" span={2}>
              {shards.acl_allow_shards?.length ? shards.acl_allow_shards.map((s) => <Tag key={s} color="green">{s}</Tag>) : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="ACL deny" span={2}>
              {shards.acl_deny_shards?.length ? shards.acl_deny_shards.map((s) => <Tag key={s} color="red">{s}</Tag>) : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="命中部门码" span={2}>{shards.matched_dept_codes?.join('、') || '-'}</Descriptions.Item>
          </Descriptions>
          {shards.warnings?.length ? (
            <Alert style={{ marginTop: 12 }} type="warning" showIcon message="警告" description={shards.warnings.join('；')} />
          ) : null}
        </Card>
      ) : null}

      {/* §3.4 约束 3：权限模拟首期仅 super_admin 可调用，无该权限点时**整个入口不渲染** */}
      <Auth code="rag:playground:simulate">
      <Card title="权限模拟检索（高危）">
        <Typography.Paragraph type="warning">
          以指定用户身份检索，是系统唯一允许越权读取的入口（默认只有超级管理员能用）。
          每次调用都会落安全事件。
        </Typography.Paragraph>
        <Form form={simForm} layout="inline">
          <Form.Item name="query" rules={[{ required: true, message: '请输入问题' }]}>
            <Input placeholder="问题" style={{ width: 260 }} />
          </Form.Item>
          <Form.Item name="user_id" rules={[{ required: true, message: '请输入用户 ID' }]}>
            <Input placeholder="用户 ID" style={{ width: 120 }} />
          </Form.Item>
          <Button danger loading={loading} onClick={() => void runSimulate()}>以该身份检索</Button>
        </Form>
      </Card>
      </Auth>
    </Space>
  )
}
