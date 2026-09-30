/**
 * 「页面已过期」提示（部署卫生）。
 *
 * 为什么需要：`vite build` 每次都会换一套带哈希的 chunk 名并删掉旧的。已经打开的页面
 * 不会自己知道 —— 它会继续跑旧代码；点懒加载路由时则直接崩在
 * 「Failed to fetch dynamically imported module」上（`ErrorBoundary` 已能识别并提示刷新）。
 *
 * 这里补上另一半：**停留在当前页**时也把"发过新版本"说出来。
 * 只在窗口重新获得焦点时比一次（切回标签页才提示，不轮询、不打扰）；
 * 比对失败（离线、非 /admin 部署）一律静默，绝不因为探测本身打断使用。
 */
import { useEffect, useState } from 'react'
import { Alert, Button } from 'antd'

/** 从 index.html 里取入口 chunk 的引用（`/admin/assets/index.<hash>.js`）。 */
export function entryChunkOf(html: string): string | null {
  const match = html.match(/assets\/index\.[A-Za-z0-9_-]+\.js/)
  return match ? match[0] : null
}

/** 当前页面加载的入口 chunk（取不到就返回 null，调用方据此跳过判断）。 */
export function currentEntryChunk(): string | null {
  const script = document.querySelector('script[type="module"][src*="assets/index."]')
  return script?.getAttribute('src')?.match(/assets\/index\.[A-Za-z0-9_-]+\.js/)?.[0] ?? null
}

/** 服务器上的入口 chunk 与当前页面不同 ⇒ 页面已过期。任一为空则不判断（不猜）。 */
export function isStale(current: string | null, server: string | null): boolean {
  if (!current || !server) return false
  return current !== server
}

export default function VersionWatcher() {
  const [stale, setStale] = useState(false)

  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch('/admin/', { cache: 'no-store' })
        if (!res.ok) return
        if (isStale(currentEntryChunk(), entryChunkOf(await res.text()))) setStale(true)
      } catch {
        /* 探测失败不打扰用户：它只是提醒，不是功能 */
      }
    }
    const onFocus = () => void check()
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [])

  if (!stale) return null
  return (
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 12 }}
      message="管理台已发布新版本"
      description="当前页面还是旧版本，新功能与修复不会生效。刷新后即可使用最新版本。"
      action={
        <Button size="small" type="primary" onClick={() => window.location.reload()}>
          刷新
        </Button>
      }
    />
  )
}
