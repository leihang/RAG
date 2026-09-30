/**
 * 权限 Hook（方案 05 §6.4.1 的接口形状：has / hasAll / hasAny / isRole / clearance / canAccessLevel / fingerprint）。
 */
import { useMemo } from 'react'
import { useAuthStore } from '@/stores/authStore'
import { canAccessLevel as canAccess, has, hasAll, hasAny, type Level } from '@/utils/permission'

export interface PermissionApi {
  has: (code: string) => boolean
  hasAll: (codes: string[]) => boolean
  hasAny: (codes: string[]) => boolean
  isRole: (roleCode: string) => boolean
  clearance: Level | null
  canAccessLevel: (level: Level | number) => boolean
  fingerprint: string
}

export function usePermission(): PermissionApi {
  const permissions = useAuthStore((s) => s.me?.permissions)
  const roles = useAuthStore((s) => s.me?.roles)
  const clearance = useAuthStore((s) => s.clearance)
  const fingerprint = useAuthStore((s) => s.permFingerprint)

  const perms = useMemo(() => permissions ?? [], [permissions])
  const roleList = useMemo(() => roles ?? [], [roles])

  return useMemo<PermissionApi>(
    () => ({
      has: (code) => has(perms, code),
      hasAll: (codes) => hasAll(perms, codes),
      hasAny: (codes) => hasAny(perms, codes),
      isRole: (code) => roleList.includes(code),
      clearance: (clearance as Level | null) ?? null,
      canAccessLevel: (level) => canAccess(clearance, level),
      fingerprint: fingerprint ?? '',
    }),
    [perms, roleList, clearance, fingerprint],
  )
}
