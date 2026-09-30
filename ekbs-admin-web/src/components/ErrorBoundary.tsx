/** 路由级错误边界（方案 05 §5.4「服务异常」文案模板）。 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button, Result, Typography } from 'antd'

interface Props { children: ReactNode }
interface State { error: Error | null }

/**
 * 懒加载路由取不到 chunk 时的报错（浏览器原文，各引擎措辞不同）。
 *
 * 为什么会发生：`vite build` 会**清空 outDir**，每次构建都换一套带内容哈希的 chunk 名、
 * 并删掉旧的那批。已经打开的页面若还拿着旧的 `index.html`（或旧 chunk 已被浏览器强缓存），
 * 点一个懒加载路由就会请求一个**已不存在**的 `Foo.<旧哈希>.js` → 动态 import 失败 →
 * 组件树抛错 → 落到这个错误边界。
 *
 * 实测（本仓库一次会话）：浏览器先后加载过 `Playground.Bc9T60xU.js` 与
 * `Playground.Bt4Ceovy.js`，前者在后续构建里已被删除 —— 用户看到的是「页面渲染异常」，
 * 真正原因只是**页面是旧的，刷一下就好**。
 */
const CHUNK_ERROR = /dynamically imported module|Importing a module script failed|Loading chunk \S+ failed|error loading dynamically imported module/i

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 埋点/日志：管理台不做外发，只在控制台留痕便于排障
    console.error('[ekbs-admin] 渲染异常', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    // 区分「部署后页面过期」与「真的崩了」：前者让用户刷新即可；做成 500 + "联系运维"
    // 只会把人引向错误方向（实测就是这么被误导的）。
    if (CHUNK_ERROR.test(error.message)) {
      return (
        <Result
          status="info"
          title="页面已更新，请刷新"
          subTitle="管理台刚发布过新版本，当前页面引用的脚本已经不存在。刷新一次即可继续。"
          extra={
            <Button type="primary" onClick={() => window.location.reload()}>
              立即刷新
            </Button>
          }
        >
          <Typography.Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
            {error.message}
          </Typography.Paragraph>
        </Result>
      )
    }

    return (
      <Result
        status="500"
        title="页面渲染异常"
        subTitle="服务暂时不可用，请稍后重试或联系运维"
        extra={
          <Button type="primary" onClick={() => this.setState({ error: null })}>
            重试
          </Button>
        }
      >
        <Typography.Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap' }}>
          {error.message}
        </Typography.Paragraph>
      </Result>
    )
  }
}
