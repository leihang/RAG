"""确定性表格序列化与 table 分块契约。"""

from __future__ import annotations

from ekbs.ingest.chunker import TableInput, chunk_markdown
from ekbs.ingest.table_serialize import (
    coerce_rows,
    serialize_information_blocks,
    serialize_table_body,
)


def test_html_table_serialize_contains_headers_and_values() -> None:
    html = """
    <table>
      <tr><th>档次</th><th>金额（元）</th></tr>
      <tr><td>一档</td><td>500</td></tr>
      <tr><td>二档</td><td>800</td></tr>
    </table>
    """
    blocks = serialize_table_body(caption="差旅补助标准", body=html, table_id="1")
    assert len(blocks) == 1
    text = blocks[0]
    assert "表名: 差旅补助标准" in text
    assert "相关表头: 档次 | 金额（元）" in text
    assert "档次=一档; 金额（元）=500" in text
    assert "档次=二档; 金额（元）=800" in text


def test_markdown_pipe_table_coerce() -> None:
    md = "| 项目 | 标准 |\n| --- | --- |\n| 通讯费 | 200 |\n"
    rows = coerce_rows(md)
    assert rows[0] == ["项目", "标准"]
    assert rows[1] == ["通讯费", "200"]
    blocks = serialize_information_blocks(caption="补贴", rows=rows)
    assert "项目=通讯费; 标准=200" in blocks[0]


def test_chunk_markdown_emits_table_type() -> None:
    result = chunk_markdown(
        "# 制度\n\n正文一段。\n",
        tables=[
            TableInput(
                body="<table><tr><th>档次</th><th>金额</th></tr>"
                "<tr><td>一档</td><td>500</td></tr></table>",
                caption="报销档次表",
                page=2,
                table_id="1",
            )
        ],
    )
    tables = [n for n in result.nodes if n.chunk_type == "table"]
    assert len(tables) == 1
    assert tables[0].page_from == 2
    assert "档次=一档" in tables[0].content
    assert "金额=500" in tables[0].content


def test_merged_title_rows_are_not_used_as_header() -> None:
    """开头的合并标题行不能当表头 —— 否则列名退化成 `列2…`，表格语义就没了。

    实测事故（2026-10-02，`2026年7月能耗分析.xlsx` 的「用电分析」表）：前两行都是
    合并单元格（第 1 行表名、第 2 行写单位 `电量分析(KW/h）`），真正的内容行在第 3 行。
    旧实现把第 1 行当表头 → 出题 97 个表格块的列名全是 `列2…列39`：按列名检索召不回、
    取数也定位不到列（README §四 第 80 条）。
    """
    rows = [
        ["熙街2026年7月用电分析表"] + [""] * 8,
        ["电量分析(KW/h）"] + [""] * 8,
        [" 用量   日期", "46234", "45869", "46203", "同比", "环比", "图表", "", "分析"],
        ["", "100", "200", "300", "1%", "2%", "", "", "ok"],
    ]
    blocks = serialize_information_blocks(caption="用电分析", rows=rows)
    text = blocks[0]
    assert "列2=" not in text and "列2 |" not in text, "不应再出现列序号占位（除真正无名的列）"
    assert "相关表头: 用量   日期 | 46234" in text
    assert "同比=1%" in text
    # 标题行与单位口径不能丢：它们折进「表名」
    assert "熙街2026年7月用电分析表" in text
    assert "电量分析(KW/h）" in text


def test_plain_two_row_table_unchanged() -> None:
    """普通两行表（真表头 + 一行数据）行为不变 —— 修的是"标题行"，不是所有行。"""
    blocks = serialize_information_blocks(caption="t", rows=[["a", "b"], ["c", "d"]])
    assert "相关表头: a | b" in blocks[0]
    assert "a=c; b=d" in blocks[0]


def test_table_with_only_title_rows_keeps_content() -> None:
    """极端：整张表只有标题行 → 内容必须保留（当单列数据），不能返回空。"""
    blocks = serialize_information_blocks(caption="", rows=[["只有标题"], [""]])
    assert blocks and "只有标题" in blocks[0]


def test_form_like_sheet_does_not_fake_header() -> None:
    """表单型 sheet 第 1 行是字段而不是列头：不能被当成 header。"""
    rows = [
        ["重庆.熙街商铺撤场清算表", "", "", "", "", "", "", ""],
        ["填表日期：", "2025.10.16", "", "品牌", "尊宝比萨", "", "", ""],
        ["承租人", "刘国静", "", "经营方式", "个体", "", "", ""],
        ["部门", "清算明细", "审核情况及处理意见", "", "", "审核人及部门负责人签字", "", ""],
        ["营运部", "①货品撤场日", "", "", "", "", "", ""],
    ]
    blocks = serialize_information_blocks(caption="商户撤柜清算表", rows=rows)
    text = blocks[0]
    assert "表格形态: 表单型" in text
    assert "相关表头: 列1 | 列2" in text
    assert "列1=填表日期：; 列2=2025.10.16" in text
    assert "填表日期：=2025.10.16" not in text


def test_xlsx_blank_rows_split_sheet_into_multiple_tables(tmp_path) -> None:
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "清算表"
    ws.append(["重庆.熙街商铺撤场清算表"])
    ws.append(["填表日期：", "2025.10.16", "", "品牌", "尊宝比萨"])
    ws.append([])
    ws.append(["部门", "清算明细", "审核情况及处理意见"])
    ws.append(["营运部", "①货品撤场日", ""])
    path = tmp_path / "清算表.xlsx"
    wb.save(path)
    doc = parse_file(path)
    assert len(doc.tables) >= 2
    assert any("第1段" in t.caption for t in doc.tables)
    assert any("第2段" in t.caption for t in doc.tables)
    from ekbs.ingest.table_serialize import serialize_table_body
    first = serialize_table_body(caption=doc.tables[0].caption, body=doc.tables[0].body)
    second = serialize_table_body(caption=doc.tables[1].caption, body=doc.tables[1].body)
    assert "填表日期" in first[0] and "表单型" in first[0]
    assert "部门" in second[0] and "清算明细" in second[0]


def test_two_row_header_is_merged() -> None:
    rows = [
        ["部门", "清算明细", "审核情况及处理意见", "", "审核人及部门负责人签字", ""],
        ["", "", "检查结果", "金额", "营运部经办人签字", "财务审核"],
        ["营运部", "①货品撤场日", "合格", "0", "张三", "李四"],
    ]
    blocks = serialize_information_blocks(caption="清算表", rows=rows)
    text = blocks[0]
    assert "审核情况及处理意见/检查结果" in text
    assert "审核人及部门负责人签字/营运部经办人签字" in text
    assert "部门=营运部; 清算明细=①货品撤场日" in text
    assert "检查结果=合格" in text


def test_header_row_not_first_uses_numeric_contrast() -> None:
    """首行是元数据、第二行才是表头：用"文字下方是数字"的对比信号找表头。"""
    rows = [
        ["填报单位", "工程部", ""],
        ["项目", "数量", "金额"],
        ["A", "100", "200"],
        ["B", "300", "400"],
    ]
    blocks = serialize_information_blocks(caption="统计表", rows=rows)
    text = blocks[0]
    assert "相关表头: 项目 | 数量 | 金额" in text
    assert "项目=A; 数量=100; 金额=200" in text


def test_xlsx_merged_header_inherits_master(tmp_path) -> None:
    """表头行里的横向合并单元格：从格应继承主格文字，不出现 列N 占位。"""
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "检查表"
    ws.merge_cells("A1:B1")
    ws["A1"] = "审核意见"
    ws["C1"] = "日期"
    ws["A2"] = "营运部"
    ws["B2"] = "合格"
    ws["C2"] = "2026-10-01"
    path = tmp_path / "merged.xlsx"
    wb.save(path)
    doc = parse_file(path)
    tables = doc.tables
    assert tables
    blocks = serialize_table_body(caption=tables[0].caption, body=tables[0].body)
    text = blocks[0]
    assert "审核意见" in text
    assert "列2" not in text.split("相关表头:", 1)[1].split("\n", 1)[0]


def test_single_row_table_does_not_crash_header_scan() -> None:
    blocks = serialize_information_blocks(caption="单行", rows=[["只有一行"]])
    assert blocks and "只有一行" in blocks[0]


def test_table_row_chunks_repeat_header() -> None:
    rows = [["项目", "金额"], ["A", "100"], ["B", "200"]]
    blocks = serialize_information_blocks(caption="费用", rows=rows, row_chunks=True)
    assert len(blocks) == 2
    for block in blocks:
        assert "相关表头: 项目 | 金额" in block
    assert "项目=A; 金额=100" in blocks[0]
    assert "项目=B; 金额=200" in blocks[1]


def test_split_rows_at_image_anchors_repeats_header() -> None:
    from ekbs.ingest.parser import _split_rows_at_image_anchors
    rows = [(1, ["项目", "金额"]), (2, ["A", "1"]), (3, ["B", "2"]), (4, ["C", "3"])]
    parts = _split_rows_at_image_anchors(rows, 0, [3])
    assert len(parts) == 2
    assert parts[0][0][1] == ["项目", "金额"]
    assert parts[1][0][1] == ["项目", "金额"]
    assert [r[0] for r in parts[0]] == [1, 2]
    assert [r[0] for r in parts[1]] == [1, 3, 4]


def test_single_cell_section_title_splits_sheet(tmp_path) -> None:
    """表之间用单格标题行分节（不是空行）时，必须切成多张表。"""
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "气分析"
    ws.append(["熙街气分析"])
    ws.append(["类别", "2026年9月用量", "单价"])
    ws.append(["锅炉", "0", "3.606"])
    ws.append(["年用气量统计对比（m³)"])
    ws.append(["项目", "年", "1月", "2月"])
    ws.append(["锅炉", "2023", "24506", "23361"])
    path = tmp_path / "gas.xlsx"
    wb.save(path)
    doc = parse_file(path)
    assert len(doc.tables) >= 2
    texts = [serialize_table_body(caption=t.caption, body=t.body)[0] for t in doc.tables]
    assert any("2026年9月用量=0" in t for t in texts)
    assert any("1月=24506" in t for t in texts)
