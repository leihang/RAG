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
