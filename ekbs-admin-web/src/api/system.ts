import { http } from './client'
import type { Overview, ShardInfo } from './types'

export const getOverview = () => http.get('/dashboard/overview') as Promise<Overview>
export const getHealth = () => http.get('/system/health') as Promise<Record<string, unknown>>
export const listShards = () =>
  http.get('/system/shards') as Promise<{ items: ShardInfo[]; count: number }>
export const listAlertRules = () =>
  http.get('/system/alert-rules') as Promise<{ items?: unknown[] } | unknown[]>
export const rebuildIndex = (body: { shard_id?: string | null; dry_run?: boolean } = {}) =>
  http.post('/system/index/rebuild', body) as Promise<Record<string, unknown>>
