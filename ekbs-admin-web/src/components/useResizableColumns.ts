import { useCallback, useMemo, useState } from 'react'
import type { HTMLAttributes } from 'react'
import type { ProColumns } from '@ant-design/pro-components'

const STORAGE_PREFIX = 'ekbs:col-widths:'

/**
 * 让 ProTable 的列可拖拽调宽，并按 `storageKey` 记忆到 localStorage。
 *
 * 用法：
 * ```tsx
 * const columns = useResizableColumns(baseColumns, { storageKey: 'parse-tasks' })
 * <ProTable columns={columns} components={{ header: { cell: ResizableHeader } }} />
 * ```
 * 拖拽本身在 `ResizableHeader` 里；这里只维护宽度状态与持久化。
 */
export function useResizableColumns<T>(
  columns: ProColumns<T>[],
  options: { storageKey: string; defaults?: Record<string, number> },
): ProColumns<T>[] {
  const { storageKey, defaults = {} } = options
  const [widths, setWidths] = useState<Record<string, number>>(() => {
    if (typeof window === 'undefined') return defaults
    try {
      const raw = window.localStorage.getItem(STORAGE_PREFIX + storageKey)
      return raw ? { ...defaults, ...(JSON.parse(raw) as Record<string, number>) } : defaults
    } catch {
      return defaults
    }
  })

  const onResize = useCallback(
    (key: string, width: number) => {
      setWidths((prev) => {
        const next = { ...prev, [key]: width }
        try {
          window.localStorage.setItem(STORAGE_PREFIX + storageKey, JSON.stringify(next))
        } catch {
          // 隐私模式 / 配额满：记忆失败不影响本次调整
        }
        return next
      })
    },
    [storageKey],
  )

  return useMemo(
    () =>
      columns.map((column) => {
        const key = String(column.dataIndex ?? column.key ?? '')
        if (!key) return column
        const width = widths[key] ?? (column.width as number | undefined)
        return {
          ...column,
          width,
          onHeaderCell: () =>
            ({
              width,
              onResize: (_event: unknown, data: { size: { width: number } }) =>
                onResize(key, data.size.width),
            }) as HTMLAttributes<HTMLElement>,
        } as ProColumns<T>
      }),
    [columns, widths, onResize],
  )
}
