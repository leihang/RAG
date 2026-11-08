"""「模型上下文」暴露 + 落审计（task-5）。

**用户困惑的原点**（doc 205 / 「能耗管理员在能耗管理中的主要作用」）：真实 prompt 里有
3 个上下文块 —— [1] 35280（被引用）、[2] 35274←35277（被引用）、[3] 35281←35284
（**进了 prompt 但没被引用**）。管理台过去只展示引用，于是用户看到"只有 2 块"，
得出 35281「没发给模型」的错误结论。

本组用例钉住三件事：

1. ``AnswerResult.context_chunks`` / ``context_chunk_ids`` = **真正进入 prompt 的块**
   （``_fit_prompt`` 裁剪后），且索引 i 对应引用编号 i+1；
2. 答案缓存命中时块正文无法重建，但 ``context_chunk_ids`` 必须不丢；
3. ``ekbs/api/playground._context_chunk_views`` 的 ``cited`` 是「引用集合」的反推，
   未引用 ≠ 未发送。
"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.api.playground import _context_chunk_views
from ekbs.ingest.chunker import count_tokens
from ekbs.retrieval.pipeline import (
    AnswerResult,
    Citation,
    RetrievalResult,
    RetrievedChunk,
    _merge_prompt_context,
)
from tests.conftest import user_id_of

# --- 与线上 doc 205 同形的 3 个上下文块 -------------------------------------------


def _chunk(
    chunk_id: int,
    *,
    expanded_from: int | None = None,
    content: str = "能耗管理员负责能耗统计、分析与节能措施跟踪。",
    heading_path: str = "5. 工作程序和要求 > 5.1 能耗管理员职责",
    doc_title: str = "XJWY-GC-12 能耗管理作业指导书",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=205,
        shard_id="kb_energy_L2",
        content=content,
        heading_path=heading_path,
        chunk_type="parent" if expanded_from is not None else "child",
        doc_title=doc_title,
        rerank_score=0.899 if expanded_from is not None else 0.912,
        expanded_from=expanded_from,
    )


def _citation(cite_index: int, chunk_id: int, source_chunk_id: int | None = None) -> Citation:
    return Citation(
        cite_index=cite_index,
        chunk_id=chunk_id,
        doc_id=205,
        doc_title="XJWY-GC-12 能耗管理作业指导书",
        doc_version="v1",
        heading_path="5. 工作程序和要求 > 5.1 能耗管理员职责",
        page_from=3,
        page_to=3,
        rerank_score=0.9,
        acl_result="pass",
        page_check="pass",
        source_chunk_id=source_chunk_id,
    )


def _doc205_answer(
    chunks: list[RetrievedChunk], *, cited_indices: tuple[int, ...] = (1, 2)
) -> AnswerResult:
    """doc 205 的答案：3 块进 prompt，只引用前 2 块（[3] 35281 未引用）。"""
    result = RetrievalResult(
        query="能耗管理员在能耗管理中的主要作用",
        chunks=chunks,
        # 精排名次按来源块给：35277=1、35280=2、35284=3
        stage_debug={
            "rerank": {
                "scores": [
                    {"chunk_id": 35277},
                    {"chunk_id": 35280},
                    {"chunk_id": 35284},
                ]
            }
        },
    )
    citations_by_index = {
        1: _citation(1, 35280),
        2: _citation(2, 35274, source_chunk_id=35277),
        3: _citation(3, 35281, source_chunk_id=35284),
    }
    return AnswerResult(
        answer="能耗管理员负责…… [1][2]",
        citations=[citations_by_index[i] for i in cited_indices],
        retrieval=result,
        model="stub-llm",
        context_chunks=list(chunks),
        context_chunk_ids=[c.chunk_id for c in chunks],
    )


# --- 单元级：视图与合并 ----------------------------------------------------------


def test_context_views_expose_prompt_blocks_even_when_uncited() -> None:
    """doc 205 的复刻：第 3 块（35281←35284）在上下文里、但 cited=false。"""
    chunks = [
        _chunk(35280),
        _chunk(35274, expanded_from=35277),
        _chunk(35281, expanded_from=35284),
    ]
    answer = _doc205_answer(chunks)

    views = _context_chunk_views(answer)

    assert [v["index"] for v in views] == [1, 2, 3]
    assert [v["chunk_id"] for v in views] == [35280, 35274, 35281]
    assert [v["cited"] for v in views] == [True, True, False]
    # 上下文块 ← 精排来源块：未引用的第 3 块也要能追到它由 35284 扩展而来
    assert views[1]["source_chunk_id"] == 35277
    assert views[1]["source_rerank_rank"] == 1
    assert views[2]["source_chunk_id"] == 35284
    assert views[2]["source_rerank_rank"] == 3
    assert views[0]["source_rerank_rank"] == 2
    # token 数是逐块 count_tokens，用于前端算上下文占比
    assert views[0]["token_count"] == count_tokens(chunks[0].content)
    assert views[0]["token_count"] > 0
    assert views[2]["doc_title"] == "XJWY-GC-12 能耗管理作业指导书"


def test_merge_prompt_context_dedupes_keeping_first_order() -> None:
    """多子问合并：同一块只出现一次，顺序按首见（与全局引用重编号同序）。"""
    first = AnswerResult(
        answer="a",
        citations=[],
        retrieval=RetrievalResult(query="q1"),
        model="m",
        context_chunks=[_chunk(11), _chunk(12)],
        context_chunk_ids=[11, 12],
    )
    second = AnswerResult(
        answer="b",
        citations=[],
        retrieval=RetrievalResult(query="q2"),
        model="m",
        context_chunks=[_chunk(12), _chunk(13)],
        context_chunk_ids=[12, 13],
    )
    chunks, ids = _merge_prompt_context([first, second])
    assert ids == [11, 12, 13]
    assert [chunk.chunk_id for chunk in chunks] == [11, 12, 13]


def test_merge_prompt_context_keeps_ids_when_chunks_unavailable() -> None:
    """缓存命中的子结果没有块正文，只有 id —— id 仍必须并进来（不丢「喂过哪些块」）。"""
    cached_style = AnswerResult(
        answer="c",
        citations=[],
        retrieval=RetrievalResult(query="q3"),
        model="m",
        context_chunk_ids=[13, 14],
    )
    chunks, ids = _merge_prompt_context([cached_style])
    assert ids == [13, 14]
    assert chunks == []


# --- 端到端：真实 pipeline（mock provider，离线） --------------------------------


async def _ask(runtime, question: str, **kwargs):
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    return await runtime.pipeline.answer(
        question, perm, use_cache=False, collect_debug=True, **kwargs
    )


async def test_answer_exposes_prompt_context_chunks(seeded) -> None:
    runtime, _ = seeded
    answer = await _ask(runtime, "年假 申请 审批")

    assert answer.context_chunks, "有语料命中时必须记录进 prompt 的块"
    assert answer.context_chunk_ids == [c.chunk_id for c in answer.context_chunks]
    # 上下文块是检索结果的子集（可能因预算裁剪少几块，但不会凭空多出来）
    assert set(answer.context_chunk_ids) <= {c.chunk_id for c in answer.retrieval.chunks}
    # 引用编号必须落在上下文编号范围内
    assert {c.cite_index for c in answer.citations} <= set(
        range(1, len(answer.context_chunks) + 1)
    )
    # mock LLM 只引前两块：上下文多于引用时，未引用块依然在列表里（cited=false）
    views = _context_chunk_views(answer)
    assert len(views) >= 2, f"该问句预期至少 2 块上下文，实际 {answer.context_chunk_ids}"
    cited = {c.cite_index for c in answer.citations}
    assert [v["cited"] for v in views] == [v["index"] in cited for v in views]
    if len(views) > len(cited):
        assert any(not v["cited"] for v in views), "未被引用的上下文块不得从「模型上下文」里消失"


async def test_answer_numeric_mode_also_exposes_prompt_context(seeded) -> None:
    runtime, _ = seeded
    answer = await _ask(runtime, "年假有多少天", mode="numeric")
    assert answer.context_chunk_ids, "取数模式的拒答/成功路径同样要记模型上下文"
    assert answer.context_chunk_ids == [c.chunk_id for c in answer.context_chunks]


async def test_retrieval_only_answer_context_is_evidence_list(seeded) -> None:
    runtime, _ = seeded
    answer = await _ask(runtime, "年假有多少天", allow_generation=False)
    assert answer.context_chunk_ids == [c.chunk_id for c in answer.retrieval.chunks]
    assert [c.chunk_id for c in answer.context_chunks] == answer.context_chunk_ids


async def test_answer_cache_hit_keeps_context_ids_but_not_chunk_bodies(seeded) -> None:
    """缓存只存 id：命中时块正文没了，但「喂过哪些块」不得丢。"""
    runtime, _ = seeded
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    first = await runtime.pipeline.answer("年假有多少天", perm, use_cache=True)
    second = await runtime.pipeline.answer("年假有多少天", perm, use_cache=True)

    assert first.context_chunk_ids
    assert "answer_cache_hit" not in first.degraded
    assert "answer_cache_hit" in second.degraded
    assert second.context_chunk_ids == first.context_chunk_ids
    assert second.context_chunks == [], "缓存载荷不含正文，命中也无法重建块对象"


def test_answer_from_cache_tolerates_old_payload_without_key(seeded) -> None:
    """老缓存条目没有 context_chunk_ids 键 → 空列表，不崩（滚动升级兼容）。"""
    runtime, _ = seeded
    rewrite = SimpleNamespace(rewritten="年假有多少天", used_history=False)
    old_payload = {
        "answer": "旧答案 [1]",
        "model": "mock-llm-v1",
        "refused": False,
        "citations": [_citation(1, 35280).__dict__],
    }
    result = runtime.pipeline._answer_from_cache(old_payload, rewrite, "prose", "factual")  # noqa: SLF001
    assert result.context_chunk_ids == []
    assert result.context_chunks == []

    new_payload = {**old_payload, "context_chunk_ids": [35280, 35274, 35281]}
    restored = runtime.pipeline._answer_from_cache(new_payload, rewrite, "prose", "factual")  # noqa: SLF001
    assert restored.context_chunk_ids == [35280, 35274, 35281]
    assert restored.context_chunks == []
