/** 部门映射（方案 05 §4.8）：展示「钉钉部门 → 分片部门码」映射。 */
import { useEffect, useState } from 'react'
import { Button, Card, Input, Space, Table, Typography, message } from 'antd'
import Auth from '@/components/Auth'
import EmptyState from '@/components/EmptyState'
import { listDepts, setDeptMapping } from '@/api/iam'

interface Row { dept_id: string; dept_code: string; name?: string }

export default function DeptMapping() {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(false)
  const [edit, setEdit] = useState<Record<string, string>>({})

  const load = async () => {
    setLoading(true)
    try {
      const data = await listDepts()
      const map = (data.dept_code_map ?? {}) as Record<string, string>
      const items = (data.items ?? []) as { dept_id?: string | number; name?: string }[]
      const merged: Row[] = items.length
        ? items.map((d) => {
            const id = String(d.dept_id ?? '')
            return { dept_id: id, dept_code: map[id] ?? '', name: d.name }
          })
        : Object.entries(map).map(([dept_id, dept_code]) => ({ dept_id, dept_code }))
      setRows(merged)
      setEdit({})
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }

  useEffect(() => { void load() }, [])

  const save = async (deptId: string) => {
    const code = (edit[deptId] ?? '').trim()
    try {
      await setDeptMapping(deptId, { dept_code: code })
      message.success('已保存')
      await load()
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)) }
  }

  return (
    <Card
      title="部门映射"
      extra={<Button onClick={() => void load()} loading={loading}>刷新</Button>}
    >
      <Typography.Paragraph type="secondary">
        分片命名是 <code>kb_&lt;部门码&gt;_L&lt;密级&gt;</code>，部门码来自这张映射表；改它要
        <code>perm:dept:map</code>。
        <br />
        只有「L1 且没填部门」才会落到 <code>all</code> 分片。填了部门的文档，如果部门值既不在映射表里、
        又不是 2–16 位小写字母数字下划线（如 <code>102</code>），后端会直接拒绝、文档不入库 —— 不猜。
      </Typography.Paragraph>
      <Table<Row>
        rowKey="dept_id"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <EmptyState description="没有部门数据，先做一次组织同步" /> }}
        columns={[
          { title: '钉钉部门 ID', dataIndex: 'dept_id', width: 160 },
          { title: '部门名', dataIndex: 'name', width: 200, render: (v) => v ?? '-' },
          { title: '分片部门码', dataIndex: 'dept_code', width: 220,
            render: (v: string, r) => (
              <Input
                value={edit[r.dept_id] ?? v}
                placeholder="如 hr / finance / all"
                onChange={(e) => setEdit((p) => ({ ...p, [r.dept_id]: e.target.value }))}
              />
            ) },
          { title: '操作', width: 120, render: (_, r) => (
            <Auth code="perm:dept:read">
              <Space>
                <a onClick={() => void save(r.dept_id)}>保存</a>
              </Space>
            </Auth>
          ) },
        ]}
      />
    </Card>
  )
}
