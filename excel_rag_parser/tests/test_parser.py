from __future__ import annotations

import pytest

from excel_rag_parser import ParseOptions, parse_excel
from excel_rag_parser.exceptions import EmptyWorkbookError, UnsupportedFormatError


def test_parse_complex_workbook(complex_xlsx) -> None:
    chunks = parse_excel(complex_xlsx)
    types = {c["metadata"]["chunk_type"] for c in chunks}
    assert types & {"markdown_table", "markdown_table_slice"}
    assert "numeric_records" in types
    assert "text" in types


def test_columns_are_flattened(complex_xlsx) -> None:
    chunks = parse_excel(complex_xlsx)
    sales = [
        c for c in chunks
        if c["metadata"]["sheet_name"] == "销售" and c["metadata"].get("columns")
    ]
    assert sales
    assert "2025年_销售额" in sales[0]["metadata"]["columns"]


def test_stacked_tables_split_into_sections(complex_xlsx) -> None:
    chunks = parse_excel(complex_xlsx)
    sections = {
        c["metadata"].get("section_index")
        for c in chunks
        if c["metadata"]["sheet_name"] == "多段" and c["metadata"].get("columns")
    }
    assert len(sections) >= 2


def test_chunk_id_is_deterministic(complex_xlsx) -> None:
    first = parse_excel(complex_xlsx)
    second = parse_excel(complex_xlsx)
    assert [c["chunk_id"] for c in first] == [c["chunk_id"] for c in second]


def test_schema_version_present(complex_xlsx) -> None:
    chunks = parse_excel(complex_xlsx)
    assert all(c["metadata"]["schema_version"] == "1.0" for c in chunks)
    assert all(c["metadata"]["source_file"] == "complex.xlsx" for c in chunks)


def test_empty_workbook_raises(empty_xlsx) -> None:
    with pytest.raises(EmptyWorkbookError):
        parse_excel(empty_xlsx)


def test_unsupported_format_raises(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        parse_excel(path)


def test_long_table_sliced_by_budget(tmp_path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "名称", "金额"])
    for i in range(100):
        ws.append([i, f"项目{i}", i * 1.5])
    path = tmp_path / "long.xlsx"
    wb.save(path)

    chunks = parse_excel(path, ParseOptions(max_chunk_chars=400, min_slice_rows=3))
    slices = [c for c in chunks if c["metadata"]["chunk_type"] == "markdown_table_slice"]
    assert len(slices) >= 2
    assert all(c["metadata"]["row_count"] >= 1 for c in slices)
    # 每片都带表头（Markdown 第二行是分隔行）
    assert all("| 编号 | 名称 | 金额 |" in c["chunk_text"] for c in slices)


def test_force_all_text_disables_numeric_isolation(complex_xlsx) -> None:
    chunks = parse_excel(complex_xlsx, ParseOptions(force_all_text=True))
    assert all(c["metadata"]["chunk_type"] != "numeric_records" for c in chunks)
