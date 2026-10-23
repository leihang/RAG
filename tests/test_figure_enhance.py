"""浅色印章预览增强。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ekbs.ingest.figure_enhance import classify_figure, enhance_figure_file, enhance_figure_image


def _faint_seal(path: Path) -> None:
    """白底 + 极淡圆环（模拟浅色公章）。"""
    arr = np.full((200, 200, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:200, :200]
    r = np.sqrt((xx - 100) ** 2 + (yy - 100) ** 2)
    ring = (r > 70) & (r < 78)
    arr[ring] = (248, 248, 248)  # 几乎看不见
    Image.fromarray(arr).save(path)


def test_classify_faint_seal(tmp_path: Path) -> None:
    p = tmp_path / "seal.jpg"
    _faint_seal(p)
    meta = classify_figure(p)
    assert meta.kind in ("faint", "blank")


def test_enhance_makes_ink_visible(tmp_path: Path) -> None:
    p = tmp_path / "seal.jpg"
    _faint_seal(p)
    data, media, meta = enhance_figure_file(p, slate_bg=True)
    assert media == "image/jpeg"
    assert meta.enhanced is True
    assert len(data) > 200
    out = Image.open(__import__("io").BytesIO(data)).convert("RGB")
    arr = np.asarray(out)
    # 深色底：平均亮度应明显低于白纸
    assert float(arr.mean()) < 120
    # 应出现偏红的墨迹像素
    redish = (arr[..., 0] > arr[..., 1] + 20) & (arr[..., 0] > 80)
    assert float(redish.mean()) > 0.01


def test_normal_photo_not_forced_to_slate(tmp_path: Path) -> None:
    p = tmp_path / "photo.jpg"
    arr = np.zeros((80, 80, 3), dtype=np.uint8)
    arr[:40] = (30, 120, 200)
    arr[40:] = (200, 80, 40)
    Image.fromarray(arr).save(p)
    out, meta = enhance_figure_image(Image.open(p), slate_bg=True)
    assert meta.kind == "normal"
    # 不应整图变成 slate 灰
    assert float(np.asarray(out).mean()) > 40
