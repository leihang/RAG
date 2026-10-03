"""改写结果的**两类分离**与**稠密多查询 + RRF**（2026-10-03）。

## 背景（为什么这么切）

改写分两类：**① 利用上下文**（把"它/这个/详细说说"补成自包含查询）与
**② 理解意图**（判断用户要什么，据此选检索/生成策略）。而"变体"必须**分两侧**：

* BM25 要**词面变体**（别名/编号/关键词组合）—— 现有 `expand_variants` 是这类；
* 稠密要**语义变体**（自然语言改写）—— 把 `_keyword_focus` 那种碎片喂给嵌入模型
  语义会被拉偏（此前实测过"关键词碎片喂嵌入更差"）。

## 稠密多查询为什么用 RRF 而不是平均向量

变体越多，平均向量越糊（= 把多个语义中心折中）。正确做法：**每个变体各召回一路，
再用 RRF 融合**（复用 `rrf_fuse`，与生产融合同一套口径）。
"""

from __future__ import annotations

import pytest

from ekbs.config import load_settings
from ekbs.retrieval.pipeline import RetrievalOverrides
from ekbs.retrieval.rrf import rrf_fuse
from ekbs.session import (
    INTENT_FACTUAL,
    INTENT_FIGURE,
    INTENT_NUMERIC,
    INTENT_PROCEDURE,
    QueryRewriter,
    RewriteResult,
    _as_question,
    _keyword_focus,
    _strip_particles,
)
from tests.conftest import user_id_of


def _rewriter(settings) -> QueryRewriter:
    return QueryRewriter(settings=settings, llm=None)


# ---------------------------------------------------------------- ① 字段分离


def test_rewrite_result_keeps_legacy_variants_in_sync(settings) -> None:
    """`variants`（历史字段）必须与 `sparse_variants` 一致 —— 旧调用方不能被打断。"""
    rewritten = RewriteResult("它多少钱？", "电梯维保一年要花多少钱", "heuristic")
    filled = _rewriter(settings)._fill_variants(rewritten)
    assert filled.sparse_variants, "词面变体不能为空（BM25 用）"
    assert filled.variants == filled.sparse_variants, "兼容字段必须同步"
    assert filled.dense_variants, "语义变体不能为空（稠密用）"
    assert filled.intent == INTENT_NUMERIC


def test_dense_variants_are_semantic_not_keyword_fragments(settings) -> None:
    """语义变体只能来自"自然语言改写"那一组，**不得**混入关键词碎片。"""
    question = "请问帮我查一下，交接班记录要在哪签字？"
    rewriter = _rewriter(settings)
    dense = rewriter.expand_dense_variants(question, original=question)
    allowed = {question, _strip_particles(question), _as_question(question)}
    assert set(dense) <= allowed, f"出现了非语义改写项：{set(dense) - allowed}"
    assert _keyword_focus(question) not in dense, "关键词碎片不该进稠密变体"
    # 反证：词面变体里确实有"关键词聚焦"这种形态（两侧本就不同）
    assert _keyword_focus(question) in rewriter.expand_variants(question, original=question)


def test_dense_variants_are_deterministic_and_deduped(settings) -> None:
    """语义变体：确定性、去重、不超过 `dense_multi_query` 条。"""
    rewriter = _rewriter(settings)
    settings.retrieval.dense_multi_query = 3
    first = rewriter.expand_dense_variants("年假有多少天？")
    assert first == rewriter.expand_dense_variants("年假有多少天？"), "必须确定性（无随机/无 LLM）"
    assert len(first) == len(set(first)), "重复变体等于白花一次嵌入"
    settings.retrieval.dense_multi_query = 2
    assert len(rewriter.expand_dense_variants("年假有多少天？")) <= 2


def test_dense_variant_count_one_means_disabled(settings) -> None:
    """`dense_multi_query=1` ⇒ 只有主查询（与改动前行为一致）。"""
    rewriter = _rewriter(settings)
    settings.retrieval.dense_multi_query = 1
    assert rewriter.expand_dense_variants("年假有多少天？") == ["年假有多少天？"]
    settings.retrieval.dense_multi_query = 3
    assert len(rewriter.expand_dense_variants("请问年假有多少天？")) > 1


# ---------------------------------------------------------------- ② 意图


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("中央空调合同的总价是多少钱", INTENT_NUMERIC),
        ("电费一共花了多少", INTENT_NUMERIC),
        ("有没有现场照片", INTENT_FIGURE),
        ("交接班要怎么处理接班人迟到", INTENT_PROCEDURE),
        ("公司名称是什么", INTENT_FACTUAL),
    ],
)
def test_intent_detection_is_conservative(settings, question: str, intent: str) -> None:
    """意图识别**保守**：只认明确线索，其余一律 factual（误判比不判更糟）。"""
    assert _rewriter(settings).detect_intent(question) == intent


def test_intent_and_variants_set_on_every_path(settings) -> None:
    """每条分支都要带上 intent/两个变体字段（否则下游拿到空字段会静默退化）。"""
    filled = _rewriter(settings)._fill_variants(
        RewriteResult("交接班制度说了什么", "交接班制度说了什么", "off")
    )
    assert filled.intent == INTENT_FACTUAL
    assert filled.sparse_variants and filled.dense_variants
    assert filled.variants == filled.sparse_variants


# ---------------------------------------------------------------- ③ 接线


async def test_dense_recall_uses_one_lane_when_disabled(seeded, monkeypatch) -> None:
    """`dense_multi_query=1` ⇒ 与旧行为一致：只嵌 1 次、只召 1 路。"""
    runtime, _ = seeded
    calls = {"embed": 0, "recall": 0}
    original_embed = runtime.pipeline.query_vector
    original_recall = runtime.pipeline._dense_recall_scored

    async def counting_embed(q):
        calls["embed"] += 1
        return await original_embed(q)

    def counting_recall(vector, perm, top_k):
        calls["recall"] += 1
        return original_recall(vector, perm, top_k)

    monkeypatch.setattr(runtime.pipeline, "query_vector", counting_embed)
    monkeypatch.setattr(runtime.pipeline, "_dense_recall_scored", counting_recall)
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    await runtime.pipeline.retrieve(
        "年假有多少天", alice, overrides=RetrievalOverrides(dense_multi_query=1)
    )
    assert calls == {"embed": 1, "recall": 1}, calls


async def test_dense_recall_uses_multiple_lanes_and_fuses(seeded, monkeypatch) -> None:
    """开启后：每个语义变体各嵌一次、各召一路，结果用 RRF 融合。"""
    runtime, _ = seeded
    calls = {"variants": [], "recall": 0}
    original_embed = runtime.pipeline.query_vector
    original_recall = runtime.pipeline._dense_recall_scored

    async def counting_embed(q):
        calls["variants"].append(q)
        return await original_embed(q)

    def counting_recall(vector, perm, top_k):
        calls["recall"] += 1
        return original_recall(vector, perm, top_k)

    monkeypatch.setattr(runtime.pipeline, "query_vector", counting_embed)
    monkeypatch.setattr(runtime.pipeline, "_dense_recall_scored", counting_recall)
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve(
        "请问年假有多少天", alice, overrides=RetrievalOverrides(dense_multi_query=3)
    )
    assert calls["recall"] >= 2, f"应有多路稠密召回，实际 {calls['recall']}"
    assert len(calls["variants"]) == calls["recall"], "每个变体各嵌一次"
    assert result.chunks, "融合后仍要有结果"


def test_retrieval_cache_revision_tracks_dense_multi_query(seeded) -> None:
    """缓存版本必须随 `dense_multi_query` 变化 —— 否则改配置后旧答案继续被服务，A/B 会被污染。"""
    runtime, _ = seeded
    before = runtime.pipeline.retrieval_cache_revision()
    original = runtime.settings.retrieval.dense_multi_query
    try:
        runtime.settings.retrieval.dense_multi_query = int(original) + 1
        assert runtime.pipeline.retrieval_cache_revision() != before
    finally:
        runtime.settings.retrieval.dense_multi_query = original


def test_shipped_configs_gate_dense_multi_query() -> None:
    """发布策略：本机开启以便 A/B，离线配置与生产模板保持关闭。"""
    assert load_settings("config/application.cloud.yaml").retrieval.dense_multi_query == 3
    assert load_settings("config/application.dev.yaml").retrieval.dense_multi_query == 1
    assert load_settings("deploy/config/application.yaml").retrieval.dense_multi_query == 1


def test_rrf_fusion_is_rank_based() -> None:
    """RRF 只看排名：多路都靠前的块胜出；只在单路且靠后的块垫底。"""
    fused = rrf_fuse([[1, 2, 3], [2, 1, 4]], k=60)
    order = [chunk_id for chunk_id, _ in fused]
    assert order[:2] == [1, 2], order
    assert order[-1] == 4, order


def test_explicit_limit_overrides_process_config(settings) -> None:
    """**显式 limit 优先于进程级配置**（否则调试台的按请求调参会被静默吞掉）。"""
    rewriter = _rewriter(settings)
    settings.retrieval.dense_multi_query = 1
    settings.retrieval.multi_query = 1
    assert len(rewriter.expand_dense_variants("请问年假有多少天？", limit=3)) > 1
    assert len(rewriter.expand_variants("请问年假有多少天？", limit=3)) > 1
    assert len(rewriter.expand_dense_variants("请问年假有多少天？", limit=1)) == 1
