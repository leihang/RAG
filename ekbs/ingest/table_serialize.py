"""确定性表格序列化 —— 产出可检索的 ``information_block`` 文本。

**不走云端 LLM**：同表同输出、无出网、可单测。

口径（与 chunker 父/叶子配合，已偏离旧「整表一个 LLM chunk」规格）：

* 解析层可给出连续 ``<th>`` 多级表头（日期行+字段名）；本模块优先消费，启发式仅兜底；
* 宽抄表按「身份列 + 单个指标组」切开，相关表头只带本组列；
* 电子表格 ``row_chunks`` 时一行一块（仍超预算则列窗）；父容器超 2048 由 chunker 分段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        self._cell_tag = "td"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "tr":
            self._row = []
            return
        if name in {"td", "th"} and self._row is not None:
            self._cell = []
            self._in_cell = True
            self._cell_tag = name
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
                    "tag": self._cell_tag,
                }
            )
            self._cell = None
            self._in_cell = False
            self._colspan = 1
            self._rowspan = 1
            self._cell_tag = "td"
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

        def consume_pending(bound_cells: dict[int, str] = cells) -> None:
            nonlocal column
            while column in pending and pending[column][1] > 0:
                text, remain = pending[column]
                bound_cells[column] = text
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


def _leading_th_row_count(raw_rows: Sequence[Sequence[dict[str, Any]]]) -> int:
    """开头连续「整行都是 th」的行数；xlsx 多级表头靠这个传到序列化，不再二次猜。"""
    count = 0
    for raw in raw_rows:
        if not raw:
            break
        tags = {str(cell.get("tag") or "td").lower() for cell in raw}
        if tags == {"th"}:
            count += 1
        else:
            break
    return count


def parse_table_html(html: str) -> list[list[str]]:
    """HTML → 二维单元格（``colspan``/``rowspan`` 已展开成等宽网格）；失败返回空表。"""
    rows, _header_n = parse_table_html_with_headers(html)
    return rows


def parse_table_html_with_headers(html: str) -> tuple[list[list[str]], int]:
    """同 :func:`parse_table_html`，额外返回开头连续 ``<th>`` 行数（0=无显式表头）。"""
    if not (html or "").strip():
        return [], 0
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return [], 0
    header_n = _leading_th_row_count(parser.raw_rows)
    return _expand_spans(parser.raw_rows), header_n


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
    rows, _header_n = coerce_rows_with_headers(body)
    return rows


def coerce_rows_with_headers(body: str) -> tuple[list[list[str]], int]:
    """同 :func:`coerce_rows`，附带显式 ``<th>`` 表头行数。"""
    text = (body or "").strip()
    if not text:
        return [], 0
    if "<table" in text.lower() or "<tr" in text.lower():
        rows, header_n = parse_table_html_with_headers(text)
        if rows:
            return rows, header_n
    rows = parse_table_markdown(text)
    if rows:
        return rows, 0
    return [[text]], 0


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


#: 表头账期：全日或月（归一后只保留 ``YYYY-MM``）
_DATE_CELL_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_FULL_DATE_CELL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_CELL_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _row_mostly_numeric(row: Sequence[str]) -> bool:
    """行内非空格半数以上是数字 → 数据行，不能当子表头合并。"""
    nonempty = [str(c).strip() for c in row if str(c).strip()]
    if len(nonempty) < 2:
        return False
    numeric = sum(1 for c in nonempty if _is_numeric_cell(c))
    return numeric * 2 >= len(nonempty)


def _is_excel_date_serial_text(text: str) -> bool:
    raw = str(text or "").strip()
    return bool(raw.isdigit() and 20000 <= int(raw) <= 80000)


def _excel_serials_look_like_calendar(serials: list[int]) -> bool:
    """与 parser 同口径：相邻差约 28–31 → 按月日期表头，不是抄表读数。"""
    uniq = sorted(set(serials))
    if len(uniq) < 2:
        return False
    diffs = [b - a for a, b in zip(uniq, uniq[1:], strict=False)]
    monthish = sum(1 for d in diffs if 27 <= d <= 32)
    return monthish >= max(1, (len(diffs) + 1) // 2)


def _row_looks_like_date_header(row: Sequence[str]) -> bool:
    """日期子表头：已是账期 ``YYYY-MM`` / ``YYYY-MM-DD``，或序列号呈按月间隔。"""
    cells = [str(c).strip() for c in row]
    dates = [c for c in cells if _DATE_CELL_RE.match(c)]
    if len(dates) >= 2:
        return True
    serials = [int(c) for c in cells if _is_excel_date_serial_text(c)]
    return len(serials) >= 2 and _excel_serials_look_like_calendar(serials)


def _coerce_date_cell(text: str) -> str:
    """表头账期格子 → ``YYYY-MM``（月序列不要带日）。"""
    raw = str(text or "").strip()
    full = _FULL_DATE_CELL_RE.match(raw)
    if full:
        return f"{full.group(1)}-{full.group(2)}"
    month = _MONTH_CELL_RE.match(raw)
    if month:
        return f"{month.group(1)}-{month.group(2)}"
    if _is_excel_date_serial_text(raw):
        try:
            from openpyxl.utils.datetime import from_excel

            return from_excel(int(raw)).strftime("%Y-%m")
        except Exception:
            return raw
    return ""


def normalize_header_period(raw: str) -> str:
    """列名后缀 / observation.period：``YYYY-MM-DD`` → ``YYYY-MM``，其余原样。"""
    text = str(raw or "").strip()
    full = _FULL_DATE_CELL_RE.match(text)
    if full:
        return f"{full.group(1)}-{full.group(2)}"
    return text


def _forward_fill_date_row(row: Sequence[str], width: int) -> list[str]:
    """把稀疏日期行（合并单元格留下的空格）按列前向填充。

    非日期的标签格（序号/户号）会打断填充，避免把日期灌进身份列。
    """
    out = [""] * width
    last = ""
    for i in range(width):
        cell = str(row[i]).strip() if i < len(row) else ""
        dated = _coerce_date_cell(cell)
        if dated:
            last = dated
            out[i] = last
        elif cell:
            last = ""
            out[i] = ""
        else:
            out[i] = last
    return out


def _apply_date_row_to_headers(headers: list[str], date_row: Sequence[str]) -> list[str]:
    """字段名行 + 上方日期行 → ``上月起度/2024-01``。"""
    filled = _forward_fill_date_row(date_row, len(headers))
    out: list[str] = []
    for i, header in enumerate(headers):
        date = filled[i] if i < len(filled) else ""
        if date and header and date not in header:
            out.append(f"{header}/{date}")
        else:
            out.append(header)
    return out


def _has_repeated_header_band(top: Sequence[str]) -> bool:
    """顶行是否有连续相同标签（xlsx 横向合并填充后的典型形态）。"""
    run = 1
    for i in range(1, len(top)):
        if top[i] and top[i] == top[i - 1]:
            run += 1
            if run >= 2:
                return True
        else:
            run = 1
    return False


def _looks_like_field_name_row(values: Sequence[str]) -> bool:
    """子行像字段名（短、少数字），不像「营运部 / 合格 / 2026-10-01」数据行。"""
    if _row_mostly_numeric(values):
        return False
    filled = [str(c).strip() for c in values if str(c).strip()]
    if len(filled) < 2:
        return False
    if sum(1 for c in filled if len(c) <= 20) < 2:
        return False
    digitish = sum(1 for c in filled if sum(ch.isdigit() for ch in c) >= 2)
    return digitish < max(1, len(filled) // 2)


def _can_merge_two_header_rows(top: Sequence[str], sub: Sequence[str]) -> bool:
    """两行能否合成父子表头。

    经典信号：顶行有空列、子行正好补上。
    xlsx 横向合并填充后顶行往往**没有空列**（整段都是「项目（收入成本均含税）」），
    但子行仍是「项目 / 取数账套 / 取数科目 / 项目明细」——也要合并，否则上级费用名
    全部塌成同一个键，问答定位错列。须同时有「连续同文案带宽」+「子行像字段名」，
    避免把首行数据误吃成子表头。
    """
    if not top or _row_mostly_numeric(sub):
        return False
    filled = [i for i, c in enumerate(sub) if c]
    if any(not top[i] and sub[i] for i in filled):
        return True
    if not _has_repeated_header_band(top) or not _looks_like_field_name_row(sub):
        return False
    differ = sum(
        1
        for i, label in enumerate(sub)
        if label and i < len(top) and top[i] and label != top[i]
    )
    return differ >= 2


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
    if not _can_merge_two_header_rows(top, sub):
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
    *,
    header_row_count: int = 0,
) -> tuple[bool, list[str], list[list[str]], str]:
    """确定 (form_like, headers, data_rows, section)。

    ``header_row_count``：解析层写入的连续 ``<th>`` 行数（日期行+字段名行）。>0 时
    直接消费，不再 `_pick_header_index` 二次猜测。
    ``section``：合并伪表头修复时回收的分节名（酒店/餐饮），否则空串。
    """
    form_like = _looks_like_form(matrix)
    if form_like:
        # 表单不伪造表头：保留所有行，列名用列序号，行内仍是原值（标签在值里）。
        headers = [f"列{i + 1}" for i in range(max(len(r) for r in matrix))]
        return True, headers, matrix, ""

    date_rows: list[list[str]] = []
    if header_row_count > 0:
        n = min(int(header_row_count), len(matrix))
        header_block = matrix[:n]
        data_rows = [list(r) for r in matrix[n:]]
        # 多行 th：日期行并进字段名；字段行走父子合并。
        # Word 式 rowspan 表头常是「一行 th + 下一行 td 子表头」—— th 计数只有 1，
        # 仍要把紧邻的非数字行交给 `_split_header_rows` 合并。
        field_rows: list[list[str]] = []
        for row in header_block:
            if _row_looks_like_date_header(row):
                date_rows.append(list(row))
            else:
                field_rows.append(list(row))
        if (
            len(field_rows) == 1
            and data_rows
            and not _row_mostly_numeric(data_rows[0])
        ):
            # Word rowspan：一行 th + 下一行 td 子表头
            headers, rest = _split_header_rows([field_rows[0], data_rows[0]])
            if not rest:  # 合并成功，子表头行已吃掉
                data_rows = list(data_rows[1:])
            else:
                headers, rest2 = _split_header_rows(field_rows)
                data_rows = [*rest2, *data_rows]
        elif field_rows:
            headers, rest = _split_header_rows(field_rows)
            data_rows = [*rest, *data_rows]
        elif date_rows:
            headers = [str(c).strip() or f"列{i+1}" for i, c in enumerate(date_rows[0])]
            date_rows = date_rows[1:]
        else:
            headers = [f"列{i + 1}" for i in range(max(len(r) for r in matrix))]
    else:
        header_index = _pick_header_index(matrix)
        before_header = matrix[:header_index]
        headers, data_rows = _split_header_rows(matrix[header_index:])
        # 兜底：无显式 th 时仍把 before_header 里的日期行并进列名（旧 HTML / Markdown）。
        kept_before: list[list[str]] = []
        for row in before_header:
            if _row_looks_like_date_header(row):
                date_rows.append(list(row))
            else:
                kept_before.append(list(row))
        while data_rows and _row_looks_like_date_header(data_rows[0]):
            date_rows.append(list(data_rows.pop(0)))
        data_rows = [*kept_before, *data_rows]

    for date_row in date_rows:
        headers = _apply_date_row_to_headers(headers, date_row)
    # 无表头只有一行时：整行当数据，用列序号作表头
    if not data_rows:
        data_rows = [matrix[0]]
        headers = [f"列{i + 1}" for i in range(len(matrix[0]))]
    # 列名里若已带 ``/YYYY-MM-DD``（旧串/手工矩阵），收成 ``/YYYY-MM``
    headers = [_normalize_dated_header_label(h) for h in headers]
    headers, section = _repair_month_series_headers(headers, data_rows)
    return False, headers, data_rows, section


def _normalize_dated_header_label(header: str) -> str:
    text = str(header or "")
    if "/" not in text:
        return text
    # 单位里带斜杠（KW/h）时不能 split("/", 1)，只收尾部账期
    match = re.search(r"(20\d{2}-\d{2})(?:-\d{2})?\s*$", text)
    if match:
        base = text[: match.start()].rstrip("/").strip() or text
        return f"{base}/{normalize_header_period(match.group(1))}"
    base, suffix = text.split("/", 1)
    return f"{base}/{normalize_header_period(suffix)}"


def _repair_month_series_headers(
    headers: list[str], data_rows: list[list[str]]
) -> tuple[list[str], str]:
    """修复「整行合并表头」丢失月份名：15 列、后 12 列是数值时补 1月..12月。

    实测「大户用电监视」：xlsx 合并表头把 15 个 <th> 都填成「酒店」，
    序列化后变成 ``酒店=三期3幢…; 酒店=嘛嘛公寓; 酒店=2026年; 酒店=84445…``，
    模型能看到数字却不知道哪一列是 1 月，于是回答“未提供 1—9 月数据”。

    返回 ``(新表头, 分节名)``：分节名（酒店/餐饮…）写入题注供 ``类别=`` 熔叶，
    身份列用铺号/用户名而非「字段1/字段2」。
    """
    width = len(headers)
    if width < 13:
        return headers, ""
    labels = {str(h).strip() for h in headers if str(h).strip()}
    if len(labels) > 1:
        return headers, ""
    section = next(iter(labels)) if labels else ""
    # 后 12 列至少一半非空数字，才认定是月份序列；避免误伤其它重复表头表。
    numeric_tail = 0
    for row in data_rows:
        tail = row[-12:]
        numeric_tail += sum(1 for value in tail if _is_numeric_cell(value))
    if numeric_tail < 6:
        return headers, ""
    prefix_count = width - 12
    # 最后一列身份列若是年份（2024年/2025年/2026年），命名成“年度”。
    year_like = 0
    if prefix_count and data_rows:
        year_like = sum(
            1 for row in data_rows
            if len(row) >= prefix_count and re.search(r"20\d{2}", str(row[prefix_count - 1]))
        )
    if prefix_count and year_like >= max(1, len(data_rows) // 2):
        prefix = [*_identity_header_names(prefix_count - 1), "年度"]
    else:
        prefix = _identity_header_names(prefix_count)
    return [*prefix, *[f"{month}月" for month in range(1, 13)]], section


def _drop_empty_columns(
    headers: list[str], data_rows: list[list[str]]
) -> tuple[list[str], list[list[str]]]:
    """丢掉**整列都没有数据**的列。

    xlsx/docx 的合并标题行会把表头展开成几十列（``列15…列39``），每个数据行都写成
    ``列N=`` 的空键值；LLM 在噪声里容易漏掉真正的行（实测 2026-09 用电分析表 39 列，
    2 段切分后漏了「经营用电」）。这里按数据行判断，统整列空的才丢；表头有含义但
    全空也丢——检索/回答都不需要它。
    """
    if not headers or not data_rows:
        return headers, data_rows
    width = len(headers)
    normalized = [
        [str(row[i]).strip() if i < len(row) else "" for i in range(width)]
        for row in data_rows
    ]
    def _placeholder(header: str) -> bool:
        text = str(header or "").strip()
        return not text or bool(re.fullmatch(r"列\d+", text))

    keep = [
        i
        for i in range(width)
        # 只丢「整列无数据 + 表头是占位符/空」的列；有业务含义的表头即使暂时无值也保留
        # （测试钉住「用量日期」这种首列标签不能被误删）。
        if any(row[i] for row in normalized) or not _placeholder(headers[i])
    ]
    if not keep:  # 只有表头、没有数据：保留原样
        return headers, data_rows
    if len(keep) == width:
        return headers, data_rows
    return (
        [headers[i] for i in keep],
        [[row[i] for i in keep] for row in normalized],
    )


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


#: 宽月份表 → tidy 长叶子（一行一个「实体×年×月」观测）。比较/排名题靠它做同行集合召回。
_MONTH_HEADER_RE = re.compile(r"^(\d{1,2})\s*月$")
_YEAR_VALUE_RE = re.compile(r"^(20\d{2})\s*年?$")
_YEAR_HEADER_RE = re.compile(r"^(年度|年份|年)$")
#: 排名摘要块默认 TopN
_MONTH_RANK_TOP_N = 10


@dataclass(frozen=True)
class _MonthSeriesLayout:
    """宽月份表布局：身份列 + 年度列 + 1–12 月列。"""

    identity: tuple[tuple[int, str], ...]  # (col_idx, header)
    year_col: int
    year_header: str
    months: tuple[tuple[int, int], ...]  # (col_idx, month 1-12)
    category: str
    metric: str
    #: 无物理年列时由文件名/题注推断的年份（仅开关开启时使用）
    default_year: str | None = None


def _metric_name_from_caption(caption: str) -> str:
    text = caption or ""
    if "用电" in text:
        return "用电量"
    if "用水" in text:
        return "用水量"
    if "用气" in text or "燃气" in text:
        return "用气量"
    return "数值"


def _category_from_caption(caption: str) -> str:
    """``大户用电监视（第1段）（… / 餐饮）`` → ``餐饮``。"""
    match = re.search(r"/\s*([^/）\)]+?)\s*[）)]\s*$", caption or "")
    return match.group(1).strip() if match else ""


def _normalize_year_value(raw: str) -> str:
    match = _YEAR_VALUE_RE.match((raw or "").strip())
    return match.group(1) if match else (raw or "").strip()


def _headers_uniform_label(headers: Sequence[str]) -> str | None:
    """全列同标签（分节后伪表头，如整行「娱乐」）→ 返回该标签；否则 None。"""
    labels = [str(h).strip() for h in headers if str(h).strip()]
    if len(labels) < 4:
        return None
    first = labels[0]
    if _MONTH_HEADER_RE.match(first) or _YEAR_HEADER_RE.match(first):
        return None
    if all(label == first for label in labels):
        return first
    return None


def _find_year_column(data_rows: Sequence[Sequence[str]], width: int) -> int | None:
    """数据里哪一列多数是 ``2024年`` / ``2026`` 这种年份。"""
    if not data_rows or width <= 0:
        return None
    best_col, best_hits = None, 0
    for col in range(width):
        hits = 0
        for row in data_rows:
            cell = str(row[col]).strip() if col < len(row) else ""
            if cell and _YEAR_VALUE_RE.match(cell):
                hits += 1
        if hits > best_hits:
            best_col, best_hits = col, hits
    if best_col is None or best_hits < max(1, len(data_rows) // 3):
        return None
    return best_col


def _identity_header_names(count: int) -> list[str]:
    """无可靠表头时的身份列命名：两列默认铺号/用户名（大户监视表口径）。"""
    if count <= 0:
        return []
    if count == 1:
        return ["名称"]
    if count == 2:
        return ["铺号", "用户名"]
    return ["铺号", "用户名", *[f"列{i + 1}" for i in range(2, count)]]


def _month_cols_from_headers(headers: Sequence[str]) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for idx, header in enumerate(headers):
        match = _MONTH_HEADER_RE.match(str(header).strip())
        if not match:
            continue
        month = int(match.group(1))
        if 1 <= month <= 12:
            found.append((idx, month))
    return found


def _resolve_year_col_for_months(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    width: int,
    synthetic_year: str | None,
) -> int | None:
    """表头/数据年列；缺年且无 synthetic → None（不熔）。"""
    year_col = next(
        (i for i, h in enumerate(headers) if _YEAR_HEADER_RE.match(str(h).strip())),
        None,
    )
    if year_col is None:
        year_col = _find_year_column(data_rows, width)
    if year_col is not None:
        return year_col
    if synthetic_year is None:
        # Phase A 安全默认（单测钉死）：铺号|用户名|1月… 且无年列 → **不熔**。
        # 缺年无法同行跨月对齐；禁止静默用文件名猜年（须显式传 default_year，
        # 对应 retrieval.month_series_infer_year_from_filename，默认关）。
        return None
    return width  # 虚拟年列：熔时读 default_year


def _identity_before_year(
    headers: Sequence[str],
    *,
    width: int,
    year_col: int,
    months: Sequence[tuple[int, int]],
) -> tuple[tuple[int, str], ...]:
    month_idxs = {m[0] for m in months}
    identity = tuple(
        (i, str(headers[i]).strip() or f"列{i + 1}")
        for i in range(width)
        if i != year_col and i not in month_idxs
    )
    if year_col < width:
        return tuple((i, name) for i, name in identity if i < year_col)
    first_month = min(m[0] for m in months)
    return tuple((i, name) for i, name in identity if i < first_month)


def _layout_from_month_headers(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    *,
    months: Sequence[tuple[int, int]],
    category: str,
    metric: str,
    synthetic_year: str | None,
) -> _MonthSeriesLayout | None:
    width = len(headers)
    year_col = _resolve_year_col_for_months(headers, data_rows, width, synthetic_year)
    if year_col is None:
        return None
    identity = _identity_before_year(
        headers, width=width, year_col=year_col, months=months
    )
    if not identity:
        return None
    return _MonthSeriesLayout(
        identity=identity,
        year_col=year_col,
        year_header=(
            str(headers[year_col]).strip() or "年度" if year_col < width else "年度"
        ),
        months=tuple(months),
        category=category,
        metric=metric,
        default_year=synthetic_year if year_col >= width else None,
    )


def _infer_months_after_year(
    data_rows: Sequence[Sequence[str]], *, year_col: int, width: int
) -> list[tuple[int, int]]:
    """年份列后连续数值列推断为 1..N 月（至少要 6 列才算系列）。"""
    inferred: list[tuple[int, int]] = []
    for offset, col in enumerate(range(year_col + 1, min(width, year_col + 1 + 12))):
        numeric_hits = 0
        nonempty = 0
        for row in data_rows:
            cell = str(row[col]).strip() if col < len(row) else ""
            if not cell:
                continue
            nonempty += 1
            try:
                float(cell.replace(",", ""))
                numeric_hits += 1
            except ValueError:
                pass
        if nonempty == 0 or numeric_hits >= max(1, nonempty // 2):
            inferred.append((col, offset + 1))
        else:
            break
    return inferred


def detect_month_series_layout(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    *,
    caption: str = "",
    default_year: str | None = None,
) -> _MonthSeriesLayout | None:
    """识别「身份 + 年度 + 1–12 月」宽表；分节伪表头也走数据形态兜底。

    ``default_year``：仅当配置 ``month_series_infer_year_from_filename=true`` 时由调用方
    传入（文件名/题注推断）。**默认 ``None`` = 缺年列则不熔**（安全默认，避免跨年错位）。
    """
    if not headers or not data_rows:
        return None
    width = len(headers)
    months = _month_cols_from_headers(headers)
    category = _category_from_caption(caption)
    metric = _metric_name_from_caption(caption)
    synthetic_year = (default_year or "").strip() or None

    if len(months) >= 6:
        return _layout_from_month_headers(
            headers,
            data_rows,
            months=months,
            category=category,
            metric=metric,
            synthetic_year=synthetic_year,
        )

    # 分节后伪表头：全列同标签 + 数据里有年份列 + 其后多为月读数
    uniform = _headers_uniform_label(headers)
    year_col = _find_year_column(data_rows, width)
    if year_col is None or year_col < 1:
        return None
    inferred = _infer_months_after_year(data_rows, year_col=year_col, width=width)
    if len(inferred) < 6:
        return None
    id_names = _identity_header_names(year_col)
    identity = tuple((i, id_names[i]) for i in range(year_col))
    if uniform and not category:
        category = uniform
    return _MonthSeriesLayout(
        identity=identity,
        year_col=year_col,
        year_header="年度",
        months=tuple(inferred),
        category=category,
        metric=metric,
        default_year=None,
    )


def table_body_is_month_series(
    *,
    caption: str = "",
    body: str = "",
    default_year: str | None = None,
) -> bool:
    """电子表格叶子路径：是否应按月份熔成长叶子。"""
    rows, header_n = coerce_rows_with_headers(body)
    prepared = _prepare_information_blocks(
        caption=caption, rows=rows, table_id="", header_row_count=header_n
    )
    if prepared is None:
        return False
    _cap, _pre, _fl, headers, data_rows = prepared
    return (
        detect_month_series_layout(
            headers, data_rows, caption=caption, default_year=default_year
        )
        is not None
    )


def _parse_metric_number(raw: str) -> float | None:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _tidy_out_headers(layout: _MonthSeriesLayout) -> list[str]:
    headers = [name for _i, name in layout.identity]
    if layout.category:
        headers.append("类别")
    headers.extend([layout.year_header or "年度", "月份", layout.metric])
    return [
        "年度" if (_YEAR_HEADER_RE.match(name) or name in {"年份", "年"}) else name
        for name in headers
    ]


def _rank_summary_spans(
    *,
    caption: str,
    table_id: str,
    form_like: bool,
    layout: _MonthSeriesLayout,
    buckets: dict[tuple[str, int], list[tuple[str, str, float, int]]],
) -> list[tuple[str, int, int]]:
    """按 (年,月) 生成 TopN 排名摘要块。"""
    spans: list[tuple[str, int, int]] = []
    summary_headers = ["排名摘要", "年度", "月份", "指标"]
    preamble = _build_preamble(caption, table_id, summary_headers, form_like)
    for (year, month), entries in sorted(buckets.items()):
        ranked = sorted(entries, key=lambda item: item[2], reverse=True)[:_MONTH_RANK_TOP_N]
        if not ranked:
            continue
        parts = [
            f"排名类型=Top{_MONTH_RANK_TOP_N}",
            f"年度={year}",
            f"月份={month}",
            f"指标={layout.metric}",
        ]
        if layout.category:
            parts.append(f"类别={layout.category}")
        rank_bits = [
            f"{idx}. {entity}={surface}"
            for idx, (entity, surface, _num, _row) in enumerate(ranked, start=1)
        ]
        line = "; ".join(parts) + "\n" + "; ".join(rank_bits)
        spans.append(("\n".join([*preamble, line]), ranked[0][3], ranked[0][3]))
    return spans


def _melt_month_series_blocks(
    *,
    caption: str,
    table_id: str,
    form_like: bool,
    layout: _MonthSeriesLayout,
    data_rows: Sequence[Sequence[str]],
    row_range: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    """熔成 tidy 长叶子，并按 (年,月) 追加 TopN 排名摘要块。"""
    out_headers = _tidy_out_headers(layout)
    preamble = _build_preamble(caption, table_id, out_headers, form_like)
    start, end = 0, len(data_rows) - 1
    if row_range is not None:
        start, end = int(row_range[0]), int(row_range[1])

    buckets: dict[tuple[str, int], list[tuple[str, str, float, int]]] = {}
    spans: list[tuple[str, int, int]] = []
    for row_idx in range(start, end + 1):
        row = data_rows[row_idx]
        if layout.default_year and layout.year_col >= len(row):
            year = _normalize_year_value(layout.default_year)
        else:
            year_raw = (
                str(row[layout.year_col]).strip() if layout.year_col < len(row) else ""
            )
            year = _normalize_year_value(year_raw)
        if not year:
            continue
        id_values = [
            str(row[col]).strip() if col < len(row) else ""
            for col, _name in layout.identity
        ]
        if not any(id_values):
            continue
        entity = next((v for v in reversed(id_values) if v), id_values[0])
        for col, month in layout.months:
            raw = str(row[col]).strip() if col < len(row) else ""
            if not raw:
                continue
            cells = list(id_values)
            if layout.category:
                cells.append(layout.category)
            cells.extend([year, str(month), raw])
            spans.append(("\n".join([*preamble, _format_row(cells, out_headers)]), row_idx, row_idx))
            number = _parse_metric_number(raw)
            if number is not None:
                buckets.setdefault((year, month), []).append((entity, raw, number, row_idx))
    spans.extend(
        _rank_summary_spans(
            caption=caption,
            table_id=table_id,
            form_like=form_like,
            layout=layout,
            buckets=buckets,
        )
    )
    return spans


def _chunk_by_budget(
    preamble: list[str], data_rows: list[list[str]], headers: list[str],
    *, max_tokens: int = TABLE_MAX_TOKENS,
) -> list[str]:
    """按 token 预算把数据行分批，每批重复 preamble。

    宽表单行超预算时**不**在这里按列拆——列窗只给 ``row_chunks`` 叶子用；父容器
    仍按行打包，单行超限走原有 preamble 退化块口径（与 span 版对齐）。
    """
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
    if len(current_lines) > len(preamble) or not blocks and data_rows:
        blocks.append("\n".join(current_lines))
    return blocks


def _prepare_information_blocks(
    *,
    caption: str,
    rows: Sequence[Sequence[str]],
    table_id: str,
    header_row_count: int = 0,
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
    # 剥掉标题行后，显式 th 计数要扣掉已剥行数（标题常被标成合并单元格 td，一般是 0）。
    form_like, headers, data_rows, section = _resolve_headers_and_data(
        matrix, header_row_count=header_row_count
    )
    headers, data_rows = _drop_empty_columns(headers, data_rows)
    # 合并伪表头「酒店|酒店|…」修好后，把分节名写回题注，熔叶才能带 ``类别=酒店``。
    if section and section not in (caption or ""):
        base = (caption or "").strip()
        caption = f"{base}（/ {section}）" if base else section
    preamble = _build_preamble(caption, table_id, headers, form_like)
    return caption, preamble, form_like, headers, data_rows


def _leading_identity_width(headers: Sequence[str]) -> int:
    """宽表身份列宽度：截止到第一个「后面还会再出现」的字段名之前。

    水费表头是 ``序号|水卡号|水表号|用途|上月起度/…|…|上月起度/…`` —— 身份列是前 4 列，
    不能把第一个月的指标组算进身份（否则每窗都带着一整月，叶子很容易 >384）。
    """
    bases = [str(h).split("/", 1)[0].strip() for h in headers]
    for i, base in enumerate(bases):
        if base and base in bases[i + 1 :]:
            return i if i > 0 else 1
    return min(4, len(headers)) or 1


def _metric_group_width(headers: Sequence[str], id_n: int) -> int:
    """重复指标组宽度（水费：上月起度…金额 = 5；找不到则 8）。"""
    rest = [str(h).split("/", 1)[0].strip() for h in headers[id_n:]]
    if not rest:
        return 1
    first = rest[0]
    for i in range(1, len(rest)):
        if rest[i] == first:
            return i
    return min(8, len(rest)) or 1


def _is_wide_metric_table(headers: Sequence[str]) -> bool:
    """是否应按指标组切开：身份列后至少两组重复指标（抄表宽表）。"""
    id_n = _leading_identity_width(headers)
    rest = len(headers) - id_n
    if rest < 4:
        return False
    group = _metric_group_width(headers, id_n)
    return group >= 2 and rest >= group * 2


def _iter_metric_group_slices(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
) -> list[tuple[list[str], list[list[str]]]]:
    """把宽表拆成「身份列 + 单个指标组」子表；非宽表返回整表一份。

    每片 preamble 只带本组表头，避免 169 列「相关表头」吃光叶子预算。
    """
    headers_l = [str(h) for h in headers]
    rows_l = [
        [str(row[i]) if i < len(row) else "" for i in range(len(headers_l))]
        for row in data_rows
    ]
    if not _is_wide_metric_table(headers_l):
        return [(headers_l, rows_l)]
    id_n = _leading_identity_width(headers_l)
    group = _metric_group_width(headers_l, id_n)
    id_h = headers_l[:id_n]
    rest_h = headers_l[id_n:]
    slices: list[tuple[list[str], list[list[str]]]] = []
    for start in range(0, len(rest_h), group):
        win_h = id_h + rest_h[start : start + group]
        win_rows = [
            row[:id_n] + row[id_n + start : id_n + start + group] for row in rows_l
        ]
        slices.append((win_h, win_rows))
    return slices or [(headers_l, rows_l)]


def _format_row_column_windows(
    preamble: list[str],
    headers: Sequence[str],
    cells: Sequence[str],
    *,
    max_tokens: int,
) -> list[str]:
    """单行仍超预算时按列窗拆（宽抄表：169/233 列，一行就能上千 token）。

    每窗重复身份列 + 若干「指标组」；按组推进并用二分找能装下的最大组数，
    避免按列线性收缩导致 O(列²) 次 ``count_tokens``（实测会卡死分块）。
    """
    full = "\n".join([*preamble, _format_row(cells, headers)])
    if count_tokens(full) <= max_tokens:
        return [full]
    id_n = _leading_identity_width(headers)
    id_headers = list(headers[:id_n])
    id_cells = [str(cells[i]) if i < len(cells) else "" for i in range(id_n)]
    rest_headers = list(headers[id_n:])
    rest_cells = [str(cells[i]) if i < len(cells) else "" for i in range(id_n, len(headers))]
    if not rest_headers:
        return [full]
    group = _metric_group_width(headers, id_n)
    n = len(rest_headers)
    n_groups = (n + group - 1) // group

    def _window_text(g0: int, g1: int) -> str:
        c0, c1 = g0 * group, min(n, g1 * group)
        win_h = id_headers + rest_headers[c0:c1]
        win_c = id_cells + rest_cells[c0:c1]
        win_preamble = [
            preamble[0] if preamble else "表名: 表",
            preamble[1] if len(preamble) > 1 else "核心实体: 表",
            f"相关表头: {' | '.join(win_h)}",
            *preamble[3:],
        ]
        return "\n".join([*win_preamble, _format_row(win_c, win_h)])

    blocks: list[str] = []
    g_start = 0
    while g_start < n_groups:
        lo, hi, best = 1, n_groups - g_start, 1
        while lo <= hi:
            mid = (lo + hi) // 2
            text = _window_text(g_start, g_start + mid)
            if count_tokens(text) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        text = _window_text(g_start, g_start + best)
        # 单组仍超预算：退化为按组硬切（至少前进，内容不丢）
        if count_tokens(text) > max_tokens and best == 1:
            text = _window_text(g_start, g_start + 1)
        blocks.append(text)
        g_start += best
    return blocks or [full]


def _sample_row_indices(n_rows: int, sample_max: int) -> list[int]:
    """头尾 + 均匀抽样，保证定位用叶子覆盖表首尾与中间段。"""
    if n_rows <= sample_max or sample_max <= 0:
        return list(range(n_rows))
    if sample_max == 1:
        return [0]
    # 保留首尾各约 10%，其余均匀
    head = max(1, sample_max // 10)
    tail = max(1, sample_max // 10)
    mid_budget = sample_max - head - tail
    chosen: set[int] = set(range(head))
    chosen.update(range(n_rows - tail, n_rows))
    if mid_budget > 0 and n_rows > head + tail:
        span = n_rows - head - tail
        for i in range(mid_budget):
            offset = head + int((i + 0.5) * span / mid_budget)
            chosen.add(min(n_rows - 1, offset))
    return sorted(chosen)


def _serialize_prepared_slices(
    *,
    caption: str,
    table_id: str,
    form_like: bool,
    headers: list[str],
    data_rows: list[list[str]],
    row_chunks: bool,
    max_tokens: int,
    default_year: str | None = None,
    leaf_sample_max: int | None = None,
) -> list[str]:
    """宽表先按指标组切（窄 preamble），再按行/行块打包。

    电子表格 ``row_chunks`` 且识别为月份宽表时：只发 tidy 长叶子（+ 按月 TopN），
    避免宽行与长观测双倍噪声。

    ``leaf_sample_max``：B 型大表明细叶子抽样上限（``None`` = 全量）；观测表仍全量双写。
    """
    if row_chunks:
        layout = detect_month_series_layout(
            headers, data_rows, caption=caption, default_year=default_year
        )
        if layout is not None:
            return [
                text
                for text, _first, _last in _melt_month_series_blocks(
                    caption=caption,
                    table_id=table_id,
                    form_like=form_like,
                    layout=layout,
                    data_rows=data_rows,
                )
            ]
    blocks: list[str] = []
    for slice_headers, slice_rows in _iter_metric_group_slices(headers, data_rows):
        preamble = _build_preamble(caption, table_id, slice_headers, form_like)
        if row_chunks:
            if not slice_rows:
                blocks.append("\n".join(preamble))
                continue
            sampling = (
                leaf_sample_max is not None and len(slice_rows) > int(leaf_sample_max)
            )
            # 摘要叶占 1 个预算槽，行样本用剩余额度，保证总叶子 ≤ leaf_sample_max
            row_budget = (
                max(1, int(leaf_sample_max) - 1)
                if sampling and leaf_sample_max is not None
                else leaf_sample_max
            )
            indices = (
                _sample_row_indices(len(slice_rows), int(row_budget))
                if leaf_sample_max is not None
                else list(range(len(slice_rows)))
            )
            # B 型大表抽样时先塞一条「摘要叶」：列名 + 行数，便于 FAISS 定位到表，
            # 全量合计仍走观测表 structured_query（本函数只负责向量侧叶子）。
            if sampling:
                summary = list(preamble) + [
                    f"数据行数={len(slice_rows)}",
                    f"向量叶子=抽样{len(indices)}行（头尾+均匀）",
                    "完整行已写入结构化观测表，合计/笔数请走聚合查询",
                ]
                blocks.append("\n".join(summary))
            for idx in indices:
                cells = slice_rows[idx]
                # 组切后通常一行已 ≤ 预算；仍超则列窗兜底
                blocks.extend(
                    _format_row_column_windows(
                        preamble, slice_headers, cells, max_tokens=max_tokens
                    )
                )
        else:
            blocks.extend(
                _chunk_by_budget(preamble, slice_rows, slice_headers, max_tokens=max_tokens)
            )
    return blocks


def serialize_information_blocks(
    *,
    caption: str = "",
    rows: Sequence[Sequence[str]] = (),
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
    header_row_count: int = 0,
    default_year: str | None = None,
    leaf_sample_max: int | None = None,
) -> list[str]:
    """把二维表打成一段或多段自解释文本（超长按行批拆）。

    ``max_tokens``：每段预算。默认 2048（整表父容器用）；行组叶子传更小的值
    （见 ``chunker.TABLE_LEAF_TOKENS=384``），避免十几行挤进同一个向量被稀释。

    宽抄表（重复指标组）会先按「身份列+单月指标」切开，相关表头只带本组列。
    """
    prepared = _prepare_information_blocks(
        caption=caption,
        rows=rows,
        table_id=table_id,
        header_row_count=header_row_count,
    )
    if prepared is None:
        return []
    cap, _preamble, form_like, headers, data_rows = prepared
    return _serialize_prepared_slices(
        caption=cap,
        table_id=table_id,
        form_like=form_like,
        headers=headers,
        data_rows=data_rows,
        row_chunks=row_chunks,
        max_tokens=max_tokens,
        default_year=default_year,
        leaf_sample_max=leaf_sample_max,
    )


def serialize_table_body(
    *,
    caption: str = "",
    body: str = "",
    table_id: str = "",
    row_chunks: bool = False,
    max_tokens: int = TABLE_MAX_TOKENS,
    default_year: str | None = None,
    leaf_sample_max: int | None = None,
) -> list[str]:
    """从 MinerU ``table_body`` / 题注直接序列化。"""
    rows, header_n = coerce_rows_with_headers(body)
    return serialize_information_blocks(
        caption=caption,
        rows=rows,
        table_id=table_id,
        row_chunks=row_chunks,
        default_year=default_year,
        leaf_sample_max=leaf_sample_max,
        max_tokens=max_tokens,
        header_row_count=header_n,
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
    header_row_count: int = 0,
) -> list[tuple[str, int, int]]:
    """同 :func:`serialize_information_blocks`，但返回 ``(block_text, first_row, last_row)``。

    ``first_row``/``last_row`` 是**解析后数据行**的闭区间下标（0 基）：分块器用它按行
    切「表格父容器」、再让每段派生自己的 ≤384 叶子（见 ``chunker`` 的超限分段）。
    ``row_range`` 只序列化某一段数据行（表头仍按整表解析并重复），行号是**整表绝对下标**。

    对同一组行 / 同一 ``max_tokens``，本函数的文本与 :func:`serialize_information_blocks`
    逐字节一致 —— 分段只是换个"打包单位"，不改序列化口径。

    宽表按指标组切开后，同一行号可能在多个 span 中出现（每月一片）；行号仍是整表绝对下标。
    """
    prepared = _prepare_information_blocks(
        caption=caption,
        rows=rows,
        table_id=table_id,
        header_row_count=header_row_count,
    )
    if prepared is None:
        return []
    cap, _preamble, form_like, headers, data_rows = prepared
    start, end = 0, len(data_rows) - 1
    if row_range is not None:
        start, end = int(row_range[0]), int(row_range[1])
        if start < 0 or end < start or end >= len(data_rows):
            raise ValueError(f"row_range 越界：{row_range}（数据行 0..{len(data_rows) - 1}）")
    if row_chunks:
        layout = detect_month_series_layout(headers, data_rows, caption=cap)
        if layout is not None:
            return _melt_month_series_blocks(
                caption=cap,
                table_id=table_id,
                form_like=form_like,
                layout=layout,
                data_rows=data_rows,
                row_range=(start, end),
            )
    spans: list[tuple[str, int, int]] = []
    for slice_headers, slice_rows in _iter_metric_group_slices(headers, data_rows):
        preamble = _build_preamble(cap, table_id, slice_headers, form_like)
        if row_chunks:
            for row in range(start, end + 1):
                for text in _format_row_column_windows(
                    preamble, slice_headers, slice_rows[row], max_tokens=max_tokens
                ):
                    spans.append((text, row, row))
        else:
            spans.extend(
                _chunk_spans_by_budget(
                    preamble,
                    slice_rows,
                    slice_headers,
                    max_tokens=max_tokens,
                    start_row=start,
                    end_row=end,
                )
            )
    return spans


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
    rows, header_n = coerce_rows_with_headers(body)
    return serialize_information_blocks_spans(
        caption=caption,
        rows=rows,
        table_id=table_id,
        row_chunks=row_chunks,
        max_tokens=max_tokens,
        row_range=row_range,
        header_row_count=header_n,
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
