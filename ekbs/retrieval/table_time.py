"""表格问题的时间表达式解析（通用，不绑定某张表）。

支持的问法：
* 月份区间：``1-9月`` / ``1—9月`` / ``1至9月`` / ``前9个月``；
* 季度：``Q1`` / ``一季度`` / ``第一季度``；
* 半年：``上半年`` / ``下半年``；
* YTD：``YTD`` / ``年初至今`` / ``年初以来``；
* 全年/整年。

解析器只返回“要哪些月份 + 是否要求月份完整”，具体由执行器从 CanonicalTable
的 month facts 里取数；季度/YTD 不要求源表一定有季度列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4}
_MONTH_RANGE_RE = re.compile(r"(\d{1,2})\s*[-—–~～至到]\s*(\d{1,2})\s*月")
_SINGLE_MONTH_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*月")
_QUARTER_RE = re.compile(r"(?i)(?:第)?([一二三四1-4])\s*(?:季度|季)|Q([1-4])")
_HALF_RE = re.compile(r"(上|下)\s*半年")
_YTD_RE = re.compile(r"(?i)YTD|年初至今|年初以来|今年以来")
_FIRST_N_RE = re.compile(r"前\s*(\d{1,2})\s*个?月")


@dataclass(frozen=True)
class TimeSpec:
    kind: str                       # month_range | quarter | half | ytd | year
    start_month: int | None = None
    end_month: int | None = None
    require_complete: bool = True

    def month_set(self, *, max_month: int = 12) -> set[int]:
        if self.kind == "month_range":
            return set(range(int(self.start_month or 1), int(self.end_month or max_month) + 1))
        if self.kind == "quarter":
            start = int(self.start_month or 1)
            return set(range(start, min(start + 3, 13)))
        if self.kind == "half":
            if self.start_month == 7:
                return set(range(7, 13))
            return set(range(1, 7))
        if self.kind == "ytd":
            return set(range(1, max(1, min(max_month, 12)) + 1))
        return set(range(1, max(1, min(max_month, 12)) + 1))


def parse_time_spec(question: str) -> TimeSpec | None:
    text = str(question or "")
    match = _MONTH_RANGE_RE.search(text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 1 <= start <= end <= 12:
            return TimeSpec("month_range", start, end, True)
    match = _SINGLE_MONTH_RE.search(text)
    if match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            return TimeSpec("month_range", month, month, True)
    match = _QUARTER_RE.search(text)
    if match:
        raw = match.group(1) or match.group(2) or ""
        quarter = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
        if 1 <= quarter <= 4:
            return TimeSpec("quarter", (quarter - 1) * 3 + 1, quarter * 3, True)
    match = _HALF_RE.search(text)
    if match:
        return TimeSpec("half", 7 if match.group(1) == "下" else 1, 12 if match.group(1) == "下" else 6, True)
    if _YTD_RE.search(text):
        return TimeSpec("ytd", require_complete=False)
    match = _FIRST_N_RE.search(text)
    if match:
        end = int(match.group(1))
        if 1 <= end <= 12:
            return TimeSpec("month_range", 1, end, True)
    if re.search(r"全年|整年|年度合计", text):
        return TimeSpec("year", 1, 12, False)
    return None


def time_label(spec: TimeSpec) -> str:
    if spec.kind == "month_range":
        return f"{spec.start_month}—{spec.end_month}月"
    if spec.kind == "quarter":
        return f"Q{((int(spec.start_month or 1) - 1) // 3) + 1}"
    if spec.kind == "half":
        return "下半年" if spec.start_month == 7 else "上半年"
    if spec.kind == "ytd":
        return "年初至今"
    return "全年"
