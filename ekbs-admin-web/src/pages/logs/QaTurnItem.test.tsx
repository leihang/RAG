/**
 * 会话详情里的引用溯源展示。
 *
 * 文档 205 实测：引用 [2] 的 `chunk_id=35274` 是**整篇父块上下文**（实际喂给 LLM 的块），
 * 它由精排块 `35277`（精排 #2）扩展而来。旧展示只有一个 chunk id，用户无法判断
 * 「这块到底在不在精排候选里」。有 `source_chunk_id` 时必须显示 `#上下文块 ← #精排块`；
 * 旧数据没有该列时保持原样（不编造来源）。
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

describe('QaTurnItem 引用的精排来源', () => {
  it('有 sourceChunkId 时显示 `#上下文块 ← #精排块`，并附 tooltip 说明', async () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 2, chunkId: 35274, sourceChunkId: 35277, docId: 205 }],
    })

    // 35274 = 整篇父块（喂给 LLM 的上下文），35277 = 精排 #2 的检索单元
    const label = screen.getByText('#35274 ← #35277')
    expect(label).toBeInTheDocument()

    fireEvent.mouseOver(label)
    expect(
      await screen.findByText('上下文块（由精排块 #35277 扩展）'),
    ).toBeInTheDocument()
  })

  it('没有 sourceChunkId（旧数据 / 引用本身就是精排块）时保持原样', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
    })

    expect(screen.getByText('分块 id：#35280')).toBeInTheDocument()
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

describe('QaTurnItem 模型上下文行（引用 ≠ 上下文）', () => {
  it('显示该轮全部上下文块：被引用的标 [引用]，未引用的标 [仅上下文]', () => {
    renderTurn({
      ...BASE_TURN,
      // 实测 doc 205：prompt 3 块，只有前 2 块被引用
      citations: [
        { citeIndex: 1, chunkId: 35280, docId: 205 },
        { citeIndex: 2, chunkId: 35274, sourceChunkId: 35277, docId: 205 },
      ],
      contextChunkIds: [35280, 35274, 35281],
    })

    expect(screen.getByText('模型上下文（3 块）：')).toBeInTheDocument()
    expect(screen.getByText('#35280[引用]')).toBeInTheDocument()
    expect(screen.getByText('#35274[引用]')).toBeInTheDocument()
    // 35281 没进引用，但同样推给了模型 —— 这一行就是它存在的证据
    expect(screen.getByText('#35281[仅上下文]')).toBeInTheDocument()
  })

  it('重复的上下文 id 只显示一次（顺序按后端存的顺序）', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
      contextChunkIds: [35280, 35274, 35280],
    })

    expect(screen.getByText('模型上下文（2 块）：')).toBeInTheDocument()
    expect(screen.getByText('#35280[引用]')).toBeInTheDocument()
    expect(screen.getByText('#35274[仅上下文]')).toBeInTheDocument()
  })

  it('历史行没有 context_chunk_ids 时整行不渲染（旧页面行为不变）', () => {
    renderTurn({
      ...BASE_TURN,
      citations: [{ citeIndex: 1, chunkId: 35280, docId: 205 }],
      contextChunkIds: [],
    })

    expect(screen.queryByText(/模型上下文/)).toBeNull()
    expect(screen.queryByText(/#\d+\[(引用|仅上下文)\]/)).toBeNull()
  })
})
