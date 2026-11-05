"""表格分片 → 结构化行（供确定性表格问答）。

分片层的 ``键=值`` 文本适合检索，但不应让 LLM 从文本里猜“年度合计能不能当 1-9 月”。
这里把命中的表格分片还原成 ``TableRow(subject, year, months{1..12}, annual, unit)``，
供 :mod:`ekbs.retrieval.table_query` 做过滤/求和/比较。

只解析“年份行 × 月份列”的表格；其它形状保持原文，继续走 LLM。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

_NUMBER_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_CAPTION_RE = re.compile(r"表名[:：]\s*(.+)")
_UNIT_RE = re.compile(r"[（(]([^（）()]{1,16})[）)]")
_SUBJECT_KEYS = ("日期", "项目", "类别", "指标", "名称")
_YEAR_KEYS = ("年月份", "年份", "年")


def _norm(text: str) -> str:
    return re.sub(r"[\s，,。；;：:、]+", "", str(text or ""))


def _number(text: str) -> float | None:
    match = _NUMBER_RE.search(str(text or ""))
    if not match:
        return None
    raw = match.group(0).replace(",", "")
    negative = raw.startswith("(") and raw.endswith(")")
    try:
        value = float(raw.strip("()"))
    except ValueError:
        return None
    return -value if negative else value


def unit_from_text(text: str) -> str:
    """从表名/标题的括号里取单位（元/万元/度/m³/%）；取不到返回空串。"""
    for match in reversed(_UNIT_RE.findall(text or "")):
        candidate = match.strip()
        if any(ch in candidate for ch in ("元", "万", "度", "m³", "m3", "%", "KW", "kw")):
            return candidate
    return ""


@dataclass(frozen=True)
class TableRow:
    chunk_index: int
    doc_id: int
    caption: str
    unit: str
    subject: str
    year: int
    months: dict[int, float] = field(default_factory=dict)
    annual_total: float | None = None


def _parse_row(line: str) -> dict[str, str] | None:
    if "=" not in line:
        return None
    pairs: dict[str, str] = {}
    for part in line.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        pairs[key.replace(" ", "").strip()] = value.strip()
    return pairs or None


def _row_subject(row: dict[str, str]) -> str:
    for key in _SUBJECT_KEYS:
        value = row.get(key)
        if value:
            return value.strip()
    return ""


def _row_year(row: dict[str, str]) -> int | None:
    for key in _YEAR_KEYS:
        value = row.get(key)
        if not value:
            continue
        match = re.search(r"(20\d{2})", value)
        if match:
            return int(match.group(1))
    return None


def parse_table_rows(chunks: Sequence[Any]) -> list[TableRow]:
    """从检索命中的分片里抽出年份×月份行；无法识别的分片跳过。"""
    rows: list[TableRow] = []
    seen: set[tuple[str, str, int, tuple[tuple[int, float], ...]]] = set()
    for chunk_index, chunk in enumerate(chunks, start=1):
        content = str(getattr(chunk, "content", "") or "")
        heading = " ".join(
            str(getattr(chunk, attr, "") or "") for attr in ("heading_path", "doc_title")
        )
        caption_match = _CAPTION_RE.search(content)
        caption = caption_match.group(1).strip() if caption_match else heading
        unit = unit_from_text(caption) or unit_from_text(heading)
        for line in content.splitlines():
            row = _parse_row(line)
            if not row:
                continue
            subject = _row_subject(row)
            year = _row_year(row)
            if not subject or year is None:
                continue
            months: dict[int, float] = {}
            annual: float | None = None
            for key, value in row.items():
                if key == "年度合计":
                    annual = _number(value)
                    continue
                month_match = re.fullmatch(r"(\d{1,2})月", key)
                if not month_match:
                    continue
                number = _number(value)
                if number is not None:
                    months[int(month_match.group(1))] = number
            if not months:
                continue
            signature = (caption, subject, year, tuple(sorted(months.items())))
            if signature in seen:
                continue  # 同一行可能同时出现在父块/分段里，避免重复累加
            seen.add(signature)
            rows.append(
                TableRow(
                    chunk_index=chunk_index,
                    doc_id=int(getattr(chunk, "doc_id", 0) or 0),
                    caption=caption,
                    unit=unit,
                    subject=subject,
                    year=year,
                    months=months,
                    annual_total=annual,
                )
            )
    return rows


@dataclass
class TableIndex:
    rows: list[TableRow]

    @classmethod
    def from_chunks(cls, chunks: Sequence[Any]) -> "TableIndex":
        return cls(parse_table_rows(chunks))

    def find(self, subject: str) -> list[TableRow]:
        target = _norm(subject)
        if not target:
            return []
        return [row for row in self.rows if target in _norm(row.subject)]
