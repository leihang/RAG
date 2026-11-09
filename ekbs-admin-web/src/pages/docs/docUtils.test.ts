/**
 * `src/pages/docs/docUtils.ts` 的单测（方案 05 §5.5/§5.6/§6.5）。
 *
 * 重点是三处**契约与后端不一致**的整形逻辑：批量结果、详情响应、visible_dept_ids。
 * 这三处一旦回归，页面会静默显示错数据，所以必须有用例盯住。
 */
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_CHUNK_SIZE,
  deptText,
  documentExtra,
  fileExtension,
  hasActiveFilter,
  initialLevelForKb,
  isAllowedFile,
  joinUploadDest,
  kbOptionsFrom,
  levelLabel,
  MAX_FILE_BYTES,
  normalizeBatchResult,
  parseDeptIds,
  planChunks,
  rejectReason,
  resolveKbTarget,
  sha256Hex,
  summarizeBatch,
  summarizeQueue,
  unwrapDocument,
  UPLOAD_ACCEPT_EXTS,
} from './docUtils'
import type { DocumentItem } from '@/api/types'

function doc(over: Partial<DocumentItem> = {}): DocumentItem {
  return {
    doc_id: 1,
    kb_id: 10,
    kb_code: 'product',
    title: '产品白皮书.pdf',
    level: 2,
    status: 'published',
    visible_dept_ids: '[]',
    ...over,
  }
}

describe('上传准入（§4.4 边界）', () => {
  it('后缀判定与后端白名单一致，且大小写不敏感', () => {
    expect(fileExtension('a.b.PDF')).toBe('pdf')
    expect(fileExtension('noext')).toBe('')
    expect(isAllowedFile('白皮书.pdf')).toBe(true)
    expect(isAllowedFile('白皮书.PDF')).toBe(true)
    expect(isAllowedFile('宏.exe')).toBe(false)
    expect(isAllowedFile('旧.doc')).toBe(true)
    expect(UPLOAD_ACCEPT_EXTS).toContain('doc')
  })

  it('本地先拦类型/大小/空文件，不让后端 413/422 兜底', () => {
    expect(rejectReason({ name: 'a.pdf', size: 1024 })).toBeNull()
    expect(rejectReason({ name: 'a.exe', size: 1024 })).toContain('不支持 .exe 类型')
    expect(rejectReason({ name: 'a.pdf', size: 0 })).toContain('空文件')
    expect(rejectReason({ name: 'a.pdf', size: MAX_FILE_BYTES + 1 })).toContain('超出单文件限制 100 MB')
    expect(rejectReason({ name: 'a.pdf', size: MAX_FILE_BYTES })).toBeNull()
  })

  it('crypto.subtle 不可用时降级为不算摘要（不抛错）', async () => {
    const file = new File(['hello ekbs'], 'a.txt', { type: 'text/plain' })
    const got = await sha256Hex(file)
    // jsdom/Node 环境可能给也可能不给 subtle：两种结果都必须被接受，绝不允许抛错
    expect(got === null || /^[0-9a-f]{64}$/.test(got)).toBe(true)
  })
})

describe('上传目标与分片（§6.5）', () => {
  it('保存到文件夹前缀与文件相对路径正确拼接', () => {
    expect(joinUploadDest('', 'a.md')).toBe('a.md')
    expect(joinUploadDest(undefined, 'a.md')).toBe('a.md')
    expect(joinUploadDest('制度/2026', '预算.xlsx')).toBe('制度/2026/预算.xlsx')
    expect(joinUploadDest('/制度/', '子/a.md')).toBe('制度/子/a.md')
    expect(joinUploadDest('制度\\2026', 'a.md')).toBe('制度/2026/a.md')
  })

  it('目标知识库：按 code 命中候选，纯数字当 kb_id，其余当 kb_code', () => {
    const options = [{ kb_id: 10, kb_code: 'product' }]
    expect(resolveKbTarget('product', options)).toEqual({ kb_id: 10, kb_code: 'product' })
    expect(resolveKbTarget(' 7 ', options)).toEqual({ kb_id: 7 })
    expect(resolveKbTarget('unknown_code', options)).toEqual({ kb_code: 'unknown_code' })
    expect(resolveKbTarget('   ', options)).toBeNull()
  })

  it('知识库候选来自已见文档并按 kb_id 去重', () => {
    const opts = kbOptionsFrom([
      doc({ doc_id: 1, kb_id: 10, kb_code: 'product' }),
      doc({ doc_id: 2, kb_id: 10, kb_code: 'product' }),
      doc({ doc_id: 3, kb_id: 11, kb_code: undefined }),
    ])
    expect(opts).toHaveLength(2)
    expect(opts[0]).toEqual({ value: 'product', label: 'product（kb_id=10）', kb_id: 10, kb_code: 'product' })
    expect(opts[1].value).toBe('11')
  })

  it('可见部门：中英文逗号/分号/空格都能切，且去重去空', () => {
    expect(parseDeptIds('tech, market')).toEqual(['tech', 'market'])
    expect(parseDeptIds('tech，market；finance  tech')).toEqual(['tech', 'market', 'finance'])
    expect(parseDeptIds('  ')).toEqual([])
  })

  it('分片规划用后端 hint，缺失时兜底 5 MB；最后一片允许更小', () => {
    expect(planChunks(0, 1024)).toEqual({ chunkSize: 1024, total: 1 })
    expect(planChunks(2048, 1024)).toEqual({ chunkSize: 1024, total: 2 })
    expect(planChunks(2049, 1024)).toEqual({ chunkSize: 1024, total: 3 })
    expect(planChunks(10, null).chunkSize).toBe(DEFAULT_CHUNK_SIZE)
    expect(planChunks(10, 0).chunkSize).toBe(DEFAULT_CHUNK_SIZE)
  })

  it('队列汇总区分秒传/成功/失败/进行中（串行上传的完成判定）', () => {
    const s = summarizeQueue([
      { status: 'instant', uploaded: 0, total: 100 },
      { status: 'success', uploaded: 200, total: 200 },
      { status: 'failed', uploaded: 50, total: 300 },
      { status: 'uploading', uploaded: 25, total: 400 },
      { status: 'pending', uploaded: 0, total: 500 },
    ])
    expect(s).toEqual({
      total: 5,
      pending: 1,
      running: 1,
      success: 1,
      instant: 1,
      failed: 1,
      bytesTotal: 1500,
      // 秒传/成功按整份文件计入，失败与进行中按已上传字节计入
      bytesUploaded: 375,
    })
  })
})

describe('批量结果归一化（契约类型 ≠ 后端真实报文）', () => {
  it('认后端真实形状 {action, succeeded: number, results[]}', () => {
    const got = normalizeBatchResult({
      action: 'publish',
      succeeded: 2,
      results: [
        { doc_id: 1, ok: true, status: 'published' },
        { doc_id: 2, ok: true, status: 'published' },
        { doc_id: 3, ok: false, error: '文档尚无分块' },
      ],
    })
    expect(got.succeeded).toBe(2)
    expect(got.failed).toEqual([{ doc_id: 3, error: '文档尚无分块' }])
  })

  it('同时兼容契约里写的 {succeeded: number[], failed[]}', () => {
    const got = normalizeBatchResult({ succeeded: [1, 2], failed: [{ doc_id: 3, error: 'denied' }] })
    expect(got.succeeded).toBe(2)
    expect(got.failed).toEqual([{ doc_id: 3, error: 'denied' }])
  })

  it('空/异常响应不炸', () => {
    expect(normalizeBatchResult(undefined)).toEqual({ succeeded: 0, failed: [] })
    expect(normalizeBatchResult({})).toEqual({ succeeded: 0, failed: [] })
  })
})

describe('L-B 影响清单（§5.5 影响面数字必须真实）', () => {
  it('统计份数与密级分布，并标出高于自身密级的份数', () => {
    const impact = summarizeBatch(
      [doc({ doc_id: 1, level: 2 }), doc({ doc_id: 2, level: 2 }), doc({ doc_id: 3, level: 4 })],
      3,
    )
    expect(impact.count).toBe(3)
    expect(impact.ids).toEqual([1, 2, 3])
    expect(impact.byLevel).toEqual([
      { level: 'L2', count: 2 },
      { level: 'L4', count: 1 },
    ])
    expect(impact.aboveClearance).toBe(1)
  })

  it('拿不到密级时不虚报"超过"', () => {
    expect(summarizeBatch([doc({ level: 4 })], null).aboveClearance).toBe(0)
    expect(levelLabel(3)).toBe('L3 秘密')
    expect(levelLabel(null)).toBe('未知')
  })
})

describe('列表/详情报文整形', () => {
  it('visible_dept_ids 数组（后端真实）与 JSON 字符串（类型声明）都能显示', () => {
    expect(deptText(['tech', 'market'])).toBe('tech、market')
    expect(deptText('["tech","market"]')).toBe('tech、market')
    expect(deptText('tech,market')).toBe('tech,market')
    expect(deptText([])).toBe('不限部门')
    expect(deptText('')).toBe('不限部门')
    expect(deptText(null)).toBe('-')
  })

  it('详情响应带 document 包装（后端真实）或被剥壳（契约声明）都能取到', () => {
    const inner = doc({ doc_id: 42 })
    expect(unwrapDocument({ document: inner, chunks: {}, shards: [] })?.doc_id).toBe(42)
    expect(unwrapDocument(inner)?.doc_id).toBe(42)
    expect(unwrapDocument(null)).toBeNull()
    expect(unwrapDocument({ foo: 1 })).toBeNull()
  })

  it('详情附加统计：分块构成 / 分片 / 引用次数，缺失时给中性默认值', () => {
    const extra = documentExtra({
      document: doc(),
      chunks: { child: 12, parent: 3 },
      shards: ['shard-a'],
      citation_count: 5,
    })
    expect(extra.chunkTypeCounts).toEqual({ child: 12, parent: 3 })
    expect(extra.shards).toEqual(['shard-a'])
    expect(extra.citationCount).toBe(5)

    expect(documentExtra(null)).toEqual({ chunkTypeCounts: {}, shards: [], citationCount: null })
  })

  it('空库与筛选无结果要能区分（§5.4 两套文案）', () => {
    expect(hasActiveFilter({})).toBe(false)
    expect(hasActiveFilter({ q: '   ' })).toBe(false)
    expect(hasActiveFilter({ status: '' })).toBe(false)
    expect(hasActiveFilter({ q: '白皮书' })).toBe(true)
    expect(hasActiveFilter({ level: '3' })).toBe(true)
  })
})

describe('知识库默认密级 vs 本文档密级（两个不是一回事）', () => {
  const kbs = [
    { kb_id: 1, default_level: 1 },
    { kb_id: 3, default_level: 2 },
    { kb_id: 9, default_level: null },
    { kb_id: 11 },
    { kb_id: 12, default_level: 0 },
    { kb_id: 13, default_level: 5 },
  ]

  it('选中库就把该库的默认密级带成上传页初值', () => {
    expect(initialLevelForKb(kbs, 1)).toBe(1)
    expect(initialLevelForKb(kbs, 3)).toBe(2)
  })

  it('库没给/给的是越界值 → 不改动用户当前选择（返回 undefined）', () => {
    // 这条是重点：早年写成 `if (kb?.default_level)` 时，null/0 会静默落空，
    // 而 5 这种越界值会被塞进表单、由后端 422 兜底 —— 两种都不该发生
    expect(initialLevelForKb(kbs, 9)).toBeUndefined()
    expect(initialLevelForKb(kbs, 11)).toBeUndefined()
    expect(initialLevelForKb(kbs, 12)).toBeUndefined()
    expect(initialLevelForKb(kbs, 13)).toBeUndefined()
  })

  it('没选库 / 库不在列表里 → 不动', () => {
    expect(initialLevelForKb(kbs, undefined)).toBeUndefined()
    expect(initialLevelForKb(kbs, null)).toBeUndefined()
    expect(initialLevelForKb(kbs, 999)).toBeUndefined()
    expect(initialLevelForKb([], 1)).toBeUndefined()
  })
})

describe('批量结果归一化（后端真实报文）', () => {
  // 摘自 ekbs/api/docs.py::batch_documents 的真实返回：
  // {action, succeeded: <数字>, results: [{doc_id, ok, status|error}]}
  // 原先前端把它声明成 {succeeded: number[]; failed: [...]} —— 两个字段后端都不发，
  // 于是 `res.succeeded.length` 恒为 undefined、`res.failed` 恒为 []，
  // **部分失败会被显示成"全部已提交"**。
  const real = {
    action: 'delete',
    succeeded: 2,
    results: [
      { doc_id: 3, ok: true, status: 'deleted' },
      { doc_id: 5, ok: true, status: 'deleted' },
      { doc_id: 9, ok: false, error: '文档不存在：9' },
    ],
  }

  it('逐条判定：失败条目要带出 doc_id 与原因', () => {
    expect(normalizeBatchResult(real)).toEqual({
      succeeded: 2,
      failed: [{ doc_id: 9, error: '文档不存在：9' }],
    })
  })

  it('全成功时 failed 为空', () => {
    expect(normalizeBatchResult({ action: 'offline', succeeded: 1, results: [{ doc_id: 1, ok: true }] }))
      .toEqual({ succeeded: 1, failed: [] })
  })

  it('旧口径（succeeded 是数组 / 带 failed 数组）仍兼容', () => {
    expect(normalizeBatchResult({ succeeded: [1, 2] })).toEqual({ succeeded: 2, failed: [] })
    expect(normalizeBatchResult({ failed: [{ doc_id: 7, error: 'boom' }] }))
      .toEqual({ succeeded: 0, failed: [{ doc_id: 7, error: 'boom' }] })
  })

  it('空/非对象输入不炸', () => {
    expect(normalizeBatchResult(null)).toEqual({ succeeded: 0, failed: [] })
    expect(normalizeBatchResult(undefined)).toEqual({ succeeded: 0, failed: [] })
  })
})
