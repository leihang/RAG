/** 统一空态（方案 05 §5.4 文案模板：还没有{对象}，{下一步动作} + 主按钮）。 */
import { Button, Empty } from 'antd'
import type { ReactNode } from 'react'

export interface EmptyStateProps {
  description?: ReactNode
  actionText?: string
  onAction?: () => void
}

export default function EmptyState({ description, actionText, onAction }: EmptyStateProps) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={description ?? '暂无数据'}
      style={{ padding: '24px 0' }}
    >
      {actionText && onAction ? (
        <Button type="primary" onClick={onAction}>
          {actionText}
        </Button>
      ) : null}
    </Empty>
  )
}
