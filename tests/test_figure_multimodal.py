"""figure 多模态旁路索引：入库双写 + 检索第三路 RRF。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ekbs.ingest.chunker import FigureInput
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.index.figure_faiss import FigureIndex
from ekbs.provider.mock import MockMultimodalEmbeddingProvider
from ekbs.retrieval.pipeline import RetrievalOverrides
from tests.conftest import user_id_of


def test_figure_index_upsert_search_roundtrip(tmp_path: Path) -> None:
    index = FigureIndex(tmp_path / "figures.faiss", dim=8)
    vectors = np.asarray([[1.0, 0, 0, 0, 0, 0, 0, 0], [0, 1.0, 0, 0, 0, 0, 0, 0]], dtype="float32")
    assert index.upsert(vectors, [101, 202]) == 2
    hits = index.search([1.0, 0, 0, 0, 0, 0, 0, 0], top_k=1)
    assert hits and hits[0][0] == 101
    assert index.remove([101]) == 1
    hits2 = index.search([1.0, 0, 0, 0, 0, 0, 0, 0], top_k=2)
    assert all(cid != 101 for cid, _ in hits2)


@pytest.mark.asyncio
async def test_mock_multimodal_aligns_stem_with_query(tmp_path: Path) -> None:
    mm = MockMultimodalEmbeddingProvider(dim=64)
    img = tmp_path / "电表更换.png"
    Image.new("RGB", (16, 16), color=(20, 120, 40)).save(img)
    q = await mm.embed_text("电表更换")
    v = await mm.embed_image(img)
    # 共享 stem token → 内积应明显大于垂直基向量
    score = float(np.dot(np.asarray(q), np.asarray(v)))
    assert score > 0.3


@pytest.mark.asyncio
async def test_ingest_writes_figure_multimodal_and_retrieve(seeded) -> None:
    runtime, summary = seeded
    assert runtime.providers.multimodal is not None
    assert runtime.figure_index is not None

    assets = runtime.settings.data_dir / "parse" / "mm_test" / "images"
    assets.mkdir(parents=True, exist_ok=True)
    image_name = "电表更换现场.png"
    Image.new("RGB", (32, 32), color=(180, 40, 40)).save(assets / image_name)
    image_rel = str((assets / image_name).relative_to(runtime.settings.data_dir))

    pipeline = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    before = runtime.figure_index.vector_count
    result = await pipeline.ingest_markdown(
        title="巡检图册",
        markdown="# 巡检\n\n现场更换记录见附图。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
        figures=(
            FigureInput(
                image_path=image_rel,
                caption="电表更换现场照片",
                ocr_text="旧表读数 12345",
                page=1,
                figure_id="图-电表",
            ),
        ),
    )
    assert result.figure_vector_count >= 1
    assert runtime.figure_index.vector_count >= before + 1

    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    # 关掉稀疏/文本稠密主导：仍应靠 MM 第三路捞到 figure
    retrieved = await runtime.pipeline.retrieve(
        "电表更换",
        alice,
        overrides=RetrievalOverrides(
            dense_top_k=5,
            sparse_top_k=5,
            multi_query=1,
            figure_multimodal=True,
        ),
        collect_debug=True,
    )
    assert "figure_mm" in retrieved.stage_debug
    assert retrieved.stage_debug["figure_mm"]["enabled"] is True
    figure_hits = {
        c.chunk_id
        for c in retrieved.chunks
        if c.chunk_type == "figure" and "电表" in (c.content or "")
    }
    # 至少 MM lane 有命中记录，或最终上下文含该 figure
    mm_ids = {h["chunk_id"] for h in retrieved.stage_debug["figure_mm"]["hits"]}
    assert mm_ids or figure_hits, (
        f"expected figure mm recall; refused={retrieved.refused} "
        f"debug={retrieved.stage_debug.get('figure_mm')}"
    )
