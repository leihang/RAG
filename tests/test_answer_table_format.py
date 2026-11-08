"""答案出口 Markdown 表格规范化。"""

from __future__ import annotations

from ekbs.retrieval.answer_table_format import (
    markdown_tables_to_labeled_blocks,
    normalize_answer_markdown_tables,
)


def test_normalize_strips_cell_citations_and_unifies_empty() -> None:
    raw = (
        "| 月份 | 用量 | 同比 |\n"
        "| --- | --- | --- |\n"
        "| 1月 | 56888 |  |\n"
        "| 2月 | 1343.00[1] | - |\n"
    )
    out = normalize_answer_markdown_tables(raw)
    table_lines = [ln for ln in out.splitlines() if ln.startswith("|")]
    assert "[1]" not in "\n".join(table_lines)
    assert out.rstrip().endswith("[1]")
    assert "| — |" in out


def test_normalize_aligns_decimal_places_in_column() -> None:
    raw = (
        "| 月份 | 用量 |\n"
        "| --- | --- |\n"
        "| 1月 | 56888 |\n"
        "| 2月 | 1343.00 |\n"
        "| 3月 | 99.5 |\n"
    )
    out = normalize_answer_markdown_tables(raw)
    assert "| 56888.00 |" in out
    assert "| 1343.00 |" in out
    assert "| 99.50 |" in out


def test_normalize_leaves_percent_and_non_table_alone() -> None:
    prose = "用量为 100，环比 +12.3%。"
    assert normalize_answer_markdown_tables(prose) == prose
    raw = (
        "| 项 | 同比 |\n"
        "| --- | --- |\n"
        "| A | +12.3% |\n"
        "| B | — |\n"
    )
    out = normalize_answer_markdown_tables(raw)
    assert "+12.3%" in out
    assert "—" in out


def test_normalize_noop_without_pipes() -> None:
    text = "没有表格的普通答案。"
    assert normalize_answer_markdown_tables(text) == text


def test_normalize_inserts_blank_line_before_table() -> None:
    """钉钉 AI 卡片：表头紧贴上一段时不识别为表。"""
    raw = (
        "2026年9月公共用电量明细如下：\n"
        "| 项目 | 9月用量 |\n"
        "| --- | --- |\n"
        "| 活动用电 | 1343.00 |\n"
    )
    out = normalize_answer_markdown_tables(raw)
    assert "如下：\n\n| 项目 |" in out


def test_labeled_blocks_make_columns_explicit() -> None:
    raw = (
        "明细：\n"
        "| 项目 | 9月用量 | 同比 |\n"
        "| --- | --- | --- |\n"
        "| 活动用电 | 1343.00 | 229.17% |\n"
        "| 公共用电 | 56888.00 | — |\n"
    )
    out = markdown_tables_to_labeled_blocks(raw)
    assert "| 项目 |" not in out
    assert "【活动用电】" in out
    assert "9月用量：1343.00" in out
    assert "同比：229.17%" in out
    assert "【公共用电】" in out
    assert "同比：—" in out
