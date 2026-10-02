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


def test_docx_extracts_header_media_via_zip_fallback(tmp_path: Path) -> None:
    """页眉图不在 document.part.rels —— 必须靠 word/media zip 兜底（实测交接班制度）。"""
    import zipfile

    docx = pytest.importorskip("docx")
    png = tmp_path / "logo.png"
    png.write_bytes(_tiny_png(b"\xaa\xbb\xcc"))

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
