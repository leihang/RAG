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

from ekbs.retrieval.table_struct import TableIndex, TableRow

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


def compute_table_comparison(
    question: str, chunks: Sequence[Any]
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

    index = TableIndex.from_chunks(chunks)
    subject_rows = index.find(subject)
    if not subject_rows:
        return None

    # 按 (表格, 单位) 分组，优先选能同时覆盖目标年份、且单位明确的表。
    groups: dict[tuple[str, str], list[TableRow]] = {}
    for row in subject_rows:
        if row.unit and row.unit.strip():
            groups.setdefault((row.caption, row.unit), []).append(row)
    candidates: list[tuple[tuple[str, str], dict[int, TableRow]]] = []
    for key, rows in groups.items():
        picked: dict[int, TableRow] = {}
        for year in years:
            year_rows = [row for row in rows if row.year == year and _valid_annual(row)]
            best = _best_row(year_rows, start, end)
            if best is not None:
                picked[year] = best
        if len(picked) >= 2:
            candidates.append((key, picked))
    if not candidates:
        return None
    # 选择第一张能覆盖最早两个年份的表；多表时保持稳定（按捕获顺序）。
    (_caption, unit), picked = candidates[0]
    year_a, year_b = years[0], years[1]
    if year_a not in picked or year_b not in picked:
        return None
    totals = {
        year: sum(picked[year].months[month] for month in range(start, end + 1))
        for year in (year_a, year_b)
    }
    diff = totals[year_a] - totals[year_b]
    percent = diff / totals[year_b] * 100.0 if totals[year_b] else None
    return TableComparison(
        subject=subject,
        start_month=start,
        end_month=end,
        years=(year_a, year_b),
        totals=totals,
        chunk_indices={year: picked[year].chunk_index for year in (year_a, year_b)},
        diff=diff,
        percent=percent,
        unit=unit,
    )
