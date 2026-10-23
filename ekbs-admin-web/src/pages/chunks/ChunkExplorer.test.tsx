/**
 * 分块预览：**父块不参与向量是设计**，不能显示成"未入向量"的告警。
 *
 * 用户实测反馈：「我看父子块没有向量，是不是向量不成功」。原因是左栏父块列表里，
 * 每一行都打了橙色「未入向量」标签 —— 而父块是上下文容器，按设计就不进向量
 * （`ekbs/ingest/retrievable.py`：只有叶子块进向量与 FTS）。
 * 只有**叶子块**缺 `faiss_row` 才是真异常，那才该报警。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getChunkTree: vi.fn() }))

vi.mock('@/api/parse', () => ({
  getChunkTree: api.getChunkTree,
  getChunk: vi.fn(),
  updateChunk: vi.fn(),
  splitChunk: vi.fn(),
  mergeChunk: vi.fn(),
  reembedChunk: vi.fn(),
}))

import ChunkExplorer from './ChunkExplorer'

function mount() {
  return render(
    <MemoryRouter initialEntries={['/chunks/1']}>
      <Routes>
        <Route path="/chunks/:docId" element={<ChunkExplorer />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('分块预览的向量标签', () => {
  it('父块（有子块、faiss_row 为空）显示「上下文块」，不是「未入向量」', async () => {
    api.getChunkTree.mockResolvedValue({
      document: { doc_id: 1, title: '月度汇报', status: 'published', level: 1 },
      parents: 1,
      children: 1,
      tree: [
        {
          chunk_id: 1, doc_id: 1, chunk_type: 'parent', seq: 1, content: '第 1 页正文',
          heading_path: '第 1 页', token_count: 8, page_from: 2, page_to: 2, faiss_row: null,
          children: [
            {
              chunk_id: 2, doc_id: 1, chunk_type: 'child', seq: 2, content: '第 1 页正文',
              heading_path: '第 1 页', token_count: 8, page_from: 2, page_to: 2, faiss_row: 2,
            },
          ],
        },
      ],
      orphan_children: [],
    })

    mount()
    await waitFor(() => expect(screen.getByText('上下文块')).toBeInTheDocument())
    expect(screen.queryByText('未入向量')).toBeNull()
  })

  it('叶子块（有父块但 faiss_row 为空）仍然报警「未入向量」', async () => {
    api.getChunkTree.mockResolvedValue({
      document: { doc_id: 1, title: '月度汇报', status: 'published', level: 1 },
      parents: 0,
      children: 1,
      tree: [
        // 没有 children 的 parent 就是叶子（代码知识库的顶层符号块也是这样）
        {
          chunk_id: 7, doc_id: 1, chunk_type: 'parent', seq: 1, content: 'def run(): ...',
          heading_path: 'run()', token_count: 12, page_from: 1, page_to: 1, faiss_row: null,
          children: [],
        },
      ],
      orphan_children: [],
    })

    mount()
    await waitFor(() => expect(screen.getByText('未入向量')).toBeInTheDocument())
    expect(screen.queryByText('上下文块')).toBeNull()
  })

  it('figure 块展示「多模态已索引」标识', async () => {
    api.getChunkTree.mockResolvedValue({
      document: { doc_id: 1, title: '图册', status: 'published', level: 1 },
      parents: 1,
      children: 0,
      tree: [
        {
          chunk_id: 88, doc_id: 1, chunk_type: 'figure', seq: 1,
          content: '图注：电表更换', heading_path: '电表更换', token_count: 6,
          page_from: 1, page_to: 1, faiss_row: 3, figure_id: '图-1',
          figure_mm_indexed: true, children: [],
        },
      ],
      orphan_children: [],
    })

    mount()
    await waitFor(() => expect(screen.getAllByText('多模态已索引').length).toBeGreaterThan(0))
  })
})
