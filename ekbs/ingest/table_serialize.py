"""确定性表格序列化 —— 产出可检索的 ``information_block`` 文本（`docs/spec/20` §3.4）。

**不走云端 LLM**：同表同输出、无出网、可单测。复杂合并单元格的语义补全留给后续
可选 LLM 开关；本模块先保证数值/档次类问答能独立召回整表块。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Sequence

from ekbs.ingest.clean import normalize_math_in_text
from ekbs.ingest.chunker import count_tokens

#: 单块上限（spec：>2048 token 按行分组）
TABLE_MAX_TOKENS = 2048


class _TableHTMLParser(HTMLParser):
    """抽出 ``<tr><th>/<td>`` 的单元格**几何**：文字 + ``colspan`` + ``rowspan``。

    只留文字不够：``rowspan`` 的延续行在 HTML 里根本没有格（``<td rowspan="7">照明</td>``
    只出现在首行），按文字逐行对齐会把延续行整体左移一列 —— 实测 Word「设施设备开关机
    控制时间表」18 行里 12 行错列、13 块"管理内容"为空。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: list[str] | None = None
        self._in_cell = False
        self._colspan = 1
        self._rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "tr":
            self._row = []
            return
        if name in {"td", "th"} and self._row is not None:
            self._cell = []
            self._in_cell = True
            self._colspan = 1
            self._rowspan = 1
            for key, value in attrs:
                if key.lower() == "colspan":
                    self._colspan = _span_attr(value)
                elif key.lower() == "rowspan":
                    self._rowspan = _span_attr(value)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(
                {
                    "text": "".join(self._cell).strip(),
                    "colspan": self._colspan,
                    "rowspan": self._rowspan,
                }
            )
            self._cell = None
            self._in_cell = False
            self._colspan = 1
            self._rowspan = 1
        elif name == "tr" and self._row is not None:
            if self._row:
                self.raw_rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._cell is not None:
            self._cell.append(data)


def _span_attr(value: str | None) -> int:
    """``colspan="3"`` / ``rowspan="2"`` → 3 / 2；非法值按 1（不伪造合并）。"""
    raw = (value or "").strip()
    return max(1, min(int(raw), 32)) if raw.isdigit() else 1


def _expand_spans(raw_rows: Sequence[Sequence[dict[str, Any]]]) -> list[list[str]]:
    """把带 ``rowspan``/``colspan`` 的稀疏行还原成**等宽网格**。

    * ``colspan`` 覆盖列填 ``""`` —— 保持"一格文字只出现一次"的既有口径；
    * ``rowspan`` 延续行**重复起始格文字** —— 每行都要能独立回答
      "类别=照明; 项目=车库照明"，留空会让分组标签彻底消失。

    ``rowspan`` 与 ``colspan`` 同时出现时，起始格文字只放首列，覆盖到的其它列留空。
    """
    grid: list[list[str]] = []
    #: 列号 → (文字, 还需覆盖几行)
    pending: dict[int, tuple[str, int]] = {}
    for raw in raw_rows:
        cells: dict[int, str] = {}
        column = 0

        def consume_pending() -> None:
            nonlocal column
            while column in pending and pending[column][1] > 0:
                text, remain = pending[column]
                cells[column] = text
                pending[column] = (text, remain - 1)
                column += 1

        for cell in raw:
            consume_pending()
            span = max(1, int(cell.get("colspan") or 1))
            rowspan = max(1, int(cell.get("rowspan") or 1))
            text = str(cell.get("text") or "")
            cells[column] = text
            for offset in range(1, span):
                cells[column + offset] = ""
            if rowspan > 1:
                for offset in range(span):
                    pending[column + offset] = (text if offset == 0 else "", rowspan - 1)
            column += span
        consume_pending()
        width = max(cells) + 1 if cells else 0
        grid.append([cells.get(index, "") for index in range(width)])
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([""] * (width - len(row)))
    return grid


def parse_table_html(html: str) -> list[list[str]]:
    """HTML → 二维单元格（``colspan``/``rowspan`` 已展开成等宽网格）；失败返回空表。"""
    if not (html or "").strip():
        return []
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return _expand_spans(parser.raw_rows)


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


def _is_title_row(row: Sequence[str]) -> bool:
    """整行合并的**标题行**：这一行足够宽、却只有一格有字 → 它是标题，不是表头。

    实测（`2026年7月能耗分析.xlsx` 的「用电分析」表）：

    .. code-block:: text

        第1行  ['熙街2026年7月用电分析表', '', '', ...]   ← 合并标题行
        第2行  ['电量分析(KW/h）', '', '', ...]            ← 又一个合并行（写的是单位口径）
        第3行  [' 用量   日期', '46234', '45869', ...]     ← 才是真正的内容行

    旧实现把第 1 行当表头 → 列名全退化成 `列2…列39`（实测 97 个表格块中招）。
    表格语义（列名）就此消失：按列名检索召不回、取数也定位不到列。
    """
    cells = [str(c).strip() for c in row]
    return len(cells) >= 3 and sum(1 for c in cells if c) <= 1


def _looks_like_form(matrix: Sequence[Sequence[str]]) -> bool:
    """表单型 sheet：前几行是"标签：值"字段，而不是列名。

    复杂 Excel（如商户撤场清算表）第 1 行是 `填表日期：|2025.10.16|品牌|尊宝比萨`，
    不是表头；若直接拿它当 header，整表会变成 `填表日期：=营运部` 这种错位文本。
    这里只做**保守**识别：首行至少两个单元格以冒号结尾，或首行有冒号且非空格≥3。
    """
    if not matrix:
        return False
    first = [str(c).strip() for c in matrix[0]]
    nonempty = [c for c in first if c]
    colon = [c for c in nonempty if c.endswith(("：", ":"))]
    return len(colon) >= 2 or (len(colon) >= 1 and len(nonempty) >= 3)


_NUMERIC_CELL_RE = re.compile(r"^[\$\+\-]?[\d,]+(\.\d+)?%?$")


def _is_numeric_cell(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and bool(_NUMERIC_CELL_RE.match(text))


def _pick_header_index(matrix: Sequence[Sequence[str]]) -> int:
    """在开头最多 4 行里找表头（借鉴 RAGFlow 的 contrast 信号）。

    - 少于 2 个非空格的标题/分节行跳过；
    - 非空单元格一半以上是数字的行是数据行，跳过；
    - 某行的文字列下方（下一行同列）出现 >=2 个数字 → 该行更像表头。
    找不到时退回第 1 行（保持原行为）。
    """
    n = len(matrix)
    if n < 2:
        return 0
    scan = min(4, n - 1)
    for k in range(scan):
        row = [str(c).strip() for c in matrix[k]]
        nonempty = [c for c in row if c]
        if len(nonempty) < 2:
            continue
        numeric = sum(1 for c in nonempty if _is_numeric_cell(c))
        if numeric and numeric * 2 >= len(nonempty):
            continue
        below = [str(c).strip() for c in matrix[k + 1]]
        contrast = 0
        for i, value in enumerate(row):
            if not value or _is_numeric_cell(value):
                continue
            if i < len(below) and _is_numeric_cell(below[i]):
                contrast += 1
        if contrast >= 2:
            return k
    return 0


def _normalize_matrix(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """单元格过一遍公式/单位规整，并丢掉整行空行。"""
    # xlsx/docx 的原生表格不经 Markdown 清洗那一路，但 MinerU/Office 同样会把
    # `50%` 写成 `$5 0 \\%$`（见 ekbs/ingest/clean.py）。
    return [
        [normalize_math_in_text(str(c))[0] for c in r]
        for r in rows
        if any(str(c).strip() for c in r)
    ]


def _strip_title_rows(matrix: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """剥掉开头的标题行（合并单元格），它们**不当表头**；文字折叠进 caption 而不是丢掉 ——
    第 2 行那种常常是单位口径（`电量分析(KW/h）`），丢了会让数值无法解读。
    """
    titles: list[str] = []
    while matrix and _is_title_row(matrix[0]):
        title = next((str(c).strip() for c in matrix[0] if str(c).strip()), "")
        if title:
            titles.append(title)
        matrix = matrix[1:]
    return titles, matrix


def _split_header_rows(
    rows: Sequence[Sequence[str]],
) -> tuple[list[str], list[list[str]]]:
    """两行表头（如 审核意见 / 检查结果、日期、金额）合并成 "父/子"。"""
    if len(rows) < 2:
        return [str(c).strip() or f"列{i+1}" for i, c in enumerate(rows[0])], []
    top = [str(c).strip() for c in rows[0]]
    sub = [str(c).strip() for c in rows[1]]
    width = max(len(top), len(sub))
    top += [""] * (width - len(top))
    sub += [""] * (width - len(sub))
    # 仅当第一行有空列、第二行恰好补上这些列时，才认为是两行表头
    filled = [i for i, c in enumerate(sub) if c]
    if not top or not any(not top[i] and sub[i] for i in filled):
        return [h or f"列{i+1}" for i, h in enumerate(top)], list(rows[1:])
    headers: list[str] = []
    #: 上一列的主表头。横向合并（colspan）的从格在矩阵里是空串，若不继承，
    #: `<th rowspan=2>级别</th><th colspan=3>分级内容</th>` 配子表头时会整体错位一格：
    #: 变成 "级别/能耗检查 | 分级内容/能耗运行管控 | 节能改造"，A/B/C 三行的值全绑错字段。
    parent = ""
    for i in range(width):
        if top[i]:
            parent = top[i]
        parent_label = top[i] or (parent if sub[i] else "")
        if parent_label and sub[i] and parent_label != sub[i]:
            headers.append(f"{parent_label}/{sub[i]}")
        else:
            headers.append(parent_label or sub[i] or f"列{i+1}")
    return headers, list(rows[2:])


def _resolve_headers_and_data(
    matrix: list[list[str]],
) -> tuple[bool, list[str], list[list[str]]]:
    """确定 (form_like, headers, data_rows)。"""
    form_like = _looks_like_form(matrix)
    if form_like:
        # 表单不伪造表头：保留所有行，列名用列序号，行内仍是原值（标签在值里）。
        headers = [f"列{i + 1}" for i in range(max(len(r) for r in matrix))]
        return True, headers, matrix
    header_index = _pick_header_index(matrix)
    before_header = matrix[:header_index]
    headers, data_rows = _split_header_rows(matrix[header_index:])
    data_rows = [*before_header, *data_rows]
    # 无表头只有一行时：整行当数据，用列序号作表头
    if not data_rows:
        data_rows = [matrix[0]]
        headers = [f"列{i + 1}" for i in range(len(matrix[0]))]
    return False, headers, data_rows


def _build_preamble(caption: str, table_id: str, headers: list[str], form_like: bool) -> list[str]:
    subject = caption.strip() or (table_id.strip() and f"表 {table_id}") or headers[0]
    preamble = [
        f"表名: {caption.strip() or subject}",
        f"核心实体: {subject}",
        f"相关表头: {' | '.join(headers)}",
    ]
    if form_like:
        preamble.append("表格形态: 表单型（按行保留字段值，不做列头推断）")
    return preamble


def _format_row(cells: Sequence[str], headers: Sequence[str]) -> str:
    values = [str(cells[i]).strip() if i < len(cells) else "" for i in range(len(headers))]
    # 整行横向合并的**说明行**（如 Word 表尾 `colspan=3` 的备注）不是任何一列的值：
    # 挂到"类别"会造出 `类别=设备控制开关显眼位置应张贴…` 这种错语义检索块。
    # 只认"仅首格有字且是一句说明"的行，避免把稀疏数据行误标成备注。
    nonempty = [value for value in values if value]
    if len(headers) >= 3 and nonempty == [values[0]] and len(values[0]) >= 12:
        return f"备注={values[0]}"
    return "; ".join(f"{header}={values[i]}" for i, header in enumerate(headers))


def _chunk_by_budget(
    preamble: list[str], data_rows: list[list[str]], headers: list[str],
    *, max_tokens: int = TABLE_MAX_TOKENS,
) -> list[str]:
    """按 token 预算把数据行分批，每批重复 preamble。"""
    blocks: list[str] = []
    current_lines = list(preamble)
    for cells in data_rows:
        line = _format_row(cells, headers)
        candidate = "\n".join([*current_lines, line])
        if current_lines is not preamble and count_tokens(candidate) > max_tokens:
            blocks.append("\n".join(current_lines))
            current_lines = [*preamble, line]
        else:
            current_lines.append(line)
    if len(current_lines) > len(preamble):
        blocks.append("\n".join(current_lines))
    elif not blocks and data_rows:
        blocks.append("\n".join(current_lines))
    return blocks


def _prepare_information_blocks(
    *, caption: str, rows: Sequence[Sequence[str]], table_id: str
) -> tuple[str, list[str], bool, list[str], list[list[str]]] | None:
    """矩阵预处理：规整 → 剥标题行 → 定表头 → 拼 preamble。

    整块序列化与 span 序列化**共用这一段**，两种入口对同一批数据行才会逐字节一致
    （测试钉住）；空表返回 ``None``。
    """
    matrix = _normalize_matrix(rows)
    if not matrix:
        return None
    titles, matrix = _strip_title_rows(matrix)
    if not matrix:
        # 极端：整张表只有标题行 → 保留内容（当单列数据），不丢
        matrix = [[t] for t in titles]
        titles = []
    if titles:
        title_text = " / ".join(dict.fromkeys(titles))
        base = caption.strip()
        caption = f"{base}（{title_text}）" if base else title_text
    form_like, headers, data_rows = _resolve_headers_and_data(matrix)
    preamble = _build_preamble(caption, table_id, headers, form_like)
    return caption, preamble, form_like, headers, data_rows


def serialize_information_blocks(
    *,
    caption: str = "",
    rows: Sequence[Sequence[str]] = (),
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
) -> list[str]:
    """把二维表打成一段或多段自解释文本（超长按行批拆）。

    ``max_tokens``：每段预算。默认 2048（整表父容器用）；行组叶子传更小的值
    （见 ``chunker.TABLE_LEAF_TOKENS=384``），避免十几行挤进同一个向量被稀释。
    """
    prepared = _prepare_information_blocks(caption=caption, rows=rows, table_id=table_id)
    if prepared is None:
        return []
    _caption, preamble, _form_like, headers, data_rows = prepared
    if row_chunks:
        # RAGFlow spreadsheet 口径：一行一个块，每块重复表头；header-only 保留表头。
        if not data_rows:
            return ["\n".join(preamble)]
        return ["\n".join([*preamble, _format_row(cells, headers)]) for cells in data_rows]
    return _chunk_by_budget(preamble, data_rows, headers, max_tokens=max_tokens)


def serialize_table_body(
    *,
    caption: str = "",
    body: str = "",
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
) -> list[str]:
    """从 MinerU ``table_body`` / 题注直接序列化。"""
    return serialize_information_blocks(
        caption=caption,
        rows=coerce_rows(body),
        table_id=table_id,
        row_chunks=row_chunks,
        max_tokens=max_tokens,
    )


def _chunk_spans_by_budget(
    preamble: list[str],
    data_rows: list[list[str]],
    headers: list[str],
    *,
    max_tokens: int = TABLE_MAX_TOKENS,
    start_row: int = 0,
    end_row: int | None = None,
) -> list[tuple[str, int, int]]:
    """``_chunk_by_budget`` 的行号版：同样的分批算法，额外返回首/末**数据行**下标（闭区间）。

    分批条件必须与 ``_chunk_by_budget`` 完全一致，否则「同预算下 span 文本 == 整块文本」
    这条不变量（正确性测试）会破。

    ⚠️ 首行本身就超预算时，``_chunk_by_budget`` 会先 flush 一个**只有 preamble 的退化块**
    （``current_lines is not preamble`` 对 ``list(preamble)`` 恒为真）；这里原样保留该文本
    以维持一致性，但给它 ``last_row = first_row - 1`` 的**空区间** —— 它不含数据行，
    分段器据此跳过（preamble 在后续每块里都会重复，不会丢内容）。
    """
    if end_row is None:
        end_row = len(data_rows) - 1
    spans: list[tuple[str, int, int]] = []
    current_lines = list(preamble)
    first_row = start_row
    last_row = start_row - 1
    for row in range(start_row, end_row + 1):
        line = _format_row(data_rows[row], headers)
        candidate = "\n".join([*current_lines, line])
        if current_lines is not preamble and count_tokens(candidate) > max_tokens:
            if last_row >= first_row:
                spans.append(("\n".join(current_lines), first_row, last_row))
            else:
                # preamble-only 退化块：文本保留（口径一致），区间留空（无数据行）
                spans.append(("\n".join(current_lines), row, row - 1))
            current_lines = [*preamble, line]
            first_row = row
            last_row = row
        else:
            current_lines.append(line)
            last_row = row
    if len(current_lines) > len(preamble):
        spans.append(("\n".join(current_lines), first_row, last_row))
    return spans


def serialize_information_blocks_spans(
    *,
    caption: str = "",
    rows: Sequence[Sequence[str]] = (),
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
    row_range: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    """同 :func:`serialize_information_blocks`，但返回 ``(block_text, first_row, last_row)``。

    ``first_row``/``last_row`` 是**解析后数据行**的闭区间下标（0 基）：分块器用它按行
    切「表格父容器」、再让每段派生自己的 ≤384 叶子（见 ``chunker`` 的超限分段）。
    ``row_range`` 只序列化某一段数据行（表头仍按整表解析并重复），行号是**整表绝对下标**。

    对同一组行 / 同一 ``max_tokens``，本函数的文本与 :func:`serialize_information_blocks`
    逐字节一致 —— 分段只是换个"打包单位"，不改序列化口径。
    """
    prepared = _prepare_information_blocks(caption=caption, rows=rows, table_id=table_id)
    if prepared is None:
        return []
    _caption, preamble, _form_like, headers, data_rows = prepared
    start, end = 0, len(data_rows) - 1
    if row_range is not None:
        start, end = int(row_range[0]), int(row_range[1])
        if start < 0 or end < start or end >= len(data_rows):
            raise ValueError(f"row_range 越界：{row_range}（数据行 0..{len(data_rows) - 1}）")
    if row_chunks:
        return [
            ("\n".join([*preamble, _format_row(data_rows[row], headers)]), row, row)
            for row in range(start, end + 1)
        ]
    return _chunk_spans_by_budget(
        preamble, data_rows, headers, max_tokens=max_tokens, start_row=start, end_row=end
    )


def serialize_table_body_spans(
    *,
    caption: str = "",
    body: str = "",
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
    row_range: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    """从 MinerU ``table_body`` / 题注做 span 序列化（见 :func:`serialize_information_blocks_spans`）。"""
    return serialize_information_blocks_spans(
        caption=caption,
        rows=coerce_rows(body),
        table_id=table_id,
        row_chunks=row_chunks,
        max_tokens=max_tokens,
        row_range=row_range,
    )


def _note_row_cell(line: str, headers: Sequence[str]) -> str | None:
    """``备注=整行合并的说明`` → 说明文字；不是备注行返回 None。"""
    if not headers or not line.startswith("备注=") or ";" in line:
        return None
    note = line.split("=", 1)[1].strip()
    return note or None


def _row_cells_map(line: str) -> dict[str, str]:
    """``键=值; 键=值`` → ``{键: 值}``（保持出现顺序）。"""
    cells: dict[str, str] = {}
    for part in line.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        cells[key.strip()] = value.strip()
    return cells


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
        note = _note_row_cell(line, headers)
        if note is not None:
            # 整行合并的说明行：还原成"首格有字、其余留空"的一行，别在 HTML 里丢掉
            rows.append([note, *([""] * (len(headers) - 1))])
            continue
        if "=" in line and ";" in line:
            cells_map = _row_cells_map(line)
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
