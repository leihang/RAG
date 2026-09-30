/**
 * 文件扩展名归一化（**与后端 `ekbs.ingest.parser.normalize_suffix` 同口径**）。
 *
 * 为什么不能直接 `name.split('.').pop()`：真实文件名常带这些"看不见"的东西，
 * 它们会让一个 `.xlsx` 被判成"不支持的类型"（实测踩到）：
 *
 * | 文件名 | 裸切法 | 归一化后 |
 * |---|---|---|
 * | `报表.xlsx `（尾部空格，Windows/邮件下载常见） | `'xlsx '` | `xlsx` |
 * | `报表．xlsx`（**全角**句点，中文输入法） | `'报表．xlsx'` | `xlsx` |
 * | `报表.xlsx.`（尾部多余点） | `''` | `xlsx` |
 *
 * 注意：`xlsx` 里的 `x` 之后还有 `lsx`，**不要**用 `\w+$` 之类去匹配中文名。
 */
export function normalizeExt(filename: string): string {
  let name = (filename ?? '').trim()
  // 全角句点 / 全角空格先归一到半角，否则后缀切不出来
  name = name.replace(/．/g, '.').replace(/\u3000/g, ' ')
  name = name.trim().replace(/[.\s]+$/, '')
  const idx = name.lastIndexOf('.')
  if (idx <= 0 || idx === name.length - 1) return ''
  return name.slice(idx + 1).toLowerCase()
}

/** 人类可读的支持列表：`.pdf / .docx / …` */
export function formatExtList(exts: readonly string[]): string {
  return exts.map((e) => `.${e}`).join(' / ')
}
