"""确定性表格问答：按“指标行 × 年份 × 月份区间”求和/比较。

与 :mod:`table_struct` 配套：不再写死“元/1-9月/前两年”的单点规则，而是：
* 从问题里解析月份区间、年份、指标；
* 从命中的结构化表行里按列求和；
* 用表名里的单位（元/万元/度/m³…）回答；
* 同表 1-12 月之和与「年度合计」可校验时先校验，避免拿脏表硬算。

无法可靠解析时返回 None，继续走原 LLM 链路。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from ekbs.retrieval.table_canonical import CanonicalTable, canonical_from_rows
from ekbs.retrieval.table_struct import TableRow, load_table_rows

_PERIOD_RE = re.compile(r"(\d{1,2})\s*[-—–~～至到]\s*(\d{1,2})\s*月")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_COMPARE_WORDS = ("增长", "减少", "对比", "较", "同比", "增加", "下降")


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。；;：:、]+", "", str(text or ""))


def _subject_from_question(question: str) -> str:
    head = re.split(r"[，,。；;：:]", str(question or ""), maxsplit=1)[0]
    head = re.sub(r"20\d{2}\s*年.*$", "", head)
    return head.strip()


@dataclass(frozen=True)
class TableComparison:
    subject: str
    start_month: int
    end_month: int
    years: tuple[int, int]
    totals: dict[int, float]
    chunk_indices: dict[int, int]
    diff: float
    percent: float | None
    unit: str

    def render(self) -> str:
        year_a, year_b = self.years
        a, b = self.totals[year_a], self.totals[year_b]
        mark_a = f"[{self.chunk_indices[year_a]}]" if year_a in self.chunk_indices else ""
        mark_b = f"[{self.chunk_indices[year_b]}]" if year_b in self.chunk_indices else ""
        direction = "增长" if self.diff > 0 else ("减少" if self.diff < 0 else "持平")
        pct = f"（约 {self.percent:+.2f}%）" if self.percent is not None else ""
        return (
            f"{year_a}年{self.start_month}—{self.end_month}月{self.subject}合计为 "
            f"{a:,.2f} {self.unit}{mark_a}；{year_b}年同期"
            f"（{self.start_month}—{self.end_month}月）合计为 {b:,.2f} {self.unit}{mark_b}；"
            f"因此 {year_a}年{self.start_month}—{self.end_month}月较 {year_b}年同期"
            f"**{direction}** {abs(self.diff):,.2f} {self.unit}{pct}。"
        )


def _best_row(rows: list[TableRow], start: int, end: int) -> TableRow | None:
    complete = [row for row in rows if all(month in row.months for month in range(start, end + 1))]
    if not complete:
        return None
    complete.sort(key=lambda row: (len(row.months), row.chunk_index), reverse=True)
    return complete[0]


def _valid_annual(row: TableRow) -> bool:
    """若行含全年 1-12 月与年度合计，先校验；不一致的表不接管。"""
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
    # 过渡兜底：尚未回填索引/旧测试直接传 chunk 时，仍从文本解析。
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


def compute_table_comparison(
    question: str, chunks: Sequence[Any], *, db: Any | None = None
) -> TableComparison | None:
    """能确定性比较则返回结果；否则 None。"""
    period = _PERIOD_RE.search(str(question or ""))
    years = tuple(dict.fromkeys(int(y) for y in _YEAR_RE.findall(str(question or ""))))
    if not period or len(years) < 2:
        return None
    start, end = int(period.group(1)), int(period.group(2))
    if not (1 <= start <= end <= 12):
        return None
    if not any(word in str(question or "") for word in _COMPARE_WORDS):
        return None
    subject = _subject_from_question(question)
    if len(_normalize(subject)) < 2:
        return None

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

    for table in _candidate_tables(rows, subject):
        totals: dict[int, tuple[float, int | None]] = {}
        valid = True
        for year in years:
            year_facts = [
                fact
                for fact in table.facts_for(subject)
                if fact.dimensions.get("year") == year
                and fact.dimensions.get("month") is not None
            ]
            present = {fact.dimensions["month"] for fact in year_facts}
            if not all(month in present for month in range(start, end + 1)):
                valid = False
                break
            total = sum(fact.value for fact in year_facts if start <= fact.dimensions["month"] <= end)
            chunk_id = next((fact.chunk_id for fact in year_facts), None)
            totals[year] = (total, chunk_id)
        if not valid or len(totals) < 2:
            continue
        year_a, year_b = years[0], years[1]
        a, b = totals[year_a][0], totals[year_b][0]
        diff = a - b
        percent = diff / b * 100.0 if b else None
        indices: dict[int, int] = {}
        for year in (year_a, year_b):
            chunk_id = totals[year][1]
            index = chunk_pos.get(chunk_id or 0) or table_pos.get(table.doc_id) or 0
            if index:
                indices[year] = index
        return TableComparison(
            subject=subject,
            start_month=start,
            end_month=end,
            years=(year_a, year_b),
            totals={year_a: a, year_b: b},
            chunk_indices=indices,
            diff=diff,
            percent=percent,
            unit=table.unit,
        )
    return None
