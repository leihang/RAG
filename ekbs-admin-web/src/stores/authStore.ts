/**
 * 鉴权与权限快照（Zustand）。**只放 UI 状态**（方案 05 §6.2 边界）。
 *
 * 刻意持久化到 localStorage：管理台是内网单机部署，刷新页面不该被弹回登录页。
 * `permFingerprint` **只用于展示与排错**，绝不放进请求头（方案 §6.3 硬要求）。
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface MeSnapshot {
  user_id: number
  nick?: string
  dingtalk_userid?: string
  clearance?: number
  /** 部门 ID 链（钉钉 dept id） */
  dept_chain?: string[]
  /** 部门名称链（与 dept_chain 对齐，后端已解析） */
  dept_names?: string[]
  roles?: string[]
  permissions?: string[]
}

interface AuthState {
  accessToken: string | null
  me: MeSnapshot | null
  clearance: number | null
  policyVersion: number | null
  permFingerprint: string | null
  visibleShards: string[]
  setSession: (token: string, me: MeSnapshot) => void
  setPermContext: (ctx: {
    clearance?: number | null
    policy_version?: number | null
    fingerprint?: string | null
    visible_shards?: string[]
  }) => void
  clear: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      me: null,
      clearance: null,
      policyVersion: null,
      permFingerprint: null,
      visibleShards: [],
      setSession: (token, me) =>
        set({
          accessToken: token,
          me,
          // /auth/me 自带 clearance；避免只依赖 /perm/me 时顶栏密级空白
          clearance: me.clearance ?? null,
        }),
      setPermContext: (ctx) =>
        set({
          clearance: ctx.clearance ?? null,
          policyVersion: ctx.policy_version ?? null,
          permFingerprint: ctx.fingerprint ?? null,
          visibleShards: ctx.visible_shards ?? [],
        }),
      clear: () =>
        set({
          accessToken: null,
          me: null,
          clearance: null,
          policyVersion: null,
          permFingerprint: null,
          visibleShards: [],
        }),
    }),
    { name: 'ekbs.auth' },
  ),
)

export const selectPermissions = (s: AuthState): string[] => s.me?.permissions ?? []
