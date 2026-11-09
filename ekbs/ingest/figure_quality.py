"""图片入库 / 回源共用的**确定性**质量门（装饰小图、近空白壳页）。

规则（所有格式共用，不按单篇文档或单条问句特判）：

1. **装饰小图**：``max(宽, 高) < DECORATIVE_MAX_EDGE`` → 不进 figure、不回源。
   页眉 logo / 角标常见 50×40、89×26；有题注也不能放行。
2. **近空白壳页**：大图但颜色极单调（封面底、分隔页）→ 不进 figure。
   判定只看像素统计，不看问句。

回源侧再挡一层：旧索引里已入库的装饰图仍可被 ``is_decorative_image_path`` 丢掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

#: 短边与长边都偏小的栅格视为装饰（与历史 docx 页眉阈值同口径）
DECORATIVE_MAX_EDGE = 120

#: 近空白壳：先缩到 64×64 再统计；须同时「够大」+「极单调」
#: 阈值避开普通插图（如 800×600 实心占位），对准整页封面底（≥1000 长边）
_SHELL_SAMPLE = 64
_SHELL_MIN_LONG_EDGE = 1000
_SHELL_MIN_SHORT_EDGE = 700
_SHELL_MAX_UNIQUE = 96
_SHELL_MIN_DOMINANT_RATIO = 0.55


def is_decorative_size(width: int, height: int, *, max_edge: int = DECORATIVE_MAX_EDGE) -> bool:
    """像素尺寸是否属于页眉 logo / 角标一类装饰小图。"""
    return max(int(width), int(height)) < int(max_edge)


def is_decorative_raster(
    blob: bytes, *, max_edge: int = DECORATIVE_MAX_EDGE
) -> bool:
    """字节流是否装饰小图；读不了尺寸时保守返回 False。"""
    size = _image_size_from_blob(blob)
    if size is None:
        return False
    return is_decorative_size(size[0], size[1], max_edge=max_edge)


def is_near_blank_shell_raster(blob: bytes) -> bool:
    """大面积近纯色壳页（封面底、分隔页）：无信息量，入库只会污染挂图。"""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # pragma: no cover
        return False
    try:
        with Image.open(BytesIO(blob)) as image:
            width, height = int(image.size[0]), int(image.size[1])
            long_edge, short_edge = max(width, height), min(width, height)
            if long_edge < _SHELL_MIN_LONG_EDGE or short_edge < _SHELL_MIN_SHORT_EDGE:
                return False
            sample = image.convert("RGB").resize(
                (_SHELL_SAMPLE, _SHELL_SAMPLE), Image.Resampling.BILINEAR
            )
            counts: dict[tuple[int, int, int], int] = {}
            for pixel in sample.getdata():
                counts[pixel] = counts.get(pixel, 0) + 1  # type: ignore[index]
    except Exception:  # noqa: BLE001
        return False
    if not counts:
        return False
    unique = len(counts)
    dominant = max(counts.values()) / float(_SHELL_SAMPLE * _SHELL_SAMPLE)
    return unique <= _SHELL_MAX_UNIQUE and dominant >= _SHELL_MIN_DOMINANT_RATIO


def should_drop_figure_bytes(blob: bytes) -> str | None:
    """入库门：返回丢弃原因；``None`` 表示可登记为 figure。"""
    if not blob:
        return "empty"
    if is_decorative_raster(blob):
        return "decorative_logo"
    if is_near_blank_shell_raster(blob):
        return "near_blank_shell"
    return None


def is_decorative_image_path(image_path: str | Path) -> bool:
    """回源门：已落盘路径是否装饰小图（旧索引未洗净时的第二道闸）。"""
    path = _resolve_figure_path(image_path)
    if path is None:
        return False
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        return False
    try:
        with Image.open(path) as image:
            width, height = image.size
        return is_decorative_size(width, height)
    except Exception:  # noqa: BLE001
        return False


def should_drop_figure_path(image_path: str | Path) -> str | None:
    """对已落盘图做与 ``should_drop_figure_bytes`` 同口径的判定。"""
    path = _resolve_figure_path(image_path)
    if path is None:
        return None
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    return should_drop_figure_bytes(blob)


def _image_size_from_blob(blob: bytes) -> tuple[int, int] | None:
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # pragma: no cover
        return None
    try:
        with Image.open(BytesIO(blob)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:  # noqa: BLE001
        return None


def _resolve_figure_path(image_path: str | Path) -> Path | None:
    raw = Path(str(image_path or "").strip())
    if not str(raw):
        return None
    candidates = [raw]
    if not raw.is_absolute():
        candidates.append(Path("var") / raw)
        candidates.append(Path("var/parse") / raw.name)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _open_rgb(stream: BinaryIO):  # pragma: no cover - helper for future callers
    from PIL import Image

    return Image.open(stream).convert("RGB")
