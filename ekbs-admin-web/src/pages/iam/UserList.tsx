/** 管理台账号。只登记能用登录名进管理台的人，不和钉钉组织架构混在一起。 */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Typography, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import EmptyState from '@/components/EmptyState'
import { changePassword } from '@/api/auth'
import { assignRole, createUser, listRoles, listUsers, resetPassword, setClearance } from '@/api/iam'
import { useAuth } from '@/hooks/useAuth'
import type { RoleItem, UserItem } from '@/api/types'

const MIN_REASON_LENGTH = 10

export default function UserList() {
  const [roles, setRoles] = useState<RoleItem[]>([])
  const [users, setUsers] = useState<UserItem[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [clearanceTarget, setClearanceTarget] = useState<UserItem | null>(null)
  const [nextClearance, setNextClearance] = useState(2)
  const [assignTarget, setAssignTarget] = useState<UserItem | null>(null)
  const [roleId, setRoleId] = useState<number | undefined>()
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [resetTarget, setResetTarget] = useState<UserItem | null>(null)
  const [changingOwn, setChangingOwn] = useState(false)
  const { me } = useAuth()
  const [createForm] = Form.useForm<{
    username: string
    password: string
    confirm_password: string
    nick: string
    clearance: number
    role_code?: string
  }>()
  const [resetForm] = Form.useForm<{ password: string; confirm_password: string }>()
  const [ownForm] = Form.useForm<{ current_password: string; new_password: string; confirm: string }>()

  useEffect(() => { void listRoles().then((r) => setRoles(r.items ?? [])).catch(() => setRoles([])) }, [])

  const CLEARANCE_OPTIONS = [
    { value: 1, label: 'L1 公开：只能看公开文档' },
    { value: 2, label: 'L2 内部：公开和内部' },
    { value: 3, label: 'L3 秘密：再加秘密文档' },
    { value: 4, label: 'L4 机密：全部密级' },
  ]

  const submitCreate = async () => {
    const values = await createForm.validateFields()
    setLoading(true)
    try {
      await createUser({
        username: values.username.trim(),
        password: values.password,
        nick: values.nick.trim(),
        clearance: values.clearance,
        role_code: values.role_code || undefined,
      })
      message.success('已新建，可用该登录名和口令进入管理台')
      setCreating(false)
      createForm.resetFields()
      void loadList()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const loadList = async () => {
    setListLoading(true)
    try {
      const userRes = await listUsers({ limit: 2000, auth_source: 'local' })
      setUsers(userRes.items ?? [])
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setListLoading(false)
    }
  }

  useEffect(() => { void loadList() }, [])

  const shown = users.filter((user) => {
    const query = keyword.trim().toLowerCase()
    if (!query) return true
    return `${user.nick ?? ''} ${user.username ?? ''}`.toLowerCase().includes(query)
  })

  const columns: ColumnsType<UserItem> = [
    { title: '姓名', dataIndex: 'nick', ellipsis: true },
    { title: '登录名', dataIndex: 'username', width: 160, render: (value) => value || '—' },
    {
      title: '密级', width: 110,
      render: (_, row) => <ClearanceTag level={row.clearance} />,
    },
    {
      title: '角色', width: 220,
      render: (_, row) => (
        <Space size={4} wrap>
          {(row.roles ?? []).map((code) => {
            const named = roles.find((item) => item.code === code)
            return <span key={code}>{named?.name || code}</span>
          })}
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 90 },
    {
      title: '操作', width: 240,
      render: (_, person) => (
        <Space size={8} wrap>
          <Auth code="perm:user:set_clearance">
            <a onClick={() => { setClearanceTarget(person); setNextClearance(person.clearance) }}>改密级</a>
          </Auth>
          <Auth code="perm:role:assign">
            <a onClick={() => { setAssignTarget(person); setRoleId(undefined) }}>分配角色</a>
          </Auth>
          {person.username && person.user_id === me?.user_id ? (
            <a onClick={() => { ownForm.resetFields(); setChangingOwn(true) }}>修改口令</a>
          ) : person.username ? (
            <Auth code="perm:user:update">
              <a onClick={() => { resetForm.resetFields(); setResetTarget(person) }}>重置口令</a>
            </Auth>
          ) : null}
        </Space>
      ),
    },
  ]

  const applyClearance = async (payload: { reason?: string }) => {
    if (!clearanceTarget || !payload.reason) return
    setLoading(true)
    try {
      await setClearance(clearanceTarget.user_id, nextClearance, payload.reason)
      message.success('已保存并记入审计')
      setClearanceTarget(null)
      void loadList()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const applyRole = async () => {
    if (!assignTarget || !roleId) return
    setLoading(true)
    try {
      await assignRole(roleId, { user_id: assignTarget.user_id })
      message.success('已分配')
      setAssignTarget(null)
      void loadList()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  return (
    <Card title="管理台账号">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里只登记管理台自己的登录账号"
        description="钉钉同事在「组织架构」里看和改密级、角色。这边新建的人用登录名和口令进管理台，不会出现在钉钉部门树里。"
      />
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          allowClear
          value={keyword}
          placeholder="搜姓名或登录名"
          style={{ width: 260 }}
          onChange={(event) => setKeyword(event.target.value)}
        />
        <Button onClick={() => void loadList()} loading={listLoading}>刷新</Button>
        <Auth code="perm:user:create">
          <Button type="primary" onClick={() => { createForm.resetFields(); setCreating(true) }}>新建用户</Button>
        </Auth>
      </Space>
      <Table<UserItem>
        rowKey="user_id"
        size="small"
        loading={listLoading}
        columns={columns}
        dataSource={shown}
        pagination={false}
        locale={{ emptyText: <EmptyState description="还没有管理台账号。点「新建用户」登记，不要和钉钉组织混在一起。" /> }}
      />

      <Modal
        title="新建用户"
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => void submitCreate()}
        confirmLoading={loading}
        okText="创建"
      >
        <Form form={createForm} layout="vertical" initialValues={{ clearance: 2 }}>
          <Form.Item name="nick" label="姓名" rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="显示在管理台顶栏的名字" />
          </Form.Item>
          <Form.Item name="username" label="登录名" rules={[{ required: true, message: '必填' }, { min: 2, message: '至少 2 个字符' }]}>
            <Input placeholder="登录页填写的名字，不要用空格" autoComplete="off" />
          </Form.Item>
          <Form.Item name="password" label="口令" rules={[{ required: true, message: '请输入口令' }, { min: 6, message: '至少 6 位' }]}>
            <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="确认口令"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再输入一次口令' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) return Promise.resolve()
                  return Promise.reject(new Error('两次输入的口令不一致'))
                },
              }),
            ]}
          >
            <Input.Password placeholder="再输入一次口令" autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="clearance" label="密级" extra="只决定能看哪一级文档，不决定能进哪些管理页面。">
            <Select options={CLEARANCE_OPTIONS} />
          </Form.Item>
          <Form.Item name="role_code" label="角色" extra="要进管理台，至少选一个带管理权限的角色，例如超级管理员或知识库管理员。">
            <Select
              allowClear
              placeholder="不选则只能登录，没有管理菜单"
              options={roles.map((r) => ({ value: r.code, label: r.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`改密级：${clearanceTarget?.nick ?? ''}`}
        open={clearanceTarget !== null}
        onCancel={() => setClearanceTarget(null)}
        onOk={() => { /* 由 ImpactConfirmModal 承担，这里不直接提交 */ }}
        footer={null}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            当前 <ClearanceTag level={clearanceTarget?.clearance} /> → 目标
          </Typography.Text>
          <Select style={{ width: '100%' }} value={nextClearance} options={CLEARANCE_OPTIONS} onChange={setNextClearance} />
          <Typography.Text type="secondary">
            调高后能看到更高密级的文档，调低后那些文档从检索和问答里消失。变更会写入审计。
          </Typography.Text>
          <Button
            type="primary"
            danger
            disabled={!clearanceTarget}
            onClick={() => {
              if (!clearanceTarget) return
              const reason = window.prompt(`请输入变更原因（至少 ${MIN_REASON_LENGTH} 字）`) ?? ''
              if (reason.trim().length < MIN_REASON_LENGTH) {
                message.warning(`原因至少 ${MIN_REASON_LENGTH} 字`)
                return
              }
              void applyClearance({ reason: reason.trim() })
            }}
          >
            提交变更
          </Button>
        </Space>
      </Modal>

      <Modal
        title="修改口令"
        open={changingOwn}
        onCancel={() => setChangingOwn(false)}
        okText="确认修改"
        confirmLoading={loading}
        onOk={() => {
          void ownForm.validateFields().then((values) => {
            setLoading(true)
            void changePassword(values.current_password, values.new_password)
              .then(() => {
                message.success('口令已更新，下次请用新口令登录')
                setChangingOwn(false)
                ownForm.resetFields()
              })
              .catch((err) => message.error(err instanceof Error ? err.message : String(err)))
              .finally(() => setLoading(false))
          })
        }}
      >
        <Form form={ownForm} layout="vertical">
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

      <Modal
        title={`重置口令：${resetTarget?.nick ?? resetTarget?.username ?? ''}`}
        open={resetTarget !== null}
        onCancel={() => setResetTarget(null)}
        okText="确认修改"
        confirmLoading={loading}
        onOk={() => {
          if (!resetTarget) return
          void resetForm.validateFields().then((values) => {
            setLoading(true)
            void resetPassword(resetTarget.user_id, values.password)
              .then(() => {
                message.success('口令已更新')
                setResetTarget(null)
                resetForm.resetFields()
              })
              .catch((err) => message.error(err instanceof Error ? err.message : String(err)))
              .finally(() => setLoading(false))
          })
        }}
      >
        <Form form={resetForm} layout="vertical">
          <Form.Item name="password" label="新口令" rules={[{ required: true, message: '请输入新口令' }, { min: 6, message: '至少 6 位' }]}>
            <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="确认新口令"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再输入一次新口令' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) return Promise.resolve()
                  return Promise.reject(new Error('两次输入的新口令不一致'))
                },
              }),
            ]}
          >
            <Input.Password placeholder="再输入一次新口令" autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={`分配角色：${assignTarget?.nick ?? ''}`} open={assignTarget !== null}
        onCancel={() => setAssignTarget(null)} onOk={() => void applyRole()} confirmLoading={loading}>
        <Select
          style={{ width: '100%' }}
          placeholder="选择角色"
          value={roleId}
          onChange={setRoleId}
          options={roles.map((r) => ({ value: r.role_id, label: `${r.code} · ${r.name}` }))}
        />
      </Modal>
    </Card>
  )
}
