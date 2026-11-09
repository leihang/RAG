"""图片质量门：装饰小图 / 近空白壳页（入库与回源共用口径）。"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ekbs.ingest.figure_quality import (
    DECORATIVE_MAX_EDGE,
    is_decorative_raster,
    is_near_blank_shell_raster,
    should_drop_figure_bytes,
)


def _png(width: int, height: int, rgb: bytes = b"\xaa\xbb\xcc") -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + rgb * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_decorative_logo_dropped_by_size() -> None:
    logo = _png(50, 40)
    keep = _png(400, 300)
    assert is_decorative_raster(logo) is True
    assert is_decorative_raster(keep) is False
    assert should_drop_figure_bytes(logo) == "decorative_logo"
    assert should_drop_figure_bytes(keep) is None
    assert max(50, 40) < DECORATIVE_MAX_EDGE


def test_near_blank_shell_large_flat_page() -> None:
    """整页近纯色封面底应丢；普通插图尺寸的实心图不走壳页规则。"""
    shell = _png(1200, 1600, b"\xac\x8d\x8a")
    assert is_near_blank_shell_raster(shell) is True
    assert should_drop_figure_bytes(shell) == "near_blank_shell"
    mid_flat = _png(800, 600, b"\xac\x8d\x8a")
    assert is_near_blank_shell_raster(mid_flat) is False
    small_flat = _png(200, 200, b"\xac\x8d\x8a")
    assert is_near_blank_shell_raster(small_flat) is False


def test_noisy_content_image_kept(tmp_path: Path) -> None:
    """颜色多样的内容图不得被壳页规则误杀。"""
    from PIL import Image
    import random

    random.seed(0)
    im = Image.new("RGB", (1054, 534))
    px = im.load()
    assert px is not None
    for y in range(534):
        for x in range(1054):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    path = tmp_path / "map.png"
    im.save(path)
    blob = path.read_bytes()
    assert is_near_blank_shell_raster(blob) is False
    assert should_drop_figure_bytes(blob) is None
