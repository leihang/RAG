/**
 * 全局布局（方案 05 §2.2 线框图）：左侧菜单 240px（收起 64px）+ 顶栏 56px + 内容区。
 *
 * 菜单树唯一来源：``nav.tsx``（可单测）。此处只负责图标与 ProLayout 壳。
 *
 * ⚠️ 顶栏身份区不用 ProLayout 的 ``actionsRender`` / ``avatarProps``：
 * 该组件在 ``side`` / ``mix`` 下会把用户操作塞进侧栏底部。
 */
import { useMemo, useState, type ReactNode } from 'react'
import { ProLayout } from '@ant-design/pro-components'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Badge, Button, Dropdown, Form, Input, Modal, Tag, Typography, message } from 'antd'
import {
  AuditOutlined,
  BookOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  NodeIndexOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { changePassword } from '@/api/auth'
import { usePermission } from '@/hooks/usePermission'
import { useAuth } from '@/hooks/useAuth'
import { useAuthStore } from '@/stores/authStore'
import ClearanceTag from '@/components/ClearanceTag'
import VersionWatcher from '@/components/VersionWatcher'
import { NAV, buildBreadcrumb, filterNav } from './nav'

const GROUP_ICONS: Record<string, ReactNode> = {
  '/dashboard': <DashboardOutlined />,
  '/pipeline': <NodeIndexOutlined />,
  '/docs': <BookOutlined />,
  '/iam': <SafetyCertificateOutlined />,
  '/logs': <AuditOutlined />,
}

export default function BasicLayout() {
  const location = useLocation()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)
  const [changingPassword, setChangingPassword] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)
  const [passwordForm] = Form.useForm<{ current_password: string; new_password: string; confirm: string }>()
  const { has, clearance, fingerprint } = usePermission()
  const { me, refresh, logout } = useAuth()
  const policyVersion = useAuthStore((s) => s.policyVersion)

  const menuData = useMemo(
    () =>
      filterNav(NAV, has).map((item) => ({
        path: item.path,
        name: item.name,
        icon: GROUP_ICONS[item.path],
        routes: item.children?.map((c) => ({ path: c.path, name: c.name })),
      })),
    [has],
  )

  const crumbs = buildBreadcrumb(location.pathname)
  const displayName = String(me?.nick ?? me?.user_id ?? '未登录')
  const deptTail = me?.dept_names?.length
    ? me.dept_names[me.dept_names.length - 1]
    : null

  const doLogout = () => {
    logout()
    navigate('/login')
  }

  const submitPassword = async () => {
    const values = await passwordForm.validateFields()
    setSavingPassword(true)
    try {
      await changePassword(values.current_password, values.new_password)
      message.success('口令已更新')
      setChangingPassword(false)
      passwordForm.resetFields()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <div className="ekbs-shell" role="application" aria-label="EKBS 管理台">
      <header className="ekbs-topbar">
        <div className="ekbs-topbar-left">
          <Typography.Text strong className="ekbs-topbar-brand">
            EKBS 管理台
          </Typography.Text>
          <Typography.Text type="secondary" className="ekbs-topbar-crumbs">
            {crumbs.join(' / ')}
          </Typography.Text>
        </div>
        <div className="ekbs-topbar-right">
          {policyVersion != null ? (
            <Tag color="blue" title="策略版本（每次 ACL/角色变更 +1）">
              policy v{policyVersion}
            </Tag>
          ) : null}
          {clearance != null ? (
            <span title="当前身份密级">
              <ClearanceTag level={clearance} />
            </span>
          ) : null}
          <Button
            type="text"
            icon={<ReloadOutlined />}
            title="重新拉取权限快照（权限变更后点这里）"
            onClick={() => void refresh()}
          />
          <Dropdown
            placement="bottomRight"
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
                  key: 'password',
                  label: '修改口令',
                  onClick: () => {
                    passwordForm.resetFields()
                    setChangingPassword(true)
                  },
                },
                {
                  key: 'logout',
                  icon: <LogoutOutlined />,
                  danger: true,
                  label: '退出登录',
                  onClick: doLogout,
                },
              ],
            }}
          >
            <Button type="text" className="ekbs-user-trigger" icon={<UserOutlined />}>
              <span className="ekbs-user-name">{displayName}</span>
              {deptTail ? (
                <Typography.Text type="secondary" className="ekbs-user-dept">
                  {deptTail}
                </Typography.Text>
              ) : null}
            </Button>
          </Dropdown>
        </div>
      </header>

      <div className="ekbs-shell-body">
        <ProLayout
          title={false}
          logo={false}
          layout="side"
          fixSiderbar
          headerRender={false}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          siderWidth={240}
          location={{ pathname: location.pathname }}
          route={{ path: '/', routes: menuData }}
          menuItemRender={(item, dom) => (item.path ? <Link to={item.path}>{dom}</Link> : dom)}
          subMenuItemRender={(_item, dom) => dom}
          breadcrumbRender={false}
          pageTitleRender={false}
          actionsRender={() => []}
          token={{
            sider: { colorMenuBackground: '#ffffff', colorTextMenuSelected: '#1677ff' },
            pageContainer: { paddingInlinePageContainerContent: 24 },
          }}
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
          <Modal
            title="修改口令"
            open={changingPassword}
            onCancel={() => setChangingPassword(false)}
            onOk={() => void submitPassword()}
            confirmLoading={savingPassword}
            okText="保存"
          >
            <Form form={passwordForm} layout="vertical">
              <Form.Item name="current_password" label="旧口令" rules={[{ required: true, message: '请输入旧口令' }]}>
                <Input.Password autoComplete="current-password" placeholder="先输入现在的口令" />
              </Form.Item>
              <Form.Item
                name="new_password"
                label="新口令"
                dependencies={['current_password']}
                rules={[
                  { required: true, message: '请输入新口令' },
                  { min: 6, message: '至少 6 位' },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || value !== getFieldValue('current_password')) return Promise.resolve()
                      return Promise.reject(new Error('新口令不能和旧口令相同'))
                    },
                  }),
                ]}
              >
                <Input.Password autoComplete="new-password" placeholder="至少 6 位" />
              </Form.Item>
              <Form.Item
                name="confirm"
                label="确认新口令"
                dependencies={['new_password']}
                rules={[
                  { required: true, message: '请再输入一次新口令' },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                      return Promise.reject(new Error('两次输入的新口令不一致'))
                    },
                  }),
                ]}
              >
                <Input.Password autoComplete="new-password" placeholder="再输入一次新口令" />
              </Form.Item>
            </Form>
          </Modal>
          <VersionWatcher />
          <Outlet />
        </ProLayout>
      </div>
    </div>
  )
}
