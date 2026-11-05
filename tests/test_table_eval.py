"""表格问答评测集：合成表覆盖时间/单位/缺月/多年/记录表。

这些用例不绑定真实文件；新增表型或时间算子时先在这里加 golden case。
"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.table_query import compute_table_comparison, compute_table_lookup

HEADER = "相关表头: 日期 | 年       月份 | 1月 | 2月 | 3月 | 4月 | 5月 | 6月 | 7月 | 8月 | 9月 | 10月 | 11月 | 12月 | 年度合计"


def _row(year: int, values: dict[int, float], *, annual: float | None = None) -> str:
    parts = [f"日期=测试指标; 年       月份={year}"]
    parts += [f"{month}月={values[month]}" for month in sorted(values)]
    if annual is not None:
        parts.append(f"年度合计={annual}")
    return "; ".join(parts)


def _chunk(caption: str = "测试表（元）", rows: list[str] | None = None) -> SimpleNamespace:
    content = f"表名: {caption}\n{HEADER}\n" + "\n".join(rows or [])
    return SimpleNamespace(
        chunk_id=1, doc_id=1, chunk_type="table",
        content=content, heading_path="分析", doc_title="测试",
    )


def test_month_range_golden() -> None:
    rows = [
        _row(2025, {m: 10.0 for m in range(1, 13)}, annual=120.0),
        _row(2026, {m: 20.0 for m in range(1, 13)}, annual=240.0),
    ]
    result = compute_table_comparison(
        "测试指标，2026年1-9月较2025年1-9月是增长还是减少", [_chunk(rows=rows)]
    )
    assert result is not None
    assert result.totals == {2026: 180.0, 2025: 90.0}
    assert result.diff == 90.0


def test_quarter_golden() -> None:
    rows = [
        _row(2025, {1: 1.0, 2: 2.0, 3: 3.0}),
        _row(2026, {1: 2.0, 2: 2.0, 3: 2.0}),
    ]
    result = compute_table_comparison(
        "测试指标，2026年Q1较2025年Q1是增长还是减少", [_chunk(rows=rows)]
    )
    assert result is not None
    assert result.totals == {2026: 6.0, 2025: 6.0}


def test_unit_is_from_caption() -> None:
    rows = [_row(2025, {1: 1.0}), _row(2026, {1: 2.0})]
    result = compute_table_comparison(
        "测试指标，2026年1月较2025年1月是增长还是减少",
        [_chunk(caption="测试表（万元）", rows=rows)],
    )
    assert result is not None and result.unit == "万元"


def test_missing_month_returns_none() -> None:
    rows = [
        _row(2025, {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 6: 6.0, 7: 7.0, 8: 8.0, 9: 9.0}),
        _row(2026, {m: 1.0 for m in range(1, 10)}),
    ]
    assert compute_table_comparison(
        "测试指标，2026年1-9月较2025年1-9月是增长还是减少", [_chunk(rows=rows)]
    ) is None


def test_annual_mismatch_returns_none() -> None:
    rows = [
        _row(2025, {m: 1.0 for m in range(1, 13)}, annual=999.0),
        _row(2026, {m: 1.0 for m in range(1, 13)}, annual=12.0),
    ]
    assert compute_table_comparison(
        "测试指标，2026年1月较2025年1月是增长还是减少", [_chunk(rows=rows)]
    ) is None


def test_three_years_golden() -> None:
    rows = [
        _row(2024, {m: 1.0 for m in range(1, 10)}),
        _row(2025, {m: 2.0 for m in range(1, 10)}),
        _row(2026, {m: 3.0 for m in range(1, 10)}),
    ]
    result = compute_table_comparison(
        "测试指标，2026年1-9月较2025年1-9月、2024年1-9月是增长还是减少",
        [_chunk(rows=rows)],
    )
    assert result is not None
    assert result.years == (2026, 2025, 2024)
    assert result.totals == {2026: 27.0, 2025: 18.0, 2024: 9.0}


def test_record_lookup_golden() -> None:
    content = "表名: 岗位\n相关表头: 岗位 | 职责\n岗位=工程经理; 职责=负责项目能耗管理。\n"
    chunk = SimpleNamespace(
        chunk_id=1, doc_id=1, chunk_type="table",
        content=content, heading_path="岗位", doc_title="测试",
    )
    result = compute_table_lookup("工程经理的职责是什么", [chunk])
    assert result is not None and result.value == "负责项目能耗管理。"


def test_direct_quarter_columns_are_used_without_month_derivation() -> None:
    content = """表名: 季度表（元）
相关表头: 日期 | 年       月份 | Q1 | Q2 | Q3 | Q4
日期=测试指标; 年       月份=2025; Q1=10; Q2=20; Q3=30; Q4=40
日期=测试指标; 年       月份=2026; Q1=12; Q2=22; Q3=32; Q4=42
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_comparison(
        "测试指标，2026年Q1较2025年Q1是增长还是减少", [chunk]
    )
    assert result is not None
    assert result.totals == {2026: 12.0, 2025: 10.0}


def test_single_period_value_uses_total_or_months() -> None:
    from ekbs.retrieval.table_query import compute_table_value

    rows = [
        _row(2025, {m: 10.0 for m in range(1, 13)}, annual=120.0),
        _row(2026, {m: 20.0 for m in range(1, 13)}, annual=240.0),
    ]
    chunk = _chunk(rows=rows)
    annual = compute_table_value("测试指标2026年全年总费用是多少", [chunk])
    assert annual is not None and annual.value == 240.0
    partial = compute_table_value("测试指标2026年1-9月总费用是多少", [chunk])
    assert partial is not None and partial.value == 180.0
