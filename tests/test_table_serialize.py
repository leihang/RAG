"""确定性表格序列化与 table 分块契约。"""

from __future__ import annotations

from ekbs.ingest.chunker import TableInput, chunk_markdown
from ekbs.ingest.table_serialize import (
    coerce_rows,
    serialize_information_blocks,
    serialize_information_blocks_spans,
    serialize_table_body,
)


def _sample_rows(count: int) -> list[list[str]]:
    return [["项目", "说明", "金额"]] + [
        [
            f"项目{i}",
            f"与年度能耗管理计划相关的说明文字第{i}条，需记录责任人及时间节点并确保执行到位",
            str(i),
        ]
        for i in range(count)
    ]


# --- span 序列化（表格父容器超限分段用；与整块序列化同口径）-------------------

def test_span_serializer_matches_full_serializer() -> None:
    """span 只是换"打包单位"：同预算下每段文本与 ``serialize_information_blocks`` 逐字节一致。"""
    rows = _sample_rows(40)
    for max_tokens in (2048, 600, 200):
        full = serialize_information_blocks(caption="费用", rows=rows, max_tokens=max_tokens)
        spans = serialize_information_blocks_spans(
            caption="费用", rows=rows, max_tokens=max_tokens
        )
        assert [span[0] for span in spans] == full, f"max_tokens={max_tokens}"


def test_span_serializer_marks_preamble_only_block_with_empty_range() -> None:
    """首行就超预算时，整块序列化会先吐一个只有 preamble 的退化块。

    span 版要逐字节复刻这段文本（口径一致），但用 ``last_row < first_row`` 的空区间标记
    它没有数据行 —— 分段器据此跳过，不会伪造成某一行。
    """
    rows = [["项目", "说明"]] + [["项目0", "超长单元格说明文字" * 400], ["项目1", "短说明"]]
    full = serialize_information_blocks(caption="大表", rows=rows, max_tokens=384)
    spans = serialize_information_blocks_spans(caption="大表", rows=rows, max_tokens=384)
    assert [span[0] for span in spans] == full
    assert spans[0][2] < spans[0][1], "只有 preamble 的退化块必须标成空区间"
    assert all(span[2] >= span[1] for span in spans[1:]), "其余段必须覆盖真实数据行"
    covered = [row for span in spans[1:] for row in range(span[1], span[2] + 1)]
    assert covered == list(range(2)), "数据行区间仍连续覆盖每一行"


def test_span_serializer_spans_are_contiguous_and_cover_every_row_once() -> None:
    rows = _sample_rows(40)
    spans = serialize_information_blocks_spans(caption="费用", rows=rows, max_tokens=200)
    per_row = serialize_information_blocks_spans(caption="费用", rows=rows, row_chunks=True)
    assert len(per_row) == 40, "row_chunks=True 应一行一段"
    assert [(span[1], span[2]) for span in per_row] == [(i, i) for i in range(40)]
    assert len(spans) > 1, "200 token 预算下 41 行必须切成多段"
    assert spans[0][1] == 0
    for prev, cur in zip(spans, spans[1:]):
        assert cur[1] == prev[2] + 1, f"数据行必须连续覆盖、无重叠：{prev} → {cur}"
    assert spans[-1][2] == 39


def test_span_serializer_row_range_keeps_header_and_absolute_indices() -> None:
    """按行号只序列化一段：仍重复表头，返回的是**整表绝对下标**（供叶子映射）。"""
    rows = _sample_rows(10)
    single = serialize_information_blocks_spans(
        caption="费用", rows=rows, max_tokens=2048, row_range=(2, 3)
    )
    assert [(span[1], span[2]) for span in single] == [(2, 3)]
    assert "相关表头: 项目 | 说明 | 金额" in single[0][0], "每段必须重复表头（自解释）"
    assert "项目=项目2" in single[0][0]
    assert "项目=项目0" not in single[0][0], "行范围之外的数据行不得出现"

    per_row = serialize_information_blocks_spans(
        caption="费用", rows=rows, row_chunks=True, row_range=(2, 4)
    )
    assert [(span[1], span[2]) for span in per_row] == [(2, 2), (3, 3), (4, 4)]
    assert all("相关表头:" in span[0] for span in per_row)


def test_span_serializer_rejects_out_of_range_row_range() -> None:
    import pytest

    rows = _sample_rows(3)
    with pytest.raises(ValueError):
        serialize_information_blocks_spans(rows=rows, row_range=(0, 3))


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


def test_xlsx_two_level_budget_headers_keep_parent_fee_name(tmp_path) -> None:
    """预算分月两级表头：子列名保留，上级费用名可定位（不塌成同一键）。

    复现：A2:A3 + B2:E2 合并成「项目（收入成本均含税）」，第 3 行才是
    项目|取数账套|取数科目|项目明细。旧逻辑子行未标 th → 纵向合并跳过 A3 →
    整行左移，五列全叫同一个名字，问「上级费用名称」必然答错。
    """
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "2026年度预算模板"
    ws.merge_cells("A2:A3")
    ws.merge_cells("B2:E2")
    ws.merge_cells("F2:F3")
    ws["A2"] = "项目（收入成本均含税）"
    ws["B2"] = "项目（收入成本均含税）"
    ws["F2"] = "2026年度计划金额"
    ws["B3"] = "项目"
    ws["C3"] = "取数账套"
    ws["D3"] = "取数科目"
    ws["E3"] = "项目明细"
    ws["A4"] = "六、工程费用"
    ws["B4"] = "2、工程维护及日常维修材料费"
    ws["C4"] = "1204"
    ws["D4"] = "销售费用-其他设备维修费"
    ws["E4"] = "街区应急维修、给排水管网等费用"
    ws["F4"] = 90000
    path = tmp_path / "budget_headers.xlsx"
    wb.save(path)
    doc = parse_file(path)
    assert doc.tables
    blocks = serialize_table_body(caption=doc.tables[0].caption, body=doc.tables[0].body)
    text = "\n".join(blocks)
    header_line = next(line for line in text.splitlines() if line.startswith("相关表头:"))
    assert "取数科目" in header_line
    assert "项目明细" in header_line
    # 五列不再是同一个裸键；子列名挂在父标签下
    assert "/取数科目" in header_line or header_line.split("|")[3].strip().endswith("取数科目")
    assert "取数科目=销售费用-其他设备维修费" in text or "项目（收入成本均含税）/取数科目=销售费用-其他设备维修费" in text
    assert "项目明细=街区应急维修、给排水管网等费用" in text or "项目（收入成本均含税）/项目明细=街区应急维修、给排水管网等费用" in text


def test_filled_two_level_headers_merge_without_empty_top() -> None:
    """xlsx 合并填充后顶行无空列，仍应按子行合成父子表头。"""
    rows = [
        [
            "项目（收入成本均含税）",
            "项目（收入成本均含税）",
            "项目（收入成本均含税）",
            "项目（收入成本均含税）",
            "2026年度计划金额",
        ],
        ["项目（收入成本均含税）", "项目", "取数账套", "取数科目", "2026年度计划金额"],
        ["六、工程费用", "2、维护费", "1204", "销售费用-其他设备维修费", "90000"],
    ]
    blocks = serialize_information_blocks(caption="预算", rows=rows, header_row_count=2)
    text = blocks[0]
    assert "取数科目" in text
    assert "取数科目=销售费用-其他设备维修费" in text or "项目（收入成本均含税）/取数科目=销售费用-其他设备维修费" in text


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


def test_sectioned_month_series_inherits_sheet_header(tmp_path) -> None:
    """同 Sheet 仅首段有真表头时，后续「酒店」分节必须继承列名，不能当伪表头。

    复现大户用电监视：真表头只出现一次，后面餐饮/酒店只有单格分区标题；
    旧逻辑把「酒店」当 header 再合并填充 → ``酒店=铺号; 酒店=用户名; …``。
    """
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    months = [f"{m}月" for m in range(1, 13)]
    header = ["铺号", "用户名", "年度", *months]
    wb = Workbook()
    ws = wb.active
    ws.title = "大户用电监视"
    ws.append(["用电大户用量监视（度）"])
    ws.append(header)
    ws.append(["(2A-1)", "海豚电竞", "2026年", *([str(i) for i in range(1, 13)])])
    ws.append(["酒店"])
    hotel_row = ["(3幢)", "嘛嘛公寓", "2026年", *[str(1000 + m) for m in range(1, 13)]]
    hotel_row[3 + 8] = "91645"  # 铺号|用户名|年度 之后第 9 个月
    ws.append(hotel_row)
    # 合并「酒店」跨多列：旧路径会把整行 th 填成酒店
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=15)
    path = tmp_path / "dashu.xlsx"
    wb.save(path)

    doc = parse_file(path)
    hotel_tables = [t for t in doc.tables if "酒店" in (t.caption or "")]
    assert hotel_tables, f"captions={[t.caption for t in doc.tables]}"
    hotel = hotel_tables[0]
    assert "（/ 酒店）" in hotel.caption or "/ 酒店" in hotel.caption
    # 解析 HTML 表头必须已是真列名，不依赖 serialize 侧 repair
    assert "<th>用户名</th>" in hotel.body
    assert "<th>9月</th>" in hotel.body
    assert hotel.body.count("<th>酒店</th>") == 0

    texts = [
        serialize_table_body(caption=t.caption, body=t.body)[0]
        for t in doc.tables
        if "酒店" in (t.caption or "")
    ]
    joined = "\n".join(texts)
    assert "用户名=嘛嘛公寓" in joined
    assert "9月=91645" in joined or "用电量=91645" in joined
    assert "酒店=铺号" not in joined
    assert "酒店=嘛嘛公寓" not in joined


def test_xlsx_date_cell_converted_from_serial(tmp_path) -> None:
    """Excel 日期单元格不能以序列号 46295 入库，应转为可读日期。"""
    from datetime import date
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "用电分析"
    ws["A1"] = "日期"
    ws["B1"] = date(2026, 9, 30)
    ws["B1"].number_format = "yyyy-mm-dd"
    ws["A2"] = "总电量"
    ws["B2"] = 4379405
    path = tmp_path / "date.xlsx"
    wb.save(path)
    doc = parse_file(path)
    body = doc.tables[0].body
    assert "46295" not in body
    assert "2026-09" in body
    assert "2026-09-30" not in body


def test_general_format_date_serials_in_date_header(tmp_path) -> None:
    """日期行里的 General 格式序列号（46295）也应转成账期 YYYY-MM。"""
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "用电分析"
    ws.append(["用量   日期", 46295, 45930, 46265, "同比"])
    ws.append(["总电量", 4379405, 4616475, 5288426, "-5%"])
    path = tmp_path / "serial.xlsx"
    wb.save(path)
    doc = parse_file(path)
    body = doc.tables[0].body
    assert "46295" not in body
    assert "2026-09" in body
    assert "2026-09-30" not in body


def test_calendar_serial_row_without_date_word_converts(tmp_path) -> None:
    """水费/熙丰：日期行没有「日期」二字，但序列号按月间隔 → 账期 YYYY-MM（不带日）。"""
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "水费账单"
    # 45292=2024-01-01, 45323=2024-02-01, 45352=2024-03-01（General 格式）
    ws.append(["序号", "水卡号", "用途", 45292, "", "", 45323, "", "", 45352, "", ""])
    ws.append(["序号", "水卡号", "用途", "上月起度", "本月止度", "用量", "上月起度", "本月止度", "用量", "上月起度", "本月止度", "用量"])
    ws.append([1, "30005", "绿化", 100, 110, 10, 110, 125, 15, 125, 140, 15])
    path = tmp_path / "water_serial.xlsx"
    wb.save(path)
    doc = parse_file(path)
    body = doc.tables[0].body
    assert "45292" not in body
    assert "2024-01" in body
    assert "2024-01-01" not in body


def test_month_end_date_formatted_header_collapses_to_yyyy_mm(tmp_path) -> None:
    """日期格式单元格若是月末（熙丰常见），表头仍归一成 YYYY-MM，不要 2026-01-31。"""
    from datetime import date

    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "熙丰用电"
    ws.append(
        [
            "户号",
            date(2026, 1, 31),
            "",
            "",
            date(2026, 2, 28),
            "",
            "",
        ]
    )
    for cell in (ws.cell(1, 2), ws.cell(1, 5)):
        cell.number_format = "YYYY-MM-DD"
    ws.append(["户号", "用量", "单价", "金额", "用量", "单价", "金额"])
    ws.append(["差异", 1, 1, 1987.86, 1, 1, 270.98])
    path = tmp_path / "xifeng_month_end.xlsx"
    wb.save(path)
    body = parse_file(path).tables[0].body
    assert "2026-01" in body
    assert "2026-02" in body
    assert "2026-01-31" not in body
    assert "2026-02-28" not in body


def test_date_row_above_labels_merges_into_headers() -> None:
    """日期行在字段名行之上时并入列名，不得变成 `上月起度=45292` 数据行。

    宽表按指标组切开后，两个月可能落在不同块；拼起来断言即可。
    """
    rows = [
        ["序号", "用途", "2024-01-01", "", "", "2024-02-01", "", ""],
        ["序号", "用途", "上月起度", "本月止度", "用量", "上月起度", "本月止度", "用量"],
        ["1", "绿化", "100", "110", "10", "110", "125", "15"],
    ]
    blocks = serialize_information_blocks(caption="水费账单", rows=rows)
    text = "\n".join(blocks)
    assert "上月起度=45292" not in text
    assert "上月起度/2024-01" in text
    assert "上月起度/2024-02" in text
    assert "2024-01-01" not in text
    assert "用途=绿化" in text
    assert all("相关表头:" in b for b in blocks)
    # 每块相关表头不得拖着另一月的整组列
    assert all(
        not ("上月起度/2024-01" in b and "上月起度/2024-02" in b) for b in blocks
    ), blocks


def test_numeric_data_row_not_merged_as_subheader() -> None:
    """气费：顶行空首列 + 下一行锅炉读数，不得并成 `起度/379749` 相关表头。"""
    rows = [
        ["", "起度", "止度", "用量", "单价", "金额"],
        ["锅炉", "379749", "410802", "31053", "2.576", "79992.53"],
        ["食堂", "8911", "9088", "177", "2.576", "455.95"],
    ]
    text = serialize_information_blocks(caption="气费账单", rows=rows)[0]
    header_line = next(line for line in text.splitlines() if line.startswith("相关表头:"))
    assert "起度/379749" not in header_line
    assert "379749" not in header_line
    assert "起度" in header_line
    assert "列1=锅炉" in text or "; 起度=379749" in text
    assert "食堂" in text


def test_wide_row_chunks_split_by_column_windows() -> None:
    """宽抄表一行仍超叶子预算时按列窗拆，每块 ≤ max_tokens。"""
    # 4 身份列 + 12 个月 × 5 指标
    headers = ["序号", "卡号", "表号", "用途"]
    for month in range(1, 13):
        headers.extend([
            f"上月起度/2024-{month:02d}-01",
            f"本月止度/2024-{month:02d}-01",
            f"用量/2024-{month:02d}-01",
            f"单价/2024-{month:02d}-01",
            f"金额/2024-{month:02d}-01",
        ])
    data = ["1", "30005", "1905", "绿化"] + ["100", "110", "10", "4.55", "45.5"] * 12
    rows = [headers, data]
    blocks = serialize_information_blocks(
        caption="水费账单", rows=rows, row_chunks=True, max_tokens=384
    )
    from ekbs.ingest.chunker import count_tokens

    assert len(blocks) > 1, "12 个月宽行必须拆成多块"
    assert all(count_tokens(b) <= 384 for b in blocks), [count_tokens(b) for b in blocks]
    assert any("上月起度/2024-01" in b for b in blocks)
    assert not any("上月起度/2024-01-01" in b for b in blocks)


# --- Word/PowerPoint 合并单元格（colspan / rowspan）的网格还原 -------------------
# 事故（doc_id=196 XJWY-GC-12 能耗管理作业指导书）：
#   表 1 两行表头 `<th rowspan="2">级别</th><th colspan="3">分级内容</th>` 被展平成
#   "级别/能耗检查 | 分级内容/能耗运行管控 | 节能改造 | 列4"，A/B/C 三行的值整体错绑一格；
#   表 6 `<td rowspan="7">照明</td>` 的 12 行延续行因少一格整体左移，13 块"管理内容"为空。
# 根因：`parse_table_html` 只补 colspan 的空格，**完全不处理 rowspan**，延续行少一格。

def test_rowspan_two_row_header_aligns_columns() -> None:
    html = (
        "<table>"
        '<tr><th rowspan="2">级别</th><th colspan="3">分级内容</th></tr>'
        "<tr><td>能耗检查</td><td>能耗运行管控</td><td>节能改造</td></tr>"
        "<tr><td>A级</td><td>能耗普查</td><td>空调锅炉运行能耗管控</td>"
        "<td>年度成本降低10万元以上</td></tr>"
        "</table>"
    )
    blocks = serialize_table_body(caption="能耗管理分级", body=html, table_id="1")
    text = blocks[0]
    assert (
        "相关表头: 级别 | 分级内容/能耗检查 | 分级内容/能耗运行管控 | 分级内容/节能改造"
        in text
    ), text
    assert "级别=A级; 分级内容/能耗检查=能耗普查" in text, text
    assert "分级内容/能耗运行管控=空调锅炉运行能耗管控" in text, text
    assert "分级内容/节能改造=年度成本降低10万元以上" in text, text
    assert "列4" not in text, text


def test_rowspan_data_rows_repeat_group_label() -> None:
    html = (
        "<table>"
        "<tr><th>类别</th><th>项目</th><th>管理内容</th></tr>"
        '<tr><td rowspan="2">照明</td><td>大堂照明</td><td>8:00-22:00</td></tr>'
        "<tr><td>车库照明</td><td>双亮度感应</td></tr>"
        "<tr><td>设备</td><td>设备房电器</td><td>人走灯熄</td></tr>"
        "</table>"
    )
    text = serialize_table_body(caption="开关机时间表", body=html, table_id="6")[0]
    assert "类别=照明; 项目=大堂照明; 管理内容=8:00-22:00" in text, text
    assert "类别=照明; 项目=车库照明; 管理内容=双亮度感应" in text, text
    assert "类别=设备; 项目=设备房电器; 管理内容=人走灯熄" in text, text


def test_colspan_note_row_is_not_labelled_as_first_column() -> None:
    html = (
        "<table>"
        "<tr><th>类别</th><th>项目</th><th>管理内容</th></tr>"
        "<tr><td>照明</td><td>车库</td><td>感应灯</td></tr>"
        '<tr><td colspan="3">设备控制开关显眼位置应张贴对应的控制时间表</td></tr>'
        "</table>"
    )
    text = serialize_table_body(caption="开关机时间表", body=html, table_id="6")[0]
    assert "备注=设备控制开关显眼位置应张贴对应的控制时间表" in text, text
    assert "类别=设备控制开关显眼位置" not in text, text


def test_information_block_html_roundtrip_keeps_note_row() -> None:
    from ekbs.ingest.table_serialize import information_block_to_html

    html = (
        "<table>"
        "<tr><th>类别</th><th>项目</th><th>管理内容</th></tr>"
        "<tr><td>照明</td><td>车库</td><td>感应灯</td></tr>"
        '<tr><td colspan="3">控制时间表应张贴在显眼位置</td></tr>'
        "</table>"
    )
    text = serialize_table_body(caption="开关机时间表", body=html, table_id="6")[0]
    rebuilt = information_block_to_html(text)
    assert rebuilt is not None
    assert "控制时间表应张贴在显眼位置" in rebuilt


def test_rowspan_grid_width_stays_rectangular() -> None:
    """rowspan 跨列组合也不能让后续行错列。"""
    from ekbs.ingest.table_serialize import parse_table_html

    rows = parse_table_html(
        "<table>"
        '<tr><td rowspan="2" colspan="2">A</td><td>B</td></tr>'
        "<tr><td>C</td></tr>"
        "<tr><td>D</td><td>E</td><td>F</td></tr>"
        "</table>"
    )
    assert len({len(r) for r in rows}) == 1, rows
    assert rows[2] == ["D", "E", "F"], rows


def test_empty_placeholder_columns_are_dropped() -> None:
    """合并标题行会展开出几十个 ``列N`` 空列；整列空且表头是占位符的列必须丢掉。

    实测 2026-09 用电分析表 39 列、其中 25 个 ``列N`` 全空，切分后 LLM 漏了
    「经营用电」行；紧凑化后整表 1432 token、不再分段，所有行都在同一容器。
    """
    from ekbs.ingest.table_serialize import _drop_empty_columns

    headers = ["用量日期", "2026-09-30", "列3", "列4", "无数据标签"]
    rows = [
        ["总电量", "4379405", "", "", ""],
        ["经营用电", "186726", "", "", ""],
    ]
    kept_headers, kept_rows = _drop_empty_columns(headers, rows)
    assert kept_headers == ["用量日期", "2026-09-30", "无数据标签"]
    assert kept_rows == [["总电量", "4379405", ""], ["经营用电", "186726", ""]]


def test_xlsx_percent_cell_preserves_excel_display(tmp_path) -> None:
    """Excel 百分比格式 ``0.00%`` 必须还原成 -5.14%，不能把 0.0513 原样入库。"""
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "用电分析"
    ws.append(["项目", "同比", "环比"])
    ws.append(["总电量", -0.0513530345122631, -0.17188876236521])
    ws["B2"].number_format = "0.00%"
    ws["C2"].number_format = "0.00%"
    path = tmp_path / "percent.xlsx"
    wb.save(path)
    doc = parse_file(path)
    body = doc.tables[0].body
    assert "-5.14%" in body
    assert "-17.19%" in body
    assert "0.0513530345122631" not in body


def test_xlsx_table_body_is_html_for_document_compare(tmp_path) -> None:
    """复核页「文档对比」直接渲染 body_html；xlsx 表体必须是带合并的 HTML 表格。

    旧实现存 Markdown 管道表，复核页 iframe 里就是 `| a | b |` 文本，和原件对不上。
    """
    from openpyxl import Workbook
    from ekbs.ingest.parser import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "用电分析"
    ws.merge_cells("A1:D1")
    ws["A1"] = "熙街2026年9月用电分析表"
    ws.append(["项目", "2026-09-30", "同比", "环比"])
    ws.append(["总电量", 4379405, -0.0513530345122631, -0.17188876236521])
    ws["B3"].number_format = "0.00"
    ws["C3"].number_format = "0.00%"
    ws["D3"].number_format = "0.00%"
    path = tmp_path / "compare.xlsx"
    wb.save(path)
    doc = parse_file(path)
    body = doc.tables[0].body
    assert body.startswith("<table>")
    assert 'colspan="4"' in body
    assert "-5.14%" in body
    assert "-17.19%" in body
    assert "4379405.00" in body
    assert not body.lstrip().startswith("|")


def test_month_wide_table_melts_to_tidy_leaves_when_row_chunks() -> None:
    """宽月份表 + row_chunks → 一行一月观测；并产出按月 TopN 摘要。"""
    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "9月", "10月", "11月", "12月"],
        ["A-1", "甲商户", "2026年", "10", "20", "100", "40", "50", "60"],
        ["B-2", "乙商户", "2026年", "11", "21", "200", "41", "51", "61"],
        ["C-3", "丙商户", "2026年", "12", "22", "150", "42", "52", "62"],
    ]
    # 非 row_chunks：保持宽行，不熔
    wide = serialize_information_blocks(caption="大户用电监视", rows=rows, row_chunks=False)
    assert any("1月=10" in block and "9月=100" in block for block in wide)

    leaves = serialize_information_blocks(
        caption="大户用电监视（第1段）（用电大户用量监视（度） / 娱乐）",
        rows=rows,
        row_chunks=True,
    )
    tidy = [b for b in leaves if "月份=" in b and "用电量=" in b]
    assert len(tidy) == 3 * 6  # 3 户 × 6 个月列
    hit = next(b for b in tidy if "用户名=乙商户" in b and "月份=9" in b)
    assert "用电量=200" in hit
    assert "年度=2026" in hit
    assert "类别=娱乐" in hit
    # 宽行键不应再出现在叶子里
    assert not any("; 1月=" in b and "; 9月=" in b for b in tidy)
    summaries = [b for b in leaves if "排名类型=Top10" in b]
    assert summaries
    sep = next(b for b in summaries if "月份=9" in b and "年度=2026" in b)
    assert "1. 乙商户=200" in sep
    assert "2. 丙商户=150" in sep


def test_month_series_infers_layout_from_uniform_category_headers() -> None:
    """分节后伪表头（整行「娱乐」）仍能按数据形态熔成长叶子。"""
    rows = [
        ["娱乐"] * 15,
        ["(2A-1)", "海豚电竞", "2026年", "1", "2", "3", "4", "5", "6", "7", "8", "131120", "10", "11", "12"],
        ["(其它)", "保利影城", "2026年", "1", "2", "3", "4", "5", "6", "7", "8", "90000", "10", "11", "12"],
    ]
    leaves = serialize_information_blocks(
        caption="大户用电监视（第3段）", rows=rows, row_chunks=True
    )
    hit = next(
        b for b in leaves
        if "用户名=海豚电竞" in b and "月份=9" in b and "用电量=131120" in b
    )
    assert "类别=娱乐" in hit
    assert "年度=2026" in hit


def test_month_wide_without_year_column_does_not_melt() -> None:
    """Phase A：≥6 个月列但无年列 → 不熔（安全默认；不从文件名猜年）。

    显式 ``default_year`` 才会熔（对应 ``month_series_infer_year_from_filename``）。
    """
    from ekbs.ingest.table_serialize import detect_month_series_layout

    rows = [
        ["铺号", "用户名", "1月", "2月", "3月", "4月", "5月", "6月", "9月"],
        ["A-1", "甲商户", "10", "20", "30", "40", "50", "60", "100"],
        ["B-2", "乙商户", "11", "21", "31", "41", "51", "61", "200"],
    ]
    headers, data = rows[0], rows[1:]
    assert detect_month_series_layout(headers, data, caption="大户用电2026") is None
    # 宽行保留，不产出月份= tidy 叶子
    leaves = serialize_information_blocks(
        caption="大户用电2026", rows=rows, row_chunks=True
    )
    assert not any("月份=" in b for b in leaves)
    assert any("1月=" in b and "9月=" in b for b in leaves)
    # 显式传年才熔（产品开关路径）
    assert detect_month_series_layout(
        headers, data, caption="大户用电", default_year="2026"
    ) is not None
    melted = serialize_information_blocks(
        caption="大户用电", rows=rows, row_chunks=True, default_year="2026"
    )
    assert any("月份=9" in b and "用电量=200" in b for b in melted)


def test_uniform_merged_month_header_gets_month_labels() -> None:
    """合并表头整行都是“酒店”时，后 12 列要恢复成 1月..12月。

    实证：大户用电监视（第4段）2026 年行是
    84445/29905/50263/.../58678，但表头全是“酒店”，模型不知道列含义。
    """
    html = (
        "<table><tr>"
        + "".join("<th>酒店</th>" for _ in range(15))
        + "</tr><tr>"
        + "".join(
            f"<td>{v}</td>"
            for v in ["三期3幢", "嘛嘛公寓", "2026年", *range(1, 13)]
        )
        + "</tr></table>"
    )
    blocks = serialize_table_body(caption="大户用电监视", body=html)
    text = "\n".join(blocks)
    assert "相关表头:" in text
    assert "1月" in text and "12月" in text
    assert "年度=2026年" in text
    assert "1月=1" in text and "9月=9" in text
    # 不得再出现「酒店=三期3幢; 酒店=嘛嘛公寓; 酒店=2026年」
    assert "酒店=三期" not in text
    assert "铺号=三期3幢" in text and "用户名=嘛嘛公寓" in text

    # tidy 长叶子：身份列 + 类别=酒店
    melted = serialize_table_body(
        caption="大户用电监视（第4段）", body=html, row_chunks=True
    )
    leaf = "\n".join(melted)
    assert "类别=酒店" in leaf
    assert "铺号=三期3幢" in leaf
    assert "相关表头: 酒店 | 酒店" not in leaf
