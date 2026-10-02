/**
 * 全局布局（方案 05 §2.2 线框图）：左侧菜单 240px（收起 64px）+ 顶栏 56px + 面包屑 40px + 内容区。
 *
 * 菜单**按权限点动态生成**：无权限的项直接不渲染（方案 §2.1：「不是禁用」）。
 * 首期只暴露「自研 6」模块（§1.5）；二期模块/配置文件类模块不渲染它们的入口 ——
 * 这条是 §3.4 约束 2 的硬要求（预留权限点的入口首期不渲染），不是偷懒。
 */
import { useMemo, useState } from 'react'
import { ProLayout } from '@ant-design/pro-components'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Badge, Button, Dropdown, Space, Tag, Typography } from 'antd'
import {
  AuditOutlined,
  BookOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { usePermission } from '@/hooks/usePermission'
import { useAuth } from '@/hooks/useAuth'
import { useAuthStore } from '@/stores/authStore'
import ClearanceTag from '@/components/ClearanceTag'
import VersionWatcher from '@/components/VersionWatcher'

export interface NavItem {
  path: string
  name: string
  /** 需要的权限点；缺省表示登录即可见 */
  perm?: string
  icon?: React.ReactNode
  children?: NavItem[]
}

/** 首期菜单（对应方案 §2.1 菜单树的自研 6 模块子集）。 */
export const NAV: NavItem[] = [
  { path: '/dashboard', name: '概览看板', perm: 'sys:dashboard:read', icon: <DashboardOutlined /> },
  {
    path: '/docs',
    name: '知识资产',
    icon: <BookOutlined />,
    children: [
      { path: '/docs/list', name: '文档管理', perm: 'doc:document:read' },
      { path: '/docs/trash', name: '回收站', perm: 'doc:document:read' },
      { path: '/docs/upload', name: '上传文档', perm: 'doc:document:create' },
    ],
  },
  {
    path: '/parse',
    name: '处理与检索',
    icon: <ExperimentOutlined />,
    children: [
      { path: '/parse/tasks', name: '解析任务中心', perm: 'parse:task:read' },
      { path: '/rag/playground', name: '检索调试台', perm: 'rag:playground:run' },
    ],
  },
  {
    path: '/iam',
    name: '权限与安全',
    icon: <SafetyCertificateOutlined />,
    children: [
      { path: '/iam/users', name: '用户管理', perm: 'perm:user:read' },
      { path: '/iam/roles', name: '角色管理', perm: 'perm:role:read' },
      { path: '/iam/depts', name: '部门映射', perm: 'perm:dept:read' },
      { path: '/iam/acl', name: 'ACL 配置', perm: 'perm:acl:read' },
      { path: '/iam/perm-changes', name: '权限变更记录', perm: 'perm:audit:read' },
    ],
  },
  {
    path: '/logs',
    name: '运维与审计',
    icon: <AuditOutlined />,
    children: [
      { path: '/logs/qa', name: '问答日志', perm: 'audit:qa:read' },
      { path: '/logs/audit', name: '安全事件', perm: 'audit:qa:read_all' },
      { path: '/logs/doc-access', name: '数据访问日志', perm: 'audit:trace:read' },
    ],
  },
]

/** 按权限过滤菜单：父节点在没有任何可见子节点时也隐藏。 */
export function filterNav(items: NavItem[], hasAny: (codes: string[]) => boolean, has: (c: string) => boolean): NavItem[] {
  const out: NavItem[] = []
  for (const item of items) {
    if (item.children?.length) {
      const kids = filterNav(item.children, hasAny, has)
      if (kids.length) out.push({ ...item, children: kids })
      continue
    }
    if (!item.perm || has(item.perm)) out.push(item)
  }
  return out
}

/** 面包屑：一级菜单 / 二级菜单 / 当前对象名（方案 §2.2）。 */
function buildBreadcrumb(pathname: string): string[] {
  for (const group of NAV) {
    for (const child of group.children ?? []) {
      if (pathname.startsWith(child.path)) return [group.name, child.name]
    }
  }
  const top = NAV.find((n) => pathname.startsWith(n.path))
  return top ? [top.name] : ['管理台']
}

export default function BasicLayout() {
  const location = useLocation()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)
  const { has, hasAny, clearance, fingerprint } = usePermission()
  const { me, refresh, logout } = useAuth()
  const policyVersion = useAuthStore((s) => s.policyVersion)

  const menuData = useMemo(
    () =>
      filterNav(NAV, hasAny, has).map((item) => ({
        path: item.path,
        name: item.name,
        icon: item.icon,
        routes: item.children?.map((c) => ({ path: c.path, name: c.name })),
      })),
    [has, hasAny],
  )

  const crumbs = buildBreadcrumb(location.pathname)

  return (
    <ProLayout
      title="EKBS 管理台"
      logo={false}
      layout="side"
      fixSiderbar
      fixedHeader
      collapsed={collapsed}
      onCollapse={setCollapsed}
      siderWidth={240}
      location={{ pathname: location.pathname }}
      route={{ path: '/', routes: menuData }}
      menuItemRender={(item, dom) => (item.path ? <Link to={item.path}>{dom}</Link> : dom)}
      subMenuItemRender={(_item, dom) => dom}
      breadcrumbRender={false}
      token={{
        sider: { colorMenuBackground: '#ffffff', colorTextMenuSelected: '#1677ff' },
        header: { colorBgHeader: '#ffffff' },
        pageContainer: { paddingInlinePageContainerContent: 24 },
      }}
      headerContentRender={() => (
        <Space size={12}>
          <Typography.Text type="secondary">{crumbs.join(' / ')}</Typography.Text>
        </Space>
      )}
      actionsRender={() => [
        policyVersion != null ? (
          <Tag key="pv" color="blue" title="策略版本（每次 ACL/角色变更 +1）">
            policy v{policyVersion}
          </Tag>
        ) : null,
        clearance != null ? (
          <span key="cl" title="当前身份密级">
            <ClearanceTag level={clearance} />
          </span>
        ) : null,
        <Button
          key="refresh"
          type="text"
          icon={<ReloadOutlined />}
          title="重新拉取权限快照（权限变更后点这里）"
          onClick={() => void refresh()}
        />,
        <Dropdown
          key="user"
          menu={{
            items: [
              { key: 'id', label: `用户 #${me?.user_id ?? '-'}`, disabled: true },
              {
                key: 'dept',
                label: `部门：${(me?.dept_names ?? []).join(' / ') || '-'}`,
                disabled: true,
              },
              { key: 'roles', label: `角色：${(me?.roles ?? []).join('、') || '-'}`, disabled: true },
              { key: 'fp', label: `指纹：${(fingerprint || '-').slice(0, 12)}…`, disabled: true },
              { type: 'divider' },
              {
                key: 'logout',
                icon: <LogoutOutlined />,
                label: '退出登录',
                onClick: () => {
                  logout()
                  navigate('/login')
                },
              },
            ],
          }}
        >
          <Button type="text" icon={<UserOutlined />}>
            {/* nick 只显示人名；部门/密级用结构化字段，避免 seed 把「Carol（总部/L1）」塞进 nick */}
            {me?.nick ?? me?.user_id ?? '未登录'}
            {me?.dept_names?.length ? (
              <Typography.Text type="secondary" style={{ marginLeft: 6, fontSize: 12 }}>
                {me.dept_names[me.dept_names.length - 1]}
              </Typography.Text>
            ) : null}
          </Button>
        </Dropdown>,
      ]}
      menuFooterRender={() =>
        (me?.permissions ?? []).length ? (
          <div style={{ padding: '8px 16px' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              <Badge status="processing" /> 权限点 {(me?.permissions ?? []).length} 个
            </Typography.Text>
          </div>
        ) : null
      }
    >
      <FileSearchOutlined style={{ display: 'none' }} />
      {/* 部署卫生：切回标签页时比对一次入口 chunk，页面过期就提示刷新（详见组件注释） */}
      <VersionWatcher />
      <Outlet />
    </ProLayout>
  )
}
