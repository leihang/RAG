from __future__ import annotations

import pytest

from excel_rag_parser import ParseOptions
from excel_rag_parser.exceptions import CorruptFileError, MissingDependencyError, UnsupportedFormatError
from excel_rag_parser.readers import detect_format, read_workbook


def test_detect_format() -> None:
    assert detect_format("a.xlsx") == "xlsx"
    assert detect_format("a.XLSM") == "xlsx"
    assert detect_format("a.xls") == "xls"
    assert detect_format("a.xlsb") == "xlsb"
    assert detect_format("a.csv") == "csv"
    assert detect_format("a.tsv") == "csv"
    with pytest.raises(UnsupportedFormatError):
        detect_format("a.txt")


def test_read_xlsx_keeps_merges(complex_xlsx) -> None:
    grids = read_workbook(complex_xlsx, ParseOptions())
    assert [g.name for g in grids] == ["销售", "财务", "多段"]
    sales = grids[0]
    assert any(m.min_row == 2 and m.min_col == 0 for m in sales.merges)  # A3:A4


def test_read_csv(sample_csv) -> None:
    grids = read_workbook(sample_csv, ParseOptions())
    assert len(grids) == 1
    assert grids[0].rows[0] == ["名称", "数量", "单价"]


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        read_workbook("/definitely/not/here.xlsx", ParseOptions())


def test_xlsb_dependency_or_corrupt(tmp_path) -> None:
    path = tmp_path / "x.xlsb"
    path.write_bytes(b"not a real xlsb")
    try:
        import pyxlsb  # noqa: F401
    except ImportError:
        with pytest.raises(MissingDependencyError):
            read_workbook(path, ParseOptions())
    else:
        with pytest.raises(CorruptFileError):
            read_workbook(path, ParseOptions())
