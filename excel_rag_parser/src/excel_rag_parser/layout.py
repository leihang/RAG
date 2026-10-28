"""布局分析：剥离说明文字、识别表头、多段表切分、列折叠、数值表判定。

所有判定都是启发式（Excel 没有结构化元信息），阈值集中在 :mod:`options`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .grid import (
    MergeRange,
    SheetGrid,
    display,
    fill_merged,
    is_blank,
    is_numeric,
    row_has_number,
    row_is_blank,
    row_is_label,
    row_is_note_like,
    row_is_strong_note,
    row_values,
)
from .options import ParseOptions

#: 顶部前 N 行的稀疏行视为标题/副标题（真实表常把标题裸写在 A1）
TOP_TITLE_ZONE = 3
#: 表头最多识别 8 行，超过视为异常
MAX_HEADER_ROWS = 8
#: 至少 2 个数值列才按「数值表」处理
NUMERIC_MIN_COLUMNS = 2
#: 「分析/对比」列信号词：命中即视为分析表，走 Markdown
ANALYSIS_HINTS = ("同比", "环比", "分析", "对比", "差异", "增减")
#: 数值/财务信号词（边界表的二次确认）
NUMERIC_HINTS = (
    "金额", "销售额", "利润", "成本", "占比", "比例", "单价", "数量", "预算",
    "费用", "收入", "支出", "万元", "亿元", "元", "率", "百分比", "合计",
)
_NOTE_PREFIXES = ("注", "备注", "说明", "注意", "附", "Note", "NOTE", "以上", "其中", "单位")


@dataclass
class Section:
    """一个独立表段。行号均为 **0-based**。"""

    header_start: int
    header_rows: int
    data_start: int
    data_end: int
    title: str = ""


@dataclass
class SheetLayout:
    """一个 Sheet 的布局分析结果。"""

    grid: SheetGrid                    # 合并区已填充
    start: int                         # 表体首行（跳过顶部说明后）
    end: int                           # 表体末行（exclusive，跳过底部说明后）
    leading: list[list[Any]] = field(default_factory=list)
    trailing: list[list[Any]] = field(default_factory=list)
    title: str = ""
    title_index: int | None = None
    sections: list[Section] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 说明/标题行判定
# ---------------------------------------------------------------------------

def _merge_span(merges: Sequence[MergeRange], r: int, c: int) -> int:
    for mg in merges:
        if mg.min_row == r and mg.min_col == c:
            return mg.max_col - mg.min_col + 1
    return 1


def _looks_like_note(value: Any) -> bool:
    text = str(value).strip()
    if not text:
        return True
    if len(text) > 40:
        return True
    if any(text.startswith(p) for p in _NOTE_PREFIXES):
        return True
    return any(sep in text for sep in ("。", "；", "\n"))


def is_note_row(grid: SheetGrid, r: int) -> bool:
    """说明/标题行判定（用未填充的 grid，避免把跨全宽标题填“密”）。"""
    row = grid.rows[r]
    values = [c for c, v in enumerate(row) if not is_blank(v)]
    if not values:
        return True  # 空行
    if r < TOP_TITLE_ZONE and len(values) <= 1:
        return True  # 顶部稀疏行 = 标题/副标题（即便没合并）
    if len(values) > 1:
        return False  # 多个值 → 表头或数据
    c = values[0]
    span = _merge_span(grid.merges, r, c)
    if span >= grid.width:
        return True   # 跨全宽合并 → 标题/说明
    if span > 1:
        return False  # 跨部分列的合并 → 表头层级
    return _looks_like_note(row[c])


# ---------------------------------------------------------------------------
# 表头深度 / 多段切分
# ---------------------------------------------------------------------------

def header_depth(grid: SheetGrid, h: int, limit: int) -> int:
    """从 h 起连续的表头行数（多级表头可能占多行），至少 1。"""
    depth = 0
    while h + depth < limit and row_is_label(grid.rows[h + depth]):
        depth += 1
    return max(1, depth)


def detect_first_header_depth(grid: SheetGrid, start: int, end: int,
                              options: ParseOptions) -> int:
    if options.header_rows is not None:
        return max(1, int(options.header_rows))
    first_numeric = next(
        (r for r in range(start, end)
         if any(is_numeric(v) for v in grid.rows[r] if not is_blank(v))),
        None,
    )
    if first_numeric is None:
        return 1
    return max(1, min(first_numeric - start, MAX_HEADER_ROWS))


def data_region_end(grid: SheetGrid, data_start: int, limit: int) -> int:
    """从 data_start 起连续数据行到哪里结束。

    数值表：遇到「强备注行」（无数字+长句）或空行即止，类别短标签行继续留在表内；
    文本表：遇到空行或新表头即止。
    """
    if data_start >= limit:
        return data_start
    numeric = row_has_number(grid.rows[data_start])
    r = data_start
    while r < limit:
        row = grid.rows[r]
        if row_is_blank(row):
            break
        if numeric:
            if row_is_strong_note(row):
                break
        elif row_is_label(row):
            break
        r += 1
    return r


def split_sections(grid: SheetGrid, start: int, end: int,
                   first_header_rows: int) -> list[Section]:
    """把 Sheet 切成若干独立表段（表段 = 表头 + 数据）。"""
    header_starts: list[int] = [start]
    for r in range(start + first_header_rows, end):
        if not row_is_label(grid.rows[r]):
            continue
        prev = grid.rows[r - 1] if r - 1 >= 0 else None
        if prev is None or row_is_blank(prev) or row_is_note_like(prev):
            header_starts.append(r)
    header_starts = sorted(set(header_starts))

    sections: list[Section] = []
    for i, h in enumerate(header_starts):
        nxt = header_starts[i + 1] if i + 1 < len(header_starts) else end
        hrows = first_header_rows if i == 0 else header_depth(grid, h, nxt)
        dstart = h + hrows
        dend = data_region_end(grid, dstart, nxt)
        title = ""
        if h - 1 >= start and row_is_note_like(grid.rows[h - 1]):
            vals = row_values(grid.rows[h - 1])
            title = vals[0] if vals else ""
        sections.append(Section(h, hrows, dstart, dend, title))
    return sections


# ---------------------------------------------------------------------------
# 列处理
# ---------------------------------------------------------------------------

def keep_columns(header_block: Sequence[Sequence[Any]],
                 data_block: Sequence[Sequence[Any]], width: int) -> list[int]:
    """去掉全空列，返回保留列下标。"""
    keep: list[int] = []
    for c in range(width):
        if any(c < len(row) and not is_blank(row[c]) for row in header_block) or \
           any(c < len(row) and not is_blank(row[c]) for row in data_block):
            keep.append(c)
    return keep


def collapse_identical_columns(keep_cols: Sequence[int],
                               data_block: Sequence[Sequence[Any]],
                               merges: Sequence[MergeRange],
                               last_header_row: int) -> list[int]:
    """把「末级表头跨列合并、且数据逐行相同」的列折叠成一列。"""
    keep = list(keep_cols)
    if len(keep) <= 1:
        return keep

    def column(c: int) -> list[str]:
        return [display(row[c]) for row in data_block]

    def subsumes(c1: int, c2: int) -> bool:
        head = column(c1)
        target = column(c2)
        return all((not t.strip()) or t == v for v, t in zip(head, target))

    drop: set[int] = set()
    for mg in merges:
        if mg.min_row != last_header_row:
            continue
        if mg.max_col <= mg.min_col:
            continue
        for c in range(mg.min_col + 1, mg.max_col + 1):
            if subsumes(mg.min_col, c):
                drop.add(c)
    return [c for c in keep if c not in drop]


def _clean_header_label(text: str, limit: int = 40) -> str:
    label = " ".join(str(text).split())
    return label if len(label) <= limit else label[: limit - 1] + "…"


def flatten_header(header_block: Sequence[Sequence[Any]],
                   keep_cols: Sequence[int]) -> list[str]:
    """多级表头 → 平铺列名：纵向拼接、相邻重复去重、全局同名加序号。"""
    names: list[str] = []
    for c in keep_cols:
        parts: list[str] = []
        for row in header_block:
            label = _clean_header_label(display(row[c]) if c < len(row) else "")
            if label and (not parts or parts[-1] != label):
                parts.append(label)
        names.append("_".join(parts) if parts else f"列{c + 1}")
    seen: dict[str, int] = {}
    final: list[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 0
            final.append(name)
        else:
            seen[name] += 1
            final.append(f"{name}_{seen[name] + 1}")
    return final


# ---------------------------------------------------------------------------
# 数值表判定
# ---------------------------------------------------------------------------

def _is_prose(value: Any) -> bool:
    text = str(value).strip()
    if len(text) < 30:
        return False
    return any(sep in text for sep in ("。", "，", "；", "、")) or text.count("\n") >= 2


def is_numeric_table(rows: Sequence[Sequence[Any]], columns: Sequence[str],
                     options: ParseOptions) -> bool:
    """判定「纯数字/财务」表（→ JSON/SQL）。

    先排除分析表（同比/环比/分析/长句列），再看**列级**数值占比。
    """
    if not rows or not columns:
        return False
    header_text = "".join(str(c) for c in columns)
    if any(hint in header_text for hint in ANALYSIS_HINTS):
        return False

    numeric_cols = 0
    for c in range(len(columns)):
        values = [row[c] for row in rows if c < len(row) and not is_blank(row[c])]
        if not values:
            continue
        if sum(1 for v in values if _is_prose(v)) / len(values) >= 0.5:
            return False  # 长句分析列 → 分析表
        if sum(1 for v in values if is_numeric(v)) / len(values) >= 0.6:
            numeric_cols += 1

    ratio = numeric_cols / len(columns)
    if ratio < options.numeric_column_ratio or numeric_cols < NUMERIC_MIN_COLUMNS:
        return False
    if ratio >= max(options.numeric_column_ratio, 0.85):
        return True
    return any(hint in header_text for hint in NUMERIC_HINTS)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def analyze_sheet(grid: SheetGrid, options: ParseOptions) -> SheetLayout:
    """对一个 Sheet 做完整布局分析。"""
    if not grid.rows or all(is_blank(v) for row in grid.rows for v in row):
        return SheetLayout(grid=fill_merged(grid), start=0, end=0)

    n = len(grid.rows)
    start = 0
    while start < n and is_note_row(grid, start):
        start += 1
    end = n
    while end > start and is_note_row(grid, end - 1):
        end -= 1

    leading = grid.rows[:start]
    trailing = grid.rows[end:]

    title = grid.name
    title_index: int | None = None
    for r in range(start):
        values = [c for c, v in enumerate(grid.rows[r]) if not is_blank(v)]
        if len(values) != 1:
            continue
        c = values[0]
        label = str(grid.rows[r][c]).strip()
        span = _merge_span(grid.merges, r, c)
        if len(label) <= 40 and (span >= grid.width or r < TOP_TITLE_ZONE):
            title = label[:60]
            title_index = r
            break

    filled = fill_merged(grid)
    if start >= end:
        return SheetLayout(
            grid=filled, start=start, end=end, leading=leading, trailing=trailing,
            title=title, title_index=title_index, sections=[],
        )

    first_header_rows = detect_first_header_depth(filled, start, end, options)
    sections = split_sections(filled, start, end, first_header_rows)
    return SheetLayout(
        grid=filled, start=start, end=end, leading=leading, trailing=trailing,
        title=title, title_index=title_index, sections=sections,
    )
