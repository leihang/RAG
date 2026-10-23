import { describe, expect, it } from 'vitest'
import type { ParseDraftFigure, ParseDraftTable } from '@/api/parse'
import {
  deleteFigureFromDraft,
  deleteTableFromDraft,
  figureAnchorLabel,
  setFigureCaptionLine,
  setTableCaptionLine,
} from './reviewAssets'

const tables: ParseDraftTable[] = [
  { table_id: '1', body_html: '<table>1</table>', caption: '甲' },
  { table_id: '2', body_html: '<table>2</table>' },
  { table_id: '3', body_html: '<table>3</table>', caption: '丙' },
]

describe('复核表格和图片编号', () => {
  it('图片编号与正文锚点相同', () => {
    expect(figureAnchorLabel('48da7cf6afd1ebb0744eeb74')).toBe('[图片 48da7cf6afd1]')
  })

  it('删掉中间一张表后，后面的编号前移', () => {
    const markdown = '[表格 1] 甲\n[表格 2]\n[表格 3] 丙\n后记\n'
    const next = deleteTableFromDraft(markdown, tables, '2')
    expect(next.markdown).toBe('[表格 1] 甲\n[表格 2] 丙\n后记\n')
    expect(next.tables.map((item) => item.table_id)).toEqual(['1', '2'])
    expect(next.tables[1]?.body_html).toBe('<table>3</table>')
  })

  it('改题注时同步正文里的编号行', () => {
    const markdown = '[表格 1]\n[图片 48da7cf6afd1]\n'
    expect(setTableCaptionLine(markdown, '1', '服务范围')).toContain('[表格 1] 服务范围')
    expect(setFigureCaptionLine(markdown, '48da7cf6afd1ebb0', '现场照片')).toContain(
      '[图片 48da7cf6afd1] 现场照片',
    )
  })

  it('删除图片时去掉正文里的对应编号', () => {
    const figures: ParseDraftFigure[] = [
      { figure_id: '48da7cf6afd1ebb0', caption: '' },
      { figure_id: '387c44f11edb401e', caption: '平面' },
    ]
    const markdown = '前文\n[图片 48da7cf6afd1]\n[图片 387c44f11edb] 平面\n'
    const next = deleteFigureFromDraft(markdown, figures, figures[0]!.figure_id)
    expect(next.markdown).toBe('前文\n[图片 387c44f11edb] 平面\n')
    expect(next.figures).toHaveLength(1)
  })
})
