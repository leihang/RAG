"""Word 表格的**通用**处理口径：保留合并单元格结构（colspan / rowspan），不按列重复。

口径（对所有 docx 表一视同仁，不是给某份文件打补丁）：

* 水平合并 ``w:gridSpan`` → 目标格只出现一次 + ``colspan="N"``；
* 纵向合并 ``w:vMerge restart/continue`` → 起始格带 ``rowspan``，延续行不再重复该格；
* 不同格子恰好同值（并排的 ☑/□）→ 各自保留；
* 普通表（无合并）→ 每格一个 ``<td>``，与原来一致。

回归背景：docx 的 ``row.cells`` 会把合并单元格按网格列重复返回，摊平后"整行合并"
的行变成 2–4 份重复文本，复合页与原件版式不一致。
"""

from __future__ import annotations

from ekbs.ingest.parser import _parse_docx
from ekbs.ingest.table_serialize import parse_table_html


def test_horizontal_merge_uses_colspan_not_repeated_text(tmp_path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=4)
    table.cell(0, 0).merge(table.cell(0, 3)).text = "申请装修内容（对下选项进行√选）"
    for index, value in enumerate(["一装", "二装", "店招", "其他"]):
        table.cell(1, index).text = value
    path = tmp_path / "merged.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    body = parsed.tables[0].body
    assert 'colspan="4"' in body, body
    assert body.count("申请装修内容") == 1, body
    # 下游解析（检索口径）只取单元格文本：合并格仍只出现一次
    assert sum(row.count("申请装修内容（对下选项进行√选）") for row in parse_table_html(body)) == 1


def test_vertical_merge_uses_rowspan_and_skips_continuation(tmp_path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "类别"
    table.cell(0, 1).text = "内容"
    table.cell(1, 0).merge(table.cell(2, 0)).text = "纵向合并"
    table.cell(1, 1).text = "A"
    table.cell(2, 1).text = "B"
    path = tmp_path / "vmerge.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    body = parsed.tables[0].body
    assert 'rowspan="2"' in body, body
    assert body.count("纵向合并") == 1, body


def test_distinct_cells_with_same_text_are_kept(tmp_path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "☑"
    table.cell(0, 1).text = "☑"
    path = tmp_path / "dup.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert parsed.tables[0].body.count("☑") == 2, parsed.tables[0].body


def test_plain_table_stays_a_plain_grid(tmp_path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "姓名"
    table.cell(0, 1).text = "部门"
    table.cell(1, 0).text = "张三"
    table.cell(1, 1).text = "工程部"
    path = tmp_path / "plain.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    body = parsed.tables[0].body
    assert "colspan" not in body and "rowspan" not in body
    assert body.count("<td>") == 2


def test_table_repair_skips_word_layout_tables() -> None:
    """表修复走"行 → 重拼 HTML"，会丢跨度；原生 Word 版式的表（preserve_layout）必须原样保留。"""
    from ekbs.ingest.table_merge import repair_table_html

    body = '<table><tr><td colspan="2">工作内容</td><td rowspan="2">备注</td></tr></table>'
    assert repair_table_html(body, preserve_layout=True) == (body, False)
    # 扫描件表（无版式标记）仍走修复
    fixed, changed = repair_table_html(
        '<table><tr><td>  冷却 水  </td><td>6.5-9.0</td></tr></table>'
    )
    assert changed is True and "冷却水" in fixed


def test_continued_table_merge_skips_word_layout_tables() -> None:
    """续页合并会按行重拼 HTML；原生 Word 版式的表不参与合并，跨度不被拍平。"""
    from ekbs.ingest.chunker import TableInput
    from ekbs.ingest.table_merge import merge_continued_tables

    left = TableInput(
        body='<table><tr><td colspan="2">姓名</td></tr></table>',
        page=1,
        table_id="1",
        preserve_layout=True,
    )
    right = TableInput(body="<table><tr><td>乙</td></tr></table>", page=2, table_id="2")
    _, tables, stats = merge_continued_tables("[表格 1]\n[表格 2]\n", [left, right])

    assert stats.get("tables_merged_pairs", 0) == 0
    assert len(tables) == 2
    assert 'colspan="2"' in tables[0].body


def test_word_symbol_checkboxes_are_preserved(tmp_path) -> None:
    """Word 把复选框存在 ``w:sym``（符号字体）里；只读 ``w:t`` 会整段丢掉。

    回归：《客户满意度调查表》的服务评价区 18 个 Wingdings 复选框全部消失，
    变成"一至三次三至五次五次以上"。
    """
    import docx
    from docx.oxml.ns import qn

    document = docx.Document()
    paragraph = document.add_paragraph("选择：")
    run = paragraph.add_run()
    sym = run._r.makeelement(
        qn("w:sym"), {qn("w:font"): "Wingdings", qn("w:char"): "00A8"}
    )
    run._r.append(sym)
    paragraph.add_run("一至三次")
    path = tmp_path / "sym.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert "□一至三次" in parsed.markdown, parsed.markdown
