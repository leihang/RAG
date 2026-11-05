"""表格计算算子注册表：lookup/sum/compare 的可组合执行。"""

from __future__ import annotations

from ekbs.retrieval.table_calc import CompareExpr, LookupExpr, SumExpr, evaluate
from ekbs.retrieval.table_canonical import CrossTabAdapter
from ekbs.retrieval.table_struct import TableRow
from ekbs.retrieval.table_time import TimeSpec


def _table():
    rows = [
        TableRow(
            chunk_index=1, doc_id=1, caption="年费用（元）", unit="元",
            subject="指标", year=2025,
            months={1: 10.0, 2: 20.0}, annual_total=30.0, chunk_id=1,
        ),
        TableRow(
            chunk_index=1, doc_id=1, caption="年费用（元）", unit="元",
            subject="指标", year=2026,
            months={1: 12.0, 2: 22.0}, annual_total=34.0, chunk_id=2,
        ),
    ]
    return CrossTabAdapter.from_rows(rows)[0]


def test_sum_and_lookup_operators() -> None:
    table = _table()
    sum_value = evaluate(
        table, SumExpr(subject="指标", year=2026, time=TimeSpec("month_range", 1, 2))
    )
    assert sum_value is not None and sum_value.value == 34.0 and sum_value.unit == "元"
    assert sum_value.provenance["op"] == "sum"

    lookup = evaluate(
        table, LookupExpr(subject="指标", year=2026, time=TimeSpec("year", 1, 12))
    )
    assert lookup is not None and lookup.value == 34.0
    assert lookup.provenance["period"] == "total"


def test_compare_operator_provenance() -> None:
    table = _table()
    expr = CompareExpr(
        left=SumExpr(subject="指标", year=2026, time=TimeSpec("month_range", 1, 2)),
        right=SumExpr(subject="指标", year=2025, time=TimeSpec("month_range", 1, 2)),
    )
    result = evaluate(table, expr)
    assert result is not None
    assert result.value == 4.0
    assert result.provenance["op"] == "compare"
    assert result.provenance["left"] == 34.0


def test_ratio_yoy_rank_operators() -> None:
    from ekbs.retrieval.table_calc import RankExpr, RatioExpr, YoyExpr

    table = _table()
    t = TimeSpec("month_range", 1, 2)
    ratio = evaluate(
        table,
        RatioExpr(
            numerator=SumExpr(subject="指标", year=2026, time=t),
            denominator=SumExpr(subject="指标", year=2026, time=t),
        ),
    )
    assert ratio is not None and abs(ratio.value - 100.0) < 1e-9

    yoy = evaluate(
        table,
        YoyExpr(
            current=SumExpr(subject="指标", year=2026, time=t),
            previous=SumExpr(subject="指标", year=2025, time=t),
        ),
    )
    assert yoy is not None and abs(yoy.value - (4.0 / 30.0 * 100.0)) < 1e-9

    rank = evaluate(table, RankExpr(year=2026, time=t, top=2))
    assert rank is not None and rank.provenance["op"] == "rank"
    assert rank.provenance["ranking"][0]["subject"] == "指标"
