import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import AnswerMarkdown from './AnswerMarkdown'

describe('AnswerMarkdown', () => {
  it('renders pipe tables as HTML table with headers', () => {
    const text = [
      '明细如下：',
      '| 项目 | 9月用量 | 同比 |',
      '| --- | --- | --- |',
      '| 活动用电 | 1343.00 | 229.17% |',
      '| 公共用电 | 56888.00 | — |',
    ].join('\n')
    const { container } = render(<AnswerMarkdown text={text} />)
    expect(screen.getByText('明细如下：')).toBeInTheDocument()
    expect(container.querySelector('table')).not.toBeNull()
    expect(screen.getByRole('columnheader', { name: '项目' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '9月用量' })).toBeInTheDocument()
    expect(screen.getByText('活动用电')).toBeInTheDocument()
    expect(screen.getByText('56888.00')).toBeInTheDocument()
  })

  it('keeps plain answers as pre-wrap text', () => {
    render(<AnswerMarkdown text="用量为 100。" />)
    expect(screen.getByText('用量为 100。')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })
})
