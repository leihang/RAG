"""多种**放置方式**与**方向**下的图/表抽取（用户要求：都应能识别）。

覆盖：
1. PPT 竖向画布（slide_height > slide_width）；
2. PPT 形状旋转 90°（手机竖放照片常见）；
3. PPT 图表 → ParsedTable；
4. EXIF Orientation 矫正（落盘后直立）；
5. Word VML ``v:imagedata``（旧式 pict，zip 注入夹具）。
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from ekbs.ingest.parser import parse_file


def _tiny_png(color: bytes = b"\xff\x00\x00", *, width: int = 1, height: int = 1) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + color * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKBS_FIGURE_OCR", "0")


def test_pptx_portrait_slide_marked_and_picture_extracted(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    png = tmp_path / "v.png"
    # 边长须 ≥ decorative 阈值，否则入库质量门会当 logo 丢掉
    png.write_bytes(_tiny_png(b"\x00\xff\x00", width=80, height=160))

    deck = pptx.Presentation()
    # 竖向画布：高 > 宽
    deck.slide_width, deck.slide_height = deck.slide_height, deck.slide_width
    assert deck.slide_height > deck.slide_width
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(0.5), Inches(0.2), Inches(3), Inches(0.5)).text_frame.text = "竖版封面"
    slide.shapes.add_picture(str(png), Inches(0.5), Inches(1))
    path = tmp_path / "portrait.pptx"
    deck.save(str(path))

    assets = tmp_path / "parse" / "p"
    parsed = parse_file(path, assets_dir=assets)
    assert "（竖向）" in parsed.markdown
    assert "竖版封面" in parsed.markdown
    assert len(parsed.figures) >= 1
    assert (assets / parsed.figures[0].name).is_file()


def test_pptx_rotated_picture_persisted_upright(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches
    from PIL import Image

    # 横图，形状旋转 90° → 落盘后应变竖（高≥宽）；边长过装饰闸
    png = tmp_path / "wide.png"
    png.write_bytes(_tiny_png(b"\xff\x00\x00", width=160, height=40))

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    pic = slide.shapes.add_picture(str(png), Inches(1), Inches(1))
    pic.rotation = 90.0
    path = tmp_path / "rotated.pptx"
    deck.save(str(path))

    assets = tmp_path / "parse" / "r"
    parsed = parse_file(path, assets_dir=assets)
    assert parsed.figures
    stored = Image.open(assets / parsed.figures[0].name)
    assert stored.size[1] >= stored.size[0], stored.size


def test_pptx_chart_becomes_parsed_table(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(0.3), Inches(0.2), Inches(4), Inches(0.4)).text_frame.text = "能耗"
    data = CategoryChartData()
    data.categories = ["7月", "8月"]
    data.add_series("用电", (100, 120))
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1), Inches(5), Inches(3), data
    )
    path = tmp_path / "chart.pptx"
    deck.save(str(path))

    parsed = parse_file(path, assets_dir=tmp_path / "parse" / "c")
    assert parsed.tables, "图表应升为 ParsedTable"
    body = parsed.tables[0].body
    assert "7月" in body and "120" in body
    assert "[表格 1]" in parsed.markdown


def test_exif_orientation_normalized_on_persist(tmp_path: Path) -> None:
    """手机竖拍 JPEG：像素横向存储 + EXIF Orientation=6 → 落盘后直立。"""
    from PIL import Image

    # 宽>高的像素，标记为 Orientation 6（显示时应旋转为高>宽）；边长过装饰闸
    img = Image.new("RGB", (200, 80), (0, 128, 255))
    buf = io.BytesIO()
    # Pillow 用 exif 字典写 Orientation
    exif = img.getexif()
    exif[274] = 6  # Orientation
    img.save(buf, format="JPEG", exif=exif)
    jpeg = buf.getvalue()

    md = tmp_path / "note.md"
    photo = tmp_path / "phone.jpg"
    photo.write_bytes(jpeg)
    md.write_text("现场：![竖拍](phone.jpg)\n", encoding="utf-8")

    assets = tmp_path / "parse" / "exif"
    parsed = parse_file(md, assets_dir=assets)
    assert len(parsed.figures) == 1
    stored = Image.open(assets / parsed.figures[0].name)
    assert stored.size[1] > stored.size[0], stored.size


def test_docx_vml_imagedata_extracted(tmp_path: Path) -> None:
    """旧式 ``w:pict`` / ``v:imagedata`` 也应抽到（zip 注入夹具）。"""
    docx = pytest.importorskip("docx")
    png_bytes = _tiny_png(b"\x11\x22\x33", width=160, height=120)

    document = docx.Document()
    document.add_paragraph("正文")
    base = tmp_path / "vml_base.docx"
    document.save(str(base))

    rid = "rIdVml1"
    out = tmp_path / "vml.docx"
    with zipfile.ZipFile(base, "r") as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                if "image_vml.png" not in text:
                    text = text.replace(
                        "</Types>",
                        '<Override PartName="/word/media/image_vml.png" '
                        'ContentType="image/png"/></Types>',
                    )
                data = text.encode("utf-8")
            elif item.filename == "word/document.xml":
                text = data.decode("utf-8")
                pict = (
                    '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                    'xmlns:v="urn:schemas-microsoft-com:vml" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    "<w:r><w:pict>"
                    f'<v:shape style="width:10pt;height:10pt"><v:imagedata r:id="{rid}"/></v:shape>'
                    "</w:pict></w:r></w:p>"
                )
                text = text.replace("</w:body>", pict + "</w:body>")
                data = text.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                text = data.decode("utf-8")
                text = text.replace(
                    "</Relationships>",
                    f'<Relationship Id="{rid}" '
                    f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="media/image_vml.png"/></Relationships>',
                )
                data = text.encode("utf-8")
            dst.writestr(item, data)
        dst.writestr("word/media/image_vml.png", png_bytes)

    assets = tmp_path / "parse" / "vml"
    parsed = parse_file(out, assets_dir=assets)
    assert parsed.figures, "VML imagedata 应被抽出"
    assert any((assets / f.name).is_file() for f in parsed.figures)
