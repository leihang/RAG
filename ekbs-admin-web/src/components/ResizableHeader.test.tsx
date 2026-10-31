import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ResizableHeader from './ResizableHeader'

describe('ResizableHeader', () => {
  it('拖动右边界回调新宽度', () => {
    const onResize = vi.fn()
    render(
      <table>
        <thead>
          <tr>
            <ResizableHeader width={200} onResize={onResize}>
              文档名
            </ResizableHeader>
          </tr>
        </thead>
      </table>,
    )

    const handle = screen.getByTitle('拖动调整列宽')
    fireEvent.mouseDown(handle, { clientX: 100 })
    fireEvent.mouseMove(document, { clientX: 160 })
    fireEvent.mouseUp(document)

    expect(onResize).toHaveBeenCalled()
    expect(onResize.mock.calls[onResize.mock.calls.length - 1]?.[1]).toEqual({ size: { width: 260 } })
  })

  it('宽度不会被拖到 80px 以下', () => {
    const onResize = vi.fn()
    render(
      <table>
        <thead>
          <tr>
            <ResizableHeader width={200} onResize={onResize}>
              x
            </ResizableHeader>
          </tr>
        </thead>
      </table>,
    )

    fireEvent.mouseDown(screen.getByTitle('拖动调整列宽'), { clientX: 100 })
    fireEvent.mouseMove(document, { clientX: -9999 })
    fireEvent.mouseUp(document)

    expect(onResize.mock.calls[onResize.mock.calls.length - 1]?.[1]).toEqual({ size: { width: 80 } })
  })
})

describe('ResizableHeader 无 onResize', () => {
  it('不可调整的列（行选择/展开列）不画死手柄', () => {
    render(
      <table>
        <thead>
          <tr>
            <ResizableHeader>选择</ResizableHeader>
          </tr>
        </thead>
      </table>,
    )
    expect(screen.queryByTitle('拖动调整列宽')).toBeNull()
  })
})

describe('ResizableHeader 边界兜底', () => {
  it('拖 th 右边界命中区也能起拖（长文档名压住手柄时）', () => {
    const onResize = vi.fn()
    render(
      <table>
        <thead>
          <tr>
            <ResizableHeader width={200} onResize={onResize}>
              工程部/2025版装修施工作业指导书/02 客户满意度调查表(1)
            </ResizableHeader>
          </tr>
        </thead>
      </table>,
    )

    // jsdom 里 getBoundingClientRect().right = 0，clientX=0 落在右边界 14px 命中区
    fireEvent.mouseDown(screen.getByRole('columnheader'), { clientX: 0 })
    fireEvent.mouseMove(document, { clientX: 50 })
    fireEvent.mouseUp(document)

    expect(onResize.mock.calls[onResize.mock.calls.length - 1]?.[1]).toEqual({
      size: { width: 250 },
    })
  })
})
