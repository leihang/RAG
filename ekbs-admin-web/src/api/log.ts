import { http } from './client'
import type { DocAccessItem, Page, QaSessionItem, SecurityEventItem } from './types'

export const listQaSessions = (params: { limit?: number; offset?: number } = {}) =>
  http.get('/logs/qa/sessions', { params }) as Promise<Page<QaSessionItem>>

export const getQaSession = (sessionId: string) =>
  http.get(`/logs/qa/sessions/${sessionId}`) as Promise<Record<string, unknown>>

export const listDocAccess = (params: { limit?: number; offset?: number } = {}) =>
  http.get('/logs/doc-access', { params }) as Promise<Page<DocAccessItem>>

export const listSecurityEvents = (params: { limit?: number; offset?: number } = {}) =>
  http.get('/audit/security-events', { params }) as Promise<Page<SecurityEventItem>>
