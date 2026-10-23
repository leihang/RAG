/**
 * 钉钉 H5 免登：JSAPI 动态加载 + `authCode` 获取（`04` §3.4）。
 *
 * 三条设计取舍：
 *
 * 1. **不引 npm 依赖**：钉钉 JSAPI 只在钉钉容器内有意义，打进包里等于给所有
 *    部署平白增加体积；这里按需从 CDN 注入 `<script>`（同官方文档做法）。
 * 2. **加载失败要能说清原因**：内网可能访问不到 `g.alicdn.com`，报错必须指明
 *    "是 CDN 不通"还是"不在钉钉里"，否则用户只看到一个白屏。
 * 3. **全部带超时**：钉钉容器异常时 `dd.ready` 可能永不回调，无限等待 = 页面卡死。
 */

//: 官方 JSAPI（H5 微应用）。版本号与 `04` §3.4 的接入说明保持一致。
const JSAPI_SRC = 'https://g.alicdn.com/dingding/dingtalk-jsapi/3.0.25/dingtalk.open.js'
const SCRIPT_ID = 'ekbs-dingtalk-jsapi'
const LOAD_TIMEOUT_MS = 8000
const READY_TIMEOUT_MS = 8000
const CODE_TIMEOUT_MS = 15000

/** 是否运行在钉钉容器内（PC 客户端与手机端 UA 都含 DingTalk）。 */
export function isInDingTalk(): boolean {
  return /DingTalk/i.test(navigator.userAgent)
}

interface DingTalkGlobal {
  ready: (cb: () => void) => void
  error?: (cb: (err: { errorMessage?: string; errorCode?: number }) => void) => void
  runtime?: {
    permission?: {
      requestAuthCode?: (opts: {
        corpId: string
        onSuccess: (res: { code: string }) => void
        onFail: (err: { errorMessage?: string; errorCode?: number }) => void
      }) => void
    }
  }
}

function loadScript(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.getElementById(SCRIPT_ID)) {
      resolve()
      return
    }
    const el = document.createElement('script')
    el.id = SCRIPT_ID
    el.src = JSAPI_SRC
    el.async = true
    const timer = window.setTimeout(() => {
      reject(new Error('钉钉 JSAPI 加载超时：内网可能访问不到 g.alicdn.com（需放行该域名）'))
    }, LOAD_TIMEOUT_MS)
    el.onload = () => {
      window.clearTimeout(timer)
      resolve()
    }
    el.onerror = () => {
      window.clearTimeout(timer)
      reject(new Error('钉钉 JSAPI 加载失败：请确认能访问 g.alicdn.com'))
    }
    document.head.appendChild(el)
  })
}

function dingTalk(): DingTalkGlobal {
  const dd = (window as unknown as { dd?: DingTalkGlobal }).dd
  if (!dd) {
    throw new Error('未检测到钉钉 JSAPI：请在企业钉钉客户端中打开本页面')
  }
  return dd
}

function waitReady(dd: DingTalkGlobal): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false
    const timer = window.setTimeout(() => {
      if (!settled) {
        settled = true
        reject(new Error('钉钉 JSAPI 就绪超时（请确认已配置企业应用与可信域名）'))
      }
    }, READY_TIMEOUT_MS)
    if (typeof dd.error === 'function') {
      dd.error((err) => {
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        reject(new Error(`钉钉 JSAPI 错误：${err?.errorMessage ?? err?.errorCode ?? '未知'}`))
      })
    }
    dd.ready(() => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      resolve()
    })
  })
}

/**
 * 取一次性 `authCode`（后端拿它换身份，5 分钟有效、一次一换）。
 * @throws 不在钉钉内 / CDN 不通 / 未配置 corpId / 用户取消授权
 */
export async function requestAuthCode(corpId: string): Promise<string> {
  if (!corpId) {
    throw new Error('未配置 DINGTALK_CORP_ID（见 ~/.ekbs/secrets.env）')
  }
  await loadScript()
  const dd = dingTalk()
  await waitReady(dd)
  const request = dd.runtime?.permission?.requestAuthCode
  if (typeof request !== 'function') {
    throw new Error('当前钉钉 JSAPI 版本不支持 requestAuthCode')
  }
  return new Promise<string>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error('获取 authCode 超时：请确认应用可见范围包含当前用户'))
    }, CODE_TIMEOUT_MS)
    request({
      corpId,
      onSuccess: (res) => {
        window.clearTimeout(timer)
        if (res?.code) resolve(res.code)
        else reject(new Error('钉钉未返回 authCode'))
      },
      onFail: (err) => {
        window.clearTimeout(timer)
        reject(new Error(`获取 authCode 失败：${err?.errorMessage ?? err?.errorCode ?? '未知'}`))
      },
    })
  })
}
