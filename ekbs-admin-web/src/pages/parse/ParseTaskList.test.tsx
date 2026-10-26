/**
 * 清洗解析列表：行主键是 task_id；待复核跳双栏，已完成跳分块——不弹 Drawer。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listParseTasks: vi.fn(),
}))

vi.mock('@/api/parse', () => ({
  listParseTasks: api.listParseTasks,
  retryParseTask: vi.fn(),
  cancelParseTask: vi.fn(),
  rejectParseTask: vi.fn(),
}))

vi.mock('@/components/Auth', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

import ParseTaskList from './ParseTaskList'

beforeEach(() => {
  api.listParseTasks.mockReset()
  api.listParseTasks.mockResolvedValue({
    items: [
      {
        task_id: 1,
        kb_id: 3,
        title: 'PPT-混合/2026年8月工作汇报-工程',
        status: 'published',
        level: 1,
        page_count: 38,
        chunk_count: 76,
        created_at: '2026-09-29 03:19:29',
        updated_at: '2026-09-29 03:19:35',
      },
      {
        task_id: 76,
        kb_id: 3,
        title: '空调保养合同',
        status: 'review',
        level: 1,
        page_count: 3,
        chunk_count: 0,
        created_at: '2026-10-11 22:00:00',
        updated_at: '2026-10-11 22:09:00',
      },
    ],
    total: 2,
    limit: 20,
    offset: 0,
    by_status: { published: 1, review: 1 },
    success_rate: 0.5,
    success_rate_target: 0.98,
    note: 'task_id 即 doc_id',
  })
})

describe('清洗解析列表', () => {
  it('已完成点「打开分块」跳到 /chunks/:task_id，不弹全文预览', async () => {
    render(
      <MemoryRouter initialEntries={['/parse/tasks']}>
        <Routes>
          <Route path="/parse/tasks" element={<ParseTaskList />} />
          <Route path="/chunks/:docId" element={<div>分块页</div>} />
        </Routes>
      </MemoryRouter>,
    )

    const link = await screen.findByText('打开分块')
    fireEvent.click(link)
    expect(await screen.findByText('分块页')).toBeInTheDocument()
    expect(screen.queryByText(/解析结果预览/)).toBeNull()
  })

  it('待复核点「查看清洗稿」跳到双栏页', async () => {
    render(
      <MemoryRouter initialEntries={['/parse/tasks']}>
        <Routes>
          <Route path="/parse/tasks" element={<ParseTaskList />} />
          <Route path="/parse/review/:docId" element={<div>双栏复核页</div>} />
        </Routes>
      </MemoryRouter>,
    )

    const link = await screen.findByText('查看清洗稿')
    fireEvent.click(link)
    expect(await screen.findByText('双栏复核页')).toBeInTheDocument()
  })

  it('表格渲染行选择复选框（批量向量入库依赖）', async () => {
    render(
      <MemoryRouter>
        <ParseTaskList />
      </MemoryRouter>,
    )
    await screen.findByText('空调保养合同')
    const boxes = await screen.findAllByRole('checkbox')
    expect(boxes.length).toBeGreaterThan(1)
  })

  it('文档名链接指向 /docs/<task_id>', async () => {
    render(
      <MemoryRouter>
        <ParseTaskList />
      </MemoryRouter>,
    )
    const title = await screen.findByText('PPT-混合/2026年8月工作汇报-工程')
    expect(title.closest('a')).toHaveAttribute('href', '/docs/1')
  })
})
