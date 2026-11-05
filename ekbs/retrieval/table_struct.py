"""表格分片 → 结构化行（供确定性表格问答）。

分片层的 ``键=值`` 文本适合检索，但不应让 LLM 从文本里猜“年度合计能不能当 1-9 月”。
这里把命中的表格分片还原成 ``TableRow(subject, year, months{1..12}, annual, unit)``，
供 :mod:`ekbs.retrieval.table_query` 做过滤/求和/比较。

只解析“年份行 × 月份列”的表格；其它形状保持原文，继续走 LLM。
"""

from __future__ import annotations

import json
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
    # 中文括号负数常见于 Excel 财务表：``（123）`` = -123。
    normalized = str(text or "").replace("（", "(").replace("）", ")")
    match = _NUMBER_RE.search(normalized)
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
    chunk_id: int | None = None
    months: dict[int, float] = field(default_factory=dict)
    quarters: dict[int, float] = field(default_factory=dict)
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


_QUARTER_KEY_RE = re.compile(r"(?i)^(?:Q([1-4])|第?([一二三四1234])季度?)$")
_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4}


def _quarter_key(key: str) -> int | None:
    match = _QUARTER_KEY_RE.fullmatch(str(key or "").strip())
    if not match:
        return None
    raw = match.group(1) or match.group(2) or ""
    if raw.isdigit():
        return int(raw)
    return _CN_QUARTER.get(raw)


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
    seen: set[tuple[object, ...]] = set()
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
            quarters: dict[int, float] = {}
            annual: float | None = None
            for key, value in row.items():
                if key == "年度合计":
                    annual = _number(value)
                    continue
                month_match = re.fullmatch(r"(\d{1,2})月", key)
                if month_match:
                    number = _number(value)
                    if number is not None:
                        months[int(month_match.group(1))] = number
                    continue
                quarter = _quarter_key(key)
                if quarter is not None:
                    number = _number(value)
                    if number is not None:
                        quarters[quarter] = number
            if not months and not quarters:
                continue
            signature = (
                caption, subject, year,
                tuple(sorted(months.items())), tuple(sorted(quarters.items())),
            )
            if signature in seen:
                continue  # 同一行可能同时出现在父块/分段里，避免重复累加
            seen.add(signature)
            rows.append(
                TableRow(
                    chunk_index=chunk_index,
                    doc_id=int(getattr(chunk, "doc_id", 0) or 0),
                    chunk_id=int(getattr(chunk, "chunk_id", 0) or 0) or None,
                    caption=caption,
                    unit=unit,
                    subject=subject,
                    year=year,
                    months=months,
                    quarters=quarters,
                    annual_total=annual,
                )
            )
    return rows


@dataclass
class TableIndex:
    rows: list[TableRow]

    @classmethod
    def from_chunks(cls, chunks: Sequence[Any]) -> "TableIndex":
        # 简单横表 + 多层表头路径（2026年/1月）都归一到 TableRow。
        return cls([*parse_table_rows(chunks), *parse_nested_rows(chunks)])

    def find(self, subject: str) -> list[TableRow]:
        target = _norm(subject)
        if not target:
            return []
        return [row for row in self.rows if target in _norm(row.subject)]


def load_table_rows(db: Any, doc_ids: Sequence[int]) -> list[TableRow]:
    """从 ``table_row_index`` 读结构化行（按文档过滤，权限由调用方先过滤 chunk）。"""
    ids = [int(doc_id) for doc_id in doc_ids if int(doc_id) > 0]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    try:
        with db.sync_connect() as conn:
            records = conn.execute(
                "SELECT doc_id, chunk_id, caption, unit, subject, year, months_json, "
                "quarters_json, annual_total "
                f"FROM table_row_index WHERE doc_id IN ({placeholders}) ORDER BY row_id",
                ids,
            ).fetchall()
    except Exception:  # noqa: BLE001 —— 旧库/测试库未补表时退回 chunk 解析
        return []
    rows: list[TableRow] = []
    for record in records:
        try:
            months = {
                int(month): float(value)
                for month, value in json.loads(record["months_json"] or "{}").items()
            }
            quarters = {
                int(quarter): float(value)
                for quarter, value in json.loads(record["quarters_json"] or "{}").items()
            }
        except (TypeError, ValueError):
            continue
        if not months and not quarters:
            continue
        rows.append(
            TableRow(
                chunk_index=0,
                chunk_id=int(record["chunk_id"]) if record["chunk_id"] is not None else None,
                doc_id=int(record["doc_id"]),
                caption=str(record["caption"] or ""),
                unit=str(record["unit"] or ""),
                subject=str(record["subject"] or ""),
                year=int(record["year"]),
                months=months,
                quarters=quarters,
                annual_total=(
                    float(record["annual_total"]) if record["annual_total"] is not None else None
                ),
            )
        )
    return rows


@dataclass(frozen=True)
class RecordFact:
    """记录表/表单表的一格：subject + field + value。"""

    chunk_index: int
    chunk_id: int | None
    doc_id: int
    caption: str
    subject: str
    field: str
    value: str


def _record_headers(content: str) -> list[str]:
    match = re.search(r"相关表头[:：]\s*(.+)", content or "")
    if not match:
        return []
    return [
        re.sub(r"\s+", "", part)
        for part in match.group(1).split("|")
        if part.strip()
    ]


def parse_record_facts(chunks: Sequence[Any]) -> list[RecordFact]:
    """解析“相关表头 + 每行字段=值”的记录表/表单；跳过月份横表。"""
    facts: list[RecordFact] = []
    seen: set[tuple[str, str, str, str]] = set()
    for chunk_index, chunk in enumerate(chunks, start=1):
        content = str(getattr(chunk, "content", "") or "")
        headers = _record_headers(content)
        if not headers:
            continue
        caption_match = _CAPTION_RE.search(content)
        caption = caption_match.group(1).strip() if caption_match else str(
            getattr(chunk, "heading_path", "") or ""
        )
        for line in content.splitlines():
            row = _parse_row(line)
            if not row:
                continue
            # 月份横表走 table_row_index，不当记录表重复索引。
            if any("年月份" in key or "年度合计" in key for key in row):
                continue
            ordered = [row[key] for key in headers if key in row]
            if not ordered:
                continue
            subject = ordered[0].strip()
            if not subject:
                continue
            for key in headers:
                value = str(row.get(key) or "").strip()
                if not key or not value:
                    continue
                signature = (caption, subject, key, value)
                if signature in seen:
                    continue
                seen.add(signature)
                facts.append(
                    RecordFact(
                        chunk_index=chunk_index,
                        chunk_id=int(getattr(chunk, "chunk_id", 0) or 0) or None,
                        doc_id=int(getattr(chunk, "doc_id", 0) or 0),
                        caption=caption,
                        subject=subject,
                        field=key,
                        value=value,
                    )
                )
    return facts


def load_record_facts(db: Any, doc_ids: Sequence[int]) -> list[RecordFact]:
    """读 ``table_record_index``；旧库未补表时返回空。"""
    ids = [int(doc_id) for doc_id in doc_ids if int(doc_id) > 0]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    try:
        with db.sync_connect() as conn:
            records = conn.execute(
                "SELECT doc_id, chunk_id, caption, subject, field, value "
                f"FROM table_record_index WHERE doc_id IN ({placeholders}) ORDER BY row_id",
                ids,
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [
        RecordFact(
            chunk_index=0,
            chunk_id=int(record["chunk_id"]) if record["chunk_id"] is not None else None,
            doc_id=int(record["doc_id"]),
            caption=str(record["caption"] or ""),
            subject=str(record["subject"] or ""),
            field=str(record["field"] or ""),
            value=str(record["value"] or ""),
        )
        for record in records
    ]


_NESTED_YEAR_RE = re.compile(r"(20\d{2})\s*年?")
_NESTED_MONTH_RE = re.compile(r"(\d{1,2})\s*月")
_NESTED_QUARTER_RE = re.compile(r"(?i)Q([1-4])|第?([一二三四])季度")


def _nested_time_path(header: str) -> tuple[int | None, int | None]:
    """从 ``2026年/1月`` 这类嵌套表头取 (year, month)；季度暂不展开为月份。"""
    parts = re.split(r"[/／>\s]+", str(header or ""))
    year: int | None = None
    month: int | None = None
    for part in parts:
        if year is None:
            ym = _NESTED_YEAR_RE.search(part)
            if ym:
                year = int(ym.group(1))
        if month is None:
            mm = _NESTED_MONTH_RE.search(part)
            if mm:
                month = int(mm.group(1))
    return year, month


def parse_nested_rows(chunks: Sequence[Any]) -> list[TableRow]:
    """多层表头（``2026年 / 1月`` 路径）→ 同一 TableRow 模型。

    只处理能明确解析出 year+month 的列；季度/累计列不硬拆月份，继续走原链路。
    """
    rows: list[TableRow] = []
    seen: set[tuple[str, str, int, tuple[tuple[int, float], ...]]] = set()
    for chunk_index, chunk in enumerate(chunks, start=1):
        content = str(getattr(chunk, "content", "") or "")
        headers = _record_headers(content)
        if not headers:
            continue
        mapped: dict[str, tuple[int, int]] = {}
        for header in headers:
            year, month = _nested_time_path(header)
            if year is not None and month is not None:
                mapped[header] = (year, month)
        if not mapped:
            continue
        caption_match = _CAPTION_RE.search(content)
        caption = caption_match.group(1).strip() if caption_match else str(
            getattr(chunk, "heading_path", "") or ""
        )
        unit = unit_from_text(caption) or unit_from_text(
            " ".join(str(getattr(chunk, attr, "") or "") for attr in ("heading_path", "doc_title"))
        )
        grouped: dict[tuple[str, int], dict[int, float]] = {}
        chunk_id = int(getattr(chunk, "chunk_id", 0) or 0) or None
        for line in content.splitlines():
            row = _parse_row(line)
            if not row:
                continue
            subject = _row_subject(row)
            if not subject:
                continue
            for header, (year, month) in mapped.items():
                number = _number(row.get(header, ""))
                if number is None:
                    continue
                grouped.setdefault((subject, year), {}).setdefault(month, number)
        for (subject, year), months in grouped.items():
            signature = (caption, subject, year, tuple(sorted(months.items())))
            if signature in seen:
                continue
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
                    chunk_id=chunk_id,
                )
            )
    return rows


@dataclass(frozen=True)
class CellFact:
    chunk_id: int | None
    doc_id: int
    caption: str
    unit: str
    subject: str
    year: int
    month: int | None
    value: float


_PERIOD_HEADER_RE = re.compile(
    r"(20\d{2})\s*(?:年|[-/])\s*(\d{1,2})?\s*(?:月|[-/])?\s*(\d{1,2})?"
)


def _header_period(header: str) -> tuple[int | None, int | None]:
    if not re.search(r"20\d{2}", header or ""):
        return None, None
    match = _PERIOD_HEADER_RE.search(header)
    if not match:
        return None, None
    year = int(match.group(1))
    month = None
    for raw in (match.group(2), match.group(3)):
        if raw and 1 <= int(raw) <= 12:
            month = int(raw)
            break
    return year, month


def parse_cell_facts(chunks: Sequence[Any]) -> list[CellFact]:
    """宽表（``用量日期 | 2026年9月 | ...``）→ 日期列单元格事实。"""
    facts: list[CellFact] = []
    seen: set[tuple[str, str, int, int | None, float]] = set()
    for chunk in chunks:
        content = str(getattr(chunk, "content", "") or "")
        headers = _record_headers(content)
        if not headers or any("年月份" in header for header in headers):
            continue
        period_headers = {
            header: _header_period(header) for header in headers[1:]
        }
        period_headers = {h: period for h, period in period_headers.items() if period[0] is not None}
        if not period_headers:
            continue
        caption_match = _CAPTION_RE.search(content)
        caption = caption_match.group(1).strip() if caption_match else str(
            getattr(chunk, "heading_path", "") or ""
        )
        unit = unit_from_text(caption)
        chunk_id = int(getattr(chunk, "chunk_id", 0) or 0) or None
        for line in content.splitlines():
            row = _parse_row(line)
            if not row:
                continue
            subject = str(row.get(headers[0]) or "").strip()
            if not subject:
                continue
            for header, (year, month) in period_headers.items():
                value = _number(row.get(header, ""))
                if value is None or year is None:
                    continue
                signature = (caption, subject, year, month, value)
                if signature in seen:
                    continue
                seen.add(signature)
                facts.append(
                    CellFact(
                        chunk_id=chunk_id,
                        doc_id=int(getattr(chunk, "doc_id", 0) or 0),
                        caption=caption,
                        unit=unit,
                        subject=subject,
                        year=year,
                        month=month,
                        value=value,
                    )
                )
    return facts


def load_cell_facts(db: Any, doc_ids: Sequence[int]) -> list[CellFact]:
    ids = [int(doc_id) for doc_id in doc_ids if int(doc_id) > 0]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    try:
        with db.sync_connect() as conn:
            records = conn.execute(
                "SELECT doc_id, chunk_id, caption, unit, subject, year, month, value_num "
                f"FROM table_cell_index WHERE doc_id IN ({placeholders}) ORDER BY row_id",
                ids,
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [
        CellFact(
            chunk_id=int(record["chunk_id"]) if record["chunk_id"] is not None else None,
            doc_id=int(record["doc_id"]),
            caption=str(record["caption"] or ""),
            unit=str(record["unit"] or ""),
            subject=str(record["subject"] or ""),
            year=int(record["year"]),
            month=int(record["month"]) if record["month"] is not None else None,
            value=float(record["value_num"]),
        )
        for record in records
        if record["value_num"] is not None
    ]
