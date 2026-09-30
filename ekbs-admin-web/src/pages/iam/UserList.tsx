/** 用户管理（方案 05 §4.8）。改密级是 §5.5 的 L-C 高危操作：原因必填 ≥10 字并落审计。 */
import { useEffect, useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, InputNumber, Modal, Select, Space, Typography, message } from 'antd'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import EmptyState from '@/components/EmptyState'
import ImpactConfirmModal, { MIN_REASON_LENGTH } from '@/pages/docs/ImpactConfirmModal'
import { assignRole, listRoles, listUsers, setClearance } from '@/api/iam'
import type { RoleItem, UserItem } from '@/api/types'

export default function UserList() {
  const actionRef = useRef<ActionType>()
  const [roles, setRoles] = useState<RoleItem[]>([])
  const [clearanceTarget, setClearanceTarget] = useState<UserItem | null>(null)
  const [nextClearance, setNextClearance] = useState(2)
  const [assignTarget, setAssignTarget] = useState<UserItem | null>(null)
  const [roleId, setRoleId] = useState<number | undefined>()
  const [loading, setLoading] = useState(false)

  useEffect(() => { void listRoles().then((r) => setRoles(r.items ?? [])).catch(() => setRoles([])) }, [])

  const columns: ProColumns<UserItem>[] = [
    { title: 'ID', dataIndex: 'user_id', width: 64, search: false },
    { title: '钉钉 userid', dataIndex: 'dingtalk_userid', width: 150, copyable: true },
    { title: '昵称', dataIndex: 'nick', ellipsis: true },
    { title: '密级', dataIndex: 'clearance', width: 110, search: false, render: (_, r) => <ClearanceTag level={r.clearance} /> },
    { title: '角色', dataIndex: 'roles', width: 160, search: false,
      render: (_, r) => <Space size={4} wrap>{(r.roles ?? []).map((x) => <span key={x}>{x}</span>)}</Space> },
    { title: '状态', dataIndex: 'status', width: 90, search: false },
    { title: '操作', valueType: 'option', width: 170, fixed: 'right', render: (_, r) => [
      <Auth key="cl" code="perm:user:set_clearance">
        <a onClick={() => { setClearanceTarget(r); setNextClearance(r.clearance) }}>改密级</a>
      </Auth>,
      <Auth key="role" code="perm:role:assign">
        <a onClick={() => { setAssignTarget(r); setRoleId(undefined) }}>分配角色</a>
      </Auth>,
    ] },
  ]

  const applyClearance = async (payload: { reason?: string }) => {
    if (!clearanceTarget || !payload.reason) return
    setLoading(true)
    try {
      await setClearance(clearanceTarget.user_id, nextClearance, payload.reason)
      message.success('已保存并记入审计')
      setClearanceTarget(null)
      actionRef.current?.reload()
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
      actionRef.current?.reload()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  return (
    <>
      <ProTable<UserItem>
        headerTitle="用户管理"
        rowKey="user_id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 900 }}
        options={{ density: true, setting: true, reload: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        request={async (params) => {
          const res = await listUsers({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            q: (params as Record<string, unknown>).nick as string | undefined,
          })
          return { data: res.items, total: res.total, success: true }
        }}
        locale={{ emptyText: <EmptyState description="还没有用户，先做一次组织同步" /> }}
      />

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
          <InputNumber min={1} max={4} value={nextClearance} onChange={(v) => setNextClearance(Number(v ?? 2))} />
          <Typography.Text type="secondary">
            改密级会改变该用户可见的分片范围（C1），并写入权限变更审计。
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

      <ImpactConfirmModal
        open={false}
        level="C"
        title=""
        consequence={null}
        onCancel={() => setClearanceTarget(null)}
        onOk={() => undefined}
      />
    </>
  )
}
