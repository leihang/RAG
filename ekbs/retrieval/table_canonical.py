"""表格统一语义模型（Phase 0）。

不同来源/表型最终都归一为：

* ``TableColumn``：列的时间维度（year/month/quarter/ytd/total）与角色；
* ``TableFact``：一行/一格的长表事实（subject + dimensions + value + unit）；
* ``CanonicalTable``：一张表 + 列 + 事实 + 来源。

查询执行器只认 ``CanonicalTable``，不认原始 chunk 文本、表头布局或文件类型。
横表适配器（``CrossTabAdapter``）把当前 ``TableRow``（年份×月份）转成长表事实；
以后季度表/记录表/表单表只需要增加适配器，查询算子不动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ekbs.retrieval.table_struct import TableRow


@dataclass(frozen=True)
class TableColumn:
    label: str
    dimension: str              # year | month | quarter | ytd | total | other
    ordinal: int | None = None
    role: str = "period"        # period | measure | total | meta
    unit: str = ""


@dataclass(frozen=True)
class TableFact:
    subject: str
    dimensions: dict[str, int]  # {"year": 2026, "month": 9}
    value: float
    unit: str
    doc_id: int
    chunk_id: int | None
    caption: str
    table_key: str


@dataclass
class CanonicalTable:
    table_key: str
    caption: str
    doc_id: int
    unit: str
    columns: list[TableColumn] = field(default_factory=list)
    facts: list[TableFact] = field(default_factory=list)

    def subjects(self) -> list[str]:
        return sorted({fact.subject for fact in self.facts})

    def facts_for(self, subject: str) -> list[TableFact]:
        return [fact for fact in self.facts if fact.subject == subject]

    def sum_period(
        self,
        subject: str,
        *,
        years: Sequence[int],
        month_from: int,
        month_to: int,
    ) -> dict[int, tuple[float, int | None]]:
        """按年份累加 month 维度；返回 ``{year: (sum, chunk_id)}``。"""
        out: dict[int, tuple[float, int | None]] = {}
        for fact in self.facts_for(subject):
            year = fact.dimensions.get("year")
            month = fact.dimensions.get("month")
            if year not in years or month is None:
                continue
            if not (month_from <= month <= month_to):
                continue
            total, chunk_id = out.get(year, (0.0, None))
            out[year] = (total + fact.value, chunk_id or fact.chunk_id)
        return out


class CrossTabAdapter:
    """年份行 × 月份列（当前 table_row_index 的横表）→ CanonicalTable。"""

    @staticmethod
    def from_rows(rows: Sequence[TableRow]) -> list[CanonicalTable]:
        groups: dict[tuple[str, str], CanonicalTable] = {}
        for row in rows:
            key = (row.caption, row.unit)
            table = groups.get(key)
            if table is None:
                table = CanonicalTable(
                    table_key=f"{row.caption}|{row.unit}",
                    caption=row.caption,
                    doc_id=row.doc_id,
                    unit=row.unit,
                )
                groups[key] = table
            if not table.columns:
                table.columns = [
                    TableColumn(label="年份", dimension="year"),
                    *[
                        TableColumn(label=f"{month}月", dimension="month", ordinal=month)
                        for month in range(1, 13)
                    ],
                    TableColumn(label="年度合计", dimension="total", role="total"),
                ]
            for month, value in row.months.items():
                table.facts.append(
                    TableFact(
                        subject=row.subject,
                        dimensions={"year": row.year, "month": month},
                        value=float(value),
                        unit=row.unit,
                        doc_id=row.doc_id,
                        chunk_id=row.chunk_id,
                        caption=row.caption,
                        table_key=table.table_key,
                    )
                )
            if row.annual_total is not None:
                table.facts.append(
                    TableFact(
                        subject=row.subject,
                        dimensions={"year": row.year, "total": 1},
                        value=float(row.annual_total),
                        unit=row.unit,
                        doc_id=row.doc_id,
                        chunk_id=row.chunk_id,
                        caption=row.caption,
                        table_key=table.table_key,
                    )
                )
        return list(groups.values())


def canonical_from_rows(rows: Sequence[TableRow]) -> list[CanonicalTable]:
    return CrossTabAdapter.from_rows(rows)


def flatten_tables(tables: Iterable[CanonicalTable]) -> list[TableFact]:
    return [fact for table in tables for fact in table.facts]
