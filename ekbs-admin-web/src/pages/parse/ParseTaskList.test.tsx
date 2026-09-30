/**
 * 解析任务中心：**行主键是 `task_id`，不是 `doc_id`**。
 *
 * 后端 `GET /parse/tasks` 的行是 `SELECT d.doc_id AS task_id, ...`
 * （`ekbs/api/parse_tasks.py` 也写明「task_id 即 doc_id」），响应里**没有** `doc_id`。
 * 页面原先写 `dataIndex: 'doc_id'` + `rowKey="doc_id"` + `viewResult(r.doc_id)`：
 *
 * * 每一行的 key 都取到 `undefined` → 行 key 全撞在一起；
 * * 点「查看结果」请求 `/parse/tasks/undefined/result` → 422
 *   （实测证据：`var/log/api.out.log` 里就有这条 422）。
 *
 * `parseModel.parseTaskId()` 一直都在处理这个差异，只是页面没用它。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock 的工厂是**提升**到文件顶部的，里面不能引用普通的顶层变量
// （引用就会 "Cannot access before initialization"），所以用 vi.hoisted 一起提升。
const api = vi.hoisted(() => ({
  getParseResult: vi.fn(),
  listParseTasks: vi.fn(),
}))

vi.mock('@/api/parse', () => ({
  listParseTasks: api.listParseTasks,
  getParseResult: api.getParseResult,
  retryParseTask: vi.fn(),
  cancelParseTask: vi.fn(),
}))

import ParseTaskList from './ParseTaskList'

beforeEach(() => {
  api.getParseResult.mockReset()
  api.getParseResult.mockResolvedValue({
    task_id: 1,
    preview: '第一段预览',
    preview_truncated: false,
    note: 'task_id 即 doc_id',
    chunk_count: 2,
  })
  api.listParseTasks.mockResolvedValue({
    items: [
      {
        // 后端真实字段：只有 task_id，没有 doc_id
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
    ],
    total: 1,
    limit: 20,
    offset: 0,
    by_status: { published: 1 },
    success_rate: 1,
    success_rate_target: 0.98,
    note: 'task_id 即 doc_id（本栈一个文档就是一个解析任务）',
  })
})

describe('解析任务中心', () => {
  it('点「查看结果」用的是 task_id，不是 undefined', async () => {
    render(
      <MemoryRouter>
        <ParseTaskList />
      </MemoryRouter>,
    )

    const link = await screen.findByText('查看结果')
    fireEvent.click(link)

    await waitFor(() => expect(api.getParseResult).toHaveBeenCalledWith(1))
    // 关键回归：绝不能把 undefined 拼进 URL（那会得到 422）
    expect(api.getParseResult).not.toHaveBeenCalledWith(undefined)
    expect(await screen.findByText(/第一段预览/)).toBeInTheDocument()
  })

  it('文档名链接指向 /docs/<task_id>（task_id 即 doc_id）', async () => {
    render(
      <MemoryRouter>
        <ParseTaskList />
      </MemoryRouter>,
    )
    const title = await screen.findByText('PPT-混合/2026年8月工作汇报-工程')
    expect(title.closest('a')).toHaveAttribute('href', '/docs/1')
  })
})
