import { describe, expect, it } from 'vitest'
import {
  buildQueryBody,
  faithfulnessBanner,
  citationRows,
  DEFAULT_PARAMS,
  epochSecondsToIso,
  parseStages,
  readDebugHistory,
  readShardVisibility,
  readSimulated,
  stageSectionKeys,
  timingRows,
} from './playgroundData'

/**
 * `stages` 样本逐字取自 `ekbs/retrieval/pipeline.py::retrieve(collect_debug=True)`。
 */
const REAL_STAGES = {
  params: {
    dense_top_k: 50,
    sparse_top_k: 50,
    rrf_k: 60,
    rerank_enabled: true,
    rerank_top_n: 20,
    rerank_keep: 5,
    rerank_min_score: 0.35,
    parent_expand: true,
  },
  dense: {
    requested_top_k: 50,
    per_shard: {
      'shard-tech-L2': [
        { faiss_row: 12, score: 0.8421 },
        { faiss_row: 88, score: 0.8104 },
      ],
      'shard-hr-L1': [{ faiss_row: 3, score: 0.7712 }],
    },
    merged_chunk_ids: [4512, 9871, 4519],
  },
  sparse: {
    requested_top_k: 50,
    hits: [
      { chunk_id: 9871, bm25: 18.42, shard_id: 'shard-tech-L2' },
      { chunk_id: 4512, bm25: 16.91, shard_id: 'shard-tech-L2' },
    ],
  },
  rrf: {
    k: 60,
    fused: [
      { chunk_id: 9871, score: 0.03251543 },
      { chunk_id: 4512, score: 0.03225806 },
    ],
  },
  rerank: {
    enabled: true,
    requested_top_n: 20,
    keep: 5,
    min_score: 0.35,
    scores: [
      { chunk_id: 9871, rerank_score: 0.9214 },
      { chunk_id: 4512, rerank_score: 0.8871 },
    ],
  },
  c5: { kept: [9871, 4512], dropped: [7711] },
}

describe('parseStages 逐跳解析', () => {
  it('完整 stages 全部解析出来', () => {
    const stages = parseStages(REAL_STAGES)
    expect(stageSectionKeys(stages)).toEqual(['params', 'dense', 'sparse', 'rrf', 'rerank', 'c5'])
    expect(stages.params?.dense_top_k).toBe(50)
    expect(stages.dense?.requestedTopK).toBe(50)
    expect(stages.dense?.perShard).toHaveLength(2)
    expect(stages.dense?.perShard[0].hits[0]).toEqual({ faiss_row: 12, score: 0.8421 })
    expect(stages.dense?.mergedChunkIds).toEqual([4512, 9871, 4519])
    expect(stages.sparse?.hits[0]).toEqual({ chunk_id: 9871, bm25: 18.42, shard_id: 'shard-tech-L2' })
    expect(stages.rrf?.k).toBe(60)
    expect(stages.rrf?.fused[0]).toEqual({ chunk_id: 9871, score: 0.03251543 })
    expect(stages.rerank?.requestedTopN).toBe(20)
    expect(stages.rerank?.keep).toBe(5)
    expect(stages.rerank?.minScore).toBe(0.35)
    expect(stages.c5?.kept).toEqual([9871, 4512])
    expect(stages.c5?.dropped).toEqual([7711])
  })

  it('拒答提前 return 时只有 params（不能凭空补出 dense/rerank）', () => {
    const stages = parseStages({ params: { rerank_keep: 5, parent_expand: true } })
    expect(stageSectionKeys(stages)).toEqual(['params'])
    expect(stages.dense).toBeNull()
    expect(stages.rerank).toBeNull()
    expect(stages.c5).toBeNull()
  })

  it('stages 缺失/不是对象时全为 null，不抛错', () => {
    expect(stageSectionKeys(parseStages(undefined))).toEqual([])
    expect(stageSectionKeys(parseStages('oops'))).toEqual([])
    expect(parseStages(null).params).toBeNull()
  })

  it('子段字段缺失时局部为 null，其余照常', () => {
    const stages = parseStages({ dense: { per_shard: { s1: [{ score: 0.5 }] } } })
    expect(stages.dense?.requestedTopK).toBeNull()
    expect(stages.dense?.mergedChunkIds).toEqual([])
    expect(stages.dense?.perShard[0].hits[0]).toEqual({ faiss_row: null, score: 0.5 })
  })

  it('rerank.enabled 只认真布尔，不把 "false" 当 false', () => {
    expect(parseStages({ rerank: { enabled: false } }).rerank?.enabled).toBe(false)
    expect(parseStages({ rerank: { enabled: 'false' } }).rerank?.enabled).toBeNull()
  })

  it('params 只收叶子值，嵌套对象被丢弃', () => {
    const params = parseStages({ params: { a: 1, b: true, c: 'x', d: { nested: 1 }, e: null } }).params
    expect(params).toEqual({ a: 1, b: true, c: 'x', e: null })
  })
})

describe('timingRows 耗时表', () => {
  it('已知键排成方案 §4.7 的链路顺序并给中文名，未知键排在后面', () => {
    const rows = timingRows({
      generate_ms: 911,
      rerank: 462,
      embed: 341,
      total: 2184,
      extra_stage: 7,
    })
    expect(rows.map((row) => row.key)).toEqual(['embed', 'rerank', 'total', 'generate_ms', 'extra_stage'])
    expect(rows[0].label).toBe('向量化')
    expect(rows[4].label).toBe('extra_stage')
  })

  it('没有耗时数据时是空表（不补零）', () => {
    expect(timingRows(undefined)).toEqual([])
    expect(timingRows({})).toEqual([])
  })
})

describe('citationRows 引用表', () => {
  it('后端只回 6 个字段', () => {
    const rows = citationRows([
      { cite_index: 1, chunk_id: 9871, doc_title: '差旅费管理办法', page_from: 12, acl_result: 'allow', page_check: 'pass' },
    ])
    expect(rows[0]).toEqual({
      cite_index: 1,
      chunk_id: 9871,
      doc_title: '差旅费管理办法',
      page_from: 12,
      acl_result: 'allow',
      page_check: 'pass',
    })
    expect(citationRows(undefined)).toEqual([])
  })
})

describe('readShardVisibility 权限解释', () => {
  it('registered_shards 是对象数组（不是 api/rag.ts 声明的 string[]）', () => {
    const view = readShardVisibility({
      user_id: 7,
      clearance: 2,
      dept_chain: ['集团总部', '技术中心'],
      visible_shards: ['shard-tech-L2'],
      acl_allow_shards: ['shard-tech-L2'],
      acl_deny_shards: ['shard-fin-L3'],
      matched_dept_codes: ['TECH'],
      registered_shards: [{ shard_id: 's1', dept_code: 'TECH', level: 2, status: 'active' }],
      warnings: ['clearance_capped'],
    })
    expect(view?.clearance).toBe(2)
    expect(view?.deptChain).toEqual(['集团总部', '技术中心'])
    expect(view?.aclDenyShards).toEqual(['shard-fin-L3'])
    expect(view?.registeredShards).toEqual([
      { shard_id: 's1', dept_code: 'TECH', level: 2, status: 'active' },
    ])
    expect(view?.warnings).toEqual(['clearance_capped'])
  })

  it('非对象返回 null', () => {
    expect(readShardVisibility(null)).toBeNull()
    expect(readShardVisibility([])).toBeNull()
  })
})

describe('readDebugHistory 历史', () => {
  it('解析 {items,count,retention}，at 是 epoch 秒', () => {
    const history = readDebugHistory({
      items: [
        { at: 1758600000, kind: 'retrieve', operator: 7, query: '住宿标准', chunks: 5, refused: false },
      ],
      count: 1,
      retention: 'ekbs_cache TTL 7 天 / 最多 50 条（Schema 无调试历史表）',
    })
    expect(history?.items).toHaveLength(1)
    expect(history?.items[0].kind).toBe('retrieve')
    expect(history?.items[0].refused).toBe(false)
    expect(history?.items[0].at).toBe(new Date(1758600000 * 1000).toISOString())
    expect(history?.count).toBe(1)
    expect(history?.retention).toContain('7 天')
  })

  it('items 缺失或不是数组 → null（页面据此省略整块，不显示假历史）', () => {
    expect(readDebugHistory({ count: 0 })).toBeNull()
    expect(readDebugHistory(null)).toBeNull()
  })

  it('epoch 转换只认 9–11 位秒级时间戳', () => {
    expect(epochSecondsToIso('1758600000')).toBe(new Date(1758600000 * 1000).toISOString())
    expect(epochSecondsToIso('2025-09-23 09:12:00')).toBe('2025-09-23 09:12:00')
    expect(epochSecondsToIso(null)).toBeNull()
  })
})

describe('readSimulated 权限模拟结果', () => {
  it('回显 operator / simulated_user / simulated_perm', () => {
    const result = readSimulated({
      operator: 1,
      simulated_user: 7,
      reason: '排查张伟看不到制度库',
      query: '住宿标准',
      refused: true,
      refusal_reason: 'no_visible_shard',
      simulated_perm: {
        clearance: 1,
        dept_chain: ['集团总部'],
        visible_shards: [],
        fingerprint: 'abc123',
      },
      chunks: [],
    })
    expect(result?.operator).toBe(1)
    expect(result?.simulatedUser).toBe(7)
    expect(result?.refused).toBe(true)
    expect(result?.refusalReason).toBe('no_visible_shard')
    expect(result?.clearance).toBe(1)
    expect(result?.chunkCount).toBeNull()
  })
})

describe('buildQueryBody 按后端真实字段名下参', () => {
  it('用 dense_top_k / sparse_top_k / rerank_keep / rerank_min_score（不是 top_k/keep/min_score）', () => {
    const body = buildQueryBody('住宿标准', { ...DEFAULT_PARAMS, rerankKeep: 8 })
    expect(body).toEqual({
      query: '住宿标准',
      mode: 'prose',
      rerank_enabled: true,
      dense_top_k: 50,
      sparse_top_k: 50,
      rerank_keep: 8,
      rerank_min_score: 0.35,
    })
    expect('top_k' in body).toBe(false)
    expect('keep' in body).toBe(false)
    expect('min_score' in body).toBe(false)
  })
})

describe('真实后端报文（实测 /rag/debug/query，2026-09-29）', () => {
  // 这份 payload 从**真实响应**里摘出来，键名逐字保留、长列表各留 1 条。
  // 它锁住的是"归一化用对了键名"，也就是用户实测那次崩溃的根：
  // 后端给 `per_shard`，面板读 `perShard`；跳过归一化直接传原文 → undefined → reduce 抛错。
  const realStages =   {
    "params": {
      "dense_top_k": 30,
      "sparse_top_k": 30,
      "rrf_k": 60,
      "rerank_enabled": true,
      "rerank_top_n": 15,
      "rerank_keep": 5,
      "rerank_min_score": 0.6,
      "parent_expand": true
    },
    "dense": {
      "requested_top_k": 30,
      "per_shard": {
        "kb_all_L1": [
          {
            "faiss_row": 10,
            "score": 0.595099
          }
        ]
      },
      "merged_chunk_ids": [
        10
      ]
    },
    "sparse": {
      "requested_top_k": 30,
      "hits": [
        {
          "chunk_id": 10,
          "bm25": -5.365153,
          "shard_id": "kb_all_L1"
        }
      ]
    },
    "rrf": {
      "k": 60,
      "fused": [
        {
          "chunk_id": 10,
          "score": 0.03278689
        }
      ]
    },
    "rerank": {
      "enabled": true,
      "requested_top_n": 15,
      "keep": 5,
      "min_score": 0.6,
      "scores": [
        {
          "chunk_id": 10,
          "rerank_score": 0.8464576878253378
        }
      ]
    },
    "c5": {
      "kept": [
        10
      ],
      "dropped": []
    }
  }

  it('归一化后各跳都有值，perShard 可直接 reduce', () => {
    const stages = parseStages(realStages)

    expect(stages.dense?.requestedTopK).toBe(30)
    expect(stages.dense?.perShard).toHaveLength(1)
    expect(stages.dense?.perShard[0].shard_id).toBe('kb_all_L1')
    expect(stages.dense?.perShard[0].hits[0]).toEqual({ faiss_row: 10, score: 0.595099 })
    expect(stages.dense?.mergedChunkIds).toEqual([10])

    // 面板第一行就是这么写的（StagesPanel.tsx:54）
    const total = stages.dense!.perShard.reduce((sum, shard) => sum + shard.hits.length, 0)
    expect(total).toBe(1)

    expect(stages.sparse?.hits[0]).toEqual({ chunk_id: 10, bm25: -5.365153, shard_id: 'kb_all_L1' })
    expect(stages.rrf?.k).toBe(60)
    expect(stages.rrf?.fused[0]).toEqual({ chunk_id: 10, score: 0.03278689 })
    expect(stages.rerank?.requestedTopN).toBe(15)
    expect(stages.c5).toEqual({ kept: [10], dropped: [] })
    expect(stages.params?.dense_top_k).toBe(30)
  })

  it('故意用原始报文（不归一化）时 perShard 就是 undefined —— 这正是当时的崩溃', () => {
    const raw = realStages as unknown as { dense: { perShard?: unknown } }
    expect(raw.dense.perShard).toBeUndefined()
  })
})

describe('faithfulnessBanner（与后端 P0 分级策略一致）', () => {
  it('未给忠实度报告 → 不渲染', () => {
    expect(faithfulnessBanner({ ok: undefined })).toBeNull()
    expect(faithfulnessBanner({ ok: null })).toBeNull()
  })

  it('通过 → 绿，并带上引用编号与 claim 数', () => {
    const b = faithfulnessBanner({ ok: true, citedIndices: [1, 2], claimCount: 3 })
    expect(b?.type).toBe('success')
    expect(b?.detail).toContain('[1, 2]')
    expect(b?.detail).toContain('3 条')
  })

  it('**降级放行**：未通过但没拒答 → 黄（不能误报成拒答）', () => {
    const b = faithfulnessBanner({
      ok: false,
      refused: false,
      reason: 'unsupported_claim',
      unsupported: ['474.004万元', '7.69%'],
      claimCount: 25,
    })
    expect(b?.type).toBe('warning')
    expect(b?.message).toContain('降级放行')
    expect(b?.detail).toContain('2 个')
    expect(b?.detail).toContain('人工核对')
  })

  it('整篇对不上 → 拒答 → 红', () => {
    const b = faithfulnessBanner({ ok: false, refused: true, reason: 'unsupported_claim' })
    expect(b?.type).toBe('error')
    expect(b?.message).toContain('已拒答')
    expect(b?.detail).toContain('整篇不可信')
  })

  it('无引用标记 → 拒答 → 红，并说明禁止伪溯源', () => {
    const b = faithfulnessBanner({ ok: false, refused: true, reason: 'missing_citation' })
    expect(b?.type).toBe('error')
    expect(b?.detail).toContain('禁止挂伪溯源引用')
  })
})

describe('作答模式进请求体（prose / numeric）', () => {
  it('默认是业务问答（prose）—— 不加模式时行为与原来完全一致', () => {
    expect(buildQueryBody('年假有多少天', DEFAULT_PARAMS).mode).toBe('prose')
  })

  it('取数模式要真的发到后端（否则界面上选了也没用）', () => {
    const body = buildQueryBody('电梯维保一年多少钱', { ...DEFAULT_PARAMS, mode: 'numeric' })
    expect(body.mode).toBe('numeric')
  })
})
