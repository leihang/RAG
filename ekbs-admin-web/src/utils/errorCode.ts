/**
 * 错误码 → 文案（方案 05 §5.8 对照表）。
 *
 * 实际后端多数时候不给 `code`（只给 HTTP 状态 + `detail`），所以这张表是**补充**：
 * 有 code 用表里的标准文案，没有就把 `detail` 原样展示 —— 不编造 code。
 */
export const ERROR_TEXT: Record<string, string> = {
  AUTH_TOKEN_EXPIRED: '登录已过期，请重新登录',
  AUTH_INVALID_CREDENTIALS: '账号或密码错误',
  PERM_DENIED: '无操作权限',
  PERM_CLEARANCE_INSUFFICIENT: '密级不足',
  PERM_ACL_DENIED: '该资源的 ACL 明确拒绝访问',
  PERM_POLICY_CHANGED: '权限策略已更新，请重新预览后保存',
  KB_ARCHIVED: '知识库已归档，无法写入',
  DOC_VERSION_CONFLICT: '该文档已被他人修改，请刷新后重试',
  FILE_TOO_LARGE: '文件超过大小限制',
  FILE_TYPE_UNSUPPORTED: '不支持的文件类型',
  FILE_DUPLICATE: '已存在相同内容的文件',
  UPLOAD_CHUNK_MISSING: '上传分片不完整，正在续传',
  PARSE_TIMEOUT: '解析超时',
  PDF_ENCRYPTED: '文件已加密，无法解析',
  SHARD_UNAVAILABLE: '可见分片加载失败，已降级为仅全文检索',
  MODEL_SERVICE_UNAVAILABLE: '模型服务不可用',
  MODEL_QUOTA_EXCEEDED: '已超出 Token 配额',
  RATE_LIMITED: '操作过于频繁，请稍后再试',
  INTERNAL_ERROR: '服务异常，请稍后重试或联系运维',
}

/** 状态码兜底文案（后端没给 code 时用）。 */
export const STATUS_TEXT: Record<number, string> = {
  400: '请求参数有误',
  401: '登录已过期，请重新登录',
  403: '无操作权限',
  404: '对象不存在或已被删除',
  409: '状态冲突，请刷新后重试',
  413: '文件超过大小限制',
  415: '不支持的文件类型',
  422: '提交内容不符合要求',
  429: '操作过于频繁，请稍后再试',
  500: '服务异常，请稍后重试',
  503: '依赖服务不可用，请稍后重试',
  504: '处理超时',
}

export function errorTextOf(code?: string, status?: number): string {
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code]
  if (status && STATUS_TEXT[status]) return STATUS_TEXT[status]
  return ''
}
