/** 当前用户与权限快照（含 dev 身份切换，仅开发环境可见）。 */
import { useCallback, useEffect } from 'react'
import { http } from '@/api/client'
import { useAuthStore, type MeSnapshot } from '@/stores/authStore'

export interface PermContext {
  user_id: number
  clearance: number
  visible_shards: string[]
  policy_version: number
  fingerprint: string
  visible_kb_ids?: number[]
  warnings?: string[]
}

export function useAuth() {
  const me = useAuthStore((s) => s.me)
  const token = useAuthStore((s) => s.accessToken)
  const clearance = useAuthStore((s) => s.clearance)
  const setSession = useAuthStore((s) => s.setSession)
  const setPermContext = useAuthStore((s) => s.setPermContext)
  const clear = useAuthStore((s) => s.clear)

  /** 拉取 /auth/me + /perm/me：菜单与密级都依赖这两份快照。 */
  const refresh = useCallback(async () => {
    const snapshot = (await http.get('/auth/me')) as MeSnapshot
    setSession(useAuthStore.getState().accessToken ?? '', snapshot)
    try {
      const ctx = (await http.get('/perm/me')) as PermContext
      setPermContext(ctx)
    } catch {
      // 某些身份没有 /perm/me 权限，不影响登录
    }
    return snapshot
  }, [setSession, setPermContext])

  /** 开发环境用 dev-token 切身份（生产环境该端点 404，方案 §4.1）。 */
  const loginAsDev = useCallback(
    async (userId: number) => {
      const data = (await http.post('/auth/dev-token', { user_id: userId })) as {
        token: string
        user_id: number
      }
      useAuthStore.getState().setSession(data.token, { user_id: data.user_id })
      setSession(data.token, { user_id: data.user_id })
      return refresh()
    },
    [refresh, setSession],
  )

  const logout = useCallback(() => clear(), [clear])

  useEffect(() => {
    // 有 token 但没快照（刷新页面后）→ 补拉一次
    if (token && !me) void refresh().catch(() => clear())
  }, [token, me, refresh, clear])

  return { me, token, clearance, refresh, loginAsDev, logout, isAuthed: Boolean(token) }
}
