"""多问题逐子问作答 + 输出截断提示（2026-10-31 的 P0 补强）。

两条契约：
* 一个提问里含多个问题 → **每个子问单独生成**，不共享 2048 输出上限；
* 输出撞到上限（``finish_reason=length``）→ 答案尾附截断提示，不能伪装成完整答案。
"""

from __future__ import annotations

from ekbs.provider.base import LLMResult
from ekbs.retrieval.pipeline import TRUNCATION_NOTICE
from tests.conftest import user_id_of


async def test_multi_intent_answers_each_sub_question(seeded) -> None:
    runtime, _summary = seeded
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    result = await runtime.pipeline.answer(
        "年假有多少天？年假要提前几天申请？", perm, use_cache=False
    )

    assert len(result.sub_questions) == 2
    assert result.refused is False
    # 每个子问都出现在答案里（逐子问生成 + 拼接）
    for sub in result.sub_questions:
        assert sub["text"] in result.answer


async def test_output_truncation_appends_notice(seeded, monkeypatch) -> None:
    runtime, _summary = seeded
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    async def _fake_generate(messages, **kwargs):
        return LLMResult(
            text="根据资料 [1]，年假按规定执行。",
            model="fake-llm",
            finish_reason="length",
        )

    monkeypatch.setattr(runtime.providers.llm, "generate", _fake_generate)

    result = await runtime.pipeline.answer("年假有多少天", perm, use_cache=False)

    assert result.truncated is True
    assert result.answer.rstrip().endswith(TRUNCATION_NOTICE)
    assert "output_truncated" in result.degraded


async def test_finish_reason_stop_has_no_notice(seeded, monkeypatch) -> None:
    runtime, _summary = seeded
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    async def _fake_generate(messages, **kwargs):
        return LLMResult(
            text="根据资料 [1]，年假按规定执行。",
            model="fake-llm",
            finish_reason="stop",
        )

    monkeypatch.setattr(runtime.providers.llm, "generate", _fake_generate)

    result = await runtime.pipeline.answer("年假有多少天", perm, use_cache=False)

    assert result.truncated is False
    assert TRUNCATION_NOTICE not in result.answer
