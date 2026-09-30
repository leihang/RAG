"""多轮会话上下文与查询改写（`00` §2.1、`04` §5.5、`01` §5.5）。

最要紧的两条：

* **群聊上下文按用户隔离** —— ``conversation_id`` 是共享的，没有用户维度就会串台；
* **答案缓存必须按历史分区** —— 同一个问题在不同上下文下答案不同，否则多轮里会复用上一轮的答案。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.provider.mock import MockLLMProvider
from ekbs.session import QueryRewriter, SessionStore, _needs_context
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


@pytest.fixture
def sessions(runtime) -> SessionStore:
    return SessionStore(runtime.db, runtime.settings)


def test_append_and_load_round_trip(sessions: SessionStore) -> None:
    sessions.append("c1", "u1", [{"role": "user", "content": "年假多少天"}])
    sessions.append("c1", "u1", [{"role": "assistant", "content": "5 天 [1]"}])
    history = sessions.load("c1", "u1")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "年假多少天"


def test_group_context_is_isolated_per_user(sessions: SessionStore) -> None:
    """群聊里 A 的上下文不能出现在 B 的历史里（04 §5.5）。"""
    sessions.append("group-1", "alice", [{"role": "user", "content": "A 的问题"}])
    sessions.append("group-1", "bob", [{"role": "user", "content": "B 的问题"}])
    assert [m["content"] for m in sessions.load("group-1", "alice")] == ["A 的问题"]
    assert [m["content"] for m in sessions.load("group-1", "bob")] == ["B 的问题"]
    assert sessions.load("group-1", "carol") == []


def test_max_turns_truncates_history(runtime, sessions: SessionStore) -> None:
    runtime.settings.session["context_max_turns"] = 1
    for index in range(4):
        sessions.append("c1", "u1", [{"role": "user", "content": f"问题{index}"}])
    history = sessions.load("c1", "u1")
    assert len(history) <= 2, history
    assert history[-1]["content"] == "问题3"


def test_ttl_expiry_returns_empty(runtime, sessions: SessionStore) -> None:
    runtime.settings.session["context_ttl_seconds"] = 60
    sessions.append("c1", "u1", [{"role": "user", "content": "旧问题"}])
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_session_ctx SET updated_at = datetime('now', '-2 hours')"
        )
        conn.commit()
    assert sessions.load("c1", "u1") == []
    assert sessions.cleanup_expired() == 1


def test_clear_removes_only_that_user(runtime, sessions: SessionStore) -> None:
    sessions.append("c1", "u1", [{"role": "user", "content": "x"}])
    sessions.append("c1", "u2", [{"role": "user", "content": "y"}])
    assert sessions.clear("c1", "u1") == 1
    assert sessions.load("c1", "u1") == []
    assert sessions.load("c1", "u2") != []


# ---------------------------------------------------------------------------
# 查询改写
# ---------------------------------------------------------------------------


def test_anaphora_detection() -> None:
    assert _needs_context("那它需要提前几天申请")
    assert _needs_context("这个怎么算")
    assert _needs_context("年假")  # 过短，脱离上下文无法检索
    assert not _needs_context("员工年假天数如何计算")


async def test_rewriter_without_history_is_noop(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    result = await rewriter.rewrite("年假有多少天", [])
    assert result.rewritten == "年假有多少天"
    assert result.used_history is False


async def test_rewriter_merges_previous_question_on_anaphora(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "年假有多少天"},
        {"role": "assistant", "content": "5 天 [1]"},
    ]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.strategy == "heuristic"
    assert result.used_history is True
    assert result.rewritten.startswith("年假有多少天")
    assert "提前几天申请" in result.rewritten


async def test_rewriter_keeps_self_contained_question(runtime) -> None:
    """语义已经明确的问题不去污染它（并入历史会稀释查询向量）。"""
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("财务专用章由谁保管", history)
    assert result.rewritten == "财务专用章由谁保管"
    assert result.used_history is False


async def test_rewriter_strategy_off(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings, strategy_override="off")
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("那它呢", history)
    assert result.strategy == "off" and result.rewritten == "那它呢"


async def test_rewriter_llm_strategy_skips_mock_provider(runtime) -> None:
    """mock 模型会返回固定串，拿它当改写器只会污染查询 —— 因此自动退回启发式。"""
    mock = MockLLMProvider()
    rewriter = QueryRewriter(settings=runtime.settings, llm=mock, strategy_override="llm")
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.strategy == "heuristic"
    assert mock.calls == [], "不应把 mock 模型当改写器调用"


async def test_rewriter_llm_strategy_uses_real_provider(runtime) -> None:
    class FakeLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        async def generate(self, messages, **kwargs):
            from ekbs.provider.base import LLMResult

            return LLMResult(text="年假申请需要提前几天", model="qwen-max")

    rewriter = QueryRewriter(settings=runtime.settings, llm=FakeLLM(), strategy_override="llm")
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.strategy == "llm"
    assert result.rewritten == "年假申请需要提前几天"


# ---------------------------------------------------------------------------
# 链路与接口
# ---------------------------------------------------------------------------


async def test_pipeline_multi_turn_records_rewrite_and_context(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    first = await runtime.pipeline.answer(
        "年假有多少天", alice, conversation_id="conv-1", use_cache=False
    )
    assert first.refused is False and first.rewritten_query == "年假有多少天"

    second = await runtime.pipeline.answer(
        "那它需要提前几天申请", alice, conversation_id="conv-1", use_cache=False
    )
    assert second.history_used is True
    assert second.rewritten_query.startswith("年假有多少天")

    # 上下文已落库，且键含用户维度
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT dingtalk_userid, turn, payload FROM ekbs_session_ctx "
            "WHERE conversation_id = 'conv-1'"
        ).fetchone()
        assert row["dingtalk_userid"] == "demo_alice"
        assert int(row["turn"]) >= 2
        # 说明：审计由 API 层写（ekbs/api/qa.py），这里直接调 pipeline 不会落审计 —— 见下一个用例


async def test_answer_cache_is_partitioned_by_history(seeded) -> None:
    """同一问题：**无历史** 与 **有历史** 必须落到不同的缓存键。

    （注意：两个都还没历史的会话共用缓存是**正确**的 —— 那种情况下答案确实相同，
    所以这里用「同一会话的第一轮 vs 第二轮」来构造真正的上下文差异。）
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    await runtime.pipeline.answer("年假有多少天", alice, conversation_id="c-a")
    await runtime.pipeline.answer("年假有多少天", alice, conversation_id="c-a")
    with runtime.db.sync_connect() as conn:
        keys = [
            str(r["cache_key"])
            for r in conn.execute(
                "SELECT cache_key FROM ekbs_cache WHERE cache_key LIKE 'ekbs:cache:answer:%'"
            ).fetchall()
        ]
    assert len(keys) == 2, f"有/无历史必须是不同的缓存键：{keys}"
    assert all(alice.fingerprint in key for key in keys)


async def test_api_multi_turn_and_session_clear(seeded) -> None:
    runtime, _ = seeded
    app = create_app(runtime.settings)
    alice_id = user_id_of(runtime.db, "demo_alice")
    headers = {
        "X-Internal-Token": TEST_INTERNAL_TOKEN,
        "X-Internal-User-Id": str(alice_id),
    }
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.post(
                "/api/v1/qa/chat",
                json={"question": "年假有多少天", "conversation_id": "conv-api"},
                headers=headers,
            )
            assert first.status_code == 200
            assert first.json()["rewritten_query"] == "年假有多少天"

            second = await client.post(
                "/api/v1/qa/chat",
                json={"question": "那它需要提前几天申请", "conversation_id": "conv-api"},
                headers=headers,
            )
            body = second.json()
            assert body["history_used"] is True
            assert body["rewritten_query"].startswith("年假有多少天")

            # 审计里同时留下原问题与改写后的查询（01 §5.4 的 rewritten_query）
            with runtime.db.sync_connect() as conn:
                audit = conn.execute(
                    "SELECT question, rewritten_query FROM ekbs_qa_message "
                    "WHERE rewritten_query != '' ORDER BY message_id DESC LIMIT 1"
                ).fetchone()
            assert audit is not None, "审计必须记录改写后的查询"
            assert audit["question"] == "那它需要提前几天申请"
            assert audit["rewritten_query"].startswith("年假有多少天")

            cleared = await client.post(
                "/api/v1/qa/session/clear", params={"conversation_id": "conv-api"},
                headers=headers,
            )
            assert cleared.status_code == 200
            assert cleared.json()["removed"] >= 1


async def test_same_conversation_reuses_one_audit_session(seeded) -> None:
    """同一 conversation_id 的两轮问答必须落在**同一个** qa_session 里，turn 递增。

    回归测试：早先把 conversation_id 当 session_id 查，导致每轮新建会话、turn 恒为 1，
    「按会话完整回溯」失效（00 §4 要求审计完备率 100%）。
    """
    runtime, _ = seeded
    app = create_app(runtime.settings)
    alice_id = user_id_of(runtime.db, "demo_alice")
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(alice_id)}
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.post(
                "/api/v1/qa/chat",
                json={"question": "年假有多少天", "conversation_id": "conv-session"},
                headers=headers,
            )
            second = await client.post(
                "/api/v1/qa/chat",
                json={"question": "那它需要提前几天申请", "conversation_id": "conv-session"},
                headers=headers,
            )
            assert first.json()["session_id"] == second.json()["session_id"]
            assert (first.json()["turn"], second.json()["turn"]) == (1, 2)

    with runtime.db.sync_connect() as conn:
        sessions = conn.execute(
            "SELECT session_id, message_count, conversation_id FROM ekbs_qa_session "
            "WHERE conversation_id = 'conv-session'"
        ).fetchall()
        turns = [
            int(r["turn"])
            for r in conn.execute(
                "SELECT turn FROM ekbs_qa_message WHERE session_id = ? ORDER BY message_id",
                (sessions[0]["session_id"],),
            ).fetchall()
        ]
    assert len(sessions) == 1, "同一会话只能有一个 qa_session"
    assert int(sessions[0]["message_count"]) == 4  # 两轮 × (user+assistant)
    assert turns == [1, 2]
