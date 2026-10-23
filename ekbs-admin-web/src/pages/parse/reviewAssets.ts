import type { ParseDraftFigure, ParseDraftTable } from '@/api/parse'

/** 正文锚点只用文件名摘要的前 12 位，与 ``[图片 48da7cf6afd1]`` 相同。 */
export function figureToken(figureId: string): string {
  return (figureId || '').slice(0, 12)
}

export function figureAnchorLabel(figureId: string): string {
  const token = figureToken(figureId)
  return token ? `[图片 ${token}]` : '[图片]'
}

function escapeReg(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function setTableCaptionLine(markdown: string, tableId: string, caption: string): string {
  const suffix = caption.trim() ? ` ${caption.trim()}` : ''
  return markdown.replace(
    new RegExp(`^\\[表格\\s+${escapeReg(tableId)}\\][^\\n]*`, 'm'),
    `[表格 ${tableId}]${suffix}`,
  )
}

export function setFigureCaptionLine(markdown: string, figureId: string, caption: string): string {
  const token = figureToken(figureId)
  if (!token) return markdown
  const suffix = caption.trim() ? ` ${caption.trim()}` : ''
  return markdown.replace(
    new RegExp(`^\\[图片\\s+${escapeReg(token)}\\][^\\n]*`, 'm'),
    `[图片 ${token}]${suffix}`,
  )
}

export function deleteTableFromDraft(
  markdown: string,
  tables: ParseDraftTable[],
  tableId: string,
): { markdown: string; tables: ParseDraftTable[] } {
  const kept = tables.filter((item) => item.table_id !== tableId)
  let text = markdown.replace(new RegExp(`^\\[表格\\s+${escapeReg(tableId)}\\][^\\n]*\\n?`, 'm'), '')
  const renumber = new Map(kept.map((item, index) => [item.table_id, String(index + 1)]))
  text = text.replace(/^\[表格\s+(\d+)\]/gm, (line, id: string) => {
    const next = renumber.get(id)
    return next ? line.replace(`[表格 ${id}]`, `[表格 ${next}]`) : line
  })
  return {
    markdown: text,
    tables: kept.map((item, index) => ({ ...item, table_id: String(index + 1) })),
  }
}

export function deleteFigureFromDraft(
  markdown: string,
  figures: ParseDraftFigure[],
  figureId: string,
): { markdown: string; figures: ParseDraftFigure[] } {
  const token = figureToken(figureId)
  const text = token
    ? markdown.replace(new RegExp(`^\\[图片\\s+${escapeReg(token)}\\][^\\n]*\\n?`, 'm'), '')
    : markdown
  return {
    markdown: text,
    figures: figures.filter((item) => item.figure_id !== figureId),
  }
}
