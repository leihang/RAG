/** 文档详情（方案 05 §4.4 详情线框图：元数据 + Tabs）。
 * 待复核扫描件：跳转双栏复核页（原件 ↔ 清洗稿）。 */
import { useEffect, useState } from 'react'
import { ProDescriptions } from '@ant-design/pro-components'
import { Button, Card, Space, Spin, Tabs, Tag, Typography, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import Auth from '@/components/Auth'
import ClearanceTag from '@/components/ClearanceTag'
import StatusTag from '@/components/StatusTag'
import {
  getDocumentDetail,
  offlineDocument,
  publishDocument,
  updateDocument,
  type DocumentDetail as DocDetail,
} from '@/api/doc'
import { verifyDocRecommend } from '@/api/parse'
import { formatBytes, formatTime } from '@/utils/format'
import { personalOwnerText } from '@/utils/personalKb'

export default function DocumentDetail() {
  const { docId } = useParams<{ docId: string }>()
  const id = Number(docId)
  const navigate = useNavigate()
  const [detail, setDetail] = useState<DocDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [verifyMsg, setVerifyMsg] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      setDetail(await getDocumentDetail(id))
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (Number.isFinite(id)) void load()
  }, [id])

  const doc = detail?.document ?? null
  const isReview = doc?.status === 'review'

  const act = async (kind: 'publish' | 'offline') => {
    setActing(true)
    try {
      if (kind === 'publish') await publishDocument(id)
      else await offlineDocument(id)
      message.success(kind === 'publish' ? '已发布' : '已下线')
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setActing(false)
    }
  }

  const editMeta = async () => {
    const title = window.prompt('文档标题', doc?.title ?? '')
    if (!title || title === doc?.title) return
    try {
      await updateDocument(id, { title })
      message.success('已保存')
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    }
  }

  const verifyRecommend = async () => {
    setVerifying(true)
    setVerifyMsg(null)
    try {
      const res = await verifyDocRecommend(id)
      if (res.verified) {
        message.success(`代表问句验证通过（rerank ${(res.rerank_score ?? 0).toFixed(2)}）`)
        setVerifyMsg(`已命中：${res.question}`)
      } else {
        message.warning('代表问句未通过命中验证（rerank 低于阈值），将不进入推荐')
        setVerifyMsg(`未通过：${res.question}`)
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setVerifying(false)
    }
  }

  const chunkTotal = detail?.chunks
    ? Object.values(detail.chunks).reduce((a, b) => a + Number(b ?? 0), 0)
    : 0

  if (loading) return <Card><Spin /></Card>
  if (!doc) return <Card>文档不存在或已被删除</Card>

  const tabItems = [
    {
      key: 'overview',
      label: '概览',
      children: (
        <ProDescriptions column={2} dataSource={doc} bordered size="small">
          <ProDescriptions.Item label="文档 ID" dataIndex="doc_id" />
          <ProDescriptions.Item label="知识库">
            {personalOwnerText(doc) || doc.kb_name || doc.kb_code || '—'}
          </ProDescriptions.Item>
          <ProDescriptions.Item label="密级" render={() => <ClearanceTag level={doc.level} />} />
          <ProDescriptions.Item label="状态" render={() => <StatusTag status={doc.status} />} />
          <ProDescriptions.Item label="页数" dataIndex="page_count" />
          <ProDescriptions.Item label="大小" render={() => formatBytes(doc.size_bytes)} />
          <ProDescriptions.Item label="版本" dataIndex="doc_version" />
          <ProDescriptions.Item label="分片" dataIndex="shard_id" />
          <ProDescriptions.Item label="生效日期" dataIndex="effective_date" />
          <ProDescriptions.Item label="失效日期" dataIndex="expire_date" />
          <ProDescriptions.Item label="可见部门" dataIndex="visible_dept_ids" />
          <ProDescriptions.Item label="更新时间" render={() => formatTime(doc.updated_at)} />
          <ProDescriptions.Item label="源文件路径" dataIndex="source_path" span={2} />
        </ProDescriptions>
      ),
    },
    {
      key: 'chunks',
      label: `分块（${chunkTotal}）`,
      children: (
        <Space direction="vertical">
          {isReview ? (
            <Typography.Text type="secondary">
              待复核文档尚未分块。请先打开双栏复核确认通过。
            </Typography.Text>
          ) : (
            <>
              <Typography.Text type="secondary">
                共 {chunkTotal} 个块（父 {detail?.chunks?.parent ?? 0} / 子{' '}
                {detail?.chunks?.child ?? 0}）。查看与编辑请进入分块预览页。
              </Typography.Text>
              <Button type="primary" onClick={() => navigate(`/chunks/${doc.doc_id}`)}>
                打开分块预览
              </Button>
            </>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={12} wrap>
          <Typography.Title level={5} style={{ margin: 0 }}>
            {doc.title}
          </Typography.Title>
          <ClearanceTag level={doc.level} />
          <StatusTag status={doc.status} />
          {doc.doc_version ? <Tag>{doc.doc_version}</Tag> : null}
          <span />
          <Button size="small" onClick={() => navigate('/docs/list')}>
            返回列表
          </Button>
          {isReview || doc.status === 'published' ? (
            <Button size="small" type="primary" onClick={() => navigate(`/parse/review/${id}`)}>
              {isReview ? '查看清洗稿（双栏对照）' : '原件对照'}
            </Button>
          ) : null}
          {doc.status === 'published' ? (
            <Auth code="parse:task:manage">
              <Button size="small" loading={verifying} onClick={() => void verifyRecommend()}>
                验证代表问句
              </Button>
            </Auth>
          ) : null}
          <Auth code="doc:document:update">
            <Button size="small" loading={acting} onClick={() => void editMeta()}>
              编辑元数据
            </Button>
          </Auth>
          <Auth code="doc:document:publish">
            <Button
              size="small"
              type="primary"
              loading={acting}
              disabled={doc.status === 'published' || isReview}
              onClick={() => void act('publish')}
            >
              发布
            </Button>
          </Auth>
          <Auth code="doc:document:offline">
            <Button
              size="small"
              danger
              loading={acting}
              disabled={doc.status === 'offline'}
              onClick={() => void act('offline')}
            >
              下线
            </Button>
          </Auth>
        </Space>
        {isReview ? (
          <Typography.Paragraph type="warning" style={{ marginTop: 12, marginBottom: 0 }}>
            当前为待复核：请打开「查看清洗稿（双栏对照）」，对照原件修订后再确认通过。
          </Typography.Paragraph>
        ) : null}
        {verifyMsg ? (
          <Typography.Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
            代表问句：{verifyMsg}
          </Typography.Paragraph>
        ) : null}
      </Card>

      <Card>
        <Tabs defaultActiveKey="overview" items={tabItems} />
      </Card>
    </Space>
  )
}
