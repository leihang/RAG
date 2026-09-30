"""生成侧忠实度（P0）：无引用不成溯源 + 数字/量词须落在资料里。"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from ekbs.retrieval.faithfulness import (
    REFUSE_MISSING_CITATION,
    REFUSE_UNSUPPORTED_CLAIM,
    assess_faithfulness,
    extract_claims,
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
async def test_generate_refuses_unsupported_claim(seeded) -> None:
    """合法 [1] + 编造数字 → 整答 refused=unsupported_claim，citations 清空。"""
    from ekbs.provider.base import LLMResult

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            return LLMResult(
                text="市内交通补贴上限为 600 元 [1]。",
                model="faithfulness-test",
            )

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        retrieval = await runtime.pipeline.retrieve("年假有多少天", alice)
        if not retrieval.chunks:
            pytest.skip("demo 语料未召回，跳过集成断言")
        evidence = " ".join(c.content for c in retrieval.chunks)
        if "600" in evidence.replace(",", ""):
            pytest.skip("召回块碰巧含 600，换问题再测")
        answer = await runtime.pipeline.generate("补贴多少", retrieval, alice)
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]

    assert answer.refused is True
    assert answer.refusal_reason == REFUSE_UNSUPPORTED_CLAIM
    assert answer.citations == []
    assert answer.faithfulness.get("ok") is False
    assert any("600" in u for u in answer.faithfulness.get("unsupported", []))


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
