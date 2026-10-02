/**
 * 目录导入（服务器目录 → `pending` 队列），方案里对应「导入 → 目录导入」。
 *
 * 为什么要有这一页：能力本来就在（`ekbs/api/imports.py`），但它只挂在免构建的旧单页
 * `ekbs/api/admin_ui.py` 上，而 `/admin` 已被本 SPA 顶掉 —— 接口在、界面不可达。
 *
 * 三条与后端对齐的硬约束（读 `ekbs/api/imports.py` + `ekbs/ingest/dir_import.py` 得来）：
 *
 * 1. `path` 只能是 raw 根下的**相对路径**，越界一律 403；界面逐层浏览，不让人手打 `..`。
 * 2. 扫描**不写库**；导入只把文档登记成 `pending`，解析由 worker 干，所以进度看「解析任务中心」。
 * 3. 预览最多回 200 条（`preview_limit`）。**拿预览的勾选去导入 = 只导前 200 份**，
 *    所以一旦截断，本页只允许「导入整个目录」（不传 `files`，后端自己重扫拿全量）。
 */
import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import {
  Alert,
  Breadcrumb,
  Button,
  Card,
  Input,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ArrowUpOutlined, ReloadOutlined } from '@ant-design/icons'
import { ProTable, type ProColumns } from '@ant-design/pro-components'
import { Link, useNavigate } from 'react-router-dom'
import Auth from '@/components/Auth'
import EmptyState from '@/components/EmptyState'
import { getImportMeta, getImportQueue, listImportDirs, runImport, scanImportDir } from '@/api/import'
import { listKnowledgeBases } from '@/api/doc'
import type {
  ImportDirEntry,
  ImportKbItem,
  ImportMeta,
  ImportPreviewItem,
  ImportRunResp,
  ImportScanResp,
} from '@/api/types'
import { parseDeptIds } from '@/pages/docs/docUtils'
import { formatBytes } from '@/utils/format'
import {
  actionMeta,
  defaultSelectedPaths,
  describeImportError,
  importBlockers,
  isImportable,
  previewTruncated,
} from './importModel'

/** 密级选项与上传页同一套文案（L2/L3 必须给可见部门，L4 首期不入库）。 */
const LEVEL_OPTIONS = [
  { value: 1, label: 'L1 公开（全公司可见）' },
  { value: 2, label: 'L2 内部（须指定部门）' },
  { value: 3, label: 'L3 秘密（须指定部门）' },
  { value: 4, label: 'L4 机密（首期不入库）' },
]

const LABEL_STYLE: CSSProperties = { color: 'rgba(0,0,0,.45)', fontSize: 12, marginBottom: 4 }

/** 知识库默认密级 → 导入密级初值；拿不到/越界就 L1（界面上的密级始终可改）。 */
function pickLevel(metaLevel: unknown, kbLevel: unknown): number {
  for (const raw of [metaLevel, kbLevel]) {
    const n = Number(raw)
    if (Number.isInteger(n) && n >= 1 && n <= 4) return n
  }
  return 1
}

export default function ImportDirs() {
  const navigate = useNavigate()

  const [meta, setMeta] = useState<ImportMeta | null>(null)
  const [kbs, setKbs] = useState<ImportKbItem[]>([])
  const [kbLevels, setKbLevels] = useState<Record<number, number>>({})
  const [metaLoading, setMetaLoading] = useState(true)
  const [metaError, setMetaError] = useState<string | null>(null)

  const [kbId, setKbId] = useState<number | undefined>(undefined)
  const [level, setLevel] = useState(1)
  const [deptText, setDeptText] = useState('')
  const [recursive, setRecursive] = useState(true)

  const [cwd, setCwd] = useState('')
  const [parentRel, setParentRel] = useState<string | null>(null)
  const [dirs, setDirs] = useState<ImportDirEntry[]>([])
  const [dirsLoading, setDirsLoading] = useState(false)
  const [dirsError, setDirsError] = useState<string | null>(null)

  const [scan, setScan] = useState<ImportScanResp | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])

  const [running, setRunning] = useState(false)
  const [runResp, setRunResp] = useState<ImportRunResp | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [queueCounts, setQueueCounts] = useState<Record<string, number> | null>(null)

  const deptIds = useMemo(() => parseDeptIds(deptText), [deptText])

  /** 逐层进入 raw 下的某个相对目录；失败**保持当前目录**，只把原因摆出来（404 多半是输错或该库目录还没建）。 */
  const enterDir = useCallback(async (rel: string) => {
    setDirsLoading(true)
    setDirsError(null)
    try {
      const res = await listImportDirs(rel, false)
      setCwd(String(res?.rel_path ?? rel ?? ''))
      setParentRel(res?.parent ?? null)
      setDirs(res?.dirs ?? [])
    } catch (err) {
      setDirsError(describeImportError(err))
    } finally {
      setDirsLoading(false)
    }
  }, [])

  useEffect(() => {
    void enterDir('')
  }, [enterDir])

  useEffect(() => {
    let alive = true
    void Promise.all([
      getImportMeta(),
      // `/import/meta` 的知识库行不含 `default_level`（后端当前口径），而导入密级的初值
      // 应当沿用「该库默认密级」—— 所以补一次 `/kb/knowledge-bases`。没有
      // `kb:knowledge_base:read` 或请求失败都只是拿不到初值（回落 L1），
      // 绝不把导入卡在这一个字段上（原则 P3：权威判定始终在后端）。
      listKnowledgeBases().catch(() => null),
    ])
      .then(([m, kbResp]) => {
        if (!alive) return
        const levels: Record<number, number> = {}
        for (const k of kbResp?.items ?? []) levels[k.kb_id] = Number(k.default_level)
        setMeta(m)
        setKbLevels(levels)
        const list = m.knowledge_bases ?? []
        setKbs(list)
        const first = list[0]?.kb_id
        if (first != null) {
          setKbId(first)
          setLevel(pickLevel(list[0]?.default_level, levels[first]))
        }
      })
      .catch((err) => {
        if (alive) setMetaError(describeImportError(err))
      })
      .finally(() => {
        if (alive) setMetaLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const doScan = async () => {
    setScanning(true)
    setScanError(null)
    setRunResp(null)
    setRunError(null)
    try {
      const res = await scanImportDir({ path: cwd, level, dept_ids: deptIds, recursive })
      setScan(res)
      // 默认只勾「可导入的 new」：重复/超限/不支持不勾，免得"看起来要全导"造成误操作
      setSelected(defaultSelectedPaths(res.preview))
      const blocked = importBlockers(res).length
      message.success(
        `扫描完成：共 ${res.total_files} 个文件，可导入 ${res.importable} 份${blocked ? '（存在阻断项，暂不能导入）' : ''}`,
      )
    } catch (err) {
      setScan(null)
      setSelected([])
      setScanError(describeImportError(err))
    } finally {
      setScanning(false)
    }
  }

  const doRun = async (mode: 'selected' | 'all') => {
    if (kbId == null) {
      message.warning('请先选择目标知识库')
      return
    }
    if (importBlockers(scan).length) {
      message.error('当前目录存在阻断项，不能导入；请按上方提示处理后重新扫描')
      return
    }
    const files = mode === 'selected' ? selected : []
    if (mode === 'selected' && files.length === 0) {
      message.warning('还没有勾选任何文件')
      return
    }
    setRunning(true)
    setRunError(null)
    setRunResp(null)
    try {
      const res = await runImport({ path: cwd, kb_id: kbId, level, dept_ids: deptIds, recursive, files })
      setRunResp(res)
      message.success(`已登记 ${res.queued} 份待解析文档，进度见「解析任务中心」`)
      // 队列计数只是补充信息（让操作者立刻看到 pending 有没有涨），拿不到就算了
      try {
        const q = await getImportQueue(10)
        setQueueCounts(q.counts ?? {})
      } catch {
        setQueueCounts(null)
      }
    } catch (err) {
      setRunError(describeImportError(err))
    } finally {
      setRunning(false)
    }
  }

  const kb = kbs.find((k) => k.kb_id === kbId)
  const previewRows = scan?.preview ?? []
  const counts = scan?.counts ?? {}
  const blockers = importBlockers(scan)
  const truncated = previewTruncated(scan)
  const previewByPath = useMemo(
    () => new Map(previewRows.map((r) => [String(r.rel_path), r])),
    [previewRows],
  )
  const selectedImportable = selected.filter((key) => {
    const row = previewByPath.get(key)
    return row ? isImportable(row) : false
  }).length
  const importDisabled = !scan || blockers.length > 0 || kbId == null

  /** 面包屑：每一段都可点回（用 Button 而不是 <a>，保证键盘也能走）。 */
  const crumbs = useMemo(() => {
    const segs = cwd.split('/').filter(Boolean)
    const items: { key: string; title: ReactNode }[] = [
      {
        key: '__root__',
        title: segs.length ? (
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => void enterDir('')}>
            raw 根目录
          </Button>
        ) : (
          <span>raw 根目录</span>
        ),
      },
    ]
    let acc = ''
    segs.forEach((seg, index) => {
      acc = acc ? `${acc}/${seg}` : seg
      const rel = acc
      const last = index === segs.length - 1
      items.push({
        key: rel,
        title: last ? (
          <span>{seg}</span>
        ) : (
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => void enterDir(rel)}>
            {seg}
          </Button>
        ),
      })
    })
    return items
  }, [cwd, enterDir])

  const columns: ProColumns<ImportPreviewItem>[] = [
    {
      title: '相对路径',
      dataIndex: 'rel_path',
      ellipsis: true,
      width: 420,
      render: (_, r) => (
        <Typography.Text ellipsis style={{ maxWidth: 400 }} title={String(r.rel_path)}>
          {r.rel_path}
        </Typography.Text>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 100,
      render: (_, r) => formatBytes(r.size_bytes),
    },
    {
      title: '动作',
      dataIndex: 'action',
      width: 140,
      render: (_, r) => {
        const m = actionMeta(String(r.action))
        return <Tag color={m.color}>{m.text}</Tag>
      },
    },
    {
      title: '原因',
      dataIndex: 'reason',
      ellipsis: true,
      render: (_, r) => r.reason || '-',
    },
    {
      title: '文档',
      dataIndex: 'doc_id',
      width: 90,
      render: (_, r) => (r.doc_id == null ? '-' : <Link to={`/docs/${r.doc_id}`}>#{r.doc_id}</Link>),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="目录导入只登记，不解析"
        description={
          <>
            服务器 raw 目录：
            <Typography.Text code>{meta?.raw_root ?? '-'}</Typography.Text>
            ；一次最多登记 {meta?.enqueue_limit ?? '-'} 份。文件本来就已在服务器上，
            这里只扫描 + 登记为「待解析」，真正的解析由 ekbs-worker 异步进行 ——
            进度、重跑、取消都在「解析任务中心」。
          </>
        }
      />

      {metaError ? (
        <Alert type="error" showIcon message="导入配置加载失败" description={metaError} />
      ) : null}

      <Card title="1. 目标与范围">
        <Space size={24} wrap align="start">
          <div>
            <div style={LABEL_STYLE}>目标知识库</div>
            <Select
              style={{ width: 280 }}
              value={kbId}
              loading={metaLoading}
              placeholder={metaLoading ? '正在加载知识库…' : kbs.length ? '请选择知识库' : '还没有知识库，先去上传页新建'}
              options={kbs.map((k) => ({
                value: k.kb_id,
                label: `${k.name}${k.document_count ? `（${k.document_count} 篇）` : ''}`,
                title: `code=${k.code}`,
              }))}
              onChange={(id: number) => {
                setKbId(id)
                // 换库就把密级初值跟到该库的默认密级（与上传页同一口径）
                setLevel(pickLevel(kbs.find((k) => k.kb_id === id)?.default_level, kbLevels[id]))
                setRunResp(null)
              }}
            />
          </div>

          <div>
            <div style={LABEL_STYLE}>密级</div>
            <Select
              style={{ width: 240 }}
              value={level}
              onChange={(v: number) => setLevel(v)}
              options={LEVEL_OPTIONS}
            />
          </div>

          <div>
            <div style={LABEL_STYLE}>可见部门 ID（可留空）</div>
            <Input
              style={{ width: 220 }}
              value={deptText}
              placeholder="102,103"
              onChange={(e) => setDeptText(e.target.value)}
            />
          </div>

          <div>
            <div style={LABEL_STYLE}>子目录</div>
            <Space size={8}>
              <Switch checked={recursive} onChange={setRecursive} />
              <span>递归包含子目录</span>
            </Space>
          </div>
        </Space>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
          密级 L2/L3 必须填可见部门，L3/L4 还要求服务端已开入库闸；这些条件不满足时，
          「扫描预览」会把原因列在阻断项里，导入按钮随即禁用（后端也会 409，界面先拦能少一次白跑）。
        </Typography.Paragraph>
      </Card>

      <Card
        title="2. 选择服务器目录"
        extra={
          <Space>
            <Button
              size="small"
              icon={<ArrowUpOutlined />}
              disabled={cwd === ''}
              onClick={() => void enterDir(parentRel ?? '')}
            >
              上级
            </Button>
            <Button size="small" icon={<ReloadOutlined />} loading={dirsLoading} onClick={() => void enterDir(cwd)}>
              刷新
            </Button>
            <Button
              size="small"
              disabled={!kb}
              onClick={() => void enterDir(kb?.code ?? '')}
              title="跳到 raw 下与该知识库 code 同名的目录（不存在时会提示）"
            >
              定位到知识库目录{kb ? `（${kb.code}）` : ''}
            </Button>
          </Space>
        }
      >
        <Breadcrumb items={crumbs} />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, fontSize: 12 }}>
          当前相对路径：
          <Typography.Text code>{cwd || '（raw 根目录）'}</Typography.Text>
        </Typography.Paragraph>

        {dirsError ? (
          <Alert type="warning" showIcon message="目录读取失败" description={dirsError} style={{ marginBottom: 12 }} />
        ) : null}

        <Table<ImportDirEntry>
          rowKey="rel_path"
          size="small"
          loading={dirsLoading}
          dataSource={dirs}
          pagination={false}
          locale={{
            emptyText: (
              <EmptyState
                description={
                  cwd
                    ? '这个目录下没有子目录；可以直接对本目录点「扫描预览」'
                    : 'raw 根目录下还没有子目录，先把文件放到服务器 raw 目录'
                }
              />
            ),
          }}
          columns={[
            {
              title: '子目录',
              dataIndex: 'name',
              ellipsis: true,
              render: (_, r) => (r.has_children ? `${r.name}/` : r.name),
            },
            {
              title: '含可导入文件',
              dataIndex: 'importable_files',
              width: 140,
              render: (_, r) => (r.importable_files ? `${r.importable_files} 个` : '—'),
            },
            {
              title: '操作',
              width: 100,
              render: (_, r) => (
                <Button type="link" size="small" style={{ padding: 0 }} onClick={() => void enterDir(String(r.rel_path))}>
                  进入
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title="3. 扫描与导入"
        extra={
          <Auth code="doc:document:create">
            <Space>
              <Button type="primary" loading={scanning} onClick={() => void doScan()}>
                扫描预览
              </Button>
              <Button
                loading={running}
                disabled={importDisabled || truncated || selected.length === 0}
                onClick={() => void doRun('selected')}
                title={truncated ? '预览被截断，勾选导入只会导列出的这些；请用「导入整个目录」' : undefined}
              >
                导入选中（{selected.length}）
              </Button>
              <Button loading={running} disabled={importDisabled} onClick={() => void doRun('all')}>
                导入整个目录
              </Button>
            </Space>
          </Auth>
        }
      >
        {scanError ? (
          <Alert type="error" showIcon message="扫描失败" description={scanError} style={{ marginBottom: 12 }} />
        ) : null}

        {blockers.length ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前目录不能导入"
            description={
              <>
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {blockers.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  这些是服务端的入库闸门（合规闸 / L3-L4 闸 / 密级-部门），补齐后重新扫描即可导入。
                </Typography.Text>
              </>
            }
          />
        ) : null}

        {scan?.warn?.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="提醒"
            description={
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {scan.warn.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            }
          />
        ) : null}

        {truncated ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`预览只列出前 ${previewRows.length} 条，可导入共 ${scan?.importable ?? 0} 份`}
            description="为避免「只导了前 200 份却显示已完成」，预览被截断时不能按勾选导入；请用「导入整个目录」（后端按目录重扫取全量）。"
          />
        ) : null}

        {scan ? (
          <Space size={32} wrap style={{ marginBottom: 12 }}>
            <Statistic title="文件总数" value={scan.total_files} />
            <Statistic title="可导入" value={Number(counts.new ?? scan.importable ?? 0)} />
            <Statistic title="重复（已在库）" value={Number(counts.duplicate ?? 0)} />
            <Statistic title="超过大小限制" value={Number(counts.too_large ?? 0)} />
            <Statistic title="格式不支持" value={Number(counts.unsupported ?? 0)} />
            <div>
              <div style={LABEL_STYLE}>扫描目录</div>
              <Typography.Text code>{scan.rel_dir || '（raw 根目录）'}</Typography.Text>
            </div>
          </Space>
        ) : null}

        <ProTable<ImportPreviewItem>
          headerTitle="扫描预览"
          rowKey="rel_path"
          columns={columns}
          dataSource={previewRows}
          loading={scanning}
          search={false}
          options={false}
          scroll={{ x: 1000 }}
          pagination={{ defaultPageSize: 50, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 200] }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys.map((k) => String(k))),
          }}
          tableAlertRender={({ selectedRowKeys }) => (
            <Space size={12}>
              <span>已选 {selectedRowKeys.length} 条</span>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                其中可导入 {selectedImportable} 条
              </Typography.Text>
            </Space>
          )}
          locale={{
            emptyText: (
              <EmptyState
                description="还没有扫描结果，先点右上角「扫描预览」"
                actionText="扫描预览"
                onAction={() => void doScan()}
              />
            ),
          }}
        />
      </Card>

      {runError ? <Alert type="error" showIcon message="导入失败" description={runError} /> : null}

      {runResp ? (
        <Alert
          type={runResp.failed.length ? 'error' : runResp.skipped.length ? 'warning' : 'success'}
          showIcon
          message={
            `已登记 ${runResp.queued} 份待解析文档` +
            (runResp.skipped.length ? `，跳过 ${runResp.skipped.length} 份` : '') +
            (runResp.failed.length ? `，失败 ${runResp.failed.length} 份` : '')
          }
          description={
            <>
              {runResp.skipped.length ? (
                <ul style={{ margin: '0 0 8px', paddingLeft: 20 }}>
                  {runResp.skipped.slice(0, 5).map((s) => (
                    <li key={s.rel_path}>
                      {s.rel_path}：{s.reason}
                      {s.doc_id != null ? `（已在库 #${s.doc_id}）` : ''}
                    </li>
                  ))}
                  {runResp.skipped.length > 5 ? <li>…另有 {runResp.skipped.length - 5} 条</li> : null}
                </ul>
              ) : null}
              {runResp.failed.length ? (
                <ul style={{ margin: '0 0 8px', paddingLeft: 20 }}>
                  {runResp.failed.slice(0, 5).map((f) => (
                    <li key={f.rel_path}>
                      {f.rel_path}：{f.error}
                    </li>
                  ))}
                  {runResp.failed.length > 5 ? <li>…另有 {runResp.failed.length - 5} 条</li> : null}
                </ul>
              ) : null}
              <Typography.Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
                {runResp.hint ??
                  '解析由 ekbs-worker 异步进行；进度见「解析任务中心」（status: pending → parsing → published）。'}
                {queueCounts
                  ? ` 当前队列：${Object.entries(queueCounts)
                      .map(([k, v]) => `${k} ${v}`)
                      .join(' / ') || '（空）'}`
                  : ''}
                已导入的内容再扫一次会显示为「重复」，可点「扫描预览」刷新。
              </Typography.Paragraph>
              <Button size="small" type="primary" onClick={() => navigate('/parse/tasks')}>
                去解析任务中心
              </Button>
            </>
          }
        />
      ) : null}
    </Space>
  )
}
