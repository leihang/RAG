/** 角色管理（方案 05 §4.8）。内置角色禁删；权限点目录接口后端未提供，故用文本输入。 */
import { useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Form, Input, Modal, Space, Tag, Typography, message } from 'antd'
import Auth from '@/components/Auth'
import EmptyState from '@/components/EmptyState'
import { createRole, deleteRole, listRoles, updateRole } from '@/api/iam'
import type { RoleItem } from '@/api/types'

export default function RoleList() {
  const actionRef = useRef<ActionType>()
  const [builtin, setBuiltin] = useState<string[]>([])
  const [editing, setEditing] = useState<RoleItem | null>(null)
  const [creating, setCreating] = useState(false)
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm<{ code: string; name: string; permissions: string }>()

  const columns: ProColumns<RoleItem>[] = [
    { title: 'code', dataIndex: 'code', width: 160 },
    { title: '名称', dataIndex: 'name', width: 180 },
    { title: '类型', width: 100, search: false,
      render: (_, r) => (r.is_builtin || builtin.includes(r.code) ? <Tag color="blue">内置</Tag> : <Tag>自定义</Tag>) },
    { title: '权限点数', width: 110, search: false, render: (_, r) => (r.permissions?.length ?? '-') },
    { title: '用户数', dataIndex: 'user_count', width: 100, search: false },
    { title: '操作', valueType: 'option', width: 150, fixed: 'right', render: (_, r) => {
      const isBuiltin = Boolean(r.is_builtin) || builtin.includes(r.code)
      return [
        <Auth key="e" code="perm:role:update">
          <a onClick={() => { setEditing(r); form.setFieldsValue({ code: r.code, name: r.name, permissions: (r.permissions ?? []).join(',') }) }}>编辑</a>
        </Auth>,
        <Auth key="d" code="perm:role:delete">
          {isBuiltin ? <Typography.Text type="secondary">内置不可删</Typography.Text> : <a onClick={() => void remove(r)}>删除</a>}
        </Auth>,
      ]
    } },
  ]

  const remove = async (r: RoleItem) => {
    if (!window.confirm(`确认删除角色「${r.name}」？该操作不可撤销。`)) return
    try { await deleteRole(r.role_id); message.success('已删除'); actionRef.current?.reload() }
    catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const submit = async () => {
    const v = await form.validateFields()
    const permissions = v.permissions.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    setLoading(true)
    try {
      if (editing) await updateRole(editing.role_id, { name: v.name, permissions })
      else await createRole({ code: v.code, name: v.name, permissions })
      message.success('已保存')
      setEditing(null); setCreating(false); form.resetFields(); actionRef.current?.reload()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  return (
    <>
      <ProTable<RoleItem>
        headerTitle="角色管理"
        rowKey="role_id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        options={{ density: true, reload: true }}
        pagination={false}
        toolBarRender={() => [
          <Auth key="new" code="perm:role:create">
            <Button type="primary" onClick={() => { setCreating(true); form.resetFields() }}>新建角色</Button>
          </Auth>,
        ]}
        request={async () => {
          const res = await listRoles()
          setBuiltin(res.builtin_codes ?? [])
          return { data: res.items ?? [], success: true }
        }}
        locale={{ emptyText: <EmptyState description="还没有自定义角色" /> }}
      />

      <Modal
        title={editing ? `编辑角色：${editing.code}` : '新建角色'}
        open={editing !== null || creating}
        onCancel={() => { setEditing(null); setCreating(false) }}
        onOk={() => void submit()}
        confirmLoading={loading}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="code" label="角色 code" rules={editing ? [] : [{ required: true, message: '必填' }]}>
            <Input disabled={Boolean(editing)} placeholder="如 doc_reviewer" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '必填' }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="permissions"
            label="权限点（逗号分隔）"
            extra="权限点目录接口尚未提供，这里用文本输入；按权限码填，如 doc:document:read"
          >
            <Input.TextArea rows={4} placeholder="doc:document:read,parse:task:read" />
          </Form.Item>
          <Space />
        </Form>
      </Modal>
    </>
  )
}
