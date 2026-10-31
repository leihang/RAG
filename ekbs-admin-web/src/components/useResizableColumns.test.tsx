import { fireEvent, render, screen } from '@testing-library/react'
import { useMemo } from 'react'
import { beforeEach, describe, expect, it } from 'vitest'
import type { ProColumns } from '@ant-design/pro-components'
import { useResizableColumns } from './useResizableColumns'

interface Row {
  title: string
}

function Probe() {
  const columns = useMemo<ProColumns<Row>[]>(
    () => [{ title: '文档名', dataIndex: 'title' }],
    [],
  )
  const merged = useResizableColumns(columns, { storageKey: 'unit', defaults: { title: 420 } })
  // ProColumns 的 render 签名与 antd ColumnType 不兼容，测试里按结构取 onHeaderCell
  const header = (
    merged[0] as unknown as {
      onHeaderCell?: (column: unknown) => {
        width?: number
        onResize?: (_event: unknown, data: { size: { width: number } }) => void
      }
    }
  ).onHeaderCell?.(merged[0]) ?? {}
  return (
    <div>
      <span data-testid="width">{String(header.width)}</span>
      <button onClick={() => header.onResize?.(null, { size: { width: 500 } })}>resize</button>
    </div>
  )
}

describe('useResizableColumns', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('应用默认宽度，调整后更新并持久化', () => {
    render(<Probe />)
    expect(screen.getByTestId('width').textContent).toBe('420')

    fireEvent.click(screen.getByText('resize'))

    expect(screen.getByTestId('width').textContent).toBe('500')
    expect(window.localStorage.getItem('ekbs:col-widths:unit')).toContain('"title":500')
  })
})
