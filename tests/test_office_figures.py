"""Office / Markdown **图片抽取**补缺（与 ``test_pptx_figures`` 同口径）。

背景：pptx / MinerU 已能落盘 ``figures``，但 docx / xlsx / md 原先只抽正文/表 ——
用户反馈「每种类型都应有图/表处理」。这里钉住：

1. ``.docx`` 嵌入图 → ``assets_dir/images/img-*.png`` + ``ParsedFigure``；
2. ``.xlsx`` 嵌入图（``sheet._images`` / ``xl/media``）同样落盘；
3. ``.md`` 本地 ``![](…)`` → figure，正文改短锚点（不把链接喂进向量）；
4. 没给 ``assets_dir`` 时不落盘、不报错（预览场景）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ekbs.ingest.parser import ParsedDocument, parse_file


def _tiny_png(color: bytes = b"\xff\x00\x00") -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"\x00" + color
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKBS_FIGURE_OCR", "0")


def _png_bytes(width: int, height: int, rgb: bytes = b"\xaa\xbb\xcc") -> bytes:
    """任意尺寸实心 PNG（页眉装饰小图 vs 可保留图的对照用）。"""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + (rgb * width)
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_docx_extracts_header_media_via_zip_fallback(tmp_path: Path) -> None:
    """页眉**较大**图不在 document.part.rels —— 必须靠 word/media / header part 兜底。

    装饰小 logo（短边 <120）现改为跳过进 figure，避免污染检索；本夹具用 160×160。
    """
    import zipfile

    docx = pytest.importorskip("docx")
    png = tmp_path / "logo.png"
    png.write_bytes(_png_bytes(160, 160, b"\xaa\xbb\xcc"))

    document = docx.Document()
    document.add_paragraph("正文无图")
    # 往页眉塞图
    header = document.sections[0].header
    header.paragraphs[0].add_run().add_picture(str(png))
    path = tmp_path / "header.docx"
    document.save(str(path))

    with zipfile.ZipFile(path) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/") and not n.endswith("/")]
    assert media, "夹具应写出 word/media"

    assets = tmp_path / "parse" / "hdr"
    parsed = parse_file(path, assets_dir=assets)
    assert len(parsed.figures) >= 1
    assert (assets / parsed.figures[0].name).is_file()
    assert "正文无图" in parsed.markdown


def test_docx_preserves_header_control_fields(tmp_path: Path) -> None:
    """制度 Word 页眉管控表（文件名/编号/页数）必须进文首「文件信息」，不能只抽 logo。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("1. 目的")
    document.add_paragraph("满足公共设备管理需要。")
    table = document.sections[0].header.add_table(rows=2, cols=4, width=docx.shared.Cm(16))
    cells = [
        ["文件名称", "设备编号作业指导书", "文件编号：XJWY-GC-02", "文件编号：XJWY-GC-02"],
        ["生效日期", "2024年4月1日", "页    数： 1 / 2", "页    数： 1 / 2"],
    ]
    for ri, row in enumerate(cells):
        for ci, text in enumerate(row):
            table.rows[ri].cells[ci].text = text
    # 装饰小 logo：不应进 figures
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(_png_bytes(40, 20))
    document.sections[0].header.paragraphs[0].add_run().add_picture(str(tiny))

    path = tmp_path / "control.docx"
    document.save(str(path))
    assets = tmp_path / "parse" / "ctrl"
    parsed = parse_file(path, assets_dir=assets)

    # 页眉管控信息是**文档元数据**：进 header_meta，不再作为「文件信息」正文块落库。
    assert parsed.header_meta["文件名称"] == "设备编号作业指导书"
    assert parsed.header_meta["文件编号"] == "XJWY-GC-02"
    assert parsed.header_meta["生效日期"] == "2024年4月1日"
    assert parsed.header_meta["页数"] == "1 / 2"
    assert "文件信息" not in parsed.markdown
    assert "设备编号作业指导书" not in parsed.markdown
    assert "XJWY-GC-02" not in parsed.markdown
    assert "2024年4月1日" not in parsed.markdown
    assert parsed.page_count == 2
    assert parsed.figures == [], "页眉装饰小图不应进 figure"
    assert "1. 目的" in parsed.markdown


def test_doc_binary_scan_skips_decorative_logo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.doc`` textutil 丢图后的二进制扫描也必须跳过页眉 logo（89×26）。"""
    import shutil
    import subprocess

    from ekbs.ingest import parser as parser_mod
    from ekbs.ingest.parser import ParsedDocument, _docx_raster_is_decorative

    logo = _png_bytes(89, 26)
    keep = _png_bytes(400, 300)
    assert _docx_raster_is_decorative(logo) is True
    assert _docx_raster_is_decorative(keep) is False

    fake = tmp_path / "record.doc"
    fake.write_bytes(b"OLE-FAKE")
    empty = ParsedDocument(markdown="设施设备维保记录\n", engine="docx", source_path=str(fake))

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/textutil")

    def _run(cmd: list[str], **_kwargs: object) -> object:
        out = Path(cmd[cmd.index("-output") + 1])
        out.write_bytes(b"PK")
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(parser_mod, "_parse_docx", lambda _path, *, assets_dir=None: empty)
    monkeypatch.setattr(
        parser_mod,
        "_scan_embedded_raster_blobs",
        lambda _data: [(logo, "png"), (keep, "png")],
    )

    assets = tmp_path / "parse" / "doc58"
    parsed = parser_mod._parse_doc(fake, assets_dir=assets)
    assert parsed.engine == "doc"
    assert len(parsed.figures) == 1, [f.name for f in parsed.figures]
    # name 形如 images/img-xxxxxxxx.png（相对 assets_dir）
    disk = assets / parsed.figures[0].name
    assert disk.is_file(), disk
    from PIL import Image

    with Image.open(disk) as im:
        assert im.size == (400, 300)


def test_docx_extracts_embedded_pictures(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    red.write_bytes(_tiny_png(b"\xff\x00\x00"))
    blue.write_bytes(_tiny_png(b"\x00\x00\xff"))

    document = docx.Document()
    document.add_heading("现场照片", level=1)
    document.add_paragraph("更换前：")
    document.add_picture(str(red))
    document.add_paragraph("更换后：")
    document.add_picture(str(blue))
    document.add_picture(str(red))  # 去重
    path = tmp_path / "meter.docx"
    document.save(str(path))

    assets = tmp_path / "parse" / "1"
    parsed = parse_file(path, assets_dir=assets)

    assert isinstance(parsed, ParsedDocument)
    assert parsed.engine == "docx"
    assert len(parsed.figures) == 2, [f.name for f in parsed.figures]
    assert all((assets / f.name).is_file() for f in parsed.figures)
    assert "![" not in parsed.markdown
    assert "现场照片" in parsed.markdown


def test_docx_without_assets_dir_keeps_text(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    img = tmp_path / "x.png"
    img.write_bytes(_tiny_png())
    document = docx.Document()
    document.add_paragraph("只有正文")
    document.add_picture(str(img))
    path = tmp_path / "preview.docx"
    document.save(str(path))

    parsed = parse_file(path)
    assert parsed.figures == []
    assert "只有正文" in parsed.markdown


def test_xlsx_extracts_embedded_pictures(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.drawing.image import Image as XLImage

    png = tmp_path / "chart.png"
    png.write_bytes(_tiny_png(b"\x00\xff\x00"))

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "用电"
    ws.append(["月份", "读数"])
    ws.append(["8月", "120"])
    ws.add_image(XLImage(str(png)), "C2")
    path = tmp_path / "meter.xlsx"
    wb.save(str(path))

    assets = tmp_path / "parse" / "2"
    parsed = parse_file(path, assets_dir=assets)

    assert parsed.engine == "xlsx"
    assert len(parsed.tables) == 1
    assert parsed.figures, "xlsx 嵌入图应进 figures"
    assert all((assets / f.name).is_file() for f in parsed.figures)
    assert any(f.caption == "用电" for f in parsed.figures)


def test_markdown_local_images_and_pipe_tables(tmp_path: Path) -> None:
    img = tmp_path / "photo.png"
    img.write_bytes(_tiny_png(b"\x11\x22\x33"))
    md = tmp_path / "note.md"
    md.write_text(
        "# 巡检\n\n"
        "见表：\n\n"
        "| 项 | 值 |\n"
        "| --- | --- |\n"
        "| 电表 | A1 |\n\n"
        "现场：![电表更换照片](photo.png)\n",
        encoding="utf-8",
    )

    assets = tmp_path / "parse" / "3"
    parsed = parse_file(md, assets_dir=assets)

    assert parsed.engine == "text"
    assert len(parsed.tables) == 1
    assert "电表" in parsed.tables[0].body
    assert "[表格 1]" in parsed.markdown
    assert "| 电表 |" not in parsed.markdown
    assert len(parsed.figures) == 1
    assert (assets / parsed.figures[0].name).is_file()
    assert parsed.figures[0].caption == "电表更换照片"
    assert "![" not in parsed.markdown
    assert "photo.png" not in parsed.markdown


def test_markdown_skips_remote_images(tmp_path: Path) -> None:
    md = tmp_path / "remote.md"
    md.write_text("![x](https://example.com/a.png)\n", encoding="utf-8")
    assets = tmp_path / "parse" / "4"
    parsed = parse_file(md, assets_dir=assets)
    assert parsed.figures == []
    assert "https://example.com/a.png" in parsed.markdown
