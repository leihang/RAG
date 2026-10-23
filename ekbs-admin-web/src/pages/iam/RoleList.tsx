/** 角色管理（方案 05 §4.8）。内置角色禁删。权限点按中文名勾选，提交仍用权限码。 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Checkbox, Form, Input, Modal, Tag, Typography, message } from 'antd'
import Auth from '@/components/Auth'
import EmptyState from '@/components/EmptyState'
import { createRole, deleteRole, listPermissions, listRoles, updateRole } from '@/api/iam'
import type { PermissionItem, RoleItem } from '@/api/types'

const MODULE_ZH: Record<string, string> = {
  doc: '文档',
  kb: '知识库',
  parse: '解析',
  rag: '检索与调试',
  audit: '审计',
  perm: '权限',
  sys: '系统',
  model: '模型',
  eval: '评估',
}

const BACKEND_ROLES = new Set(['super_admin', 'kb_admin', 'doc_reviewer', 'auditor'])

export default function RoleList() {
  const actionRef = useRef<ActionType>()
  const [builtin, setBuiltin] = useState<string[]>([])
  const [catalog, setCatalog] = useState<PermissionItem[]>([])
  const [editing, setEditing] = useState<RoleItem | null>(null)
  const [creating, setCreating] = useState(false)
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm<{ code: string; name: string; permissions: string[] }>()

  useEffect(() => {
    void listPermissions().then((res) => setCatalog(res.items ?? [])).catch(() => setCatalog([]))
  }, [])

  const nameOf = useMemo(() => {
    const map = new Map(catalog.map((item) => [item.code, item.name]))
    return (code: string) => map.get(code) || code
  }, [catalog])

  const grouped = useMemo(() => {
    const buckets = new Map<string, PermissionItem[]>()
    for (const item of catalog) {
      const list = buckets.get(item.module) ?? []
      list.push(item)
      buckets.set(item.module, list)
    }
    return [...buckets.entries()]
  }, [catalog])

  const columns: ProColumns<RoleItem>[] = [
    { title: 'code', dataIndex: 'code', width: 160 },
    { title: '名称', dataIndex: 'name', width: 180 },
    { title: '类型', width: 120, search: false,
      render: (_, r) => (
        BACKEND_ROLES.has(r.code) ? <Tag color="blue">后台角色</Tag>
          : r.code === 'end_user' ? <Tag>钉钉用户</Tag>
            : <Tag>自定义</Tag>
      ) },
    { title: '权限', search: false, render: (_, r) => {
      const names = (r.permissions ?? []).map(nameOf)
      if (!names.length) return '-'
      const head = names.slice(0, 6).join('、')
      return names.length > 6 ? `${head} 等 ${names.length} 项` : head
    } },
    { title: '用户数', dataIndex: 'user_count', width: 100, search: false },
    { title: '操作', valueType: 'option', width: 150, fixed: 'right', render: (_, r) => {
      const isBuiltin = Boolean(r.is_builtin) || builtin.includes(r.code)
      return [
        <Auth key="e" code="perm:role:update">
          <a onClick={() => { setEditing(r); form.setFieldsValue({ code: r.code, name: r.name, permissions: r.permissions ?? [] }) }}>编辑</a>
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
    const permissions = v.permissions ?? []
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
      <Typography.Paragraph type="secondary">
        超级管理员、知识库管理员、文档审核员、审计员是后台角色，不按钉钉部门收窄管理台里的文档和检索。
        钉钉同步只给新员工「普通用户」，不会改这四个角色。钉钉里提问仍按提问人自己的部门。
      </Typography.Paragraph>
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
        width={720}
      >
        <Form form={form} layout="vertical" initialValues={{ permissions: [] }}>
          <Form.Item name="code" label="角色代号" rules={editing ? [] : [{ required: true, message: '必填' }]}>
            <Input disabled={Boolean(editing)} placeholder="如 doc_reviewer" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '必填' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="permissions" label="权限">
            <Checkbox.Group style={{ width: '100%' }}>
              <div style={{ maxHeight: 360, overflow: 'auto' }}>
                {grouped.map(([module, items]) => (
                  <div key={module} style={{ marginBottom: 12 }}>
                    <Typography.Text strong>{MODULE_ZH[module] || module}</Typography.Text>
                    <div>
                      {items.map((item) => (
                        <Checkbox key={item.code} value={item.code} style={{ marginInlineStart: 0, width: '46%' }}>
                          {item.name}
                        </Checkbox>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </Checkbox.Group>
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
