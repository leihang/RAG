/**
 * 两种"页面挂了"必须给出不同的出路。
 *
 * 背景（用户实测）：一次会话里浏览器先后加载过 `Playground.Bc9T60xU.js` 和
 * `Playground.Bt4Ceovy.js`，前者在后续构建里已被删除。已经打开的页面点懒加载路由时
 * 动态 import 失败（旧 chunk 已不存在/被强缓存），组件树抛错 → 边界显示
 * 「页面渲染异常 / 服务暂时不可用，请联系运维」—— 把"刷一下就好"说成了服务故障。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ErrorBoundary from './ErrorBoundary'

function Boom({ message }: { message: string }): React.ReactNode {
  throw new Error(message)
}

const reload = vi.fn()

// 上一次用例点过「立即刷新」会把 spy 记下来，这里每个用例前清一次
beforeEach(() => reload.mockClear())

function mountBoom(message: string) {
  // window.location.reload 在 jsdom 里不可写，这里整体替换 location 的 reload
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload },
  })
  return render(
    <ErrorBoundary>
      <Boom message={message} />
    </ErrorBoundary>,
  )
}

describe('错误边界', () => {
  it('chunk 加载失败 → 提示"页面已更新，请刷新"并可一键刷新', () => {
    mountBoom('Failed to fetch dynamically imported module: /admin/assets/Playground.Bc9T60xU.js')

    expect(screen.getByText('页面已更新，请刷新')).toBeInTheDocument()
    expect(screen.queryByText('页面渲染异常')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '立即刷新' }))
    expect(reload).toHaveBeenCalled()
  })

  it('其它渲染错误仍按服务异常处理，不当成"页面过期"', () => {
    mountBoom("Cannot read properties of undefined (reading 'reduce')")

    expect(screen.getByText('页面渲染异常')).toBeInTheDocument()
    expect(screen.queryByText('页面已更新，请刷新')).toBeNull()
    // 重试按钮只清错误态，不刷新页面
    // antd 给两个汉字的按钮插了空格（「重试」的可访问名是「重 试」）
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }))
    expect(reload).not.toHaveBeenCalled()
  })

  it('正常子树照常渲染', () => {
    render(
      <ErrorBoundary>
        <div>一切正常</div>
      </ErrorBoundary>,
    )
    expect(screen.getByText('一切正常')).toBeInTheDocument()
  })
})
