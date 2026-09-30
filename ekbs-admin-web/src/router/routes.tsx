/**
 * 路由表（方案 05 §2.3 路由表的**首期子集**：自研 6 模块）。
 *
 * 二期模块（知识库管理、模型/Prompt 配置、评估标注、数据源接入、系统设置…）
 * 与其"预留权限点"对应的入口**首期不渲染** —— 方案 §3.4 约束 2 的硬要求。
 */
import { lazy, type ComponentType, type LazyExoticComponent } from 'react'

export interface AppRoute {
  path: string
  Component: LazyExoticComponent<ComponentType>
  /** 页面要求的权限点 */
  perm?: string
}

const Dashboard = lazy(() => import('@/pages/dashboard'))
const DocList = lazy(() => import('@/pages/docs/DocumentList'))
const DocUpload = lazy(() => import('@/pages/docs/DocumentUpload'))
const DocDetail = lazy(() => import('@/pages/docs/DocumentDetail'))
const ParseTasks = lazy(() => import('@/pages/parse/ParseTaskList'))
const ChunkExplorer = lazy(() => import('@/pages/chunks/ChunkExplorer'))
const Playground = lazy(() => import('@/pages/rag/Playground'))
const UserList = lazy(() => import('@/pages/iam/UserList'))
const RoleList = lazy(() => import('@/pages/iam/RoleList'))
const DeptMapping = lazy(() => import('@/pages/iam/DeptMapping'))
const AclConsole = lazy(() => import('@/pages/iam/AclConsole'))
const PermChangeList = lazy(() => import('@/pages/iam/PermChangeList'))
const QaSessionList = lazy(() => import('@/pages/logs/QaSessionList'))
const QaSessionDetail = lazy(() => import('@/pages/logs/QaSessionDetail'))
const AuditEvents = lazy(() => import('@/pages/logs/AuditEvents'))
const DocAccessList = lazy(() => import('@/pages/logs/DocAccessList'))

export const ROUTES: AppRoute[] = [
  { path: 'dashboard', Component: Dashboard, perm: 'sys:dashboard:read' },
  { path: 'docs/list', Component: DocList, perm: 'doc:document:read' },
  { path: 'docs/upload', Component: DocUpload, perm: 'doc:document:create' },
  { path: 'docs/:docId', Component: DocDetail, perm: 'doc:document:read' },
  { path: 'parse/tasks', Component: ParseTasks, perm: 'parse:task:read' },
  { path: 'chunks/:docId', Component: ChunkExplorer, perm: 'doc:chunk:read' },
  { path: 'rag/playground', Component: Playground, perm: 'rag:playground:run' },
  { path: 'iam/users', Component: UserList, perm: 'perm:user:read' },
  { path: 'iam/roles', Component: RoleList, perm: 'perm:role:read' },
  { path: 'iam/depts', Component: DeptMapping, perm: 'perm:dept:read' },
  { path: 'iam/acl', Component: AclConsole, perm: 'perm:acl:read' },
  { path: 'iam/perm-changes', Component: PermChangeList, perm: 'perm:audit:read' },
  { path: 'logs/qa', Component: QaSessionList, perm: 'audit:qa:read' },
  { path: 'logs/qa/:sessionId', Component: QaSessionDetail, perm: 'audit:qa:read' },
  { path: 'logs/audit', Component: AuditEvents, perm: 'audit:qa:read_all' },
  { path: 'logs/doc-access', Component: DocAccessList, perm: 'audit:trace:read' },
]
