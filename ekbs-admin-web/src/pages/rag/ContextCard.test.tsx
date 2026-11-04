/**
 * 「模型上下文」卡（引用 ≠ 上下文）。
 *
 * 实测文档 205 的那一轮：prompt 里其实有 3 块 —— [1] 35280（被引用）、
 * [2] 35274 ← 35277（父块，被引用）、[3] 35281 ← 35284（表格容器，**仅上下文**）。
 * 旧界面只展示引用，用户因此以为 35281「根本没推给模型」。这张卡必须把 3 块都摆出来，
 * 并明确标出哪块只是「仅上下文」。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ContextCard from './ContextCard'
import type { ContextChunk } from '@/api/rag'

const DOC_205_CONTEXT: ContextChunk[] = [
  {
    index: 1,
    chunk_id: 35280,
    chunk_type: 'child',
    doc_title: 'XJWY-GC-12能耗管理作业指导书 - 定稿',
    heading_path: '能耗管理作业指导书 > 能耗管理员职责',
    token_count: 260,
    rerank_score: 0.912,
    source_chunk_id: null,
    source_rerank_rank: 1,
    cited: true,
  },
  {
    index: 2,
    chunk_id: 35274,
    chunk_type: 'parent',
    doc_title: 'XJWY-GC-12能耗管理作业指导书 - 定稿',
    heading_path: '能耗管理作业指导书 > 能耗管理员职责',
    token_count: 900,
    rerank_score: 0.899,
    source_chunk_id: 35277,
    source_rerank_rank: 2,
    cited: true,
  },
  {
    index: 3,
    chunk_id: 35281,
    chunk_type: 'table',
    doc_title: 'XJWY-GC-12能耗管理作业指导书 - 定稿',
    heading_path: '能耗管理作业指导书 > 表2 能耗指标',
    token_count: 962,
    rerank_score: 0.812,
    source_chunk_id: 35284,
    source_rerank_rank: 4,
    cited: false,
  },
]

describe('ContextCard 模型上下文（引用 ≠ 上下文）', () => {
  it('doc 205 的 3 块全部渲染：35280/35274 标「引用」，35281 标「仅上下文」', () => {
    render(<ContextCard contextChunks={DOC_205_CONTEXT} contextTokens={2122} />)

    expect(screen.getByText('模型上下文（3 块 · 被引用 2）')).toBeInTheDocument()
    // 三块上下文 id 都在（35281 虽未被引用，也必须出现）
    expect(screen.getByText('35280')).toBeInTheDocument()
    expect(screen.getByText('35274')).toBeInTheDocument()
    expect(screen.getByText('35281')).toBeInTheDocument()

    // cited → 绿色「引用 [n]」（n 与答案里的 [n] 编号一致）
    expect(screen.getByText('引用 [1]')).toBeInTheDocument()
    expect(screen.getByText('引用 [2]')).toBeInTheDocument()
    // 未引用但同样进了 prompt
    expect(screen.getByText('仅上下文')).toBeInTheDocument()
    expect(screen.queryByText('引用 [3]')).toBeNull()

    // 类型标签：父块/表格容器/子块
    expect(screen.getByText('子块')).toBeInTheDocument()
    expect(screen.getByText('父容器')).toBeInTheDocument()
    expect(screen.getByText('表格容器')).toBeInTheDocument()
  })

  it('展示「上下文块 ← 精排来源」与各自精排名次', () => {
    render(<ContextCard contextChunks={DOC_205_CONTEXT} contextTokens={2122} />)

    // 35274 由精排块 35277（#2）扩展而来；35281 由 35284（#4）扩展而来
    expect(screen.getByText('← #35277')).toBeInTheDocument()
    expect(screen.getByText('← #35284')).toBeInTheDocument()
    expect(screen.getByText('精排#1')).toBeInTheDocument()
    expect(screen.getByText('精排#2')).toBeInTheDocument()
    expect(screen.getByText('精排#4')).toBeInTheDocument()
  })

  it('header 显示合计 token，并提供「引用≠上下文」解释', async () => {
    render(<ContextCard contextChunks={DOC_205_CONTEXT} contextTokens={2122} />)

    expect(screen.getByText('合计 2,122 tok')).toBeInTheDocument()

    fireEvent.mouseEnter(screen.getByLabelText('question-circle'))
    expect(
      await screen.findByText(/引用≠上下文：只有答案里标了 \[n\] 的块才进引用\/审计/),
    ).toBeInTheDocument()
  })

  it('后端没给 context_tokens 时用各块 token_count 求和兜底', () => {
    render(<ContextCard contextChunks={DOC_205_CONTEXT} />)
    expect(screen.getByText('合计 2,122 tok')).toBeInTheDocument()
  })

  it('点上下文块 / 精排来源都能打开块预览', () => {
    const onOpenChunk = vi.fn()
    render(<ContextCard contextChunks={DOC_205_CONTEXT} contextTokens={2122} onOpenChunk={onOpenChunk} />)

    fireEvent.click(screen.getByText('35280'))
    expect(onOpenChunk).toHaveBeenCalledWith(35280)
    fireEvent.click(screen.getByText('← #35277'))
    expect(onOpenChunk).toHaveBeenCalledWith(35277)
  })

  it('旧后端没有 context_chunks 时整张卡不渲染（优雅降级）', () => {
    const { container } = render(<ContextCard />)
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByText(/模型上下文/)).toBeNull()
  })

  it('后端明确返回空数组时给空态（而不是渲染一张空表）', () => {
    render(<ContextCard contextChunks={[]} contextTokens={0} />)

    expect(screen.getByText('模型上下文（0 块 · 被引用 0）')).toBeInTheDocument()
    expect(screen.getByText(/本次请求没有记录模型上下文/)).toBeInTheDocument()
    expect(screen.getByText('合计 0 tok')).toBeInTheDocument()
  })
})
