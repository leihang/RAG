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

/** 右边缘命中区（px）：文档名很长时，8px 手柄可能被内容/省略号干扰，整条边都能拖更稳。 */
const EDGE_HIT_PX = 14

/**
 * antd Table 的可拖拽表头：拖列右边界调整宽度。
 *
 * 两道保险：① 表头右边缘固定手柄；② 整个 th 右边界 14px 内也能起拖
 * （文档名长、内容压住手柄时依然能拉）。宽度记忆由 `useResizableColumns` 负责。
 *
 * ⚠️ 只有 `onResize` 存在的列才画手柄：行选择列、展开列没有 onResize，
 * 画了也无法拖动 —— 用户拖到"假手柄"上会以为功能坏了。
 */
export default function ResizableHeader({ width, onResize, children, style, ...rest }: Props) {
  const [active, setActive] = useState(false)
  const [hover, setHover] = useState(false)
  const startX = useRef(0)
  const startWidth = useRef(0)

  const begin = useCallback(
    (clientX: number) => {
      if (!onResize) return
      startX.current = clientX
      startWidth.current = Number(width) || 100
      setActive(true)
      const onMove = (moveEvent: MouseEvent) => {
        const next = Math.max(80, startWidth.current + (moveEvent.clientX - startX.current))
        onResize(null, { size: { width: next } })
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

  const onThMouseDown = useCallback(
    (event: ReactMouseEvent<HTMLTableCellElement>) => {
      if (!onResize) return
      const rect = event.currentTarget.getBoundingClientRect()
      if (rect.right - event.clientX > EDGE_HIT_PX) return
      event.preventDefault()
      event.stopPropagation()
      begin(event.clientX)
    },
    [begin, onResize],
  )

  return (
    <th
      {...rest}
      onMouseDown={onThMouseDown}
      style={{ ...style, position: 'relative', userSelect: 'none' }}
    >
      {children}
      {onResize ? (
        <span
          aria-hidden
          onMouseDown={(event) => {
            event.preventDefault()
            event.stopPropagation()
            begin(event.clientX)
          }}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          title="拖动调整列宽"
          style={{
            position: 'absolute',
            top: 0,
            right: 0,
            bottom: 0,
            width: EDGE_HIT_PX,
            cursor: 'col-resize',
            zIndex: 3,
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
