"""多格式读入层：把 ``.xlsx/.xlsm`` / ``.xls`` / ``.xlsb`` / ``.csv`` 读成统一 :class:`SheetGrid`。

原则：
- 读入层只负责「拿到值 + 合并区 + 把日期/错误值归一」，不做表头/切段等布局判断；
- 各格式的库差异在这里被吃掉，下游全部与格式无关；
- 可选依赖缺失时抛 :class:`MissingDependencyError`（带安装提示），不静默降级。
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from pathlib import Path
from typing import Any

from .exceptions import CorruptFileError, MissingDependencyError, UnsupportedFormatError
from .grid import (
    EXCEL_ERRORS,
    MergeRange,
    SheetGrid,
    is_date_format,
    looks_like_date_serial,
    to_date,
)
from .log import get_logger
from .options import ParseOptions

log = get_logger("readers")

XLSX_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
XLS_SUFFIXES = {".xls"}
XLSB_SUFFIXES = {".xlsb"}
CSV_SUFFIXES = {".csv", ".tsv"}
SUPPORTED_SUFFIXES = sorted(XLSX_SUFFIXES | XLS_SUFFIXES | XLSB_SUFFIXES | CSV_SUFFIXES)


def detect_format(path: str | Path) -> str:
    """按后缀判定格式；不支持时抛 :class:`UnsupportedFormatError`。"""
    suffix = Path(path).suffix.lower()
    if suffix in XLSX_SUFFIXES:
        return "xlsx"
    if suffix in XLS_SUFFIXES:
        return "xls"
    if suffix in XLSB_SUFFIXES:
        return "xlsb"
    if suffix in CSV_SUFFIXES:
        return "csv"
    raise UnsupportedFormatError(str(path), suffix, SUPPORTED_SUFFIXES)


def read_workbook(path: str | Path, options: ParseOptions) -> list[SheetGrid]:
    """读取工作簿，返回各 Sheet 的规整网格。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    fmt = detect_format(p)
    log.debug("reading_workbook path=%s format=%s", p, fmt)
    if fmt == "xlsx":
        return _read_xlsx(p, options)
    if fmt == "xls":
        return _read_xls(p, options)
    if fmt == "xlsb":
        return _read_xlsb(p, options)
    return _read_csv(p, options)


# ---------------------------------------------------------------------------
# 值归一化（各格式共用）
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str | None:
    text = str(value).strip()
    if not text or text in EXCEL_ERRORS:
        return None
    return text


def _maybe_convert_date(value: Any, fmt: str, options: ParseOptions) -> Any:
    """数值单元格：日期格式或 General 日期序列号 → date/datetime。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, float) and value != value:  # NaN
        return None
    if is_date_format(fmt) or (
        fmt in ("", "General", "general")
        and looks_like_date_serial(value, options.date_serial_min, options.date_serial_max)
    ):
        converted = to_date(value)
        if converted is not None:
            return converted
    return value


# ---------------------------------------------------------------------------
# .xlsx / .xlsm（openpyxl）
# ---------------------------------------------------------------------------

def _read_xlsx(path: Path, options: ParseOptions) -> list[SheetGrid]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 必装依赖
        raise MissingDependencyError("xlsx", "openpyxl", "core") from exc
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True)
    except Exception as exc:  # noqa: BLE001 - 库异常类型不稳定
        raise CorruptFileError(str(path), "xlsx", str(exc)[:240]) from exc

    grids: list[SheetGrid] = []
    try:
        for ws in wb.worksheets:
            rows = [
                [_xlsx_cell_value(cell, options) for cell in row]
                for row in ws.iter_rows()
            ]
            merges = [
                MergeRange(rng.min_row - 1, rng.min_col - 1, rng.max_row - 1, rng.max_col - 1)
                for rng in ws.merged_cells.ranges
            ]
            grids.append(SheetGrid(ws.title, rows, merges))
    finally:
        wb.close()
    return grids


def _xlsx_cell_value(cell: Any, options: ParseOptions) -> Any:
    value = cell.value
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value
    if isinstance(value, (int, float)):
        try:
            fmt = cell.number_format or ""
        except AttributeError:  # pragma: no cover - 防御性
            fmt = ""
        return _maybe_convert_date(value, fmt, options)
    return _clean_text(value)


# ---------------------------------------------------------------------------
# .xls（xlrd）
# ---------------------------------------------------------------------------

def _read_xls(path: Path, options: ParseOptions) -> list[SheetGrid]:
    try:
        import xlrd
    except ImportError as exc:
        raise MissingDependencyError("xls", "xlrd", "xls") from exc
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as exc:  # noqa: BLE001
        raise CorruptFileError(str(path), "xls", str(exc)[:240]) from exc

    grids: list[SheetGrid] = []
    for idx in range(book.nsheets):
        sheet = book.sheet_by_index(idx)
        rows = [
            [_xls_cell_value(book, xlrd, sheet.cell(r, c), options) for c in range(sheet.ncols)]
            for r in range(sheet.nrows)
        ]
        merges = [
            MergeRange(rlo, clo, rhi - 1, chi - 1)
            for (rlo, rhi, clo, chi) in sheet.merged_cells
        ]
        grids.append(SheetGrid(sheet.name, rows, merges))
    return grids


def _xls_format(book: Any, xlrd: Any, cell: Any) -> str:
    try:
        xf = book.xf_list[cell.xf_index]
        fmt = book.format_map[xf.format_key]
        return fmt.format_str or ""
    except Exception:  # noqa: BLE001 - 格式信息缺失不致命
        return ""


def _xls_cell_value(book: Any, xlrd: Any, cell: Any, options: ParseOptions) -> Any:
    ctype = cell.ctype
    if ctype == xlrd.XL_CELL_EMPTY:
        return None
    if ctype == xlrd.XL_CELL_TEXT:
        return _clean_text(cell.value)
    if ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
        except Exception:  # noqa: BLE001 - 越界日期退回原值
            return cell.value
    if ctype == xlrd.XL_CELL_ERROR:
        return None
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if ctype == xlrd.XL_CELL_NUMBER:
        return _maybe_convert_date(cell.value, _xls_format(book, xlrd, cell), options)
    return _clean_text(cell.value)


# ---------------------------------------------------------------------------
# .xlsb（pyxlsb）
# ---------------------------------------------------------------------------

def _read_xlsb(path: Path, options: ParseOptions) -> list[SheetGrid]:
    try:
        from pyxlsb import open_workbook as _open
    except ImportError as exc:
        raise MissingDependencyError("xlsb", "pyxlsb", "xlsb") from exc

    grids: list[SheetGrid] = []
    try:
        with _open(str(path)) as wb:
            for name in wb.sheets:
                sheet = wb.get_sheet(name)
                rows = [
                    [_xlsb_cell_value(cell, options) for cell in row]
                    for row in sheet.rows()
                ]
                # pyxlsb 不公开合并区信息 → 无法填充（见 README「已知限制」）
                grids.append(SheetGrid(name, rows, []))
    except Exception as exc:  # noqa: BLE001
        raise CorruptFileError(str(path), "xlsb", str(exc)[:240]) from exc
    return grids


def _xlsb_cell_value(cell: Any, options: ParseOptions) -> Any:
    if cell is None:
        return None
    value = getattr(cell, "v", None)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (int, float)):
        fmt = getattr(cell, "f", "") or ""
        return _maybe_convert_date(value, fmt, options)
    return _clean_text(value)


# ---------------------------------------------------------------------------
# .csv / .tsv（stdlib）
# ---------------------------------------------------------------------------

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1")


def _read_csv(path: Path, options: ParseOptions) -> list[SheetGrid]:
    raw = path.read_bytes()
    text: str | None = None
    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:  # pragma: no cover - latin-1 不会失败
        text = raw.decode("utf-8", errors="replace")

    delimiter = "\t" if path.suffix.lower() == ".tsv" else _sniff_delimiter(text)
    # CSV 无类型信息：全部按文本处理（不做数字推断，避免 0012345 丢前导零）
    rows = [[_clean_text(cell) for cell in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return [SheetGrid(path.stem, rows, [])]


def _sniff_delimiter(text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
        return dialect.delimiter or ","
    except csv.Error:
        return ","
