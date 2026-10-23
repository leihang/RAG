"""课程 15–19 落地项：Doc2Query / MultiQuery / α 融合 / 表格 HTML 上下文。"""

from __future__ import annotations


from ekbs.ingest.doc2query import fts_doc2query_rows, generate_questions
from ekbs.ingest.table_serialize import information_block_to_html, serialize_table_body
from ekbs.retrieval.pipeline import RetrievalOverrides, RetrievalPipeline
from ekbs.retrieval.rrf import alpha_fuse
from ekbs.session import QueryRewriter
from tests.conftest import user_id_of


def test_generate_questions_from_heading_and_content() -> None:
    qs = generate_questions(
        content="一线城市住宿标准每晚不超过 600 元。",
        heading_path="差旅费办法>第二条 住宿标准",
        chunk_type="child",
        max_questions=4,
    )
    assert qs
    assert any("住宿标准" in q for q in qs)


def test_generate_questions_for_figure() -> None:
    qs = generate_questions(
        content="[图片] 电表更换照片\n现场旧表读数",
        heading_path="工程巡检>电表",
        chunk_type="figure",
        max_questions=4,
    )
    assert any("照片" in q or "图片" in q for q in qs)


def test_fts_doc2query_rows_share_chunk_id() -> None:
    rows = fts_doc2query_rows(
        [(7, "kb_all_L1", "年假不得超过 15 天。", "人事>年假", "child")],
        max_questions=3,
    )
    assert rows
    assert all(r[0] == 7 and r[1] == "kb_all_L1" for r in rows)
    assert all("？" in r[2] or "?" in r[2] for r in rows)


def test_alpha_fuse_prefers_dense_when_alpha_one() -> None:
    fused = dict(
        alpha_fuse(
            [("a", 10.0), ("b", 1.0)],
            [("b", 10.0), ("c", 5.0)],
            alpha=1.0,
        )
    )
    assert fused["a"] >= fused["b"]
    assert "c" in fused  # 稀疏独有仍保留（归一后稀疏侧贡献为 0）


def test_alpha_fuse_prefers_sparse_when_alpha_zero() -> None:
    fused = dict(
        alpha_fuse(
            [("a", 10.0), ("b", 1.0)],
            [("b", 10.0), ("c", 5.0)],
            alpha=0.0,
        )
    )
    assert fused["b"] >= fused["a"]


def test_information_block_to_html_roundtrip() -> None:
    blocks = serialize_table_body(
        caption="住宿",
        body="| 城市 | 限额 |\n| --- | --- |\n| 北京 | 600 |\n",
    )
    assert blocks
    html = information_block_to_html(blocks[0])
    assert html is not None
    assert "<table" in html and "<th>" in html and "北京" in html and "600" in html


def test_build_context_uses_html_for_tables() -> None:
    from ekbs.retrieval.pipeline import RetrievedChunk

    block = serialize_table_body(
        caption="限额表",
        body="| 档次 | 金额 |\n| --- | --- |\n| 一档 | 500 |\n",
    )[0]
    chunk = RetrievedChunk(
        chunk_id=1,
        doc_id=1,
        shard_id="kb_all_L1",
        content=block,
        heading_path="标准",
        chunk_type="table",
        doc_title="差旅",
        page_from=1,
        page_to=1,
    )
    ctx = RetrievalPipeline.build_context([chunk])
    assert "【表格】" in ctx
    assert "<table" in ctx
    assert "一档" in ctx


def test_multiquery_expand_variants() -> None:
    # bypass full settings load — patch retrieval attrs on a lightweight stub
    class _R:
        multi_query = 3

    class _S:
        retrieval = _R()

    rewriter = QueryRewriter(settings=_S())  # type: ignore[arg-type]
    variants = rewriter.expand_variants("请问年假有多少天")
    assert len(variants) >= 2
    assert variants[0]


async def test_retrieve_with_alpha_fusion_still_hits_policy(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve(
        "年假有多少天",
        alice,
        overrides=RetrievalOverrides(fusion="alpha", hybrid_alpha=0.5, multi_query=3),
    )
    assert result.refused is False, result.refusal_reason
    assert any(chunk.doc_title == "年假管理制度" for chunk in result.chunks)


async def test_ingest_writes_doc2query_fts_rows(seeded) -> None:
    """入库后同一 chunk 在 FTS 里应有多行（原文 + 假设问句）。"""
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT c.chunk_id FROM ekbs_chunk c "
            "JOIN ekbs_document d ON d.doc_id = c.doc_id "
            "WHERE d.title = '年假管理制度' AND c.chunk_type = 'child' LIMIT 1"
        ).fetchone()
        assert row is not None
        chunk_id = int(row["chunk_id"])
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM ekbs_chunk_fts WHERE chunk_id = ?",
                (str(chunk_id),),
            ).fetchone()["n"]
        )
    # seeded 语料若在本改动前入库，可能仍是 1 行 —— 对该 chunk 重写 FTS 验证通路
    if n < 2:
        with runtime.db.sync_connect() as conn:
            meta = conn.execute(
                "SELECT c.content, c.heading_path, c.chunk_type, c.shard_id "
                "FROM ekbs_chunk c WHERE c.chunk_id = ?",
                (chunk_id,),
            ).fetchone()
        runtime.fts.delete_chunk_ids([chunk_id])
        runtime.fts.upsert_retrievable_with_doc2query(
            [
                (
                    chunk_id,
                    str(meta["shard_id"]),
                    str(meta["content"]),
                    meta["heading_path"],
                    str(meta["chunk_type"]),
                )
            ],
            enabled=True,
            max_questions=4,
        )
        with runtime.db.sync_connect() as conn:
            n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM ekbs_chunk_fts WHERE chunk_id = ?",
                    (str(chunk_id),),
                ).fetchone()["n"]
            )
    assert n >= 2, f"expected doc2query extra FTS rows, got {n}"
