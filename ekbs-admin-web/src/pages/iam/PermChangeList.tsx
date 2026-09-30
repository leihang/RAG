/** 权限变更记录（方案 05 §4.8）——**纯只读**页面（§1.2：auditor 无任何写权限）。 */
import { ProTable, type ProColumns } from '@ant-design/pro-components'
import { Card, Typography } from 'antd'
import EmptyState from '@/components/EmptyState'
import { listPermChanges } from '@/api/iam'
import type { PermChangeItem } from '@/api/types'
import { formatTime } from '@/utils/format'

export default function PermChangeList() {
  const columns: ProColumns<PermChangeItem>[] = [
    { title: 'ID', dataIndex: 'change_id', width: 70, search: false },
    { title: '时间', dataIndex: 'created_at', width: 170, search: false, render: (_, r) => formatTime(r.created_at) },
    { title: '操作人', dataIndex: 'operator_id', width: 90, search: false },
    { title: '对象类型', dataIndex: 'target_type', width: 120, search: false },
    { title: '对象', dataIndex: 'target_id', width: 140, search: false },
    { title: '动作', dataIndex: 'action', width: 140, search: false },
    { title: '原因', dataIndex: 'reason', ellipsis: true, search: false },
    { title: '变更前', dataIndex: 'before', ellipsis: true, search: false },
    { title: '变更后', dataIndex: 'after', ellipsis: true, search: false },
  ]

  return (
    <Card>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
        本页只读：权限变更由权限变更服务写入，管理台不提供修改入口。
      </Typography.Paragraph>
      <ProTable<PermChangeItem>
        headerTitle="权限变更记录"
        rowKey="change_id"
        columns={columns}
        search={false}
        scroll={{ x: 1200 }}
        options={{ density: true, setting: true, reload: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        request={async (params) => {
          const res = await listPermChanges({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
          })
          return { data: res.items, total: res.total, success: true }
        }}
        locale={{ emptyText: <EmptyState description="还没有权限变更记录" /> }}
      />
    </Card>
  )
}
