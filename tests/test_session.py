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
    assert _needs_context("他们需要提前申请")
    assert _needs_context("年假")  # 过短，又不是人名或编号
    assert _needs_context("能介绍详细点吗？")  # 追问展开，无指代也要改写
    assert _needs_context("再说详细一点")
    assert not _needs_context("员工年假天数如何计算")
    assert not _needs_context("财务专用章由谁保管")
    # 整词：「其他」不是「他」，「应该」不是「该」
    assert not _needs_context("其他城市不超过400元")
    assert not _needs_context("应该提前几天")
    assert not _needs_context("其它部门怎么办")
    # 人名、编号本身完整，不因过短去补上一轮
    assert not _needs_context("雷航")
    assert not _needs_context("何刚")
    assert not _needs_context("雷航是谁")
    assert not _needs_context("118")
    assert not _needs_context("V4")
    assert not _needs_context("GB/T 50300")
    assert not _needs_context("制度〔2024〕15号")


async def test_rewriter_keeps_name_and_does_not_merge_false_anaphora(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "熙物联使用教程V4"},
        {"role": "assistant", "content": "见教程第 1 页 [1]"},
    ]
    name = await rewriter.rewrite("雷航", history)
    assert name.rewritten == "雷航"
    assert name.used_history is False
    plain = await rewriter.rewrite("其他城市不超过400元", history)
    assert plain.rewritten == "其他城市不超过400元"
    assert plain.used_history is False


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


async def test_rewriter_merges_elaboration_followup(runtime) -> None:
    """「能介绍详细点吗？」无指代词，但必须并入上一轮主题（用户实测）。"""
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "崔超是谁"},
        {"role": "assistant", "content": "崔超是…… [1]"},
    ]
    result = await rewriter.rewrite("能介绍详细点吗？", history)
    assert result.used_history is True
    assert result.rewritten.startswith("崔超是谁")
    assert "详细" in result.rewritten


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
    """mock 不可用时不回退 heuristic，直接原文进检索。"""
    mock = MockLLMProvider()
    rewriter = QueryRewriter(settings=runtime.settings, llm=mock, strategy_override="llm")
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.strategy == "llm_skipped"
    assert result.rewritten == "那它需要提前几天申请"
    assert mock.calls == [], "不应把 mock 模型当改写器调用"


async def test_rewriter_llm_failed_keeps_original(runtime) -> None:
    """模型改写失败 → 原文进检索，不拼上一轮。"""

    class BoomLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        async def generate(self, messages, **kwargs):
            raise RuntimeError("boom")

    rewriter = QueryRewriter(
        settings=runtime.settings, llm=BoomLLM(), strategy_override="llm"
    )
    history = [{"role": "user", "content": "新东方1-3月用电量"}]
    result = await rewriter.rewrite("提供一下2026年1-3月的", history)
    assert result.strategy == "llm_failed"
    assert result.rewritten == "提供一下2026年1-3月的"
    assert result.used_history is False


async def test_rewriter_llm_strategy_uses_real_provider(runtime) -> None:
    class FakeLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        def __init__(self) -> None:
            self.messages: list[list[dict[str, str]]] = []

        async def generate(self, messages, **kwargs):
            from ekbs.provider.base import LLMResult

            self.messages.append([dict(m) for m in messages])
            return LLMResult(text="年假申请需要提前几天", model="qwen-max")

    llm = FakeLLM()
    rewriter = QueryRewriter(settings=runtime.settings, llm=llm, strategy_override="llm")
    history = [{"role": "user", "content": "年假有多少天"}]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.strategy == "llm"
    assert result.rewritten == "年假申请需要提前几天"
    assert len(llm.messages[0]) == 2
    assert llm.messages[0][0]["role"] == "system"
    assert llm.messages[0][1]["role"] == "user"
    system = llm.messages[0][0]["content"]
    user = llm.messages[0][1]["content"]
    # system：规则 + 示例；user：仅历史 + 最新问题
    assert "【通用规则】" in system
    assert system.index("【通用规则】") < system.index("【示例】")
    assert system.index("【示例】") < system.index("【改写后的查询词】")
    assert "【改写】" in system
    assert "【扩写】" in system
    assert "【改扩写】" in system
    assert "青禾店2025年用水量" in system
    assert "新东方" not in system
    assert "桔子酒店" not in system
    assert "只输出改写后的查询词" in system
    assert "改写后的查询词：年假申请需要提前几天" in system
    assert "对话历史（最近一轮）" in user
    assert "最新问题：那它需要提前几天申请" in user
    assert "【通用规则】" not in user


async def test_rewriter_llm_history_window_is_last_round_only(runtime) -> None:
    """改写 user 消息只拼最近 1 轮（最多 2 条）；更早轮次不得进入。"""

    class FakeLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        def __init__(self) -> None:
            self.messages: list[list[dict[str, str]]] = []

        async def generate(self, messages, **kwargs):
            from ekbs.provider.base import LLMResult

            self.messages.append([dict(m) for m in messages])
            return LLMResult(text="熙物联V5使用教程", model="qwen-max")

    llm = FakeLLM()
    rewriter = QueryRewriter(settings=runtime.settings, llm=llm, strategy_override="llm")
    # 用不会出现在示例里的独特内容，避免与 prompt 模板示例撞车
    history = [
        {"role": "user", "content": "崔超是谁"},
        {"role": "assistant", "content": "崔超简介 [1]"},
        {"role": "user", "content": "雷航负责什么"},
        {"role": "assistant", "content": "产品侧 [1]"},
        {"role": "user", "content": "熙物联使用教程V4"},
        {"role": "assistant", "content": "见教程第 1 页 [1]"},
    ]
    result = await rewriter.rewrite("V5版本呢", history)
    assert result.strategy == "llm"
    assert result.rewritten == "熙物联V5使用教程"
    user = llm.messages[0][1]["content"]
    after_history = user.split("对话历史（最近一轮）：", 1)[1]
    history_section = after_history.split("最新问题：", 1)[0]
    assert "熙物联使用教程V4" in history_section
    assert "见教程第 1 页" in history_section
    assert "崔超是谁" not in history_section
    assert "雷航负责什么" not in history_section
    assert "最新问题：V5版本呢" in user


async def test_rewriter_llm_guard_rejects_foreign_entity(runtime) -> None:
    """改写若抄进历史∪原话没有的主体，拒绝并回退（防 few-shot 污染）。"""

    class FakeLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        async def generate(self, messages, **kwargs):
            from ekbs.provider.base import LLMResult

            return LLMResult(text="新东方2025年1-3月用电量", model="qwen-max")

    rewriter = QueryRewriter(
        settings=runtime.settings, llm=FakeLLM(), strategy_override="llm"
    )
    history = [
        {"role": "user", "content": "桔子酒店2024年12月用水量"},
        {"role": "assistant", "content": "桔子酒店2024年12月用水量为108 m³[2]。"},
    ]
    result = await rewriter.rewrite("2025年啦", history)
    assert result.strategy in {"llm_rejected", "llm_guarded"}
    assert "新东方" not in result.rewritten


async def test_rewriter_llm_guard_accepts_history_entity(runtime) -> None:
    class FakeLLM:
        class _Ref:
            provider = "dashscope"
            model = "qwen-max"

        ref = _Ref()

        async def generate(self, messages, **kwargs):
            from ekbs.provider.base import LLMResult

            return LLMResult(text="桔子酒店2025年用水量", model="qwen-max")

    rewriter = QueryRewriter(
        settings=runtime.settings, llm=FakeLLM(), strategy_override="llm"
    )
    history = [
        {"role": "user", "content": "桔子酒店2024年12月用水量"},
        {"role": "assistant", "content": "108"},
    ]
    result = await rewriter.rewrite("2025年啦", history)
    assert result.strategy == "llm"
    assert result.rewritten == "桔子酒店2025年用水量"


class _AngleRef:
    provider = "dashscope"
    model = "qwen-turbo"


class _AngleLLM:
    def __init__(self, text: str = "", boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.prompts: list[str] = []
        self.ref = _AngleRef()

    async def generate(self, messages, **kwargs):
        from ekbs.provider.base import LLMResult

        self.prompts.append(str(messages[-1]["content"]))
        if self.boom:
            raise RuntimeError("timeout")
        return LLMResult(text=self.text, model="qwen-turbo")


async def test_fast_model_angles_are_shared_and_keep_original(runtime) -> None:
    """快速模型写的角度同时给关键词和向量；第 1 条必须是改写后的原问。"""
    runtime.settings.retrieval.multi_query = 3
    runtime.settings.retrieval.dense_multi_query = 3
    llm = _AngleLLM(
        "1. 员工年假一共有几天\n"
        "请问年假有多少天\n"
        "\n"
        "2、年假天数是如何规定的\n"
        "这是一段用来解释为什么要这样问的很长很长很长很长很长很长很长很长很长很长的说明文字不应该进入检索"
    )
    rewriter = QueryRewriter(settings=runtime.settings, expand_llm=llm)
    result = await rewriter.rewrite("请问年假有多少天", [])
    assert result.rewritten == "请问年假有多少天"
    assert result.sparse_variants == [
        "请问年假有多少天",
        "员工年假一共有几天",
        "年假天数是如何规定的",
    ]
    assert result.dense_variants == result.sparse_variants
    expand_prompt = llm.prompts[0]
    assert "对话历史" not in expand_prompt
    assert "【示例】" in expand_prompt
    assert "年假天数是如何规定的" in expand_prompt
    assert rewriter.expand_variants("请问年假有多少天") == result.sparse_variants
    assert rewriter.expand_dense_variants("请问年假有多少天") == result.dense_variants


async def test_model_angles_expand_the_context_query_not_the_history(runtime) -> None:
    runtime.settings.retrieval.multi_query = 3
    llm = _AngleLLM("年假申请的提前天数是多少\n请假需要提前多久")
    rewriter = QueryRewriter(settings=runtime.settings, expand_llm=llm)
    history = [
        {"role": "user", "content": "年假有多少天"},
        {"role": "assistant", "content": "5 天 [1]"},
    ]
    result = await rewriter.rewrite("那它需要提前几天申请", history)
    assert result.rewritten.startswith("年假有多少天")
    assert result.sparse_variants[0] == result.rewritten
    assert "5 天" not in llm.prompts[0]
    assert "assistant" not in llm.prompts[0]
    assert result.rewritten in llm.prompts[0]


async def test_model_angle_failure_keeps_rule_variants(runtime) -> None:
    runtime.settings.retrieval.multi_query = 3
    runtime.settings.retrieval.dense_multi_query = 3
    rewriter = QueryRewriter(
        settings=runtime.settings, expand_llm=_AngleLLM(boom=True),
    )
    result = await rewriter.rewrite("请问年假有多少天", [])
    plain = QueryRewriter(settings=runtime.settings)
    assert result.sparse_variants == plain.expand_variants("请问年假有多少天")
    assert result.dense_variants == plain.expand_dense_variants("请问年假有多少天")
    assert "员工年假一共有几天" not in result.sparse_variants


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


async def test_followup_history_never_enters_generation_prompt(seeded) -> None:
    """会话历史只用于查询改写，绝不进生成 prompt。

    追问轮：改写把上一轮主题并进自包含问句；生成端吃「系统提示 + 改写后问句」
    （与检索同一口径，避免原句丢年份/主体），仍不得带历史 user/assistant 消息。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    await runtime.pipeline.answer("年假有多少天", alice, conversation_id="conv-g")
    second = await runtime.pipeline.answer(
        "那它需要提前几天申请", alice, conversation_id="conv-g"
    )
    # 第二轮确实是追问：改写用到了历史，但历史只能进改写，不能进生成
    assert second.history_used is True
    assert second.rewritten_query.startswith("年假有多少天")

    last_messages = runtime.pipeline.providers.llm.calls[-1]
    roles = [m["role"] for m in last_messages]
    assert roles[0] == "system"
    assert "assistant" not in roles, "生成 prompt 不得带历史 assistant 消息"
    user_contents = [m["content"] for m in last_messages if m["role"] == "user"]
    assert user_contents == [second.rewritten_query], (
        "生成 prompt 应用改写后问句（与检索一致），且不得带历史"
    )


async def test_answer_cache_is_partitioned_by_history(seeded) -> None:
    """只有追问（rewrite 用到历史）才分缓存键；自包含问题不带历史、可共享。

    自包含问题重复问时，UI/钉钉应产出同一份 prompt、同一答案，
    所以历史指纹不进键；真正的上下文差异来自“追问/指代”。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    first = await runtime.pipeline.answer("年假有多少天", alice, conversation_id="c-a")
    second = await runtime.pipeline.answer("那它需要提前几天申请", alice, conversation_id="c-a")
    assert first.history_used is False
    assert second.history_used is True
    with runtime.db.sync_connect() as conn:
        keys = [
            str(r["cache_key"])
            for r in conn.execute(
                "SELECT cache_key FROM ekbs_cache WHERE cache_key LIKE 'ekbs:cache:answer:%'"
            ).fetchall()
        ]
    assert len(keys) == 2, f"追问必须与首轮分缓存键：{keys}"
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


async def test_rewriter_merges_period_followup_without_previous_question_noise(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "2026年公司承担电费是多少"},
        {"role": "assistant", "content": "1,921,171.03 元 [1]"},
    ]
    result = await rewriter.rewrite("那1-5月啦", history)
    assert result.used_history is True
    assert result.rewritten == "2026年公司承担电费1-5月是多少"


async def test_rewriter_merges_solicit_year_period_keep_entity_metric(runtime) -> None:
    """「提供一下2026年1-3月的」须继承上一问主体，不能丢掉新东方/用电量。"""
    from ekbs.session import _merge_followup

    assert (
        _merge_followup("新东方1-3月用电量", "提供一下2026年1-3月的")
        == "新东方2026年1-3月用电量是多少"
    )
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "新东方1-3月用电量"},
        {"role": "assistant", "content": "2025年…[1]"},
    ]
    result = await rewriter.rewrite("提供一下2026年1-3月的", history)
    assert result.used_history is True
    assert result.rewritten == "新东方2026年1-3月用电量是多少"


async def test_rewriter_merges_short_entity_followup(runtime) -> None:
    """「新东方的」继承上一问已钉死的年/期间/指标。"""
    from ekbs.session import _merge_followup

    assert (
        _merge_followup("新东方2026年1-3月用电量是多少", "新东方的")
        == "新东方2026年1-3月用电量是多少"
    )


async def test_rewriter_carries_year_for_period_question(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [{"role": "user", "content": "2026年公司承担电费是多少"}]
    result = await rewriter.rewrite("公司承担电费1-5月是多少", history)
    assert result.rewritten == "2026年公司承担电费1-5月是多少"


async def test_rewriter_carries_year_from_older_turn(runtime) -> None:
    rewriter = QueryRewriter(settings=runtime.settings)
    history = [
        {"role": "user", "content": "2026年公司承担电费是多少"},
        {"role": "assistant", "content": "1,921,171.03 元 [1]"},
        {"role": "user", "content": "那1-5月啦"},
        {"role": "assistant", "content": "789,663.63 元 [1]"},
    ]
    result = await rewriter.rewrite("公司承担电费1-5月是多少", history)
    assert result.rewritten == "2026年公司承担电费1-5月是多少"
