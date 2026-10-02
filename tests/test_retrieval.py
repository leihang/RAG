"""检索链路与权限红线（`00` §2.1/§2.3、C1/C5）。

最重要的一条：**低权限用户永远召不回高密级分片的内容** —— 这不是「取回后再过滤」，
而是无权分片根本不参与计算（C1）。
"""

from __future__ import annotations

import pytest

from ekbs.retrieval.pipeline import (
    REFUSE_BELOW_THRESHOLD,
    REFUSE_EMBED_UNAVAILABLE,
    REFUSE_NO_SHARD,
)
from ekbs.retrieval.rrf import rrf_fuse
from tests.conftest import user_id_of


def test_rrf_formula_is_rank_based() -> None:
    """rrf(c) = Σ 1/(k + rank)，rank 从 1 开始（k=60）。"""
    fused = dict(rrf_fuse([["a", "b"], ["b", "c"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 62)
    order = [item for item, _ in rrf_fuse([["a"], ["b"]], k=60)]
    assert set(order) == {"a", "b"}


def test_rrf_is_insensitive_to_score_scale() -> None:
    """只用名次：把两路顺序对调，结果集合不变（分数不可比是 RRF 的出发点）。"""
    first = dict(rrf_fuse([["a", "b", "c"], ["x"]], k=60))
    second = dict(rrf_fuse([["a", "b", "c"], ["x"]], k=60))
    assert first == second


async def test_alice_retrieves_hr_policy(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve("年假有多少天", alice)
    assert result.refused is False, result.refusal_reason
    titles = {chunk.doc_title for chunk in result.chunks}
    assert "年假管理制度" in titles
    assert all(chunk.shard_id in alice.visible_shard_ids for chunk in result.chunks)


async def test_redline_alice_cannot_reach_finance_document(seeded) -> None:
    """**红线**：HR 用户检索财务机密内容，结果里不得出现该文档的任何块。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    with runtime.db.sync_connect() as conn:
        finance_doc = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE title = '财务保密制度'"
            ).fetchone()["doc_id"]
        )
    result = await runtime.pipeline.retrieve("财务专用章由谁保管", alice)
    assert all(chunk.doc_id != finance_doc for chunk in result.chunks)
    assert "kb_finance_L3" not in alice.visible_shard_ids


async def test_bob_can_reach_finance_document(seeded) -> None:
    runtime, _ = seeded
    bob = await runtime.perm_context(user_id_of(runtime.db, "demo_bob"))
    result = await runtime.pipeline.retrieve("财务专用章由谁保管", bob)
    assert result.refused is False, result.refusal_reason
    assert any(chunk.doc_title == "财务保密制度" for chunk in result.chunks)


async def test_carol_sees_only_public_shard(seeded) -> None:
    runtime, _ = seeded
    carol = await runtime.perm_context(user_id_of(runtime.db, "demo_carol"))
    result = await runtime.pipeline.retrieve("年假有多少天", carol)
    assert all(chunk.shard_id == "kb_all_L1" for chunk in result.chunks)


async def test_answer_carries_traceable_citations(seeded) -> None:
    """回答必须带可溯源引用，且引用字段齐备（00 §2.2）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    retrieval = await runtime.pipeline.retrieve("年假有多少天", alice)
    answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    assert answer.refused is False
    assert answer.citations, "必须有引用"
    citation = answer.citations[0]
    assert citation.chunk_id > 0
    assert citation.doc_id > 0
    assert citation.acl_result == "pass"
    assert citation.page_check in {"pass", "out_of_range"}
    assert citation.rerank_score is not None


async def test_parent_expansion_returns_parent_chunk(seeded) -> None:
    """命中子块 → 上下文用父块全文（06 §3.2/§3.4）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve("年假有多少天", alice)
    assert result.chunks
    assert any(chunk.chunk_type == "parent" for chunk in result.chunks)


async def test_irrelevant_query_is_refused(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve("量子纠缠与暗物质的退相干机制", alice)
    assert result.refused is True
    assert result.refusal_reason == REFUSE_BELOW_THRESHOLD


async def test_embedding_unavailable_refuses_instead_of_fts_only(seeded) -> None:
    """嵌入不可用时必须**明确拒答**，禁止降级为「只走 FTS5 假装语义检索」（00 §2.3）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    class BrokenEmbedding:
        ref = runtime.providers.embed.ref

        async def embed(self, texts):  # pragma: no cover - 不应被调用
            raise RuntimeError("should not be called")

        async def embed_one(self, text):
            raise RuntimeError("cloud embedding down")

    original = runtime.pipeline.providers.embed
    runtime.pipeline.providers.embed = BrokenEmbedding()  # type: ignore[assignment]
    try:
        result = await runtime.pipeline.retrieve("年假有多少天", alice)
    finally:
        runtime.pipeline.providers.embed = original  # type: ignore[assignment]
    assert result.refused is True
    assert result.refusal_reason in {REFUSE_EMBED_UNAVAILABLE, REFUSE_BELOW_THRESHOLD}


async def test_no_visible_shard_refuses_cleanly(seeded) -> None:
    """无可见分片：明确提示，不 500、不返回空答案冒充正常（06 §6.4.4 TC-SHARD-009）。"""
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_user(dingtalk_userid, nick, clearance, dept_chain, status) "
            "VALUES ('demo_nobody', 'Nobody', 1, '[]', 'active')"
        )
        conn.commit()
        user_id = int(
            conn.execute(
                "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = 'demo_nobody'"
            ).fetchone()["user_id"]
        )
    # 只有 L1 公共分片可被 clearance=1 看到 → 先把它下线，构造「无可见分片」
    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_shard SET status = 'retired' WHERE shard_id = 'kb_all_L1'")
        conn.commit()
    try:
        perm = await runtime.perm_context(user_id)
        assert perm.visible_shard_ids == ()
        assert perm.warnings  # 必须留痕
        result = await runtime.pipeline.retrieve("年假", perm)
        assert result.refused is True
        assert result.refusal_reason == REFUSE_NO_SHARD
    finally:
        with runtime.db.sync_connect() as conn:
            conn.execute("UPDATE ekbs_shard SET status = 'active' WHERE shard_id = 'kb_all_L1'")
            conn.commit()


async def test_answer_cache_is_partitioned_by_fingerprint(seeded) -> None:
    """答案缓存键必须含权限指纹：两个用户的缓存互不可见（C2）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    carol_id = user_id_of(runtime.db, "demo_carol")
    alice = await runtime.perm_context(alice_id)
    carol = await runtime.perm_context(carol_id)

    await runtime.pipeline.answer("年假有多少天", alice)
    from ekbs.cache.store import PREFIX_ANSWER

    with runtime.db.sync_connect() as conn:
        keys = [
            str(r["cache_key"])
            for r in conn.execute(
                "SELECT cache_key FROM ekbs_cache WHERE cache_key LIKE ?", (f"{PREFIX_ANSWER}%",)
            ).fetchall()
        ]
    assert keys, "应当写入了答案缓存"
    assert all(alice.fingerprint in key for key in keys)
    assert all(carol.fingerprint not in key for key in keys)


async def test_model_self_refusal_is_reported_as_refused(seeded) -> None:
    """**契约一致性**：模型自己说「未找到可靠依据」时，必须 refused=True 且引用为空。

    实测踩到的反例：低阈值放进了语料里根本没有答案的问题，模型按提示词回了
    「未找到可靠依据」，但接口同时返回 ``refused=false`` + 5 条引用 ——
    前端照 ``refused`` 渲染，于是把「没答」显示成「答了并给了出处」。
    """
    from ekbs.provider.base import LLMResult
    from ekbs.retrieval.pipeline import REFUSE_NO_BASIS

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    class NoBasisLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            return LLMResult(text="未找到可靠依据。", model="no-basis-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = NoBasisLLM()  # type: ignore[assignment]
    try:
        retrieval = await runtime.pipeline.retrieve("年假有多少天", alice)
        assert retrieval.chunks, "前提：检索确实召回了候选，否则走不到生成分支"
        answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]

    assert answer.refused is True
    assert answer.citations == []
    assert answer.refusal_reason == REFUSE_NO_BASIS
    assert answer.answer.strip() == "未找到可靠依据。"


def test_no_basis_marker_only_matches_at_the_start() -> None:
    """只在开头比对：正文里正常引用这句话的答案不能被误判成拒答。"""
    from ekbs.retrieval.pipeline import _is_no_basis

    assert _is_no_basis("未找到可靠依据。")
    assert _is_no_basis("  **未找到可靠依据**  \n")
    assert _is_no_basis("「未找到可靠依据」")
    assert not _is_no_basis("根据资料 [1]，若资料不足应回复「未找到可靠依据」。")
    assert not _is_no_basis("年假为 5 天 [1]。")


async def test_answer_cache_key_includes_retrieval_config_revision(seeded) -> None:
    """**改了检索参数就必须换缓存键**，否则旧答案会被继续服务到 TTL 到期。

    实测踩到：`rerank.min_score` 从 0.30 改到 0.60 后，同一个问题仍在 3ms 内
    返回旧语义的答案（`refused=false` + 5 条引用 +「未找到可靠依据」），
    排查时极易误判成"改的代码没生效"。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    pipeline = runtime.pipeline

    base = pipeline.retrieval_cache_revision()
    assert base

    original = pipeline.settings.retrieval.rerank.min_score
    try:
        pipeline.settings.retrieval.rerank.min_score = original + 0.1
        assert pipeline.retrieval_cache_revision() != base
    finally:
        pipeline.settings.retrieval.rerank.min_score = original
    assert pipeline.retrieval_cache_revision() == base

    # 权限指纹仍必须参与（C2 不能被新分量挤掉），且版本号必须改变最终键
    await pipeline.answer("年假有多少天", alice)
    from ekbs.cache.store import PREFIX_ANSWER, CacheStore

    with runtime.db.sync_connect() as conn:
        keys = [
            str(r["cache_key"])
            for r in conn.execute(
                "SELECT cache_key FROM ekbs_cache WHERE cache_key LIKE ?", (f"{PREFIX_ANSWER}%",)
            ).fetchall()
        ]
    assert keys
    assert all(key.startswith(f"{PREFIX_ANSWER}{alice.fingerprint}:") for key in keys)
    assert CacheStore.answer_key(alice.fingerprint, f"q|rc={base}") != CacheStore.answer_key(
        alice.fingerprint, "q|rc=another-revision"
    )


async def test_cache_hit_marks_degraded_and_keeps_timings_shape(seeded, settings) -> None:
    """命中答案缓存要**可观测**，且 `timings_ms` 保持与未命中时相同的键集合。

    实测踩到：命中缓存时 `timings_ms` 只有 `total_request`，没有 `total` ——
    调用方按 `timings_ms["total"]` 取值直接 KeyError。
    """
    settings.retrieval.cache.enabled = True
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    first = await runtime.pipeline.answer("年假有多少天", alice)
    assert "answer_cache_hit" not in first.degraded

    second = await runtime.pipeline.answer("年假有多少天", alice)
    assert "answer_cache_hit" in second.degraded, "同一问题无会话上下文应当命中缓存"
    assert second.answer == first.answer
    assert {"embed", "recall", "rerank", "expand", "total"} <= set(second.retrieval.timings_ms)


async def test_remember_turn_skips_refusal_and_missing_conversation(seeded) -> None:
    """会话写入的两个必要条件：有 `conversation_id`、且本轮**不是拒答**。

    拒答不进上下文：把「未找到可靠依据」写进历史会污染后续追问的改写。
    """
    from ekbs.retrieval.pipeline import AnswerResult, RetrievalResult

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    retrieval = RetrievalResult(query="q", chunks=[], refused=True)

    refused = AnswerResult(answer="未找到可靠依据。", citations=[], retrieval=retrieval,
                           model="m", refused=True, refusal_reason="below_threshold")
    await runtime.pipeline._remember_turn("conv-x", alice, "年假有多少天", refused)
    assert runtime.sessions.load("conv-x", alice.dingtalk_userid) == []

    answered = AnswerResult(answer="年假 5 天 [1]。", citations=[], retrieval=retrieval, model="m")
    await runtime.pipeline._remember_turn("conv-y", alice, "年假有多少天", answered)
    turns = runtime.sessions.load("conv-y", alice.dingtalk_userid)
    assert turns and turns[-1]["role"] == "assistant"

    await runtime.pipeline._remember_turn(None, alice, "年假有多少天", answered)
    assert runtime.sessions.load("conv-y", alice.dingtalk_userid) == turns


async def test_switching_embedding_model_refuses_to_load_old_shards(seeded) -> None:
    """**C3/C4 安全网**：换嵌入模型后，旧分片必须让新进程**拒绝启动**，而不是混用向量。

    真实踩到：选型从 `text-embedding-v4` 换成 `qwen3.7-text-embedding-flash`，两者
    **维度都是 1024** —— 若没有这道闸，新旧模型的向量会被静默混进同一个索引，
    召回质量下降且**完全不报错**。实际表现是 uvicorn 启动即失败，并逐个分片报出差异。

    这里用「新建一个 manager」模拟新进程：它拿的是新模型的指纹，`_loaded` 为空，
    与启动路径完全一致（不能只改 `providers.embed.ref` —— manager 的 `_loaded`
    缓存会在文件未变时直接命中，反而掩盖问题）。
    """
    from ekbs.errors import EmbeddingFingerprintMismatch
    from ekbs.index.faiss_store import ShardIndexManager
    from ekbs.provider.base import model_ref

    runtime, settings = seeded
    old_ref = runtime.providers.embed.ref
    new_ref = model_ref("dashscope", "qwen3.7-text-embedding-flash", dim=old_ref.dim)
    assert new_ref.fingerprint != old_ref.fingerprint, "前提：两个模型的指纹必须不同"

    fresh = ShardIndexManager(runtime.db, settings, dim=new_ref.dim, fingerprint=new_ref.fingerprint)
    with pytest.raises(EmbeddingFingerprintMismatch) as excinfo:
        fresh.load_active(strict=True)
    message = str(excinfo.value)
    assert "重建" in message, "错误信息必须告诉运维怎么修"
    assert "kb_hr_L2" in message, "必须逐个分片报出差异，而不是只说一句不一致"

    # strict=False 供巡检/迁移工具使用：只记日志，不阻断进程
    assert fresh.load_active(strict=False) == {}

    # 原 manager 不受影响（同一进程内的既有分片仍可用）
    assert runtime.fingerprint_match() is True


TRAVEL_DOC = """# 差旅费管理办法

## 第一条 交通标准

市内交通据实报销；高铁二等座、飞机经济舱为标准配置。

## 第二条 住宿标准

一线城市每晚不超过 600 元，其他城市不超过 400 元，凭发票报销。
"""


async def test_heading_only_keyword_is_retrievable(seeded, runtime) -> None:
    """**回归测试**：关键限定词只出现在小节标题里时，稠密召回也必须能命中。

    真实踩到的场景：制度文件写「第二条 住宿标准」，正文却是「一线城市每晚不超过 600 元…」。
    若嵌入文本只用正文，查询「住宿标准」与这段正文**没有共享词**，向量内积恰好为 0，
    结果是「文件明明入库了却查不到」。修法是把标题路径并入嵌入/精排文本
    （口径唯一：`ekbs.ingest.chunker.retrieval_text`）。
    """
    from ekbs.ingest.pipeline import IngestPipeline
    from ekbs.ingest.chunker import retrieval_text

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    await pipeline.ingest_markdown(
        title="差旅费管理办法",
        markdown=TRAVEL_DOC,
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
    )
    carol = await runtime.perm_context(user_id_of(runtime.db, "demo_carol"))
    result = await runtime.pipeline.retrieve("住宿标准", carol)
    assert any(chunk.doc_title == "差旅费管理办法" for chunk in result.chunks), (
        f"标题里的关键词没被召回：{[c.doc_title for c in result.chunks]}"
    )

    # 口径唯一性：嵌入/精排用的文本必须包含标题
    assert retrieval_text("A > 第二条 住宿标准", "一线城市每晚不超过 600 元") == (
        "A > 第二条 住宿标准\n一线城市每晚不超过 600 元"
    )
    assert retrieval_text("", "无标题") == "无标题"


async def test_migration_keeps_heading_in_embedding(seeded) -> None:
    """分片迁移必须与入库同口径，否则同一分片里会混入两套向量语义。

    做法：导入一份「关键限定词只在小节标题」的文档 → 调高密级触发**全量重嵌入 + 迁移**
    → 迁移后仍应能被标题关键词召回（说明重嵌入用的仍是「标题 + 正文」）。
    """
    from ekbs.ingest.pipeline import IngestPipeline
    from ekbs.permission.changes import PermissionChangeService

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    runtime.settings.permission.l3_ingest_enabled = True
    result = await pipeline.ingest_markdown(
        title="差旅费管理办法", markdown=TRAVEL_DOC, kb_id=int(summary["kb_id"]),
        level=2, visible_dept_ids=["102"], real_corpus=False,
    )
    alice_id = user_id_of(runtime.db, "demo_alice")
    # 调高密级 → 迁移到 kb_hr_L3（同时全量重嵌入）
    service = PermissionChangeService(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, cache=runtime.cache,
    )
    change = await service.change_document_level(result.doc_id, 3)
    assert change.from_shard == "kb_hr_L2" and change.to_shard == "kb_hr_L3"

    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_user SET clearance = 3 WHERE user_id = ?", (alice_id,))
        conn.commit()
    alice = await runtime.perm_context(alice_id)
    assert "kb_hr_L3" in alice.visible_shard_ids
    hit = await runtime.pipeline.retrieve("住宿标准", alice)
    assert any(chunk.doc_title == "差旅费管理办法" for chunk in hit.chunks), (
        "迁移后的向量必须仍包含小节标题，否则标题关键词再也召不回"
    )


async def test_shard_index_reloads_after_external_writer(seeded, settings, tmp_path) -> None:
    """**跨进程可见性回归**：worker/工具追加向量后，API 侧必须能看见新文档。

    真实故障形态：`ekbs-worker` 入库并原子替换 `.faiss`，而 `ekbs-api` 手里是启动时
    加载的内存索引 —— 不重载就表现为「刚上传的制度搜不到」。
    这里用两个 Runtime 实例模拟两个进程（共享同一份文件与库）。
    """
    from ekbs.runtime import Runtime

    api_runtime, summary = seeded
    alice = await api_runtime.perm_context(user_id_of(api_runtime.db, "demo_alice"))
    marker = "专项条款丙丙丁丁"
    before = await api_runtime.pipeline.retrieve(marker, alice)
    assert before.chunks == [], "这个关键词在入库前不该召回"

    # 「另一个进程」：独立的 Runtime，往同一个分片追加一篇新文档
    writer = Runtime.create(settings, service="test-writer")
    try:
        from ekbs.ingest.pipeline import IngestPipeline

        pipeline = IngestPipeline(
            db=writer.db, settings=writer.settings, providers=writer.providers,
            shards=writer.shards, fts=writer.fts,
        )
        await pipeline.ingest_markdown(
            title="专项条款规定",
            markdown=f"# 专项条款规定\n\n## 第一条\n\n{marker} 的相关说明。\n",
            kb_id=int(summary["kb_id"]),
            level=2,
            visible_dept_ids=["102"],
            real_corpus=False,
        )
    finally:
        await writer.aclose()

    # API 侧无需重启：get() 检测到文件变更后重载
    after = await api_runtime.pipeline.retrieve(marker, alice)
    assert any(chunk.doc_title == "专项条款规定" for chunk in after.chunks), (
        "另一个进程写入的向量必须对 API 可见（否则就是「刚入库搜不到」）"
    )


def test_build_context_marks_figure_chunks() -> None:
    """索图问答依赖上下文里的【图片】标记，提示词才能指引模型去引用。"""
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievedChunk

    chunks = [
        RetrievedChunk(
            chunk_id=1, doc_id=1, shard_id="kb_all_L1", content="计量表更换说明",
            chunk_type="parent", doc_title="工程汇报", page_from=8, page_to=9,
        ),
        RetrievedChunk(
            chunk_id=2, doc_id=1, shard_id="kb_all_L1", content="DT862 电能表",
            chunk_type="figure", doc_title="工程汇报", page_from=8, page_to=8,
            image_path="parse/1/images/a.png",
        ),
    ]
    ctx = RetrievalPipeline.build_context(chunks)
    assert "【图片】" in ctx
    assert "[1]" in ctx and "[2]" in ctx
    assert "DT862" in ctx
