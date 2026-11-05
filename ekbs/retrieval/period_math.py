"""部分期间的表格汇总（确定性计算，避免 LLM 把「年度合计」当 1-9 月合计）。

用户实测：钉钉问「公司承担水费 2026年1-9月 比 2025年1-9月」时，LLM 把 2025 全年
`年度合计=330621.95` 当成 1-9 月合计，结论从「减少 66,271.62」变成「减少 52,837.60」；
UI 另一路碰巧做对了逐月累加。这一类问题不适合交给自由文本模型做算术，这里直接按
表格的 1月..N月 列求和，再套模板回答。

只处理同时满足以下条件的问法：
* 问题含月份区间（``1-9月`` / ``1—9月`` / ``1至9月``）；
* 问题里出现两个及以上年份；
* 命中分片里有「年 + 月份 + 1月..12月」表，且能找到同名的指标行。
不满足就让原 LLM 链路继续，避免过度接管。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

#: 数字里允许千分位、负号、括号负数
_NUMBER_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_PERIOD_RE = re.compile(r"(\d{1,2})\s*[-—–~～至到]\s*(\d{1,2})\s*月")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_MONTH_KEYS = {f"{i}月": i for i in range(1, 13)}
_YEAR_KEYS = ("年       月份", "年月份", "年份", "年")
_SUBJECT_KEYS = ("日期", "项目", "类别", "指标", "名称")
_PUNCT_RE = re.compile(r"[，,。；;：:？?！!\s]+")


@dataclass(frozen=True)
class PeriodAggregate:
    """一次确定性的期间对比结果。"""

    subject: str
    start_month: int
    end_month: int
    years: tuple[int, ...]
    totals: dict[int, float]
    chunk_indices: dict[int, int]
    diff: float
    percent: float | None
    unit: str = "元"

    def render(self) -> str:
        year_a, year_b = self.years[0], self.years[1]
        unit = self.unit
        a, b = self.totals[year_a], self.totals[year_b]
        cite_a = self.chunk_indices.get(year_a)
        cite_b = self.chunk_indices.get(year_b)
        mark_a = f"[{cite_a}]" if cite_a else ""
        mark_b = f"[{cite_b}]" if cite_b else ""
        direction = "增长" if self.diff > 0 else ("减少" if self.diff < 0 else "持平")
        amount = f"{abs(self.diff):,.2f}"
        pct = f"（约 {self.percent:+.2f}%）" if self.percent is not None else ""
        return (
            f"{year_a}年{self.start_month}—{self.end_month}月{self.subject}合计为 "
            f"{a:,.2f} {unit}{mark_a}；{year_b}年同期"
            f"（{self.start_month}—{self.end_month}月）合计为 {b:,.2f} {unit}{mark_b}；"
            f"因此 {year_a}年{self.start_month}—{self.end_month}月较 {year_b}年同期"
            f"**{direction}** {amount} {unit}{pct}。"
        )


def _number(text: str) -> float | None:
    raw = _NUMBER_RE.search(str(text or ""))
    if not raw:
        return None
    value = raw.group(0).replace(",", "")
    negative = value.startswith("(") and value.endswith(")")
    try:
        number = float(value.strip("()"))
    except ValueError:
        return None
    return -number if negative else number


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or ""))


def _subject_from_question(question: str) -> str:
    # 取第一个逗号前的核心指标；“公司承担水费，2026年...” → 公司承担水费
    head = re.split(r"[，,。；;：:]", str(question or ""), maxsplit=1)[0]
    head = re.sub(r"20\d{2}\s*年.*$", "", head)
    return head.strip()


def _parse_row(line: str) -> dict[str, str] | None:
    if "=" not in line:
        return None
    pairs: dict[str, str] = {}
    for part in line.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs or None


def _row_subject(row: dict[str, str]) -> str:
    for key in _SUBJECT_KEYS:
        value = row.get(key)
        if value:
            return value.strip()
    return ""


def _collect_sums(
    chunks: Sequence[Any],
    *,
    subject: str,
    years: tuple[int, ...],
    start: int,
    end: int,
) -> tuple[dict[int, float], dict[int, bool], dict[int, int]]:
    sums = {year: 0.0 for year in years}
    seen = {year: False for year in years}
    indices: dict[int, int] = {}
    target = _normalize(subject)
    for chunk_index, chunk in enumerate(chunks, start=1):
        content = str(getattr(chunk, "content", "") or "")
        for line in content.splitlines():
            row = _parse_row(line)
            if not row:
                continue
            row_subject = _row_subject(row)
            if not row_subject or target not in _normalize(row_subject):
                continue
            year_value = next((row[key] for key in _YEAR_KEYS if row.get(key)), "")
            year_match = re.search(r"(20\d{2})", year_value)
            if not year_match:
                continue
            year = int(year_match.group(1))
            if year not in sums:
                continue
            total = 0.0
            found = False
            for key, value in row.items():
                month = _MONTH_KEYS.get(key.strip())
                if month is None or not (start <= month <= end):
                    continue
                number = _number(value)
                if number is None:
                    continue
                total += number
                found = True
            if found:
                sums[year] += total
                seen[year] = True
                indices.setdefault(year, chunk_index)
    return sums, seen, indices


def compute_period_aggregate(
    question: str, chunks: Sequence[Any]
) -> PeriodAggregate | None:
    """若问题可确定性汇总，返回结果；否则 None。"""
    period = _PERIOD_RE.search(str(question or ""))
    if not period:
        return None
    start, end = int(period.group(1)), int(period.group(2))
    if not (1 <= start <= end <= 12):
        return None
    years = tuple(dict.fromkeys(int(year) for year in _YEAR_RE.findall(str(question or ""))))
    if len(years) < 2:
        return None
    subject = _subject_from_question(question)
    if len(_normalize(subject)) < 2:
        return None

    # 当前只接管以「（元）」为金额口径的表；其它数量/百分比表交给原 LLM 链路，
    # 避免硬编码单位把「万度/万元/百分比」答错。
    unit_text = " ".join(
        str(getattr(chunk, attr, "") or "")
        for chunk in chunks
        for attr in ("content", "heading_path", "doc_title")
    )
    if "（元）" not in unit_text and "(元)" not in unit_text:
        return None
    sums, seen, indices = _collect_sums(
        chunks, subject=subject, years=tuple(years[:2]), start=start, end=end
    )
    if not all(seen.get(year) for year in years[:2]):
        return None
    year_a, year_b = years[:2]
    diff = sums[year_a] - sums[year_b]
    percent = (diff / sums[year_b] * 100.0) if sums[year_b] else None
    return PeriodAggregate(
        subject=subject,
        start_month=start,
        end_month=end,
        years=(year_a, year_b),
        totals=sums,
        chunk_indices=indices,
        diff=diff,
        percent=percent,
    )
