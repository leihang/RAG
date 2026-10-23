/** 分块预览入口：先选文档，再进入 ChunkExplorer。 */
import { useRef } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Space, Typography } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import EmptyState from '@/components/EmptyState'
import { listDocuments, type DocListQuery } from '@/api/doc'
import type { DocumentItem } from '@/api/types'
import { formatTime } from '@/utils/format'

export default function ChunkDocList() {
  const actionRef = useRef<ActionType>()
  const navigate = useNavigate()

  const columns: ProColumns<DocumentItem>[] = [
    { title: 'ID', dataIndex: 'doc_id', width: 64, search: false },
    {
      title: '文档名',
      dataIndex: 'title',
      ellipsis: true,
      render: (_, row) => <Link to={`/chunks/${row.doc_id}`}>{row.title}</Link>,
    },
    {
      title: '密级',
      dataIndex: 'level',
      width: 100,
      valueType: 'select',
      valueEnum: {
        1: { text: 'L1' },
        2: { text: 'L2' },
        3: { text: 'L3' },
        4: { text: 'L4' },
      },
      render: (_, row) => <ClearanceTag level={row.level} />,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      valueType: 'select',
      valueEnum: {
        pending: { text: '待解析' },
        parsing: { text: '解析中' },
        review: { text: '待复核' },
        published: { text: '已发布' },
        failed: { text: '失败' },
        offline: { text: '已下线' },
      },
      render: (_, row) => <StatusTag status={row.status} />,
    },
    {
      title: '分块数',
      dataIndex: 'chunk_count',
      width: 90,
      search: false,
      render: (_, row) => row.chunk_count ?? 0,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      search: false,
      render: (_, r) => formatTime(r.updated_at),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 140,
      render: (_, row) => [
        <a key="open" onClick={() => navigate(`/chunks/${row.doc_id}`)}>
          打开分块
        </a>,
        <Link key="doc" to={`/docs/${row.doc_id}`}>
          详情
        </Link>,
      ],
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
        处理流程第 3 步：选择文档进入父子块树，可编辑 / 拆合 / 重嵌入。待复核文档尚未分块，请先在「清洗解析」完成双栏复核。
      </Typography.Paragraph>
      <ProTable<DocumentItem, DocListQuery>
        headerTitle="分块预览"
        rowKey="doc_id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 900 }}
        options={{ density: true, reload: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        toolBarRender={() => [
          <Button key="parse" onClick={() => navigate('/parse/tasks')}>
            去清洗解析
          </Button>,
        ]}
        request={async (params) => {
          const res = await listDocuments({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            status: params.status as string | undefined,
            level: params.level as number | undefined,
            q: (params as Record<string, unknown>).title as string | undefined,
          })
          return { data: res.items, total: res.total, success: true }
        }}
        locale={{
          emptyText: (
            <EmptyState
              description="还没有文档"
              actionText="去上传"
              onAction={() => navigate('/docs/upload')}
            />
          ),
        }}
      />
    </Space>
  )
}
