import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const sheetjs = vi.hoisted(() => ({
  read: vi.fn(),
  sheetToHtml: vi.fn(),
}))

const api = vi.hoisted(() => ({
  getDocument: vi.fn(),
  fetchDocumentSource: vi.fn(),
  fetchDocumentSourcePreview: vi.fn(),
  getParseDraft: vi.fn(),
  fetchParseDraftFigure: vi.fn(),
  saveParseDraft: vi.fn(),
  approveParseTask: vi.fn(),
  rejectParseTask: vi.fn(),
}))

vi.mock('@/api/doc', () => ({
  getDocument: api.getDocument,
  fetchDocumentSource: api.fetchDocumentSource,
  fetchDocumentSourcePreview: api.fetchDocumentSourcePreview,
}))

vi.mock('@/api/parse', () => ({
  getParseDraft: api.getParseDraft,
  fetchParseDraftFigure: api.fetchParseDraftFigure,
  saveParseDraft: api.saveParseDraft,
  approveParseTask: api.approveParseTask,
  rejectParseTask: api.rejectParseTask,
}))

vi.mock('@/components/Auth', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

vi.mock('xlsx', () => ({
  read: sheetjs.read,
  utils: { sheet_to_html: sheetjs.sheetToHtml },
}))

vi.mock('docx-preview', () => ({
  renderAsync: vi.fn(async (_blob: Blob, el: HTMLElement) => {
    el.textContent = '满足公共设备、设施管理的需要'
  }),
}))

import ReviewCompare from './ReviewCompare'

beforeEach(() => {
  sheetjs.read.mockReset()
  sheetjs.sheetToHtml.mockReset()
  sheetjs.read.mockReturnValue({ SheetNames: ['用电分析'], Sheets: { 用电分析: {} } })
  sheetjs.sheetToHtml.mockReturnValue('<table><tr><td>-5.14%</td></tr></table>')
  api.getDocument.mockReset()
  api.fetchDocumentSource.mockReset()
  api.fetchDocumentSourcePreview.mockReset()
  api.getParseDraft.mockReset()
  api.getDocument.mockResolvedValue({
    doc_id: 76,
    title: '空调保养合同',
    source_path: '/data/raw/pdf-扫描/空调保养合同.pdf',
    status: 'review',
  })
  api.getParseDraft.mockResolvedValue({
    task_id: 76,
    status: 'review',
    markdown: '# 清洗后正文\n[表格 1]\n',
    engine: 'mineru',
    tables: [
      {
        table_id: '1',
        page: 1,
        body_html: '<table><tr><td>序号</td><td>项目</td></tr></table>',
        chars: 40,
      },
    ],
    figures: [
      {
        figure_id: 'abc123',
        page: 7,
        caption: '',
        ocr_text: '',
        has_image: true,
        kind: 'faint',
        display_hint: '淡色附图，预览已自动增强对比',
      },
    ],
  })
  api.fetchParseDraftFigure.mockResolvedValue(
    new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: 'image/jpeg' }),
  )
  api.fetchDocumentSource.mockResolvedValue(
    new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: 'application/pdf' }),
  )
  Object.defineProperty(URL, 'createObjectURL', {
    writable: true,
    value: vi.fn(() => 'blob:mock-pdf'),
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    writable: true,
    value: vi.fn(),
  })
})

describe('双栏复核', () => {
  it('拉取原件 blob 与清洗稿，并渲染双栏标题', async () => {
    render(
      <MemoryRouter initialEntries={['/parse/review/76']}>
        <Routes>
          <Route path="/parse/review/:docId" element={<ReviewCompare />} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(api.fetchDocumentSource).toHaveBeenCalledWith(76))
    expect(api.getParseDraft).toHaveBeenCalledWith(76)
    expect(await screen.findByText(/双栏复核/)).toBeInTheDocument()
    expect(screen.getByText('原件')).toBeInTheDocument()
    expect(screen.getByText(/清洗稿/)).toBeInTheDocument()
    expect(screen.getByText(/表格（1）/)).toBeInTheDocument()
    expect(screen.getByText(/图片（1）/)).toBeInTheDocument()
    expect(screen.getByText('[图片 abc123]')).toBeInTheDocument()
    expect(screen.getByText(/\[表格 1\]/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /删\s*除/ })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /修\s*改/ })).toHaveLength(2)
    expect(screen.getByTitle('清洗稿正文')).toBeInTheDocument()
    expect(document.querySelector('textarea')).toBeNull()
    expect(api.fetchDocumentSourcePreview).not.toHaveBeenCalled()
  })

  it('Word 原件按上传的 docx 排版，不转成另一份 HTML', async () => {
    api.getDocument.mockResolvedValue({
      doc_id: 113,
      title: '设备编号作业指导书',
      source_path: '/data/raw/doc文档/XJWY-GC-02设备编号作业指导书.docx',
      status: 'published',
    })
    api.getParseDraft.mockResolvedValue({
      task_id: 113,
      status: 'published',
      markdown: '1. 目的\n',
      engine: 'docx',
      tables: [],
      figures: [],
    })
    api.fetchDocumentSource.mockResolvedValue(
      new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      }),
    )

    render(
      <MemoryRouter initialEntries={['/parse/review/113']}>
        <Routes>
          <Route path="/parse/review/:docId" element={<ReviewCompare />} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(api.fetchDocumentSource).toHaveBeenCalledWith(113))
    expect(api.fetchDocumentSourcePreview).not.toHaveBeenCalled()
    expect(await screen.findByText('满足公共设备、设施管理的需要')).toBeInTheDocument()
    expect(document.querySelector('iframe[title="原件"]')).toBeNull()
    expect(screen.queryByText(/当前格式不支持在线预览/)).toBeNull()
    expect(screen.queryByRole('button', { name: /下载原件/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /确认通过/ })).toBeNull()
    expect(screen.getByText('仅查看')).toBeInTheDocument()
  })
})


it('Excel 原件从原始 xlsx 字节前端渲染，不走后端 source-preview', async () => {
  api.getDocument.mockResolvedValue({
    doc_id: 214,
    title: '2026年9月能耗分析',
    source_path: '/data/raw/工程部/2026年9月能耗分析.xlsx',
    status: 'review',
  })
  api.getParseDraft.mockResolvedValue({
    task_id: 214,
    status: 'review',
    markdown: '## 用电分析\n[表格 1]\n',
    engine: 'xlsx',
    tables: [],
    figures: [],
  })
  const xlsxBlob = new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  }) as Blob & { arrayBuffer: () => Promise<ArrayBuffer> }
  xlsxBlob.arrayBuffer = async () => new Uint8Array([0x50, 0x4b, 0x03, 0x04]).buffer
  api.fetchDocumentSource.mockResolvedValue(xlsxBlob)

  render(
    <MemoryRouter initialEntries={['/parse/review/214']}>
      <Routes>
        <Route path="/parse/review/:docId" element={<ReviewCompare />} />
      </Routes>
    </MemoryRouter>,
  )

  await waitFor(() => expect(sheetjs.read).toHaveBeenCalled())
  expect(api.fetchDocumentSource).toHaveBeenCalledWith(214)
  expect(api.fetchDocumentSourcePreview).not.toHaveBeenCalled()
  expect(await screen.findByTitle('原件 Excel')).toBeInTheDocument()
})
