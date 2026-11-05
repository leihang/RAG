"""时间表达式解析：月份/季度/半年/YTD。"""

from __future__ import annotations

from ekbs.retrieval.table_time import parse_time_spec, time_label


def test_month_range() -> None:
    spec = parse_time_spec("2026年1-9月较2025年同期")
    assert spec is not None and spec.kind == "month_range"
    assert spec.start_month == 1 and spec.end_month == 9
    assert spec.month_set() == set(range(1, 10))


def test_quarter_and_half_and_ytd() -> None:
    q1 = parse_time_spec("2026年Q1比2025年Q1")
    assert q1 is not None and q1.kind == "quarter" and q1.month_set() == {1, 2, 3}
    q3 = parse_time_spec("2026年第三季度较2025年第三季度")
    assert q3 is not None and q3.month_set() == {7, 8, 9}
    half = parse_time_spec("2026年上半年较2025年上半年")
    assert half is not None and half.month_set() == set(range(1, 7))
    ytd = parse_time_spec("2026年YTD较2025年YTD")
    assert ytd is not None and ytd.kind == "ytd" and not ytd.require_complete
    assert time_label(ytd) == "年初至今"


def test_invalid_text_returns_none_until_recognized() -> None:
    assert parse_time_spec("没有期间的问题") is None
