import { describe, expect, it } from 'vitest'
import { formatExtList, normalizeExt } from './fileExt'

describe('扩展名归一化（与后端同口径）', () => {
  it('常规文件名', () => {
    expect(normalizeExt('报表.xlsx')).toBe('xlsx')
    expect(normalizeExt('汇总表.2026.xlsx')).toBe('xlsx')
    expect(normalizeExt('a.b.c.PDF')).toBe('pdf')
  })

  it('尾部空格 —— Windows/邮件下载最常见', () => {
    expect(normalizeExt('报表.xlsx ')).toBe('xlsx')
    expect(normalizeExt('  报表.xlsx   ')).toBe('xlsx')
  })

  it('全角句点 —— 中文输入法', () => {
    expect(normalizeExt('报表．xlsx')).toBe('xlsx')
    expect(normalizeExt('报表．XLSX ')).toBe('xlsx')
  })

  it('尾部多余点', () => {
    expect(normalizeExt('报表.xlsx.')).toBe('xlsx')
    expect(normalizeExt('报表.xlsx . ')).toBe('xlsx')
  })

  it('大写与全角空格', () => {
    expect(normalizeExt('REPORT.XLSX')).toBe('xlsx')
    expect(normalizeExt('报表\u3000.xlsx')).toBe('xlsx')
  })

  it('没有扩展名 / 隐藏文件返回空串', () => {
    expect(normalizeExt('无扩展名')).toBe('')
    expect(normalizeExt('.gitignore')).toBe('')
    expect(normalizeExt('')).toBe('')
  })

  it('支持列表渲染', () => {
    expect(formatExtList(['pdf', 'xlsx'])).toBe('.pdf / .xlsx')
  })
})
