"""确定性表格序列化 —— 产出可检索的 ``information_block`` 文本（`docs/spec/20` §3.4）。

**不走云端 LLM**：同表同输出、无出网、可单测。复杂合并单元格的语义补全留给后续
可选 LLM 开关；本模块先保证数值/档次类问答能独立召回整表块。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Sequence

from ekbs.ingest.chunker import count_tokens

#: 单块上限（spec：>2048 token 按行分组）
TABLE_MAX_TOKENS = 2048


class _TableHTMLParser(HTMLParser):
    """极简 HTML 表解析：抽出 ``<tr><th>/<td>`` 二维字符矩阵。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "tr":
            self._row = []
        elif name in {"td", "th"} and self._row is not None:
            self._cell = []
            self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
            self._in_cell = False
        elif name == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._cell is not None:
            self._cell.append(data)


def parse_table_html(html: str) -> list[list[str]]:
    """HTML → 二维单元格；解析失败返回空表。"""
    if not (html or "").strip():
        return []
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return parser.rows


def parse_table_markdown(text: str) -> list[list[str]]:
    """简易 Markdown 管道表 → 二维单元格。"""
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
            continue  # 分隔行
        if cells:
            rows.append(cells)
    return rows


def coerce_rows(body: str) -> list[list[str]]:
    """优先当 HTML 解析；否则尝试 Markdown 管道表；再否则整段当单行。"""
    text = (body or "").strip()
    if not text:
        return []
    if "<table" in text.lower() or "<tr" in text.lower():
        rows = parse_table_html(text)
        if rows:
            return rows
    rows = parse_table_markdown(text)
    if rows:
        return rows
    return [[text]]


def serialize_information_blocks(
    *,
    caption: str = "",
    rows: Sequence[Sequence[str]] = (),
    table_id: str = "",
) -> list[str]:
    """把二维表打成一段或多段自解释文本（超长按行批拆）。"""
    matrix = [list(r) for r in rows if any(str(c).strip() for c in r)]
    if not matrix:
        return []

    headers = [str(c).strip() or f"列{i + 1}" for i, c in enumerate(matrix[0])]
    data_rows = matrix[1:] if len(matrix) > 1 else []
    # 无表头只有一行时：整行当数据，用列序号作表头
    if not data_rows:
        data_rows = [matrix[0]]
        headers = [f"列{i + 1}" for i in range(len(matrix[0]))]

    subject = caption.strip() or (table_id.strip() and f"表 {table_id}") or headers[0]
    header_line = " | ".join(headers)
    preamble = [
        f"表名: {caption.strip() or subject}",
        f"核心实体: {subject}",
        f"相关表头: {header_line}",
    ]

    def format_row(cells: Sequence[str]) -> str:
        parts: list[str] = []
        for i, header in enumerate(headers):
            value = str(cells[i]).strip() if i < len(cells) else ""
            parts.append(f"{header}={value}")
        return "; ".join(parts)

    blocks: list[str] = []
    current_lines = list(preamble)
    for cells in data_rows:
        line = format_row(cells)
        candidate = "\n".join([*current_lines, line])
        if current_lines is not preamble and count_tokens(candidate) > TABLE_MAX_TOKENS:
            blocks.append("\n".join(current_lines))
            current_lines = [*preamble, line]
        else:
            current_lines.append(line)
    if len(current_lines) > len(preamble):
        blocks.append("\n".join(current_lines))
    elif not blocks and data_rows:
        blocks.append("\n".join(current_lines))
    return blocks


def serialize_table_body(*, caption: str = "", body: str = "", table_id: str = "") -> list[str]:
    """从 MinerU ``table_body`` / 题注直接序列化。"""
    return serialize_information_blocks(
        caption=caption, rows=coerce_rows(body), table_id=table_id
    )


def information_block_to_html(text: str) -> str | None:
    """把入库的 ``information_block`` 还原成 HTML 表，供生成侧组装上下文。

    课程 19 结论：喂给 LLM 时 HTML 对合并单元格/表头的理解优于纯 Markdown / 属性串。
    还原失败返回 None（调用方回退原文）。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    caption = ""
    headers: list[str] = []
    rows: list[list[str]] = []
    for line in lines:
        if line.startswith("表名:"):
            caption = line.split(":", 1)[1].strip()
            continue
        if line.startswith("核心实体:"):
            continue
        if line.startswith("相关表头:"):
            headers = [h.strip() for h in line.split(":", 1)[1].split("|") if h.strip()]
            continue
        if "=" in line and ";" in line:
            cells_map = {}
            for part in line.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                key, _, value = part.partition("=")
                cells_map[key.strip()] = value.strip()
            if headers:
                rows.append([cells_map.get(h, "") for h in headers])
            elif cells_map:
                if not headers:
                    headers = list(cells_map.keys())
                rows.append([cells_map.get(h, "") for h in headers])
    if not headers or not rows:
        return None
    parts = ['<table border="1" cellpadding="4" cellspacing="0">']
    if caption:
        parts.append(f"<caption>{_xml_escape(caption)}</caption>")
    parts.append("<thead><tr>")
    parts.extend(f"<th>{_xml_escape(h)}</th>" for h in headers)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(f"<td>{_xml_escape(c)}</td>" for c in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _xml_escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
