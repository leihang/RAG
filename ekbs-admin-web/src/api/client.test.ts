import { describe, expect, it } from 'vitest'
import { detailToText } from './client'

describe('后端错误文案归一化', () => {
  it('字符串 detail 原样返回', () => {
    expect(detailToText('上传初始化时需给出 kb_id 或 kb_code')).toBe(
      '上传初始化时需给出 kb_id 或 kb_code',
    )
  })

  it('FastAPI 校验错误数组压成一句可读的话', () => {
    const detail = [
      { loc: ['body', 'level'], msg: 'Input should be less than or equal to 4', type: 'less_than_equal' },
    ]
    expect(detailToText(detail)).toBe('level：Input should be less than or equal to 4')
  })

  it('多个校验错误用分号连接', () => {
    const detail = [
      { loc: ['body', 'a'], msg: 'x' },
      { loc: ['body', 'b'], msg: 'y' },
    ]
    expect(detailToText(detail)).toBe('a：x；b：y')
  })

  it('对象 detail 取 message', () => {
    expect(detailToText({ message: '冲突' })).toBe('冲突')
  })

  it('空 detail 返回空串（交给上层兜底）', () => {
    expect(detailToText(null)).toBe('')
    expect(detailToText(undefined)).toBe('')
  })
})
