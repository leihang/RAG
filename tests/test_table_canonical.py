"""统一表格模型：横表适配器 → CanonicalTable/Fact/Column。"""

from __future__ import annotations

from ekbs.retrieval.table_canonical import CrossTabAdapter
from ekbs.retrieval.table_struct import TableRow


def test_cross_tab_adapter_emits_long_facts_and_columns() -> None:
    rows = [
        TableRow(
            chunk_index=1,
            doc_id=1,
            caption="年用水费统计对比（元）",
            unit="元",
            subject="公司承担水费",
            year=2025,
            months={1: 10.0, 2: 20.0},
            annual_total=30.0,
            chunk_id=9,
        ),
        TableRow(
            chunk_index=1,
            doc_id=1,
            caption="年用水费统计对比（元）",
            unit="元",
            subject="公司承担水费",
            year=2026,
            months={1: 12.0, 2: 22.0},
            annual_total=34.0,
            chunk_id=9,
        ),
    ]
    tables = CrossTabAdapter.from_rows(rows)
    assert len(tables) == 1
    table = tables[0]
    assert table.unit == "元"
    assert {column.dimension for column in table.columns} >= {"year", "month", "total"}
    assert table.sum_period("公司承担水费", years=[2026], month_from=1, month_to=2)[2026][0] == 34.0
    assert any(
        fact.dimensions.get("total") == 1 and fact.value == 30.0 for fact in table.facts
    )
