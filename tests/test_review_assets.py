"""复核稿里的表格和图片可以改、可以删，图片路径不能被换掉。"""

from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.scan_review import (
    load_review_markdown,
    load_review_sidecar,
    save_review_asset_edits,
    save_review_draft,
)


def test_edit_table_and_drop_figure(tmp_path) -> None:
    save_review_draft(
        data_dir=tmp_path,
        doc_id=7,
        markdown="[表格 1]\n[图片 deadbeef01] 旧题注\n[图片 cafebabe02]\n",
        engine="mineru",
        page_map=[(0, 10, 1)],
        tables=[TableInput(body="<table>旧</table>", caption="", page=1, table_id="1")],
        figures=[
            FigureInput(image_path="parse/7/images/deadbeef01.jpg", caption="旧题注", page=2, figure_id="deadbeef01"),
            FigureInput(image_path="parse/7/images/cafebabe02.jpg", caption="", page=3, figure_id="cafebabe02"),
        ],
    )
    save_review_asset_edits(
        tmp_path,
        7,
        "[表格 1] 报价\n[图片 cafebabe02] 平面\n",
        tables=[{"table_id": "1", "caption": "报价", "body_html": "<table>新</table>", "page": 1}],
        figures=[{"figure_id": "cafebabe02", "caption": "平面", "ocr_text": "一层"}],
    )
    text = load_review_markdown(tmp_path, 7)
    side = load_review_sidecar(tmp_path, 7)
    assert "报价" in text
    assert "deadbeef01" not in text
    assert side.tables[0]["body"] == "<table>新</table>"
    assert side.tables[0]["caption"] == "报价"
    assert [item["figure_id"] for item in side.figures] == ["cafebabe02"]
    assert side.figures[0]["image_path"] == "parse/7/images/cafebabe02.jpg"
    assert side.figures[0]["ocr_text"] == "一层"
    assert side.figures[0]["page"] == 3
    assert side.page_map == [[0, 10, 1]]


def test_unknown_figure_is_rejected(tmp_path) -> None:
    save_review_draft(
        data_dir=tmp_path,
        doc_id=7,
        markdown="[图片 deadbeef01]\n",
        figures=[FigureInput(image_path="parse/7/a.jpg", figure_id="deadbeef01")],
    )
    try:
        save_review_asset_edits(
            tmp_path,
            7,
            "[图片 deadbeef01]\n",
            figures=[{"figure_id": "not-a-real-id", "caption": "x"}],
        )
    except ValueError as exc:
        assert "未知图片" in str(exc)
    else:
        raise AssertionError("应当拒绝未知图片")
