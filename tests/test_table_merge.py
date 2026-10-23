"""跨页续表合并（清洗步 4）。"""

from __future__ import annotations

from ekbs.ingest.chunker import TableInput, chunk_markdown
from ekbs.ingest.table_merge import TABLE_MERGE_MAX_TOKENS, merge_continued_tables


def _html(rows: list[list[str]]) -> str:
    parts = ["<table>"]
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        parts.append("<tr>")
        parts.extend(f"<{tag}>{c}</{tag}>" for c in row)
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def test_merge_same_header_across_pages() -> None:
    t1 = TableInput(
        body=_html([["姓名", "金额"], ["甲", "100"], ["乙", "200"]]),
        caption="差旅明细",
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["姓名", "金额"], ["丙", "300"], ["丁", "400"]]),
        caption="",
        page=2,
        table_id="2",
    )
    md = "前言\n[表格 1] 差旅明细\n[表格 2]\n后记\n"
    new_md, tables, stats = merge_continued_tables(md, [t1, t2])
    assert stats["tables_merged_pairs"] == 1
    assert len(tables) == 1
    assert tables[0].table_id == "1"
    assert "丙" in tables[0].body and "甲" in tables[0].body
    assert "[表格 1]" in new_md
    assert "[表格 2]" not in new_md
    # 重复表头只保留一次
    assert tables[0].body.count("姓名") == 1


def test_merge_continued_caption() -> None:
    t1 = TableInput(
        body=_html([["项目", "值"], ["A", "1"]]),
        caption="表1",
        page=3,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["项目", "值"], ["B", "2"]]),
        caption="续表",
        page=4,
        table_id="2",
    )
    md = "[表格 1] 表1\n[表格 2] 续表\n"
    _, tables, stats = merge_continued_tables(md, [t1, t2])
    assert stats["tables_merged_pairs"] == 1
    assert len(tables) == 1
    assert tables[0].caption == "表1"
    assert "B" in tables[0].body


def test_no_merge_when_intervening_text() -> None:
    t1 = TableInput(
        body=_html([["姓名", "金额"], ["甲", "1"]]),
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["姓名", "金额"], ["乙", "2"]]),
        page=2,
        table_id="2",
    )
    md = "[表格 1]\n这是另一段说明，不是续表。\n还有更多文字分隔。\n[表格 2]\n"
    _, tables, stats = merge_continued_tables(md, [t1, t2])
    assert not stats.get("tables_merged_pairs")
    assert len(tables) == 2


def test_no_merge_different_schema() -> None:
    t1 = TableInput(
        body=_html([["姓名", "金额"], ["甲", "1"]]),
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["城市", "标准", "备注"], ["京", "500", "-"]]),
        page=2,
        table_id="2",
    )
    md = "[表格 1]\n[表格 2]\n"
    _, tables, stats = merge_continued_tables(md, [t1, t2])
    assert not stats.get("tables_merged_pairs")
    assert len(tables) == 2


def test_skip_merge_when_too_long() -> None:
    header = ["列A", "列B"]
    big = [header] + [[f"r{i}a", f"r{i}b"] for i in range(4000)]
    t1 = TableInput(body=_html(big[:2000]), page=1, table_id="1")
    t2 = TableInput(body=_html([header] + big[2000:]), page=2, table_id="2")
    md = "[表格 1]\n[表格 2]\n"
    _, tables, stats = merge_continued_tables(md, [t1, t2], max_tokens=500)
    assert stats.get("tables_merge_skipped_too_long") == 1
    assert not stats.get("tables_merged_pairs")
    assert len(tables) == 2


def test_chain_merge_three_pages() -> None:
    rows = lambda start: _html(
        [["姓名", "金额"]] + [[f"人{i}", str(i * 10)] for i in range(start, start + 2)]
    )
    tables = [
        TableInput(body=rows(0), caption="名册", page=1, table_id="1"),
        TableInput(body=rows(2), caption="", page=2, table_id="2"),
        TableInput(body=rows(4), caption="续表", page=3, table_id="3"),
    ]
    md = "[表格 1] 名册\n[表格 2]\n[表格 3] 续表\n"
    new_md, out, stats = merge_continued_tables(md, tables)
    assert stats["tables_merged_pairs"] == 2
    assert len(out) == 1
    assert "人0" in out[0].body and "人4" in out[0].body
    assert "[表格 2]" not in new_md and "[表格 3]" not in new_md


def test_merged_table_still_chunks() -> None:
    """合并后仍可走分块；超长由 serialize 按行批拆，不挡入库。"""
    t1 = TableInput(
        body=_html([["k", "v"], ["a", "1"]]),
        caption="T",
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["k", "v"], ["b", "2"]]),
        page=2,
        table_id="2",
    )
    md, tables, _ = merge_continued_tables("[表格 1]\n[表格 2]\n", [t1, t2])
    result = chunk_markdown(md, tables=tables, doc_title="测")
    table_nodes = [n for n in result.nodes if n.chunk_type == "table"]
    assert table_nodes
    assert any("a=1" in n.content or "k=a" in n.content for n in table_nodes)


def test_merge_header_page_with_body_page() -> None:
    """首页只剩分组表头，次页才是数据；末行被竖线粘住也要拆开。"""
    header = (
        "<table><tr><td colspan=\"2\">工作内容</td><td rowspan=\"2\">备注</td></tr>"
        "<tr><td>内容</td><td>频次</td></tr></table>"
    )
    body = _html(
        [
            ["投药", "视水质情况，不低于每月一次", "冷冻水（制冷季）"],
            ["", "管路、主机清洗及投药|开机（年1次，主要为制冷季)", "冷、热、冷却水"],
        ]
    )
    tables = [
        TableInput(body=header, page=1, table_id="1"),
        TableInput(body=body, page=2, table_id="2"),
    ]
    new_md, out, stats = merge_continued_tables("[表格 1]\n[表格 2]\n", tables)
    assert stats["tables_merged_pairs"] == 1
    assert len(out) == 1
    assert "[表格 2]" not in new_md
    assert "工作内容" in out[0].body and "频次" in out[0].body
    assert "管路、主机清洗及投药" in out[0].body
    assert "开机（年1次" in out[0].body
    assert "|" not in out[0].body


def test_repair_water_quality_shifted_cells() -> None:
    body = (
        "<table>"
        "<tr><td colspan=\"9\">水处理控制指标（标准DB44/T115-2000)</td></tr>"
        "<tr><td>项目</td><td>PH</td><td>电导率</td><td>总硬总浊度 度</td><td></td>"
        "<td>度</td><td>总碱氯离总铁 子</td><td></td><td>铜离子</td></tr>"
        "<tr><td>单位</td><td></td><td>Us/cm</td><td>Mg/L</td><td>度（ntu）Mg/L</td>"
        "<td></td><td>Mg/L</td><td>Mg/L</td><td>Mg/L</td></tr>"
        "<tr><td>冷却 水</td><td>6.5-9.0</td><td>&lt;2500</td><td>&lt;600</td><td>&lt;50</td>"
        "<td>&lt;600</td><td>&lt;300</td><td>&lt;1.0</td><td>&lt;0.1</td></tr>"
        "<tr><td>水</td><td>冷冻7.0-10.0</td><td>500-3000</td><td>&lt;600</td><td>&lt;20</td>"
        "<td>&lt;600</td><td>&lt;200</td><td>&lt;1.0</td><td>&lt;0.1</td></tr>"
        "<tr><td>水</td><td>采暖7.5-10.0</td><td>&lt;3000</td><td>&lt;200</td><td>&lt;20</td>"
        "<td>&lt;800</td><td>&lt;200</td><td>&lt;1.0</td><td>&lt;0.1</td></tr>"
        "</table>"
    )
    _, tables, stats = merge_continued_tables(
        "[表格 1]\n",
        [TableInput(body=body, page=4, table_id="1")],
    )
    assert stats.get("tables_repaired") == 1
    html = tables[0].body
    assert "总硬度" in html and "氯离子" in html
    assert "冷却水" in html and "冷冻水" in html and "采暖水" in html
    assert "冷冻7.0" not in html
    assert "度（ntu）" in html


def test_default_limit_matches_constant() -> None:
    assert TABLE_MERGE_MAX_TOKENS >= 2048


def test_merge_data_row_without_repeated_header() -> None:
    """次页不重复表头、也不写续表，第一行已是金额数据，仍应合成一张。"""
    t1 = TableInput(
        body=_html([["姓名", "金额"], ["甲", "100"]]),
        caption="清单",
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["乙", "200"], ["丙", "300"]]),
        caption="",
        page=2,
        table_id="2",
    )
    md = "[表格 1] 清单\n第2页\n施工合同\n[表格 2]\n"
    new_md, tables, stats = merge_continued_tables(md, [t1, t2])
    assert stats["tables_merged_pairs"] == 1
    assert len(tables) == 1
    assert "乙" in tables[0].body and "甲" in tables[0].body
    assert "[表格 2]" not in new_md


def test_no_merge_different_header_same_width() -> None:
    t1 = TableInput(
        body=_html([["姓名", "部门"], ["甲", "工程"]]),
        page=1,
        table_id="1",
    )
    t2 = TableInput(
        body=_html([["城市", "标准"], ["京", "一类"]]),
        page=2,
        table_id="2",
    )
    _, tables, stats = merge_continued_tables("[表格 1]\n[表格 2]\n", [t1, t2])
    assert not stats.get("tables_merged_pairs")
    assert len(tables) == 2


def test_merge_when_continuation_drops_columns() -> None:
    """扫描续表常少 1～2 列（空列被吃掉），仍应合成一张。"""
    header = ["序号", "类型", "项目", "数量", "单位", "次数", "型号", "单价", "总价", "备注"]
    row = ["1", "离心机", "油过滤器", "1", "个", "1", "501", "600", "600", ""]
    cont = ["", "", "施工费用", "", "", "5000", "5000", ""]
    t1 = TableInput(body=_html([header, row]), caption="清单", page=1, table_id="1")
    t2 = TableInput(body=_html([cont, ["8", "", "合计", "", "", "", "17300", ""]]), page=2, table_id="2")
    _, tables, stats = merge_continued_tables("[表格 1]\n[表格 2]\n", [t1, t2])
    assert stats["tables_merged_pairs"] == 1
    assert len(tables) == 1
    assert "施工费用" in tables[0].body and "油过滤器" in tables[0].body


def test_merge_colspan_page_split() -> None:
    left = (
        "<table><tr><td colspan=\"2\">姓名</td><td>金额</td></tr>"
        "<tr><td colspan=\"2\">甲</td><td>100</td></tr></table>"
    )
    right = "<table><tr><td>乙</td><td></td><td>200</td></tr></table>"
    t1 = TableInput(body=left, caption="清单", page=1, table_id="1")
    t2 = TableInput(body=right, page=2, table_id="2")
    _, tables, stats = merge_continued_tables("[表格 1]\n[表格 2]\n", [t1, t2])
    assert stats["tables_merged_pairs"] == 1
    assert len(tables) == 1
    assert "乙" in tables[0].body and "甲" in tables[0].body
