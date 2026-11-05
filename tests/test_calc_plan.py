"""数值问题计划路由。"""

from __future__ import annotations

from ekbs.retrieval.calc_plan import generate_calc_plan


def test_compare_plan() -> None:
    plan = generate_calc_plan("公司承担水费，2026年1-9月较2025年1-9月是增长还是减少")
    assert plan is not None and plan.operation == "compare"
    assert plan.years == (2026, 2025)
    assert plan.time.kind == "month_range"


def test_value_plan() -> None:
    plan = generate_calc_plan("2026年总电量是多少")
    assert plan is not None and plan.operation == "value"
    assert plan.years == (2026,)
    assert plan.subject_hint == "总电量"


def test_non_numeric_returns_none() -> None:
    assert generate_calc_plan("工程经理的职责是什么") is None
