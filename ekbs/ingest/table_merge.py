"""跨页续表合并（清洗 10 步之 4，`docs/spec/20` §2 #4）。

MinerU 常把同一张表按页拆成多个 ``type=table`` 块。本模块在解析/清洗阶段把
**相邻且同构**的续表合成一张；超长表（合并后无法稳妥序列化/分片）则保持拆开，
由 ``table_serialize`` 按行批拆成多个 ``table`` 块。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Sequence, TypeVar

from ekbs.ingest.chunker import count_tokens
from ekbs.ingest.table_serialize import TABLE_MAX_TOKENS, coerce_rows
from ekbs.logging import get_logger

log = get_logger(component="ingest.table_merge")

#: 合并后预估 token 上限：超过则不合并（分片侧单块 2048，允许多块，但整表过大易拖垮解析/复核）
TABLE_MERGE_MAX_TOKENS = TABLE_MAX_TOKENS * 4  # 8192

_PLACEHOLDER = re.compile(r"^\[表格\s+(\d+)\](.*)$", re.MULTILINE)
_CONTINUED_CAPTION = re.compile(
    r"(续表|续前表|续上表|表\s*续|(?:^|[（(【\[])\s*续\s*(?:[）)】\]]|$)|continued)",
    re.IGNORECASE,
)

T = TypeVar("T")

_WQ_HEADER = ["项目", "PH", "电导率", "总硬度", "总浊度", "总碱度", "氯离子", "总铁", "铜离子"]
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_GLUED_UNIT = re.compile(r"^(度[（(]ntu[）)])\s*(Mg/L)$", re.IGNORECASE)
_LABEL_SHIFT = re.compile(r"^(冷冻|采暖|冷却|热水)([\d<≤≥].*)$")


def repair_table_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """修扫描件表格里几种稳定的错位，不改已经对齐的表。

    * 分组表头「工作内容 / 内容|频次|备注」收成一行三列。
    * 首格为空、次格被竖线粘住的行拆回三列。
    * 水质指标表的竖排表头、单位粘连、以及「水 / 冷冻7.0」这种串行。
    """
    matrix = [[_CJK_GAP.sub("", str(cell).strip()) for cell in row] for row in rows]
    matrix = [_unshift_pipe_row(row) for row in matrix]
    matrix = _flatten_work_header(matrix)
    return _repair_water_quality(matrix)


def repair_table_html(body: str) -> tuple[str, bool]:
    """返回 (html, 是否改过)。没改则保留原 HTML。"""
    rows = coerce_rows(body)
    if not rows:
        return body, False
    fixed = repair_table_rows(rows)
    before = [[str(cell).strip() for cell in row] for row in rows]
    if fixed == before:
        return body, False
    return _rows_to_html(fixed), True


def _unshift_pipe_row(row: list[str]) -> list[str]:
    """``['', '管路、主机清洗及投药|开机…', '备注']`` → 三列各归其位。"""
    if len(row) >= 2 and not row[0] and "|" in row[1]:
        left, right = row[1].split("|", 1)
        return [left.strip(), right.strip(), *row[2:]]
    return row


def _flatten_work_header(rows: list[list[str]]) -> list[list[str]]:
    """跨页表的首页往往只剩分组表头：工作内容（跨两列）+ 备注，次行才是 内容|频次。"""
    if len(rows) < 2:
        return rows
    head, sub = rows[0], rows[1]
    if "工作内容" in head and "备注" in head and "频次" in sub and "内容" in sub:
        return [["工作内容", "频次", "备注"], *rows[2:]]
    return rows


def _repair_water_quality(rows: list[list[str]]) -> list[list[str]]:
    blob = "".join(cell for row in rows for cell in row)
    if "水处理控制指标" not in blob and "DB44" not in blob:
        return rows
    out: list[list[str]] = []
    for row in rows:
        joined = "".join(row)
        if "水处理控制指标" in joined or "DB44" in joined:
            title = next((cell for cell in row if cell), "")
            width = max(len(row), len(_WQ_HEADER))
            out.append([title] + [""] * (width - 1))
            continue
        if "总硬" in joined and "浊" in joined and "铜离子" in joined:
            out.append(list(_WQ_HEADER))
            continue
        if row and row[0] == "单位":
            out.append(_split_glued_units(row))
            continue
        out.append(_repair_quality_label(row))
    return out


def _split_glued_units(row: list[str]) -> list[str]:
    cells: list[str] = []
    index = 0
    while index < len(row):
        matched = _GLUED_UNIT.match(row[index])
        if matched:
            cells.append(matched.group(1))
            cells.append(matched.group(2))
            if index + 1 < len(row) and not row[index + 1]:
                index += 2
                continue
        else:
            cells.append(row[index])
        index += 1
    width = len(_WQ_HEADER)
    if len(cells) < width:
        cells.extend([""] * (width - len(cells)))
    return cells[:width]


def _repair_quality_label(row: list[str]) -> list[str]:
    if len(row) < 2:
        return row
    if row[0] == "水":
        matched = _LABEL_SHIFT.match(row[1])
        if matched:
            return [matched.group(1) + "水", matched.group(2), *row[2:]]
    return row


def merge_continued_tables(
    markdown: str,
    tables: Sequence[T],
    *,
    max_tokens: int = TABLE_MERGE_MAX_TOKENS,
) -> tuple[str, list[T], dict[str, Any]]:
    """合并跨页续表，重写正文 ``[表格 N]`` 占位并重编号。

    ``tables`` 元素需具备 ``body/caption/page/table_id``（``ParsedTable`` / ``TableInput``）。
    返回 ``(markdown, tables, stats)``；无可合并时原样返回。
    """
    items, repaired = _repair_table_list(list(tables or []))
    if len(items) < 2:
        stats = {"tables_repaired": repaired} if repaired else {}
        return markdown or "", items, stats

    order = _table_order(markdown or "", items)
    absorbed: set[int] = set()
    #: 合并链的“末页”（表对象只保留起始 page，链式跨多页时用此判断邻页）
    page_end: dict[int, int | None] = {
        i: getattr(t, "page", None) for i, t in enumerate(items)
    }
    merged_pairs = 0
    skipped_too_long = 0

    i = 0
    while i < len(order) - 1:
        a_idx = order[i]
        b_idx = order[i + 1]
        if a_idx in absorbed or b_idx in absorbed:
            i += 1
            continue
        a, b = items[a_idx], items[b_idx]
        ignore_ids = _absorbed_ids(items, absorbed)
        if not _looks_like_continuation(
            a,
            b,
            markdown or "",
            left_page_end=page_end.get(a_idx),
            ignore_placeholder_ids=ignore_ids,
        ):
            i += 1
            continue
        combined_rows, caption = _merge_rows(a, b)
        if not combined_rows:
            i += 1
            continue
        est = _estimate_tokens(caption, combined_rows)
        if est > max_tokens:
            skipped_too_long += 1
            log.info(
                "table_merge_skipped_too_long",
                left=getattr(a, "table_id", ""),
                right=getattr(b, "table_id", ""),
                tokens=est,
                limit=max_tokens,
            )
            i += 1
            continue
        items[a_idx] = _replace_table(
            a,
            body=_rows_to_html(combined_rows),
            caption=caption,
            page=getattr(a, "page", None),
        )
        right_page = getattr(b, "page", None)
        if right_page is not None:
            page_end[a_idx] = right_page
        absorbed.add(b_idx)
        merged_pairs += 1
        # 链式：a 再与下一张试合并
        order = [idx for idx in order if idx not in absorbed]
        # 停在当前位置，继续用合并后的 a 试下一张
        try:
            i = order.index(a_idx)
        except ValueError:
            break

    if not merged_pairs and not skipped_too_long:
        stats = {"tables_repaired": repaired} if repaired else {}
        return markdown or "", items, stats

    kept = [items[idx] for idx in _table_order(markdown or "", items) if idx not in absorbed]
    # 若 markdown 无占位，按原序去掉 absorbed
    if not kept:
        kept = [t for i, t in enumerate(items) if i not in absorbed]

    renumbered: list[T] = []
    for seq, table in enumerate(kept, start=1):
        renumbered.append(_replace_table(table, table_id=str(seq)))

    new_md = _rewrite_placeholders(markdown or "", absorbed_ids=_absorbed_ids(items, absorbed), kept=renumbered)
    stats = {
        "tables_merged_pairs": merged_pairs,
        "tables_after_merge": len(renumbered),
    }
    if skipped_too_long:
        stats["tables_merge_skipped_too_long"] = skipped_too_long
    if repaired:
        stats["tables_repaired"] = repaired
    if merged_pairs:
        log.info("tables_merged", **stats)
    return new_md, renumbered, stats


def _absorbed_ids(items: Sequence[Any], absorbed: set[int]) -> set[str]:
    out: set[str] = set()
    for idx in absorbed:
        tid = str(getattr(items[idx], "table_id", "") or "").strip()
        if tid:
            out.add(tid)
        out.add(str(idx + 1))  # 常见为按序 1..n
    return out


def _table_order(markdown: str, tables: Sequence[Any]) -> list[int]:
    """按正文占位出现顺序排列 table 下标；无占位则按 seq。"""
    id_to_idx: dict[str, int] = {}
    for i, t in enumerate(tables):
        tid = str(getattr(t, "table_id", "") or "").strip()
        if tid:
            id_to_idx[tid] = i
        id_to_idx.setdefault(str(i + 1), i)

    seen: list[int] = []
    used: set[int] = set()
    for match in _PLACEHOLDER.finditer(markdown or ""):
        tid = match.group(1)
        idx = id_to_idx.get(tid)
        if idx is not None and idx not in used:
            seen.append(idx)
            used.add(idx)
    for i in range(len(tables)):
        if i not in used:
            seen.append(i)
    return seen


def _looks_like_continuation(
    left: Any,
    right: Any,
    markdown: str,
    *,
    left_page_end: int | None = None,
    ignore_placeholder_ids: set[str] | None = None,
) -> bool:
    left_page = left_page_end if left_page_end is not None else getattr(left, "page", None)
    if not _pages_compatible(left_page, getattr(right, "page", None)):
        return False
    if not _placeholders_adjacent(
        markdown, left, right, ignore_ids=ignore_placeholder_ids
    ):
        return False

    aligned = _align_columns(
        coerce_rows(getattr(left, "body", "") or ""),
        coerce_rows(getattr(right, "body", "") or ""),
    )
    if aligned is None:
        return False
    left_rows, right_rows = aligned

    right_cap = str(getattr(right, "caption", "") or "")
    if _CONTINUED_CAPTION.search(right_cap):
        return True

    left_header = _norm_row(left_rows[0])
    right_header = _norm_row(right_rows[0])
    if left_header and left_header == right_header:
        return True
    # 次页没有重复表头，第一行已是数据（金额/序号），列结构又相同 → 同一张表
    if _is_data_row(right_rows[0]):
        return True
    # 表头单独落在上一页（工作内容|频次|备注），下一页直接是数据行
    if _is_header_only(left_rows) and _row_has_body_cell(right_rows[0]):
        return True
    return False


def _pages_compatible(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return True
    return b == a or b == a + 1


def _placeholders_adjacent(
    markdown: str,
    left: Any,
    right: Any,
    *,
    ignore_ids: set[str] | None = None,
) -> bool:
    """两张表占位之间不应夹大段正文（允许空行；已吸收的续表占位忽略）。"""
    left_id = str(getattr(left, "table_id", "") or "").strip()
    right_id = str(getattr(right, "table_id", "") or "").strip()
    if not left_id or not right_id:
        return True
    pattern = re.compile(
        rf"\[表格\s+{re.escape(left_id)}\][^\n]*\n(.*?)\[表格\s+{re.escape(right_id)}\]",
        re.DOTALL,
    )
    match = pattern.search(markdown or "")
    if not match:
        # 占位缺失时不因正文卡死合并（靠表头/续表判定）
        return True
    gap = match.group(1)
    for tid in ignore_ids or ():
        gap = re.sub(rf"\[表格\s+{re.escape(tid)}\][^\n]*\n?", "", gap)
    return _gap_is_page_furniture(gap)


def _gap_is_page_furniture(gap: str) -> bool:
    """占位之间只允许页码和短页眉；出现另一段说明则不是续表。"""
    gap = (gap or "").strip()
    if not gap:
        return True
    if re.search(r"\[表格\s+\d+\]|^#{1,6}\s|[。！？]", gap, re.MULTILINE):
        return False
    lines = [ln.strip() for ln in gap.splitlines() if ln.strip()]
    for line in lines:
        if _is_furniture_line(line):
            continue
        return False
    return True


def _is_furniture_line(line: str) -> bool:
    if re.fullmatch(r"第?\s*\d+\s*页?", line):
        return True
    if re.fullmatch(r"[-—]?\s*\d+\s*[-—]?", line):
        return True
    if re.fullmatch(r"page\s*\d+", line, re.IGNORECASE):
        return True
    # 短页眉：无句号，且不像一整段说明
    return len(line) <= 20


def _align_columns(
    left_rows: Sequence[Sequence[str]],
    right_rows: Sequence[Sequence[str]],
) -> tuple[list[list[str]], list[list[str]]] | None:
    """列数相同，或续表少 1～2 列（空列/rowspan 被 OCR 吃掉）时补齐。"""
    left = [list(row) for row in left_rows]
    right = [list(row) for row in right_rows]
    if not left or not right:
        return None
    left_cols = max(len(row) for row in left)
    right_cols = max(len(row) for row in right)
    if left_cols < 2 or right_cols < 2:
        return None
    if left_cols != right_cols:
        wider = max(left_cols, right_cols)
        if abs(left_cols - right_cols) > 2 or min(left_cols, right_cols) / wider < 0.7:
            return None
        left = [row + [""] * (wider - len(row)) for row in left]
        right = [row + [""] * (wider - len(row)) for row in right]
    return left, right


def _merge_rows(left: Any, right: Any) -> tuple[list[list[str]], str]:
    aligned = _align_columns(
        coerce_rows(getattr(left, "body", "") or ""),
        coerce_rows(getattr(right, "body", "") or ""),
    )
    if aligned is None:
        return [], str(getattr(left, "caption", "") or "")
    left_rows, right_rows = aligned
    if not left_rows:
        return [], str(getattr(left, "caption", "") or "")

    left_header = _norm_row(left_rows[0])
    data_right = list(right_rows)
    if data_right and _norm_row(data_right[0]) == left_header:
        data_right = data_right[1:]

    left_cap = str(getattr(left, "caption", "") or "").strip()
    right_cap = str(getattr(right, "caption", "") or "").strip()
    if left_cap:
        caption = left_cap
    elif right_cap and not _CONTINUED_CAPTION.search(right_cap):
        caption = right_cap
    else:
        caption = left_cap or re.sub(r"^\s*续表\s*[:：-]?\s*", "", right_cap).strip()

    return left_rows + data_right, caption


def _estimate_tokens(caption: str, rows: Sequence[Sequence[str]]) -> int:
    # 粗估：序列化后的体量；避免为判定再跑完整 serialize
    bits = [caption or ""]
    for row in rows:
        bits.append(" | ".join(str(c) for c in row))
    return count_tokens("\n".join(bits))


def _col_count(rows: Sequence[Sequence[str]]) -> int:
    trimmed = _trim_rows(rows)
    if not trimmed:
        return 0
    return max(len(r) for r in trimmed)


def _trim_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """去掉每一行尾部全空的列，避免 colspan 补位把列数撑偏。"""
    padded = [list(row) for row in rows]
    if not padded:
        return []
    width = max(len(row) for row in padded)
    for row in padded:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
    while width > 0 and all(not str(row[width - 1]).strip() for row in padded):
        width -= 1
        for row in padded:
            del row[-1]
    return padded


_VALUE_CELL = re.compile(r"^[￥¥]?\s*\d[\d,.]*%?(?:元)?$")


def _repair_table_list(items: list[T]) -> tuple[list[T], int]:
    repaired = 0
    out: list[T] = []
    for table in items:
        new_body, changed = repair_table_html(getattr(table, "body", "") or "")
        if changed:
            out.append(_replace_table(table, body=new_body))
            repaired += 1
        else:
            out.append(table)
    return out, repaired


def _is_header_only(rows: Sequence[Sequence[str]]) -> bool:
    """只有短列名、没有金额/长正文。跨页时这种表是下一页的表头。"""
    if len(rows) != 1:
        return False
    if any(_is_data_row(row) for row in rows):
        return False
    cells = [str(cell).strip() for row in rows for cell in row if str(cell).strip()]
    if not cells or len(cells) > 6:
        return False
    return all(len(cell) <= 8 for cell in cells)


def _row_has_body_cell(row: Sequence[str]) -> bool:
    return any(len(str(cell).strip()) > 8 for cell in row)


def _is_data_row(row: Sequence[str]) -> bool:
    """含金额/序号的行视为数据。纯列名（含「2024年」这类）不算。"""
    cells = [str(cell).strip() for cell in row if str(cell).strip()]
    if not cells:
        return False
    if any(_VALUE_CELL.fullmatch(cell) for cell in cells):
        return True
    return False


def _norm_row(row: Sequence[str]) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", "", str(c) or "") for c in row)


def _rows_to_html(rows: Sequence[Sequence[str]]) -> str:
    parts = ["<table>"]
    for i, row in enumerate(rows):
        parts.append("<tr>")
        tag = "th" if i == 0 else "td"
        for cell in row:
            parts.append(f"<{tag}>{_xml_escape(str(cell))}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _xml_escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _replace_table(table: T, **fields: Any) -> T:
    try:
        return replace(table, **fields)  # type: ignore[arg-type]
    except TypeError:
        # 非 dataclass 时退回原地改（测试用 SimpleNamespace）
        for key, value in fields.items():
            setattr(table, key, value)
        return table


def _rewrite_placeholders(
    markdown: str,
    *,
    absorbed_ids: set[str],
    kept: Sequence[Any],
) -> str:
    """删掉被吸收的 ``[表格 N]`` 行，剩余按 kept 顺序重编号为 1..n。"""
    if not markdown:
        return markdown

    kept_caps = [
        str(getattr(t, "caption", "") or "").strip() for t in kept
    ]
    next_seq = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal next_seq
        tid = match.group(1)
        rest = match.group(2) or ""
        if tid in absorbed_ids:
            return ""  # 整行占位去掉（续表不再单独占位）
        if next_seq >= len(kept):
            return match.group(0)
        next_seq += 1
        cap = kept_caps[next_seq - 1]
        # 保留原行尾题注文字；若有标准 caption 则用 caption
        suffix = f" {cap}" if cap and not rest.strip() else rest
        if cap and rest.strip() and _CONTINUED_CAPTION.search(rest):
            suffix = f" {cap}"
        return f"[表格 {next_seq}]{suffix}"

    rewritten = _PLACEHOLDER.sub(repl, markdown)
    # 去掉因删除占位产生的多余空行
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten)
    return rewritten
