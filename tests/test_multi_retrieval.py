"""多意图检索（P0+P1+P2）集成与单元测试。

覆盖：
- 单问零回归：不拆分、sub_questions 为空；
- 多子问：拆分 → 每子问独立召回 → 合并，带 sub_questions / coverage；
- 合并配额：每子问保底、上限截断。
"""

from __future__ import annotations

from ekbs.retrieval.pipeline import RetrievalResult, RetrievedChunk
from tests.conftest import user_id_of


def _chunk(chunk_id: int, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, doc_id=1, shard_id="kb_test_L1",
        content=f"内容{chunk_id}", rerank_score=score,
    )


async def test_single_question_keeps_original_path(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.answer("年假有多少天", alice, use_cache=False)
    assert result.sub_questions == []
    assert result.coverage == {}
    assert result.rewritten_query == "年假有多少天"


async def test_multi_question_splits_and_reports_coverage(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.answer(
        "年假有多少天？财务专用章由谁保管？", alice, use_cache=False,
    )
    assert len(result.sub_questions) == 2
    assert {q["text"] for q in result.sub_questions} == {"年假有多少天", "财务专用章由谁保管"}
    assert all(q["source"] == "rules" for q in result.sub_questions)
    assert " ; " in result.rewritten_query
    assert result.coverage.get("total") == 2
    assert result.coverage.get("covered", 0) >= 1
    # 多子问最终 keep 不超过配置上限
    assert len(result.retrieval.chunks) <= runtime.settings.retrieval.rerank.keep_cap


def test_merge_retrievals_keeps_per_question_quota(runtime) -> None:
    pipeline = runtime.pipeline
    first = RetrievalResult(query="q0", chunks=[_chunk(1, 0.9), _chunk(2, 0.8)])
    second = RetrievalResult(query="q1", chunks=[_chunk(3, 0.1), _chunk(4, 0.05)])

    merged, hit_by = pipeline._merge_retrievals([(0, first), (1, second)])
    ids = {c.chunk_id for c in merged.chunks}
    # 第二子问分很低，但仍应有保底 1 块（这正是修「偏科」的关键）
    assert 3 in ids or 4 in ids
    assert 1 in ids
    assert hit_by[1] == {0}
    assert hit_by[3] == {1}
    assert merged.refused is False


def test_merge_retrievals_respects_cap(runtime) -> None:
    pipeline = runtime.pipeline
    cap = int(runtime.settings.retrieval.rerank.keep_cap)
    per_sub = [
        (i, RetrievalResult(query=f"q{i}", chunks=[_chunk(i * 10 + j, 1.0 - j * 0.01) for j in range(10)]))
        for i in range(3)
    ]
    merged, _ = pipeline._merge_retrievals(per_sub)
    assert len(merged.chunks) <= cap
    assert len(merged.chunks) > 0


def test_merge_retrievals_all_empty_is_refused(runtime) -> None:
    pipeline = runtime.pipeline
    merged, hit_by = pipeline._merge_retrievals(
        [(0, RetrievalResult(query="q0", refused=True, refusal_reason="below_threshold"))]
    )
    assert merged.chunks == []
    assert merged.refused is True
    assert hit_by == {}


def test_merge_retrievals_invariants_random(runtime) -> None:
    """随机不变量：不重复、不超 cap、有块的子问在未触顶时必须被覆盖。

    这类「合并/配额」逻辑最容易在边界出错，用随机输入把不变量钉住，
    比手写几个样例更能防逻辑错误。
    """
    import random

    pipeline = runtime.pipeline
    cap = int(runtime.settings.retrieval.rerank.keep_cap)
    random.seed(20261028)

    for _ in range(80):
        n_sub = random.randint(2, 5)
        per_sub = []
        for i in range(n_sub):
            size = random.randint(0, 8)
            chunks = [_chunk(i * 100 + j, round(random.random(), 4)) for j in range(size)]
            per_sub.append((i, RetrievalResult(query=f"q{i}", chunks=chunks)))

        merged, hit_by = pipeline._merge_retrievals(per_sub)
        ids = [c.chunk_id for c in merged.chunks]

        assert len(ids) == len(set(ids)), "合并后不得出现重复 chunk"
        assert len(ids) <= cap, "合并块数不得超过 keep_cap"
        assert merged.refused is (len(ids) == 0)

        if len(ids) < cap:
            for sub_index, result in per_sub:
                if not result.chunks:
                    continue
                assert any(sub_index in hit_by.get(cid, set()) for cid in ids), (
                    f"子问 {sub_index} 有命中却被整段丢弃"
                )
