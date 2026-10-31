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
