"""docx 合并单元格（gridSpan）不能按列重复展开。

回归：《客户满意度调查表(1)》里 4 列合并的「申请装修内容…」「服务评价…」在表格
序列化里出现 4 份 —— python-docx 的 ``row.cells`` 会把同一个 ``_Cell`` 按网格列
重复返回；只软删/去重文本又会误伤"不同格子恰好同值"（并排的 ☑/□）。
"""

from __future__ import annotations

from ekbs.ingest.parser import _parse_docx


def test_merged_cell_text_appears_once(tmp_path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=4)
    merged = table.cell(0, 0).merge(table.cell(0, 3))
    merged.text = "申请装修内容（对下选项进行√选）"
    for index, value in enumerate(["一装", "二装", "店招", "其他"]):
        table.cell(1, index).text = value
    path = tmp_path / "merged.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert parsed.tables
    body = parsed.tables[0].body
    assert body.count("申请装修内容") == 1, body
    # 去重后按网格宽度补空：整行合并 = 1 格有字 + 3 格空（序列化能识别成标题行）
    assert "| 申请装修内容（对下选项进行√选） |  |  |  |" in body, body
    assert "一装" in body and "其他" in body


def test_distinct_cells_with_same_text_are_kept(tmp_path) -> None:
    """不同格子的同值（并排的 ☑/□）不能被当成合并单元格去掉。"""
    import docx

    document = docx.Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "☑"
    table.cell(0, 1).text = "☑"
    path = tmp_path / "dup.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert parsed.tables
    assert parsed.tables[0].body.count("☑") == 2, parsed.tables[0].body
