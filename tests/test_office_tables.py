"""Office 表格 → ``ParsedTable``（不摊进 parent/child，升为 ``chunk_type=table``）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekbs.ingest.chunker import TableInput, chunk_markdown
from ekbs.ingest.parser import parse_file


def test_docx_tables_become_parsed_table(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("差旅标准", level=1)
    document.add_paragraph("以下为住宿标准。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "档次"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "一档"
    table.cell(1, 1).text = "500"
    path = tmp_path / "policy.docx"
    document.save(str(path))

    parsed = parse_file(path)
    assert parsed.engine == "docx"
    assert len(parsed.tables) == 1
    assert "一档" in parsed.tables[0].body
    assert "[表格 1]" in parsed.markdown
    assert "| 一档 |" not in parsed.markdown  # 不进正文双索引

    result = chunk_markdown(
        parsed.markdown,
        tables=[
            TableInput(
                body=t.body, caption=t.caption, page=t.page, table_id=t.table_id
            )
            for t in parsed.tables
        ],
        doc_title="差旅标准",
    )
    tables = [n for n in result.nodes if n.chunk_type == "table"]
    assert len(tables) == 1
    assert "档次=一档" in tables[0].content
    assert "金额=500" in tables[0].content
    # 表格块**不得**带 figure_id：那会让前端把表格当图片渲染（取不到图 → "图不可用"）。
    # 原本这里塞的是 table_id 计数器（1、2、3…），线上 164 个表格块全中。
    assert tables[0].figure_id is None
    assert not tables[0].image_path


def test_xlsx_sheets_become_parsed_tables(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "住宿"
    ws.append(["城市", "限额"])
    ws.append(["北京", "600"])
    path = tmp_path / "rates.xlsx"
    wb.save(str(path))

    parsed = parse_file(path)
    assert parsed.engine == "xlsx"
    assert len(parsed.tables) == 1
    assert parsed.tables[0].caption == "住宿"
    assert "[表格 1]" in parsed.markdown
    assert "北京" in parsed.tables[0].body
    assert "| 北京 |" not in parsed.markdown


def test_pptx_tables_become_parsed_table(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "预算表"
    rows, cols = 2, 2
    shape = slide.shapes.add_table(rows, cols, Inches(1), Inches(2), Inches(4), Inches(1.5))
    table = shape.table
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "差旅"
    table.cell(1, 1).text = "3000"
    path = tmp_path / "budget.pptx"
    deck.save(str(path))

    parsed = parse_file(path)
    assert len(parsed.tables) == 1
    assert "差旅" in parsed.tables[0].body
    assert parsed.tables[0].page == 1
    assert "[表格 1]" in parsed.markdown
