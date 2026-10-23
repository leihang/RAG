/**
 * 首期菜单树与**按权限过滤**的纯函数（方案 05 §2.1 / §6.4）。
 *
 * 单独成文件是为了可单测：菜单不该出现"点进去 403"的项，也不该出现
 * "有权限但看不到"的项 —— 这两条都只有把过滤逻辑拿出来测才守得住。
 *
 * 侧栏「处理流程」按入库链路排序：上传 → 清洗解析 → 分块 → 向量检索。
 */
import type { ReactNode } from 'react'

export interface NavItem {
  path: string
  name: string
  /** 需要的权限点；缺省表示登录即可见 */
  perm?: string
  icon?: ReactNode
  children?: NavItem[]
}

export const NAV: NavItem[] = [
  { path: '/dashboard', name: '概览看板', perm: 'sys:dashboard:read' },
  {
    path: '/pipeline',
    name: '处理流程',
    children: [
      { path: '/docs/upload', name: '上传文档', perm: 'doc:document:create' },
      // 目录导入不占侧栏：从「上传文档」页进入（服务器 raw 目录批量登记）
      { path: '/parse/tasks', name: '清洗解析', perm: 'parse:task:read' },
      { path: '/chunks', name: '分块预览', perm: 'doc:chunk:read' },
      { path: '/rag/playground', name: '向量检索', perm: 'rag:playground:run' },
    ],
  },
  {
    path: '/docs',
    name: '知识资产',
    children: [
      { path: '/docs/list', name: '文档管理', perm: 'doc:document:read' },
      { path: '/docs/trash', name: '回收站', perm: 'doc:document:read' },
    ],
  },
  {
    path: '/iam',
    name: '权限与安全',
    children: [
      { path: '/iam/accounts', name: '管理台账号', perm: 'perm:user:read' },
      { path: '/iam/roles', name: '角色管理', perm: 'perm:role:read' },
      { path: '/iam/depts', name: '组织架构', perm: 'perm:dept:read' },
      { path: '/iam/acl', name: 'ACL 配置', perm: 'perm:acl:read' },
      { path: '/iam/perm-changes', name: '权限变更记录', perm: 'perm:audit:read' },
    ],
  },
  {
    path: '/logs',
    name: '运维与审计',
    children: [
      { path: '/logs/qa', name: '问答日志', perm: 'audit:qa:read' },
      { path: '/logs/audit', name: '安全事件', perm: 'audit:qa:read_all' },
      { path: '/logs/doc-access', name: '数据访问日志', perm: 'audit:trace:read' },
    ],
  },
]

/** 按权限过滤：父节点在没有任何可见子节点时一并隐藏（方案 §2.1「不是禁用」）。 */
export function filterNav(items: NavItem[], has: (code: string) => boolean): NavItem[] {
  const out: NavItem[] = []
  for (const item of items) {
    if (item.children?.length) {
      const kids = filterNav(item.children, has)
      if (kids.length) out.push({ ...item, children: kids })
      continue
    }
    if (!item.perm || has(item.perm)) out.push(item)
  }
  return out
}

/** 详情路由 → 所属列表页（面包屑归属）。 */
const DETAIL_OWNER: [string, string][] = [
  ['/parse/review/', '/parse/tasks'],
  ['/chunks/', '/chunks'],
  ['/docs/import', '/docs/upload'],
  ['/docs/', '/docs/list'],
]

/** 面包屑（方案 §2.2：一级菜单 / 二级菜单 / 当前对象名）。 */
export function buildBreadcrumb(pathname: string, nav: NavItem[] = NAV): string[] {
  const childrenOf = (g: NavItem) => g.children ?? []

  // 1) 精确命中
  for (const group of nav) {
    for (const child of childrenOf(group)) {
      if (pathname === child.path) return [group.name, child.name]
    }
  }
  // 2) 前缀命中（子路由，如 /logs/qa/abc → 问答日志）
  for (const group of nav) {
    for (const child of childrenOf(group)) {
      if (pathname.startsWith(child.path + '/')) return [group.name, child.name]
    }
  }
  // 2.5) 详情页归属
  for (const [prefix, owner] of DETAIL_OWNER) {
    if (!pathname.startsWith(prefix)) continue
    // /chunks 列表本身不要被 /chunks/ 规则吃掉
    if (prefix === '/chunks/' && pathname === '/chunks') continue
    for (const group of nav) {
      for (const child of group.children ?? []) {
        if (child.path === owner) return [group.name, child.name]
      }
    }
  }
  // 3) 详情页归到所属分组
  const segs = pathname.split('/').filter(Boolean)
  for (const group of nav) {
    if (segs[0] !== group.path.replace('/', '')) continue
    const kids = childrenOf(group)
    if (!kids.length) return [group.name]
    const listChild = kids.find((c) => c.path.split('/').filter(Boolean)[1] === 'list')
    return [group.name, (listChild ?? kids[0]).name]
  }
  const top = nav.find((n) => pathname === n.path || pathname.startsWith(n.path + '/'))
  return top ? [top.name] : ['管理台']
}
