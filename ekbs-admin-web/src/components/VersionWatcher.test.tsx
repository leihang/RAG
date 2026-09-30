/**
 * 「页面已过期」的判定与提示（部署卫生，见组件注释）。
 *
 * 真实背景：一次会话里浏览器先后加载过 `Playground.Bc9T60xU.js` 与
 * `Playground.Bt4Ceovy.js`。重新构建会换掉哈希并删除旧 chunk，已打开的页面
 * 要么继续跑旧代码，要么在懒加载时崩掉 —— 两次都表现为"改了没生效/页面渲染异常"。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import VersionWatcher, { entryChunkOf, isStale } from './VersionWatcher'

describe('入口 chunk 解析', () => {
  it('从 index.html 里取出入口包名', () => {
    const html = '<script type="module" crossorigin src="/admin/assets/index.bwqCao12.js"></script>'
    expect(entryChunkOf(html)).toBe('assets/index.bwqCao12.js')
  })

  it('没有入口包（不是 /admin 部署）→ null，调用方据此跳过判断', () => {
    expect(entryChunkOf('<html><body>hello</body></html>')).toBeNull()
  })
})

describe('过期判定', () => {
  it('两边都有且不同才算过期', () => {
    expect(isStale('assets/index.aaa.js', 'assets/index.bbb.js')).toBe(true)
    expect(isStale('assets/index.aaa.js', 'assets/index.aaa.js')).toBe(false)
  })

  it('任一侧拿不到就不判断（绝不因为探测本身误报）', () => {
    expect(isStale(null, 'assets/index.bbb.js')).toBe(false)
    expect(isStale('assets/index.aaa.js', null)).toBe(false)
    expect(isStale(null, null)).toBe(false)
  })
})

describe('提示行为', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('服务器入口包与当前页面不一致时，给出可点的刷新提示', async () => {
    // 当前页面引用的入口包
    const script = document.createElement('script')
    script.type = 'module'
    script.src = '/admin/assets/index.OLDOLDOL.js'
    document.head.appendChild(script)

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        text: async () => '<script type="module" src="/admin/assets/index.NEWNEWNEW.js"></script>',
      })),
    )

    render(<VersionWatcher />)
    window.dispatchEvent(new Event('focus'))

    await waitFor(() => expect(screen.getByText('管理台已发布新版本')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /刷\s*新/ })).toBeInTheDocument()
    document.head.removeChild(script)
  })

  it('探测失败时静默（只是提醒，不能打断使用）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('offline')
      }),
    )
    render(<VersionWatcher />)
    window.dispatchEvent(new Event('focus'))
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByText('管理台已发布新版本')).toBeNull()
  })
})
