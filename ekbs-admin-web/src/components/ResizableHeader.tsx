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
 *
 * ⚠️ 只有 `onResize` 存在的列才画手柄：行选择列、展开列没有 onResize，
 * 画了也无法拖动 —— 之前每个表头都画，用户拖到"假手柄"上会以为功能坏了。
 */
export default function ResizableHeader({ width, onResize, children, style, ...rest }: Props) {
  const [active, setActive] = useState(false)
  const [hover, setHover] = useState(false)
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
      {onResize ? (
        <span
          aria-hidden
          onMouseDown={onMouseDown}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          title="拖动调整列宽"
          style={{
            position: 'absolute',
            top: 0,
            right: 0,
            bottom: 0,
            width: 8,
            cursor: 'col-resize',
            // 始终可见的细线：完全透明时用户根本发现不了"这里能拖"
            borderRight: '1px solid rgba(0, 0, 0, 0.14)',
            background: active
              ? 'rgba(22, 119, 255, 0.35)'
              : hover
                ? 'rgba(22, 119, 255, 0.18)'
                : 'transparent',
          }}
        >
          {/* 中部小竖条：让"这里能拖"一眼可见（1px 边框太淡） */}
          <span
            style={{
              position: 'absolute',
              top: '30%',
              bottom: '30%',
              right: 3,
              width: 2,
              borderRadius: 1,
              background: active || hover ? 'rgba(22, 119, 255, 0.8)' : 'rgba(0, 0, 0, 0.25)',
            }}
          />
        </span>
      ) : null}
    </th>
  )
}
