"""检索插图扩展（正文命中 → 同文档 figure 并入上下文）。

这组方法此前**零测试**：正文命中但图没召回时，模型会因上下文无图而拒答，
或乱引其他文档的图（典型故障：问「计量表更换」要照片却答「资料区没有像素」）。

另覆盖超限表格父块展开：命中叶子 → 叶子保留在前 + 直系父段，不同组兄妹段。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ekbs.ingest.chunker import TableInput, count_tokens
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.retrieval.pipeline import (
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


async def test_expand_bound_figures_prefers_query_theme_page(seeded) -> None:
    """同一大 parent 下多图：问句主题所在页的图优先，不能按 seq 取文档开头几张。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    parent_id = _add_chunk(
        runtime, doc_id, shard, seq=920, content="跨页大父块", chunk_type="parent",
        page_from=10, page_to=16,
    )
    body_id = _add_chunk(
        runtime, doc_id, shard, seq=921,
        content=(
            "第11页\n1.能耗管理\n换表完成\n"
            "第14页\n2.设备运维\n4、冷却塔填料更换（按合同约定完成3台更换）\n"
            "第15页\n5、水泵检修"
        ),
        page_from=11, page_to=16, parent_id=parent_id,
    )
    early = _add_chunk(
        runtime, doc_id, shard, seq=922, content="[图片]", chunk_type="figure",
        page_from=11, page_to=11, figure_id="fig-early", image_path="parse/x/e.png",
        parent_id=parent_id,
    )
    theme = _add_chunk(
        runtime, doc_id, shard, seq=923, content="[图片]", chunk_type="figure",
        page_from=14, page_to=14, figure_id="fig-theme", image_path="parse/x/t.png",
        parent_id=parent_id, heading_path="2.设备运维（安防、工程）",
    )
    late = _add_chunk(
        runtime, doc_id, shard, seq=924, content="[图片]", chunk_type="figure",
        page_from=15, page_to=15, figure_id="fig-late", image_path="parse/x/l.png",
        parent_id=parent_id,
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard,
        content=(
            "第11页\n1.能耗管理\n换表完成\n"
            "第14页\n2.设备运维\n4、冷却塔填料更换（按合同约定完成3台更换）\n"
            "第15页\n5、水泵检修"
        ),
        page_from=11, page_to=16, rerank_score=0.9, parent_chunk_id=parent_id,
    )
    out = runtime.pipeline._expand_bound_figures(
        [anchor], alice, max_per_anchor=1, max_total=1, query="冷却塔填料是几月换的"
    )
    ids = [chunk.chunk_id for chunk in out if chunk.chunk_type == "figure"]
    assert ids == [theme], f"应优先第14页主题图，got={ids} early={early} late={late}"


async def test_expand_bound_figures_without_parent_is_noop(seeded) -> None:
    """锚点没有 parent_chunk_id → 无法绑图，原样返回。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    anchor = RetrievedChunk(
        chunk_id=9901, doc_id=1, shard_id="kb_all_L1", content="无父块正文",
    )
    assert runtime.pipeline._expand_bound_figures([anchor], alice) == [anchor]


async def test_expand_bound_figures_via_compat_aliases(seeded) -> None:
    """旧 expand_page/doc 入口转发到 parent 绑定展开。

    无页码时须章节 heading 命中问句主题（否则同 parent≈整篇，会乱挂封面图）。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    doc_id, shard = _visible_doc(runtime)
    parent_id = _add_chunk(
        runtime, doc_id, shard, seq=910, content="无页码父块", chunk_type="parent",
        heading_path="计量表更换",
    )
    body_id = _add_chunk(
        runtime, doc_id, shard, seq=911, content="无页码正文", parent_id=parent_id,
        heading_path="计量表更换",
    )
    fig_id = _add_chunk(
        runtime, doc_id, shard, seq=912, content="[图片]", chunk_type="figure",
        figure_id="fig-doc", image_path="parse/x/qr.png", parent_id=parent_id,
        heading_path="计量表更换流程",
    )
    anchor = RetrievedChunk(
        chunk_id=body_id, doc_id=doc_id, shard_id=shard, content="无页码正文",
        parent_chunk_id=parent_id, heading_path="计量表更换",
    )
    query = "计量表更换要照片"
    out = runtime.pipeline._expand_doc_figures([anchor], alice, query=query)
    assert fig_id in [chunk.chunk_id for chunk in out]
    out2 = runtime.pipeline._expand_page_figures([anchor], alice, query=query)
    assert fig_id in [chunk.chunk_id for chunk in out2]


def test_round_robin_expand_seeds_interleaves_by_leaf_rank() -> None:
    """多叶子扩展按轮转合并，避免 top-1 整表段独占前排、后续 keep 被 _fit_prompt 尾裁掉。"""

    def _seg(chunk_id: int, source: int) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk_id,
            doc_id=1,
            shard_id="s",
            content=f"seg-{chunk_id}",
            expanded_from=source,
        )

    # 叶子 A→3 段、叶子 B→2 段；串行会是 A1 A2 A3 B1 B2，轮转应为 A1 B1 A2 B2 A3
    merged = RetrievalPipeline._round_robin_expand_seeds(
        [
            [_seg(10, 1), _seg(11, 1), _seg(12, 1)],
            [_seg(20, 2), _seg(21, 2)],
        ]
    )
    assert [c.chunk_id for c in merged] == [10, 20, 11, 21, 12]


def test_expand_stage_debug_lists_collapsed_kept_leaves() -> None:
    """同表多叶子去重后，被折叠的 keep 叶子必须进 collapsed_kept_ids（调试台对账用）。"""

    kept = [
        RetrievedChunk(chunk_id=68816, doc_id=1, shard_id="s", content="leaf1", rerank_score=0.92),
        RetrievedChunk(chunk_id=65136, doc_id=1, shard_id="s", content="leaf2", rerank_score=0.86),
        RetrievedChunk(chunk_id=65132, doc_id=1, shard_id="s", content="leaf3", rerank_score=0.86),
    ]
    # 扩展+去重后只溯到前两片叶子（第三片同表折叠）
    context = [
        RetrievedChunk(
            chunk_id=68783, doc_id=1, shard_id="s", content="seg", expanded_from=68816
        ),
        RetrievedChunk(
            chunk_id=65122, doc_id=1, shard_id="s", content="seg", expanded_from=65136
        ),
    ]
    pipe = object.__new__(RetrievalPipeline)
    debug = pipe._expand_stage_debug(
        kept_leaves=kept,
        context_chunks=context,
        deduped=1,
        parent_expand=True,
    )
    assert debug["kept_leaf_ids"] == [68816, 65136, 65132]
    assert debug["collapsed_kept_ids"] == [65132]
    assert debug["represented_source_ids"] == [65136, 68816]
    assert len(debug["expanded"]) == 2
    assert debug["expanded"][0]["rerank_rank"] == 1


def test_prose_table_mode_parent_only_folds_tables_under_prose_parent() -> None:
    """正文父 + child + 多表叶：parent_only 只留父，避免叙事重复。"""
    prose = RetrievedChunk(
        chunk_id=100,
        doc_id=1,
        shard_id="s",
        content="方案正文含预算 22000 与节约 40848。[表格 1][表格 2]",
        chunk_type="parent",
        heading_path="方案",
    )
    child = RetrievedChunk(
        chunk_id=101,
        doc_id=1,
        shard_id="s",
        content="方案正文含预算",
        chunk_type="child",
        parent_chunk_id=100,
        heading_path="方案",
        rerank_score=0.9,
    )
    table_a = RetrievedChunk(
        chunk_id=102,
        doc_id=1,
        shard_id="s",
        content="表名: 表 1\n改造预算=22,000 元\n方案正文含预算",
        chunk_type="table",
        parent_chunk_id=100,
        heading_path="方案 · 表 1",
        rerank_score=0.8,
    )
    table_b = RetrievedChunk(
        chunk_id=103,
        doc_id=1,
        shard_id="s",
        content="表名: 表 2\n时段明细\n方案正文含预算",
        chunk_type="table",
        parent_chunk_id=100,
        heading_path="方案 · 表 2",
        rerank_score=0.7,
    )

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

    pipe = object.__new__(RetrievalPipeline)
    pipe.settings = SimpleNamespace(retrieval=SimpleNamespace(prose_table_mode="parent_only"))
    pipe.gate = _Gate()
    pipe.load_chunks = lambda ids: {prose.chunk_id: prose}

    out = pipe._expand_parents([child, table_a, table_b], SimpleNamespace(user_id=1))
    deduped, dropped = _dedupe_expanded(out)
    assert dropped >= 1
    assert [c.chunk_id for c in deduped] == [100]
    assert deduped[0].chunk_type == "parent"
    assert deduped[0].expanded_from in {101, 102, 103}


def test_attach_tables_under_prose_parents_always() -> None:
    """正文父进上下文后无条件补挂表叶（与问句意图无关；否则 LLM 只见 [表格 N]）。"""
    prose = RetrievedChunk(
        chunk_id=100,
        doc_id=1,
        shard_id="s",
        content="方案正文[表格 1]",
        chunk_type="parent",
    )
    table = RetrievedChunk(
        chunk_id=102,
        doc_id=1,
        shard_id="s",
        content="改造预算=22,000 元",
        chunk_type="table",
        parent_chunk_id=100,
    )

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

    class _DB:
        def sync_connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            assert "chunk_type = 'table'" in sql
            assert 100 in params

            class _Rows:
                def fetchall(self_inner):
                    return [{"chunk_id": 102, "doc_id": 1}]

            return _Rows()

    pipe = object.__new__(RetrievalPipeline)
    pipe.db = _DB()
    pipe.gate = _Gate()
    pipe.load_chunks = lambda ids: {102: table}
    out = pipe._attach_tables_under_prose_parents(
        [prose], SimpleNamespace(user_id=1), query="改造预算是多少"
    )
    assert [c.chunk_id for c in out] == [100, 102]
    assert out[1].chunk_type == "table"


def test_rank_tables_by_query_prefers_keyword_hits() -> None:
    from ekbs.retrieval.pipeline import _rank_tables_by_query

    low = RetrievedChunk(
        chunk_id=1, doc_id=1, shard_id="s", content="物业费收入 1月=1", chunk_type="table"
    )
    high = RetrievedChunk(
        chunk_id=2,
        doc_id=1,
        shard_id="s",
        content="六、工程费用; 2月=37500; 电梯维保费",
        chunk_type="table",
    )
    ordered = _rank_tables_by_query(
        [low, high], "2026年预算工程费用2月预算额度为多少"
    )
    assert ordered[0].chunk_id == 2


def test_select_tables_keeps_all_query_hits_beyond_max() -> None:
    """问句命中的工程费用×2月表叶不得被 max_tables 截掉。"""
    from ekbs.retrieval.pipeline import _select_tables_for_query, _table_must_keep

    query = "2026年预算工程费用2月预算额度为多少"
    must_rows = [
        RetrievedChunk(
            chunk_id=10 + i,
            doc_id=1,
            shard_id="s",
            content=f"六、工程费用; 科目{i}; 2月={1000 + i}",
            chunk_type="table",
        )
        for i in range(12)
    ]
    noise = [
        RetrievedChunk(
            chunk_id=100 + i,
            doc_id=1,
            shard_id="s",
            content=f"物业费收入 1月={i}",
            chunk_type="table",
        )
        for i in range(20)
    ]
    selected = _select_tables_for_query(must_rows + noise, query, max_tables=8)
    selected_ids = {c.chunk_id for c in selected}
    assert all(c.chunk_id in selected_ids for c in must_rows)
    assert all(_table_must_keep(c, query) for c in must_rows)
    assert len(selected) >= len(must_rows)


def test_select_tables_drops_zero_hit_siblings_under_cap() -> None:
    """≤ max_tables 时也不能把无关兄妹栏目整段灌进上下文。"""
    from ekbs.retrieval.pipeline import _select_tables_for_query

    query = "冷却塔填料是几月换的"
    hit = RetrievedChunk(
        chunk_id=1,
        doc_id=1,
        shard_id="s",
        content="当月能耗管理措施：中央空调冷却塔换热片更换",
        chunk_type="table",
    )
    noise = [
        RetrievedChunk(
            chunk_id=10 + i,
            doc_id=1,
            shard_id="s",
            content=f"用电分析（第{i}段）总电量 同比 环比",
            chunk_type="table",
        )
        for i in range(15)
    ]
    selected = _select_tables_for_query([hit, *noise], query, max_tables=40)
    assert hit in selected
    assert len(selected) < 10, f"无关栏目不应整表灌入: {len(selected)}"


def test_prioritize_context_puts_must_tables_first() -> None:
    from ekbs.retrieval.pipeline import _prioritize_context_chunks

    parent = RetrievedChunk(
        chunk_id=1, doc_id=1, shard_id="s", content="预算说明", chunk_type="parent"
    )
    must = RetrievedChunk(
        chunk_id=2,
        doc_id=1,
        shard_id="s",
        content="六、工程费用; 配电系统改造; 2月=9500",
        chunk_type="table",
    )
    other = RetrievedChunk(
        chunk_id=3, doc_id=1, shard_id="s", content="物业费 1月=1", chunk_type="table"
    )
    ordered = _prioritize_context_chunks(
        [parent, other, must], "2026年预算工程费用2月预算额度为多少"
    )
    assert [c.chunk_id for c in ordered] == [2, 1, 3]


def test_prefer_table_chunks_for_snippets_orders_tables_first() -> None:
    from ekbs.retrieval.pipeline import _prefer_table_chunks_for_snippets

    parent = RetrievedChunk(
        chunk_id=1, doc_id=1, shard_id="s", content="父[表格 1]", chunk_type="parent"
    )
    table = RetrievedChunk(
        chunk_id=2, doc_id=1, shard_id="s", content="改造预算=1", chunk_type="table"
    )
    child = RetrievedChunk(
        chunk_id=3, doc_id=1, shard_id="s", content="子", chunk_type="child"
    )
    ordered = _prefer_table_chunks_for_snippets([parent, child, table], limit=8)
    assert [c.chunk_id for c in ordered] == [2, 1, 3]


def test_prose_table_mode_leaf_only_keeps_tables_drops_prose_parent() -> None:
    prose = RetrievedChunk(
        chunk_id=100, doc_id=1, shard_id="s", content="父文", chunk_type="parent"
    )
    child = RetrievedChunk(
        chunk_id=101,
        doc_id=1,
        shard_id="s",
        content="子",
        chunk_type="child",
        parent_chunk_id=100,
        rerank_score=0.9,
    )
    table = RetrievedChunk(
        chunk_id=102,
        doc_id=1,
        shard_id="s",
        content="表叶",
        chunk_type="table",
        parent_chunk_id=100,
        rerank_score=0.8,
    )

    class _Gate:
        def filter_chunks(self, perm, pairs):
            return list(pairs), []

    pipe = object.__new__(RetrievalPipeline)
    pipe.settings = SimpleNamespace(retrieval=SimpleNamespace(prose_table_mode="leaf_only"))
    pipe.gate = _Gate()
    pipe.load_chunks = lambda ids: {prose.chunk_id: prose}
    out = pipe._expand_parents([child, table], SimpleNamespace(user_id=1))
    assert [c.chunk_id for c in out] == [102]
    assert out[0].chunk_type == "table"


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


# --- 超限表格：分段容器 → 命中叶子只扩直系父段 ---------------------------------


async def test_expand_oversized_table_only_direct_parent(seeded) -> None:
    """命中分段表的叶子 → 叶子 + 直系父段，不同组其它父段。"""
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
    own_parent = int(leaf["parent_id"])
    anchor = RetrievedChunk(
        chunk_id=int(leaf["chunk_id"]),
        doc_id=ingested.doc_id,
        shard_id=ingested.shard_id,
        content="",
        chunk_type="table",
        parent_chunk_id=own_parent,
        heading_path="制度 > 5. 工作程序和要求",
        rerank_score=0.9,
        rrf_score=0.5,
        dense_rank=1,
        sparse_rank=2,
    )
    out = runtime.pipeline._expand_parents([anchor], alice)
    assert [chunk.chunk_id for chunk in out] == [
        int(leaf["chunk_id"]),
        own_parent,
    ], "表格：命中叶子保留在前 + 直系父段；不得扩同组兄妹段"
    assert out[0].expanded_from is None
    assert out[1].expanded_from == anchor.chunk_id
    assert out[1].heading_path == anchor.heading_path
    assert out[1].rerank_score == 0.9

    other = next(row for row in leaf_rows if int(row["parent_id"]) != own_parent)
    other_parent = int(other["parent_id"])
    anchor2 = RetrievedChunk(
        chunk_id=int(other["chunk_id"]),
        doc_id=ingested.doc_id,
        shard_id=ingested.shard_id,
        content="",
        chunk_type="table",
        parent_chunk_id=other_parent,
        heading_path="制度 > 5. 工作程序和要求",
        rerank_score=0.7,
    )
    expanded_both = runtime.pipeline._expand_parents([anchor, anchor2], alice)
    # 轮转：L1, L2, P1, P2
    assert [c.chunk_id for c in expanded_both] == [
        int(leaf["chunk_id"]),
        int(other["chunk_id"]),
        own_parent,
        other_parent,
    ]
    deduped, dropped = _dedupe_expanded(expanded_both)
    assert dropped == 0
    assert {c.chunk_id for c in deduped} == {
        int(leaf["chunk_id"]),
        int(other["chunk_id"]),
        own_parent,
        other_parent,
    }
    by_id = {c.chunk_id: c for c in deduped}
    assert by_id[own_parent].expanded_from == anchor.chunk_id
    assert by_id[other_parent].expanded_from == anchor2.chunk_id
    assert set(segment_ids) - {own_parent, other_parent}, "仍存在未展开的兄妹段（对照）"

def test_expand_compare_peers_only_on_compare_intent_and_respects_budget() -> None:
    """比较意图才按 (doc,年,月) 补齐 tidy 同行；条数/token 有上限。"""
    from ekbs.retrieval.pipeline import PEER_EXPAND_MAX_LEAVES
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
