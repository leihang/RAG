/**
 * 扫描件双栏复核：左原件 ↔ 右清洗稿；下方展示表格 HTML 与解析出的图片。
 * 正文里的 ``[表格 N]`` 只是占位，完整表在 tables[].body_html。
 */
import { useEffect, useRef, useState } from 'react'
import { Button, Card, Empty, Image, Input, Modal, Popconfirm, Space, Spin, Tag, Typography, message } from 'antd'
import { renderAsync } from 'docx-preview'
import { Link, useNavigate, useParams } from 'react-router-dom'
import Auth from '@/components/Auth'
import { fetchDocumentSource, fetchDocumentSourcePreview, getDocument } from '@/api/doc'
import { formatDurationMs } from '@/utils/format'
import {
  approveParseTask,
  fetchParseDraftFigure,
  getParseDraft,
  rejectParseTask,
  saveParseDraft,
  type ParseDraftFigure,
  type ParseDraftTable,
} from '@/api/parse'
import {
  deleteFigureFromDraft,
  deleteTableFromDraft,
  figureAnchorLabel,
  setFigureCaptionLine,
  setTableCaptionLine,
} from './reviewAssets'

/** 清洗稿放在同源 iframe 里，主文档没有长文，Safari 阅读模式不会盖住双栏。 */
const EDITOR_SRCDOC = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  html, body { margin: 0; height: 100%; background: #fff; }
  #scroller { height: 100%; overflow: auto; box-sizing: border-box; }
  textarea {
    display: block; width: 100%; min-height: 100%; box-sizing: border-box;
    border: 0; margin: 0; resize: none; overflow: hidden; outline: none;
    padding: 12px 12px 48px;
    font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
</style>
</head>
<body>
<div id="scroller"><textarea id="ed" spellcheck="false" aria-label="清洗稿正文"></textarea></div>
<script>
  const ed = document.getElementById('ed');
  const scroller = document.getElementById('scroller');
  function fit() {
    ed.style.height = 'auto';
    ed.style.height = (Math.max(ed.scrollHeight, scroller.clientHeight) + 64) + 'px';
  }
  ed.addEventListener('input', function () {
    fit();
    parent.postMessage({ type: 'ekbs-draft', value: ed.value }, '*');
  });
  window.addEventListener('message', function (ev) {
    if (!ev.data) return;
    if (ev.data.type === 'ekbs-draft-set') {
      ed.value = ev.data.value || '';
      fit();
    }
    if (ev.data.type === 'ekbs-draft-lock') ed.readOnly = true;
  });
  fit();
</script>
</body>
</html>`

const OFFICE_EXT = /\.(docx?|rtf|odt|xlsx?|pptx?|txt|md|csv|html?)$/i

function kindOf(name: string, mime: string): 'pdf' | 'image' | 'docx' | 'office' | 'other' {
  const file = name.toLowerCase()
  const lower = `${file} ${mime}`.toLowerCase()
  if (file.endsWith('.pdf') || mime.includes('pdf')) return 'pdf'
  if (/\.(png|jpe?g|gif|webp|bmp)$/i.test(file) || mime.startsWith('image/')) return 'image'
  // .docx 用原件字节排版（含图和表格）。textutil 转 HTML 会丢掉图片、改掉版式。
  if (file.endsWith('.docx')) return 'docx'
  if (OFFICE_EXT.test(file)) return 'office'
  if (lower.includes('pdf')) return 'pdf'
  return 'other'
}

function DocxOriginal({ blob }: { blob: Blob }) {
  const host = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    const el = host.current
    if (!el) return undefined
    let cancelled = false
    el.replaceChildren()
    setFailed(null)
    void renderAsync(blob, el, undefined, {
      className: 'ekbs-docx',
      inWrapper: true,
      breakPages: true,
      ignoreWidth: false,
      ignoreHeight: false,
    }).catch((err: unknown) => {
      if (!cancelled) setFailed(err instanceof Error ? err.message : '原件无法排版')
    })
    return () => {
      cancelled = true
    }
  }, [blob])

  if (failed) {
    return (
      <Typography.Paragraph type="danger" style={{ padding: 16 }}>
        {failed}
      </Typography.Paragraph>
    )
  }
  return <div ref={host} style={{ height: '100%', overflow: 'auto', background: '#f0f0f0' }} />
}

function DraftFigureThumb({
  docId,
  figure,
}: {
  docId: number
  figure: ParseDraftFigure
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const faint = figure.kind === 'faint' || figure.kind === 'blank'
  const handwriting = figure.kind === 'handwriting'

  useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    if (!figure.figure_id) return undefined
    void fetchParseDraftFigure(docId, figure.figure_id, { enhance: !handwriting })
      .then((blob) => {
        if (revoked) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {
        if (!revoked) setFailed(true)
      })
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [docId, figure.figure_id, handwriting])

  if (failed) {
    return <Typography.Text type="secondary">图不可用</Typography.Text>
  }
  if (!url) {
    return <div style={{ width: 180, height: 140, background: '#2c2e3a', borderRadius: 4 }} />
  }
  return (
    <div>
      {handwriting ? (
        <Tag color="blue" style={{ marginBottom: 6 }}>
          手写签署
        </Tag>
      ) : faint ? (
        <Tag color="gold" style={{ marginBottom: 6 }}>
          淡色附图
        </Tag>
      ) : (
        <Tag style={{ marginBottom: 6 }}>附图</Tag>
      )}
      <Image
        src={url}
        alt={figure.caption || figure.figure_id}
        width={handwriting ? 360 : 180}
        style={{
          objectFit: 'contain',
          background: '#2c2e3a',
          borderRadius: 4,
          border: '1px solid #434343',
        }}
        preview={{ src: url, mask: '看大图' }}
      />
    </div>
  )
}

export default function ReviewCompare() {
  const { docId: rawId } = useParams<{ docId: string }>()
  const docId = Number(rawId)
  const navigate = useNavigate()

  const [title, setTitle] = useState('')
  const [docStatus, setDocStatus] = useState('')
  const [sourceName, setSourceName] = useState('')
  const [engine, setEngine] = useState('')
  const [parseMs, setParseMs] = useState<number | null>(null)
  const [tables, setTables] = useState<ParseDraftTable[]>([])
  const [figures, setFigures] = useState<ParseDraftFigure[]>([])
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [officeBlob, setOfficeBlob] = useState<Blob | null>(null)
  const [previewHtml, setPreviewHtml] = useState<string | null>(null)
  const [mime, setMime] = useState('')
  const [loading, setLoading] = useState(true)
  const [sourceError, setSourceError] = useState<string | null>(null)
  const [acting, setActing] = useState(false)
  const [tableEdit, setTableEdit] = useState<ParseDraftTable | null>(null)
  const [figureEdit, setFigureEdit] = useState<ParseDraftFigure | null>(null)

  const frameRef = useRef<HTMLIFrameElement>(null)
  const pendingDraft = useRef('')
  const paneHeight = 'min(70vh, calc(100vh - 280px))'

  const pushDraft = (value: string) => {
    pendingDraft.current = value
    frameRef.current?.contentWindow?.postMessage({ type: 'ekbs-draft-set', value }, '*')
  }

  const readDraft = () => {
    try {
      const ed = frameRef.current?.contentDocument?.getElementById('ed') as HTMLTextAreaElement | null
      if (ed) return ed.value
    } catch {
      /* iframe 尚未就绪时用已同步的正文 */
    }
    return pendingDraft.current
  }

  useEffect(() => {
    if (!Number.isFinite(docId)) return
    let cancelled = false
    let objectUrl: string | null = null
    let sourceFile = ''
    setLoading(true)
    setSourceError(null)
    setPreviewHtml(null)
    setOfficeBlob(null)

    void (async () => {
      try {
        const [doc, draft] = await Promise.all([getDocument(docId), getParseDraft(docId)])
        if (cancelled) return
        setTitle(doc.title || `doc #${docId}`)
        setDocStatus(doc.status || '')
        const name = doc.source_path?.split(/[/\\]/).pop() || ''
        sourceFile = name
        setSourceName(name)
        const text = draft.markdown ?? ''
        pendingDraft.current = text
        pushDraft(text)
        setEngine(draft.engine ?? '')
        setParseMs(draft.parse_ms ?? null)
        setTables(draft.tables ?? [])
        setFigures(draft.figures ?? [])
      } catch (err) {
        if (!cancelled) message.error(err instanceof Error ? err.message : String(err))
      }

      try {
        const sourceKind = kindOf(sourceFile, '')
        if (sourceKind === 'docx') {
          const blob = await fetchDocumentSource(docId)
          if (cancelled) return
          setOfficeBlob(blob)
          setBlobUrl(null)
          setPreviewHtml(null)
        } else if (sourceKind === 'office') {
          const html = await fetchDocumentSourcePreview(docId)
          if (cancelled) return
          setPreviewHtml(typeof html === 'string' ? html : String(html ?? ''))
          setBlobUrl(null)
          setOfficeBlob(null)
        } else {
          const blob = await fetchDocumentSource(docId)
          if (cancelled) return
          objectUrl = URL.createObjectURL(blob)
          setBlobUrl(objectUrl)
          setMime(blob.type || '')
          setOfficeBlob(null)
        }
      } catch (err) {
        if (!cancelled) {
          setSourceError(err instanceof Error ? err.message : '原件不可用')
          setBlobUrl(null)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [docId])

  const persist = async (
    markdown: string,
    nextTables: ParseDraftTable[],
    nextFigures: ParseDraftFigure[],
    done: string,
  ) => {
    setActing(true)
    try {
      await saveParseDraft(docId, {
        markdown,
        tables: nextTables.map((item) => ({
          table_id: item.table_id,
          caption: item.caption ?? '',
          body_html: item.body_html,
          page: item.page ?? null,
          preserve_layout: item.preserve_layout ?? false,
        })),
        figures: nextFigures.map((item) => ({
          figure_id: item.figure_id,
          caption: item.caption ?? '',
          ocr_text: item.ocr_text ?? '',
        })),
      })
      setTables(nextTables)
      setFigures(nextFigures)
      pushDraft(markdown)
      message.success(done)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const save = async () => {
    await persist(readDraft(), tables, figures, '清洗稿已保存')
  }

  const approve = async () => {
    setActing(true)
    try {
      await approveParseTask(docId, readDraft())
      message.success('已确认合格并完成分块；请在清洗解析页勾选后做向量入库')
      navigate(`/chunks/${docId}`)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const reject = async () => {
    if (!window.confirm('确认驳回？将标记为失败，可再重跑解析。')) return
    setActing(true)
    try {
      await rejectParseTask(docId)
      message.success('已驳回')
      navigate('/parse/tasks')
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const kind = kindOf(sourceName, mime)
  const editable = docStatus === 'review'

  if (!Number.isFinite(docId)) {
    return <Card>无效的文档 ID</Card>
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small">
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Typography.Title level={5} style={{ margin: 0 }}>
              双栏复核 — {title || `doc #${docId}`}
            </Typography.Title>
            {engine ? (
              <Typography.Text type="secondary">引擎 {engine}</Typography.Text>
            ) : null}
            <Typography.Text type="secondary">解析耗时 {formatDurationMs(parseMs)}</Typography.Text>
            <Tag>表格 {tables.length}</Tag>
            <Tag>图片 {figures.length}</Tag>
          </Space>
          <Space wrap>
            <Button onClick={() => navigate('/parse/tasks')}>返回解析任务</Button>
            <Link to={`/docs/${docId}`}>文档详情</Link>
            {editable ? (
              <Auth code="parse:task:manage">
                <Button loading={acting} onClick={() => void save()}>
                  保存草稿
                </Button>
              </Auth>
            ) : (
              <Tag>仅查看</Tag>
            )}
            {editable ? (
              <Auth code="parse:task:manage">
                <Button type="primary" loading={acting} onClick={() => void approve()}>
                  确认合格并分块
                </Button>
              </Auth>
            ) : null}
            {editable ? (
              <Auth code="parse:task:manage">
                <Button danger loading={acting} onClick={() => void reject()}>
                  驳回
                </Button>
              </Auth>
            ) : null}
          </Space>
        </Space>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0, marginTop: 8 }}>
          {editable
            ? '左栏是上传的原文件（Word 按原件排版，含图和表），右栏是清洗正文。'
            : '已入库，只读对照。左栏是上传的原文件，右栏是当时确认的清洗稿。'}
          正文中的{' '}
          <code>[表格 N]</code>、<code>[图片 编号]</code> 只是占位，完整内容在下方面板，编号与正文一致
          {editable ? '，可以修改或删除' : ''}。
        </Typography.Paragraph>
      </Card>

      {loading ? (
        <Card>
          <Spin />
        </Card>
      ) : (
        <>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 12,
              height: paneHeight,
              minHeight: 420,
            }}
          >
            <Card
              size="small"
              title="原件"
              style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}
              styles={{
                body: {
                  padding: 0,
                  flex: 1,
                  minHeight: 0,
                  overflow: 'hidden',
                  display: 'flex',
                  flexDirection: 'column',
                },
              }}
            >
              <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', background: '#fafafa' }}>
                {sourceError ? (
                  <Typography.Paragraph type="danger" style={{ padding: 16 }}>
                    {sourceError}
                  </Typography.Paragraph>
                ) : kind === 'docx' && officeBlob ? (
                  <DocxOriginal blob={officeBlob} />
                ) : kind === 'office' && previewHtml ? (
                  <iframe
                    title="原件"
                    sandbox=""
                    srcDoc={previewHtml}
                    style={{ width: '100%', height: '100%', minHeight: '100%', border: 0, background: '#fff' }}
                  />
                ) : kind === 'pdf' && blobUrl ? (
                  <iframe
                    title="原件 PDF"
                    src={`${blobUrl}#toolbar=0&navpanes=0&scrollbar=1`}
                    style={{ width: '100%', height: '100%', minHeight: '100%', border: 0 }}
                  />
                ) : kind === 'image' && blobUrl ? (
                  <img
                    src={blobUrl}
                    alt={sourceName || '原件'}
                    style={{ display: 'block', maxWidth: '100%', margin: '0 auto' }}
                  />
                ) : blobUrl ? (
                  <Space direction="vertical" style={{ padding: 16 }}>
                    <Typography.Text>
                      当前格式不支持在线预览（{sourceName || mime || '未知'}）。
                    </Typography.Text>
                    <Button href={blobUrl} download={sourceName || `doc-${docId}`}>
                      下载原件
                    </Button>
                  </Space>
                ) : (
                  <Typography.Paragraph type="secondary" style={{ padding: 16 }}>
                    无原件预览
                  </Typography.Paragraph>
                )}
              </div>
            </Card>

            <Card
              size="small"
              title="清洗稿（正文）"
              style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}
              styles={{
                body: {
                  padding: 0,
                  flex: 1,
                  minHeight: 0,
                  overflow: 'hidden',
                  position: 'relative',
                },
              }}
            >
              <iframe
                ref={frameRef}
                title="清洗稿正文"
                srcDoc={EDITOR_SRCDOC}
                onLoad={() => {
                  pushDraft(pendingDraft.current)
                  if (!editable) {
                    frameRef.current?.contentWindow?.postMessage({ type: 'ekbs-draft-lock' }, '*')
                  }
                }}
                style={{ width: '100%', height: '100%', border: 0, display: 'block', background: '#fff' }}
              />
            </Card>
          </div>

          <Card
            size="small"
            title={`表格（${tables.length}）`}
            extra={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                编号与正文 [表格 N] 相同
              </Typography.Text>
            }
          >
            {tables.length === 0 ? (
              <Empty description="本篇未解析出独立表格（或表格已内嵌在正文里）" />
            ) : (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                {tables.map((t, idx) => (
                  <div key={t.table_id || idx}>
                    <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                      <Typography.Text strong>
                        [表格 {t.table_id || idx + 1}]
                        {t.page != null ? ` · 约第 ${t.page} 页` : ''}
                        {t.caption ? ` · ${t.caption}` : ''}
                      </Typography.Text>
                      {editable ? (
                      <Auth code="parse:task:manage">
                        <Space>
                          <Button size="small" onClick={() => setTableEdit({ ...t })}>
                            修改
                          </Button>
                          <Popconfirm
                            title="删除这张表？正文里的对应编号会一起去掉并重新编号。"
                            onConfirm={() => {
                              const next = deleteTableFromDraft(readDraft(), tables, t.table_id)
                              void persist(next.markdown, next.tables, figures, '已删除表格')
                            }}
                          >
                            <Button size="small" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        </Space>
                      </Auth>
                      ) : null}
                    </Space>
                    {t.body_html ? (
                      <iframe
                        title={`table-${t.table_id || idx}`}
                        srcDoc={`<!DOCTYPE html><html><head><meta charset="utf-8"/><style>
                          body{margin:8px;font:12px/1.5 -apple-system,PingFang SC,sans-serif;color:#222}
                          table{border-collapse:collapse;width:100%;max-width:100%}
                          td,th{border:1px solid #d9d9d9;padding:4px 8px;vertical-align:top}
                        </style></head><body>${t.body_html}</body></html>`}
                        style={{
                          width: '100%',
                          minHeight: 160,
                          maxHeight: 360,
                          border: '1px solid #f0f0f0',
                          borderRadius: 6,
                          marginTop: 8,
                          background: '#fff',
                        }}
                      />
                    ) : (
                      <Typography.Paragraph type="secondary">（空表）</Typography.Paragraph>
                    )}
                  </div>
                ))}
              </Space>
            )}
          </Card>

          <Card
            size="small"
            title={`图片（${figures.length}）`}
            extra={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                编号与正文 [图片 …] 相同。印章已去掉。
              </Typography.Text>
            }
          >
            {figures.length === 0 ? (
              <Empty description="本篇未抽出独立图片（可在左栏原件 PDF 里直接看版式图）" />
            ) : (
              <Space size={16} wrap>
                {figures.map((fig) => (
                  <div key={fig.figure_id} style={{ width: fig.kind === 'handwriting' ? 360 : 220 }}>
                    <DraftFigureThumb docId={docId} figure={fig} />
                    <Typography.Text strong style={{ display: 'block', marginTop: 6 }}>
                      {figureAnchorLabel(fig.figure_id)}
                    </Typography.Text>
                    <Typography.Paragraph
                      type="secondary"
                      style={{ fontSize: 12, marginTop: 4, marginBottom: 6 }}
                    >
                      {fig.page != null ? `约第 ${fig.page} 页` : '页码未知'}
                      {fig.caption ? ` · ${fig.caption}` : ''}
                      {!fig.caption && !fig.ocr_text ? ' · 无题注/OCR（入库后可能不进检索）' : ''}
                    </Typography.Paragraph>
                    {editable ? (
                    <Auth code="parse:task:manage">
                      <Space>
                        <Button size="small" onClick={() => setFigureEdit({ ...fig })}>
                          修改
                        </Button>
                        <Popconfirm
                          title="删除这张图？正文里的对应编号会一起去掉。"
                          onConfirm={() => {
                            const next = deleteFigureFromDraft(readDraft(), figures, fig.figure_id)
                            void persist(next.markdown, tables, next.figures, '已删除图片')
                          }}
                        >
                          <Button size="small" danger>
                            删除
                          </Button>
                        </Popconfirm>
                      </Space>
                    </Auth>
                    ) : null}
                  </div>
                ))}
              </Space>
            )}
          </Card>

          <Modal
            title={tableEdit ? `修改 [表格 ${tableEdit.table_id}]` : '修改表格'}
            open={tableEdit != null}
            onCancel={() => setTableEdit(null)}
            confirmLoading={acting}
            onOk={() => {
              if (!tableEdit) return
              const nextTables = tables.map((item) =>
                item.table_id === tableEdit.table_id ? tableEdit : item,
              )
              const markdown = setTableCaptionLine(
                readDraft(),
                tableEdit.table_id,
                tableEdit.caption ?? '',
              )
              setTableEdit(null)
              void persist(markdown, nextTables, figures, '表格已保存')
            }}
          >
            {tableEdit ? (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                  addonBefore="题注"
                  value={tableEdit.caption ?? ''}
                  onChange={(ev) => setTableEdit({ ...tableEdit, caption: ev.target.value })}
                />
                <Input.TextArea
                  value={tableEdit.body_html}
                  autoSize={{ minRows: 8, maxRows: 16 }}
                  onChange={(ev) => setTableEdit({ ...tableEdit, body_html: ev.target.value })}
                />
              </Space>
            ) : null}
          </Modal>

          <Modal
            title={figureEdit ? `修改 ${figureAnchorLabel(figureEdit.figure_id)}` : '修改图片'}
            open={figureEdit != null}
            onCancel={() => setFigureEdit(null)}
            confirmLoading={acting}
            onOk={() => {
              if (!figureEdit) return
              const nextFigures = figures.map((item) =>
                item.figure_id === figureEdit.figure_id ? figureEdit : item,
              )
              const markdown = setFigureCaptionLine(
                readDraft(),
                figureEdit.figure_id,
                figureEdit.caption ?? '',
              )
              setFigureEdit(null)
              void persist(markdown, tables, nextFigures, '图片已保存')
            }}
          >
            {figureEdit ? (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text type="secondary">
                  编号 {figureAnchorLabel(figureEdit.figure_id)} 与正文一致，不能改。可以改题注和识别文字。
                </Typography.Text>
                <Input
                  addonBefore="题注"
                  value={figureEdit.caption ?? ''}
                  onChange={(ev) => setFigureEdit({ ...figureEdit, caption: ev.target.value })}
                />
                <Input.TextArea
                  placeholder="识别文字（会进入检索）"
                  value={figureEdit.ocr_text ?? ''}
                  autoSize={{ minRows: 4, maxRows: 10 }}
                  onChange={(ev) => setFigureEdit({ ...figureEdit, ocr_text: ev.target.value })}
                />
              </Space>
            ) : null}
          </Modal>
        </>
      )}
    </Space>
  )
}
