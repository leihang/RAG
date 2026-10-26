import { describe, expect, it } from 'vitest'
import { formatDurationMs } from './format'

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
