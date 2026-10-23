"""生成侧忠实度（P0）：无引用不成溯源 + 数字/量词须落在资料里。"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from ekbs.retrieval.faithfulness import (
    DEGRADED_UNSUPPORTED,
    REFUSE_MISSING_CITATION,
    REFUSE_UNSUPPORTED_CLAIM,
    ClaimCheck,
    FaithfulnessReport,
    assess_faithfulness,
    claim_supported,
    clause_mismatches,
    extract_claims,
    mostly_unsupported,
)
from ekbs.retrieval.pipeline import RetrievalPipeline
from tests.conftest import user_id_of


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _chunk(content: str, *, title: str = "员工手册", heading: str = "年假") -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        doc_title=title,
        heading_path=heading,
        chunk_id=1,
        doc_id=1,
    )


def test_extract_claims_skips_lone_digits_without_unit() -> None:
    claims = {_norm(c) for c in extract_claims("年假为 5 天。")}
    assert "5天" in claims
    claims2 = {_norm(c) for c in extract_claims("标准 600 元。")}
    assert any(c.startswith("600") for c in claims2)
    # 裸「5」不抽，避免把引用旁噪声当 claim
    assert extract_claims("见条款 5。") == []


def test_missing_citation_is_not_ok() -> None:
    chunks = [_chunk("员工年假为 5 天。")]
    report = assess_faithfulness("年假为 5 天。", chunks)
    assert report.ok is False
    assert report.reason == REFUSE_MISSING_CITATION
    assert report.cited_indices == []


def test_supported_claim_with_citation_passes() -> None:
    chunks = [_chunk("员工年假为 5 天，满一年起计。")]
    report = assess_faithfulness("年假为 5 天 [1]。", chunks)
    assert report.ok is True
    assert report.reason == ""
    assert report.unsupported == []
    assert 1 in report.cited_indices


def test_fabricated_amount_is_unsupported() -> None:
    """资料无「600 元」→ 答案写出须被标 unsupported。"""
    chunks = [_chunk("市内交通补贴按实报销，上限另定。")]
    report = assess_faithfulness("市内交通补贴上限为 600 元 [1]。", chunks)
    assert report.ok is False
    assert report.reason == REFUSE_UNSUPPORTED_CLAIM
    assert any("600" in u for u in report.unsupported)


def test_post_check_citations_empty_without_markers() -> None:
    """P0：答案无 [n] 时不得挂全量候选。"""
    chunks = [
        SimpleNamespace(
            chunk_id=10,
            doc_id=1,
            doc_title="手册",
            doc_version="v1",
            heading_path="年假",
            content="年假 5 天",
            page_from=1,
            page_to=1,
            rerank_score=0.9,
            figure_id=None,
            image_path=None,
        )
    ]

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

        def record_security_event(self, *args, **kwargs):
            return None

    pipe = object.__new__(RetrievalPipeline)
    pipe.gate = _Gate()  # type: ignore[attr-defined]
    assert pipe._post_check_citations("年假为 5 天。", chunks, perm=None) == []  # type: ignore[arg-type]
    cited = pipe._post_check_citations("年假为 5 天 [1]。", chunks, perm=None)  # type: ignore[arg-type]
    assert len(cited) == 1
    assert cited[0].cite_index == 1


@pytest.mark.asyncio
async def _stub_generate(runtime, alice, *, question: str, text: str, retrieve_q: str):
    """用假 LLM 跑一次 generate（不调云端），返回 (answer, retrieval)。"""
    from ekbs.provider.base import LLMResult

    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            return LLMResult(text=text, model="faithfulness-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        retrieval = await runtime.pipeline.retrieve(retrieve_q, alice)
        if not retrieval.chunks:
            pytest.skip("demo 语料未召回，跳过集成断言")
        return await runtime.pipeline.generate(question, retrieval, alice), retrieval
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_generate_degrades_low_ratio_unsupported_claim(seeded) -> None:
    """**降级而非拒答**：少量数字核对不上时，答案与引用都要保留。

    实测教训：`2026年7月能耗分析` 这类表格汇总问题，模型会做单位换算/格式化，
    旧策略"一条对不上就整答拒答 + 清空引用"会把好答案打成拒答（25 条 claim 误杀 9 条）。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    answer, retrieval = await _stub_generate(
        runtime, alice, question="补贴多少", text="市内交通补贴上限为 888 元 [1]。",
        retrieve_q="年假有多少天",
    )
    evidence = " ".join(c.content for c in retrieval.chunks).replace(",", "")
    if "888" in evidence:
        pytest.skip("召回块碰巧含 888，换数字再测")

    assert answer.refused is False, "少量存疑数字不应整答拒答"
    assert answer.citations, "降级时引用必须保留 —— 否则用户看不到出处"
    assert answer.refusal_reason == ""
    assert any(d.startswith(DEGRADED_UNSUPPORTED) for d in answer.degraded), answer.degraded
    # 报告仍然如实记录（审计/调试台可查）
    assert answer.faithfulness.get("ok") is False
    assert any("888" in u for u in answer.faithfulness.get("unsupported", []))


@pytest.mark.asyncio
async def test_generate_refuses_mostly_unsupported_claim(seeded) -> None:
    """降级**不等于放行**：整篇数字都对不上时仍然硬拒（大概是编的）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    answer, retrieval = await _stub_generate(
        runtime, alice,
        question="补贴多少",
        text="上限为 888 元 [1]，下限为 777 元 [1]，另计 666 元 [1]。",
        retrieve_q="年假有多少天",
    )
    evidence = " ".join(c.content for c in retrieval.chunks).replace(",", "")
    if sum(1 for n in ("888", "777", "666") if n in evidence) >= 2:
        pytest.skip("召回块碰巧含多数数字，换数字再测")

    assert answer.refused is True
    assert answer.refusal_reason == REFUSE_UNSUPPORTED_CLAIM
    assert answer.citations == []


def test_unit_and_format_differences_are_tolerated() -> None:
    """单位/格式化差异**不算编造**（实测：答案 474.004万元 vs 单元格 474.004）。"""
    evidence = "表名: 用电分析 合计=474.004 同比=0.0769 总量=5,960,922"
    assert claim_supported("474.004万元", evidence) is True
    assert claim_supported("7.69%", evidence) is True
    assert claim_supported("5,960,922度", evidence) is True
    assert claim_supported("7.69%", "同比=7.69") is True


def test_short_numbers_still_require_real_support() -> None:
    """1~2 位整数不参与「数字主体」匹配，避免把校验放空（28 在长文里几乎必中）。"""
    assert claim_supported("28万元", "合计=474.004") is False
    assert claim_supported("999.999万元", "合计=474.004") is False


def test_mostly_unsupported_threshold() -> None:
    """比例阈值：≥3 条 claim 且 ≥60% 对不上才判「整篇不可信」。"""
    def _report(unsupported: int, total: int) -> FaithfulnessReport:
        claims = [
            ClaimCheck(text=f"c{i}", supported=i >= unsupported) for i in range(total)
        ]
        return FaithfulnessReport(
            ok=False,
            claims=claims,
            unsupported=[c.text for c in claims if not c.supported],
            reason=REFUSE_UNSUPPORTED_CLAIM,
        )

    assert mostly_unsupported(_report(1, 3)) is False   # 1/3：降级
    assert mostly_unsupported(_report(2, 3)) is True    # 2/3：硬拒
    assert mostly_unsupported(_report(1, 2)) is False   # claim 数不足下限 → 不判整篇


@pytest.mark.asyncio
async def test_generate_refuses_missing_citation(seeded) -> None:
    """有实质答案但无 [n] → refused=missing_citation，citations 为空。"""
    from ekbs.provider.base import LLMResult

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            return LLMResult(text="年假为 5 天。", model="faithfulness-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        retrieval = await runtime.pipeline.retrieve("年假有多少天", alice)
        if not retrieval.chunks:
            pytest.skip("demo 语料未召回，跳过集成断言")
        answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]

    assert answer.refused is True
    assert answer.refusal_reason == REFUSE_MISSING_CITATION
    assert answer.citations == []


def test_clause_number_must_match_cited_heading() -> None:
    chunks = [
        _chunk("提前三个工作日。", heading="年假制度 > 第三条 申请"),
        _chunk("满一年五天。", heading="年假制度 > 第一条 天数"),
    ]
    assert clause_mismatches("依据第三条，须提前申请 [1]。", chunks) == []
    assert clause_mismatches("依据第3条，须提前申请 [1]。", chunks) == []
    assert clause_mismatches("依据第五条，须提前申请 [1]。", chunks) == ["第5条"]
    assert clause_mismatches("年假为 5 天 [1]。", chunks) == []
