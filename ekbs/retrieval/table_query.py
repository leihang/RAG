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

from ekbs.retrieval.table_calc import AvgExpr, SumExpr, evaluate
from ekbs.retrieval.table_canonical import CanonicalTable, canonical_from_rows
from ekbs.retrieval.table_plan import parse_table_plan
from ekbs.retrieval.table_struct import (
    TableRow,
    load_cell_facts,
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
    effective = spec
    if spec.kind == "ytd" and ytd_end is not None:
        effective = TimeSpec("month_range", 1, ytd_end, require_complete=False)
    calc = evaluate(table, SumExpr(subject=subject, year=year, time=effective))
    if calc is None:
        return None
    return calc.value, next((cid for cid in calc.chunk_ids if cid is not None), None)


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


@dataclass(frozen=True)
class TableValue:
    subject: str
    year: int
    value: float
    unit: str
    chunk_index: int
    label: str

    def render(self) -> str:
        mark = f"[{self.chunk_index}]" if self.chunk_index else ""
        return (
            f"{self.year}年{self.label}{self.subject}："
            f"{self.value:,.2f} {self.unit}{mark}"
        )


def _citation_context(chunks: Sequence[Any]) -> tuple[dict[int, int], dict[int, int]]:
    chunk_pos = {
        int(getattr(chunk, "chunk_id", 0)): index
        for index, chunk in enumerate(chunks, start=1)
        if int(getattr(chunk, "chunk_id", 0) or 0) > 0
    }
    table_pos: dict[int, int] = {}
    for index, chunk in enumerate(chunks, start=1):
        if str(getattr(chunk, "chunk_type", "")) == "table":
            table_pos.setdefault(int(getattr(chunk, "doc_id", 0) or 0), index)
    return chunk_pos, table_pos


def _value_from_tables(
    rows: Sequence[TableRow],
    text: str,
    year: int,
    time_spec: Any,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> TableValue | None:
    matches: list[tuple[CanonicalTable, str]] = []
    for table in canonical_from_rows(rows):
        if not table.unit.strip():
            continue
        for subject in table.subjects():
            if len(_normalize(subject)) >= 2 and _normalize(subject) in _normalize(text):
                matches.append((table, subject))
    if len({(table.caption, subject, table.unit) for table, subject in matches}) != 1:
        return None
    table, subject = matches[0]
    calc = evaluate(table, SumExpr(subject=subject, year=year, time=time_spec))
    if calc is None:
        return None
    chunk_id = next((cid for cid in calc.chunk_ids if cid is not None), None)
    index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
    if not index:
        return None
    return TableValue(
        subject=subject, year=year, value=calc.value, unit=calc.unit,
        chunk_index=index, label=time_label(time_spec),
    )


def _value_from_cells(
    chunks: Sequence[Any],
    text: str,
    year: int,
    time_spec: Any,
    db: Any | None,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> TableValue | None:
    # 宽表（用量日期 | 2026年9月 | ...）没有 TableRow，但可以直查单元格事实。
    if time_spec.kind not in {"month_range", "quarter"}:
        return None
    cells = (
        load_cell_facts(db, sorted({int(getattr(c, "doc_id", 0) or 0) for c in chunks}))
        if db is not None
        else []
    )
    if not cells:
        from ekbs.retrieval.table_struct import parse_cell_facts

        cells = parse_cell_facts(chunks)
    target = [cell for cell in cells if _normalize(cell.subject) in _normalize(text)]
    selected = time_spec.month_set()
    selected_cells = [
        cell for cell in target if cell.year == year and cell.month in selected
    ]
    if not selected_cells:
        return None
    if time_spec.require_complete and {
        cell.month for cell in selected_cells
    } < selected:
        return None
    first = selected_cells[0]
    index = chunk_pos.get(first.chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    return TableValue(
        subject=first.subject, year=first.year,
        value=sum(cell.value for cell in selected_cells),
        unit=first.unit, chunk_index=index, label=time_label(time_spec),
    )


def compute_table_value(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableValue | None:
    """单期数值直查（年度合计/单月/季度；含宽表单元格事实）。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    chunk_pos, table_pos = _citation_context(chunks)
    rows = _rows_for_question(db, chunks)
    if rows:
        value = _value_from_tables(rows, text, years[0], time_spec, chunk_pos, table_pos)
        if value is not None:
            return value
    return _value_from_cells(chunks, text, years[0], time_spec, db, chunk_pos, table_pos)


def compute_table_average(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableValue | None:
    """单期平均值（按所选月份 facts 求平均，不拿年度合计冒充平均）。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    rows = _rows_for_question(db, chunks)
    if not rows:
        return None
    chunk_pos, table_pos = _citation_context(chunks)
    matches: list[tuple[CanonicalTable, str]] = []
    for table in canonical_from_rows(rows):
        if not table.unit.strip():
            continue
        for subject in table.subjects():
            if len(_normalize(subject)) >= 2 and _normalize(subject) in _normalize(text):
                matches.append((table, subject))
    if len({(table.caption, subject, table.unit) for table, subject in matches}) != 1:
        return None
    table, subject = matches[0]
    calc = evaluate(table, AvgExpr(subject=subject, year=years[0], time=time_spec))
    if calc is None:
        return None
    chunk_id = next((cid for cid in calc.chunk_ids if cid is not None), None)
    index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
    if not index:
        return None
    label = "平均每月" if time_spec.kind == "year" else f"平均{time_label(time_spec)}"
    return TableValue(
        subject=subject, year=years[0], value=calc.value, unit=calc.unit,
        chunk_index=index, label=label,
    )


@dataclass(frozen=True)
class TableRatio:
    subject: str
    denominator_subject: str
    year: int
    percent: float
    numerator: float
    denominator: float
    unit: str
    chunk_index: int
    label: str

    def render(self) -> str:
        mark = f"[{self.chunk_index}]" if self.chunk_index else ""
        return (
            f"{self.year}年{self.label}{self.subject}占{self.denominator_subject}："
            f"{self.percent:.2f}%（{self.numerator:,.2f} / {self.denominator:,.2f} {self.unit}）{mark}"
        )


def compute_table_ratio(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableRatio | None:
    """占比/比例：同一表内两个指标在同一期间的比例。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    rows = _rows_for_question(db, chunks)
    chunk_pos, table_pos = _citation_context(chunks)
    if not rows:
        return _ratio_from_cells(text, years[0], time_spec, chunks, db, chunk_pos, table_pos)
    candidates: list[tuple[CanonicalTable, list[tuple[int, str]]]] = []
    normalized_text = _normalize(text)
    for table in canonical_from_rows(rows):
        if not table.unit.strip():
            continue
        hits: list[tuple[int, str]] = []
        for subject in table.subjects():
            norm = _normalize(subject)
            pos = normalized_text.find(norm)
            if len(norm) >= 2 and pos >= 0:
                hits.append((pos, subject))
        hits.sort()
        if len({subject for _pos, subject in hits}) >= 2:
            candidates.append((table, hits))
    if not candidates:
        return _ratio_from_cells(text, years[0], time_spec, chunks, db, chunk_pos, table_pos)
    if len(candidates) != 1:
        return None
    table, hits = candidates[0]
    numerator_subject, denominator_subject = hits[0][1], hits[1][1]
    numerator = evaluate(table, SumExpr(subject=numerator_subject, year=years[0], time=time_spec))
    denominator = evaluate(table, SumExpr(subject=denominator_subject, year=years[0], time=time_spec))
    if numerator is None or denominator is None or denominator.value == 0:
        return None
    if numerator.unit != denominator.unit:
        return None
    chunk_id = next((cid for cid in numerator.chunk_ids if cid is not None), None)
    index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
    if not index:
        return None
    return TableRatio(
        subject=numerator_subject,
        denominator_subject=denominator_subject,
        year=years[0],
        percent=numerator.value / denominator.value * 100.0,
        numerator=numerator.value,
        denominator=denominator.value,
        unit=table.unit,
        chunk_index=index,
        label=time_label(time_spec),
    )


def _ratio_from_cells(
    text: str,
    year: int,
    time_spec: TimeSpec,
    chunks: Sequence[Any],
    db: Any | None,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> TableRatio | None:
    cells = (
        load_cell_facts(db, sorted({int(getattr(c, "doc_id", 0) or 0) for c in chunks}))
        if db is not None
        else []
    )
    if not cells:
        from ekbs.retrieval.table_struct import parse_cell_facts

        cells = parse_cell_facts(chunks)
    normalized = _normalize(text)
    hits: dict[str, int] = {}
    for cell in cells:
        norm = _normalize(cell.subject)
        pos = normalized.find(norm)
        if len(norm) >= 2 and pos >= 0 and cell.year == year:
            hits.setdefault(cell.subject, pos)
    ordered = sorted(hits.items(), key=lambda item: item[1])
    if len(ordered) < 2:
        return None
    numerator_subject, denominator_subject = ordered[0][0], ordered[1][0]
    selected = time_spec.month_set()
    results: dict[str, float] = {}
    first_cells: dict[str, Any] = {}
    for subject in (numerator_subject, denominator_subject):
        matching = [
            cell for cell in cells
            if cell.subject == subject and cell.year == year and cell.month in selected
        ]
        if not matching:
            return None
        if time_spec.require_complete and {
            cell.month for cell in matching
        } < selected:
            return None
        results[subject] = sum(cell.value for cell in matching)
        first_cells[subject] = matching[0]
    denominator = results[denominator_subject]
    if denominator == 0:
        return None
    first = first_cells[numerator_subject]
    index = chunk_pos.get(first.chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    return TableRatio(
        subject=numerator_subject,
        denominator_subject=denominator_subject,
        year=year,
        percent=results[numerator_subject] / denominator * 100.0,
        numerator=results[numerator_subject],
        denominator=denominator,
        unit=first.unit,
        chunk_index=index,
        label=time_label(time_spec),
    )


@dataclass(frozen=True)
class TableRank:
    year: int
    label: str
    unit: str
    direction: str
    items: tuple[tuple[str, float], ...]
    chunk_index: int

    def render(self) -> str:
        mark = f"[{self.chunk_index}]" if self.chunk_index else ""
        word = "最低" if self.direction == "asc" else "最高"
        ranking = "；".join(
            f"{index}. {subject} {value:,.2f} {self.unit}"
            for index, (subject, value) in enumerate(self.items, start=1)
        )
        return f"{self.year}年{self.label}{word}排名：{ranking}{mark}"


def _rank_from_cells(
    cells: Sequence[Any],
    year: int,
    time_spec: TimeSpec,
    direction: str,
    top: int,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> TableRank | None:
    selected = time_spec.month_set()
    values: dict[str, float] = {}
    chunk_ids: dict[str, int | None] = {}
    units: dict[str, str] = {}
    for cell in cells:
        if cell.year != year or cell.month not in selected:
            continue
        values[cell.subject] = values.get(cell.subject, 0.0) + float(cell.value)
        chunk_ids.setdefault(cell.subject, cell.chunk_id)
        units.setdefault(cell.subject, cell.unit)
    if len(values) < 2:
        return None
    ordered = sorted(values.items(), key=lambda item: (item[1] if direction == "asc" else -item[1], item[0]))
    first_subject = ordered[0][0]
    chunk_id = chunk_ids.get(first_subject)
    first = next(cell for cell in cells if cell.subject == first_subject)
    index = chunk_pos.get(chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    return TableRank(
        year=year,
        label=time_label(time_spec),
        unit=units.get(first_subject, ""),
        direction=direction,
        items=tuple(ordered[: max(1, top)]),
        chunk_index=index,
    )


def compute_table_rank(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableRank | None:
    """单期指标排名（最高/最低/TopN），优先表格行，纯宽表走 cell 事实。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    chunk_pos, table_pos = _citation_context(chunks)
    top_match = re.search(r"(?:前|Top|top|TOP)\s*([一二三四五六七八九十\d]+)", text)
    top = 3
    if top_match:
        raw = top_match.group(1)
        top = int(raw) if raw.isdigit() else {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}.get(raw, 3)
    direction = "asc" if ("最低" in text or "最少" in text) else "desc"

    # 宽表单元格优先级更高：同一期间通常只有它保留完整指标列。
    cells = (
        load_cell_facts(db, sorted({int(getattr(c, "doc_id", 0) or 0) for c in chunks}))
        if db is not None
        else []
    )
    if not cells:
        from ekbs.retrieval.table_struct import parse_cell_facts

        cells = parse_cell_facts(chunks)
    cell_rank = _rank_from_cells(cells, years[0], time_spec, direction, top, chunk_pos, table_pos)
    if cell_rank is not None:
        return cell_rank

    rows = _rows_for_question(db, chunks)
    best: TableRank | None = None
    if rows:
        for table in canonical_from_rows(rows):
            if not table.unit.strip():
                continue
            values: list[tuple[str, float]] = []
            for subject in table.subjects():
                calc = evaluate(table, SumExpr(subject=subject, year=years[0], time=time_spec))
                if calc is not None:
                    values.append((subject, calc.value))
            if len(values) < 2:
                continue
            values.sort(key=lambda item: (item[1] if direction == "asc" else -item[1], item[0]))
            chunk_id = table.facts[0].chunk_id if table.facts else None
            index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
            if not index:
                continue
            candidate = TableRank(
                year=years[0], label=time_label(time_spec), unit=table.unit,
                direction=direction, items=tuple(values[: max(1, top)]), chunk_index=index,
            )
            if best is None or len(candidate.items) > len(best.items):
                best = candidate
    return best


@dataclass(frozen=True)
class TableBinary:
    left_subject: str
    right_subject: str
    year: int
    value: float
    unit: str
    op: str
    chunk_index: int
    label: str

    def render(self) -> str:
        mark = f"[{self.chunk_index}]" if self.chunk_index else ""
        word = {
            "add": "合计", "sub": "差额", "mul": "乘积", "div": "商",
        }.get(self.op, "结果")
        return (
            f"{self.year}年{self.label}{self.left_subject}与{self.right_subject}{word}："
            f"{self.value:,.2f} {self.unit}{mark}"
        )


def _binary_op_from_text(text: str) -> str:
    if any(word in text for word in ("乘以", "乘", "×", "*", "倍")):
        return "mul"
    if any(word in text for word in ("除以", "除", "/")):
        return "div"
    if any(word in text for word in ("减", "差额", "相差", "多多少", "少多少")):
        return "sub"
    return "add"


def compute_table_binary(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableBinary | None:
    """同表两个指标的加/减（表格行；宽表后续再接）。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    rows = _rows_for_question(db, chunks)
    chunk_pos, table_pos = _citation_context(chunks)
    normalized = _normalize(text)
    if not rows:
        return _binary_from_cells(text, years[0], time_spec, chunks, db, chunk_pos, table_pos)
    candidates: list[tuple[CanonicalTable, list[tuple[int, str]]]] = []
    for table in canonical_from_rows(rows):
        if not table.unit.strip():
            continue
        hits = [
            (normalized.find(_normalize(subject)), subject)
            for subject in table.subjects()
            if len(_normalize(subject)) >= 2 and _normalize(subject) in normalized
        ]
        hits.sort()
        if len({subject for _pos, subject in hits}) >= 2:
            candidates.append((table, hits))
    if not candidates:
        return _binary_from_cells(text, years[0], time_spec, chunks, db, chunk_pos, table_pos)
    if len(candidates) != 1:
        return None
    table, hits = candidates[0]
    left_subject, right_subject = hits[0][1], hits[1][1]
    op = _binary_op_from_text(text)
    left = evaluate(table, SumExpr(subject=left_subject, year=years[0], time=time_spec))
    right = evaluate(table, SumExpr(subject=right_subject, year=years[0], time=time_spec))
    if left is None or right is None or left.unit != right.unit:
        return None
    chunk_id = next((cid for cid in left.chunk_ids if cid is not None), None)
    index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
    if not index:
        return None
    if op == "add":
        value = left.value + right.value
    elif op == "sub":
        value = left.value - right.value
    elif op == "mul":
        value = left.value * right.value
    else:
        if right.value == 0:
            return None
        value = left.value / right.value
    return TableBinary(
        left_subject=left_subject, right_subject=right_subject, year=years[0],
        value=value, unit=left.unit, op=op, chunk_index=index, label=time_label(time_spec),
    )


def _binary_from_cells(
    text: str,
    year: int,
    time_spec: TimeSpec,
    chunks: Sequence[Any],
    db: Any | None,
    chunk_pos: dict[int, int],
    table_pos: dict[int, int],
) -> TableBinary | None:
    cells = (
        load_cell_facts(db, sorted({int(getattr(c, "doc_id", 0) or 0) for c in chunks}))
        if db is not None
        else []
    )
    if not cells:
        from ekbs.retrieval.table_struct import parse_cell_facts

        cells = parse_cell_facts(chunks)
    normalized = _normalize(text)
    hits: dict[str, int] = {}
    for cell in cells:
        norm = _normalize(cell.subject)
        pos = normalized.find(norm)
        if len(norm) >= 2 and pos >= 0 and cell.year == year:
            hits.setdefault(cell.subject, pos)
    ordered = sorted(hits.items(), key=lambda item: item[1])
    if len(ordered) < 2:
        return None
    left_subject, right_subject = ordered[0][0], ordered[1][0]
    selected = time_spec.month_set()
    values: dict[str, float] = {}
    first_cells: dict[str, Any] = {}
    for subject in (left_subject, right_subject):
        matching = [
            cell for cell in cells
            if cell.subject == subject and cell.year == year and cell.month in selected
        ]
        if not matching:
            return None
        if time_spec.require_complete and {cell.month for cell in matching} < selected:
            return None
        values[subject] = sum(cell.value for cell in matching)
        first_cells[subject] = matching[0]
    first = first_cells[left_subject]
    index = chunk_pos.get(first.chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    op = _binary_op_from_text(text)
    if op == "add":
        value = values[left_subject] + values[right_subject]
    elif op == "sub":
        value = values[left_subject] - values[right_subject]
    elif op == "mul":
        value = values[left_subject] * values[right_subject]
    else:
        if values[right_subject] == 0:
            return None
        value = values[left_subject] / values[right_subject]
    return TableBinary(
        left_subject=left_subject, right_subject=right_subject, year=year,
        value=value, unit=first.unit, op=op, chunk_index=index, label=time_label(time_spec),
    )


def _filter_terms(text: str) -> list[str]:
    patterns = (
        r"除(.+?)外",
        r"排除(.+?)(?:[，,。；;]|$)",
        r"不含(.+?)(?:[，,。；;]|$)",
        r"不包括(.+?)(?:[，,。；;]|$)",
        r"只算(.+?)(?:的|，|,|$)",
        r"仅(.+?)(?:的|，|,|$)",
        r"其中(.+?)(?:的|，|,|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return [part.strip() for part in re.split(r"[、和与]", match.group(1)) if part.strip()]
    return []


def _select_subjects(subjects: Sequence[str], terms: Sequence[str], mode: str) -> list[str]:
    matched = [
        subject for subject in subjects
        if any(_normalize(term) in _normalize(subject) for term in terms)
    ]
    if not matched:
        return []
    if mode == "include":
        return matched
    return [subject for subject in subjects if subject not in matched]


_METRIC_WORDS = ("电量", "电费", "水费", "水量", "费用", "金额")


def _metric_word(text: str) -> str:
    for word in _METRIC_WORDS:
        if word in text:
            return word
    return ""


def _drop_aggregate_subjects(selected: list[str]) -> list[str]:
    details = [
        subject for subject in selected
        if not _normalize(subject).startswith("总") and "合计" not in _normalize(subject)
    ]
    return details or selected


_REQUESTED_UNIT_RE = re.compile(r"(万元|元|度|千瓦时|KW/h|kwh|m³|立方米|%)", re.I)
_UNIT_ALIASES = {
    "kw/h": "kwh", "kwh": "kwh", "度": "kwh", "千瓦时": "kwh",
    "m3": "m³", "立方米": "m³", "m³": "m³",
    "元": "元", "万元": "万元", "%": "%",
}


def _requested_unit(text: str) -> str:
    match = _REQUESTED_UNIT_RE.search(text or "")
    return match.group(1) if match else ""


def _unit_alias(unit: str) -> str:
    return _UNIT_ALIASES.get(str(unit or "").strip().lower(), str(unit or "").strip())


def _convert_value(value: float, source_unit: str, target_unit: str) -> float | None:
    source = _unit_alias(source_unit)
    target = _unit_alias(target_unit)
    if not target or source == target:
        return value
    if source == "元" and target == "万元":
        return value / 10000.0
    if source == "万元" and target == "元":
        return value * 10000.0
    return None


def compute_table_filtered(  # noqa: C901
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableValue | None:
    """条件筛选后求和：``除住宅外`` / ``只算住宅`` 等。"""
    text = str(question or "")
    years = tuple(dict.fromkeys(int(y) for y in re.findall(r"(20\d{2})\s*年", text)))
    if len(years) != 1:
        return None
    terms = _filter_terms(text)
    if not terms:
        return None
    mode = "include" if any(word in text for word in ("只算", "仅", "其中")) else "exclude"
    from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

    time_spec = parse_time_spec(text) or TimeSpec("year", 1, 12, False)
    chunk_pos, table_pos = _citation_context(chunks)

    rows = _rows_for_question(db, chunks)
    if rows:
        metric = _metric_word(text)
        for table in canonical_from_rows(rows):
            if metric and metric not in table.caption and not any(
                metric in subject for subject in table.subjects()
            ):
                continue
            selected = _select_subjects(table.subjects(), terms, mode)
            if mode == "exclude":
                selected = _drop_aggregate_subjects(selected)
            if not selected:
                continue
            values: list[float] = []
            chunk_id: int | None = None
            for subject in selected:
                calc = evaluate(table, SumExpr(subject=subject, year=years[0], time=time_spec))
                if calc is None:
                    values = []
                    break
                values.append(calc.value)
                chunk_id = chunk_id or next((cid for cid in calc.chunk_ids if cid), None)
            if not values:
                continue
            index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
            if not index:
                continue
            label = f"仅{'、'.join(terms)}" if mode == "include" else f"除{'、'.join(terms)}外"
            requested = _requested_unit(text)
            value = sum(values)
            unit = table.unit
            if requested:
                converted = _convert_value(value, table.unit, requested)
                if converted is None:
                    continue
                value, unit = converted, requested
            return TableValue(
                subject="合计", year=years[0], value=value, unit=unit,
                chunk_index=index, label=label,
            )

    cells = (
        load_cell_facts(db, sorted({int(getattr(c, "doc_id", 0) or 0) for c in chunks}))
        if db is not None
        else []
    )
    if not cells:
        from ekbs.retrieval.table_struct import parse_cell_facts

        cells = parse_cell_facts(chunks)
    metric = _metric_word(text)
    metric_cells = [cell for cell in cells if not metric or metric in cell.subject]
    if metric and not metric_cells:
        metric_cells = [cell for cell in cells if metric in cell.caption]
    all_subjects = sorted({cell.subject for cell in metric_cells})
    selected = _select_subjects(all_subjects, terms, mode)
    if mode == "exclude":
        selected = _drop_aggregate_subjects(selected)
    if not selected:
        return None
    months = time_spec.month_set()
    values = [
        cell.value for cell in metric_cells
        if cell.subject in selected and cell.year == years[0] and cell.month in months
    ]
    if not values:
        return None
    first = next(
        cell for cell in metric_cells
        if cell.subject in selected and cell.year == years[0] and cell.month in months
    )
    index = chunk_pos.get(first.chunk_id or 0) or table_pos.get(first.doc_id) or 0
    if not index:
        return None
    label = f"仅{'、'.join(terms)}" if mode == "include" else f"除{'、'.join(terms)}外"
    requested = _requested_unit(text)
    value = sum(values)
    unit = first.unit
    if requested:
        converted = _convert_value(value, first.unit, requested)
        if converted is None:
            return None
        value, unit = converted, requested
    return TableValue(
        subject="合计", year=years[0], value=value, unit=unit,
        chunk_index=index, label=label,
    )
