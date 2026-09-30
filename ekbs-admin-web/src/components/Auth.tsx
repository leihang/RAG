/**
 * 按钮级权限组件（方案 05 §6.4）。
 * **只做体验优化**：无权限就不渲染（不是置灰），后端仍逐点校验（原则 P3）。
 */
import type { ReactNode } from 'react'
import { usePermission } from '@/hooks/usePermission'

export interface AuthProps {
  code: string
  /** 满足任一即可 */
  anyOf?: string[]
  children: ReactNode
  fallback?: ReactNode
}

export default function Auth({ code, anyOf, children, fallback = null }: AuthProps) {
  const { has, hasAny } = usePermission()
  const ok = anyOf?.length ? hasAny(anyOf) : has(code)
  return <>{ok ? children : fallback}</>
}
