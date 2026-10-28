"""找图意图后处理（`_apply_figure_adjustments`）。

正文命中、图也筛出来了，但模型常因「资料区没有像素」口头拒答；
这里把这类误拒答纠正为「已找到相关图片」，并清洗垃圾图引用。
"""

from __future__ import annotations

from ekbs.retrieval.pipeline import (
    REFUSE_NO_BASIS,
    AnswerResult,
    Citation,
    RetrievalResult,
    RetrievedChunk,
)


def _body(chunk_id: int = 10, doc_id: int = 1, content: str = "电表更换说明") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, doc_id=doc_id, shard_id="kb_all_L1", content=content,
        chunk_type="child", doc_title="工程汇报", page_from=8, page_to=9,
    )


def _figure(
    chunk_id: int = 11, doc_id: int = 1, content: str = "电表照片 DT862",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, doc_id=doc_id, shard_id="kb_all_L1", content=content,
        chunk_type="figure", doc_title="工程汇报", page_from=8, page_to=8,
        figure_id=f"fig-{chunk_id}", image_path=f"parse/{doc_id}/images/{chunk_id}.png",
    )


def _cite(chunk_id: int, figure_id: str | None = None) -> Citation:
    return Citation(
        cite_index=1, chunk_id=chunk_id, doc_id=1, doc_title="工程汇报",
        doc_version="v1", heading_path="", page_from=8, page_to=8,
        rerank_score=0.9, acl_result="pass", page_check="pass",
        figure_id=figure_id, image_path=f"parse/1/images/{chunk_id}.png" if figure_id else None,
    )


def _result(
    chunks: list[RetrievedChunk], citations: list[Citation], *, refused: bool,
) -> AnswerResult:
    return AnswerResult(
        answer="资料区没有像素" if refused else "见下方原图",
        citations=citations,
        retrieval=RetrievalResult(query="电表照片", chunks=chunks),
        model="mock",
        rewritten_query="工程汇报 电表照片",
        refused=refused,
        refusal_reason=REFUSE_NO_BASIS if refused else "",
        intent="figure",
    )


async def test_figure_intent_refusal_corrected(seeded) -> None:
    """找图意图 + 已筛出图 + 模型口头拒答 → 纠正为「已找到相关图片」并补引用。"""
    runtime, _ = seeded
    result = _result([_body(), _figure()], [], refused=True)
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="figure", question="把电表照片发我"
    )
    assert result.figure_attachments, "应筛出电表照片"
    assert result.refused is False
    assert "已找到相关图片" in result.answer
    assert result.citations, "无引用时应补最小引用，便于调试台/钉钉对齐"


async def test_figure_intent_drops_junk_figure(seeded) -> None:
    """空白垃圾图不得回源；没有可发图时保持原拒答。"""
    runtime, _ = seeded
    junk = _figure(chunk_id=99, content="纯白背景，没有任何可识别文字")
    result = _result([_body(), junk], [], refused=True)
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="figure", question="把电表照片发我"
    )
    assert result.figure_attachments == []
    assert result.refused is True


async def test_figure_intent_cleans_junk_citation(seeded) -> None:
    """引用里的垃圾图被清掉，其余引用重新编号。"""
    runtime, _ = seeded
    good = _figure(chunk_id=11)
    junk = _figure(chunk_id=99, content="纯白背景，没有任何可识别文字")
    result = _result(
        [_body(), good, junk],
        [_cite(11, figure_id="fig-11"), _cite(99, figure_id="fig-99")],
        refused=False,
    )
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="figure", question="把电表照片发我"
    )
    assert [cite.chunk_id for cite in result.citations] == [11]
    assert result.citations[0].cite_index == 1
