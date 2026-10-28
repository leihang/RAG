"""切块与序列化：Markdown（向量检索）/ JSON（Text-to-SQL）/ 文本（说明）。

切块按**字符预算**而不是行数：49 行 × 40 列可能比 60 行 × 3 列还大，按行数切会超窗。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .grid import display, is_blank, jsonable
from .options import ParseOptions
from .schema import make_chunk


def span(lo: int, hi: int) -> str:
    return str(lo) if lo == hi else f"{lo}-{hi}"


def _escape(value: Any) -> str:
    return display(value).replace("|", "\\|").replace("\n", " ").strip()


def _header_char_cost(columns: Sequence[str]) -> int:
    return sum(len(str(c)) + 3 for c in columns) + 4


def _row_char_cost(row: Sequence[Any]) -> int:
    return sum(len(_escape(v)) for v in row) + 3 * len(row) + 4


def _markdown_header(columns: Sequence[str]) -> str:
    cols = [str(c) for c in columns]
    return (
        "| " + " | ".join(_escape(c) for c in cols) + " |\n"
        "| " + " | ".join("---" for _ in cols) + " |"
    )


def _markdown_body(rows: Sequence[Sequence[Any]]) -> str:
    return "\n".join("| " + " | ".join(_escape(v) for v in row) + " |" for row in rows)


def _meta_tag(sheet_name: str, title: str) -> str:
    return f"【Sheet名: {sheet_name}】【表意: {title}】"


def _slice_rows(rows: Sequence[Sequence[Any]], columns: Sequence[str],
                max_chars: int, min_rows: int) -> list[tuple[int, list[Sequence[Any]]]]:
    """按字符预算切片，返回 [(起始偏移, 行列表), ...]。"""
    header_cost = _header_char_cost(columns)
    slices: list[tuple[int, list[Sequence[Any]]]] = []
    current: list[Sequence[Any]] = []
    current_start = 0
    current_cost = header_cost
    for offset, row in enumerate(rows):
        cost = _row_char_cost(row)
        if current and current_cost + cost > max_chars and len(current) >= min_rows:
            slices.append((current_start, current))
            current = []
            current_start = offset
            current_cost = header_cost
        current.append(row)
        current_cost += cost
    if current:
        slices.append((current_start, current))
    return slices


def markdown_chunks(rows: Sequence[Sequence[Any]], columns: Sequence[str], *,
                    source: str, sheet_name: str, title: str,
                    data_start_1based: int, options: ParseOptions) -> list[dict[str, Any]]:
    """文本/混合表 → Markdown；短表整块，长表按预算切片且每片强制带表头。"""
    if not rows:
        return []
    columns = [str(c) for c in columns]
    tag = _meta_tag(sheet_name, title)
    total_cost = _header_char_cost(columns) + sum(_row_char_cost(r) for r in rows)

    if total_cost <= options.max_chunk_chars:
        text = f"{tag}\n\n{_markdown_header(columns)}\n{_markdown_body(rows)}"
        return [make_chunk(
            chunk_text=text, source=source, sheet_name=sheet_name,
            chunk_type="markdown_table",
            row_range=span(data_start_1based, data_start_1based + len(rows) - 1),
            retrieval="vector", columns=columns, table_title=title, row_count=len(rows),
        )]

    header_md = _markdown_header(columns)
    chunks: list[dict[str, Any]] = []
    for slice_index, (offset, sl) in enumerate(
        _slice_rows(rows, columns, options.max_chunk_chars, options.min_slice_rows)
    ):
        lo = data_start_1based + offset
        hi = lo + len(sl) - 1
        text = f"{tag}\n\n{header_md}\n{_markdown_body(sl)}"
        chunks.append(make_chunk(
            chunk_text=text, source=source, sheet_name=sheet_name,
            chunk_type="markdown_table_slice", row_range=span(lo, hi),
            retrieval="vector", columns=columns, table_title=title,
            row_count=len(sl), slice_index=slice_index,
        ))
    return chunks


def json_chunks(rows: Sequence[Sequence[Any]], columns: Sequence[str], *,
                source: str, sheet_name: str, title: str,
                data_start: int, data_end: int) -> list[dict[str, Any]]:
    """数值/财务表 → 按行 JSON（Text-to-SQL；不进向量检索，故不做预算切片）。"""
    if not rows:
        return []
    columns = [str(c) for c in columns]
    records = [
        {columns[c]: jsonable(row[c]) if c < len(row) else None for c in range(len(columns))}
        for row in rows
    ]
    payload = json.dumps(records, ensure_ascii=False, indent=2)
    return [make_chunk(
        chunk_text=payload, source=source, sheet_name=sheet_name,
        chunk_type="numeric_records", row_range=span(data_start, data_end),
        retrieval="sql", columns=columns, table_title=title,
        record_count=len(records), purpose="Text-to-SQL 结构化数据，不进向量检索",
    )]


def text_chunks(rows: Sequence[Sequence[Any]], *, source: str, sheet_name: str,
                start_1based: int, position: str, section_index: int,
                options: ParseOptions) -> list[dict[str, Any]]:
    """说明/备注文字：连续行合并成块，按字符预算再切。"""
    if not rows:
        return []

    def flush(lines: list[str], lo: int, hi: int) -> dict[str, Any]:
        return make_chunk(
            chunk_text="\n".join(lines), source=source, sheet_name=sheet_name,
            chunk_type="text", row_range=span(lo, hi), retrieval="vector",
            section_index=section_index, note_position=position,
        )

    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    buf_lo = 0
    buf_hi = 0
    buf_cost = 0
    for i, row in enumerate(rows):
        values = [display(v) for v in row if not is_blank(v)]
        if not values:
            continue
        line = " ".join(values)
        row_no = start_1based + i
        if buffer and buf_cost + len(line) + 1 > options.max_chunk_chars:
            chunks.append(flush(buffer, buf_lo, buf_hi))
            buffer = []
            buf_cost = 0
        if not buffer:
            buf_lo = row_no
        buffer.append(line)
        buf_hi = row_no
        buf_cost += len(line) + 1
    if buffer:
        chunks.append(flush(buffer, buf_lo, buf_hi))
    return chunks
