"""编排层：文件 → 读入 → 布局分析 → 分块。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .chunker import json_chunks, markdown_chunks, span, text_chunks
from .exceptions import EmptyWorkbookError, ExcelRagParserError
from .grid import SheetGrid, display, is_blank
from .layout import (
    Section,
    SheetLayout,
    analyze_sheet,
    collapse_identical_columns,
    flatten_header,
    is_numeric_table,
    keep_columns,
    row_is_blank,
)
from .log import get_logger
from .options import ParseOptions
from .readers import read_workbook
from .schema import make_chunk


def parse_excel(path: str | Path, options: ParseOptions | None = None) -> list[dict[str, Any]]:
    """解析整个工作簿，返回统一结构的 chunk 列表。

    单个 Sheet 失败只告警、不拖垮整簿；全部失败或空簿时抛明确异常。
    """
    opts = options or ParseOptions()
    logger = opts.logger or get_logger("parser")
    path = Path(path)

    grids = read_workbook(path, opts)
    source = path.name
    chunks: list[dict[str, Any]] = []
    failures: list[str] = []

    for grid in grids:
        try:
            sheet_chunks = _parse_sheet(grid, source, opts)
        except Exception as exc:  # noqa: BLE001 - 单 Sheet 失败不拖垮整簿
            logger.warning("sheet_parse_failed sheet=%r error=%s", grid.name, exc)
            failures.append(f"{grid.name}: {type(exc).__name__}: {exc}")
            continue
        logger.debug("sheet_parsed sheet=%r chunks=%d", grid.name, len(sheet_chunks))
        chunks.extend(sheet_chunks)

    if not chunks:
        if failures:
            raise ExcelRagParserError("所有 Sheet 解析失败：" + "；".join(failures))
        raise EmptyWorkbookError(str(path))
    return chunks


# ---------------------------------------------------------------------------
# 单 Sheet
# ---------------------------------------------------------------------------

def _parse_sheet(grid: SheetGrid, source: str, options: ParseOptions) -> list[dict[str, Any]]:
    layout = analyze_sheet(grid, options)

    if not layout.sections:
        # 纯说明文字 Sheet
        return text_chunks(
            layout.leading + layout.trailing, source=source, sheet_name=grid.name,
            start_1based=1, position="note", section_index=0, options=options,
        )

    chunks: list[dict[str, Any]] = []

    if options.include_text_notes:
        chunks.extend(text_chunks(
            layout.leading, source=source, sheet_name=grid.name,
            start_1based=1, position="header", section_index=0, options=options,
        ))

    covered: set[int] = set()
    for sec in layout.sections:
        covered.update(range(sec.header_start, sec.header_start + sec.header_rows))
        covered.update(range(sec.data_start, sec.data_end))

    for section_index, sec in enumerate(layout.sections):
        chunks.extend(_section_chunks(layout, sec, section_index, source, options))

    if options.include_text_notes:
        gap_rows = [
            (r, layout.grid.rows[r])
            for r in range(layout.start, layout.end)
            if r not in covered and not row_is_blank(layout.grid.rows[r])
        ]
        chunks.extend(_gap_text_chunks(gap_rows, layout, source, options))
        chunks.extend(text_chunks(
            layout.trailing, source=source, sheet_name=grid.name,
            start_1based=layout.end + 1, position="footer",
            section_index=max(0, len(layout.sections) - 1), options=options,
        ))
    return chunks


def _section_chunks(layout: SheetLayout, sec: Section, section_index: int,
                    source: str, options: ParseOptions) -> list[dict[str, Any]]:
    grid = layout.grid
    header_block = grid.rows[sec.header_start : sec.header_start + sec.header_rows]
    data_block = grid.rows[sec.data_start : sec.data_end]
    if not data_block:
        return []

    keep = keep_columns(header_block, data_block, grid.width)
    keep = collapse_identical_columns(
        keep, data_block, grid.merges,
        last_header_row=sec.header_start + sec.header_rows - 1,
    )
    columns = flatten_header(header_block, keep)
    data_rows = [[row[c] for c in keep] for row in data_block]
    data_rows = [row for row in data_rows if any(not is_blank(v) for v in row)]
    if not data_rows:
        return []

    title = sec.title or layout.title
    chunks: list[dict[str, Any]]
    if options.force_all_text or not is_numeric_table(data_rows, columns, options):
        chunks = markdown_chunks(
            data_rows, columns, source=source, sheet_name=grid.name,
            title=title, data_start_1based=sec.data_start + 1, options=options,
        )
    else:
        chunks = json_chunks(
            data_rows, columns, source=source, sheet_name=grid.name,
            title=title, data_start=sec.data_start + 1, data_end=sec.data_end,
        )
    for chunk in chunks:
        chunk["metadata"]["section_index"] = section_index
    return chunks


def _gap_text_chunks(gap_rows: Sequence[tuple[int, Sequence[Any]]],
                     layout: SheetLayout, source: str,
                     options: ParseOptions) -> list[dict[str, Any]]:
    """段间说明/标题：按连续行分组，每组一个文本块。"""
    chunks: list[dict[str, Any]] = []
    group: list[tuple[int, Sequence[Any]]] = []

    def flush(items: list[tuple[int, Sequence[Any]]]) -> None:
        if not items:
            return
        lines = [" ".join(display(v) for v in row if not is_blank(v)) for _, row in items]
        lines = [line for line in lines if line]
        if not lines:
            return
        lo = items[0][0] + 1
        hi = items[-1][0] + 1
        section_index = 0
        for i, sec in enumerate(layout.sections):
            if items[0][0] > sec.header_start:
                section_index = i
        chunks.append(make_chunk(
            chunk_text="\n".join(lines), source=source, sheet_name=layout.grid.name,
            chunk_type="text", row_range=span(lo, hi), retrieval="vector",
            section_index=section_index, note_position="inline",
        ))

    for r, row in gap_rows:
        if group and r != group[-1][0] + 1:
            flush(group)
            group = []
        group.append((r, row))
    flush(group)
    return chunks
