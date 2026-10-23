/**
 * 可见部门选择。选项来自已同步的组织架构，提交值是钉钉部门 ID。
 */
import { useEffect, useState, type CSSProperties } from 'react'
import { Select } from 'antd'
import { listDeptOptions, type DeptOption } from '@/api/import'

export default function DeptPicker({
  value,
  onChange,
  style,
}: {
  value?: string[]
  onChange?: (ids: string[]) => void
  style?: CSSProperties
}) {
  const [items, setItems] = useState<DeptOption[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    listDeptOptions()
      .then((res) => {
        if (!cancelled) setItems(res.items ?? [])
      })
      .catch(() => {
        if (!cancelled) setItems([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <Select
      data-testid="visible-dept"
      mode="multiple"
      allowClear
      showSearch
      loading={loading}
      style={style ?? { width: '100%' }}
      placeholder="从组织架构选择可见部门（可多选）"
      optionFilterProp="label"
      value={value}
      onChange={onChange}
      options={items.map((dept) => {
        const indent = '\u3000'.repeat(Math.max(0, dept.tree_depth || 0))
        const code = dept.dept_code ? ` · 分片 ${dept.dept_code}` : ''
        return {
          value: String(dept.dingtalk_dept_id),
          label: `${indent}${dept.name}（ID ${dept.dingtalk_dept_id}${code}）`,
        }
      })}
      notFoundContent={loading ? '正在读取部门…' : '没有部门。请到「组织架构」点「从钉钉更新」'}
    />
  )
}
