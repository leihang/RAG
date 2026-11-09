"""扫描件 / PDF / Word 公章过滤。

MinerU 常把红章、浅章抽成独立图片。正文清洗只删「盖有红色印章」这类字，
图还在附图里。这里按题注/OCR 和画面把章丢掉，整页正文、二维码、示意图保留。

**适用范围**（见 :func:`seal_filter_applies`）：只对扫描件、PDF、Word。
PPT / Excel 多是现场照与截图，方图暖色会被误杀，入库管线不得对它们跑本过滤。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence, TypeVar

import numpy as np
from PIL import Image

_SEAL_WORD = re.compile(r"印章|公章|专用章|合同章|骑缝章")
_ANCHOR = re.compile(r"(?m)^\[图片\s+(\S+)\][^\n]*(?:\n|$)")

#: 公章过滤允许的后缀（与「扫描件」路径提示、MinerU 引擎并列）。
_SEAL_FILTER_SUFFIXES = frozenset({"pdf", "doc", "docx", "png", "jpg", "jpeg"})
#: 明确不跑公章过滤（现场照/截图为主）。
_SEAL_FILTER_SKIP_SUFFIXES = frozenset({"ppt", "pptx", "xls", "xlsx", "xlsm"})

T = TypeVar("T")


def seal_filter_applies(
    source_path: str | None,
    *,
    engine: str | None = None,
    title: str | None = None,
    default_when_unknown: bool = False,
) -> bool:
    """是否应对该来源的附图做公章丢弃。

    * **开**：PDF / Word / 独立图片（MinerU）/ 路径或标题标明扫描件；
    * **关**：PPT / Excel 等 —— 方形现场照、钉钉截图会被红章启发式误杀。
    * ``default_when_unknown``：复核旧草稿常缺 engine/路径，默认仍按扫描件处理。
    """
    from ekbs.ingest.parser import normalize_suffix
    from ekbs.ingest.scan_review import is_scan_source

    suffix = normalize_suffix(str(source_path or ""))
    eng = (engine or "").strip().lower()
    if suffix in _SEAL_FILTER_SKIP_SUFFIXES or eng in _SEAL_FILTER_SKIP_SUFFIXES:
        return False
    if suffix in _SEAL_FILTER_SUFFIXES:
        return True
    if eng == "mineru" or eng in {"doc", "docx"}:
        return True
    if is_scan_source(source_path, title):
        return True
    return bool(default_when_unknown)


def looks_like_seal(im: Image.Image, *, caption: str = "", ocr: str = "") -> bool:
    """红章 / 淡红印迹 / 题注写明印章 → True。整页黑字或黑白二维码 → False。"""
    arr = np.asarray(im.convert("RGB"))
    if arr.ndim != 3 or arr.shape[0] < 8 or arr.shape[1] < 8:
        return bool(_SEAL_WORD.search(f"{caption}\n{ocr}"))
    r = arr[..., 0].astype(np.int16)
    g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16)
    height, width = arr.shape[:2]
    aspect = width / height
    square = 0.82 <= aspect <= 1.22
    # 黑字、二维码模块是中性黑。整页正文通常不是方图，方图里的黑章要单独看圆印。
    neutral_black = (r < 70) & (g < 70) & (b < 70) & (np.abs(r - g) < 25) & (np.abs(r - b) < 25)
    if float(neutral_black.mean()) >= 0.04 and not square:
        return False
    if _SEAL_WORD.search(f"{caption}\n{ocr}"):
        return True
    near_white = (r >= 240) & (g >= 240) & (b >= 240)
    ink = ~near_white
    ink_frac = float(ink.mean())
    white_frac = float(near_white.mean())
    if square and _red_seal(r, g, b, ink):
        return True
    # 扫描件里红章经常被抽成灰度圆印：白底、四角空白、墨迹铺到外圈
    return bool(
        square
        and white_frac >= 0.72
        and 0.003 <= ink_frac <= 0.40
        and _corners_blank(near_white)
        and _ink_on_rim(ink)
    )


def _red_seal(r: np.ndarray, g: np.ndarray, b: np.ndarray, ink: np.ndarray) -> bool:
    ink_n = int(ink.sum()) or 1
    ink_frac = float(ink.mean())
    # 章是白纸上的红印。满幅现场照（水印相机方图、橙红设备/墙砖）ink≈1，
    # 旧逻辑把淡红阈值放在 ink 上限之外 → PPT 方形照片被当公章丢掉，同页只剩
    # 其它图，看起来像「图挂错页」。
    if ink_frac > 0.55:
        return False
    red = ink & (r > g + 12) & (r > b + 12) & (r > 60)
    if float(red.sum()) / ink_n >= 0.40 and ink_frac >= 0.008:
        return True
    # 淡红章墨面积极小（白底粉圈）；钉钉截图等 UI 白底 ink 常 >0.2，不能走这条。
    if ink_frac > 0.18:
        return False
    faint = (r >= 220) & (r > g + 6) & (r > b + 6) & (g >= 180)
    return float(faint.mean()) >= 0.004


def _corners_blank(near_white: np.ndarray) -> bool:
    height, width = near_white.shape
    margin = max(8, int(min(height, width) * 0.10))
    corners = (
        near_white[:margin, :margin],
        near_white[:margin, -margin:],
        near_white[-margin:, :margin],
        near_white[-margin:, -margin:],
    )
    return float(np.mean([float(part.mean()) for part in corners])) >= 0.82


def _ink_on_rim(ink: np.ndarray) -> bool:
    """墨迹铺到外圈（圆章），而不是缩在画面中心的小图标。"""
    ys, xs = np.nonzero(ink)
    if len(xs) < 40:
        return False
    cy = float(ys.mean())
    cx = float(xs.mean())
    rad = np.sqrt((ys - cy) ** 2 + (xs - cx) ** 2)
    height, width = ink.shape
    reach = min(height, width) / 2
    if float(np.percentile(rad, 80)) < 0.55 * reach:
        return False
    outer = float(np.mean(rad >= 0.62 * reach))
    return outer >= 0.25


def is_seal_image(
    path: Path | None,
    *,
    caption: str = "",
    ocr: str = "",
) -> bool:
    """有文件就看画面；打不开时只凭题注/OCR 里的印章字样。"""
    text_hit = bool(_SEAL_WORD.search(f"{caption}\n{ocr}"))
    if path is None or not Path(path).is_file():
        return text_hit
    try:
        with Image.open(path) as im:
            return looks_like_seal(im, caption=caption, ocr=ocr)
    except Exception:
        return text_hit


def strip_figure_anchors(markdown: str, figure_ids: set[str]) -> str:
    """删掉被丢掉的 ``[图片 stem]`` 行。锚点只用文件名 stem 的前 12 位。"""
    if not markdown or not figure_ids:
        return markdown or ""
    tokens: set[str] = set()
    for fid in figure_ids:
        fid = (fid or "").strip()
        if not fid:
            continue
        tokens.add(fid)
        tokens.add(fid[:12])

    def repl(match: re.Match[str]) -> str:
        if match.group(1) in tokens:
            return ""
        return match.group(0)

    text = _ANCHOR.sub(repl, markdown)
    return re.sub(r"\n{3,}", "\n\n", text)


def partition_seal_figures(
    figures: Sequence[T],
    data_dir: Path,
) -> tuple[list[T], list[T]]:
    """按 ``image_path``（相对 data_dir）把印章图分出去。"""
    kept: list[T] = []
    dropped: list[T] = []
    root = Path(data_dir)
    for fig in figures:
        rel = str(getattr(fig, "image_path", "") or "").strip()
        path = (root / rel) if rel else None
        if is_seal_image(
            path,
            caption=str(getattr(fig, "caption", "") or ""),
            ocr=str(getattr(fig, "ocr_text", "") or ""),
        ):
            dropped.append(fig)
        else:
            kept.append(fig)
    return kept, dropped
