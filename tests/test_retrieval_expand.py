"""检索插图扩展（正文命中 → 同文档 figure 并入上下文）。

这组方法此前**零测试**：正文命中但图没召回时，模型会因上下文无图而拒答，
或乱引其他文档的图（典型故障：问「计量表更换」要照片却答「资料区没有像素」）。

另覆盖超限表格的**分段展开**（task-7）：命中任一叶子 → 同一 ``table_group`` 的全部
≤2048 分段按 seq 进上下文；超预算时先保命中段、再按 seq 补，末尾注明截断。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ekbs.ingest.chunker import TableInput, count_tokens
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.retrieval.pipeline import (
    TABLE_EXPAND_MAX_TOKENS,
    RetrievalPipeline,
    RetrievedChunk,
    _dedupe_expanded,
)
from tests.conftest import user_id_of


def _oversized_table_body(min_tokens: int = 4000) -> str:
    """构造序列化后 ≥``min_tokens`` 的 3 列表（长说明文字），用于触发容器分段。"""
    from ekbs.ingest.table_serialize import serialize_table_body

    rows: list[str] = []
    body = ""
    total = 0
    while total < min_tokens:
        index = len(rows)
        rows.append(
            f"| 项目{index} | 与年度能耗管理计划相关的说明文字第{index}条，"
            f"需记录责任人及时间节点并确保执行到位 | {index} |"
        )
        body = "| 项目 | 说明 | 数值 |\n| --- | --- | --- |\n" + "\n".join(rows)
        total = sum(
            count_tokens(block)
            for block in serialize_table_body(caption="大表", body=body, table_id="1")
        )
    return body


def _ingest_pipeline(runtime) -> IngestPipeline:
    return IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )


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
    parent_id: int | None = None,
    heading_path: str = "",
) -> int:
    with runtime.db.sync_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, seq, content, shard_id, chunk_type, "
            "page_from, page_to, figure_id, image_path, parent_id, heading_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, seq, content, shard_id, chunk_type,
             page_from, page_to, figure_id, image_path, parent_id, heading_path),
        )
        conn.commit()
        return int(cursor.lastrowid)


async def test_expand_bound_figures_by_parent(seeded) -> None:
    """正文 child 命中 → 同 parent 绑定的图并入；其它 parent 下的图不并入。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    parent_near = _add_chunk(
        runtime, doc_id, shard, seq=900, content="近节父块", chunk_type="parent",
        page_from=3, page_to=4,
    )
    parent_far = _add_chunk(
        runtime, doc_id, shard, seq=901, content="远节父块", chunk_type="parent",
        page_from=9, page_to=9,
    )
    body_id = _add_chunk(
        runtime, doc_id, shard, seq=902, content="正文第3-4页",
        page_from=3, page_to=4, parent_id=parent_near,
    )
    near = _add_chunk(
        runtime, doc_id, shard, seq=903, content="[图片]", chunk_type="figure",
        page_from=4, page_to=4, figure_id="fig-near", image_path="parse/x/near.png",
        parent_id=parent_near,
    )
    far = _add_chunk(
        runtime, doc_id, shard, seq=904, content="[图片]", chunk_type="figure",
        page_from=9, page_to=9, figure_id="fig-far", image_path="parse/x/far.png",
        parent_id=parent_far,
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard, content="正文",
        page_from=3, page_to=4, rerank_score=0.87, parent_chunk_id=parent_near,
    )
    out = runtime.pipeline._expand_bound_figures([anchor], alice)
    ids = [chunk.chunk_id for chunk in out]
    assert near in ids, "同 parent 绑定的图必须并入"
    assert far not in ids, "其它 parent 下的图不得并入"
    got = next(chunk for chunk in out if chunk.chunk_id == near)
    assert got.rerank_score == pytest.approx(0.87 * 0.9)


async def test_expand_bound_figures_without_parent_is_noop(seeded) -> None:
    """锚点没有 parent_chunk_id → 无法绑图，原样返回。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    anchor = RetrievedChunk(
        chunk_id=9901, doc_id=1, shard_id="kb_all_L1", content="无父块正文",
    )
    assert runtime.pipeline._expand_bound_figures([anchor], alice) == [anchor]


async def test_expand_bound_figures_via_compat_aliases(seeded) -> None:
    """旧 expand_page/doc 入口转发到 parent 绑定展开。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    parent_id = _add_chunk(
        runtime, doc_id, shard, seq=910, content="无页码父块", chunk_type="parent",
    )
    body_id = _add_chunk(
        runtime, doc_id, shard, seq=911, content="无页码正文", parent_id=parent_id,
    )
    fig_id = _add_chunk(
        runtime, doc_id, shard, seq=912, content="[图片]", chunk_type="figure",
        figure_id="fig-doc", image_path="parse/x/qr.png", parent_id=parent_id,
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard, content="无页码正文",
        parent_chunk_id=parent_id,
    )
    out = runtime.pipeline._expand_doc_figures([anchor], alice)
    assert fig_id in [chunk.chunk_id for chunk in out]
    out2 = runtime.pipeline._expand_page_figures([anchor], alice)
    assert fig_id in [chunk.chunk_id for chunk in out2]


async def test_expand_parents_records_ranked_source_and_dedupes(seeded) -> None:
    """父块扩展要留「精排来源」，去重后保留分数最高的那个来源（doc 205 的 35274 ← 35277）。

    父块本身**不在精排候选里**（精排只给检索单元打分）；旧实现把它换进上下文后，
    审计/调试台只能看到一个从没被精排选中的 chunk_id，回源与精排数据对不上。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    parent_id = _add_chunk(
        runtime, doc_id, shard, seq=920, content="整篇正文父块", chunk_type="parent",
        heading_path="5. 工作程序和要求",
    )
    leaf_hi = _add_chunk(
        runtime, doc_id, shard, seq=921, content="5.1 能耗管理员职责",
        parent_id=parent_id, heading_path="5. 工作程序和要求 > 5.1 能耗管理员职责",
    )
    leaf_lo = _add_chunk(
        runtime, doc_id, shard, seq=922, content="5.2 能耗统计",
        parent_id=parent_id, heading_path="5. 工作程序和要求 > 5.2 能耗统计",
    )

    def _leaf(chunk_id: int, heading: str, score: float) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk_id, doc_id=doc_id, shard_id=shard, content="叶子",
            parent_chunk_id=parent_id, heading_path=heading, rerank_score=score,
        )

    out = runtime.pipeline._expand_parents(
        [_leaf(leaf_hi, "5. 工作程序和要求 > 5.1 能耗管理员职责", 0.9),
         _leaf(leaf_lo, "5. 工作程序和要求 > 5.2 能耗统计", 0.8)],
        alice,
    )
    assert [c.chunk_id for c in out] == [parent_id, parent_id], "两个叶子都换成父块"
    assert [c.expanded_from for c in out] == [leaf_hi, leaf_lo], "必须记住各自的精排来源"
    assert out[0].heading_path.endswith("5.1 能耗管理员职责"), "heading 仍取命中叶子的条款层"

    deduped, dropped = _dedupe_expanded(out)
    assert dropped == 1
    assert len(deduped) == 1
    assert deduped[0].chunk_id == parent_id
    assert deduped[0].expanded_from == leaf_hi, "保留首次出现 = 精排分最高的来源"
    assert deduped[0].rerank_score == 0.9


def test_post_check_citations_carries_source_chunk_id() -> None:
    """引用同时带上下文块 id 与精排来源：chunk_id=父块，source_chunk_id=叶子。"""
    from types import SimpleNamespace

    from ekbs.retrieval.pipeline import RetrievalPipeline

    expanded_parent = RetrievedChunk(
        chunk_id=35274, doc_id=205, shard_id="s", content="父块正文",
        heading_path="5. 工作程序和要求 > 5.1 能耗管理员职责", page_from=3, page_to=3,
        chunk_type="parent", rerank_score=0.899, expanded_from=35277,
    )
    plain_child = RetrievedChunk(
        chunk_id=35276, doc_id=205, shard_id="s", content="直接命中的块",
        heading_path="5. 工作程序和要求 > 5.2 能耗统计", page_from=4, page_to=4,
        rerank_score=0.813,
    )

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

        def record_security_event(self, *args, **kwargs):
            return None

    pipe = object.__new__(RetrievalPipeline)
    pipe.gate = _Gate()  # type: ignore[attr-defined]
    citations = pipe._post_check_citations(
        "[1][2]",
        [expanded_parent, plain_child],
        SimpleNamespace(user_id=1),  # type: ignore[arg-type]
    )
    assert citations[0].chunk_id == 35274, "chunk_id 仍是喂给 LLM 的上下文块"
    assert citations[0].source_chunk_id == 35277, "精排来源（叶子）必须可追溯"
    assert citations[0].heading_path.endswith("5.1 能耗管理员职责")
    assert citations[1].chunk_id == 35276
    assert citations[1].source_chunk_id is None, "未扩展的块自己就是精排候选"


# --- 超限表格：分段容器 → 命中任一叶子展开整表（task-7）--------------------------


async def test_expand_oversized_table_returns_all_segments(seeded) -> None:
    """命中分段表的叶子 → 同一 ``table_group`` 的全部分段按 seq 顺序进上下文。"""
    runtime, summary = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    body = _oversized_table_body(4000)
    ingested = await _ingest_pipeline(runtime).ingest_markdown(
        title="超限表格回归",
        markdown="# 制度\n\n## 5. 工作程序和要求\n\n[表格 1]\n\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        tables=[TableInput(body=body, table_id="1", caption="大表")],
    )
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, parent_id, seq, table_group FROM ekbs_chunk "
            "WHERE doc_id = ? AND chunk_type = 'table' ORDER BY seq",
            (ingested.doc_id,),
        ).fetchall()
    segment_rows = [row for row in rows if row["table_group"]]
    assert len(segment_rows) > 1, "本用例前提：整表父容器被分成多段"
    segment_ids = [int(row["chunk_id"]) for row in segment_rows]
    segment_id_set = set(segment_ids)
    leaf_rows = [
        row
        for row in rows
        if row["table_group"] is None and row["parent_id"] in segment_id_set
    ]
    assert leaf_rows, "每个分段都必须有叶子"
    leaf = leaf_rows[0]
    anchor = RetrievedChunk(
        chunk_id=int(leaf["chunk_id"]),
        doc_id=ingested.doc_id,
        shard_id=ingested.shard_id,
        content="",
        chunk_type="table",
        parent_chunk_id=int(leaf["parent_id"]),
        heading_path="制度 > 5. 工作程序和要求",
        rerank_score=0.9,
        rrf_score=0.5,
        dense_rank=1,
        sparse_rank=2,
    )
    out = runtime.pipeline._expand_parents([anchor], alice)
    assert [chunk.chunk_id for chunk in out] == segment_ids, "整表全部分段按 seq 顺序展开"
    assert all(chunk.expanded_from == anchor.chunk_id for chunk in out), "引用可回溯到命中叶子"
    assert all(chunk.heading_path == anchor.heading_path for chunk in out)
    assert all(chunk.rerank_score == 0.9 for chunk in out)
    total_tokens = sum(count_tokens(chunk.content) for chunk in out)
    assert total_tokens <= TABLE_EXPAND_MAX_TOKENS, "未超预算时不应截断"
    assert "仅展开" not in out[-1].content

    # 同一张表的另一个叶子也命中：去重后整表只出现一次，保留首次出现（最高分来源）
    other = leaf_rows[-1]
    anchor2 = RetrievedChunk(
        chunk_id=int(other["chunk_id"]),
        doc_id=ingested.doc_id,
        shard_id=ingested.shard_id,
        content="",
        chunk_type="table",
        parent_chunk_id=int(other["parent_id"]),
        heading_path="制度 > 5. 工作程序和要求",
        rerank_score=0.7,
    )
    expanded_both = runtime.pipeline._expand_parents([anchor, anchor2], alice)
    assert len(expanded_both) == len(segment_ids) * 2, "两个叶子各自展开整表"
    deduped, dropped = _dedupe_expanded(expanded_both)
    assert dropped == len(segment_ids)
    assert [chunk.chunk_id for chunk in deduped] == segment_ids
    assert all(chunk.expanded_from == anchor.chunk_id for chunk in deduped)


def test_expand_compare_peers_only_on_compare_intent_and_respects_budget() -> None:
    """比较意图才按 (doc,年,月) 补齐 tidy 同行；条数/token 有上限。"""
    from ekbs.retrieval.pipeline import PEER_EXPAND_MAX_LEAVES, RetrievalPipeline
    from ekbs.retrieval.table_rank import is_compare_rank_intent

    assert is_compare_rank_intent("9月谁更高")
    assert not is_compare_rank_intent("9月用电量是多少")

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

    def _leaf(chunk_id: int, entity: str, value: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk_id,
            doc_id=7,
            shard_id="s",
            chunk_type="table",
            parent_chunk_id=1,
            content=(
                "表名: 大户用电监视\n相关表头: 用户名 | 年度 | 月份 | 用电量\n"
                f"用户名={entity}; 年度=2026; 月份=9; 用电量={value}"
            ),
            rerank_score=0.9,
        )

    hit = _leaf(10, "甲", "100")
    peers = [_leaf(100 + i, f"商户{i}", str(1000 + i)) for i in range(5)]

    pipe = object.__new__(RetrievalPipeline)
    pipe.gate = _Gate()  # type: ignore[attr-defined]
    pipe.db = SimpleNamespace()  # type: ignore[attr-defined]

    class _Conn:
        def execute(self, sql, params):
            if "FROM ekbs_document" in sql:
                return SimpleNamespace(
                    fetchone=lambda: {"title": "工程部/2026年9月能耗分析"}
                )
            return SimpleNamespace(
                fetchall=lambda: [{"chunk_id": p.chunk_id} for p in peers]
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    pipe.db.sync_connect = lambda: _Conn()  # type: ignore[attr-defined]
    pipe.load_chunks = lambda ids: {p.chunk_id: p for p in peers if p.chunk_id in ids}  # type: ignore[attr-defined]

    out = pipe._expand_compare_peers([hit], SimpleNamespace(user_id=1), "9月用电量谁更高")
    assert len(out) == 1 + len(peers)
    assert {c.chunk_id for c in out} == {10, *[p.chunk_id for p in peers]}
    assert all(c.rerank_score == 0.9 for c in out[1:])
    assert len(out) - 1 <= PEER_EXPAND_MAX_LEAVES

    unchanged = pipe._expand_compare_peers(
        [hit], SimpleNamespace(user_id=1), "9月用电量是多少"
    )
    assert unchanged == [hit]


def test_expand_table_group_truncates_over_budget_and_keeps_hit_segment() -> None:
    """分段总量超 ``TABLE_EXPAND_MAX_TOKENS``：先保命中段，再按 seq 补，末尾注明截断。"""

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

        def record_security_event(self, *args, **kwargs):
            return None

    def _segment(chunk_id: int) -> RetrievedChunk:
        content = "表名: 整表分段\n"
        while count_tokens(content) < 3000:
            content += "项目=项目0; 说明=用于验证分段预算与命中优先级的占位说明文字。\n"
        return RetrievedChunk(
            chunk_id=chunk_id,
            doc_id=1,
            shard_id="s",
            content=content,
            chunk_type="table",
            heading_path="制度 > 5. 工作程序和要求",
            table_group="t1",
        )

    segments = [_segment(chunk_id) for chunk_id in (1, 2, 3, 4)]
    pipe = object.__new__(RetrievalPipeline)
    pipe.gate = _Gate()  # type: ignore[attr-defined]
    pipe._table_group_segments = lambda doc_id, group: segments  # type: ignore[attr-defined]
    source = RetrievedChunk(
        chunk_id=99,
        doc_id=1,
        shard_id="s",
        content="命中叶子",
        chunk_type="table",
        parent_chunk_id=4,
        heading_path="制度 > 5. 工作程序和要求",
        rerank_score=0.88,
        rrf_score=0.4,
        dense_rank=3,
        sparse_rank=4,
    )
    out = pipe._expand_table_group(segments[-1], source, SimpleNamespace(user_id=1))

    assert len(out) == 2, "每段 ~3000 token，8192 预算只够 2 段"
    assert [chunk.chunk_id for chunk in out] == [1, 4], "命中段 4 必须保留，最终按 seq 排序"
    assert all(chunk.expanded_from == source.chunk_id for chunk in out)
    assert all(chunk.rerank_score == 0.88 for chunk in out)
    included_tokens = sum(
        count_tokens(chunk.content.split("\n\n（整表共")[0]) for chunk in out
    )
    assert included_tokens <= TABLE_EXPAND_MAX_TOKENS
    note = out[-1].content
    assert "整表共 4 段" in note and "仅展开 2 段" in note, "截断必须在最后一段正文里注明"
