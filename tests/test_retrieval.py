"""检索链路与权限红线（`00` §2.1/§2.3、C1/C5）。

最重要的一条：**低权限用户永远召不回高密级分片的内容** —— 这不是「取回后再过滤」，
而是无权分片根本不参与计算（C1）。
"""

from __future__ import annotations

import pytest

from ekbs.retrieval.pipeline import (
    REFUSE_ALL_DENIED,
    REFUSE_BELOW_THRESHOLD,
    REFUSE_NO_CANDIDATE,
    RetrievedChunk,
    REFUSE_EMBED_UNAVAILABLE,
    REFUSE_NO_SHARD,
    refusal_answer_text,
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


async def test_d18_denied_chunk_never_sent_to_rerank(seeded) -> None:
    """**D18 红线**：片内 deny 的 chunk 不得进入云端 rerank 的 payload（00 v4.0 C5 双闸）。

    分片过滤只能保证「片级有权」（C1）——alice 能看 kb_hr_L2 分片，但对片内
    《年假管理制度》加 doc 级 deny 后，其 chunk 仍会参与片内召回；若缺 D18 粗筛，
    这些 chunk 会随 payload 明文发往第三方 rerank。本用例录制 rerank 入参做抓包式断言。
    """
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    bob_id = user_id_of(runtime.db, "demo_bob")
    with runtime.db.sync_connect() as conn:
        hr_doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE title = '年假管理制度'"
            ).fetchone()["doc_id"]
        )
        finance_doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE title = '财务保密制度'"
            ).fetchone()["doc_id"]
        )
        hr_chunk_ids = {
            int(r["chunk_id"])
            for r in conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (hr_doc_id,)
            ).fetchall()
        }
        finance_chunk_ids = {
            int(r["chunk_id"])
            for r in conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (finance_doc_id,)
            ).fetchall()
        }

    # 录制 rerank payload（TC-D18 的「抓包」）
    original = runtime.pipeline.providers.rerank
    payloads: list[list[int]] = []

    class _RecordingRerank:
        ref = original.ref

        async def rerank(self, query, docs, top_n):
            payloads.append([d.chunk_id for d in docs])
            return await original.rerank(query, docs, top_n)

    runtime.pipeline.providers.rerank = _RecordingRerank()

    # 正向对照：bob 正常问答 —— 录制器必须真的录到了 payload
    # （证明下方「没录到 deny 内容」不是因为 rerank 压根没被调用）
    bob = await runtime.perm_context(bob_id)
    control = await runtime.pipeline.retrieve("财务专用章由谁保管", bob)
    assert control.refused is False, control.refusal_reason
    assert payloads, "对照组未触发 rerank 调用，录制器接线有误"
    assert any(finance_chunk_ids & set(batch) for batch in payloads)

    # 片内 deny：alice 可见 kb_hr_L2 分片，但对片内该文档有 doc 级 deny
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id,"
            " permission, effect) VALUES ('document', ?, 'user', ?, 'read', 'deny')",
            (str(hr_doc_id), str(alice_id)),
        )
        conn.commit()

    alice = await runtime.perm_context(alice_id)
    before = len(payloads)
    result = await runtime.pipeline.retrieve("年假有多少天", alice)

    # ① 粗筛确实拦下了候选（闸门在工作，不是空跑）
    assert result.c5_pre_dropped >= 1
    # ② 红线：deny 的 chunk 一次都没进过 rerank payload（无权内容零出域）
    sent = {cid for batch in payloads[before:] for cid in batch}
    assert not (sent & hr_chunk_ids), "被 deny 的 chunk 泄漏进 rerank payload"
    # ③ 纵深：最终结果也不含该文档的任何块
    assert all(chunk.doc_id != hr_doc_id for chunk in result.chunks)


async def test_sparse_lane_honors_shard_acl_allow(seeded) -> None:
    """**双路径等价**（`03` §5.3）：跨部门 shard 级授权后，稀疏路（BM25）必须与稠密路
    一样能召回 —— 修复前稀疏 SQL 只做部门交集，bob 经 allow 看到 kb_hr_L2 时
    BM25 一路被清空，混合检索对他静默退化为单路。
    """
    runtime, _ = seeded
    bob_id = user_id_of(runtime.db, "demo_bob")
    with runtime.db.sync_connect() as conn:
        hr_child_ids = {
            int(r["chunk_id"])
            for r in conn.execute(
                "SELECT c.chunk_id FROM ekbs_chunk c "
                "JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE d.title = '年假管理制度' AND c.chunk_type = 'child'"
            ).fetchall()
        }
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id,"
            " permission, effect) VALUES ('shard', 'kb_hr_L2', 'user', ?, 'read', 'allow')",
            (str(bob_id),),
        )
        conn.commit()

    bob = await runtime.perm_context(bob_id)
    # 路由器已把 allow 并入可见分片（且过了密级闸：bob clearance=3 ≥ shard level=2）
    assert "kb_hr_L2" in bob.visible_shard_ids
    assert "kb_hr_L2" in bob.acl_allow_shards
    # 稀疏路也必须能召回（修复前这里恒为空集）
    sparse_ids = set(runtime.pipeline._sparse_recall("年假有多少天", bob))
    assert hr_child_ids & sparse_ids, "跨部门授权后稀疏路仍召回不到，双路径不等价"


async def test_fts_doc_level_acl_allow_branch(seeded) -> None:
    """document 级 allow 的稀疏路放行（与 ``can_read_document`` 规则 4 等价）：

    文档在可见分片内、但 `visible_dept_ids` 与本用户部门不命中时，
    无 allow 则稀疏路查不到（超取交给闸门兜底），有 allow 则必须命中。
    白盒直测 ``FtsIndex.search``，避免路由/稠密路的干扰。
    """
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE title = '年假管理制度'"
            ).fetchone()["doc_id"]
        )
        doc_child_ids = {
            int(r["chunk_id"])
            for r in conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ? AND chunk_type = 'child'",
                (doc_id,),
            ).fetchall()
        }

    fts = runtime.pipeline.fts
    base = dict(shard_ids=["kb_hr_L2"], clearance=3, top_k=10)
    stranger = [("user", "999")]  # 部门链不含 102 的用户主体

    # 未授权：部门不命中（visible_dept_ids=["102"] ∩ ["1"] = ∅）→ 查不到
    before = fts.search("年假有多少天", dept_chain=["1"], subjects=stranger, **base)
    assert not {h.chunk_id for h in before} & doc_child_ids

    # document 级 allow 后：allow 分支命中
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id,"
            " permission, effect) VALUES ('document', ?, 'user', '999', 'read', 'allow')",
            (str(doc_id),),
        )
        conn.commit()
    after = fts.search("年假有多少天", dept_chain=["1"], subjects=stranger, **base)
    assert {h.chunk_id for h in after} & doc_child_ids


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


async def test_parent_expansion_keeps_clause_heading(seeded) -> None:
    """父块扩展只换**内容**，标题路径保留命中子块的条款层。

    父块现在是章节容器（「同章小节的上下文」）。若整块替换，引用块的 heading_path
    只剩章名，答案里写「第X条」时 ``clause_mismatches`` 在标题里找不到条号 →
    正确的条款引用会被判幻觉、整答拒掉。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT c.chunk_id, c.parent_id, c.heading_path, c.doc_id, c.shard_id "
            "FROM ekbs_chunk c WHERE c.chunk_type = 'child' AND c.parent_id IS NOT NULL "
            "ORDER BY c.chunk_id LIMIT 1"
        ).fetchone()
    assert row is not None, "种子语料应产出 child（父容器 + 条款子块）"
    child = RetrievedChunk(
        chunk_id=int(row["chunk_id"]),
        doc_id=int(row["doc_id"]),
        shard_id=str(row["shard_id"]),
        content="占位正文",
        heading_path=str(row["heading_path"]),
        chunk_type="child",
        parent_chunk_id=int(row["parent_id"]),
    )
    expanded = runtime.pipeline._expand_parents([child], alice)
    assert expanded, "命中子块应扩展成父块上下文"
    assert expanded[0].chunk_type == "parent"
    assert expanded[0].heading_path == str(row["heading_path"]), "条款层标题必须保留"


def test_table_row_group_expands_into_table_parent() -> None:
    """大表行组命中 → 扩成表父块（上文 + 整表 + 下文），标题保留命中的表名路径。

    小表（整表一块、``parent_id`` 指向正文父块）不展开：展开会把它换成不含表格的正文。
    """
    from ekbs.retrieval.pipeline import RetrievalPipeline

    container = RetrievedChunk(
        chunk_id=10,
        doc_id=1,
        shard_id="s1",
        content="6．质量记录\n\n下表列出全部质量记录。\n\n表名: 表 2\n…\n以上记录保存三年。",
        heading_path="制度 > 6．质量记录 · 表 2",
        chunk_type="table",
    )
    row_group = RetrievedChunk(
        chunk_id=11,
        doc_id=1,
        shard_id="s1",
        content="表名: 表 2\n项目1=数值1",
        heading_path="制度 > 6．质量记录 · 表 2",
        chunk_type="table",
        parent_chunk_id=10,
    )
    prose_parent = RetrievedChunk(
        chunk_id=12,
        doc_id=1,
        shard_id="s1",
        content="正文父块内容（不含表格）",
        heading_path="制度 > 6．质量记录",
        chunk_type="parent",
    )

    class _Gate:
        def filter_chunks(self, perm, chunks):
            return [(cid, did) for cid, did in chunks], []

    class _Perm:
        user_id = "u1"

    pipeline = object.__new__(RetrievalPipeline)
    pipeline.load_chunks = lambda ids: {
        c.chunk_id: c for c in (container, prose_parent) if c.chunk_id in ids
    }
    pipeline.gate = _Gate()

    expanded = pipeline._expand_parents([row_group], _Perm())
    assert expanded and expanded[0].chunk_id == 10, "行组叶子应扩成表父块"
    assert expanded[0].chunk_type == "table"
    assert "以上记录保存三年" in expanded[0].content, "下文结论应进上下文"
    assert expanded[0].heading_path == row_group.heading_path, "表名路径标题必须保留"

    small = RetrievedChunk(
        chunk_id=13,
        doc_id=1,
        shard_id="s1",
        content="表名: 表 4\n…",
        heading_path="制度 · 表 4",
        chunk_type="table",
        parent_chunk_id=12,
    )
    expanded2 = pipeline._expand_parents([small], _Perm())
    assert expanded2[0].content == small.content, "小表不得展开（parent 是正文父块）"


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


def test_refusal_answer_text_distinguishes_permission_vs_miss() -> None:
    """拒答文案按 refusal_reason 区分：无权限引导授权，未命中引导「帮助」查目录。"""
    assert refusal_answer_text(REFUSE_NO_SHARD) == "对不起，你无权查阅资料，如需授权请联系管理员。"
    for reason in (REFUSE_NO_CANDIDATE, REFUSE_BELOW_THRESHOLD, REFUSE_ALL_DENIED):
        assert refusal_answer_text(reason) == (
            "对不起，未找到相关信息。你可以输入帮助，来确认你可以查阅的资料目录清单。"
        ), reason
    # 其余原因（嵌入不可用等）维持原口径
    assert refusal_answer_text(REFUSE_EMBED_UNAVAILABLE) == "未找到可靠依据。"
    assert refusal_answer_text("") == "未找到可靠依据。"


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
    assert retrieval_text("A > 第二条 住宿标准", "一线城市每晚不超过 600 元", doc_title="") == (
        "A > 第二条 住宿标准\n一线城市每晚不超过 600 元"
    )
    assert retrieval_text("", "无标题", doc_title="") == "无标题"
    # 文档名进检索文本：同模板小节（「1. 目的」）靠文档名区分不同文件
    assert retrieval_text("1. 目的", "更好地服务客户。", doc_title="设备编号作业指导书") == (
        "设备编号作业指导书\n1. 目的\n更好地服务客户。"
    )


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


def test_build_context_skips_figure_chunks() -> None:
    """图片不进 LLM 上下文：只保留文字块编号，避免题注/占位污染答案。"""
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievedChunk

    chunks = [
        RetrievedChunk(
            chunk_id=1, doc_id=1, shard_id="kb_all_L1", content="计量表更换说明",
            chunk_type="parent", doc_title="工程汇报", page_from=8, page_to=9,
        ),
        RetrievedChunk(
            chunk_id=2, doc_id=1, shard_id="kb_all_L1", content="[图片]",
            chunk_type="figure", doc_title="工程汇报", page_from=8, page_to=8,
            image_path="parse/1/images/a.png",
        ),
    ]
    ctx = RetrievalPipeline.build_context(chunks)
    assert "【图片】" not in ctx
    assert "计量表更换说明" in ctx
    assert "[1]" in ctx
    assert "[2]" not in ctx
    assert "[图片]" not in ctx


def test_build_context_includes_doc_title_and_heading() -> None:
    """进 LLM 的资料段要带文档名 —— 同模板块（「1. 目的」）靠文档名区分。"""
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievedChunk

    chunks = [
        RetrievedChunk(
            chunk_id=1,
            doc_id=1,
            shard_id="kb_97486022_L2",
            content="满足公共设备管理需要。",
            heading_path="1. 目的",
            doc_title="工程部/1、程序文件/XJWY-GC-02设备编号作业指导书",
        )
    ]

    text = RetrievalPipeline.build_context(chunks)

    assert "XJWY-GC-02设备编号作业指导书" in text
    assert "1. 目的" in text
    assert "满足公共设备管理需要。" in text


async def test_below_threshold_chunks_never_reach_context(seeded) -> None:
    """**UI 标「低于阈值」的块不得进上下文**（用户实测表格叶子 35382=0.6025 仍被展开）。

    之前 ``min_score`` 只用于 top_score 拒答；keep=5 仍会取低分块，父块/整表扩展再把它
    放大后送 prompt。本用例：rerank 只给 1 个高分块，其余 0.05 → 上下文只能有高分块。
    """
    from ekbs.provider.base import RerankResult

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    original = runtime.pipeline.providers.rerank

    class _PokeyRerank:
        ref = original.ref

        async def rerank(self, query, docs, top_n):  # noqa: ANN001, ANN202
            results = [
                RerankResult(chunk_id=docs[0].chunk_id, score=0.9, shard_id=docs[0].shard_id)
            ]
            results.extend(
                RerankResult(chunk_id=doc.chunk_id, score=0.05, shard_id=doc.shard_id)
                for doc in docs[1:]
            )
            return results

    runtime.pipeline.providers.rerank = _PokeyRerank()  # type: ignore[assignment]
    try:
        result = await runtime.pipeline.retrieve("年假有多少天", alice, collect_debug=True)
    finally:
        runtime.pipeline.providers.rerank = original  # type: ignore[assignment]

    min_score = float(runtime.pipeline.settings.retrieval.rerank.min_score)
    assert result.refused is False, result.refusal_reason
    assert result.chunks
    assert all(
        chunk.rerank_score is None or float(chunk.rerank_score) >= min_score
        for chunk in result.chunks
    ), [(c.chunk_id, c.rerank_score) for c in result.chunks]
    dropped = (result.stage_debug.get("threshold_filter") or {}).get("dropped_chunk_ids") or []
    assert dropped, "低分块应被单块阈值过滤并记录在调试信息里"
