import { http } from './client'
import type { AuthMe, PermContext } from './types'

export const getMe = () => http.get('/auth/me') as Promise<AuthMe>
export const getPermContext = () => http.get('/perm/me') as Promise<PermContext>
export const devToken = (userId: number) =>
  http.post('/auth/dev-token', { user_id: userId }) as Promise<{ token: string; user_id: number }>
