"""表格查询计划 + 候选消歧。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.table_plan import parse_table_plan
from ekbs.retrieval.table_query import compute_table_comparison

BASE = """表名: {caption}
相关表头: 日期 | 年       月份 | 1月 | 2月 | 3月 | 4月 | 5月 | 6月 | 7月 | 8月 | 9月 | 10月 | 11月 | 12月 | 年度合计
日期=公司承担水费; 年       月份=2025; 1月=10; 2月=10; 3月=10; 4月=10; 5月=10; 6月=10; 7月=10; 8月=10; 9月=10; 10月=10; 11月=10; 12月=10; 年度合计=120
日期=公司承担水费; 年       月份=2026; 1月=20; 2月=20; 3月=20; 4月=20; 5月=20; 6月=20; 7月=20; 8月=20; 9月=20; 10月=20; 11月=20; 12月=20; 年度合计=240
"""


def _chunk(caption: str, unit: str) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=1, doc_id=1, chunk_type="table",
        content=BASE.format(caption=f"{caption}（{unit}）"),
        heading_path="分析", doc_title="分析",
    )


def test_plan_compile() -> None:
    plan = parse_table_plan("公司承担水费，2026年Q1较2025年Q1是增长还是减少")
    assert plan is not None
    assert plan.subject == "公司承担水费"
    assert plan.years == (2026, 2025)
    assert plan.time.kind == "quarter"


def test_ambiguous_caption_is_rejected() -> None:
    question = "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少"
    assert compute_table_comparison(question, [_chunk("表A", "元"), _chunk("表B", "元")]) is None


def test_ambiguous_unit_is_rejected() -> None:
    question = "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少"
    assert compute_table_comparison(question, [_chunk("表A", "元"), _chunk("表A", "万元")]) is None
