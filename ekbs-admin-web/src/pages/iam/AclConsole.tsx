/** ACL 配置（方案 05 §4.8）。deny 优先，必须红色强提示（§1.4）。 */
import { useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Alert, Button, Checkbox, Form, Input, Modal, Select, Space, Tabs, Tag, Typography, message } from 'antd'
import Auth from '@/components/Auth'
import EmptyState from '@/components/EmptyState'
import { createAcl, deleteAcl, listAcl, simulateAcl } from '@/api/iam'
import type { AclItem } from '@/api/types'

const ACTION_ZH: Record<string, string> = {
  read: '阅读',
  write: '修改',
  manage: '管理',
  audit: '审计',
}
const EFFECT_ZH: Record<string, string> = {
  allow: '允许',
  deny: '拒绝',
}
const SUBJECT_ZH: Record<string, string> = {
  user: '用户',
  dept: '部门',
  role: '角色',
  group: '用户组',
}
const ACTIONS = Object.keys(ACTION_ZH)
const SUBJECT_TABS = Object.entries(SUBJECT_ZH).map(([key, label]) => ({ key, label }))

export default function AclConsole() {
  const actionRef = useRef<ActionType>()
  const [creating, setCreating] = useState(false)
  const [simulating, setSimulating] = useState(false)
  const [subjectType, setSubjectType] = useState('user')
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()
  const [simForm] = Form.useForm()

  const columns: ProColumns<AclItem>[] = [
    { title: 'ID', dataIndex: 'acl_id', width: 70, search: false },
    { title: '主体类型', dataIndex: 'subject_type', width: 110,
      render: (_, r) => SUBJECT_ZH[r.subject_type] || r.subject_type },
    { title: '主体', dataIndex: 'subject_id', width: 160 },
    { title: '作用对象', width: 180, search: false,
      render: (_, r) => (r.doc_id ? `文档 #${r.doc_id}` : r.kb_id ? `知识库 #${r.kb_id}` : '全局') },
    { title: '动作', dataIndex: 'action', width: 100,
      render: (_, r) => ACTION_ZH[r.action] || r.action },
    { title: '效果', dataIndex: 'effect', width: 120,
      render: (_, r) => (r.effect === 'deny'
        ? <Tag color="red">拒绝（优先）</Tag>
        : <Tag color="green">允许</Tag>) },
    { title: '创建时间', dataIndex: 'created_at', width: 170, search: false },
    { title: '操作', valueType: 'option', width: 90, fixed: 'right', render: (_, r) => [
      <Auth key="d" code="perm:acl:delete">
        <a onClick={() => void remove(r)}>撤销</a>
      </Auth>,
    ] },
  ]

  const remove = async (r: AclItem) => {
    const who = SUBJECT_ZH[r.subject_type] || r.subject_type
    const act = ACTION_ZH[r.action] || r.action
    const effect = EFFECT_ZH[r.effect] || r.effect
    if (!window.confirm(`确认撤销这条规则（${who} ${r.subject_id}：${act} / ${effect}）？`)) return
    try { await deleteAcl(r.acl_id); message.success('已撤销'); actionRef.current?.reload() }
    catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  const submit = async () => {
    const v = await form.validateFields()
    setLoading(true)
    try {
      await createAcl({ ...v, subject_type: subjectType })
      message.success('已新增')
      setCreating(false); form.resetFields(); actionRef.current?.reload()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const runSimulate = async () => {
    const v = await simForm.validateFields()
    setLoading(true)
    try {
      const docIds = String(v.doc_ids).split(/[,，\s]+/).map((s: string) => Number(s.trim())).filter(Boolean)
      setResult(await simulateAcl({ user_id: Number(v.user_id), doc_ids: docIds, reason: String(v.reason ?? '管理台权限模拟') }))
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="ACL 是给单份文档或单个知识库开的例外门"
        description={
          <span>
            角色决定能进哪些管理页面，部门和密级决定平时能看到哪些文件。ACL 只处理例外：
            某个人、部门、角色或用户组，对某一份文档或某一个知识库，额外允许或额外拒绝。
            「拒绝」优先于「允许」。允许也不能抬高密级：密级不够的人，即使加了允许，仍然看不到更高密级的文件。
            日常权限用角色和密级，不要把每个人都做成一条 ACL。
          </span>
        }
      />
      <ProTable<AclItem>
        headerTitle="ACL 配置"
        rowKey="acl_id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1000 }}
        options={{ density: true, reload: true }}
        pagination={{ defaultPageSize: 20 }}
        request={async (params) => {
          const res = await listAcl({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
          })
          return { data: res.items, total: res.total, success: true }
        }}
        toolBarRender={() => [
          <Auth key="new" code="perm:acl:create">
            <Button type="primary" onClick={() => setCreating(true)}>新增授权</Button>
          </Auth>,
          <Auth key="sim" code="perm:acl:simulate">
            <Button onClick={() => setSimulating(true)}>权限模拟</Button>
          </Auth>,
        ]}
        locale={{ emptyText: <EmptyState description="还没有 ACL 规则，点击「新增授权」创建第一条" /> }}
      />

      <Modal title="新增 ACL 授权" open={creating} onCancel={() => setCreating(false)} onOk={() => void submit()} confirmLoading={loading}>
        <Tabs activeKey={subjectType} onChange={setSubjectType} items={SUBJECT_TABS} />
        <Form form={form} layout="vertical" initialValues={{ effect: 'allow', action: 'read' }}>
          <Form.Item name="subject_id" label={`${subjectType} 标识`} rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="用户 id / 部门 id / 角色 code" />
          </Form.Item>
          <Form.Item name="kb_id" label="知识库 ID（与文档 ID 二选一）">
            <Input />
          </Form.Item>
          <Form.Item name="doc_id" label="文档 ID">
            <Input />
          </Form.Item>
          <Form.Item name="action" label="动作" rules={[{ required: true }]}>
            <Select options={ACTIONS.map((a) => ({ value: a, label: ACTION_ZH[a] }))} />
          </Form.Item>
          <Form.Item name="effect" label="效果" rules={[{ required: true }]}
            extra="拒绝优先于允许：命中拒绝时，其它允许规则也不放行">
            <Select options={[{ value: 'allow', label: '允许' }, { value: 'deny', label: '拒绝（优先）' }]} />
          </Form.Item>
          <Checkbox onChange={(e) => { if (!e.target.checked) form.setFieldValue('effect', 'allow') }}>
            我已知晓「拒绝」会立刻拦住这个主体访问该对象
          </Checkbox>
        </Form>
      </Modal>

      <Modal title="权限模拟（以他人身份判断可读性）" open={simulating} onCancel={() => setSimulating(false)}
        onOk={() => void runSimulate()} confirmLoading={loading} width={560}>
        <Typography.Paragraph type="warning">
          这是高危只读操作：会以指定用户身份判断文档可读性，并记入安全事件。
        </Typography.Paragraph>
        <Form form={simForm} layout="vertical">
          <Form.Item name="user_id" label="用户 ID" rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="2" />
          </Form.Item>
          <Form.Item name="doc_ids" label="文档 ID（逗号分隔）" rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="1,2,3" />
          </Form.Item>
          <Form.Item name="reason" label="原因（≥10 字）" rules={[{ required: true, min: 10, message: '至少 10 字' }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
        {result ? <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>{JSON.stringify(result, null, 2)}</pre> : null}
      </Modal>
    </Space>
  )
}
