"""上下文预算：TTL 一周 / 10 轮记忆 / prompt token 上限。

## 为什么要钉住这三条

RAG 的"记忆"是**把资料 + 多轮历史 + 问题一起塞给模型**，而模型窗口有限
（qwen-max 最大输入 30,720）。实测最坏情况：

    资料 5 块 × 2048（表格块上限）      = 10,240
    记忆 10 轮 ×（答 2048 + 问 2000）   = 40,480
    ─────────────────────────────────────────
    合计 ≈ 50k+  ⇒ 直接超窗

而改动前**没有任何兜底**（我 grep 过：`truncate/max_context` 一处都没有），
最坏路径会撞云端参数错。所以：

* `session.context_ttl_seconds` 2 小时 → **7 天**（604800）：跨天追问还能用上上下文
* `session.context_max_turns` 6 → **10 轮**：满足"至少 10 轮"的要求
* `models.llm.max_input_tokens` 新增 **20000**：按"本地 Qwen 计数 × 最坏偏差 1.19 ≈ 23.8k"
  留约 23% 余量（本地计数器在标点/数字密集文本上会低估，实测比例最高 1.1898）

裁剪**牺牲顺序**：① 最旧轮次 → ② 排名最低的块 → ③ 截断最后一块。
永远保留：最近 1 轮 + 至少 1 个块。
"""

from __future__ import annotations

from ekbs.config import load_settings
from ekbs.retrieval.pipeline import RetrievedChunk
from tests.conftest import user_id_of


def _chunk(chunk_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, doc_id=1, shard_id="kb_all_L1", content=content, doc_title="测试文档"
    )


# ---------------------------------------------------------------- 配置


def test_shipped_configs_meet_the_new_policy() -> None:
    """三份配置都要满足：TTL ≥ 7 天、10 轮、输入上限 > 0。"""
    for path in ("config/application.cloud.yaml", "config/application.dev.yaml",
                 "deploy/config/application.yaml"):
        settings = load_settings(path)
        assert int(settings.session["context_ttl_seconds"]) >= 7 * 24 * 3600, path
        assert int(settings.session["context_max_turns"]) == 10, path
        assert int(settings.models.llm.max_input_tokens) > 0, path
        # 上限必须**低于**模型窗口（30,720），且留出偏差余量
        assert settings.models.llm.max_input_tokens * 1.25 < 30720, path


# ---------------------------------------------------------------- 记忆窗口


def test_session_ttl_is_a_week(db, settings) -> None:
    """TTL 一周：2 小时前的上下文仍可用，8 天前的失效。"""
    from ekbs.session import SessionStore

    store = SessionStore(db, settings)
    assert store.ttl_seconds == 604800
    assert store.max_turns == 10
    with db.sync_connect() as conn:
        for age, conversation in (("2 hours", "c-fresh"), ("8 days", "c-stale")):
            conn.execute(
                "INSERT INTO ekbs_session_ctx (conversation_id, dingtalk_userid, payload, updated_at) "
                "VALUES (?, ?, ?, datetime('now', ?))",
                (conversation, "u1", '[{"role":"user","content":"你好"}]', f"-{age}"),
            )
        conn.commit()
    assert store.load("c-fresh", "u1"), "2 小时前的上下文不该过期"
    assert store.load("c-stale", "u1") == [], "8 天前的上下文必须过期"


def test_session_keeps_ten_turns(db, settings) -> None:
    """保证 10 轮（=20 条消息）：11 轮时丢掉最旧的一轮。"""
    import json

    from ekbs.session import SessionStore

    store = SessionStore(db, settings)
    messages = []
    for turn in range(11):
        messages.append({"role": "user", "content": f"问题{turn}"})
        messages.append({"role": "assistant", "content": f"回答{turn}"})
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_session_ctx (conversation_id, dingtalk_userid, payload, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("c10", "u1", json.dumps(messages, ensure_ascii=False)),
        )
        conn.commit()
    kept = store.load("c10", "u1")
    assert len(kept) == 20, f"应保留 10 轮 = 20 条，实际 {len(kept)}"
    assert kept[0]["content"] == "问题1", "丢掉的应是最旧的一轮"
    assert kept[-1]["content"] == "回答10"


# ---------------------------------------------------------------- 预算裁剪


def _fit(runtime, chunks, history, question, budget):
    runtime.settings.models.llm.max_input_tokens = budget
    from ekbs.retrieval.pipeline import SYSTEM_PROMPT
    from ekbs.ingest.chunker import count_tokens

    return runtime.pipeline._fit_prompt(
        chunks, history, question, system_tokens=count_tokens(SYSTEM_PROMPT.format(context=""))
    )


def test_budget_drops_oldest_turns_first(runtime) -> None:
    """① 先丢最旧的轮次 —— 资料是答案的唯一依据，记忆只是辅助。"""
    long_text = "公司制度条款内容说明。" * 10      # 资料较短：确保"先丢历史"能救回来
    chunks = [_chunk(1, long_text)]
    history = [
        {"role": "user", "content": "旧问题" * 300},      # 一条就 ~900 token
        {"role": "assistant", "content": "旧回答" * 300},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]
    kept_chunks, kept_history, stats = _fit(runtime, chunks, history, "这制度怎么规定的？", 800)
    assert len(kept_chunks) == 1, "资料必须保留"
    assert stats["dropped_history"] >= 2, "应先丢最旧的轮次"
    assert kept_history[-1]["content"] == "新回答", "最近的轮次要保留"


def test_budget_drops_lowest_ranked_chunks_then_truncates(runtime) -> None:
    """② 块不够放就丢排名最低的（保留至少 1 个）；③ 仍不够则截断并标注。"""
    body = "交接班制度条款内容。" * 200
    chunks = [_chunk(1, body), _chunk(2, body), _chunk(3, body)]
    kept_chunks, kept_history, stats = _fit(runtime, chunks, [], "交接班怎么交接？", 1200)
    assert len(kept_chunks) == 1, f"预算紧到只该剩 1 块，实际 {len(kept_chunks)}"
    assert stats["dropped_chunks"] == 2
    assert stats["truncated"] is True, "唯一剩下的块也要按预算截断"
    assert "已截断" in kept_chunks[0].content
    assert stats["prompt_tokens"] <= 1200


def test_budget_never_trips_when_plenty_of_room(runtime) -> None:
    """宽松预算下**不做任何裁剪**（避免正常问答被无谓改动）。"""
    chunks = [_chunk(1, "简短内容。"), _chunk(2, "另一段内容。")]
    kept_chunks, kept_history, stats = _fit(runtime, chunks, [{"role": "user", "content": "问"}],
                                            "问题？", 20000)
    assert len(kept_chunks) == 2
    assert stats["dropped_chunks"] == 0 and stats["dropped_history"] == 0
    assert stats["truncated"] is False


async def test_answer_reports_budget_trimming(seeded) -> None:
    """接线断言：真跑一次问答，裁剪痕迹要进 `AnswerResult`（可观测性）。"""
    runtime, _ = seeded
    from ekbs.retrieval.pipeline import SYSTEM_PROMPT
    from ekbs.ingest.chunker import count_tokens

    settings = runtime.settings
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    question = "年假有多少天"
    retrieval = await runtime.pipeline.retrieve(question, alice)
    assert retrieval.chunks, "先得有召回"
    budget = (
        count_tokens(SYSTEM_PROMPT.format(context="")) + count_tokens(question) + 64
        + count_tokens(runtime.pipeline.build_context(retrieval.chunks[:1]))
    )
    settings.models.llm.max_input_tokens = budget  # 只够放 1 块
    try:
        answer = await runtime.pipeline.generate(
            question, retrieval, alice, history=[{"role": "user", "content": "上文" * 40}]
        )
    finally:
        settings.models.llm.max_input_tokens = 20000
    assert answer.refused is False
    assert answer.dropped_chunks >= 1 or answer.dropped_history >= 1
    assert len(answer.citations) <= len(retrieval.chunks)
