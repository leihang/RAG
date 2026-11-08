/**
 * 答案正文：普通段落仍按 pre-wrap；Markdown 管道表渲染成 HTML 表，
 * 避免管理台把 `| 项目 | 用量 |` 当纯文本导致栏目对不齐。
 */
import type { CSSProperties, ReactNode } from 'react'

const TABLE_LINE = /^\s*\|.*\|\s*$/
const SEP_LINE = /^\s*\|(?:\s*:?-+:?\s*\|)+\s*$/

function splitCells(line: string): string[] {
  let text = line.trim()
  if (text.startsWith('|')) text = text.slice(1)
  if (text.endsWith('|')) text = text.slice(0, -1)
  return text.split('|').map((c) => c.trim())
}

type Block =
  | { kind: 'text'; text: string }
  | { kind: 'table'; headers: string[]; rows: string[][] }

function parseBlocks(answer: string): Block[] {
  const lines = String(answer || '').split(/\r?\n/)
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    if (!TABLE_LINE.test(lines[i] ?? '')) {
      const start = i
      while (i < lines.length && !TABLE_LINE.test(lines[i] ?? '')) i += 1
      blocks.push({ kind: 'text', text: lines.slice(start, i).join('\n') })
      continue
    }
    const tableLines: string[] = []
    while (i < lines.length && TABLE_LINE.test(lines[i] ?? '')) {
      tableLines.push(lines[i] ?? '')
      i += 1
    }
    let headers: string[] = []
    const rows: string[][] = []
    let sawSep = false
    for (const line of tableLines) {
      if (SEP_LINE.test(line)) {
        sawSep = true
        continue
      }
      const cells = splitCells(line)
      if (!headers.length && !sawSep) {
        headers = cells
        continue
      }
      if (!headers.length) headers = cells.map((_, idx) => `列${idx + 1}`)
      rows.push(cells)
    }
    if (headers.length) {
      blocks.push({ kind: 'table', headers, rows })
    } else {
      blocks.push({ kind: 'text', text: tableLines.join('\n') })
    }
  }
  return blocks
}

const tableStyle: CSSProperties = {
  borderCollapse: 'collapse',
  width: '100%',
  margin: '8px 0 12px',
  fontSize: 13,
  lineHeight: 1.45,
}

const cellStyle: CSSProperties = {
  border: '1px solid #d9d9d9',
  padding: '6px 10px',
  textAlign: 'left',
  whiteSpace: 'nowrap',
}

const thStyle: CSSProperties = {
  ...cellStyle,
  background: '#fafafa',
  fontWeight: 600,
}

export default function AnswerMarkdown({
  text,
  style,
  empty = '（空）',
}: {
  text?: string | null
  style?: CSSProperties
  empty?: string
}) {
  const raw = text ?? ''
  if (!raw.trim()) {
    return <div style={{ whiteSpace: 'pre-wrap', ...style }}>{empty}</div>
  }
  if (!raw.includes('|')) {
    return <div style={{ whiteSpace: 'pre-wrap', ...style }}>{raw}</div>
  }

  const blocks = parseBlocks(raw)
  const nodes: ReactNode[] = []
  blocks.forEach((block, idx) => {
    if (block.kind === 'text') {
      if (!block.text) return
      nodes.push(
        <div key={`t-${idx}`} style={{ whiteSpace: 'pre-wrap' }}>
          {block.text}
        </div>,
      )
      return
    }
    const width = Math.max(block.headers.length, ...block.rows.map((r) => r.length), 0)
    const headers = [...block.headers]
    while (headers.length < width) headers.push(`列${headers.length + 1}`)
    nodes.push(
      <div key={`tbl-${idx}`} style={{ overflowX: 'auto' }}>
        <table style={tableStyle}>
          <thead>
            <tr>
              {headers.map((h, col) => (
                <th key={col} style={thStyle}>
                  {h || '—'}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row, rIdx) => (
              <tr key={rIdx}>
                {headers.map((_, col) => (
                  <td key={col} style={cellStyle}>
                    {(row[col] ?? '').trim() || '—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    )
  })

  return <div style={style}>{nodes.length ? nodes : raw}</div>
}
