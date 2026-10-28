"""检索插图扩展（正文命中 → 同文档 figure 并入上下文）。

这组方法此前**零测试**：正文命中但图没召回时，模型会因上下文无图而拒答，
或乱引其他文档的图（典型故障：问「计量表更换」要照片却答「资料区没有像素」）。
"""

from __future__ import annotations

from ekbs.retrieval.pipeline import RetrievedChunk
from tests.conftest import user_id_of


def _visible_doc(runtime, title: str = "员工手册（总则）") -> tuple[int, str]:
    """取一份 demo 语料里肯定可见的文档，连同它的分片。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id FROM ekbs_document WHERE title = ?", (title,)
        ).fetchone()
        assert row is not None, f"seed 语料里没有 {title}"
        doc_id = int(row["doc_id"])
        shard_row = conn.execute(
            "SELECT shard_id FROM ekbs_chunk WHERE doc_id = ? LIMIT 1", (doc_id,)
        ).fetchone()
        assert shard_row is not None
    return doc_id, str(shard_row["shard_id"])


def _add_chunk(
    runtime,
    doc_id: int,
    shard_id: str,
    *,
    seq: int,
    content: str,
    chunk_type: str = "child",
    page_from: int | None = None,
    page_to: int | None = None,
    figure_id: str | None = None,
    image_path: str | None = None,
) -> int:
    with runtime.db.sync_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, seq, content, shard_id, chunk_type, "
            "page_from, page_to, figure_id, image_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, seq, content, shard_id, chunk_type,
             page_from, page_to, figure_id, image_path),
        )
        conn.commit()
        return int(cursor.lastrowid)


async def test_expand_page_figures_by_page_overlap(seeded) -> None:
    """正文页 3-4 命中 → 同文档 page 4 的图被并入；page 9 的图不并入。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    body_id = _add_chunk(
        runtime, doc_id, shard, seq=900, content="正文第3-4页", page_from=3, page_to=4
    )
    near = _add_chunk(
        runtime, doc_id, shard, seq=901, content="图:电表", chunk_type="figure",
        page_from=4, page_to=4, figure_id="fig-near", image_path="parse/x/near.png",
    )
    far = _add_chunk(
        runtime, doc_id, shard, seq=902, content="图:远页", chunk_type="figure",
        page_from=9, page_to=9, figure_id="fig-far", image_path="parse/x/far.png",
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard, content="正文",
        page_from=3, page_to=4, rerank_score=0.87,
    )
    out = runtime.pipeline._expand_page_figures([anchor], alice)
    ids = [chunk.chunk_id for chunk in out]
    assert near in ids, "页码重叠的图必须并入上下文"
    assert far not in ids, "页码不相交的图不得并入"
    got = next(chunk for chunk in out if chunk.chunk_id == near)
    assert got.rerank_score == 0.87, "插图应继承锚点的精排分，便于就近引用"


async def test_expand_page_figures_no_page_anchor_is_noop(seeded) -> None:
    """锚点没有 page_from（docx 正文）→ 原样返回，交给 _expand_doc_figures。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    anchor = RetrievedChunk(
        chunk_id=9901, doc_id=doc_id, shard_id=shard, content="无页码正文",
    )
    assert runtime.pipeline._expand_page_figures([anchor], alice) == [anchor]


async def test_expand_doc_figures_by_doc(seeded) -> None:
    """正文无页码时，同文档 figure 仍被并入（docx 常见，否则「扫码」问不到图）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    body_id = _add_chunk(runtime, doc_id, shard, seq=910, content="无页码正文")
    fig_id = _add_chunk(
        runtime, doc_id, shard, seq=911, content="图:二维码", chunk_type="figure",
        figure_id="fig-doc", image_path="parse/x/qr.png",
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard, content="无页码正文",
    )
    out = runtime.pipeline._expand_doc_figures([anchor], alice)
    assert fig_id in [chunk.chunk_id for chunk in out]
