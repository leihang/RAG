"""部分期间表格汇总：不得用年度合计代替 1-9 月。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.period_math import compute_period_aggregate


def _chunk(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content)


def test_company_water_fee_jan_sep_uses_month_columns() -> None:
    content = """表名: 年用水费统计对比（元）
相关表头: 日期 | 年       月份 | 1月 | 2月 | 3月 | 4月 | 5月 | 6月 | 7月 | 8月 | 9月 | 10月 | 11月 | 12月 | 年度合计
日期=公司承担水费; 年       月份=2025; 1月=8520.86; 2月=24444.10; 3月=12278.35; 4月=36500.71; 5月=14332.74; 6月=35569.18; 7月=26610.49; 8月=37169.85; 9月=16086.45; 10月=42512.16; 11月=16740.39; 12月=59856.67; 年度合计=330621.95
日期=公司承担水费; 年       月份=2026; 1月=52679.50; 2月=80913.80; 3月=76906.30; 4月=9227.15; 5月=8241.60; 6月=17404.15; 7月=23606.97; 8月=8518.63; 9月=286.25; 10月=0.00; 11月=0.00; 12月=0.00; 年度合计=277784.35
"""
    question = "公司承担水费，2026年1-9月总水费较2025年1-9月总水费是增长还是减少"
    result = compute_period_aggregate(question, [_chunk(content)])
    assert result is not None
    assert result.totals[2026] == 277784.35
    assert result.totals[2025] == 211512.73
    assert round(result.diff, 2) == 66271.62
    text = result.render()
    assert "增长" in text and "66,271.62" in text
    assert "330,621.95" not in text  # 不允许把年度合计当 1-9 月
    assert "[1]" in text


def test_no_partial_period_returns_none() -> None:
    content = "日期=公司承担水费; 年       月份=2025; 1月=1; 年度合计=1"
    assert compute_period_aggregate("公司承担水费2025年合计", [_chunk(content)]) is None
