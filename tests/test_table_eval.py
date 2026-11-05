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


def test_wide_table_cell_value() -> None:
    from ekbs.retrieval.table_query import compute_table_value

    content = """表名: 用电分析（熙街2026年9月用电分析表 / 电量分析(KW/h））
相关表头: 用量   日期 | 2026年9月 | 2025年9月 | 2026年8月 | 同比 | 环比
用量   日期=总电量; 2026年9月=4379405.00; 2025年9月=4616475.00; 2026年8月=5288426.00; 同比=-5.14%; 环比=-17.19%
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_value("2026年9月总电量是多少", [chunk])
    assert result is not None
    assert result.value == 4379405.00
    assert result.unit == "KW/h" or result.unit


def test_average_uses_months_not_total() -> None:
    from ekbs.retrieval.table_query import compute_table_average

    rows = [
        _row(2026, {1: 10.0, 2: 20.0, 3: 30.0}, annual=60.0),
    ]
    result = compute_table_average("测试指标2026年平均每月是多少", [_chunk(rows=rows)])
    assert result is not None and result.value == 20.0


def test_table_ratio_golden() -> None:
    from ekbs.retrieval.table_query import compute_table_ratio

    # 行 subject 固定为“测试指标”，另加一列无法表达；用两张同表行构造两个 subject。
    content = """表名: 占比表（元）
相关表头: 日期 | 年       月份 | 1月 | 2月
日期=商铺电量; 年       月份=2026; 1月=30; 2月=30
日期=总电量; 年       月份=2026; 1月=100; 2月=100
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_ratio("2026年1-2月商铺电量占总电量的比例是多少", [chunk])
    assert result is not None
    assert result.percent == 30.0


def test_table_ratio_from_wide_cells() -> None:
    from ekbs.retrieval.table_query import compute_table_ratio

    content = """表名: 用电分析（KW/h）
相关表头: 用量日期 | 2026年9月 | 2025年9月
用量日期=总电量; 2026年9月=1000; 2025年9月=900
用量日期=商铺电量; 2026年9月=300; 2025年9月=200
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_ratio("2026年9月商铺电量占总电量的比例是多少", [chunk])
    assert result is not None
    assert result.percent == 30.0


def test_rank_from_wide_cells() -> None:
    from ekbs.retrieval.table_query import compute_table_rank

    content = """表名: 用电分析（KW/h）
相关表头: 用量日期 | 2026年9月
用量日期=总电量; 2026年9月=1000
用量日期=商铺电量; 2026年9月=300
用量日期=住宅电量; 2026年9月=200
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_rank("2026年9月电量排名前三", [chunk])
    assert result is not None
    assert [subject for subject, _ in result.items] == ["总电量", "商铺电量", "住宅电量"]


def test_binary_add_from_table_rows() -> None:
    from ekbs.retrieval.table_query import compute_table_binary

    content = """表名: 合计表（元）
相关表头: 日期 | 年       月份 | 1月 | 2月
日期=商铺; 年       月份=2026; 1月=10; 2月=10
日期=住宅; 年       月份=2026; 1月=5; 2月=5
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_binary("2026年1-2月商铺与住宅合计是多少", [chunk])
    assert result is not None and result.value == 30.0


def test_filtered_sum_from_wide_cells() -> None:
    from ekbs.retrieval.table_query import compute_table_filtered

    content = """表名: 用电分析（KW/h）
相关表头: 用量日期 | 2026年9月
用量日期=总电量; 2026年9月=1000
用量日期=商铺电量; 2026年9月=300
用量日期=住宅电量; 2026年9月=200
"""
    import tests.test_table_eval as eval_mod

    chunk = eval_mod._chunk(rows=[])
    chunk.content = content
    result = compute_table_filtered("2026年9月除住宅外的电量合计是多少", [chunk])
    assert result is not None and result.value == 300.0
