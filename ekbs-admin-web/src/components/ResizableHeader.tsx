import { useCallback, useRef, useState } from 'react'
import type {
  HTMLAttributes,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from 'react'

interface ResizeMeta {
  width?: number
  onResize?: (_event: unknown, data: { size: { width: number } }) => void
}

type Props = HTMLAttributes<HTMLTableCellElement> & ResizeMeta & { children?: ReactNode }

/**
 * antd Table 的可拖拽表头：拖列右边界调整宽度。
 *
 * 为什么要它：文档名是目录导入的全路径（可能上百字符），固定列宽要么截断、
 * 要么把右侧列挤出视口；让使用者自己拖出想要的宽度，比猜一个"万能宽度"靠谱。
 * 宽度记忆由 `useResizableColumns` 负责（localStorage）。
 */
export default function ResizableHeader({ width, onResize, children, style, ...rest }: Props) {
  const [active, setActive] = useState(false)
  const startX = useRef(0)
  const startWidth = useRef(0)

  const onMouseDown = useCallback(
    (event: ReactMouseEvent) => {
      event.preventDefault()
      event.stopPropagation()
      startX.current = event.clientX
      startWidth.current = Number(width) || 100
      setActive(true)
      const onMove = (moveEvent: MouseEvent) => {
        const next = Math.max(80, startWidth.current + (moveEvent.clientX - startX.current))
        onResize?.(null, { size: { width: next } })
      }
      const onUp = () => {
        setActive(false)
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
      }
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [onResize, width],
  )

  return (
    <th {...rest} style={{ ...style, position: 'relative', userSelect: 'none' }}>
      {children}
      <span
        aria-hidden
        onMouseDown={onMouseDown}
        title="拖动调整列宽"
        style={{
          position: 'absolute',
          top: 0,
          right: 0,
          bottom: 0,
          width: 8,
          cursor: 'col-resize',
          background: active ? 'rgba(22, 119, 255, 0.35)' : 'transparent',
        }}
      />
    </th>
  )
}
