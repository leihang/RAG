"""网格模型与行分类：所有启发式共用的基础工具。

读入层负责把任意格式（xlsx/xls/xlsb/csv）规整成 :class:`SheetGrid`：
二维值网格 + 0-based 合并区 + 已归一化的日期/数字。之后所有逻辑都与格式无关。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Excel 错误值（读出来是字符串）→ 一律当空白，避免污染表格/JSON
EXCEL_ERRORS = frozenset({
    "#DIV/0!", "#N/A", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!",
    "#GETTING_DATA", "#SPILL!", "#CALC!", "#FIELD!",
})

#: 日期 number_format 判定（yy / mmm / dd / h: / s: 等占位符）
_DATE_FMT_TOKENS = ("yy", "mmmm", "mmm", "dd", "h:", "s:", "am/pm", "a/p")


@dataclass(frozen=True)
class MergeRange:
    """合并区（**0-based、闭区间**）。"""

    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass
class SheetGrid:
    """一个 Sheet 的规整网格。``rows[r][c]`` 为值（None=空）。"""

    name: str
    rows: list[list[Any]]
    merges: list[MergeRange] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


# ---------------------------------------------------------------------------
# 值归一化（读入层与渲染层共用）
# ---------------------------------------------------------------------------

def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return isinstance(value, str) and value.strip() == ""


def is_numeric(value: Any) -> bool:
    """判断单元格能否当数值（千分位/货币/百分比/括号负数）。"""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == value  # NaN 不算
    text = str(value).strip()
    if not text or text in EXCEL_ERRORS:
        return False
    cleaned = text
    for token in (",", "，", "¥", "￥", "$", "€", "£", "RMB", "CNY",
                  "%", "％", "(", ")", "（", "）", " "):
        cleaned = cleaned.replace(token, "")
    if not cleaned:
        return False
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


def is_date_format(fmt: str) -> bool:
    if not fmt:
        return False
    low = fmt.lower()
    return any(tok in low for tok in _DATE_FMT_TOKENS)


def looks_like_date_serial(value: Any, min_serial: int, max_serial: int) -> bool:
    """General 格式的整数落在合理日期区间 → 很可能是 Excel 日期序列号。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        if value != value or not value.is_integer():
            return False
        value = int(value)
    if not isinstance(value, int):
        return False
    return min_serial <= value <= max_serial


def to_date(value: Any) -> Any:
    """Excel 序列号 → date/datetime；越界返回 None。"""
    try:
        from openpyxl.utils.datetime import from_excel
    except Exception:  # pragma: no cover - 防御性：openpyxl 是必装依赖
        return None
    try:
        converted = from_excel(value)
    except Exception:
        return None
    if isinstance(converted, _dt.datetime):
        if converted.hour == converted.minute == converted.second == 0:
            return converted.date()
        return converted
    if isinstance(converted, (_dt.date, _dt.time)):
        return converted
    return None


def display(value: Any) -> str:
    """值 → 可读文本（Markdown / 说明文字用）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, _dt.datetime):
        if value.hour == value.minute == value.second == 0:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def jsonable(obj: Any) -> Any:
    """递归转 JSON 可序列化（NaN → None，date → ISO）。"""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, _dt.datetime):
        return obj.date().isoformat() if obj.hour == obj.minute == obj.second == 0 else obj.isoformat()
    if isinstance(obj, (_dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# 合并单元格填充
# ---------------------------------------------------------------------------

def fill_merged(grid: SheetGrid) -> SheetGrid:
    """把合并区锚点值回填到区内每格，返回**新** SheetGrid（不改原对象）。"""
    rows = [list(r) for r in grid.rows]
    for mg in grid.merges:
        if mg.min_row < 0 or mg.min_row >= len(rows):
            continue
        anchor = rows[mg.min_row][mg.min_col] if mg.min_col < len(rows[mg.min_row]) else None
        for r in range(mg.min_row, min(mg.max_row + 1, len(rows))):
            for c in range(mg.min_col, min(mg.max_col + 1, len(rows[r]))):
                rows[r][c] = anchor
    return SheetGrid(grid.name, rows, list(grid.merges))


# ---------------------------------------------------------------------------
# 行分类
# ---------------------------------------------------------------------------

def row_values(row: Sequence[Any]) -> list[str]:
    return [display(v).strip() for v in row if not is_blank(v)]


def row_is_blank(row: Sequence[Any]) -> bool:
    return not row_values(row)


def row_has_number(row: Sequence[Any]) -> bool:
    return any(is_numeric(v) for v in row if not is_blank(v))


def row_is_note_like(row: Sequence[Any]) -> bool:
    """单一逻辑值（标题行 / 跨多列合并的说明）。"""
    return len(set(row_values(row))) <= 1


def row_is_label(row: Sequence[Any]) -> bool:
    """像表头的一行：>=2 个短标签、无数字。"""
    values = row_values(row)
    if len(values) < 2 or row_has_number(row):
        return False
    return all(len(v) <= 30 for v in values)


def row_is_strong_note(row: Sequence[Any]) -> bool:
    """**无数字** 且含长句（>30 字）或句读符号 → 说明/备注。

    有数字的行永远是数据行（哪怕含「分析」长文本列）；类别短标签行很短，不是备注。
    """
    if row_has_number(row):
        return False
    for value in row_values(row):
        if len(value) > 30 or any(sep in value for sep in ("。", "；")):
            return True
    return False
