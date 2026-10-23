/**
 * 钉钉免登工具的单测（`04` §3.4）。
 *
 * 只测**不依赖真实钉钉容器**的部分：环境判定与失败前置校验。
 * 真正的 JSAPI 取码必须在钉钉客户端里验（TC-DING-* 的 H5 用例）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isInDingTalk, requestAuthCode } from './dingtalk'

function stubUA(userAgent: string): void {
  vi.stubGlobal('navigator', { userAgent })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('isInDingTalk', () => {
  it('钉钉客户端 UA 判定为容器内（PC 与手机端都含 DingTalk）', () => {
    stubUA('Mozilla/5.0 (Macintosh) DingTalk(7.6.0) Chrome/108')
    expect(isInDingTalk()).toBe(true)
  })

  it('普通浏览器判定为容器外', () => {
    stubUA('Mozilla/5.0 (Macintosh) Chrome/120.0 Safari/537.36')
    expect(isInDingTalk()).toBe(false)
  })
})

describe('requestAuthCode', () => {
  it('corpId 为空时立刻报错，且指明要补哪个配置', async () => {
    await expect(requestAuthCode('')).rejects.toThrow(/DINGTALK_CORP_ID/)
  })

  it('不在钉钉容器内时给出可读原因（而不是静默挂起）', async () => {
    stubUA('Mozilla/5.0 Chrome/120.0')
    // 注入一个立即失败的 script 加载：jsdom 不执行外部脚本，onerror 由超时兜底太久，
    // 因此这里直接预置 script 标签让 loadScript 提前 resolve，再断言 dd 缺失的分支。
    const existing = document.createElement('script')
    existing.id = 'ekbs-dingtalk-jsapi'
    document.head.appendChild(existing)
    await expect(requestAuthCode('corpid-x')).rejects.toThrow(/钉钉 JSAPI/)
  })
})
