"""旧版 `.xls` 解析（xlrd）—— 此前 `_parse_xls` 整个函数零调用。

`.xls` 若走纯文本分支会把二进制当 UTF-8 读成乱码（实测 24KB 的 .doc 读出 2.4 万字符乱码）。
本文件用 `xlwt` 现场生成 `.xls`，锁住：日期/整数归一化、合并表头继承、空表跳过、
以及缺 `xlrd` 时的明确报错。
"""

from __future__ import annotations

import datetime
import sys

import pytest

from ekbs.ingest.parser import ParserUnavailable, _parse_xls


def test_parse_xls_basic(tmp_path) -> None:
    """日期单元格转可读日期；xlrd 的 float 整数归一化；空 sheet 也能过。"""
    import xlwt

    path = tmp_path / "basic.xls"
    wb = xlwt.Workbook()
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    ws = wb.add_sheet("明细")
    ws.write(0, 0, "日期")
    ws.write(0, 1, "数量")
    ws.write(1, 0, datetime.date(2026, 9, 30), date_style)
    ws.write(1, 1, 3)  # xlrd 读成 3.0，必须归一化成 "3"
    wb.add_sheet("空表")
    wb.save(str(path))

    doc = _parse_xls(path)
    assert doc.engine == "xls"
    assert doc.tables, "应至少产出一张表"
    body = "\n".join(table.body for table in doc.tables)
    assert "2026-09-30" in body, "日期单元格要转成可读日期"
    assert "3.0" not in body, "xlrd 的 float 整数要归一化成 3"
    assert "## 明细" in doc.markdown
    assert "## 空表" in doc.markdown


def test_parse_xls_merged_header(tmp_path) -> None:
    """纵向/横向合并的表头要被继承填充（否则列名只剩左上角一格）。"""
    import xlwt

    path = tmp_path / "merged.xls"
    wb = xlwt.Workbook()
    ws = wb.add_sheet("编制")
    ws.write_merge(0, 0, 0, 2, "2025年部门编制")  # 标题行
    ws.write_merge(1, 2, 0, 0, "部门")            # 纵向合并：部门跨两行
    ws.write_merge(1, 1, 1, 2, "编制")            # 横向合并：编制跨两列
    ws.write(2, 1, "人数")
    ws.write(2, 2, "号段")
    ws.write(3, 0, "研发部")
    ws.write(3, 1, 45)
    ws.write(3, 2, "R-100")
    wb.save(str(path))

    doc = _parse_xls(path)
    assert doc.tables
    body = "\n".join(table.body for table in doc.tables)
    assert "研发部" in body
    assert "人数" in body and "号段" in body


def test_parse_xls_empty_sheet_only(tmp_path) -> None:
    import xlwt

    path = tmp_path / "empty.xls"
    wb = xlwt.Workbook()
    wb.add_sheet("空表")
    wb.save(str(path))

    doc = _parse_xls(path)
    assert doc.engine == "xls"
    assert doc.tables == []
    assert "## 空表" in doc.markdown


def test_parse_xls_without_xlrd(monkeypatch, tmp_path) -> None:
    """xlrd 缺失时必须明确报「装 xlrd 或另存 xlsx」，不能静默降级成文本。"""
    path = tmp_path / "x.xls"
    path.write_bytes(b"")
    monkeypatch.setitem(sys.modules, "xlrd", None)
    with pytest.raises(ParserUnavailable):
        _parse_xls(path)
