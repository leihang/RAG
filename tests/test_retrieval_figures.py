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


def test_prefer_prose_drops_all_recalled_figures(seeded) -> None:
    """figure 不应进召回；若旧索引混入，一律丢掉，只留 prose。"""
    from types import SimpleNamespace

    runtime, _ = seeded
    prose = _body(
        chunk_id=43108,
        content="2. 设备运维（安防、工程）\n1、按季节调整：照明、中央空调运行",
    )
    prose.rerank_score = 0.70
    handwrite = _figure(
        chunk_id=43125,
        content=(
            "[图片] 正常正常是 2026.10.26 已更换\n"
            "正常正常是 2026.10.26 未更换漏保已告知家长盛琼"
        ),
    )
    handwrite.rerank_score = 0.74
    watermark = _figure(
        chunk_id=43139,
        content=(
            "[图片] 物业巡检水印\n2026.10.13 星期二 15:39\n天气：晴 26℃\n"
            "巡检人：请输入巡检人\n今日水印\n防伪 TUEZAP"
        ),
    )
    watermark.rerank_score = 0.68
    useful = _figure(chunk_id=43122, content="[图片] 空调设备统计表 分体空调 制冷量")
    useful.rerank_score = 0.66

    result = SimpleNamespace(degraded=[], stage_debug={})
    ordered = runtime.pipeline._prefer_prose_over_figures(
        [handwrite, watermark, useful, prose], result, collect_debug=True
    )
    assert [c.chunk_id for c in ordered] == [43108]
    assert "figure_recall_dropped" in result.degraded
    assert result.stage_debug["figure_demote"]["prose_first"] is True
    assert result.stage_debug["figure_demote"]["figure_kept"] == []


def test_figure_is_junk_detects_ppt_photo_captions() -> None:
    from ekbs.retrieval.pipeline import RetrievalPipeline

    assert RetrievalPipeline._figure_is_junk(
        "正常正常是 2026.10.26 已更换·是决\n正常正常是\n正常是 孙纳"
    )
    assert RetrievalPipeline._figure_is_junk(
        "物业巡检水印\n巡检人：请输入巡检人\n今日水印\n防伪 ABC\n天气：晴 26℃"
    )
    assert not RetrievalPipeline._figure_is_junk("电表照片 DT862 读数 10544.9 kWh")


async def test_factual_intent_attaches_bound_figures(seeded) -> None:
    """非找图意图：文字命中后展开的绑定 figure 仍进入 figure_attachments。"""
    runtime, _ = seeded
    result = _result([_body(), _figure()], [], refused=False)
    result.intent = "factual"
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="factual", question="设备运维干了什么"
    )
    assert result.figure_attachments
    assert result.figure_attachments[0]["chunk_id"] == 11
