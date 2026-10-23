import { http } from './client'
import type { AclItem, Page, PermChangeItem, PermissionItem, RoleItem, UserItem } from './types'

export interface OrgSyncReport {
  mode?: string
  departments?: number
  users?: number
  created?: number
  updated?: number
  deleted?: number
  failed?: number
  detail?: {
    source?: string
    authed_departments?: number
    authed_users?: number
    [k: string]: unknown
  }
}

export interface OrgSyncSchedule {
  enabled: boolean
  interval_hours: number
  armed_at?: string | null
  running?: boolean
  next_run_at?: string | null
}

export interface OrgSyncStatus {
  latest?: {
    mode?: string
    started_at?: string
    ended_at?: string
    created?: number
    updated?: number
    deleted?: number
    failed?: number
    detail?: OrgSyncReport['detail']
  } | null
  schedule?: OrgSyncSchedule
  note?: string
}

export const orgSyncStatus = () => http.get('/perm/org/sync') as Promise<OrgSyncStatus>

export const syncOrgNow = () => http.post('/perm/org/sync') as Promise<OrgSyncReport>

export const saveOrgSyncSchedule = (body: { enabled: boolean; interval_hours: number }) =>
  http.put('/perm/org/sync/schedule', body) as Promise<OrgSyncSchedule>

export const listUsers = (params: { limit?: number; offset?: number; q?: string; auth_source?: string } = {}) =>
  http.get('/perm/users', { params }) as Promise<Page<UserItem>>

export const createUser = (body: {
  username: string
  password: string
  nick: string
  clearance?: number
  role_code?: string
}) => http.post('/perm/users', body) as Promise<{ user_id: number; username: string; nick: string }>

export const listPermissions = () =>
  http.get('/perm/permissions') as Promise<{ items: PermissionItem[] }>

export const updateUser = (userId: number, body: Record<string, unknown>) =>
  http.put(`/perm/users/${userId}`, body) as Promise<unknown>

export const setClearance = (userId: number, clearance: number, reason: string) =>
  http.put(`/perm/users/${userId}/clearance`, { clearance, reason }) as Promise<unknown>

export const resetPassword = (userId: number, password: string) =>
  http.put(`/perm/users/${userId}/password`, { password }) as Promise<{ ok: boolean }>

export const listRoles = () =>
  http.get('/perm/roles') as Promise<{ items: RoleItem[]; builtin_codes: string[] }>

export const createRole = (body: { code: string; name: string; permissions?: string[] }) =>
  http.post('/perm/roles', body) as Promise<RoleItem>

export const updateRole = (roleId: number, body: Record<string, unknown>) =>
  http.put(`/perm/roles/${roleId}`, body) as Promise<unknown>

export const deleteRole = (roleId: number) => http.delete(`/perm/roles/${roleId}`) as Promise<unknown>

export const assignRole = (roleId: number, body: { user_id: number; scope_type?: string; scope_id?: string }) =>
  http.post(`/perm/roles/${roleId}/assign`, body) as Promise<unknown>

export interface DeptMember {
  user_id: number
  dingtalk_userid: string
  nick?: string
  title?: string | null
  clearance?: number
  status?: string
  is_primary?: number
  roles?: string[]
}

export interface DeptRow {
  dept_id: number
  dingtalk_dept_id: string
  name?: string
  parent_dept_id?: number | null
  tree_depth?: number
  dept_code?: string
  status?: string
  members?: DeptMember[]
}

export const listDepts = () =>
  http.get('/perm/depts') as Promise<{
    items?: DeptRow[]
    dept_code_map?: Record<string, string[]>
    note?: string
    [k: string]: unknown
  }>

export const setDeptMapping = (deptId: string, body: { dept_code: string }) =>
  http.put(`/perm/depts/${deptId}/mapping`, body) as Promise<unknown>

export const listAcl = (params: { limit?: number; offset?: number } = {}) =>
  http.get('/perm/acl', { params }) as Promise<Page<AclItem>>

export const createAcl = (body: Record<string, unknown>) =>
  http.post('/perm/acl', body) as Promise<AclItem>

export const deleteAcl = (aclId: number) => http.delete(`/perm/acl/${aclId}`) as Promise<unknown>

export const simulateAcl = (body: { user_id: number; doc_ids: number[]; reason?: string }) =>
  http.post('/perm/acl/simulate', body) as Promise<Record<string, unknown>>

export const listPermChanges = (params: { limit?: number; offset?: number } = {}) =>
  http.get('/perm/audit', { params }) as Promise<Page<PermChangeItem> & { retention_days?: number }>
