/**
 * 破坏性确认框（方案 05 §5.5 L-C）。
 *
 * 回归的背景是**文案与判定自相矛盾**：组件标签写「请输入文档名 `DELETE` 以确认」，
 * 而判定只认字面量 `DELETE` —— 用户照文案输入文档名，得到「与文档名不完全一致」，
 * 确认键永远灰着。方案 §5.5 的原文是「输入对象名**或**指定词」，两种都该认。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ImpactConfirmModal from './ImpactConfirmModal'

const DOC_NAME = '2026年8月工作汇报-工程'
const REASON = '制度文件已废止，由新版替代'

function setup(props: { confirmWord?: string; confirmNames?: string[] } = {}) {
  const onOk = vi.fn()
  render(
    <ImpactConfirmModal
      open
      level="C"
      title={`删除文档「${DOC_NAME}」`}
      consequence="删除后这些文档会立即停止被检索到，需重新导入才能恢复。"
      confirmWord={props.confirmWord ?? 'DELETE'}
      confirmNames={props.confirmNames ?? [DOC_NAME]}
      requireReason
      onCancel={() => undefined}
      onOk={onOk}
    />,
  )
  return { onOk }
}

const okButton = () => screen.getByRole('button', { name: /确认执行/ })
const wordInput = () => screen.getByPlaceholderText('DELETE')
const reasonInput = () => screen.getByPlaceholderText(/例如/)

function fill(word: string) {
  fireEvent.change(wordInput(), { target: { value: word } })
  fireEvent.change(reasonInput(), { target: { value: REASON } })
  fireEvent.click(screen.getByRole('checkbox'))
}

describe('L-C 确认：输入对象名或指定词都算数', () => {
  it('提示里同时给出文档名与指定词，不再自相矛盾', () => {
    setup()
    const hint = screen.getByText(/以确认/)
    expect(hint.textContent).toContain('文档名')
    expect(hint.textContent).toContain(DOC_NAME)
    expect(hint.textContent).toContain('DELETE')
  })

  it('输入文档名可以提交（用户实测卡住的就是这条）', () => {
    setup()
    expect(okButton()).toBeDisabled()
    fill(DOC_NAME)
    expect(okButton()).toBeEnabled()
  })

  it('输入指定词 DELETE 也可以提交', () => {
    setup()
    fill('DELETE')
    expect(okButton()).toBeEnabled()
  })

  it('首尾空白不影响判定，随便写别的仍然拒绝', () => {
    setup()
    fill(`  ${DOC_NAME}  `)
    expect(okButton()).toBeEnabled()

    fireEvent.change(wordInput(), { target: { value: '差不多就行' } })
    expect(okButton()).toBeDisabled()
    expect(screen.getByText(/要一字不差/)).toBeInTheDocument()
  })

  it('原因不足 10 字不给提交，并提示还差几个字', () => {
    setup()
    fireEvent.change(wordInput(), { target: { value: DOC_NAME } })
    fireEvent.change(reasonInput(), { target: { value: '太短' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(okButton()).toBeDisabled()
    expect(screen.getByText(/还需 8 字/)).toBeInTheDocument()
  })

  it('未勾选「我已知晓影响」不给提交', () => {
    setup()
    fireEvent.change(wordInput(), { target: { value: DOC_NAME } })
    fireEvent.change(reasonInput(), { target: { value: REASON } })
    expect(okButton()).toBeDisabled()
  })

  it('提交时把原因带出去（审计要落库）', () => {
    const { onOk } = setup()
    fill(DOC_NAME)
    fireEvent.click(okButton())
    expect(onOk).toHaveBeenCalledWith({ reason: REASON })
  })

  it('批量删除（没给对象名）时只认指定词，提示不说"文档名"', () => {
    setup({ confirmWord: 'DELETE', confirmNames: [] })
    const hint = screen.getByText(/以确认/)
    expect(hint.textContent).not.toContain('文档名')
    expect(okButton()).toBeDisabled()
    fill(DOC_NAME)
    expect(okButton()).toBeDisabled()
    fireEvent.change(wordInput(), { target: { value: 'DELETE' } })
    expect(okButton()).toBeEnabled()
  })
})
