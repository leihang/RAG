"""文档标题命中优先级（④.5）：问句点名某份文档时，同模板的其它文档块必须让位。

真实场景：问「维修作业指导书的生效日期和截止日期」时，4 份文件的「文件信息」
块几乎逐字相同，mock/云端精排分都能过阈值（实测 0.9486 / 0.7531 / 0.7175 /
0.6627）—— 上下文混进另外三份文件的信息块，答案就可能引用错文件。

本模块用**同模板、仅标题不同**的合成语料复现该形态（seeded runtime + mock
provider，不出网）：命中标题后只保留被点名文档的候选，且 top_score 来自该文档
自己的精排分（不被别人的分数抬高/掩盖）。
"""

from __future__ import annotations

import pytest

from ekbs.retrieval import pipeline as pipeline_mod
from ekbs.retrieval.pipeline import (
    RetrievedChunk,
    _title_match_doc_ids,
    _title_match_keys,
)
from tests.conftest import user_id_of

#: 同模板正文：四份文件除了标题/编号/核心名，其余逐字相同（复现「文件信息」块互撞）。
_TEMPLATE = """# {title}

文件编号：{code}
生效日期：2026-01-01
截止日期：2026-12-31

## 文件信息

本文件规定了{core}的编制、审批与归档要求。

## 1. 目的

为规范{core}相关工作，保证作业安全与质量。
"""

#: (文档标题, 文档编号, 核心名)
_DOCS = [
    ("XJWY-GC-09维修作业指导书", "XJWY-GC-09", "维修作业指导书"),
    ("XJWY-GC-13装饰拆除作业指导书", "XJWY-GC-13", "装饰拆除作业指导书"),
    ("XJWY-GC-14电力增容作业指导书", "XJWY-GC-14", "电力增容作业指导书"),
    ("XJWY-GC-15能耗管理作业指导书", "XJWY-GC-15", "能耗管理作业指导书"),
]

_REPAIR_TITLE = _DOCS[0][0]
_DEMOLISH_TITLE = _DOCS[1][0]


def _chunk(cid: int, doc_id: int, title: str) -> RetrievedChunk:
    """构造只用于匹配判断的最小候选块（不落库、不进检索）。"""
    return RetrievedChunk(
        chunk_id=cid,
        doc_id=doc_id,
        shard_id="kb_all_L1",
        content="文件信息",
        doc_title=title,
    )


# ---------------------------------------------------------------------------
# 匹配算法本身（不需要 runtime）
# ---------------------------------------------------------------------------


def test_title_keys_strip_path_code_and_draft_marker() -> None:
    """标题 → (全名, 核心名, 编号)：去导入路径前缀、剥文档编号、去「- 定稿」。"""
    assert _title_match_keys("工程部/1、程序文件/XJWY-GC-02设备编号作业指导书") == (
        "xjwygc02设备编号作业指导书",
        "设备编号作业指导书",
        "xjwygc02",
    )
    assert _title_match_keys("XJWY-GC-09 维修作业指导书 - 定稿") == (
        "xjwygc09维修作业指导书",
        "维修作业指导书",
        "xjwygc09",
    )
    # 定稿标记也可能在开头；扩展名不应污染编号/核心名切分
    assert _title_match_keys("（定稿）XJWY-GC-09维修作业指导书") == (
        "xjwygc09维修作业指导书",
        "维修作业指导书",
        "xjwygc09",
    )
    assert _title_match_keys("demo/service.py") == ("service", "service", "")


def test_title_match_uses_core_title_or_doc_code() -> None:
    """核心名（去编号）或编号出现在问句里 → 点名该文档；全名整串出现也算。"""
    chunks = [
        _chunk(1, 10, "工程部/1、程序文件/XJWY-GC-09维修作业指导书"),
        _chunk(2, 11, "XJWY-GC-13装饰拆除作业指导书"),
    ]
    assert _title_match_doc_ids("维修作业指导书的生效日期和截止日期", chunks) == {10}
    assert _title_match_doc_ids("XJWY-GC-13 的生效日期", chunks) == {11}
    # 编号里的分隔符/空格不敏感
    assert _title_match_doc_ids("xjwy gc 13 的生效日期", chunks) == {11}
    assert _title_match_doc_ids("XJWY-GC-09维修作业指导书什么时候生效", chunks) == {10}
    # 两份都点名 → 并集
    assert _title_match_doc_ids("维修作业指导书和装饰拆除作业指导书各何时生效", chunks) == {10, 11}


def test_title_match_ignores_generic_suffix_and_short_titles() -> None:
    """只提通用体裁词（作业指导书）不算点名；短标题（< 4）不参与匹配。"""
    chunks = [
        _chunk(1, 10, "XJWY-GC-09维修作业指导书"),
        _chunk(2, 11, "XJWY-GC-13装饰拆除作业指导书"),
        _chunk(3, 12, "XJWY-GC-20作业指导书"),  # 标题本身只有通用词
        _chunk(4, 13, "年假"),  # 短标题
    ]
    assert _title_match_doc_ids("作业指导书的编制要求是什么", chunks) == set()
    assert _title_match_doc_ids("作业指导书", chunks) == set()
    assert _title_match_doc_ids("年假有多少天", chunks) == set()
    # 与点名无关的问句同样返回空集
    assert _title_match_doc_ids("生效日期和截止日期是什么", chunks) == set()


# ---------------------------------------------------------------------------
# 检索链路（seeded runtime + mock provider，不出网）
# ---------------------------------------------------------------------------


@pytest.fixture
async def title_corpus(seeded):
    """seeded 语料 + 4 份同模板文档（仅标题/编号不同），返回 (runtime, 标题→doc_id, carol)。"""
    from ekbs.ingest.pipeline import IngestPipeline

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    doc_ids: dict[str, int] = {}
    for title, code, core in _DOCS:
        result = await pipeline.ingest_markdown(
            title=title,
            markdown=_TEMPLATE.format(title=title, code=code, core=core),
            kb_id=int(summary["kb_id"]),
            level=1,
            visible_dept_ids=[],
            source_path=f"demo://{title}",
            real_corpus=False,
        )
        doc_ids[title] = int(result.doc_id)
    # carol 只有 L1 公共分片可见：4 份 L1 文档与她都在同一分片里，规避跨分片噪声
    carol = await runtime.perm_context(user_id_of(runtime.db, "demo_carol"))
    return runtime, doc_ids, carol


def _chunk_ids_of_doc(runtime, doc_id: int) -> set[int]:
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (int(doc_id),)
        ).fetchall()
    return {int(row["chunk_id"]) for row in rows}


async def test_core_title_query_keeps_only_named_document(title_corpus) -> None:
    """核心名命中 → 只剩点名文档的块，top_score 仍是该文档自己的精排最高分。"""
    runtime, doc_ids, carol = title_corpus
    named = doc_ids[_REPAIR_TITLE]

    result = await runtime.pipeline.retrieve(
        "维修作业指导书的生效日期和截止日期", carol, collect_debug=True
    )

    assert result.refused is False, result.refusal_reason
    assert result.chunks, "点名文档本身必须有块进上下文"
    assert {chunk.doc_id for chunk in result.chunks} == {named}, (
        f"同模板的其它文档块没被过滤：{[(c.doc_id, c.doc_title) for c in result.chunks]}"
    )

    debug = result.stage_debug["title_match"]
    assert debug["matched_doc_ids"] == [named]
    assert debug["before"] > debug["after"] == len(debug["matched_doc_ids"]), debug

    # top_score 不能被别的文档「抬高」或「掩盖」：等于点名文档候选里的最高精排分
    scores = {
        int(item["chunk_id"]): float(item["rerank_score"])
        for item in result.stage_debug["rerank"]["scores"]
        if item["rerank_score"] is not None
    }
    named_scores = [scores[cid] for cid in _chunk_ids_of_doc(runtime, named) if cid in scores]
    assert named_scores, "点名文档必须进过精排（否则这条断言没有意义）"
    assert result.top_score == pytest.approx(max(named_scores))


async def test_doc_code_query_keeps_only_named_document(title_corpus) -> None:
    """问句只提文档编号（XJWY-GC-13）也应按编号点名过滤。"""
    runtime, doc_ids, carol = title_corpus
    named = doc_ids[_DEMOLISH_TITLE]

    result = await runtime.pipeline.retrieve(
        "XJWY-GC-13 的生效日期和截止日期", carol, collect_debug=True
    )

    assert result.refused is False, result.refusal_reason
    assert {chunk.doc_id for chunk in result.chunks} == {named}
    assert result.stage_debug["title_match"]["matched_doc_ids"] == [named]


async def test_query_without_title_match_does_not_filter(title_corpus, monkeypatch) -> None:
    """问句没点名任何文档时，候选集合必须与「不过滤」逐块一致。"""
    runtime, doc_ids, carol = title_corpus
    question = "生效日期和截止日期是什么"

    filtered = await runtime.pipeline.retrieve(question, carol, collect_debug=True)
    assert filtered.stage_debug["title_match"]["matched_doc_ids"] == []
    assert filtered.stage_debug["title_match"]["before"] == filtered.stage_debug["title_match"]["after"]

    def _no_filter(query: str, survivors: list[RetrievedChunk]):
        return survivors, {"matched_doc_ids": [], "before": len(survivors), "after": len(survivors)}

    monkeypatch.setattr(pipeline_mod, "_apply_title_priority", _no_filter)
    baseline = await runtime.pipeline.retrieve(question, carol)

    assert [chunk.chunk_id for chunk in filtered.chunks] == [
        chunk.chunk_id for chunk in baseline.chunks
    ], "没有标题命中时不得改变候选（否则会误伤普通问句）"
    # 同模板的 4 份文档本来都在候选里：证明「不过滤」不是碰巧只剩一份
    assert len({chunk.doc_id for chunk in filtered.chunks}) >= 2


async def test_generic_title_suffix_alone_does_not_filter(title_corpus) -> None:
    """只提「作业指导书」这一通用体裁词：不算点名，不能把整库作业指导书都筛进来。"""
    runtime, _, carol = title_corpus

    result = await runtime.pipeline.retrieve(
        "作业指导书的编制要求是什么", carol, collect_debug=True
    )

    debug = result.stage_debug["title_match"]
    assert debug["matched_doc_ids"] == []
    assert debug["before"] == debug["after"] > 0, debug
    assert len({chunk.doc_id for chunk in result.chunks}) >= 2, (
        "通用后缀问句不应被收窄到某一份文档"
    )


async def test_multi_title_query_keeps_union(title_corpus) -> None:
    """问句同时点名两份文档 → 过滤到这两份的并集（比较类问题的正常形态）。"""
    runtime, doc_ids, carol = title_corpus
    named = {doc_ids[_REPAIR_TITLE], doc_ids[_DEMOLISH_TITLE]}

    result = await runtime.pipeline.retrieve(
        "维修作业指导书和装饰拆除作业指导书的生效日期", carol, collect_debug=True
    )

    assert result.refused is False, result.refusal_reason
    assert {chunk.doc_id for chunk in result.chunks} == named, (
        f"应只保留被点名的两份文档：{[(c.doc_id, c.doc_title) for c in result.chunks]}"
    )
    assert result.stage_debug["title_match"]["matched_doc_ids"] == sorted(named)
