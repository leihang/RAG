import { describe, expect, it } from 'vitest'
import { formatDurationMs, parseServerTime } from './format'

describe('formatDurationMs', () => {
  it('亚秒级解析耗时显示毫秒，不再显示 0 秒', () => {
    expect(formatDurationMs(4)).toBe('4 毫秒')
    expect(formatDurationMs(999)).toBe('999 毫秒')
  })
  it('秒级以上按秒/分显示', () => {
    expect(formatDurationMs(1500)).toBe('2 秒')
    expect(formatDurationMs(65_000)).toBe('1 分 5 秒')
    expect(formatDurationMs(120_000)).toBe('2 分')
  })
  it('空值显示占位符', () => {
    expect(formatDurationMs(null)).toBe('—')
    expect(formatDurationMs(undefined)).toBe('—')
  })
})

describe('parseServerTime（后端时间是 UTC，必须换算成本地）', () => {
  const instant = Date.UTC(2026, 10, 3, 17, 15, 16) // 2026-11-03T17:15:16Z

  it('无时区串按 UTC 解析（SQLite datetime(now) 的口径）', () => {
    expect(parseServerTime('2026-11-03 17:15:16').valueOf()).toBe(instant)
  })
  it('已带时区的串按各自偏移解析', () => {
    expect(parseServerTime('2026-11-03T17:15:16Z').valueOf()).toBe(instant)
    // 同一时刻的 +08:00 写法
    expect(parseServerTime('2026-11-04T01:15:16+08:00').valueOf()).toBe(instant)
  })
  it('无效值仍能返回可判断的 dayjs 对象', () => {
    expect(parseServerTime('not-a-time').isValid()).toBe(false)
  })
})
