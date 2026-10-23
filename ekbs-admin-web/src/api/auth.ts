import { http } from './client'
import type { AuthMe, PermContext } from './types'

export const getMe = () => http.get('/auth/me') as Promise<AuthMe>
export const getPermContext = () => http.get('/perm/me') as Promise<PermContext>
export const devToken = (userId: number) =>
  http.post('/auth/dev-token', { user_id: userId }) as Promise<{ token: string; user_id: number }>

/** 免登所需的公开配置（corpId 等；**不含任何密钥**，方案 `04` §3.4）。 */
export interface DingTalkConfig {
  enabled: boolean
  corp_id: string
  client_id: string
  /** 缺失的配置项键名，便于前端直接提示管理员补哪一项 */
  missing?: string[]
}

export const getDingTalkConfig = () =>
  http.get('/auth/dingtalk-config') as Promise<DingTalkConfig>

/** authCode → 服务端换身份 → JWT（authCode 一次性，服务端防重放）。 */
export const dingtalkLogin = (authCode: string) =>
  http.post('/auth/dingtalk-login', { auth_code: authCode }) as Promise<{
    token: string
    user_id: number
    nick?: string
  }>

/** 本机账号登录（**仅服务器本机可用**；远程会被后端 403）。 */
export const localLogin = (username: string, password: string) =>
  http.post('/auth/local-login', { username, password }) as Promise<{
    token: string
    user_id: number
    nick?: string
    must_change?: boolean
  }>

export const changePassword = (currentPassword: string, newPassword: string) =>
  http.post('/auth/password', {
    current_password: currentPassword,
    new_password: newPassword,
  }) as Promise<{ ok: boolean }>
