import { http } from './client'
import type { AclItem, Page, PermChangeItem, RoleItem, UserItem } from './types'

export const listUsers = (params: { limit?: number; offset?: number; q?: string } = {}) =>
  http.get('/perm/users', { params }) as Promise<Page<UserItem>>

export const updateUser = (userId: number, body: Record<string, unknown>) =>
  http.put(`/perm/users/${userId}`, body) as Promise<unknown>

export const setClearance = (userId: number, clearance: number, reason: string) =>
  http.put(`/perm/users/${userId}/clearance`, { clearance, reason }) as Promise<unknown>

export const listRoles = () =>
  http.get('/perm/roles') as Promise<{ items: RoleItem[]; builtin_codes: string[] }>

export const createRole = (body: { code: string; name: string; permissions?: string[] }) =>
  http.post('/perm/roles', body) as Promise<RoleItem>

export const updateRole = (roleId: number, body: Record<string, unknown>) =>
  http.put(`/perm/roles/${roleId}`, body) as Promise<unknown>

export const deleteRole = (roleId: number) => http.delete(`/perm/roles/${roleId}`) as Promise<unknown>

export const assignRole = (roleId: number, body: { user_id: number; scope_type?: string; scope_id?: string }) =>
  http.post(`/perm/roles/${roleId}/assign`, body) as Promise<unknown>

export const listDepts = () =>
  http.get('/perm/depts') as Promise<{
    items?: unknown[]
    dept_code_map?: Record<string, string>
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
