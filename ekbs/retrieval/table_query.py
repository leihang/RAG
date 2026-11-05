"""确定性表格问答：时间表达式 + 统一表事实 + 模板回答。

流程：
1. ``table_time.parse_time_spec`` 解析月份区间/季度/半年/YTD；
2. 读 ``table_row_index``（过渡期回退 chunk）→ ``CanonicalTable``；
3. 按指标 + 年份 + 时间集合筛选 facts，代码求和/比较；
4. 模板回填，不让 LLM 做算术。

覆盖不了时返回 None，继续原 prose 链路。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ekbs.retrieval.table_canonical import CanonicalTable, canonical_from_rows
from ekbs.retrieval.table_plan import parse_table_plan
from ekbs.retrieval.table_struct import (
    TableRow,
    load_record_facts,
    load_table_rows,
)
from ekbs.retrieval.table_time import TimeSpec, time_label



def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。；;：:、]+", "", str(text or ""))


@dataclass(frozen=True)
class TableComparison:
    subject: str
    start_month: int
    end_month: int
    years: tuple[int, ...]
    totals: dict[int, float]
    chunk_indices: dict[int, int]
    diff: float
    percent: float | None
    unit: str
    label: str = ""

    def _marks(self, year: int) -> str:
        return f"[{self.chunk_indices[year]}]" if year in self.chunk_indices else ""

    def render(self) -> str:
        label = self.label or f"{self.start_month}—{self.end_month}月"
        if len(self.years) == 2:
            year_a, year_b = self.years
            a, b = self.totals[year_a], self.totals[year_b]
            direction = "增长" if self.diff > 0 else ("减少" if self.diff < 0 else "持平")
            pct = f"（约 {self.percent:+.2f}%）" if self.percent is not None else ""
            return (
                f"{year_a}年{label}{self.subject}合计为 {a:,.2f} {self.unit}{self._marks(year_a)}；"
                f"{year_b}年同期（{label}）合计为 {b:,.2f} {self.unit}{self._marks(year_b)}；"
                f"因此 {year_a}年{label}较 {year_b}年同期**{direction}** "
                f"{abs(self.diff):,.2f} {self.unit}{pct}。"
            )
        lines = [
            f"{year}年{label}{self.subject}合计为 {self.totals[year]:,.2f} {self.unit}{self._marks(year)}"
            for year in self.years
        ]
        changes: list[str] = []
        for previous, current in zip(self.years, self.years[1:]):
            before, after = self.totals[current], self.totals[previous]
            delta = after - before
            direction = "增长" if delta > 0 else ("减少" if delta < 0 else "持平")
            changes.append(f"{current}年较{previous}年{direction} {abs(delta):,.2f} {self.unit}")
        return "；".join(lines) + "。对比：" + "；".join(changes) + "。"


def _valid_annual(row: TableRow) -> bool:
    if row.annual_total is None or not all(month in row.months for month in range(1, 13)):
        return True
    total = sum(row.months.values())
    return abs(total - row.annual_total) <= max(abs(row.annual_total) * 1e-6, 0.01)


def _rows_for_question(db: Any | None, chunks: Sequence[Any]) -> list[TableRow]:
    if db is not None:
        doc_ids = sorted({int(getattr(chunk, "doc_id", 0) or 0) for chunk in chunks})
        rows = load_table_rows(db, doc_ids)
        if rows:
            return [row for row in rows if _valid_annual(row)]
    from ekbs.retrieval.table_struct import TableIndex

    return [row for row in TableIndex.from_chunks(chunks).rows if _valid_annual(row)]


def _candidate_tables(rows: Sequence[TableRow], subject: str) -> list[CanonicalTable]:
    tables = canonical_from_rows(rows)
    target = _normalize(subject)
    out: list[CanonicalTable] = []
    for table in tables:
        if not table.unit.strip():
            continue
        facts = [fact for fact in table.facts if target in _normalize(fact.subject)]
        if facts:
            table.facts = facts
            out.append(table)
    return out


def _year_months(
    table: CanonicalTable, subject: str, year: int
) -> dict[int, tuple[float, int | None]]:
    out: dict[int, tuple[float, int | None]] = {}
    for fact in table.facts_for(subject):
        if fact.dimensions.get("year") != year:
            continue
        month = fact.dimensions.get("month")
        if month is None:
            continue
        out[month] = (fact.value, fact.chunk_id)
    return out


def _sum_for_spec(
    table: CanonicalTable,
    subject: str,
    year: int,
    spec: TimeSpec,
    *,
    ytd_end: int | None = None,
) -> tuple[float, int | None] | None:
    months = _year_months(table, subject, year)
    if not months:
        return None
    if spec.kind == "ytd":
        selected = spec.month_set(max_month=ytd_end or max(months))
    else:
        selected = spec.month_set()
    if spec.require_complete and not selected.issubset(months):
        return None
    present = [month for month in selected if month in months]
    if not present:
        return None
    total = sum(months[month][0] for month in present)
    chunk_id = next((months[month][1] for month in present if months[month][1] is not None), None)
    return total, chunk_id


def _pick_candidate(
    rows: Sequence[TableRow],
    subject: str,
    years: Sequence[int],
    spec: TimeSpec,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> tuple[CanonicalTable, tuple[int, ...], dict[int, float], dict[int, int]] | None:
    candidates: list[tuple[CanonicalTable, dict[int, tuple[float, int | None]]]] = []
    for table in _candidate_tables(rows, subject):
        ytd_end: int | None = None
        if spec.kind == "ytd":
            # 同期口径：用**最新年份**在表里的最大月份做截止，所有年份用同一个月，
            # 否则 2026 YTD 会对 2025 全年（12 月）算，口径不一致。
            ytd_end = max(
                (
                    month
                    for fact in table.facts_for(subject)
                    if fact.dimensions.get("year") in years
                    for month in [fact.dimensions.get("month")]
                    if month is not None
                ),
                default=None,
            )
        sums: dict[int, tuple[float, int | None]] = {}
        for year in years:
            value = _sum_for_spec(table, subject, year, spec, ytd_end=ytd_end)
            if value is not None:
                sums[year] = value
        if len(sums) >= 2:
            candidates.append((table, sums))
    if not candidates:
        return None
    # 多候选：单位不同，或表名不同且问题没有点名 → 不静默选第一个。
    if len({table.unit for table, _ in candidates}) > 1:
        return None
    if len({(table.caption, table.unit) for table, _ in candidates}) > 1:
        return None
    table, sums = candidates[0]
    common_years = tuple(year for year in years if year in sums)
    if len(common_years) < 2:
        return None
    totals = {year: sums[year][0] for year in common_years}
    indices: dict[int, int] = {}
    for year in common_years:
        chunk_id = sums[year][1]
        index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
        if index:
            indices[year] = index
    return table, common_years, totals, indices


def compute_table_comparison(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableComparison | None:
    """能确定性比较则返回结果；否则 None。"""
    plan = parse_table_plan(question)
    if plan is None:
        return None
    spec = plan.time
    years = plan.years
    subject = plan.subject

    rows = _rows_for_question(db, chunks)
    if not rows:
        return None
    chunk_pos = {
        int(getattr(chunk, "chunk_id", 0)): index
        for index, chunk in enumerate(chunks, start=1)
        if int(getattr(chunk, "chunk_id", 0) or 0) > 0
    }
    table_pos: dict[int, int] = {}
    for index, chunk in enumerate(chunks, start=1):
        if str(getattr(chunk, "chunk_type", "")) == "table":
            table_pos.setdefault(int(getattr(chunk, "doc_id", 0) or 0), index)

    picked = _pick_candidate(rows, subject, years, spec, chunk_pos, table_pos)
    if picked is None:
        return None
    table, common_years, totals, indices = picked
    diff = totals[common_years[0]] - totals[common_years[1]]
    percent = diff / totals[common_years[1]] * 100.0 if totals[common_years[1]] else None
    label = time_label(spec)
    # 后置硬校验：没有引用来源/单位/有限数值的结果不接管，避免“算出来但没有依据”。
    if not indices or not table.unit.strip():
        return None
    if any(not math.isfinite(value) for value in totals.values()):
        return None
    return TableComparison(
        subject=subject,
        start_month=int(spec.start_month or 1),
        end_month=int(spec.end_month or 12),
        years=common_years,
        totals=totals,
        chunk_indices=indices,
        diff=diff,
        percent=percent,
        unit=table.unit,
        label=label,
    )


_LOOKUP_RE = re.compile(
    r"^(?P<subject>.+?)的(?P<field>.+?)(?:是什么|是多少|有哪些|怎么规定|如何规定|是哪|有哪|？|\?|$)"
)


@dataclass(frozen=True)
class TableLookup:
    subject: str
    field: str
    value: str
    chunk_index: int
    caption: str = ""

    def render(self) -> str:
        mark = f"[{self.chunk_index}]" if self.chunk_index else ""
        return f"{self.subject}的{self.field}：{self.value}{mark}"


def compute_table_lookup(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableLookup | None:
    """记录表/表单的确定性查值（subject + field 唯一时）。"""
    match = _LOOKUP_RE.match(str(question or "").strip())
    if not match:
        return None
    subject = match.group("subject").strip()
    field = match.group("field").strip()
    if len(_normalize(subject)) < 2 or len(_normalize(field)) < 1:
        return None
    doc_ids = sorted({int(getattr(chunk, "doc_id", 0) or 0) for chunk in chunks})
    records = load_record_facts(db, doc_ids) if db is not None else []
    if not records:
        from ekbs.retrieval.table_struct import parse_record_facts

        records = parse_record_facts(chunks)
    target_subject = _normalize(subject)
    target_field = _normalize(field)
    chunk_pos = {
        int(getattr(chunk, "chunk_id", 0)): index
        for index, chunk in enumerate(chunks, start=1)
        if int(getattr(chunk, "chunk_id", 0) or 0) > 0
    }
    table_pos: dict[int, int] = {}
    for index, chunk in enumerate(chunks, start=1):
        if str(getattr(chunk, "chunk_type", "")) == "table":
            table_pos.setdefault(int(getattr(chunk, "doc_id", 0) or 0), index)
    matched = [
        record
        for record in records
        if (
            target_subject in _normalize(record.subject)
            or _normalize(record.subject) in target_subject
        )
        and (
            target_field in _normalize(record.field)
            or _normalize(record.field) in target_field
        )
    ]
    values = {record.value.strip() for record in matched if record.value.strip()}
    if len(values) != 1:
        return None
    first = matched[0]
    index = chunk_pos.get(first.chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    return TableLookup(
        subject=first.subject,
        field=first.field,
        value=next(iter(values)),
        chunk_index=index,
        caption=first.caption,
    )
