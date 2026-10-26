/** 数字/时间/字节格式化（方案 05 §5.2 排版规范：千分位、KB/MB/GB、Asia/Shanghai）。 */
import dayjs from 'dayjs'

export function formatBytes(bytes?: number | null): string {
  if (bytes == null || Number.isNaN(Number(bytes))) return '-'
  const n = Number(bytes)
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = n / 1024
  let idx = 0
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[idx]}`
}

export function formatNumber(value?: number | null): string {
  if (value == null || Number.isNaN(Number(value))) return '-'
  return Number(value).toLocaleString('zh-CN')
}

export function formatPercent(value?: number | null, digits = 1): string {
  if (value == null || Number.isNaN(Number(value))) return '-'
  return `${(Number(value) * 100).toFixed(digits)}%`
}

/** 解析耗时。无记录时用全角破折号，和「还没跑过」区分开。 */
export function formatDurationMs(ms?: number | null): string {
  if (ms == null || Number.isNaN(Number(ms)) || Number(ms) < 0) return '—'
  const value = Number(ms)
  // 解析一页 docx 常常只有几毫秒；统一按秒取整会显示成 "0 秒"，看起来像没耗时。
  if (value < 1000) return `${Math.round(value)} 毫秒`
  const sec = Math.max(1, Math.round(value / 1000))
  if (sec < 60) return `${sec} 秒`
  const minutes = Math.floor(sec / 60)
  const rest = sec % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
}

export function formatTime(value?: string | null): string {
  if (!value) return '-'
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm:ss') : String(value)
}

/** 回收站保留天数（与后端 ``TRASH_RETENTION_DAYS`` 对齐）。 */
export const TRASH_RETENTION_DAYS = 30

/** 软删后还剩几天会被物理清除；过期返回 0。 */
export function trashDaysLeft(deletedAt?: string | null, retentionDays = TRASH_RETENTION_DAYS): number | null {
  if (!deletedAt) return null
  const start = dayjs(deletedAt)
  if (!start.isValid()) return null
  const left = retentionDays - dayjs().diff(start, 'day')
  return left < 0 ? 0 : left
}

export function formatTrashDaysLeft(deletedAt?: string | null): string {
  const left = trashDaysLeft(deletedAt)
  if (left == null) return '-'
  if (left <= 0) return '即将清除'
  return `剩余 ${left} 天`
}

export function formatMs(value?: number | null): string {
  if (value == null) return '-'
  return Number(value) >= 1000 ? `${(Number(value) / 1000).toFixed(2)} s` : `${Math.round(Number(value))} ms`
}
