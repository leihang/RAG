"""ParsedFigure → FigureInput 公共转换。"""

from __future__ import annotations

from pathlib import Path

from ekbs.ingest.figure_input import figure_inputs_from_parsed
from ekbs.ingest.parser import ParsedFigure


def test_figure_inputs_from_parsed_caption_fallback_and_relative_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assets_dir = data_dir / "parse" / "42"
    assets_dir.mkdir(parents=True)
    (assets_dir / "img-a.png").write_bytes(b"x")

    figures = [
        ParsedFigure(name="img-a.png", caption="alt", ocr_text="ocr", page=3, figure_id="f1"),
        ParsedFigure(name="missing.png", caption="keep-alt", figure_id="f2"),
    ]
    out = figure_inputs_from_parsed(
        figures,
        data_dir=data_dir,
        assets_dir=assets_dir,
        captions={"img-a.png": "vision-caption"},
    )
    assert len(out) == 2
    assert out[0].image_path == "parse/42/img-a.png"
    assert out[0].caption == "vision-caption"
    assert out[0].ocr_text == "ocr"
    assert out[0].page == 3
    assert out[0].figure_id == "f1"
    assert out[1].caption == "keep-alt"
    assert out[1].image_path == "parse/42/missing.png"
