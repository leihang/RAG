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
