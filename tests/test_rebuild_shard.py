"""分片重建（`ekbs.tools.rebuild_shard`）的回归用例。"""

from __future__ import annotations

from ekbs.tools.rebuild_shard import rebuild


async def test_rebuild_empty_shard_in_segmented_layout(seeded) -> None:
    """0 块分片也要能重建。

    回归：分段分支里 `index.add(np.asarray([]))` 的形状是 (0,)，会撞上
    "向量形状 (0,) 与分片维度 1024 不符" —— 必须先判空再 add。
    """
    runtime, _summary = seeded
    runtime.settings.index.layout = "segmented"
    runtime.shards.create_shard("kb_empty_L2")

    result = await rebuild(runtime, "kb_empty_L2")

    assert result["chunks"] == 0
    assert result["vectors"] == 0
