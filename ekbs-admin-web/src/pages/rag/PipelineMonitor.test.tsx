import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import PipelineMonitor from './PipelineMonitor'
import type { PipelineMonitorPayload } from './playgroundData'

const SAMPLE: PipelineMonitorPayload = {
  question: '年假有多少天',
  rewrite: {
    original: '年假有多少天',
    rewritten: '员工年假天数规定',
    history_used: false,
    variants: ['员工年假天数规定', '带薪年休假天数'],
  },
  rerank_chunks: [
    {
      rank: 1,
      chunk_id: 101,
      rerank_score: 0.88,
      doc_title: '休假制度',
      heading_path: '第三章 / 年假',
      chunk_type: 'child',
      preview: '员工连续工作满一年享有年假……',
      decision: 'kept',
    },
    {
      rank: 2,
      chunk_id: 102,
      rerank_score: 0.4,
      doc_title: '其它',
      heading_path: '',
      chunk_type: 'child',
      preview: '无关段落',
      decision: 'below_threshold',
    },
  ],
  rerank_meta: { enabled: true, keep: 5, min_score: 0.6, count: 2 },
  answer: '根据制度，年假为 5 天。[1]',
  refused: false,
  model: 'mock-llm',
  intent: 'factual',
  mode_used: 'prose',
  timings_ms: { wall: 2100 },
  prompt_tokens: 100,
  completion_tokens: 50,
}

describe('PipelineMonitor', () => {
  it('无数据时不渲染', () => {
    const { container } = render(<PipelineMonitor monitor={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('四段折叠：问题 / 改写 / rerank / 回答', () => {
    render(<PipelineMonitor monitor={SAMPLE} />)
    expect(screen.getByText('全过程监控')).toBeInTheDocument()
    expect(screen.getByText(/① 用户问题/)).toBeInTheDocument()
    expect(screen.getByText(/② 提示词改写/)).toBeInTheDocument()
    expect(screen.getByText(/③ Rerank 块/)).toBeInTheDocument()
    expect(screen.getByText(/④ 输出回答/)).toBeInTheDocument()
    expect(screen.getAllByText('员工年假天数规定').length).toBeGreaterThan(0)
    expect(screen.getByText(/chunk 101/)).toBeInTheDocument()
    expect(screen.getByText(/根据制度，年假为 5 天/)).toBeInTheDocument()
  })

  it('拒答打标', () => {
    render(
      <PipelineMonitor
        monitor={{ ...SAMPLE, refused: true, refusal_reason: 'below_threshold', answer: '未找到可靠依据。' }}
      />,
    )
    expect(screen.getByText('已拒答')).toBeInTheDocument()
    expect(screen.getByText(/拒答原因：below_threshold/)).toBeInTheDocument()
  })
})
