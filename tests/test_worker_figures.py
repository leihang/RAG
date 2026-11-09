"""端到端：PPTX 里的图片**必须真的进库**（图块 + image_path）。

回归背景（用户实测）：一份 38 页、含 142 张图片（92 张唯一）的汇报 PPT，
解析后 `figures=92`、92 个 PNG 也落到了 `var/parse/<doc_id>/images/`，
但库里 **一个 figure 块都没有、`image_path` 全空** —— 因为 `ekbs/worker.py`
拼好了 `figures` 列表、还把它写进了日志，却**忘了传给 `ingest_markdown`**。
于是图片全被静默丢掉：磁盘上有、界面里看不见、`/assets/figure/{chunk_id}` 无块可指。

解析后先停在复核，图片必须进草稿 sidecar（确认通过时分块器只认这份清单）。
这个用例从"造一份真 pptx → 放进 raw → 入队 → worker 解析"整条走一遍，
只认结果：状态是 review，sidecar 里有 figure，且 image_path 指向落盘文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekbs.ingest.dir_import import enqueue_files, raw_root
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.ingest.scan_review import load_review_sidecar
from ekbs.worker import process_once
from tests.test_import_api import open_compliance


def _content_png(color: bytes, *, size: int = 160) -> bytes:
    """边长过装饰闸的正文图（1×1 会被 figure_quality 丢掉）。"""
    from io import BytesIO

    from PIL import Image

    r, g, b = color[0], color[1], color[2]
    image = Image.new("RGB", (size, size), (r, g, b))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR 关掉：单测不调 MinerU（要加载模型，几十秒一张图）。"""
    monkeypatch.setenv("EKBS_FIGURE_OCR", "0")


def _first_kb_id(runtime) -> int:
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT kb_id FROM ekbs_kb ORDER BY kb_id LIMIT 1").fetchone()
    return int(row["kb_id"])


async def test_pptx_with_picture_yields_figure_chunk(runtime, seeded, tmp_path: Path) -> None:
    runtime, _ = seeded
    open_compliance(runtime)

    pptx = pytest.importorskip("pptx")
    red = tmp_path / "red.png"
    red.write_bytes(_content_png(b"\xff\x00\x00"))
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "现场照片"
    picture = slide.shapes.add_picture(str(red), 0, 0)
    # 人工题注：只有"有文字"的图片才建索引块（chunker 的口径）
    picture._element._nvXxPr.cNvPr.set("descr", "8 月能耗现场照片")

    rel = "汇报/月度汇报.pptx"
    target = raw_root(runtime.settings) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(target))

    outcome = enqueue_files(
        runtime.settings, runtime.db, rel_paths=[rel],
        kb_id=_first_kb_id(runtime), level=1,
    )
    doc_id = int(outcome["enqueued"][0]["doc_id"])

    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    assert await process_once(runtime, pipeline) is True

    with runtime.db.sync_connect() as conn:
        status = str(
            conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
            ).fetchone()["status"]
        )
        chunks = conn.execute(
            "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]
    assert status == "review", f"确认前不应发布，实际 {status}"
    assert chunks == 0

    side = load_review_sidecar(runtime.settings.data_dir, doc_id)
    assert side.figures, "PPTX 里的图片没有写进复核草稿"
    row = side.figures[0]
    assert "8 月能耗现场照片" in str(row.get("caption") or "")
    image_path = str(row.get("image_path") or "")
    assert image_path.startswith("parse/"), image_path
    assert (runtime.settings.data_dir / image_path).is_file(), image_path
