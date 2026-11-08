"""办公原件转 HTML，供对照左栏内嵌。"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import docx
import pytest

from ekbs.ingest.office_preview import OfficePreviewError, render_office_preview


def test_docx_preview_without_textutil(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("ekbs.ingest.office_preview.shutil.which", lambda _name: None)
    path = tmp_path / "指导.docx"
    document = docx.Document()
    document.add_paragraph("满足公共设备、设施管理的需要")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "代码"
    table.cell(0, 1).text = "设备类别"
    document.save(path)

    html = render_office_preview(path)
    assert "满足公共设备、设施管理的需要" in html
    assert "设备类别" in html
    assert "<table>" in html


def test_preview_strips_script(tmp_path: Path) -> None:
    path = tmp_path / "note.html"
    path.write_text("<p onclick=\"alert(1)\">正文</p><script>alert(1)</script>", encoding="utf-8")
    html = render_office_preview(path)
    assert "正文" in html
    assert "<script" not in html.lower()
    assert "onclick" not in html.lower()


def test_xlsx_preview_preserves_percent_and_merged_cells(tmp_path: Path) -> None:
    """原件预览也必须按 Excel 显示格式：百分比、金额、合并单元格。"""
    from openpyxl import Workbook

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
    path = tmp_path / "preview.xlsx"
    wb.save(path)

    html = render_office_preview(path)
    assert "-5.14%" in html
    assert "-17.19%" in html
    assert "4379405.00" in html
    assert 'colspan="4"' in html
    assert "0.0513530345122631" not in html


def test_pptx_preview_allows_over_30mb_slide_deck(tmp_path: Path) -> None:
    """工程汇报 PPT 常超 30MB；预览只抽文字，应放行到 80MB。"""
    path = tmp_path / "汇报.pptx"
    # 最小合法 pptx + 填充到 >30MB，模拟带底图的大原件
    with ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml"
    ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml"
    ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="ppt/presentation.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "ppt/presentation.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>""",
        )
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    Target="slides/slide1.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "ppt/slides/slide1.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr/>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="标题"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr/>
      <p:txBody><a:bodyPr/><a:lstStyle/>
        <a:p><a:r><a:t>工程部十月工作汇报</a:t></a:r></a:p>
      </p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>""",
        )
        zf.writestr("ppt/media/pad.bin", b"\0" * (31 * 1024 * 1024))

    assert path.stat().st_size > 30 * 1024 * 1024
    html = render_office_preview(path)
    assert "工程部十月工作汇报" in html


def test_pptx_preview_rejects_over_80mb(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "过大.pptx"
    path.write_bytes(b"PK")  # 后缀够用；尺寸由 stub 伪造
    monkeypatch.setattr(Path, "stat", lambda self: type("S", (), {"st_size": 81 * 1024 * 1024})())
    with pytest.raises(OfficePreviewError, match="80MB"):
        render_office_preview(path)
