"""印章图丢弃：红章/淡红印去掉，二维码和整页正文留下。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ekbs.ingest.chunker import FigureInput
from ekbs.ingest.scan_review import (
    load_review_sidecar,
    prepare_scan_draft,
    save_review_draft,
    upgrade_review_draft_seals,
)
from ekbs.ingest.seal_filter import (
    is_seal_image,
    looks_like_seal,
    seal_filter_applies,
    strip_figure_anchors,
)


def _save(path: Path, arr: np.ndarray) -> None:
    Image.fromarray(arr.astype(np.uint8)).save(path)


def test_seal_filter_applies_only_scan_pdf_word() -> None:
    assert seal_filter_applies("合同.pdf")
    assert seal_filter_applies("制度.docx")
    assert seal_filter_applies("扫描件/说明.doc")
    assert seal_filter_applies("page.png")
    assert seal_filter_applies("x.unknown", engine="mineru")
    assert seal_filter_applies("资料/扫描件/纪要.txt")
    assert not seal_filter_applies("汇报.pptx")
    assert not seal_filter_applies("能耗.xlsx")
    assert not seal_filter_applies("deck.ppt", engine="pptx")
    assert not seal_filter_applies(None, engine="xlsx")
    assert seal_filter_applies(None, default_when_unknown=True)
    assert not seal_filter_applies(None)


def test_red_seal_dropped(tmp_path: Path) -> None:
    arr = np.full((200, 200, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:200, :200]
    circle = (xx - 100) ** 2 + (yy - 100) ** 2 < 70**2
    arr[circle] = (190, 30, 30)
    path = tmp_path / "seal.png"
    _save(path, arr)
    assert looks_like_seal(Image.open(path))
    assert is_seal_image(path)


def test_faint_red_seal_dropped(tmp_path: Path) -> None:
    arr = np.full((200, 200, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:200, :200]
    r = np.sqrt((xx - 100) ** 2 + (yy - 100) ** 2)
    ring = (r > 70) & (r < 78)
    arr[ring] = (250, 228, 228)
    path = tmp_path / "faint.png"
    _save(path, arr)
    assert looks_like_seal(Image.open(path))


def test_square_field_photo_with_warm_tones_kept(tmp_path: Path) -> None:
    """水印相机方图：满幅场景 + 暖色设备，不得当公章丢掉。

    回归：工程部 PPT 第 30 页化粪池检查照（756×756）曾被 ``_red_seal`` 淡红
    分支误杀，同页只剩小广告清除照，表现为「图与页文不对」。
    """
    rng = np.random.default_rng(0)
    # 满幅铺底（路面/墙），ink_frac ≈ 1
    arr = rng.integers(40, 120, size=(256, 256, 3), dtype=np.uint8)
    # 局部暖色（手持检测仪橙色外壳一类）
    arr[80:140, 90:150] = (230, 120, 40)
    # 角落少量近白（水印条）
    arr[0:24, 0:120] = (245, 245, 245)
    path = tmp_path / "field_square.png"
    _save(path, arr)
    assert not looks_like_seal(Image.open(path))


def test_chat_screenshot_with_white_ui_kept(tmp_path: Path) -> None:
    """钉钉白底截图（含粉边/头像暖色）不是公章。"""
    arr = np.full((240, 220, 3), 250, dtype=np.uint8)
    # 聊天气泡与正文块
    arr[40:100, 30:190] = (235, 235, 235)
    arr[110:200, 40:180] = (60, 70, 80)
    # 局部暖色（头像/按钮）
    arr[20:50, 20:50] = (240, 200, 190)
    path = tmp_path / "chat.png"
    _save(path, arr)
    assert not looks_like_seal(Image.open(path))


def test_qr_and_text_page_kept(tmp_path: Path) -> None:
    qr = np.full((200, 200, 3), 255, dtype=np.uint8)
    qr[20:180:8, 20:180] = (0, 0, 0)
    qr[20:180, 20:180:8] = (0, 0, 0)
    # 定位角：真二维码四角是黑块，公章四角是白纸
    qr[:36, :36] = (0, 0, 0)
    qr[:36, -36:] = (0, 0, 0)
    qr[-36:, :36] = (0, 0, 0)
    qr_path = tmp_path / "qr.png"
    _save(qr_path, qr)
    assert not looks_like_seal(Image.open(qr_path))

    page = np.full((900, 700, 3), 255, dtype=np.uint8)
    page[40:860:18, 40:660] = (20, 20, 20)
    yy, xx = np.ogrid[:900, :700]
    seal = (xx - 560) ** 2 + (yy - 720) ** 2 < 40**2
    page[seal] = (180, 20, 20)
    page_path = tmp_path / "page.png"
    _save(page_path, page)
    assert not looks_like_seal(Image.open(page_path), caption="盖有红色印章")


def test_prepare_draft_drops_seal_keeps_qr(tmp_path: Path) -> None:
    seal = np.full((160, 160, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:160, :160]
    seal[(xx - 80) ** 2 + (yy - 80) ** 2 < 50**2] = (200, 40, 40)
    qr = np.full((160, 160, 3), 255, dtype=np.uint8)
    qr[10:150:6, 10:150] = (0, 0, 0)
    qr[:28, :28] = (0, 0, 0)
    qr[:28, -28:] = (0, 0, 0)
    qr[-28:, :28] = (0, 0, 0)
    images = tmp_path / "parse" / "9" / "images"
    images.mkdir(parents=True)
    _save(images / "sealred.png", seal)
    _save(images / "qrcode1.png", qr)
    rel, stats = prepare_scan_draft(
        data_dir=tmp_path,
        doc_id=9,
        raw_markdown="正文\n[图片 sealred] 公章\n[图片 qrcode1]\n",
        engine="mineru",
        page_map=[(0, 1, 1)],
        figures=[
            FigureInput(
                image_path="parse/9/images/sealred.png",
                caption="公章",
                figure_id="sealred",
                page=2,
            ),
            FigureInput(
                image_path="parse/9/images/qrcode1.png",
                caption="二维码",
                figure_id="qrcode1",
                page=3,
            ),
        ],
        tables=[],
    )
    assert rel == "parse/9"
    assert stats.get("seal_figures_dropped") == 1
    side = load_review_sidecar(tmp_path, 9)
    ids = [item["figure_id"] for item in side.figures]
    assert ids == ["qrcode1"]
    text = (tmp_path / "parse" / "9" / "cleaned.md").read_text(encoding="utf-8")
    assert "[图片 sealred]" not in text
    assert "[图片 qrcode1]" in text


def test_upgrade_existing_draft_drops_keyword_seal(tmp_path: Path) -> None:
    save_review_draft(
        data_dir=tmp_path,
        doc_id=3,
        markdown="条款\n[图片 abcdef123456] 印章\n",
        figures=[
            FigureInput(
                image_path="parse/3/missing.png",
                caption="合同专用章",
                figure_id="abcdef123456",
            )
        ],
    )
    stats = upgrade_review_draft_seals(tmp_path, 3)
    assert stats.get("seal_figures_dropped") == 1
    assert load_review_sidecar(tmp_path, 3).figures == []
    assert "[图片 abcdef123456]" not in (tmp_path / "parse" / "3" / "cleaned.md").read_text(
        encoding="utf-8"
    )
    assert strip_figure_anchors("x\n[图片 abcdef123456]\ny\n", {"abcdef123456"}).strip() == "x\ny"
