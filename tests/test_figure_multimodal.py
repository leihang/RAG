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
    assert index.contains([101, 202, 999]) == {101, 202}
    hits = index.search([1.0, 0, 0, 0, 0, 0, 0, 0], top_k=1)
    assert hits and hits[0][0] == 101
    assert index.remove([101]) == 1
    assert index.contains([101, 202]) == {202}
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


def _enable_figure_multimodal(runtime) -> FigureIndex:
    """可选旁路：测试显式打开 figure_multimodal 并注入 mock 多模态索引。"""
    runtime.settings.retrieval.figure_multimodal = True
    dim = int(runtime.settings.models.multimodal_embed.dim or 64)
    if runtime.providers.multimodal is None:
        runtime.providers.multimodal = MockMultimodalEmbeddingProvider(dim=dim)
    if runtime.figure_index is None:
        path = runtime.settings.data_dir / "index" / "figures-test.faiss"
        path.parent.mkdir(parents=True, exist_ok=True)
        runtime.figure_index = FigureIndex(path, dim=dim)
    runtime.pipeline.figure_index = runtime.figure_index
    return runtime.figure_index


@pytest.mark.asyncio
async def test_ingest_writes_figure_multimodal_when_enabled(seeded) -> None:
    """可选旁路：显式打开 figure_multimodal 时仍可写入图向量（默认生产关闭）。"""
    runtime, summary = seeded
    figure_index = _enable_figure_multimodal(runtime)

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
        figure_index=figure_index,
    )
    before = figure_index.vector_count
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
    assert figure_index.vector_count >= before + 1

    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
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
    # 新口径：figure 不进最终 keep（会被 prose 优先丢掉）；MM lane 仍可有命中记录
    mm_ids = {h["chunk_id"] for h in retrieved.stage_debug["figure_mm"]["hits"]}
    assert mm_ids or not retrieved.refused, (
        f"expected figure mm lane activity; refused={retrieved.refused} "
        f"debug={retrieved.stage_debug.get('figure_mm')}"
    )


@pytest.mark.asyncio
async def test_figure_embed_failure_is_retried_then_reported(seeded) -> None:
    """单张图的向量化失败**必须重试**；重试仍失败要进 `IngestResult`（不能只记一条 warning）。

    实测事故（2026-10-02，重灌 16 篇时）：`figure_mm_embed_failed chunk_id=4543
    error=HTTPSConnectionPool(host='dashscope.aliyuncs.com')` —— 旧实现一次失败就跳过，
    结果**图片进了库却没有向量**（「存了但搜不到」），文档照样 published，
    除人工比对文件之外没有任何信号（实测 2/201 张如此）。
    """
    runtime, summary = seeded
    _enable_figure_multimodal(runtime)
    assets = runtime.settings.data_dir / "parse" / "mm_retry" / "images"
    assets.mkdir(parents=True, exist_ok=True)
    name = "重试用例.png"
    Image.new("RGB", (16, 16), color=(10, 10, 200)).save(assets / name)
    rel = str((assets / name).relative_to(runtime.settings.data_dir))
    runtime.settings.models.multimodal_embed.retry_backoff_s = 0.0  # 测试不等退避
    dim = int(runtime.settings.models.multimodal_embed.dim)

    pipeline = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )

    class FlakyThenOK:
        ref = runtime.providers.multimodal.ref

        def __init__(self) -> None:
            self.calls = 0

        async def embed_image(self, path):
            self.calls += 1
            if self.calls < 3:  # 前两次网络失败
                raise RuntimeError("HTTPSConnectionPool: connection reset by peer")
            return [1.0] + [0.0] * (dim - 1)

        async def embed_text(self, text):
            return [0.0] * dim

    flaky = FlakyThenOK()
    original = runtime.providers.multimodal
    runtime.providers.multimodal = flaky  # type: ignore[assignment]
    try:
        result = await pipeline.ingest_markdown(
            title="重试用例.md",
            markdown="# 图\n\n见附图。\n",
            kb_id=int(summary["kb_id"]),
            level=1,
            visible_dept_ids=[],
            real_corpus=False,
            figures=(FigureInput(image_path=rel, caption="某图", ocr_text="", page=1),),
        )
    finally:
        runtime.providers.multimodal = original  # type: ignore[assignment]

    assert flaky.calls == 3, "前两次失败后应重试第三次并成功"
    assert result.figure_vector_count == 1
    assert result.figure_vectors_failed == 0


@pytest.mark.asyncio
async def test_figure_embed_permanent_failure_is_visible(seeded) -> None:
    """重试仍失败 → 失败数进结果（可观测），文档不因此整篇失败。"""
    runtime, summary = seeded
    _enable_figure_multimodal(runtime)
    assets = runtime.settings.data_dir / "parse" / "mm_fail" / "images"
    assets.mkdir(parents=True, exist_ok=True)
    name = "永久失败.png"
    Image.new("RGB", (16, 16), color=(200, 10, 10)).save(assets / name)
    rel = str((assets / name).relative_to(runtime.settings.data_dir))
    runtime.settings.models.multimodal_embed.retry_backoff_s = 0.0

    pipeline = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    dim = int(runtime.settings.models.multimodal_embed.dim)

    class AlwaysFail:
        ref = runtime.providers.multimodal.ref

        def __init__(self) -> None:
            self.calls = 0

        async def embed_image(self, path):
            self.calls += 1
            raise RuntimeError("429 Too Many Requests")

        async def embed_text(self, text):
            return [0.0] * dim

    bad = AlwaysFail()
    original = runtime.providers.multimodal
    runtime.providers.multimodal = bad  # type: ignore[assignment]
    try:
        result = await pipeline.ingest_markdown(
            title="失败可见用例.md",
            markdown="# 图\n\n见附图。\n",
            kb_id=int(summary["kb_id"]),
            level=1,
            visible_dept_ids=[],
            real_corpus=False,
            figures=(FigureInput(image_path=rel, caption="某图", ocr_text="", page=1),),
        )
    finally:
        runtime.providers.multimodal = original  # type: ignore[assignment]

    assert bad.calls == int(runtime.settings.models.multimodal_embed.max_retries)
    assert result.figure_vectors_failed == 1, "失败必须可观测（否则就是静默的「存了但搜不到」）"
    assert result.status == "published", "图片旁路失败不该让整篇文档失败"
