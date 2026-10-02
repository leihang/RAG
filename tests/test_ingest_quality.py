"""解析/清洗/分片质量指标与金标 Hit@k（`06` §1.7）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekbs.ingest.quality import (
    EMPTY_PAGE_RATE_MAX,
    GARBLED_RATE_MAX,
    TABLE_SURVIVAL_MIN,
    Thresholds,
    assess_batch,
    assess_chunk_stats,
    assess_parse_quality,
    garbled_rate,
)
from ekbs.tools import ingest_quality_report as quality_cli
from ekbs.tools.eval_retrieval import evaluate_cases, load_golden


GOLDEN_PATH = Path(__file__).resolve().parents[1] / "docs" / "eval" / "golden_qa.sample.json"


def test_garbled_rate_counts_replacement_char() -> None:
    total, bad, rate = garbled_rate("正常\ufffd文本\ufffd")
    assert total == 6
    assert bad == 2
    assert rate == pytest.approx(2 / 6)


def test_garbled_rate_empty_is_zero() -> None:
    assert garbled_rate("") == (0, 0, 0.0)


def test_parse_quality_flags_high_garbled() -> None:
    chunks = [
        {
            "chunk_id": 1,
            "chunk_type": "child",
            "content": "好" * 10 + "\ufffd" * 10,
            "token_count": 5,
            "heading_path": "A",
            "page_from": None,
            "page_to": None,
            "parent_id": None,
        }
    ]
    report = assess_parse_quality(doc_id=1, title="t", status="published", chunks=chunks)
    assert report.garbled_rate > GARBLED_RATE_MAX
    assert report.checks["garbled_ok"] is False
    assert report.ok is False


def test_empty_page_rate_and_continuity() -> None:
    chunks = [
        {
            "chunk_id": 1,
            "chunk_type": "child",
            "content": "正文足够长的一页内容用于覆盖检测，再多写几个字过阈值",
            "token_count": 20,
            "heading_path": "A",
            "page_from": 1,
            "page_to": 1,
            "parent_id": None,
        },
        {
            "chunk_id": 2,
            "chunk_type": "child",
            "content": "   ",
            "token_count": 0,
            "heading_path": "A",
            "page_from": 3,
            "page_to": 3,
            "parent_id": None,
        },
    ]
    report = assess_parse_quality(doc_id=2, title="pdf", status="published", chunks=chunks)
    assert report.pages_applicable is True
    assert report.page_count == 2
    assert report.missing_pages == [2]
    assert report.page_continuity_ok is False
    assert report.checks["page_continuity_ok"] is False
    assert report.empty_page_rate == pytest.approx(0.5)
    assert report.checks["empty_page_ok"] is False
    assert report.ok is False


def test_table_survival_rate() -> None:
    chunks = [
        {
            "chunk_id": 1,
            "chunk_type": "table",
            "content": "姓名 | 部门\n---|---\n甲 | HR",
            "token_count": 10,
            "heading_path": "表1",
            "page_from": 1,
            "page_to": 1,
            "parent_id": None,
        },
        {
            "chunk_id": 2,
            "chunk_type": "table",
            "content": "   ",
            "token_count": 0,
            "heading_path": "表2",
            "page_from": 2,
            "page_to": 2,
            "parent_id": None,
        },
    ]
    report = assess_parse_quality(doc_id=3, title="t", status="published", chunks=chunks)
    assert report.table_count == 2
    assert report.table_survival_rate == pytest.approx(0.5)
    assert report.checks["table_survival_ok"] is False


def test_no_tables_is_na_and_passes() -> None:
    chunks = [
        {
            "chunk_id": 1,
            "chunk_type": "child",
            "content": "纯文本",
            "token_count": 4,
            "heading_path": "A",
            "page_from": None,
            "page_to": None,
            "parent_id": None,
        }
    ]
    report = assess_parse_quality(doc_id=4, title="t", status="published", chunks=chunks)
    assert report.table_survival_rate is None
    assert report.checks["table_survival_ok"] is None
    assert report.checks["empty_page_ok"] is None
    assert report.ok is True


def test_dual_index_detection() -> None:
    same = "短节正文完全相同"
    chunks = [
        {
            "chunk_id": 10,
            "chunk_type": "parent",
            "content": same,
            "token_count": 8,
            "heading_path": "§1",
            "page_from": None,
            "page_to": None,
            "parent_id": None,
        },
        {
            "chunk_id": 11,
            "chunk_type": "child",
            "content": same,
            "token_count": 8,
            "heading_path": "§1",
            "page_from": None,
            "page_to": None,
            "parent_id": 10,
        },
        {
            "chunk_id": 20,
            "chunk_type": "parent",
            "content": "父块较长正文AAA",
            "token_count": 10,
            "heading_path": "§2",
            "page_from": None,
            "page_to": None,
            "parent_id": None,
        },
        {
            "chunk_id": 21,
            "chunk_type": "child",
            "content": "子块不同BBB",
            "token_count": 6,
            "heading_path": "§2",
            "page_from": None,
            "page_to": None,
            "parent_id": 20,
        },
    ]
    stats = assess_chunk_stats(doc_id=5, title="t", chunks=chunks)
    assert stats.dual_index_pairs == 1
    assert stats.dual_index_rate == pytest.approx(0.5)
    assert stats.leaf_chunks == 2


def test_assess_batch_collects_failures() -> None:
    docs = [
        (
            {"doc_id": 1, "title": "ok", "status": "published"},
            [
                {
                    "chunk_id": 1,
                    "chunk_type": "child",
                    "content": "干净",
                    "token_count": 2,
                    "heading_path": "A",
                    "page_from": None,
                    "page_to": None,
                    "parent_id": None,
                }
            ],
        ),
        (
            {"doc_id": 2, "title": "bad", "status": "published"},
            [
                {
                    "chunk_id": 2,
                    "chunk_type": "child",
                    "content": "\ufffd" * 100,
                    "token_count": 10,
                    "heading_path": "A",
                    "page_from": None,
                    "page_to": None,
                    "parent_id": None,
                }
            ],
        ),
    ]
    batch = assess_batch(docs, thresholds=Thresholds())
    assert batch.failed_doc_ids == [2]
    assert batch.ok is False
    assert batch.thresholds["empty_page_rate_max"] == EMPTY_PAGE_RATE_MAX
    assert batch.thresholds["table_survival_min"] == TABLE_SURVIVAL_MIN


def test_quality_cli_on_seeded(seeded, monkeypatch, capsys) -> None:
    runtime, _ = seeded
    monkeypatch.setattr(quality_cli, "load_settings", lambda *a, **k: runtime.settings)
    code = quality_cli.main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "L1" in out and "L2" in out


def test_quality_cli_json_fail(seeded, monkeypatch, capsys) -> None:
    runtime, _ = seeded
    monkeypatch.setattr(quality_cli, "load_settings", lambda *a, **k: runtime.settings)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_chunk SET content = ? WHERE doc_id = ("
            "SELECT doc_id FROM ekbs_document WHERE title = '年假管理制度' LIMIT 1)",
            ("\ufffd" * 200,),
        )
        conn.commit()
    code = quality_cli.main(["--json", "--fail-on-gate"])
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["failed_doc_ids"]


def test_load_golden_sample() -> None:
    cases = load_golden(GOLDEN_PATH)
    assert len(cases) >= 4
    assert cases[0]["must_hit_doc"] == "年假管理制度"


async def test_eval_retrieval_on_seeded(seeded) -> None:
    runtime, _ = seeded
    cases = load_golden(GOLDEN_PATH)
    report = await evaluate_cases(runtime, cases, default_k=5)
    assert report.errors == 0
    assert report.scored == len(cases)
    assert report.hit_at_k is not None
    by_id = {c.id: c for c in report.cases}
    assert by_id["redline-alice-no-finance"].hit is True
    assert by_id["carol-no-hr-leave"].hit is True
    assert by_id["hr-leave-days"].hit is True
    assert report.hit_at_k >= 0.8


# ---------------------------------------------------------------- 空页/丢页的判读（2026-10-02）


def _page_chunk(page: int, content: str, chunk_type: str = "parent", **extra) -> dict:
    row = {
        "chunk_id": page * 10 + (1 if chunk_type == "parent" else 2),
        "parent_id": None,
        "chunk_type": chunk_type,
        "content": content,
        "token_count": 10,
        "heading_path": "h",
        "page_from": page,
        "page_to": page,
        "figure_id": None,
        "image_path": "parse/x/images/a.png" if chunk_type == "figure" else None,
        "faiss_row": 1,
    }
    row.update(extra)
    return row


def test_page_with_only_an_image_is_not_empty() -> None:
    """只有图/表的页不算空页 —— 正文短不等于没内容。

    实测：PPT 的插图页、扫描件的图片页，正文可能只有 `[图片 0758f1b17b1d]`（17 字 < 20），
    旧实现把它记成"空页"，导致 doc 33/41 反复假警报。
    """
    chunks = [
        # 第 1 页正文充足，不参与"空页"判定
        _page_chunk(1, "这是一段足够长的正文，用来把第一页撑过空页阈值，避免干扰本用例。" * 2, "parent"),
        # 第 2 页正文只有 17 字（<20），但它有图 → 不该算空页
        _page_chunk(2, "[图片 abcdef012345]", "figure"),
    ]
    report = assess_parse_quality(doc_id=1, title="t", status="published", chunks=chunks)
    assert report.visual_pages == 1
    assert report.empty_pages == 0, "有图的页不该计入空页"


def test_lost_pages_use_source_page_count_as_denominator() -> None:
    """零产出页的分母是**源文件页数** —— 只按块覆盖范围算，尾部空白/未解析页看不见。"""
    chunks = [_page_chunk(1, "第一页正文"),
              _page_chunk(2, "第二页正文")]
    report = assess_parse_quality(
        doc_id=1, title="t", status="published", chunks=chunks, source_page_count=5
    )
    assert report.lost_pages == [3, 4, 5]
    assert report.lost_page_rate == pytest.approx(0.6)
    assert any("零产出页" in w for w in report.warnings)


def test_slide_based_documents_skip_empty_page_gate() -> None:
    """幻灯片类文档不做空页率准出（指标误用），记 N/A 而不是假装通过。"""
    chunks = [_page_chunk(1, "封面"), _page_chunk(2, "01 PART"), _page_chunk(3, "02 PART")]
    report = assess_parse_quality(
        doc_id=1, title="deck.pptx", status="published", chunks=chunks, slide_based=True
    )
    assert report.empty_page_rate > EMPTY_PAGE_RATE_MAX, "前提：它确实超线了"
    assert report.checks["empty_page_ok"] is None, "幻灯片类应记 N/A"
    assert report.ok is True, "N/A 不算失败"
    assert any("幻灯片类" in w for w in report.warnings)


def test_page_documents_still_gated_on_empty_pages() -> None:
    """页面型文档（PDF/docx）照旧受准出约束 —— 豁免只给幻灯片，不是放宽所有人。"""
    chunks = [_page_chunk(1, "封面"), _page_chunk(2, "a"), _page_chunk(3, "b")]
    report = assess_parse_quality(
        doc_id=1, title="report.pdf", status="published", chunks=chunks, slide_based=False
    )
    assert report.checks["empty_page_ok"] is False
    assert report.ok is False
