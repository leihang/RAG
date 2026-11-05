"""结构化表格问答：区间求和/单位/年度合计校验。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.table_query import compute_table_comparison

CONTENT = """表名: 年用水费统计对比（元）
相关表头: 日期 | 年       月份 | 1月 | 2月 | 3月 | 4月 | 5月 | 6月 | 7月 | 8月 | 9月 | 10月 | 11月 | 12月 | 年度合计
日期=公司承担水费; 年       月份=2025; 1月=8520.86; 2月=24444.10; 3月=12278.35; 4月=36500.71; 5月=14332.74; 6月=35569.18; 7月=26610.49; 8月=37169.85; 9月=16086.45; 10月=42512.16; 11月=16740.39; 12月=59856.67; 年度合计=330621.95
日期=公司承担水费; 年       月份=2026; 1月=52679.50; 2月=80913.80; 3月=76906.30; 4月=9227.15; 5月=8241.60; 6月=17404.15; 7月=23606.97; 8月=8518.63; 9月=286.25; 10月=0.00; 11月=0.00; 12月=0.00; 年度合计=277784.35
"""


def _chunk(content: str = CONTENT) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=1, doc_id=215, chunk_type="table",
        content=content, heading_path="用水分析", doc_title="能耗分析",
    )


def test_jan_sep_comparison_uses_month_columns_and_unit() -> None:
    question = "公司承担水费，2026年1-9月总水费较2025年1-9月总水费是增长还是减少"
    result = compute_table_comparison(question, [_chunk()])
    assert result is not None
    assert result.totals[2026] == 277784.35
    assert result.totals[2025] == 211512.73
    assert round(result.diff, 2) == 66271.62
    assert result.unit == "元"
    text = result.render()
    assert "增长" in text and "66,271.62 元" in text
    assert "330,621.95" not in text


def test_other_month_range_is_generic() -> None:
    question = "公司承担水费，2026年1-8月较2025年1-8月是增长还是减少"
    result = compute_table_comparison(question, [_chunk()])
    assert result is not None
    assert result.start_month == 1 and result.end_month == 8
    assert result.totals[2026] == round(52679.50 + 80913.80 + 76906.30 + 9227.15 + 8241.60 + 17404.15 + 23606.97 + 8518.63, 2)


def test_unit_is_read_from_caption_not_hardcoded() -> None:
    content = CONTENT.replace("（元）", "（万元）")
    result = compute_table_comparison(
        "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少", [_chunk(content)]
    )
    assert result is not None
    assert result.unit == "万元"
    assert "万元" in result.render()


def test_annual_total_mismatch_is_rejected() -> None:
    content = CONTENT.replace("年度合计=330621.95", "年度合计=999999.99")
    assert compute_table_comparison(
        "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少", [_chunk(content)]
    ) is None


def test_duplicate_segments_do_not_double_count() -> None:
    question = "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少"
    one = compute_table_comparison(question, [_chunk()])
    two = compute_table_comparison(question, [_chunk(), _chunk()])
    assert one is not None and two is not None
    assert one.totals == two.totals


def test_quarter_is_derived_from_month_columns() -> None:
    result = compute_table_comparison(
        "公司承担水费，2026年Q1较2025年Q1是增长还是减少", [_chunk()]
    )
    assert result is not None
    assert abs(result.totals[2026] - 210499.60) < 0.01
    assert result.years == (2026, 2025)
    assert "Q1" in result.render()


def test_three_years_are_not_fixed_to_two() -> None:
    content = CONTENT.replace(
        "日期=公司承担水费; 年       月份=2025;",
        "日期=公司承担水费; 年       月份=2024; 1月=1.00; 2月=1.00; 3月=1.00; 4月=1.00; 5月=1.00; 6月=1.00; 7月=1.00; 8月=1.00; 9月=1.00; 10月=1.00; 11月=1.00; 12月=12.00; 年度合计=23.00\n"
        "日期=公司承担水费; 年       月份=2025;",
    )
    result = compute_table_comparison(
        "公司承担水费，2026年1-9月较2025年1-9月、2024年1-9月是增长还是减少",
        [_chunk(content)],
    )
    assert result is not None
    assert len(result.years) == 3
    assert result.totals[2024] == 9.0
    assert "对比：" in result.render()


RECORD_CONTENT = """表名: 表 2 岗位职责
相关表头: 岗位 | 职责
岗位=工程经理; 职责=对项目能耗管理情况负责，监督能耗管理工作执行情况。
岗位=设备主管; 职责=编制年度能耗管理计划并按计划实施。
"""


def test_record_table_lookup_is_deterministic() -> None:
    from types import SimpleNamespace
    from ekbs.retrieval.table_query import compute_table_lookup

    chunk = SimpleNamespace(
        chunk_id=9, doc_id=205, chunk_type="table",
        content=RECORD_CONTENT, heading_path="岗位职责", doc_title="能耗管理",
    )
    result = compute_table_lookup("工程经理的职责是什么", [chunk])
    assert result is not None
    assert result.value.startswith("对项目能耗管理情况负责")
    assert "[1]" in result.render()


def test_record_table_lookup_ambiguity_returns_none() -> None:
    from types import SimpleNamespace
    from ekbs.retrieval.table_query import compute_table_lookup

    content = RECORD_CONTENT + "岗位=工程经理; 职责=另一个职责。\n"
    chunk = SimpleNamespace(
        chunk_id=9, doc_id=205, chunk_type="table",
        content=content, heading_path="岗位职责", doc_title="能耗管理",
    )
    assert compute_table_lookup("工程经理的职责是什么", [chunk]) is None


NESTED_CONTENT = """表名: 年用水费统计对比（元）
相关表头: 日期 | 2026年/1月 | 2026年/2月 | 2025年/1月 | 2025年/2月
日期=公司承担水费; 2026年/1月=10.00; 2026年/2月=20.00; 2025年/1月=5.00; 2025年/2月=6.00
"""


def test_nested_header_paths_are_normalized() -> None:
    from types import SimpleNamespace

    chunk = SimpleNamespace(
        chunk_id=11, doc_id=1, chunk_type="table",
        content=NESTED_CONTENT, heading_path="用水分析", doc_title="能耗分析",
    )
    result = compute_table_comparison(
        "公司承担水费，2026年1-2月较2025年1-2月是增长还是减少", [chunk]
    )
    assert result is not None
    assert result.totals[2026] == 30.0
    assert result.totals[2025] == 11.0
    assert result.diff == 19.0
