"""生成后置「资料充分性」核验门禁的测试。

覆盖：解析容错、HTTP 重试、默认关闭、边界分过滤、判定不支持时拒答、fail_open 降级。
"""

from __future__ import annotations

import httpx
import pytest

from ekbs.config import AnswerVerifyCfg
from ekbs.retrieval.answer_verify import AnswerVerifier, VerifyResult, parse_verdict
from ekbs.retrieval.pipeline import (
    REFUSE_ANSWER_VERIFY,
    REFUSE_ANSWER_VERIFY_UNAVAILABLE,
)
from tests.conftest import user_id_of


def test_parse_verdict_accepts_plain_and_noisy_output() -> None:
    assert parse_verdict('{"supported": true}') == {"supported": True}
    noisy = '思考：资料支持。\n```json\n{"supported": false, "reason": "无依据"}\n```'
    assert parse_verdict(noisy)["supported"] is False
    assert parse_verdict("<think>思考过程</think>{\"supported\": true}")["supported"] is True
    assert parse_verdict("结论 supported: true") == {"supported": True}
    assert parse_verdict("完全不是 JSON") is None


async def test_verifier_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": '{"supported": false, "reason": "资料不足"}'}}],
                "usage": {"total_tokens": 10},
            },
        )

    verifier = AnswerVerifier(AnswerVerifyCfg(), transport=httpx.MockTransport(handler))
    result = await verifier.verify("年假几天？", "年假 5 天 [1]。", ["年假为 5 天。"])
    assert result.supported is False
    assert result.reason == "资料不足"
    assert calls["n"] == 2


async def test_verifier_missing_key_is_fail_closed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    verifier = AnswerVerifier(AnswerVerifyCfg(), transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    result = await verifier.verify("q", "a", ["c"])
    assert result.supported is None
    assert result.error == "missing_api_key"


async def _retrieval(runtime):
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    return alice, await runtime.pipeline.retrieve("年假有多少天", alice)


async def test_gate_disabled_by_default_does_not_call_verifier(seeded) -> None:
    runtime, _ = seeded
    alice, retrieval = await _retrieval(runtime)
    calls: list[str] = []

    async def fake_verify(question: str, answer: str, contexts: list[str]) -> VerifyResult:
        calls.append(question)
        return VerifyResult(supported=False)

    runtime.pipeline.answer_verifier.verify = fake_verify  # type: ignore[method-assign]
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert calls == []
    assert answer.refused is False


async def test_gate_refuses_when_verifier_reports_unsupported(seeded) -> None:
    runtime, _ = seeded
    alice, retrieval = await _retrieval(runtime)
    cfg = runtime.pipeline.answer_verifier.cfg
    cfg.enabled = True
    cfg.min_score = 0.0
    cfg.max_score = 1.0

    async def fake_verify(question: str, answer: str, contexts: list[str]) -> VerifyResult:
        return VerifyResult(supported=False, reason="资料没有该事实", model="deepseek-flash")

    runtime.pipeline.answer_verifier.verify = fake_verify  # type: ignore[method-assign]
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert answer.refused is True
    assert answer.refusal_reason == REFUSE_ANSWER_VERIFY
    assert answer.citations == []


async def test_gate_band_filter_skips_high_confidence(seeded) -> None:
    runtime, _ = seeded
    alice, retrieval = await _retrieval(runtime)
    cfg = runtime.pipeline.answer_verifier.cfg
    cfg.enabled = True
    cfg.min_score = 0.65
    cfg.max_score = 0.85
    retrieval.top_score = 0.99  # 高于 band 上限 → 直接放行
    calls: list[str] = []

    async def fake_verify(question: str, answer: str, contexts: list[str]) -> VerifyResult:
        calls.append(question)
        return VerifyResult(supported=False)

    runtime.pipeline.answer_verifier.verify = fake_verify  # type: ignore[method-assign]
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert calls == []
    assert answer.refused is False


async def test_gate_fail_open_and_fail_closed(seeded) -> None:
    runtime, _ = seeded
    alice, retrieval = await _retrieval(runtime)
    cfg = runtime.pipeline.answer_verifier.cfg
    cfg.enabled = True
    cfg.min_score = 0.0
    cfg.max_score = 1.0

    async def fake_verify(question: str, answer: str, contexts: list[str]) -> VerifyResult:
        return VerifyResult(supported=None, error="HTTP 500")

    runtime.pipeline.answer_verifier.verify = fake_verify  # type: ignore[method-assign]

    cfg.fail_open = True
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert answer.refused is False

    cfg.fail_open = False
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert answer.refused is True
    assert answer.refusal_reason == REFUSE_ANSWER_VERIFY_UNAVAILABLE
