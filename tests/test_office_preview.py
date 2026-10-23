"""办公原件转 HTML，供对照左栏内嵌。"""

from __future__ import annotations

from pathlib import Path

import docx

from ekbs.ingest.office_preview import render_office_preview


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
