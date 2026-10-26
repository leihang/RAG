/** 通用状态标签（方案 05 §5.1 状态语义色表 + §4.5 解析任务状态机）。 */
import { Badge } from 'antd'

type StatusKey =
  | 'published' | 'parsing' | 'indexing' | 'chunked' | 'pending_index' | 'pending' | 'review' | 'failed' | 'offline'
  | 'queued' | 'cancelled' | 'success' | 'denied' | 'deleted'

const META: Record<StatusKey, { text: string; status: 'success' | 'processing' | 'warning' | 'error' | 'default' }> = {
  published: { text: '已发布', status: 'success' },
  parsing: { text: '解析中', status: 'processing' },
  indexing: { text: '索引中', status: 'processing' },
  pending_index: { text: '待向量入库', status: 'processing' },
  pending: { text: '待解析', status: 'processing' },
  review: { text: '待复核', status: 'warning' },
  chunked: { text: '已分块待入库', status: 'processing' },
  queued: { text: '排队中', status: 'processing' },
  failed: { text: '失败', status: 'error' },
  cancelled: { text: '已取消', status: 'default' },
  offline: { text: '已下线', status: 'default' },
  deleted: { text: '回收站', status: 'warning' },
  success: { text: '成功', status: 'success' },
  denied: { text: '拦截', status: 'error' },
}

export default function StatusTag({ status }: { status?: string | null }) {
  const key = String(status ?? '') as StatusKey
  const meta = META[key]
  if (!meta) return <Badge status="default" text={String(status ?? '-')} />
  return <Badge status={meta.status} text={meta.text} />
}
