"""表格计算算子（可组合计划 AST）的 MVP。

目标不是继续写“水费分支”，而是建立：

    Lookup / Sum / Compare 等表达式
        ↓ 注册表
    执行器 → CalcValue(value, unit, provenance)

后续 rank/ratio/yoy/join 只需注册新算子；查询规划器只产出表达式，不产出数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ekbs.retrieval.table_canonical import CanonicalTable
from ekbs.retrieval.table_time import TimeSpec


@dataclass(frozen=True)
class CalcValue:
    value: float
    unit: str
    provenance: dict[str, Any] = field(default_factory=dict)
    chunk_ids: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class LookupExpr:
    subject: str
    year: int
    time: TimeSpec
    kind: str = "lookup"


@dataclass(frozen=True)
class SumExpr:
    subject: str
    year: int
    time: TimeSpec
    kind: str = "sum"


@dataclass(frozen=True)
class CompareExpr:
    left: LookupExpr | SumExpr
    right: LookupExpr | SumExpr
    percent: bool = True
    kind: str = "compare"


CalcExpr = LookupExpr | SumExpr | CompareExpr


def _year_facts(table: CanonicalTable, subject: str, year: int) -> list[Any]:
    return [
        fact
        for fact in table.facts_for(subject)
        if fact.dimensions.get("year") == year
    ]


def _op_lookup(table: CanonicalTable, expr: LookupExpr) -> CalcValue | None:
    """直接取一个已有事实：年度合计/单月/季度列；不做多月累加。"""
    facts = _year_facts(table, expr.subject, expr.year)
    if not facts:
        return None
    total_facts = [fact for fact in facts if fact.dimensions.get("total") is not None]
    if total_facts:
        fact = total_facts[0]
        return CalcValue(
            fact.value, fact.unit,
            {"op": "lookup", "table": table.table_key, "subject": expr.subject,
             "year": expr.year, "period": "total"},
            (fact.chunk_id,),
        )
    if expr.time.kind == "quarter":
        quarter = ((int(expr.time.start_month or 1) - 1) // 3) + 1
        quarter_facts = [f for f in facts if f.dimensions.get("quarter") == quarter]
        if quarter_facts:
            fact = quarter_facts[0]
            return CalcValue(
                fact.value, fact.unit,
                {"op": "lookup", "table": table.table_key, "subject": expr.subject,
                 "year": expr.year, "period": f"Q{quarter}"},
                (fact.chunk_id,),
            )
    if expr.time.kind == "month_range" and expr.time.start_month == expr.time.end_month:
        month = expr.time.start_month
        month_facts = [f for f in facts if f.dimensions.get("month") == month]
        if month_facts:
            fact = month_facts[0]
            return CalcValue(
                fact.value, fact.unit,
                {"op": "lookup", "table": table.table_key, "subject": expr.subject,
                 "year": expr.year, "period": f"{month}月"},
                (fact.chunk_id,),
            )
    return None


def _op_sum(table: CanonicalTable, expr: SumExpr) -> CalcValue | None:
    facts = _year_facts(table, expr.subject, expr.year)
    if not facts:
        return None
    if expr.time.kind == "year":
        totals = [fact for fact in facts if fact.dimensions.get("total") is not None]
        if totals:
            fact = totals[0]
            return CalcValue(
                fact.value, fact.unit,
                {"op": "sum", "table": table.table_key, "subject": expr.subject,
                 "year": expr.year, "period": "total_direct"},
                (fact.chunk_id,),
            )
    if expr.time.kind == "quarter":
        quarter = ((int(expr.time.start_month or 1) - 1) // 3) + 1
        direct = [f for f in facts if f.dimensions.get("quarter") == quarter]
        if direct:
            return CalcValue(
                sum(f.value for f in direct), table.unit,
                {"op": "sum", "table": table.table_key, "subject": expr.subject,
                 "year": expr.year, "period": f"Q{quarter}", "direct": True},
                tuple(f.chunk_id for f in direct),
            )
    months: dict[int, tuple[float, int | None]] = {}
    for fact in facts:
        month = fact.dimensions.get("month")
        if month is not None:
            months[month] = (fact.value, fact.chunk_id)
    if expr.time.kind == "ytd":
        selected = expr.time.month_set(max_month=max(months, default=0))
    else:
        selected = expr.time.month_set()
    if expr.time.require_complete and not selected.issubset(months):
        return None
    present = [month for month in selected if month in months]
    if not present:
        return None
    return CalcValue(
        sum(months[month][0] for month in present), table.unit,
        {"op": "sum", "table": table.table_key, "subject": expr.subject,
         "year": expr.year, "months": sorted(present)},
        tuple(months[month][1] for month in present),
    )


def _op_compare(table: CanonicalTable, expr: CompareExpr) -> CalcValue | None:
    left = evaluate(table, expr.left)
    right = evaluate(table, expr.right)
    if left is None or right is None:
        return None
    if left.unit != right.unit:
        return None
    diff = left.value - right.value
    percent = (diff / right.value * 100.0) if right.value else None
    return CalcValue(
        diff,
        left.unit,
        {
            "op": "compare",
            "table": table.table_key,
            "left": left.value,
            "right": right.value,
            "diff": diff,
            "percent": percent,
            "left_provenance": left.provenance,
            "right_provenance": right.provenance,
        },
        (*left.chunk_ids, *right.chunk_ids),
    )


_REGISTRY: dict[str, Callable[[CanonicalTable, Any], CalcValue | None]] = {
    "lookup": _op_lookup,
    "sum": _op_sum,
    "compare": _op_compare,
}


def register_operator(kind: str, handler: Callable[[CanonicalTable, Any], CalcValue | None]) -> None:
    """后续 ratio/yoy/rank/join 通过这里注册，查询规划器只认注册表。"""
    _REGISTRY[kind] = handler


def evaluate(table: CanonicalTable, expr: CalcExpr) -> CalcValue | None:
    handler = _REGISTRY.get(expr.kind)
    if handler is None:
        return None
    return handler(table, expr)


def make_compare_plan(
    table_key: str, subject: str, years: tuple[int, int], time: TimeSpec
) -> CompareExpr:
    """两期比较的标准表达式（当前水费/季度/同比共用）。"""
    return CompareExpr(
        left=SumExpr(subject=subject, year=years[0], time=time),
        right=SumExpr(subject=subject, year=years[1], time=time),
    )


@dataclass(frozen=True)
class RatioExpr:
    numerator: LookupExpr | SumExpr
    denominator: LookupExpr | SumExpr
    kind: str = "ratio"


@dataclass(frozen=True)
class YoyExpr:
    current: LookupExpr | SumExpr
    previous: LookupExpr | SumExpr
    kind: str = "yoy"


@dataclass(frozen=True)
class RankExpr:
    year: int
    time: TimeSpec
    top: int = 3
    kind: str = "rank"


def _op_ratio(table: CanonicalTable, expr: RatioExpr) -> CalcValue | None:
    numerator = evaluate(table, expr.numerator)
    denominator = evaluate(table, expr.denominator)
    if numerator is None or denominator is None or denominator.value == 0:
        return None
    if numerator.unit != denominator.unit:
        return None
    return CalcValue(
        numerator.value / denominator.value * 100.0,
        "%",
        {"op": "ratio", "table": table.table_key,
         "numerator": numerator.value, "denominator": denominator.value,
         "numerator_provenance": numerator.provenance,
         "denominator_provenance": denominator.provenance},
        (*numerator.chunk_ids, *denominator.chunk_ids),
    )


def _op_yoy(table: CanonicalTable, expr: YoyExpr) -> CalcValue | None:
    current = evaluate(table, expr.current)
    previous = evaluate(table, expr.previous)
    if current is None or previous is None or previous.value == 0:
        return None
    if current.unit != previous.unit:
        return None
    return CalcValue(
        (current.value - previous.value) / previous.value * 100.0,
        "%",
        {"op": "yoy", "table": table.table_key,
         "current": current.value, "previous": previous.value,
         "current_provenance": current.provenance, "previous_provenance": previous.provenance},
        (*current.chunk_ids, *previous.chunk_ids),
    )


def _op_rank(table: CanonicalTable, expr: RankExpr) -> CalcValue | None:
    rows: list[tuple[float, str, Any]] = []
    for subject in table.subjects():
        value = evaluate(table, SumExpr(subject=subject, year=expr.year, time=expr.time))
        if value is not None:
            rows.append((value.value, subject, value))
    if not rows:
        return None
    rows.sort(key=lambda row: (-row[0], row[1]))
    top = rows[: max(1, int(expr.top))]
    first_value = top[0][2]
    return CalcValue(
        top[0][0],
        first_value.unit,
        {"op": "rank", "table": table.table_key, "year": expr.year,
         "ranking": [{"subject": subject, "value": score} for score, subject, _ in top]},
        tuple(first_value.chunk_ids),
    )


_REGISTRY.update({"ratio": _op_ratio, "yoy": _op_yoy, "rank": _op_rank})


@dataclass(frozen=True)
class AvgExpr:
    subject: str
    year: int
    time: TimeSpec
    kind: str = "avg"


def _op_avg(table: CanonicalTable, expr: AvgExpr) -> CalcValue | None:
    facts = _year_facts(table, expr.subject, expr.year)
    months: dict[int, tuple[float, int | None]] = {}
    for fact in facts:
        month = fact.dimensions.get("month")
        if month is not None:
            months[month] = (fact.value, fact.chunk_id)
    selected = expr.time.month_set(max_month=max(months, default=0))
    if expr.time.require_complete and not selected.issubset(months):
        return None
    present = [month for month in selected if month in months]
    if not present:
        return None
    return CalcValue(
        sum(months[m][0] for m in present) / len(present), table.unit,
        {"op": "avg", "table": table.table_key, "subject": expr.subject,
         "year": expr.year, "months": sorted(present)},
        tuple(months[m][1] for m in present),
    )


_REGISTRY["avg"] = _op_avg
