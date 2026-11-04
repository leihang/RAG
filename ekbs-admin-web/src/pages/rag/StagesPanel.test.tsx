/**
 * 逐跳明细里的「上下文扩展」卡（第 5 跳）。
 *
 * 用户实测文档 205 的疑问：「35274 这个块没在精排候选里，为什么进了引用？」
 * 答案就在这里：35274 是精排块 35277（#2，0.899）**之后**扩展出的整篇父块。
 * 卡必须把 `上下文块 ← 精排来源` 摆出来，并解释「父块不在精排候选里属正常」。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import StagesPanel from './StagesPanel'
import { parseStages } from './playgroundData'

const EXPAND_ONLY = {
  expand: {
    enabled: true,
    deduped: 1,
    expanded: [
      {
        context_chunk_id: 35274,
        source_chunk_id: 35277,
        chunk_type: 'parent',
        heading_path: '能耗管理作业指导书 > 能耗管理员职责',
        rerank_score: 0.899,
        rerank_rank: 2,
      },
      {
        context_chunk_id: 35281,
        source_chunk_id: 35284,
        chunk_type: 'table',
        heading_path: '能耗管理作业指导书 > 表2 能耗指标',
        rerank_score: 0.812,
        rerank_rank: 4,
      },
    ],
  },
}

describe('StagesPanel 上下文扩展卡', () => {
  it('渲染 `上下文块 ← 精排来源`、名次、分数与去重数', () => {
    render(<StagesPanel stages={parseStages(EXPAND_ONLY)} />)

    expect(screen.getByText('第 5 跳 · 上下文扩展（父块/整表）')).toBeInTheDocument()
    // 35274（父块）← 35277（精排 #2，0.899）
    expect(screen.getByText('35274')).toBeInTheDocument()
    expect(screen.getByText('35277')).toBeInTheDocument()
    expect(screen.getByText('#2')).toBeInTheDocument()
    expect(screen.getByText('0.8990')).toBeInTheDocument()
    expect(screen.getByText('能耗管理作业指导书 > 能耗管理员职责')).toBeInTheDocument()
    expect(screen.getByText('去重 1 条')).toBeInTheDocument()
    // 表容器 35281 ← 35284（去重后只留分数最高的来源）
    expect(screen.getByText('35281')).toBeInTheDocument()
    expect(screen.getByText('35284')).toBeInTheDocument()
  })

  it('点上下文块 / 精排来源都能打开块预览', () => {
    const onOpenChunk = vi.fn()
    render(<StagesPanel stages={parseStages(EXPAND_ONLY)} onOpenChunk={onOpenChunk} />)

    fireEvent.click(screen.getByText('35274'))
    expect(onOpenChunk).toHaveBeenCalledWith(35274)
    fireEvent.click(screen.getByText('35277'))
    expect(onOpenChunk).toHaveBeenCalledWith(35277)
  })

  it('解释器提示：精排只给检索单元打分，父块/整表容器不在候选里属正常', async () => {
    render(<StagesPanel stages={parseStages(EXPAND_ONLY)} />)

    // Tooltip 的说明文字在 hover 后才进 DOM
    fireEvent.mouseEnter(screen.getByLabelText('question-circle'))
    expect(
      await screen.findByText(/精排只给检索单元打分；父块\/整表容器是精排后扩展的上下文/),
    ).toBeInTheDocument()
  })

  it('空扩展列表给空态（不是渲染一张空表）', () => {
    render(<StagesPanel stages={parseStages({ expand: { enabled: false, deduped: 0, expanded: [] } })} />)
    expect(screen.getByText('第 5 跳 · 上下文扩展（父块/整表）')).toBeInTheDocument()
    expect(screen.getByText(/本次没有发生上下文扩展/)).toBeInTheDocument()
    expect(screen.getByText('本次未启用')).toBeInTheDocument()
  })

  it('旧后端没有 stages.expand 时不渲染扩展卡（优雅降级，不显示假数据）', () => {
    render(<StagesPanel stages={parseStages({ params: { rerank_keep: 5, parent_expand: true } })} />)

    expect(screen.queryByText('第 5 跳 · 上下文扩展（父块/整表）')).toBeNull()
    expect(screen.queryByText(/上下文扩展（父块\/整表）/)).toBeNull()
  })
})
