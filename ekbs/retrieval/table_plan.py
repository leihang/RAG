"""表格查询计划（显式 AST）。

把“用户问题”编译成与具体表/文件无关的计划：

    TablePlan(
      operation="compare",
      subject="公司承担水费",
      years=(2026, 2025),
      time=TimeSpec("month_range", 1, 9),
    )

执行器只吃 TablePlan + CanonicalTable，不再从问题文本里临时 if。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_COMPARE_WORDS = ("增长", "减少", "对比", "较", "同比", "增加", "下降")


@dataclass(frozen=True)
class TablePlan:
    operation: str             # compare | sum | lookup（当前实现 compare）
    subject: str
    years: tuple[int, ...]
    time: TimeSpec
    require_compare: bool = True


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。；;：:、]+", "", str(text or ""))


def subject_from_question(question: str) -> str:
    head = re.split(r"[，,。；;：:]", str(question or ""), maxsplit=1)[0]
    head = re.sub(r"20\d{2}\s*年.*$", "", head)
    return head.strip()


def parse_table_plan(question: str) -> TablePlan | None:
    spec = parse_time_spec(question)
    years = tuple(dict.fromkeys(int(y) for y in _YEAR_RE.findall(str(question or ""))))
    if spec is None or len(years) < 2:
        return None
    compare = any(word in str(question or "") for word in _COMPARE_WORDS)
    if not compare:
        return None
    subject = subject_from_question(question)
    if len(_normalize(subject)) < 2:
        return None
    return TablePlan(operation="compare", subject=subject, years=years, time=spec)
