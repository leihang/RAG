"""`.pptx` 的**图片抽取**（用户反馈："ppt 内有大量图片，没有被读出来"）。

背景（实测）：一份 22.5 MB / 38 页 / 含 142 张图片（92 张唯一）的工程汇报 PPT，
经 `_parse_pptx` 只抽出 **3151 字、`figures=0`** —— 因为那个分支只认
`has_text_frame` / `has_table`，对 `PICTURE` 形状完全视而不见。
"整页都是图"的汇报类 PPT 于是几乎等于空文档。

这里钉住四件事：

1. 图片**落盘**到 `assets_dir/images/img-<sha8>.<ext>`（图片只在 zip 容器里，
   不主动写出来就等于丢图）；
2. 同一张图重复出现只登记一次（实测该 PPT 里同一张图最多重复 **33 次**）；
3. **不往 markdown 里写 `![](..)` 链接** —— 图片块是从 `figures` 建的，
   而链接会原样进父/子块正文并算进向量（`chunk_markdown` 不做剥离）；
4. 递归进组合形状（GROUP）—— 只遍历 `slide.shapes` 会漏掉成组的图。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ekbs.ingest.parser import MSO_SHAPE_TYPE_GROUP, ParsedDocument, _iter_shapes, parse_file


def _tiny_png(color: bytes) -> bytes:
    """手工拼一个 1×1 RGB PNG（不引 Pillow：只为了给 python-pptx 一个真图片）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"\x00" + color  # 1 像素、filter=0
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def sample_pptx(tmp_path: Path) -> Path:
    """2 页、2 张唯一图片：红图在第 1 页出现 2 次、第 2 页再出现 1 次；蓝图只在第 2 页。"""
    pptx = pytest.importorskip("pptx")
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    red.write_bytes(_tiny_png(b"\xff\x00\x00"))
    blue.write_bytes(_tiny_png(b"\x00\x00\xff"))

    deck = pptx.Presentation()
    layout = deck.slide_layouts[5]  # 只要标题，避免占位符干扰
    first = deck.slides.add_slide(layout)
    first.shapes.title.text = "工程进度"
    first.shapes.add_picture(str(red), 0, 0)
    first.shapes.add_picture(str(red), 100, 0)  # 同一张图重复
    second = deck.slides.add_slide(layout)
    second.shapes.title.text = "现场照片"
    second.shapes.add_picture(str(blue), 0, 0)
    second.shapes.add_picture(str(red), 100, 0)  # 跨页重复

    path = tmp_path / "deck.pptx"
    deck.save(str(path))
    return path


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉 OCR：单测不调 MinerU（它要加载模型，几十秒一张图）。"""
    monkeypatch.setenv("EKBS_FIGURE_OCR", "0")


def test_pptx_extracts_pictures_to_disk_deduped(sample_pptx: Path, tmp_path: Path) -> None:
    assets = tmp_path / "parse" / "1"
    parsed = parse_file(sample_pptx, assets_dir=assets)

    assert isinstance(parsed, ParsedDocument)
    assert parsed.engine == "pptx"
    assert parsed.page_count == 2

    # 4 次 add_picture、只有 2 张唯一图 ⇒ 2 个图片块、2 个文件
    assert len(parsed.figures) == 2, [f.name for f in parsed.figures]
    written = sorted(p.name for p in (assets / "images").glob("*"))
    assert len(written) == 2, written
    assert all(name.startswith("img-") for name in written), written

    # 内容命名 = 去重的依据；同内容两次添加必须指向同一个文件
    names = [f.name for f in parsed.figures]
    assert len(set(names)) == 2
    for figure in parsed.figures:
        assert (assets / figure.name).is_file(), figure.name
        assert figure.figure_id == Path(figure.name).stem

    # 页码跟着图片所在的页走（红图第一次出现在第 1 页、蓝图在第 2 页）
    assert sorted(f.page for f in parsed.figures) == [1, 2]


def test_pptx_markdown_has_no_image_links(sample_pptx: Path, tmp_path: Path) -> None:
    """图片链接**不能**进 markdown：它会被算进父/子块正文与向量。"""
    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")
    assert "![" not in parsed.markdown, parsed.markdown
    assert "images/" not in parsed.markdown
    # 文本本身仍要抽出来
    assert "工程进度" in parsed.markdown and "现场照片" in parsed.markdown


def test_pptx_without_assets_dir_keeps_text_and_writes_nothing(sample_pptx: Path, tmp_path: Path) -> None:
    """没给持久目录（例如只想预览文本）时：不落盘、不报错，正文照旧。"""
    parsed = parse_file(sample_pptx)
    assert parsed.figures == []
    assert "工程进度" in parsed.markdown
    assert not (tmp_path / "images").exists()


def test_page_title_fallback_gives_textless_figures_search_text(sample_pptx: Path, tmp_path: Path) -> None:
    """无题注、无 OCR 的图 → 继承**所在页的标题**作为检索文本。

    实测背景：用户那份 38 页 PPT 里 92 张图，只有 6 张有 Alt Text（还是素材名），
    其余 86 张此前**完全不进索引** —— 搜什么都搜不到它们，图片处理等于摆设。
    兜底后按主题提问（如"用电量趋势"）能召回该页的图。
    """
    from pptx import Presentation

    deck = Presentation(str(sample_pptx))
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.shape_type == 13:  # PICTURE：清掉 python-pptx 自动写的文件名
                shape._element._nvXxPr.cNvPr.set("descr", "")
    deck.save(str(sample_pptx))

    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")
    # 两张图分别在"工程进度"/"现场照片"两页 → 各自继承页标题
    assert sorted(f.caption for f in parsed.figures) == ["工程进度", "现场照片"]

    from ekbs.ingest.chunker import chunk_markdown
    from ekbs.ingest.pipeline import FigureInput

    result = chunk_markdown(
        parsed.markdown, page_map=parsed.page_map,
        figures=[
            FigureInput(image_path=f"parse/1/{f.name}", caption=f.caption, ocr_text=f.ocr_text,
                        page=f.page, figure_id=f.figure_id)
            for f in parsed.figures
        ],
    )
    figure_nodes = [n for n in result.nodes if n.chunk_type == "figure"]
    assert len(figure_nodes) == 2, [(n.chunk_type, n.content) for n in result.nodes]
    assert all("图片" in n.content for n in figure_nodes)


def test_only_largest_textless_figure_per_page_gets_title(tmp_path: Path) -> None:
    """同一页有多张无文字图时，**只给最大的那张**补页标题。

    否则一页 6 张图就会造出 6 个内容几乎相同的块（都只有页标题），把检索结果挤满 ——
    `chunker` 当初跳过无文字图片，防的正是这件事。
    """
    import struct
    import zlib

    pptx = pytest.importorskip("pptx")

    def png(size_pad: int, color: bytes) -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        raw = b"\x00" + color + b"\x00" * size_pad  # 用填充把体积做大，模拟"大图"
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1 + size_pad // 3, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))

    small = tmp_path / "small.png"
    big = tmp_path / "big.png"
    small.write_bytes(png(100, b"\x00\xff\x00"))
    big.write_bytes(png(9000, b"\xff\x00\x00"))

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "现场照片"
    for shape in (slide.shapes.add_picture(str(small), 0, 0),
                  slide.shapes.add_picture(str(big), 100, 0)):
        shape._element._nvXxPr.cNvPr.set("descr", "")
    path = tmp_path / "two.pptx"
    deck.save(str(path))

    parsed = parse_file(path, assets_dir=tmp_path / "parse" / "1")
    captioned = [f for f in parsed.figures if f.caption]
    assert len(parsed.figures) == 2
    assert len(captioned) == 1, [(f.name, f.caption) for f in parsed.figures]
    assert captioned[0].caption == "现场照片"
    # 补的是"最大那张"：解析后落盘的文件名带内容哈希，比较文件大小确认
    sizes = {f.name: (tmp_path / "parse" / "1" / f.name).stat().st_size for f in parsed.figures}
    assert sizes[captioned[0].name] == max(sizes.values())


def test_iter_shapes_recurses_into_groups() -> None:
    """组合形状里的图片必须能被找到（python-pptx 没有 add_group 的公开 API，故用替身）。"""

    class Stub:
        def __init__(self, shape_type: int, children: tuple = ()) -> None:
            self.shape_type = shape_type
            self.shapes = list(children)

    top_picture = Stub(13)
    inner_picture = Stub(13)
    text_box = Stub(17)
    group = Stub(MSO_SHAPE_TYPE_GROUP, (inner_picture, text_box))

    got = list(_iter_shapes([top_picture, group]))
    assert got == [top_picture, group, inner_picture, text_box]


def test_ocr_capability_failure_stops_after_one_attempt(sample_pptx: Path, tmp_path: Path, monkeypatch) -> None:
    """OCR 不可用时要**一次就停**，不能对每张图都白跑一遍。

    实测故障：一份含 92 张图的 PPT，MinerU 装了但模型不可用 —— 每个调用要 10~120s
    才返回空，页面卡在 `parsing` 十几分钟，日志里却没有任何告警（原先"能力不可用"
    与"图里确实没字"都返回空串，调用方分不出来，只能一张张试）。
    """
    import ekbs.ingest.parser as parser

    monkeypatch.setenv("EKBS_FIGURE_OCR", "1")
    monkeypatch.setenv("EKBS_FIGURE_OCR_MAX", "12")
    calls: list[str] = []

    from ekbs.ingest.parser import OcrOutcome

    def fake_ocr(image):
        calls.append(image.name)
        return OcrOutcome(attempted=True, usable=False)  # 能力不可用

    monkeypatch.setattr(parser, "_ocr_figure", fake_ocr)
    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")

    # 2 张唯一图片，但只应尝试 1 次 OCR
    assert len(parsed.figures) == 2
    assert len(calls) == 1, f"OCR 不可用时不该继续尝试：{calls}"
    # 图片仍然照常落盘（OCR 只是加分项，不能因为 OCR 失败就丢图）
    assert len(list((tmp_path / "parse" / "1" / "images").glob("*"))) == 2


def test_ocr_text_is_used_when_available(sample_pptx: Path, tmp_path: Path, monkeypatch) -> None:
    """OCR 可用时，识别出的文字挂到图片块上（图片块的可检索文本）。"""
    import ekbs.ingest.parser as parser

    monkeypatch.setenv("EKBS_FIGURE_OCR", "1")
    monkeypatch.setenv("EKBS_FIGURE_OCR_MAX", "12")
    from ekbs.ingest.parser import OcrOutcome

    monkeypatch.setattr(
        parser, "_ocr_figure",
        lambda image: OcrOutcome(text="图中写着：8 月用电量", attempted=True, usable=True),
    )

    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")
    assert all(f.ocr_text == "图中写着：8 月用电量" for f in parsed.figures)


def test_ocr_budget_counts_attempts_not_successes(sample_pptx: Path, tmp_path: Path, monkeypatch) -> None:
    """预算是「允许调几次 MinerU」，不是「识别成功几次」。

    实测 MinerU 每次要加载模型、**~33 秒/张**；若按"有文字才扣预算"，一份 92 张图的 PPT
    在"每张都识别不出文字"时会一张接一张跑到底（用户实测卡了 4 分钟以上仍在跑、日志无声）。
    这里让 OCR 永远返回空文字，预算设 1：**只能尝试 1 次**。
    """
    import ekbs.ingest.parser as parser
    from ekbs.ingest.parser import OcrOutcome

    monkeypatch.setenv("EKBS_FIGURE_OCR", "1")
    monkeypatch.setenv("EKBS_FIGURE_OCR_MAX", "1")
    calls: list[str] = []

    def empty_ocr(image):
        calls.append(image.name)
        return OcrOutcome(text="", attempted=True, usable=True)  # 正常跑完，但图中没字

    monkeypatch.setattr(parser, "_ocr_figure", empty_ocr)
    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")

    assert len(parsed.figures) == 2
    assert len(calls) == 1, f"预算为 1 却尝试了 {len(calls)} 次：{calls}"


def test_small_images_do_not_consume_ocr_budget(sample_pptx: Path, tmp_path: Path, monkeypatch) -> None:
    """按尺寸主动跳过的小图不消耗预算（否则一页 logo 就能把预算吃光，真正有字的图轮不到）。"""
    import ekbs.ingest.parser as parser
    from ekbs.ingest.parser import OcrOutcome

    monkeypatch.setenv("EKBS_FIGURE_OCR", "1")
    monkeypatch.setenv("EKBS_FIGURE_OCR_MAX", "1")
    calls: list[str] = []

    def mixed(image):
        calls.append(image.name)
        # 第一次按"小图"跳过（attempted=False），第二次真的调了
        if len(calls) == 1:
            return OcrOutcome()
        return OcrOutcome(text="图里写着 8 月用电量", attempted=True, usable=True)

    monkeypatch.setattr(parser, "_ocr_figure", mixed)
    parsed = parse_file(sample_pptx, assets_dir=tmp_path / "parse" / "1")

    assert len(calls) == 2, calls
    assert any(f.ocr_text for f in parsed.figures), "跳过的小图不该吃掉预算"

