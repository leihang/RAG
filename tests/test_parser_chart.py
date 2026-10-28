"""`.xlsx` 图表解析的边界：标题清洗 + 无 series/无缓存时的安全跳过。

图表只有在带缓存值（``numCache``）时才能还原成数值表；openpyxl 生成的图表默认只写
公式引用、不带缓存，所以这里锁住「不崩、不乱产表」的边界。真实带缓存图表的还原
留给后续真实语料用例。
"""

from __future__ import annotations

from ekbs.ingest.parser import _chart_title, _parse_xlsx


class _Chart:
    def __init__(self, title: object) -> None:
        self.title = title


def test_chart_title_junk() -> None:
    """openpyxl 的 Title 占位串/空标题要清空，别把 `Title()` 当表名。"""
    assert _chart_title(_Chart(None)) == ""
    assert _chart_title(_Chart("Title()")) == ""
    assert _chart_title(_Chart("<openpyxl.chart.title.Title object>")) == ""
    assert _chart_title(_Chart("月度销量")) == "月度销量"


def _write_chart_xlsx(path, *, with_data: bool) -> None:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.title = "销量"
    ws.append(["月份", "销量"])
    for month, value in (("1月", 10), ("2月", 20), ("3月", 30)):
        ws.append([month, value])
    chart = BarChart()
    chart.title = "月度销量"
    if with_data:
        data = Reference(ws, min_col=2, min_row=1, max_row=4)
        cats = Reference(ws, min_col=1, min_row=2, max_row=4)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
    ws.add_chart(chart, "E2")
    wb.save(str(path))


def test_xlsx_chart_no_series_is_skipped(tmp_path) -> None:
    """没有 series 的图表不产表、不抛异常。"""
    path = tmp_path / "empty_chart.xlsx"
    _write_chart_xlsx(path, with_data=False)
    doc = _parse_xlsx(path)
    assert all("月度销量" not in (table.caption or "") for table in doc.tables)


def test_xlsx_chart_without_cache_is_safe(tmp_path) -> None:
    """openpyxl 图表只有公式引用、没有缓存值 → 跳过图表，但工作表表格仍在。"""
    path = tmp_path / "chart.xlsx"
    _write_chart_xlsx(path, with_data=True)
    doc = _parse_xlsx(path)
    assert all("月度销量" not in (table.caption or "") for table in doc.tables)
    assert any("月份" in table.body for table in doc.tables), "普通工作表表格不能被图表逻辑吞掉"
