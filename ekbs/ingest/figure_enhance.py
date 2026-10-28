"""解析附图（尤其是浅色印章）的显示增强。

扫描合同抽出的公章经常是「白底 + 极淡红/灰点」，默认白底缩略图看起来像白纸。
这里只做**预览增强**（不改落盘原图）：抬升墨迹、可选深色底，方便复核对照。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFilter

Kind = Literal["normal", "faint", "blank"]


@dataclass(frozen=True)
class FigureDisplayMeta:
    kind: Kind
    ink_frac: float
    enhanced: bool


def _ink_frac(gray: Image.Image) -> float:
    hist = gray.histogram()
    total = sum(hist) or 1
    # 非近白像素占比
    return sum(hist[:245]) / total


def classify_figure(path: Path | Image.Image) -> FigureDisplayMeta:
    im = path if isinstance(path, Image.Image) else Image.open(path)
    gray = im.convert("L")
    ink = _ink_frac(gray)
    hist = gray.histogram()
    total = sum(hist) or 1
    near_white = sum(hist[245:]) / total
    # 极淡：几乎全白且墨迹极少（勿用灰度 span——彩色实心块 JPEG 后 span 也会很窄）
    if near_white > 0.98 and ink < 0.01:
        kind: Kind = "blank"
    elif near_white > 0.88 or ink < 0.10:
        kind = "faint"
    else:
        kind = "normal"
    return FigureDisplayMeta(kind=kind, ink_frac=ink, enhanced=False)


def enhance_figure_image(
    im: Image.Image,
    *,
    slate_bg: bool = True,
) -> tuple[Image.Image, FigureDisplayMeta]:
    """返回适合复核预览的 RGB 图。"""
    import numpy as np

    rgb = im.convert("RGB")
    meta = classify_figure(rgb)
    arr = np.asarray(rgb, dtype=np.float32)
    g = arr.mean(axis=2)
    ink = 255.0 - g

    if meta.kind == "blank":
        # 仍做一次抬升，避免「完全看不见」；并标 blank。
        # 用 np.max(..., initial=0.0) 而非 ink.max()：后者走 numpy 的 _NoValue sentinel，
        # 在 coverage 等重入场景下 C 层会把 sentinel 当数值 float() 而抛 TypeError。
        mx = float(np.max(ink, initial=0.0)) or 1.0
        m = np.power(np.clip(ink / mx, 0, 1), 0.5)
    elif meta.kind == "faint":
        mx = float(np.max(ink, initial=0.0)) or 1.0
        mask = np.power(np.clip(ink / mx, 0, 1), 0.35)
        bin_im = Image.fromarray((mask * 255).astype("uint8"), mode="L")
        bin_im = bin_im.point(lambda x: 255 if x > 18 else 0)
        bin_im = bin_im.filter(ImageFilter.MaxFilter(5))
        bin_im = bin_im.filter(ImageFilter.GaussianBlur(0.9))
        m = np.asarray(bin_im).astype(np.float32) / 255.0
        m = np.clip(np.maximum(m, mask * 1.6), 0, 1)
    else:
        # 正常图：轻度 autocontrast 即可
        from PIL import ImageOps

        out = ImageOps.autocontrast(rgb, cutoff=0.5)
        if slate_bg:
            # 正常图不强制换底，避免毁彩色图
            return out, FigureDisplayMeta(kind="normal", ink_frac=meta.ink_frac, enhanced=True)
        return out, FigureDisplayMeta(kind="normal", ink_frac=meta.ink_frac, enhanced=True)

    if slate_bg:
        slate = np.array([40.0, 44.0, 52.0], dtype=np.float32)
        red = np.array([230.0, 90.0, 90.0], dtype=np.float32)
        ink3 = m[..., None]
        composed = slate * (1.0 - ink3) + red * ink3
        out_im = Image.fromarray(composed.astype("uint8"), mode="RGB")
    else:
        out = np.empty_like(arr)
        out[..., 0] = 255 - m * 170
        out[..., 1] = 255 - m * 230
        out[..., 2] = 255 - m * 230
        out_im = Image.fromarray(out.astype("uint8"), mode="RGB")

    return out_im, FigureDisplayMeta(kind=meta.kind, ink_frac=meta.ink_frac, enhanced=True)


def enhance_figure_file(
    path: Path,
    *,
    slate_bg: bool = True,
    quality: int = 90,
) -> tuple[bytes, str, FigureDisplayMeta]:
    """读盘增强，返回 (jpeg_bytes, media_type, meta)。"""
    with Image.open(path) as im:
        out, meta = enhance_figure_image(im, slate_bg=slate_bg)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg", meta
