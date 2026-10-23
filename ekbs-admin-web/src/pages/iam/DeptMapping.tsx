/** 组织架构：钉钉部门树、直属人员，以及这些人的密级和角色。管理台账号不在这里。 */
import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Input, InputNumber, Modal, Select, Space, Tag, Tree, Typography, message } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { UserOutlined } from '@ant-design/icons'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import EmptyState from '@/components/EmptyState'
import {
  assignRole, listDepts, listRoles, orgSyncStatus, saveOrgSyncSchedule, setClearance, syncOrgNow,
  type DeptMember, type DeptRow, type OrgSyncStatus,
} from '@/api/iam'
import type { RoleItem } from '@/api/types'
import { buildOrgTree, collectDeptKeys, rootDeptKeys, type OrgBranch } from './deptTree'

const STATUS_LABEL: Record<string, string> = {
  active: '在职',
  left: '离职',
  disabled: '停用',
}

const CLEARANCE_OPTIONS = [
  { value: 1, label: 'L1 公开：只能看公开文档' },
  { value: 2, label: 'L2 内部：公开和内部' },
  { value: 3, label: 'L3 秘密：再加秘密文档' },
  { value: 4, label: 'L4 机密：全部密级' },
]

const MIN_REASON_LENGTH = 10

function MemberLabel({
  member, roleName, onClearance, onAssign,
}: {
  member: DeptMember
  roleName: (code: string) => string
  onClearance: (member: DeptMember) => void
  onAssign: (member: DeptMember) => void
}) {
  const status = member.status && member.status !== 'active' ? STATUS_LABEL[member.status] ?? member.status : ''
  return (
    <Space size={8} wrap>
      <UserOutlined />
      <span>{member.nick || member.dingtalk_userid}</span>
      {member.title ? <Typography.Text type="secondary">{member.title}</Typography.Text> : null}
      <Typography.Text copyable={{ text: String(member.user_id) }}>权限 ID {member.user_id}</Typography.Text>
      <ClearanceTag level={member.clearance} />
      {(member.roles ?? []).map((code) => <Tag key={code}>{roleName(code)}</Tag>)}
      {member.is_primary ? <Tag color="blue">主部门</Tag> : <Tag>兼任</Tag>}
      {status ? <Tag>{status}</Tag> : null}
      <Auth code="perm:user:set_clearance">
        <a onClick={(event) => { event.preventDefault(); onClearance(member) }}>改密级</a>
      </Auth>
      <Auth code="perm:role:assign">
        <a onClick={(event) => { event.preventDefault(); onAssign(member) }}>分配角色</a>
      </Auth>
    </Space>
  )
}

function DeptLabel({ dept, count }: { dept: DeptRow; count: number }) {
  return (
    <Space size={8} wrap>
      <span style={{ fontWeight: 600 }}>{dept.name || '（未命名）'}</span>
      <Typography.Text type="secondary" copyable={{ text: String(dept.dingtalk_dept_id) }}>
        ID {dept.dingtalk_dept_id}
      </Typography.Text>
      {dept.dept_code
        ? <Tag>分片 {dept.dept_code}</Tag>
        : <Typography.Text type="secondary">分片未配置</Typography.Text>}
      <Typography.Text type="secondary">直属 {count} 人</Typography.Text>
    </Space>
  )
}

function toTreeData(
  branches: OrgBranch[],
  roleName: (code: string) => string,
  onClearance: (member: DeptMember) => void,
  onAssign: (member: DeptMember) => void,
): DataNode[] {
  return branches.map((branch) => {
    const children: DataNode[] = [
      ...branch.members.map((member) => ({
        key: `user-${branch.dept.dept_id}-${member.user_id}`,
        isLeaf: true,
        selectable: false,
        title: (
          <MemberLabel
            member={member}
            roleName={roleName}
            onClearance={onClearance}
            onAssign={onAssign}
          />
        ),
      })),
      ...toTreeData(branch.children, roleName, onClearance, onAssign),
    ]
    return {
      key: `dept-${branch.dept.dept_id}`,
      selectable: false,
      title: <DeptLabel dept={branch.dept} count={branch.members.length} />,
      children: children.length ? children : undefined,
    }
  })
}

export default function DeptMapping() {
  const [rows, setRows] = useState<DeptRow[]>([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncInfo, setSyncInfo] = useState<OrgSyncStatus | null>(null)
  const [keyword, setKeyword] = useState('')
  const [expandedKeys, setExpandedKeys] = useState<string[]>([])
  const [roles, setRoles] = useState<RoleItem[]>([])
  const [hours, setHours] = useState(24)
  const [savingSchedule, setSavingSchedule] = useState(false)
  const [clearanceTarget, setClearanceTarget] = useState<DeptMember | null>(null)
  const [nextClearance, setNextClearance] = useState(2)
  const [assignTarget, setAssignTarget] = useState<DeptMember | null>(null)
  const [roleId, setRoleId] = useState<number | undefined>()
  const [acting, setActing] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const data = await listDepts()
      const items = data.items ?? []
      setRows(items)
      setExpandedKeys(rootDeptKeys(items))
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  const loadSync = () => {
    void orgSyncStatus().then((info) => {
      setSyncInfo(info)
      if (info.schedule?.interval_hours) setHours(info.schedule.interval_hours)
    }).catch(() => setSyncInfo(null))
  }

  useEffect(() => {
    void load()
    loadSync()
    void listRoles().then((res) => setRoles(res.items ?? [])).catch(() => setRoles([]))
  }, [])

  const tree = useMemo(() => buildOrgTree(rows, keyword), [rows, keyword])
  const treeData = useMemo(
    () => toTreeData(
      tree,
      (code) => roles.find((item) => item.code === code)?.name || code,
      (member) => {
        setClearanceTarget(member)
        setNextClearance(member.clearance ?? 2)
      },
      (member) => {
        setAssignTarget(member)
        setRoleId(undefined)
      },
    ),
    [tree, roles],
  )

  useEffect(() => {
    if (keyword.trim()) setExpandedKeys(collectDeptKeys(tree))
  }, [keyword, tree])

  const runSync = async () => {
    setSyncing(true)
    try {
      const report = await syncOrgNow()
      const scope = report.detail?.source === 'auth_scopes'
        ? `授权部门 ${report.detail.authed_departments ?? 0} 个、授权用户 ${report.detail.authed_users ?? 0} 人`
        : '按部门树'
      message.success(
        `已从钉钉更新部门和人员（${scope}）：部门 ${report.departments ?? 0}，人员 ${report.users ?? 0}，新增 ${report.created ?? 0}，变更 ${report.updated ?? 0}`,
      )
      loadSync()
      await load()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setSyncing(false) }
  }

  const saveSchedule = async (enabled: boolean) => {
    const value = Math.round(Number(hours))
    if (!Number.isFinite(value) || value < 1 || value > 720) {
      message.warning('间隔请填 1 到 720 的整数小时')
      return
    }
    setSavingSchedule(true)
    try {
      const saved = await saveOrgSyncSchedule({ enabled, interval_hours: value })
      setSyncInfo((prev) => ({ ...(prev ?? {}), schedule: saved }))
      setHours(saved.interval_hours)
      message.success(
        saved.enabled
          ? `已开启定时更新：每 ${saved.interval_hours} 小时同步一次部门和人员`
          : '已关闭定时更新',
      )
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setSavingSchedule(false)
    }
  }

  const applyClearance = async () => {
    if (!clearanceTarget) return
    const reason = window.prompt(`请输入变更原因（至少 ${MIN_REASON_LENGTH} 字）`) ?? ''
    if (reason.trim().length < MIN_REASON_LENGTH) {
      message.warning(`原因至少 ${MIN_REASON_LENGTH} 字`)
      return
    }
    setActing(true)
    try {
      await setClearance(clearanceTarget.user_id, nextClearance, reason.trim())
      message.success('已保存并记入审计')
      setClearanceTarget(null)
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const applyRole = async () => {
    if (!assignTarget || !roleId) return
    setActing(true)
    try {
      await assignRole(roleId, { user_id: assignTarget.user_id })
      message.success('已分配')
      setAssignTarget(null)
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const latest = syncInfo?.latest
  const schedule = syncInfo?.schedule
  const latestText = latest
    ? `最近一次：${latest.ended_at ?? latest.started_at ?? ''} · 部门与人员 · 新增 ${latest.created ?? 0} / 变更 ${latest.updated ?? 0}${latest.failed ? ' · 失败' : ''}`
    : '还没有从钉钉更新过'
  const scheduleText = schedule?.enabled
    ? `定时已开：每 ${schedule.interval_hours} 小时更新一次。下次大约 ${schedule.next_run_at ?? ''}`
    : '定时更新未开启'

  return (
    <Card
      title="组织架构"
      extra={
        <Space>
          <Auth code="perm:user:update">
            <Button type="primary" loading={syncing} onClick={() => void runSync()}>从钉钉更新</Button>
          </Auth>
          <Button onClick={() => void load()} loading={loading}>刷新</Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="「从钉钉更新」会按当前授权范围，同时重新拉取部门和人员。"
        description={`${latestText}。${scheduleText}。管理台自己的登录账号在「管理台账号」，不出现在这棵树里。`}
      />
      <Auth code="perm:user:update">
        <Space style={{ marginBottom: 12 }} wrap>
          <span>每隔</span>
          <InputNumber min={1} max={720} precision={0} value={hours} onChange={(value) => setHours(Number(value) || 1)} />
          <span>小时，从钉钉更新一次部门和人员</span>
          <Button loading={savingSchedule} onClick={() => void saveSchedule(true)}>保存定时更新</Button>
          {schedule?.enabled ? (
            <Button loading={savingSchedule} onClick={() => void saveSchedule(false)}>关闭定时</Button>
          ) : null}
        </Space>
      </Auth>
      <Typography.Paragraph type="secondary">
        点部门左侧箭头展开或收起。展开后先看到这个部门的直属人员，再看到下级部门。
        可以在人员这一行改密级、分配角色。钉钉同步不会改掉已经分配的后台角色。
        <br />
        「ID」是钉钉部门 ID，上传文档时的可见部门用它，点一下即可复制。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          allowClear
          style={{ width: 280 }}
          placeholder="搜部门、姓名、职位或部门 ID"
          value={keyword}
          onChange={(e) => {
            const next = e.target.value
            setKeyword(next)
            if (!next.trim()) setExpandedKeys(rootDeptKeys(rows))
          }}
        />
        <Button onClick={() => setExpandedKeys(collectDeptKeys(tree))}>全部展开</Button>
        <Button onClick={() => setExpandedKeys([])}>全部收起</Button>
      </Space>
      {treeData.length === 0 && !loading
        ? <EmptyState description={keyword ? '没有匹配的部门或人员' : '没有部门数据，先从钉钉更新一次'} />
        : (
          <Tree
            showLine
            blockNode
            virtual
            height={560}
            treeData={treeData}
            expandedKeys={expandedKeys}
            onExpand={(keys) => setExpandedKeys(keys.map(String))}
          />
        )}
      <Modal
        title={`改密级：${clearanceTarget?.nick ?? ''}`}
        open={clearanceTarget !== null}
        onCancel={() => setClearanceTarget(null)}
        onOk={() => void applyClearance()}
        confirmLoading={acting}
        okText="提交变更"
        okButtonProps={{ danger: true }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select style={{ width: '100%' }} value={nextClearance} options={CLEARANCE_OPTIONS} onChange={setNextClearance} />
          <Typography.Text type="secondary">调高或调低都会写入审计。原因至少 {MIN_REASON_LENGTH} 个字。</Typography.Text>
        </Space>
      </Modal>
      <Modal
        title={`分配角色：${assignTarget?.nick ?? ''}`}
        open={assignTarget !== null}
        onCancel={() => setAssignTarget(null)}
        onOk={() => void applyRole()}
        confirmLoading={acting}
      >
        <Select
          style={{ width: '100%' }}
          placeholder="选择角色"
          value={roleId}
          onChange={setRoleId}
          options={roles.map((item) => ({ value: item.role_id, label: `${item.name}` }))}
        />
      </Modal>
    </Card>
  )
}
