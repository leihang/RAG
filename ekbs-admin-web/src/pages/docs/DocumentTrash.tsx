/**
 * 文档回收站：软删除后原件保留 30 天，可恢复或永久删除。
 * 后端：status=deleted + deleted_at；scheduler 日作业 hard 清过期项。
 */
import { useRef, useState } from 'react'
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components'
import { Button, Space, Typography, message } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import EmptyState from '@/components/EmptyState'
import ImpactConfirmModal from './ImpactConfirmModal'
import { normalizeBatchResult } from './docUtils'
import {
  batchDocuments,
  listDocuments,
  restoreDocument,
  type DocListQuery,
} from '@/api/doc'
import type { DocumentItem } from '@/api/types'
import { formatTime, formatTrashDaysLeft } from '@/utils/format'

const BATCH_LIMIT = 500

export default function DocumentTrash() {
  const actionRef = useRef<ActionType>()
  const navigate = useNavigate()
  const [selected, setSelected] = useState<DocumentItem[]>([])
  const [pendingHard, setPendingHard] = useState<{ ids: number[]; titles: string[] } | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = () => {
    setSelected([])
    actionRef.current?.reload()
  }

  const restoreOne = async (docId: number) => {
    try {
      await restoreDocument(docId)
      message.success('已恢复为下线，需重新解析后再发布')
      reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    }
  }

  const restoreMany = async () => {
    if (!selected.length) return
    setLoading(true)
    let ok = 0
    const failed: string[] = []
    try {
      for (const row of selected) {
        try {
          await restoreDocument(row.doc_id)
          ok += 1
        } catch (err) {
          failed.push(`#${row.doc_id} ${err instanceof Error ? err.message : String(err)}`)
        }
      }
      if (failed.length) {
        message.warning(`${ok} 项已恢复，${failed.length} 项失败：${failed.slice(0, 3).join('；')}`)
      } else {
        message.success(`${ok} 项已恢复为下线`)
      }
      reload()
    } finally {
      setLoading(false)
    }
  }

  const runHardDelete = async (payload: { reason?: string }) => {
    if (!pendingHard) return
    setLoading(true)
    try {
      const res = await batchDocuments(pendingHard.ids, 'delete', payload.reason, { hard: true })
      const outcome = normalizeBatchResult(res)
      if (outcome.failed.length) {
        message.warning(
          `${outcome.succeeded} 项已清除，${outcome.failed.length} 项失败：` +
            outcome.failed.slice(0, 3).map((f) => `#${f.doc_id} ${f.error}`).join('；'),
        )
      } else {
        message.success(`${outcome.succeeded} 项已永久删除（含磁盘原件）`)
      }
      setPendingHard(null)
      reload()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const columns: ProColumns<DocumentItem>[] = [
    { title: 'ID', dataIndex: 'doc_id', width: 64, search: false },
    {
      title: '文档名',
      dataIndex: 'title',
      ellipsis: true,
      render: (_, row) => <Link to={`/docs/${row.doc_id}`}>{row.title}</Link>,
    },
    {
      title: '密级',
      dataIndex: 'level',
      width: 100,
      search: false,
      render: (_, row) => <ClearanceTag level={row.level} />,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      search: false,
      render: (_, row) => <StatusTag status={row.status} />,
    },
    {
      title: '删除时间',
      dataIndex: 'deleted_at',
      width: 170,
      search: false,
      render: (_, r) => formatTime(r.deleted_at),
    },
    {
      title: '保留期',
      dataIndex: 'deleted_at',
      width: 110,
      search: false,
      render: (_, r) => formatTrashDaysLeft(r.deleted_at),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 200,
      fixed: 'right',
      render: (_, row) => [
        <Auth key="restore" code="doc:document:update">
          <a onClick={() => void restoreOne(row.doc_id)}>恢复</a>
        </Auth>,
        <Auth key="purge" code="doc:document:delete">
          <a
            style={{ color: '#ff4d4f' }}
            onClick={() => setPendingHard({ ids: [row.doc_id], titles: [row.title] })}
          >
            永久删除
          </a>
        </Auth>,
      ],
    },
  ]

  const over = selected.length > BATCH_LIMIT

  return (
    <>
      <ProTable<DocumentItem, DocListQuery>
        headerTitle="回收站（原件在 trash/，保留 30 天）"
        rowKey="doc_id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 900 }}
        options={{ density: true, reload: true }}
        search={false}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        toolBarRender={() => [
          <Button key="back" onClick={() => navigate('/docs/list')}>
            返回文档列表
          </Button>,
        ]}
        request={async (params) => {
          const res = await listDocuments({
            limit: params.pageSize,
            offset: ((params.current ?? 1) - 1) * (params.pageSize ?? 20),
            status: 'deleted',
            q: (params as Record<string, unknown>).title as string | undefined,
          })
          return { data: res.items, total: res.total, success: true }
        }}
        rowSelection={{
          selectedRowKeys: selected.map((d) => d.doc_id),
          onChange: (_k, rows) => setSelected(rows),
        }}
        tableAlertRender={({ selectedRowKeys }) => (
          <Space size={12}>
            <span>已选 {selectedRowKeys.length} 项</span>
            {over ? <span style={{ color: '#ff4d4f' }}>超过单次 {BATCH_LIMIT} 条上限</span> : null}
          </Space>
        )}
        tableAlertOptionRender={() => (
          <Space>
            <Auth code="doc:document:update">
              <Button size="small" disabled={over || !selected.length} loading={loading} onClick={() => void restoreMany()}>
                批量恢复
              </Button>
            </Auth>
            <Auth code="doc:document:delete">
              <Button
                size="small"
                danger
                disabled={over || !selected.length}
                onClick={() =>
                  setPendingHard({
                    ids: selected.map((d) => d.doc_id),
                    titles: selected.map((d) => d.title),
                  })
                }
              >
                批量永久删除
              </Button>
            </Auth>
          </Space>
        )}
        locale={{
          emptyText: (
            <EmptyState
              description="回收站是空的"
              actionText="返回文档列表"
              onAction={() => navigate('/docs/list')}
            />
          ),
        }}
      />
      <ImpactConfirmModal
        open={pendingHard !== null}
        level="C"
        title={
          pendingHard
            ? pendingHard.ids.length === 1
              ? `永久删除「${pendingHard.titles[0]}」`
              : `批量永久删除（${pendingHard.ids.length} 篇）`
            : ''
        }
        consequence={
          pendingHard ? (
            <Typography.Text>
              将立即删除文档记录、索引，以及磁盘回收目录（trash/）中的原件与解析产物，不可恢复。
            </Typography.Text>
          ) : null
        }
        impact={
          pendingHard ? (
            <div>
              <div>影响文档：{pendingHard.ids.length} 篇</div>
              <div style={{ maxHeight: 120, overflow: 'auto', marginTop: 4 }}>
                {pendingHard.titles.slice(0, 20).map((t, i) => (
                  <div key={pendingHard.ids[i]}>
                    #{pendingHard.ids[i]} {t}
                  </div>
                ))}
                {pendingHard.titles.length > 20 ? <div>…等共 {pendingHard.titles.length} 篇</div> : null}
              </div>
            </div>
          ) : null
        }
        confirmWord="DELETE"
        confirmNames={pendingHard?.ids.length === 1 ? pendingHard.titles : undefined}
        requireReason
        loading={loading}
        onCancel={() => setPendingHard(null)}
        onOk={(p) => void runHardDelete(p)}
      />
    </>
  )
}
