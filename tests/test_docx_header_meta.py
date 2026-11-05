"""页眉管控信息是**文档元数据**，不是正文块。

用户实测：每篇制度都以「文件信息」开头，入库后四份制度的文件信息块互相竞争
（查询「维修作业指导书的生效日期」召回 4 篇不同制度的文件信息）。根因在解析：
docx 页眉管控表被当成正文写进 markdown。本文件钉住新口径：

* 解析：header_meta 结构化保留（文件名称/编号/生效日期/版本/页数/编制…），正文不含文件信息；
* 侧车：header_meta 随复核草稿落盘，旧侧车缺键时回空字典；
* 入库：生效/截止日期写入 ``ekbs_document.effective_date / expire_date``；
* 日期解析：中文年月日/短横线/点号/区间，非法值不落库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekbs.ingest.parser import (
    parse_cn_date,
    parse_cn_dates,
    parse_file,
    parse_header_meta,
)
from ekbs.ingest.pipeline import IngestPipeline, document_dates_from_header_meta
from ekbs.ingest.scan_review import (
    draft_dir,
    load_review_sidecar,
    save_review_draft,
)


def _pipeline(runtime) -> IngestPipeline:
    return IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )


def _make_docx_with_header(tmp_path: Path) -> Path:
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("1. 目的")
    document.add_paragraph("满足公共设备管理需要。")
    table = document.sections[0].header.add_table(rows=2, cols=4, width=docx.shared.Cm(16))
    cells = [
        ["文件名称", "维修作业指导书", "文件编号：XJWY-GC-09", "文件编号：XJWY-GC-09"],
        ["生效日期", "2025年1月1日", "页    数： 1 / 2", "页    数： 1 / 2"],
    ]
    for ri, row in enumerate(cells):
        for ci, text in enumerate(row):
            table.rows[ri].cells[ci].text = text
    path = tmp_path / "header.docx"
    document.save(str(path))
    return path


def test_parse_cn_dates_variants() -> None:
    assert parse_cn_dates("2025年1月1日") == ["2025-01-01"]
    assert parse_cn_dates("2025-1-1") == ["2025-01-01"]
    assert parse_cn_dates("2025.12.31") == ["2025-12-31"]
    assert parse_cn_dates("2025年1月1日至2027年12月31日") == ["2025-01-01", "2027-12-31"]
    assert parse_cn_date("2025年2月30日") is None, "非法日期不得编造"
    assert parse_cn_date("") is None


def test_parse_header_meta_normalizes_keys() -> None:
    meta = parse_header_meta(
        [
            "文件名称：维修作业指导书；文件编号：XJWY-GC-09",
            "生效日期：2025年1月1日；版 本：2025A；页 数： 1 / 2",
            "编 制：雷航；审 核：雷航；批准人：刘冀",
        ]
    )
    assert meta["文件名称"] == "维修作业指导书"
    assert meta["文件编号"] == "XJWY-GC-09"
    assert meta["生效日期"] == "2025年1月1日"
    assert meta["版本"] == "2025A"
    assert meta["页数"] == "1 / 2"
    assert meta["编制"] == "雷航"
    assert meta["批准人"] == "刘冀"


def test_document_dates_from_header_meta() -> None:
    assert document_dates_from_header_meta({"生效日期": "2025年1月1日"}) == ("2025-01-01", None)
    assert document_dates_from_header_meta(
        {"生效日期": "2025年1月1日", "截止日期": "2027年12月31日"}
    ) == ("2025-01-01", "2027-12-31")
    # 区间值取两端：生效取第一个日期、截止取最后一个日期
    assert document_dates_from_header_meta(
        {"有效期至": "2025年1月1日至2027年12月31日"}
    ) == (None, "2027-12-31")
    assert document_dates_from_header_meta({}) == (None, None)
    assert document_dates_from_header_meta(None) == (None, None)


def test_docx_header_meta_extracted_and_removed_from_body(tmp_path: Path) -> None:
    parsed = parse_file(_make_docx_with_header(tmp_path))
    assert parsed.header_meta["文件名称"] == "维修作业指导书"
    assert parsed.header_meta["文件编号"] == "XJWY-GC-09"
    assert parsed.header_meta["生效日期"] == "2025年1月1日"
    assert parsed.header_meta["页数"] == "1 / 2"
    assert "文件信息" not in parsed.markdown
    assert "XJWY-GC-09" not in parsed.markdown
    assert "2025年1月1日" not in parsed.markdown
    assert "1. 目的" in parsed.markdown
    assert parsed.page_count == 2


def test_sidecar_round_trips_header_meta_and_tolerates_old_files(tmp_path: Path) -> None:
    save_review_draft(
        data_dir=tmp_path,
        doc_id=7,
        markdown="正文",
        page_count=2,
        header_meta={"文件编号": "XJWY-GC-09", "生效日期": "2025年1月1日"},
    )
    sidecar = load_review_sidecar(tmp_path, 7)
    assert sidecar.header_meta["文件编号"] == "XJWY-GC-09"
    assert sidecar.header_meta["生效日期"] == "2025年1月1日"

    # 旧侧车没有 header_meta 键 → 回空字典，不崩
    import json

    path = draft_dir(tmp_path, 7) / "review_sidecar.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("header_meta", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_review_sidecar(tmp_path, 7).header_meta == {}


async def test_ingest_writes_document_dates_and_no_header_chunk(seeded) -> None:
    runtime, summary = seeded
    pipeline = _pipeline(runtime)
    result = await pipeline.ingest_markdown(
        title="维修作业指导书",
        markdown="# 维修作业指导书\n\n## 1. 目的\n\n规范维修服务。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        header_meta={"生效日期": "2025年1月1日", "截止日期": "2027年12月31日"},
        real_corpus=False,
    )
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT effective_date, expire_date FROM ekbs_document WHERE doc_id = ?",
            (result.doc_id,),
        ).fetchone()
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT chunk_type, content FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq",
                (result.doc_id,),
            )
        ]
    assert row["effective_date"] == "2025-01-01"
    assert row["expire_date"] == "2027-12-31"
    assert all("文件信息" not in str(item["content"]) for item in rows)
    # 元数据只注入上下文容器（父块），不写进叶子检索单元
    parents = [item for item in rows if item["chunk_type"] == "parent"]
    leaves = [item for item in rows if item["chunk_type"] in {"child", "table"}]
    assert parents and all(str(item["content"]).startswith("【文档信息】") for item in parents)
    # 第一个检索叶子带元数据；其余叶子不重复（避免同质检索块）
    assert leaves and str(leaves[0]["content"]).startswith("【文档信息】")
    assert all("【文档信息】" not in str(item["content"]) for item in leaves[1:])


async def test_people_metadata_stored_and_answerable(seeded) -> None:
    """编制/审核/批准人存 metadata_json，并进入父块【文档信息】行；点名文档可回答。"""
    from ekbs.provider.base import RerankResult
    from tests.conftest import user_id_of

    runtime, summary = seeded
    pipeline = _pipeline(runtime)
    header = {
        "文件编号": "XJWY-GC-09",
        "版本": "2025A",
        "生效日期": "2025年1月1日",
        "编制": "雷航",
        "审核": "雷航",
        "批准人": "刘冀",
    }
    await pipeline.ingest_markdown(
        title="工程部/XJWY-GC-09维修作业指导书",
        markdown="# 维修作业指导书\n\n## 1. 目的\n\n规范维修服务。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        header_meta=header,
        real_corpus=False,
    )
    await pipeline.ingest_markdown(
        title="工程部/XJWY-GC-10电梯作业指导书",
        markdown="# 电梯作业指导书\n\n## 1. 目的\n\n规范电梯巡检。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        header_meta={"文件编号": "XJWY-GC-10", "生效日期": "2025年3月1日",
                     "编制": "雷航", "审核": "刘冀", "批准人": "刘冀"},
        real_corpus=False,
    )

    async def fake_rerank(query, docs, top_n):  # noqa: ANN001, ANN202
        return [
            RerankResult(chunk_id=doc.chunk_id, score=0.05, shard_id=doc.shard_id)
            for doc in docs
        ]

    runtime.providers.rerank.rerank = fake_rerank  # type: ignore[method-assign]
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    named = await runtime.pipeline.retrieve(
        "工程部/XJWY-GC-09维修作业指导书的编制人、审核人和批准人是谁",
        perm,
        collect_debug=True,
    )
    assert named.refused is False
    assert "metadata_rescue" in named.degraded
    assert any(
        "编制：雷航" in chunk.content and "批准人：刘冀" in chunk.content
        for chunk in named.chunks
    )

    by_person = await runtime.pipeline.retrieve(
        "刘冀批准了哪些制度", perm, collect_debug=True
    )
    assert by_person.refused is False, "人名 + 元数据问句应走结构化扫描"
    assert "metadata_scan" in by_person.degraded
    scan = by_person.stage_debug.get("metadata_scan") or {}
    assert len(scan.get("doc_ids") or []) == 2, "两篇文档的批准人相同，都应扫出"


def test_system_prompt_handles_missing_expire_date() -> None:
    """源文件没写截止日期时，提示词要求回答「文件未标注」而不是「资料中没有」。"""
    from ekbs.retrieval.pipeline import SYSTEM_PROMPT

    assert "未标注截止日期" in SYSTEM_PROMPT


def test_system_prompt_asks_full_steps_for_process_questions() -> None:
    """流程/步骤类问题不能被「简洁」策略过度摘要（doc 208 实测只答 5 条）。"""
    from ekbs.retrieval.pipeline import SYSTEM_PROMPT

    assert "流程/步骤/怎么办" in SYSTEM_PROMPT or "流程" in SYSTEM_PROMPT
    assert "全部覆盖" in SYSTEM_PROMPT or "不要只给摘要" in SYSTEM_PROMPT


async def test_ingest_without_dates_keeps_columns_null(seeded) -> None:
    runtime, summary = seeded
    result = await _pipeline(runtime).ingest_markdown(
        title="无日期制度",
        markdown="# 无日期制度\n\n## 1. 目的\n\n正文。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
    )
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT effective_date, expire_date FROM ekbs_document WHERE doc_id = ?",
            (result.doc_id,),
        ).fetchone()
    assert row["effective_date"] is None
    assert row["expire_date"] is None


async def test_metadata_query_rescued_when_rerank_below_threshold(seeded) -> None:
    """点名文档问生效日期、正文精排分不够时，用父块的【文档信息】行兜底回答。"""
    from ekbs.provider.base import RerankResult
    from tests.conftest import user_id_of

    runtime, summary = seeded
    await _pipeline(runtime).ingest_markdown(
        title="工程部/XJWY-GC-09维修作业指导书",
        markdown="# 维修作业指导书\n\n## 1. 目的\n\n规范维修服务，接报后本班内完成。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        header_meta={"生效日期": "2025年1月1日"},
        real_corpus=False,
    )

    async def fake_rerank(query, docs, top_n):  # noqa: ANN001, ANN202
        return [
            RerankResult(chunk_id=doc.chunk_id, score=0.05, shard_id=doc.shard_id)
            for doc in docs
        ]

    runtime.providers.rerank.rerank = fake_rerank  # type: ignore[method-assign]
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve(
        "工程部/XJWY-GC-09维修作业指导书的生效日期", perm
    )
    assert result.refused is False, "元数据兜底后不应因精排分低拒答"
    assert "metadata_rescue" in result.degraded
    assert any("【文档信息】" in chunk.content for chunk in result.chunks)


async def test_metadata_scan_answers_unnamed_year_query(seeded) -> None:
    """问句没点名文档、但带年份时：按文档元数据扫出该年生效的文档并兜底。"""
    from ekbs.provider.base import RerankResult
    from tests.conftest import user_id_of

    runtime, summary = seeded
    pipeline = _pipeline(runtime)
    for title, effective in (
        ("工程部/XJWY-GC-20甲制度", "2025年1月1日"),
        ("工程部/XJWY-GC-21乙制度", "2024年4月1日"),
    ):
        await pipeline.ingest_markdown(
            title=title,
            markdown=f"# {title}\n\n## 1. 目的\n\n正文内容。\n",
            kb_id=int(summary["kb_id"]),
            level=1,
            visible_dept_ids=[],
            header_meta={"生效日期": effective},
            real_corpus=False,
        )

    async def fake_rerank(query, docs, top_n):  # noqa: ANN001, ANN202
        return [
            RerankResult(chunk_id=doc.chunk_id, score=0.05, shard_id=doc.shard_id)
            for doc in docs
        ]

    runtime.providers.rerank.rerank = fake_rerank  # type: ignore[method-assign]
    perm = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve(
        "哪些制度是2025年生效的", perm, collect_debug=True
    )
    assert result.refused is False, "元数据扫描命中后不应因精排分低拒答"
    assert "metadata_scan" in result.degraded
    scan = result.stage_debug.get("metadata_scan") or {}
    assert len(scan.get("doc_ids") or []) == 1, "只应扫出 2025 年生效的那篇"
    matched_contents = [
        chunk.content for chunk in result.chunks if chunk.chunk_id in (scan.get("chunk_ids") or [])
    ]
    assert matched_contents and "2025-01-01" in matched_contents[0]


def test_period_limited_question_must_not_use_annual_total() -> None:
    from ekbs.retrieval.pipeline import SYSTEM_PROMPT

    assert "1-9月" in SYSTEM_PROMPT
    assert "年度合计" in SYSTEM_PROMPT
    assert "禁止用年度合计代替" in SYSTEM_PROMPT
