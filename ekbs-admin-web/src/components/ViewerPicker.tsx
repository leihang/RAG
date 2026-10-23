/**
 * 入库时按组织架构多选「额外可查看的人」。提交值是用户编号。
 * 部门节点只用来折叠和一次勾选该部门下的人，不会把部门 ID 写进文件。
 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { TreeSelect } from 'antd'
import { listDeptOptions, listViewerOptions, type DeptOption, type ViewerOption } from '@/api/import'

type ViewerNode = {
  title: string
  value: string
  key: string
  children?: ViewerNode[]
  disableCheckbox?: boolean
}

function userLabel(user: ViewerOption): string {
  const title = user.title ? ` · ${user.title}` : ''
  return `${user.nick || '未命名'}${title} · L${user.clearance}`
}

function buildViewerTree(depts: DeptOption[], users: ViewerOption[]): ViewerNode[] {
  const deptIds = new Set(depts.map((dept) => dept.dept_id))
  const children = new Map<number | null, DeptOption[]>()
  for (const dept of depts) {
    const parent = dept.parent_dept_id
    const key = parent != null && deptIds.has(parent) && parent !== dept.dept_id ? parent : null
    const list = children.get(key) ?? []
    list.push(dept)
    children.set(key, list)
  }
  for (const list of children.values()) {
    list.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN') || a.dept_id - b.dept_id)
  }

  const usersByDept = new Map<number, ViewerOption[]>()
  const unassigned: ViewerOption[] = []
  for (const user of users) {
    const deptId = user.dept_id
    if (deptId == null || !deptIds.has(deptId)) {
      unassigned.push(user)
      continue
    }
    const list = usersByDept.get(deptId) ?? []
    list.push(user)
    usersByDept.set(deptId, list)
  }
  for (const list of usersByDept.values()) {
    list.sort((a, b) => (a.nick || '').localeCompare(b.nick || '', 'zh-CN') || a.user_id - b.user_id)
  }
  unassigned.sort((a, b) => (a.nick || '').localeCompare(b.nick || '', 'zh-CN') || a.user_id - b.user_id)

  const userNodes = (list: ViewerOption[]): ViewerNode[] =>
    list.map((user) => ({
      title: userLabel(user),
      value: `user:${user.user_id}`,
      key: `user:${user.user_id}`,
    }))

  const deptNode = (dept: DeptOption): ViewerNode & { userCount: number } => {
    const nested = (children.get(dept.dept_id) ?? []).map(deptNode)
    const people = userNodes(usersByDept.get(dept.dept_id) ?? [])
    const userCount = people.length + nested.reduce((sum, node) => sum + node.userCount, 0)
    return {
      title: dept.name,
      value: `dept:${dept.dept_id}`,
      key: `dept:${dept.dept_id}`,
      disableCheckbox: userCount === 0,
      userCount,
      children: [...nested.map(({ userCount: _count, ...node }) => node), ...people],
    }
  }

  const roots = (children.get(null) ?? []).map(deptNode).map(({ userCount: _count, ...node }) => node)
  if (unassigned.length) {
    roots.push({
      title: '未分配部门',
      value: 'dept:none',
      key: 'dept:none',
      children: userNodes(unassigned),
    })
  }
  return roots
}

export default function ViewerPicker({
  value,
  onChange,
  style,
}: {
  value?: number[]
  onChange?: (ids: number[]) => void
  style?: CSSProperties
}) {
  const [depts, setDepts] = useState<DeptOption[]>([])
  const [users, setUsers] = useState<ViewerOption[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([listDeptOptions(), listViewerOptions()])
      .then(([deptRes, userRes]) => {
        if (cancelled) return
        setDepts(deptRes.items ?? [])
        setUsers(userRes.items ?? [])
      })
      .catch(() => {
        if (cancelled) return
        setDepts([])
        setUsers([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const treeData = useMemo(() => buildViewerTree(depts, users), [depts, users])
  const selected = (value ?? []).map((id) => `user:${id}`)

  return (
    <TreeSelect
      data-testid="viewer-users"
      treeCheckable
      showCheckedStrategy={TreeSelect.SHOW_CHILD}
      treeData={treeData}
      treeDefaultExpandAll={false}
      treeNodeFilterProp="title"
      showSearch
      allowClear
      loading={loading}
      maxTagCount="responsive"
      style={style ?? { width: '100%' }}
      styles={{ popup: { root: { maxHeight: 360, overflow: 'auto' } } }}
      placeholder={loading ? '正在读取组织架构…' : '按部门展开后多选。不选则只按可见部门控制'}
      value={selected}
      onChange={(next) => {
        const raw = Array.isArray(next) ? next : []
        const ids = raw
          .map((item) => {
            const matched = /^user:(\d+)$/.exec(String(item))
            return matched ? Number(matched[1]) : 0
          })
          .filter((id) => id > 0)
        onChange?.([...new Set(ids)])
      }}
      notFoundContent={loading ? '正在加载' : '没有可选择的人'}
    />
  )
}
