"""原始 Office → PDF 渲染（复核页左栏）的缓存/失败边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekbs.ingest import office_render


def test_render_original_pdf_caches_and_invalidates(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "原件.xlsx"
    source.write_bytes(b"xlsx")
    cache = tmp_path / "render"
    calls: list[Path] = []

    def fake_convert(src: Path, output_dir: Path):
        calls.append(src)
        pdf = output_dir / f"{src.stem}.pdf"
        pdf.write_bytes(b"%PDF-1.7 fake")
        return pdf

    monkeypatch.setattr(office_render, "_convert_to_pdf", fake_convert)
    first = office_render.render_original_pdf(source, cache, doc_id=42)
    second = office_render.render_original_pdf(source, cache, doc_id=42)
    assert first == second
    assert first.read_bytes().startswith(b"%PDF-")
    assert len(calls) == 1
    stamp = cache / "42.json"
    assert json.loads(stamp.read_text(encoding="utf-8"))["size"] == 4

    source.write_bytes(b"xlsx-v2")
    office_render.render_original_pdf(source, cache, doc_id=42)
    assert len(calls) == 2


def test_render_without_libreoffice_is_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(office_render, "_libreoffice_binary", lambda: None)
    source = tmp_path / "原件.xlsx"
    source.write_bytes(b"xlsx")
    with pytest.raises(office_render.OfficeRenderError, match="未安装 LibreOffice"):
        office_render.render_original_pdf(source, tmp_path / "render", doc_id=7)
