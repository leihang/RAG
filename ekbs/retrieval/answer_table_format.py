"""答案出口 Markdown 表格规范化。

入库仍据实保留 Excel 原文；这里只整理**生成后的** Markdown 表，避免：
* 引用号 ``[n]`` 塞进单元格；
* 空值写法混乱（空白 / - / —）；
* 同列用量小数位忽有忽无（``56888`` vs ``1343.00``）；
* 表前缺空行导致钉钉 AI 卡片把管道表当正文。

不改动百分比字符串，不发明原文没有的数值。
"""

from __future__ import annotations

import re

_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_LINE_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
_CELL_CITE_RE = re.compile(r"\s*\[\d+\]\s*")
_PERCENT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")
_NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")


def normalize_answer_markdown_tables(answer: str) -> str:
    """规范化答案中的 Markdown 管道表；无表则原样返回。"""
    text = str(answer or "")
    if "|" not in text:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        body = raw.rstrip("\r\n")
        if not _TABLE_LINE_RE.match(body):
            out.append(raw)
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and _TABLE_LINE_RE.match(lines[index].rstrip("\r\n")):
            block.append(lines[index].rstrip("\r\n"))
            index += 1
        newline = "\n"
        if lines[index - 1].endswith("\r\n"):
            newline = "\r\n"
        elif lines[index - 1].endswith("\n"):
            newline = "\n"
        # 钉钉 AI 卡片：表头前必须有空行，否则整表当正文
        if out and out[-1].strip():
            out.append(newline)
        normalized, cites = _normalize_table_block(block)
        for row in normalized:
            out.append(row + newline)
        if cites:
            # 引用挪到表后，避免钉在最后一个数据格里
            out.append("".join(f"[{n}]" for n in cites) + newline)
    return "".join(out)


def markdown_tables_to_labeled_blocks(answer: str) -> str:
    """把管道表改成「行名 + 栏目: 值」块，供钉钉 ``sampleMarkdown`` 使用。

    官方 ``sampleMarkdown`` 不支持管道表；单聊主路径会再经
    ``format_for_sample_markdown`` 把 ``【行名】`` 升为四级标题。
    AI 卡片路径请继续用 ``normalize_answer_markdown_tables``（保留管道表）。
    """
    text = str(answer or "")
    if "|" not in text:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        body = raw.rstrip("\r\n")
        if not _TABLE_LINE_RE.match(body):
            out.append(raw)
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and _TABLE_LINE_RE.match(lines[index].rstrip("\r\n")):
            block.append(lines[index].rstrip("\r\n"))
            index += 1
        newline = "\n"
        if lines[index - 1].endswith("\r\n"):
            newline = "\r\n"
        elif lines[index - 1].endswith("\n"):
            newline = "\n"
        labeled = _table_block_to_labeled(block)
        if out and out[-1].strip() and labeled:
            out.append(newline)
        for piece in labeled:
            out.append(piece + newline)
    return "".join(out)


def _normalize_table_block(rows: list[str]) -> tuple[list[str], list[int]]:
    parsed: list[list[str]] = []
    sep_indexes: set[int] = set()
    collected_cites: list[int] = []
    for index, row in enumerate(rows):
        if _SEP_LINE_RE.match(row):
            sep_indexes.add(index)
            parsed.append([])
            continue
        cells, cites = _split_row(row)
        collected_cites.extend(cites)
        parsed.append(cells)

    width = max((len(r) for r in parsed if r), default=0)
    for row in parsed:
        if row is not None and row != []:
            while len(row) < width:
                row.append("")

    data_rows = [row for i, row in enumerate(parsed) if i not in sep_indexes and row]
    if len(data_rows) >= 2:
        # 第 0 行当表头，其余为数据
        _align_numeric_columns(data_rows[1:], width)

    for row in data_rows:
        for col, cell in enumerate(row):
            cleaned = cell.strip()
            if cleaned in {"", "-", "－", "—", "–", "N/A", "n/a", "NA"}:
                row[col] = "—"

    out_rows: list[str] = []
    data_iter = iter(data_rows)
    for index, row in enumerate(parsed):
        if index in sep_indexes:
            out_rows.append(rows[index].strip() if rows[index].strip() else _sep_line(width))
            continue
        cells = next(data_iter)
        out_rows.append("| " + " | ".join(cells) + " |")

    # 引用去重保序
    seen: set[int] = set()
    ordered: list[int] = []
    for cite in collected_cites:
        if cite not in seen:
            seen.add(cite)
            ordered.append(cite)
    return out_rows, ordered


def _table_block_to_labeled(rows: list[str]) -> list[str]:
    headers: list[str] = []
    body: list[list[str]] = []
    saw_sep = False
    for row in rows:
        if _SEP_LINE_RE.match(row):
            saw_sep = True
            continue
        cells, _ = _split_row(row)
        if not headers and not saw_sep:
            headers = cells
            continue
        if not headers:
            headers = [f"列{i + 1}" for i in range(len(cells))]
        body.append(cells)
    if not headers:
        return list(rows)
    if not body and headers:
        # 只有表头没有数据：原样退回，避免空转
        return list(rows)

    width = max(len(headers), max((len(r) for r in body), default=0))
    headers = headers + [f"列{i + 1}" for i in range(len(headers), width)]
    for row in body:
        while len(row) < width:
            row.append("—")

    pieces: list[str] = []
    for row in body:
        title = (row[0] or "—").strip() or "—"
        # 单聊 sampleText 不解析 Markdown，不用 **加粗**
        pieces.append(f"【{title}】")
        for col in range(1, width):
            header = (headers[col] or f"列{col + 1}").strip() or f"列{col + 1}"
            value = (row[col] or "—").strip() or "—"
            pieces.append(f"{header}：{value}")
        pieces.append("")  # 行间空行，方便扫读
    while pieces and pieces[-1] == "":
        pieces.pop()
    return pieces


def _split_row(row: str) -> tuple[list[str], list[int]]:
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = [c.strip() for c in text.split("|")]
    cites: list[int] = []
    cleaned: list[str] = []
    for cell in cells:
        found = [int(m) for m in re.findall(r"\[(\d+)\]", cell)]
        cites.extend(found)
        piece = _CELL_CITE_RE.sub("", cell).strip()
        cleaned.append(piece)
    return cleaned, cites


def _sep_line(width: int) -> str:
    if width <= 0:
        return "|---|"
    return "| " + " | ".join("---" for _ in range(width)) + " |"


def _align_numeric_columns(body_rows: list[list[str]], width: int) -> None:
    for col in range(width):
        decimals: list[int] = []
        for row in body_rows:
            if col >= len(row):
                continue
            cell = row[col].strip()
            if not cell or cell == "—" or _PERCENT_RE.match(cell):
                continue
            if not _NUMBER_RE.match(cell):
                continue
            raw = cell.replace(",", "")
            if "." in raw:
                decimals.append(len(raw.split(".", 1)[1]))
            else:
                decimals.append(0)
        if not decimals:
            continue
        places = max(decimals)
        if places <= 0:
            continue
        for row in body_rows:
            if col >= len(row):
                continue
            cell = row[col].strip()
            if not cell or cell == "—" or _PERCENT_RE.match(cell):
                continue
            if not _NUMBER_RE.match(cell):
                continue
            raw = cell.replace(",", "")
            try:
                number = float(raw)
            except ValueError:
                continue
            row[col] = f"{number:.{places}f}"
