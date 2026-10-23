import { http } from './client'

export interface DiscoveryDocument {
  doc_id: number
  title: string
  kb_id?: number
  kb_code?: string | null
  owner_user_id?: number | null
  owner_nick?: string | null
  owner_dingtalk_userid?: string | null
  level?: number
  effective_date?: string | null
}

export interface DiscoverySuggestion {
  question: string
  source: string
  doc_id?: number
}

export interface DiscoveryCatalog {
  documents: DiscoveryDocument[]
  suggestions: DiscoverySuggestion[]
  help_text?: string
  document_count?: number
  user_id?: number
  clearance?: number
}

export const fetchDiscoveryCatalog = (params?: {
  doc_limit?: number
  suggestion_limit?: number
}) => {
  const q = new URLSearchParams()
  if (params?.doc_limit != null) q.set('doc_limit', String(params.doc_limit))
  if (params?.suggestion_limit != null) q.set('suggestion_limit', String(params.suggestion_limit))
  const suffix = q.toString() ? `?${q}` : ''
  return http.get(`/discovery/catalog${suffix}`) as Promise<DiscoveryCatalog>
}
