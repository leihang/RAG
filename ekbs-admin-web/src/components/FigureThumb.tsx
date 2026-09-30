/**
 * 引用里的原图缩略图（`06` §4.11：图片"可检索 + 可回源"）。
 *
 * 为什么不能直接写 `<img src="/api/v1/assets/figure/317">`：
 *
 * * 那个端点**要鉴权**（无 token → 401，实测），而 `<img>` **没法带 Authorization 头**；
 * * 它也不认 `?token=`（实测同样 401）—— 服务端没有"把凭据放进 URL"的入口。
 *
 * 所以这里用 `http`（axios 实例，拦截器会自动加 Authorization）以 **blob** 取回图片，
 * 再 `URL.createObjectURL` 交给 `<img>`。这样：
 * * 权限模型一点没松（仍是同一个端点、同一套 ACL 复检，无权限拿 404）；
 * * 图片一旦判权通过就可以点开看原图（`window.open(blobUrl)`）；
 * * 组件卸载时 `revokeObjectURL`，不会漏内存。
 *
 * 404 / 403 一律**静默**（返回空），不弹错：引用是历史数据，图可能已被删或权限已回收，
 * 那种情况下引用仍应正常显示，只是没有缩略图。
 */
import { useEffect, useState } from 'react'
import { Image, Tooltip, Typography } from 'antd'
import { http } from '@/api/client'

export default function FigureThumb({
  chunkId,
  size = 96,
  title,
}: {
  chunkId?: number | null
  size?: number
  title?: string
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    if (chunkId == null) return undefined
    setUrl(null)
    setFailed(null)
    http
      .get(`/assets/figure/${chunkId}`, { responseType: 'blob' })
      .then((res) => {
        if (revoked) return
        objectUrl = URL.createObjectURL(res as unknown as Blob)
        setUrl(objectUrl)
      })
      .catch((err: unknown) => {
        if (revoked) return
        // ⚠️ 不能"什么都不显示"：401（会话过期）/403（权限被回收）/404（图已删）
        // 三种原因长得一模一样，而用户什么提示都看不到 —— 那就变成"图就是不出来"。
        // 组件里给一个小字提示 + 控制台留痕，至少能自证不是界面坏了。
        const status = (err as { status?: number })?.status
        const reason = status === 401 ? '登录已过期' : status === 403 ? '无权限' : '图不可用'
        setFailed(reason)
        console.warn('[ekbs-admin] 图片取用失败', { chunkId, status, err })
      })
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [chunkId])

  if (chunkId == null) return null
  if (failed) {
    return (
      <Tooltip title={`${failed}（chunk ${chunkId}）`}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          图不可用
        </Typography.Text>
      </Tooltip>
    )
  }
  if (!url) {
    // 占位保持行高，避免列表在图片到达时跳动
    return <div style={{ width: size, height: size * 0.75, background: '#f5f5f5', borderRadius: 4 }} />
  }
  return (
    <Image
      src={url}
      alt={title ?? `图片块 ${chunkId}`}
      width={size}
      style={{ borderRadius: 4, objectFit: 'cover' }}
      preview={{ src: url, mask: '看原图' }}
    />
  )
}
