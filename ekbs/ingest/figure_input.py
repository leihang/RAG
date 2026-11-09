"""ParsedFigure → FigureInput：入库入口共用的落盘路径换算。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ekbs.ingest.chunker import FigureInput
from ekbs.ingest.parser import ParsedFigure


def figure_inputs_from_parsed(
    figures: Sequence[ParsedFigure],
    *,
    data_dir: Path,
    assets_dir: Path,
    captions: Mapping[str, str] | None = None,
) -> list[FigureInput]:
    """把解析层报回的图换成相对 ``data_dir`` 的 :class:`FigureInput`。

    ``captions`` 优先于 ``ParsedFigure.caption``（视觉题注覆盖 Alt Text）。
    """
    caps = captions or {}
    return [
        FigureInput(
            image_path=str((assets_dir / item.name).relative_to(data_dir)),
            caption=caps.get(item.name) or item.caption,
            ocr_text=item.ocr_text,
            page=item.page,
            figure_id=item.figure_id,
        )
        for item in figures
    ]
