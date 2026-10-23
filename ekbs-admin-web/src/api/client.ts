/**
 * 请求层（方案 05 §6.3 的实现，但**按实际后端的报文口径适配**）。
 *
 * 与方案 §6.3 示例的三处差异（都是实测出来的，不是偷懒）：
 *
 * 1. **后端没有 `{code, message, data, trace_id}` 包体** —— `ekbs-api` 直接返回业务 JSON，
 *    错误是 FastAPI 的 `{"detail": "..."}`。方案里的统一包体是设计稿口径，
 *    这里两种都认（有 `code` 字段就解包，没有就当裸 JSON）。
 * 2. **没有 refresh cookie**，所以不做 401 单飞刷新，而是清空会话并跳登录页。
 * 3. **不发任何权限指纹请求头**（方案 §6.3 的硬要求，这条完全遵守）——
 *    指纹由服务端算，前端 store 里的那份只用于展示与排错。
 */
import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'

declare module 'axios' {
  interface AxiosRequestConfig {
    /** 调用方自己展示错误时关掉全局 toast。例如新库还没有目录，404 是空列表而不是失败。 */
    skipErrorToast?: boolean
  }
}
import { message } from 'antd'
import { useAuthStore } from '@/stores/authStore'
import { errorTextOf } from '@/utils/errorCode'

const BASE_URL = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

export interface ApiErrorBody {
  detail?: unknown
  code?: string | number
  message?: string
  trace_id?: string
}

/** 归一化后的错误：页面只需要 `message` 与 `status`，排障看 `traceId`/`code`。 */
export class ApiError extends Error {
  status?: number
  code?: string
  traceId?: string
  constructor(text: string, opts: { status?: number; code?: string; traceId?: string } = {}) {
    super(text)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code
    this.traceId = opts.traceId
  }
}

/** FastAPI 的 `detail` 可能是字符串、对象或校验错误数组，统一压成一句人话。 */
export function detailToText(detail: unknown): string {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          const loc = Array.isArray((item as { loc?: unknown[] }).loc)
            ? (item as { loc: unknown[] }).loc.filter((x) => x !== 'body').join('.')
            : ''
          return `${loc ? loc + '：' : ''}${String((item as { msg: unknown }).msg)}`
        }
        return JSON.stringify(item)
      })
      .join('；')
  }
  if (typeof detail === 'object' && 'message' in (detail as object)) {
    return String((detail as { message: unknown }).message)
  }
  return JSON.stringify(detail)
}

export const http: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 60_000,
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const { accessToken } = useAuthStore.getState()
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`
  // **刻意不发 X-Perm-Fingerprint / X-Clearance**：让调用方声明自己的权限等于自报家门。
  try {
    config.headers['X-Request-Id'] = crypto.randomUUID()
  } catch {
    /* 老浏览器没有 crypto.randomUUID，不影响主流程 */
  }
  return config
})

http.interceptors.response.use(
  (res) => {
    const body = res.data
    // 兼容设计稿的统一包体；实际后端走下面那条 return
    if (body && typeof body === 'object' && 'code' in body && 'data' in body) {
      const code = (body as { code: string | number }).code
      if (code === 0 || code === 'OK') return (body as { data: unknown }).data
      const text = (body as { message?: string }).message ?? '请求失败'
      message.error(text)
      throw new ApiError(text, { code: String(code) })
    }
    return body
  },
  (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status
    const body = error.response?.data
    const code = body?.code != null ? String(body.code) : undefined
    // **顺序很重要**：后端给了 `detail` 就用它，状态码兜底文案只能垫底。
    // 反过来的话，一个 422 会被压成「提交内容不符合要求」，把
    // 「上传初始化时需给出 kb_id 或 kb_code」这种真正能指导操作的原因吃掉 ——
    // 实测就是这么被误导的（方案 §5.8 的口径也是「优先用后端给的信息」）。
    const text =
      detailToText(body?.detail) ||
      errorTextOf(code, status) ||
      body?.message ||
      (status === 401
        ? '登录已过期，请重新登录'
        : status === 403
          ? '无操作权限'
          : error.message || '服务暂时不可用，请稍后重试')

    if (status === 401) {
      // 无 refresh cookie：直接清会话，由路由守卫跳 /login
      useAuthStore.getState().clear()
    }
    // 403 不做全局 toast：页面内已有 /403 与按钮级提示，避免重复打扰
    // 423 同理：只可能来自本机登录的「账号已锁定」，登录表单内联展示原因
    if (!error.config?.skipErrorToast && status !== 403 && status !== 401 && status !== 423) {
      message.error(text)
    }

    return Promise.reject(new ApiError(text, { status, code, traceId: body?.trace_id }))
  },
)

export default http
