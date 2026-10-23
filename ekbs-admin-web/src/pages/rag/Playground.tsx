/**
 * 检索调试台（方案 05 §4.7）。
 *
 * 这是权限与质量的红线所在（§1.5 把「检索调试台」列为自研 6 之一）：
 * 逐分片稠密召回 / 稀疏召回 / RRF / rerank / 父块扩展的全过程都能看到，
 * 参数覆盖**不改全局配置**（后端把 override 放在请求作用域内，并发安全）。
 * 权限模拟是唯一允许越权读取的入口，仅 `rag:playground:simulate` 可见（§3.4 约束 3）。
 *
 * 多轮：页面在 sessionStorage 里持有 `conversation_id`，连续提问走查询改写 + 会话上下文；
 * 「新会话」会换一个 id（不调 clear API，避免误清正式会话）。
 */
import { useEffect, useState } from 'react'
import {
  Alert, Button, Card, Descriptions, Form, Input, InputNumber, Radio, Select, Space, Switch, Table, Tag, Typography, message,
} from 'antd'
import Auth from '@/components/Auth'
import ChunkPeekDrawer from '@/components/ChunkPeekDrawer'
import ClearanceTag from '@/components/ClearanceTag'
import StagesPanel from './StagesPanel'
import FigureThumb from '@/components/FigureThumb'
import { faithfulnessBanner, parseStages } from './playgroundData'
import { listDocuments } from '@/api/doc'
import { fetchDiscoveryCatalog, type DiscoveryCatalog } from '@/api/discovery'
import { personalOwnerText } from '@/utils/personalKb'
import { searchDocumentChunks } from '@/api/parse'
import type { ChunkNode, DocumentItem } from '@/api/types'
import { listUsers } from '@/api/iam'
import { debugQuery, debugShards, debugSimulate, type DebugQueryResp, type Citation, type SimulateResp } from '@/api/rag'
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

const CONV_KEY = 'ekbs:playground:conversation_id'

function newConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `pg-${crypto.randomUUID()}`
  }
  return `pg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function loadOrCreateConversationId(): string {
  try {
    const existing = sessionStorage.getItem(CONV_KEY)
    if (existing) return existing
  } catch {
    /* private mode */
  }
  const id = newConversationId()
  try {
    sessionStorage.setItem(CONV_KEY, id)
  } catch {
    /* ignore */
  }
  return id
}

export default function Playground() {
  const [form] = Form.useForm()
  const [simForm] = Form.useForm()
  const [docSearchForm] = Form.useForm()
  const [result, setResult] = useState<DebugQueryResp | null>(null)
  const [shards, setShards] = useState<ShardInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [conversationId, setConversationId] = useState(loadOrCreateConversationId)
  const [peekChunkId, setPeekChunkId] = useState<number | null>(null)
  const [docOptions, setDocOptions] = useState<DocumentItem[]>([])
  const [docHits, setDocHits] = useState<ChunkNode[]>([])
  const [docSearchLoading, setDocSearchLoading] = useState(false)
  const [catalog, setCatalog] = useState<DiscoveryCatalog | null>(null)
  const [simResult, setSimResult] = useState<SimulateResp | null>(null)
  const [simUsers, setSimUsers] = useState<{ value: number; label: string }[]>([])

  useEffect(() => {
    void listDocuments({ limit: 100 })
      .then((page) => setDocOptions(page.items ?? []))
      .catch(() => { /* 无权限时文档块检索仍可手填 doc_id */ })
    void fetchDiscoveryCatalog({ doc_limit: 20, suggestion_limit: 5 })
      .then((body) => setCatalog(body))
      .catch(() => { /* 发现层失败不挡主流程 */ })
    void listUsers({ limit: 2000 })
      .then((page) => {
        const options = (page.items ?? [])
          .filter((user) => user.status !== 'left')
          .map((user) => ({
            value: user.user_id,
            label: `${user.nick || user.username || '未命名'} · #${user.user_id}${user.title ? ` · ${user.title}` : ''}`,
          }))
        setSimUsers(options)
      })
      .catch(() => { /* 无用户列表权限时仍可手填 ID */ })
  }, [])

  const openChunk = (chunkId: number) => setPeekChunkId(chunkId)

  const resetConversation = () => {
    const id = newConversationId()
    try {
      sessionStorage.setItem(CONV_KEY, id)
    } catch {
      /* ignore */
    }
    setConversationId(id)
    setResult(null)
    message.success('已开启新会话（上一轮上下文不再带入）')
  }

  const runQuery = async (query: string) => {
    const q = query.trim()
    if (!q) {
      message.error('请输入问题')
      return
    }
    form.setFieldsValue({ query: q })
    const v = form.getFieldsValue()
    setLoading(true)
    try {
      setResult(await debugQuery({
        query: q,
        mode: v.mode ?? 'prose',
        conversation_id: conversationId,
        rerank_enabled: Boolean(v.rerank_enabled),
        dense_top_k: v.top_k ?? undefined,
        rerank_keep: v.keep ?? undefined,
        rerank_min_score: v.min_score ?? undefined,
      }))
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const run = async () => {
    const v = await form.validateFields()
    await runQuery(String(v.query || ''))
  }

  const runChip = (question: string) => {
    void runQuery(question)
  }

  const loadShards = async () => {
    try { setShards(await debugShards()) }
    catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const runSimulate = async () => {
    const v = await simForm.validateFields()
    setLoading(true)
    try {
      const body = await debugSimulate({
        query: String(v.query),
        user_id: Number(v.user_id),
        reason: String(v.reason),
      })
      setSimResult(body)
      message.success(`已按用户 #${body.simulated_user} 检索，命中 ${body.chunks.length} 块`)
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const runDocChunkSearch = async () => {
    const v = await docSearchForm.validateFields()
    const docId = Number(v.doc_id)
    if (!Number.isFinite(docId) || docId <= 0) {
      message.error('请选择或填写文档 ID')
      return
    }
    setDocSearchLoading(true)
    try {
      const resp = await searchDocumentChunks(docId, { q: v.q || '', limit: 50 })
      setDocHits(resp.chunks ?? [])
      if (!(resp.chunks ?? []).length) message.info('该文档没有匹配的分块')
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setDocSearchLoading(false) }
  }

  /** 忠实度卡片口径（纯函数，见 playgroundData.faithfulnessBanner） */
  const banner = faithfulnessBanner({
    ok: result?.faithfulness?.ok,
    refused: result?.refused,
    reason: result?.faithfulness?.reason,
    unsupported: result?.faithfulness?.unsupported,
    citedIndices: result?.faithfulness?.cited_indices,
    claimCount: result?.faithfulness?.claims?.length,
  })

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="检索并生成">
        <Form form={form} layout="vertical" initialValues={{ rerank_enabled: true }}>
          <Form.Item name="query" label="问题" rules={[{ required: true, message: '请输入问题' }]}>
            <Input.TextArea rows={2} placeholder="如：年假有多少天；下一轮可追问「能介绍详细点吗？」" />
          </Form.Item>
          {/* D22-P0：空态示例问 chips —— 尚无结果时展示，点击即填入并跑 /rag/debug/query */}
          {!result && (catalog?.suggestions?.length ?? 0) > 0 ? (
            <div className="ekbs-discovery-chips" style={{ marginBottom: 12 }}>
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
                试试这些问题（按你当前可见文档过滤）：
              </Typography.Text>
              <Space size={[8, 8]} wrap>
                {catalog!.suggestions.map((s) => (
                  <Button
                    key={s.question}
                    size="small"
                    disabled={loading}
                    onClick={() => runChip(s.question)}
                  >
                    {s.question}
                  </Button>
                ))}
              </Space>
            </div>
          ) : null}
          <Space size={16} wrap align="end">
            {/*
              作答模式：管理台手动切换。prose=业务问答；numeric=取数（值/单位/页码 +
              抄写校验）。钉钉默认 auto（见配置 qa.auto_numeric）。
              两种模式在后端各走一条路径、各自独立缓存，互不串答案。
            */}
            <Form.Item name="mode" label="作答模式" initialValue="prose" style={{ marginBottom: 0 }}>
              <Radio.Group>
                <Radio.Button value="prose">业务问答</Radio.Button>
                <Radio.Button value="numeric">取数</Radio.Button>
              </Radio.Group>
            </Form.Item>
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
            <Button onClick={resetConversation}>新会话</Button>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
            参数覆盖只作用于本次请求，<b>不改全局配置</b>。同一会话连续提问会改写指代/追问；
            会话 id：<span className="ekbs-mono">{conversationId}</span>
          </Typography.Paragraph>
        </Form>
      </Card>

      {/* D22-P0：可见已发布文档导览（过 ACL） */}
      {catalog ? (
        <Card
          title={`可见文档（${catalog.document_count ?? catalog.documents?.length ?? 0}）`}
          size="small"
        >
          {(catalog.documents?.length ?? 0) === 0 ? (
            <Typography.Text type="secondary">当前账号没有可见的已发布文档。</Typography.Text>
          ) : (
            <ul className="ekbs-discovery-docs" style={{ margin: 0, paddingLeft: 20 }}>
              {catalog.documents.map((d) => (
                <li key={d.doc_id}>
                  <Typography.Text>{d.title || '未命名'}</Typography.Text>
                  <Typography.Text type="secondary" className="ekbs-mono" style={{ marginLeft: 8 }}>
                    #{d.doc_id}
                    {personalOwnerText(d) ? ` · ${personalOwnerText(d)}` : d.kb_code ? ` · ${d.kb_code}` : ''}
                  </Typography.Text>
                </li>
              ))}
            </ul>
          )}
        </Card>
      ) : null}

      <Card title="文档块检索">
        <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
          在指定文档内按关键词扫分块（不走全库 RAG）。点 chunk_id 可查看全文，也可跳转分块预览。
        </Typography.Paragraph>
        <Form form={docSearchForm} layout="inline" style={{ marginBottom: 12 }}>
          <Form.Item name="doc_id" rules={[{ required: true, message: '请选择文档' }]} style={{ minWidth: 280 }}>
            <Select
              showSearch
              allowClear
              placeholder="选择文档"
              optionFilterProp="label"
              options={docOptions.map((d) => ({
                value: d.doc_id,
                label: `${d.title || '未命名'} (#${d.doc_id})`,
              }))}
            />
          </Form.Item>
          <Form.Item name="q" style={{ minWidth: 220 }}>
            <Input placeholder="关键词（可空=浏览前 50 块）" allowClear />
          </Form.Item>
          <Button type="primary" loading={docSearchLoading} onClick={() => void runDocChunkSearch()}>
            检索文档块
          </Button>
        </Form>
        {docHits.length > 0 ? (
          <Table<ChunkNode>
            size="small"
            rowKey="chunk_id"
            pagination={{ pageSize: 10 }}
            dataSource={docHits}
            columns={[
              {
                title: 'chunk',
                dataIndex: 'chunk_id',
                width: 90,
                render: (id: number) => (
                  <Typography.Link onClick={() => openChunk(id)} className="ekbs-mono">{id}</Typography.Link>
                ),
              },
              { title: '类型', dataIndex: 'chunk_type', width: 80 },
              { title: '标题路径', dataIndex: 'heading_path', ellipsis: true },
              {
                title: '摘要',
                dataIndex: 'content_head',
                ellipsis: true,
                render: (_: unknown, row: ChunkNode) => row.content_head || row.content?.slice(0, 120) || '-',
              },
              {
                title: '多模态',
                width: 110,
                render: (_: unknown, row: ChunkNode) =>
                  row.chunk_type === 'figure' ? (
                    row.figure_mm_indexed ? <Tag color="purple">已索引</Tag> : <Tag color="orange">未索引</Tag>
                  ) : (
                    '-'
                  ),
              },
            ]}
          />
        ) : null}
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
                    {(result.suggested_questions?.length ?? 0) > 0 ? (
                      <div style={{ marginTop: 8 }}>
                        <div>相关示例问（点击重试）：</div>
                        <Space size={[8, 8]} wrap style={{ marginTop: 6 }}>
                          {result.suggested_questions!.map((q) => (
                            <Button key={q} size="small" onClick={() => runChip(q)}>
                              {q}
                            </Button>
                          ))}
                        </Space>
                      </div>
                    ) : null}
                  </>
                }
              />
            ) : null}
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginTop: 12 }}>
              {result.answer ?? '（后端未返回答案）'}
            </Typography.Paragraph>
            <Space size={8} wrap>
              {result.model ? <Tag>模型 {result.model}</Tag> : null}
              {result.history_used ? <Tag color="blue">已用会话改写</Tag> : <Tag>单轮 / 未改写</Tag>}
              {result.rewritten_query && result.rewritten_query !== result.query ? (
                <Tag color="purple">检索串：{result.rewritten_query}</Tag>
              ) : null}
              {result.timings_ms ? Object.entries(result.timings_ms).map(([k, v]) => (
                <Tag key={k}>{k} {formatMs(v)}</Tag>
              )) : null}
            </Space>
          </Card>

          {result.numeric ? (
            <Card title="取数结果（mode=numeric）" style={{ marginTop: 12 }}>
              <Alert
                type={result.numeric.verified ? 'success' : 'warning'}
                showIcon
                message={
                  result.numeric.verified
                    ? `已通过抄写校验：${result.numeric.value ?? '-'} ${result.numeric.unit ?? ''}`
                    : (result.refusal_reason === 'numeric_na'
                        ? '返回 N/A（资料未直接给出该指标）'
                        : '未通过抄写校验（值不在引用片段里，按拒答处理）')
                }
                description={
                  <Space direction="vertical" size={2}>
                    <span>指标：{result.numeric.indicator || '-'}　页码：{result.numeric.page ?? '-'}</span>
                    {result.numeric.scope ? <span>口径：{result.numeric.scope}</span> : null}
                    {result.numeric.quote ? <span>依据片段：{result.numeric.quote}</span> : null}
                    {result.numeric.reason ? <span>说明：{result.numeric.reason}</span> : null}
                    {/*
                      口径歧义必须显式摊开：同一行标签在别处还有不同数值（实测 2024年 vs 2026年
                      两个值都真实存在、都能过抄写校验），用户得自己确认要哪一行。
                    */}
                    {(result.numeric.alternatives?.length ?? 0) > 0 ? (
                      <span style={{ color: '#d46b08' }}>
                        另有同口径行的其它数值（请确认要哪一行）：
                        {result.numeric.alternatives!.map((a) => (
                          <Tag key={`${a.chunk_id}-${a.value}`} color="orange" style={{ marginLeft: 6 }}>
                            {a.label} {a.value}（{a.doc_title}
                            {a.page ? ` 第${a.page}页` : ''}）
                          </Tag>
                        ))}
                      </span>
                    ) : null}
                  </Space>
                }
              />
            </Card>
          ) : null}

          {result.faithfulness ? (
            <Card title="忠实度">
              {/*
                分级呈现（与后端 P0 策略一致）：
                * 通过 → 绿；
                * **降级放行**（规则检查未过，但没有硬拒：答案与引用都还在）→ 黄。
                  这一类是「数字/量词没能在原文里逐字核对上」，实测表格汇总题会命中
                  （模型做了单位换算/格式化），旧口径直接整答拒答，把好答案打没了；
                * 已拒答 → 红。
              */}
              <Alert
                type={banner?.type ?? 'error'}
                showIcon
                message={banner?.message ?? '未通过'}
                description={banner?.detail ?? null}
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
                {
                  title: 'chunk',
                  dataIndex: 'chunk_id',
                  width: 100,
                  render: (id: number) => (
                    <Typography.Link onClick={() => openChunk(id)} className="ekbs-mono">{id}</Typography.Link>
                  ),
                },
                { title: '页', dataIndex: 'page_from', width: 70, render: (v) => v ?? '-' },
                { title: '标题路径', dataIndex: 'heading_path', ellipsis: true },
                { title: 'ACL', dataIndex: 'acl_result', width: 90,
                  render: (v) => (v === 'pass' ? <Tag color="green">pass</Tag> : <Tag>{String(v ?? '-')}</Tag>) },
                { title: '页码校验', dataIndex: 'page_check', width: 100,
                  render: (v) => (v === 'pass' ? <Tag color="green">pass</Tag> : <Tag>{String(v ?? '-')}</Tag>) },
                // 图片块被召回时把原图显示出来（调试台里"命中了一张图"要能当场看见）
                { title: '图', dataIndex: 'figure_id', width: 110,
                  render: (_, r: Citation) => (
                    r.figure_id || r.image_path
                      ? <FigureThumb chunkId={r.chunk_id} title={r.figure_id || r.image_path || undefined} size={72} />
                      : '-'
                  ) },
                {
                  title: '多模态',
                  width: 100,
                  render: (_: unknown, r: Citation) =>
                    r.figure_id || r.image_path ? (
                      r.figure_mm_indexed ? <Tag color="purple">已索引</Tag> : <Tag color="orange">未索引</Tag>
                    ) : (
                      '-'
                    ),
                },
              ]}
            />
          </Card>

          {(result.figure_attachments?.length ?? 0) > 0 ? (
            <Card
              title={`回源图片（${result.figure_attachments!.length}）`}
              style={{ marginTop: 12 }}
              extra={result.intent === 'figure' ? <Tag color="blue">找图意图</Tag> : null}
            >
              <Space size={16} wrap>
                {result.figure_attachments!.map((fig) => (
                  <div key={`${fig.chunk_id}-${fig.image_path || fig.figure_id || ''}`} style={{ textAlign: 'center' }}>
                    <FigureThumb
                      chunkId={fig.chunk_id}
                      title={fig.figure_id || fig.doc_title || undefined}
                      size={160}
                    />
                    <div style={{ marginTop: 6, maxWidth: 160 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        #{fig.chunk_id}{fig.doc_title ? ` · ${fig.doc_title}` : ''}
                      </Typography.Text>
                    </div>
                  </div>
                ))}
              </Space>
            </Card>
          ) : null}

          <Card title="逐环节明细">
            {/* ⚠️ 必须过 `parseStages`：后端给的是 snake_case（`per_shard`），
                面板读的是归一化后的 `perShard`。原先这里用类型断言把不匹配压了下去，
                运行时就炸在 `dense.perShard.reduce`（用户实测：TypeError: undefined
                is not an object）。这类断言没有正当用途，已由
                `tests/test_admin_ui_wiring.py` 全局禁掉。 */}
            <StagesPanel stages={parseStages(result.stages)} onOpenChunk={openChunk} />
          </Card>
        </>
      ) : null}

      <ChunkPeekDrawer
        chunkId={peekChunkId}
        open={peekChunkId != null}
        onClose={() => setPeekChunkId(null)}
      />

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
          用被测用户的部门、密级和点名权限做检索。上方「检索并生成」走的是当前登录身份，看不到别人的部门文件。
          这里只返回检索块，不生成回答，也不会写入对方的问答记录。每次调用都会落安全事件。
        </Typography.Paragraph>
        <Form form={simForm} layout="vertical">
          <Space size={16} wrap align="start">
            <Form.Item name="user_id" label="被测用户" rules={[{ required: true, message: '请选择用户' }]} style={{ minWidth: 280, marginBottom: 0 }}>
              <Select
                showSearch
                placeholder="选择用户"
                optionFilterProp="label"
                options={simUsers}
              />
            </Form.Item>
            <Form.Item name="query" label="问题" rules={[{ required: true, message: '请输入问题' }]} style={{ minWidth: 320, marginBottom: 0 }}>
              <Input placeholder="要评估的问题" />
            </Form.Item>
            <Form.Item name="reason" label="原因" rules={[{ required: true, message: '请填写原因' }]} style={{ minWidth: 240, marginBottom: 0 }}>
              <Input placeholder="如：核对工程部能否搜到本部门文件" maxLength={200} />
            </Form.Item>
            <Form.Item label=" " style={{ marginBottom: 0 }}>
              <Button danger loading={loading} onClick={() => void runSimulate()}>以该身份检索</Button>
            </Form.Item>
          </Space>
        </Form>
        {simResult ? (
          <div style={{ marginTop: 16 }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="操作者">#{simResult.operator}</Descriptions.Item>
              <Descriptions.Item label="被测用户">#{simResult.simulated_user}</Descriptions.Item>
              <Descriptions.Item label="密级"><ClearanceTag level={simResult.simulated_perm.clearance} /></Descriptions.Item>
              <Descriptions.Item label="原因">{simResult.reason}</Descriptions.Item>
              <Descriptions.Item label="可见分片" span={2}>
                {simResult.simulated_perm.visible_shards.length
                  ? simResult.simulated_perm.visible_shards.map((shard) => <Tag key={shard} color="blue">{shard}</Tag>)
                  : '无'}
              </Descriptions.Item>
            </Descriptions>
            {simResult.refused ? (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message="这次检索没有可用结果"
                description={simResult.refusal_reason || '未返回原因'}
              />
            ) : null}
            <Table
              style={{ marginTop: 12 }}
              size="small"
              rowKey="chunk_id"
              pagination={{ pageSize: 8 }}
              dataSource={simResult.chunks}
              locale={{ emptyText: '没有命中分块' }}
              columns={[
                {
                  title: 'chunk',
                  dataIndex: 'chunk_id',
                  width: 90,
                  render: (id: number) => (
                    <Typography.Link onClick={() => openChunk(id)} className="ekbs-mono">{id}</Typography.Link>
                  ),
                },
                { title: '文档', dataIndex: 'doc_title', ellipsis: true },
                { title: '分片', dataIndex: 'shard_id', width: 160 },
                { title: '标题路径', dataIndex: 'heading_path', ellipsis: true },
                {
                  title: '精排',
                  dataIndex: 'rerank_score',
                  width: 90,
                  render: (score: number | null) => (score == null ? '-' : score.toFixed(3)),
                },
              ]}
            />
          </div>
        ) : null}
      </Card>
      </Auth>
    </Space>
  )
}
