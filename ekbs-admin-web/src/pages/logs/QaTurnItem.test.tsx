/**
 * 会话详情里的引用溯源与全过程折叠展示。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import QaTurnItem from './QaTurnItem'
import type { QaTurn } from './qaLogHelpers'

const BASE_TURN: QaTurn = {
  messageId: 1,
  turn: 1,
  question: '能耗管理员在能耗管理中的主要作用',
  rewrittenQuery: '',
  answer: '能耗管理员负责能耗数据统计、分析与节能措施落实。',
  model: 'deepseek-chat',
  refused: false,
  traceId: 'trace-205',
  timing: {},
  citations: [],
  contextChunkIds: [],
}

function renderTurn(turn: QaTurn) {
  return render(
    <MemoryRouter>
      <QaTurnItem turn={turn} />
    </MemoryRouter>,
  )
}

describe('QaTurnItem 全过程折叠', () => {
  it('四段：问题 / 改写 / Rerank引用 / 回答', () => {
    renderTurn({
      ...BASE_TURN,
      rewrittenQuery: '能耗管理员职责',
    })
    expect(screen.getByText(/① 用户问题/)).toBeInTheDocument()
    expect(screen.getByText(/② 提示词改写/)).toBeInTheDocument()
    expect(screen.getByText(/③ Rerank \/ 引用块/)).toBeInTheDocument()
    expect(screen.getByText(/④ 输出回答/)).toBeInTheDocument()
    expect(screen.getAllByText('能耗管理员职责').length).toBeGreaterThan(0)
  })
})

describe('QaTurnItem 引用的精排来源', () => {
  it('有 sourceChunkId 时显示 `#上下文块 ← #精排块`，并附 tooltip 说明', async () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 2, chunkId: 35274, sourceChunkId: 35277, docId: 205 }],
    })

    const label = screen.getAllByText('#35274 ← #35277')[0]
    expect(label).toBeInTheDocument()

    fireEvent.mouseOver(screen.getByText(/分块 id：/))
    expect(
      await screen.findByText('上下文块（由精排块 #35277 扩展）'),
    ).toBeInTheDocument()
  })

  it('没有 sourceChunkId（旧数据 / 引用本身就是精排块）时保持原样', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
    })

    expect(screen.getByText('分块 id：')).toBeInTheDocument()
    expect(screen.getAllByText('#35280').length).toBeGreaterThan(0)
    expect(screen.queryByText(/#\d+ ← #\d+/)).toBeNull()
  })

  it('sourceChunkId 与 chunkId 相同（没发生扩展）时不显示来源', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, sourceChunkId: 35280, docId: 205 }],
    })

    expect(screen.queryByText(/#\d+ ← #\d+/)).toBeNull()
  })
})

describe('QaTurnItem 模型上下文（引用 ≠ 上下文）', () => {
  it('仅上下文块单独列出', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [
        { citeIndex: 1, chunkId: 35280, docId: 205 },
        { citeIndex: 2, chunkId: 35274, sourceChunkId: 35277, docId: 205 },
      ],
      contextChunkIds: [35280, 35274, 35281],
    })

    expect(screen.getByText(/仅进模型上下文、未写入引用（1）/)).toBeInTheDocument()
    expect(screen.getByText('#35281')).toBeInTheDocument()
  })

  it('重复的上下文 id 只计一次', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
      contextChunkIds: [35280, 35274, 35280],
    })

    expect(screen.getByText(/仅进模型上下文、未写入引用（1）/)).toBeInTheDocument()
    expect(screen.getByText('#35274')).toBeInTheDocument()
  })

  it('历史行没有 context_chunk_ids 时不渲染仅上下文区', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
      contextChunkIds: [],
    })

    expect(screen.queryByText(/仅进模型上下文/)).toBeNull()
  })
})
