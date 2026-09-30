/**
 * 引用里的原图缩略图 —— 用户的原话是「无法收到图片」。
 *
 * 关键约束（都是实测出来的）：
 * * `GET /assets/figure/{chunk_id}` **要鉴权**：无 token → 401，`?token=` 也不认 → 401；
 *   而 `<img src>` 带不了 Authorization 头 ⇒ **必须用 axios 以 blob 取回**再挂到 `<img>`；
 * * 权限已回收/图已删时端点返回 404 —— 此时引用仍要正常显示，只是没有缩略图（静默）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const httpGet = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', () => ({ http: { get: httpGet } }))

import FigureThumb from './FigureThumb'

beforeEach(() => {
  httpGet.mockReset()
  // jsdom 没有 createObjectURL
  let seq = 0
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => `blob:mock/${(seq += 1)}`),
  })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
})

describe('FigureThumb', () => {
  it('用带鉴权的 blob 请求取图，再挂到 <img> 上', async () => {
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' })
    httpGet.mockResolvedValue(blob)

    render(<FigureThumb chunkId={317} title="图 img-x" />)

    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
    // 走的是 axios 实例（拦截器会加 Authorization），不是裸 <img src>
    expect(httpGet).toHaveBeenCalledWith('/assets/figure/317', { responseType: 'blob' })
    expect(screen.getByRole('img')).toHaveAttribute('src', expect.stringContaining('blob:'))
  })

  it('图取不到时给出可见原因（不能什么都不显示，否则用户只会说"图不出来"）', async () => {
    httpGet.mockRejectedValue(Object.assign(new Error('unauthorized'), { status: 401 }))
    render(<FigureThumb chunkId={999} />)
    await waitFor(() => expect(screen.getByText('图不可用')).toBeInTheDocument())
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('403（权限被回收）与 404（图已删）也能落到同一提示，不抛错', async () => {
    httpGet.mockRejectedValue(Object.assign(new Error('denied'), { status: 403 }))
    render(<FigureThumb chunkId={998} />)
    await waitFor(() => expect(screen.getByText('图不可用')).toBeInTheDocument())
  })

  it('没有 chunk_id（非图片引用）→ 什么都不渲染，也不发请求', () => {
    render(<FigureThumb chunkId={null} />)
    expect(httpGet).not.toHaveBeenCalled()
    expect(screen.queryByRole('img')).toBeNull()
  })
})
