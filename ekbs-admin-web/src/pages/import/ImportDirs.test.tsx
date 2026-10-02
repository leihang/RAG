/**
 * 目录导入页（`docs/import`）。
 *
 * 这一页最危险的三类错都不是样式问题，所以下面每条断言都盯着「传给后端的是什么」：
 *
 * 1. **默认勾选**：把 duplicate/too_large 也默认勾上，用户以为都要导，后端一条条跳过；
 * 2. **拦不住 blocker**：合规闸/L3 闸没开还让点「导入」，后端 409，操作者拿不到可读原因；
 * 3. **勾选与 `files` 不一致**（最危险）：界面显示勾了 2 份、实际导了目录全量或只导前 200 份。
 *
 * 后端一律 mock（`@/api/import`），不碰网络。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock 的工厂会提升到文件顶部，里面不能引用普通顶层变量；用 vi.hoisted 一起提升
const api = vi.hoisted(() => ({
  getImportMeta: vi.fn(),
  listImportDirs: vi.fn(),
  scanImportDir: vi.fn(),
  runImport: vi.fn(),
  getImportQueue: vi.fn(),
  listKnowledgeBases: vi.fn(),
}))

vi.mock('@/api/import', () => ({
  getImportMeta: api.getImportMeta,
  listImportDirs: api.listImportDirs,
  scanImportDir: api.scanImportDir,
  runImport: api.runImport,
  getImportQueue: api.getImportQueue,
}))

// 页面用 `/kb/knowledge-bases` 补「该库默认密级」（`/import/meta` 的 knowledge_bases 不含它）
vi.mock('@/api/doc', () => ({ listKnowledgeBases: api.listKnowledgeBases }))

import { useAuthStore } from '@/stores/authStore'
import ImportDirs from './ImportDirs'

/** 一份典型扫描：2 个可导入 + 1 个重复（字段名按 `/import/scan` 实测报文）。 */
const PREVIEW = [
  { rel_path: 'policy/a.pdf', size_bytes: 327_603, action: 'new', reason: '', doc_id: null, importable: true },
  { rel_path: 'policy/b.docx', size_bytes: 2048, action: 'new', reason: '', doc_id: null, importable: true },
  {
    rel_path: 'policy/c.pdf',
    size_bytes: 4096,
    action: 'duplicate',
    reason: '内容已在库（checksum 去重）',
    doc_id: 7,
    importable: false,
  },
]

const SCAN_BASE = {
  rel_dir: 'policy',
  total_files: 3,
  importable: 2,
  counts: { new: 2, duplicate: 1, too_large: 0, unsupported: 0 },
  blockers: [] as string[],
  warn: [] as string[],
  preview: PREVIEW,
  preview_truncated: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  // 按钮级权限门是 <Auth code="doc:document:create">：不先把权限放进 store，按钮根本不渲染
  useAuthStore.setState({ me: { user_id: 1, permissions: ['doc:document:create'] } })
  api.getImportMeta.mockResolvedValue({
    raw_root: '/data/ekbs/raw',
    enqueue_limit: 2000,
    knowledge_bases: [{ kb_id: 1, code: 'policy', name: '制度库', document_count: 3 }],
  })
  api.listKnowledgeBases.mockResolvedValue({
    items: [{ kb_id: 1, code: 'policy', name: '制度库', default_level: 2 }],
    total: 1,
  })
  api.listImportDirs.mockResolvedValue({
    rel_path: '',
    parent: null,
    dirs: [{ name: 'policy', rel_path: 'policy', has_children: false, importable_files: 3 }],
    root: '/data/ekbs/raw',
  })
  api.scanImportDir.mockResolvedValue(SCAN_BASE)
  api.runImport.mockResolvedValue({
    enqueued: [{ rel_path: 'policy/a.pdf', doc_id: 11, size_bytes: 327_603 }],
    skipped: [],
    failed: [],
    kb_id: 1,
    level: 2,
    queued: 1,
    hint: '解析由 ekbs-worker 异步进行；进度见「解析任务」页',
  })
  api.getImportQueue.mockResolvedValue({ counts: { pending: 1 }, recent: [] })
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <ImportDirs />
    </MemoryRouter>,
  )

/** 扫描一次并等表格出现；`depts` 用于「L2 必须给部门」的真实前置（页面按后端 blockers 判定）。 */
async function scanOnce(opts: { depts?: string } = {}) {
  renderPage()
  if (opts.depts) {
    fireEvent.change(screen.getByPlaceholderText('102,103'), { target: { value: opts.depts } })
  }
  const buttons = await screen.findAllByRole('button', { name: '扫描预览' })
  fireEvent.click(buttons[0])
  await screen.findByText('policy/a.pdf')
}

/** 表格里某一行的选择框（用行内 scoping，避免拿到表头那个全选框）。 */
const rowOf = (relPath: string) => {
  const row = screen.getByText(relPath).closest('tr')
  if (!row) throw new Error(`没找到 ${relPath} 所在的行`)
  return within(row)
}

const checkboxOf = (relPath: string) => rowOf(relPath).getByRole('checkbox')

describe('目录导入页', () => {
  it('扫描后只默认勾选「可导入的 new」，重复行不勾', async () => {
    await scanOnce()

    await waitFor(() => expect(checkboxOf('policy/a.pdf')).toBeChecked())
    expect(checkboxOf('policy/b.docx')).toBeChecked()
    expect(checkboxOf('policy/c.pdf')).not.toBeChecked()
    // 重复行的原因来自后端 reason，原样展示
    expect(rowOf('policy/c.pdf').getByText('重复（已在库）')).toBeInTheDocument()
  })

  it('有 blocker 时禁止导入，并把后端给的阻断原因显示出来', async () => {
    api.scanImportDir.mockResolvedValue({
      ...SCAN_BASE,
      blockers: ['合规闸未开：真实语料入库前请先打开合规闸'],
    })
    await scanOnce()

    expect(await screen.findByText(/合规闸未开/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /导入选中/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: '导入整个目录' })).toBeDisabled()
    expect(api.runImport).not.toHaveBeenCalled()
  })

  it('「导入选中」传给 /import/run 的 files 与勾选完全一致', async () => {
    // L2 必须给可见部门：填上之后后端扫描才不会有 blocker（页面据此放行）
    await scanOnce({ depts: '102' })

    // 取消勾选 b.docx，只留 a.pdf
    fireEvent.click(checkboxOf('policy/b.docx'))
    await waitFor(() => expect(screen.getByRole('button', { name: /导入选中/ }).textContent).toContain('1'))

    fireEvent.click(screen.getByRole('button', { name: /导入选中/ }))
    await waitFor(() => expect(api.runImport).toHaveBeenCalledTimes(1))

    expect(api.runImport).toHaveBeenCalledWith({
      path: '',
      kb_id: 1,
      // 初值来自该库 default_level=2（/import/meta 不含它，页面从 /kb/knowledge-bases 补）
      level: 2,
      dept_ids: ['102'],
      recursive: true,
      files: ['policy/a.pdf'],
    })
    // 结果面板与 toast 都会说这句话，取第一个即可
    expect((await screen.findAllByText(/已登记 1 份待解析文档/)).length).toBeGreaterThan(0)
    expect(api.getImportQueue).toHaveBeenCalled()
  })

  it('「导入整个目录」不传 files（后端按目录全量重扫）', async () => {
    await scanOnce({ depts: '102' })

    fireEvent.click(screen.getByRole('button', { name: '导入整个目录' }))
    await waitFor(() => expect(api.runImport).toHaveBeenCalledTimes(1))

    expect(api.runImport.mock.calls[0][0].files).toEqual([])
  })

  it('预览被截断时禁止按勾选导入（否则会静默只导前 200 份）', async () => {
    api.scanImportDir.mockResolvedValue({
      ...SCAN_BASE,
      total_files: 300,
      importable: 300,
      preview_truncated: true,
    })
    await scanOnce()

    expect(screen.getByRole('button', { name: /导入选中/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: '导入整个目录' })).toBeEnabled()
    expect(screen.getByText(/预览只列出前/)).toBeInTheDocument()
  })

  it('403 之类不是一句「请求失败」，而是能指导操作的原因', async () => {
    api.scanImportDir.mockRejectedValue(
      Object.assign(new Error('路径超出 raw 目录范围（不允许 .. 或指向外部的软链接）'), { status: 403 }),
    )
    renderPage()
    fireEvent.click((await screen.findAllByRole('button', { name: '扫描预览' }))[0])

    const alert = await screen.findByText(/无权限，或路径超出 raw 目录/)
    expect(alert.textContent).toContain('403')
    expect(alert.textContent).toContain('路径超出 raw 目录范围')
  })
})
