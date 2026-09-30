/**
 * 路由守卫（方案 05 §2.3 路由守卫规则）。
 *
 * 未登录 → /login?redirect=；已登录但权限点不足 → **渲染 /403（不跳转，保留 URL 便于排障）**。
 */
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { Result, Spin } from 'antd'
import { useAuthStore } from '@/stores/authStore'
import { usePermission } from '@/hooks/usePermission'

export function AuthGuard({ children }: { children: ReactNode }) {
  const location = useLocation()
  const token = useAuthStore((s) => s.accessToken)
  if (!token) {
    const redirect = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?redirect=${redirect}`} replace />
  }
  return <>{children}</>
}

export function PermissionGuard({ perm, children }: { perm?: string; children: ReactNode }) {
  const { has } = usePermission()
  const me = useAuthStore((s) => s.me)
  // 快照还没拉回来时先转圈，避免"权限点为空"被误判成无权限而闪一下 403
  if (!me) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin tip="正在加载权限快照…" />
      </div>
    )
  }
  if (perm && !has(perm)) {
    return (
      <Result
        status="403"
        title="您没有访问该页面的权限"
        subTitle={`所需权限点：${perm}。请联系知识库管理员或超级管理员开通。`}
      />
    )
  }
  return <>{children}</>
}
