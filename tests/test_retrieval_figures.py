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
    assert result.available_figures, "应筛出电表照片清单"
    assert result.refused is False
    assert "已找到相关图片" in result.answer
    assert "可提取" in result.answer or "发1张" in result.answer
    assert result.citations, "无引用时应补最小引用，便于调试台/钉钉对齐"


async def test_figure_intent_drops_junk_figure(seeded) -> None:
    """空白垃圾图不得回源；没有可发图时保持原拒答。"""
    runtime, _ = seeded
    junk = _figure(chunk_id=99, content="纯白背景，没有任何可识别文字")
    result = _result([_body(), junk], [], refused=True)
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="figure", question="把电表照片发我"
    )
    assert result.available_figures == []
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


async def test_factual_intent_lists_figures_but_hints_only(seeded) -> None:
    """发图闸门：事实问答收集可提取清单并提示张数，由通道按「发N张」再推。"""
    runtime, _ = seeded
    result = _result([_body(), _figure()], [], refused=False)
    result.intent = "factual"
    runtime.pipeline._apply_figure_adjustments(
        result, result.retrieval, intent="factual", question="设备运维干了什么"
    )
    assert result.available_figures
    assert result.available_figures[0]["chunk_id"] == 11
    assert "可提取图片" in result.answer


async def test_figure_attachments_lock_to_text_hit_page(seeded) -> None:
    """页 → 文字 → 同页图：别页题注即使有「工程部」等字样也不得抢回源。"""
    runtime, _ = seeded
    body = RetrievedChunk(
        chunk_id=30,
        doc_id=1,
        shard_id="kb_all_L1",
        content="第30页\n化粪池月度检查 小广告检查清除",
        chunk_type="child",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=30,
        page_to=30,
    )
    same_page = RetrievedChunk(
        chunk_id=301,
        doc_id=1,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=30,
        page_to=30,
        figure_id="fig-30",
        image_path="parse/1/images/30.png",
        heading_path="第 30 页",
    )
    other_page = RetrievedChunk(
        chunk_id=111,
        doc_id=1,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=11,
        page_to=11,
        figure_id="fig-11",
        image_path="parse/1/images/11.png",
        heading_path="1.能耗管理（工程部）",
    )
    result = AnswerResult(
        answer="第30页有化粪池月度检查",
        citations=[],
        retrieval=RetrievalResult(
            query="化粪池月度检查情况",
            chunks=[body, same_page, other_page],
        ),
        model="mock",
        rewritten_query="工程部2026年10月工作汇报化粪池月度检查情况",
        intent="figure",
    )
    runtime.pipeline._apply_figure_adjustments(
        result,
        result.retrieval,
        intent="figure",
        question="工程部2026年10月工作汇报，化粪池月度检查的图发我",
    )
    assert [a["chunk_id"] for a in result.available_figures] == [301]
    assert result.available_figures[0]["page_from"] == 30


async def test_figure_attachments_cover_each_cited_page(seeded) -> None:
    """多页文字引用：每页先挂 1 张，再补同页其余图，不被「页数=张数」卡死。"""
    runtime, _ = seeded
    texts = []
    figs = []
    cites = []
    for i, page in enumerate((27, 28, 29, 30, 31), start=1):
        texts.append(
            RetrievedChunk(
                chunk_id=page,
                doc_id=1,
                shard_id="kb_all_L1",
                content=f"第{page}页\n8. 创新、突破、专项主导\n事项{page}",
                chunk_type="parent",
                doc_title="工程部/2026年10月工作汇报-工程",
                page_from=page,
                page_to=page,
            )
        )
        # 每页 2 张，共 10 张；应全部进入 attachments（上限 16）
        for j in (0, 1):
            figs.append(
                RetrievedChunk(
                    chunk_id=page * 10 + j,
                    doc_id=1,
                    shard_id="kb_all_L1",
                    content="[图片]",
                    chunk_type="figure",
                    doc_title="工程部/2026年10月工作汇报-工程",
                    page_from=page,
                    page_to=page,
                    figure_id=f"fig-{page}-{j}",
                    image_path=f"parse/1/images/{page}-{j}.png",
                    parent_chunk_id=page,
                    heading_path=f"第 {page} 页",
                )
            )
        cites.append(
            Citation(
                cite_index=i,
                chunk_id=page,
                doc_id=1,
                doc_title="工程部/2026年10月工作汇报-工程",
                doc_version="v1",
                heading_path=f"第 {page} 页",
                page_from=page,
                page_to=page,
                rerank_score=0.9,
                acl_result="pass",
                page_check="pass",
            )
        )
    result = AnswerResult(
        answer="创新突破含多项[1][2][3][4][5]",
        citations=cites,
        retrieval=RetrievalResult(
            query="创新突破有些什么工作内容",
            chunks=[*texts, *figs],
        ),
        model="mock",
        rewritten_query="2026年10月工程部汇报 创新突破工作内容",
        intent="figure",
    )
    runtime.pipeline._apply_figure_adjustments(
        result,
        result.retrieval,
        intent="figure",
        question="2026年10月工程部汇报ppt，创新突破相关图片发我",
    )
    pages = sorted({int(a["page_from"]) for a in result.available_figures})
    assert pages == [27, 28, 29, 30, 31]
    assert len(result.available_figures) == 10


async def test_figure_attachments_follow_model_citation_not_rerank_mix(seeded) -> None:
    """模型只 cite 第30页时，即使上下文混入第20页（广告灯箱）也只发第30页图。"""
    runtime, _ = seeded
    page30 = RetrievedChunk(
        chunk_id=30,
        doc_id=1,
        shard_id="kb_all_L1",
        content="第30页\n小广告检查清除",
        chunk_type="parent",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=30,
        page_to=30,
    )
    page20 = RetrievedChunk(
        chunk_id=20,
        doc_id=1,
        shard_id="kb_all_L1",
        content="第20页\n商户及公共广告灯箱安全整改复查",
        chunk_type="parent",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=20,
        page_to=20,
    )
    fig30 = RetrievedChunk(
        chunk_id=301,
        doc_id=1,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=30,
        page_to=30,
        figure_id="fig-30",
        image_path="parse/1/images/30.png",
        parent_chunk_id=30,
        heading_path="第 30 页",
    )
    fig20 = RetrievedChunk(
        chunk_id=201,
        doc_id=1,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=20,
        page_to=20,
        figure_id="fig-20",
        image_path="parse/1/images/20.png",
        parent_chunk_id=20,
        heading_path="第 20 页",
    )
    result = AnswerResult(
        answer="第30页列有小广告检查清除[1]",
        citations=[
            Citation(
                cite_index=1,
                chunk_id=30,
                doc_id=1,
                doc_title="工程部/2026年10月工作汇报-工程",
                doc_version="v1",
                heading_path="第 30 页",
                page_from=30,
                page_to=30,
                rerank_score=0.9,
                acl_result="pass",
                page_check="pass",
            )
        ],
        retrieval=RetrievalResult(
            query="小广告清除内容",
            chunks=[page30, page20, fig30, fig20],
        ),
        model="mock",
        rewritten_query="工程部2026年10月工作汇报小广告清除内容",
        intent="figure",
    )
    runtime.pipeline._apply_figure_adjustments(
        result,
        result.retrieval,
        intent="figure",
        question="工程部2026年10月工作汇报，小广告清除相关图发我",
    )
    assert [a["chunk_id"] for a in result.available_figures] == [301]
    assert result.available_figures[0]["page_from"] == 30


async def test_word_unpaged_cite_requires_section_aligned_figures(seeded) -> None:
    """找图 + 无页码引用：只发章节与引用块对齐的图，同 parent 不等于整篇可发。"""
    runtime, _ = seeded
    parent_id = 116845
    table = RetrievedChunk(
        chunk_id=117308,
        doc_id=240,
        shard_id="kb_all_L1",
        content="4#楼配电室 专配1B 电表编号 DT862",
        chunk_type="table",
        doc_title="熙街物业工程部工作手册2026.11.8",
        heading_path="熙街物业工程部工作手册2026.11.8 > 6. 附件-6专配室回路清单 · 表 21",
        parent_chunk_id=parent_id,
        page_from=None,
        page_to=None,
    )
    cover = RetrievedChunk(
        chunk_id=116850,
        doc_id=240,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="熙街物业工程部工作手册2026.11.8",
        heading_path=(
            "熙街物业工程部工作手册2026.11.8 · "
            "重庆卓越城市运营服务有限责任公司熙街管理处 · 图1"
        ),
        figure_id="fig-116850",
        image_path="parse/240/images/cover.png",
        parent_chunk_id=parent_id,
    )
    route = RetrievedChunk(
        chunk_id=116852,
        doc_id=240,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="熙街物业工程部工作手册2026.11.8",
        heading_path="熙街物业工程部工作手册2026.11.8 · 照明巡检路线图 · 图3",
        figure_id="fig-116852",
        image_path="parse/240/images/route.png",
        parent_chunk_id=parent_id,
    )
    related = RetrievedChunk(
        chunk_id=116900,
        doc_id=240,
        shard_id="kb_all_L1",
        content="[图片]",
        chunk_type="figure",
        doc_title="熙街物业工程部工作手册2026.11.8",
        heading_path="熙街物业工程部工作手册2026.11.8 · 附件-6专配室回路清单 · 图9",
        figure_id="fig-116900",
        image_path="parse/240/images/panel.png",
        parent_chunk_id=parent_id,
    )
    result = AnswerResult(
        answer="4号楼专配1B电表编号见附件-6[1]",
        citations=[
            Citation(
                cite_index=1,
                chunk_id=117308,
                doc_id=240,
                doc_title="熙街物业工程部工作手册2026.11.8",
                doc_version="v1",
                heading_path=table.heading_path,
                page_from=None,
                page_to=None,
                rerank_score=0.9,
                acl_result="pass",
                page_check="pass",
            )
        ],
        retrieval=RetrievalResult(
            query="4号楼专配1B电表编号",
            chunks=[table, cover, route, related],
        ),
        model="mock",
        rewritten_query="4号楼专配1B电表编号",
        intent="figure",
    )
    runtime.pipeline._apply_figure_adjustments(
        result,
        result.retrieval,
        intent="figure",
        question="4号楼专配1B电表相关图发我",
    )
    assert [a["chunk_id"] for a in result.available_figures] == [116900]
    assert "可提取图片" in result.answer or "发1张" in result.answer

    # 事实意图同样只给清单+提示，不改变「按需提取」协议
    runtime.pipeline._apply_figure_adjustments(
        result,
        result.retrieval,
        intent="factual",
        question="4号楼专配1B电表编号",
    )
    assert result.available_figures
    assert "可提取图片" in result.answer


def test_figure_heading_matches_cites_requires_section_overlap() -> None:
    from ekbs.retrieval.pipeline import RetrievalPipeline

    cite = "熙街物业工程部工作手册2026.11.8 > 6. 附件-6专配室回路清单 · 表 21"
    assert RetrievalPipeline._figure_heading_matches_cites(
        "熙街物业工程部工作手册2026.11.8 · 附件-6专配室回路清单 · 图9",
        [cite],
    )
    assert not RetrievalPipeline._figure_heading_matches_cites(
        "熙街物业工程部工作手册2026.11.8 · 照明巡检路线图 · 图3",
        [cite],
    )
