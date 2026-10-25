"""方案 B / P1b：分段 manager 的写入、冻结、跨段检索与持久化。"""

from __future__ import annotations

import numpy as np

from ekbs.index.faiss_store import ShardIndexManager


def _manager(runtime) -> ShardIndexManager:
    settings = runtime.settings
    settings.index.layout = "segmented"
    settings.index.segment.batch_max_vectors = 2
    settings.index.segment.personal_max_vectors = 1
    return ShardIndexManager(
        runtime.db, settings, dim=8, fingerprint="test-fingerprint"
    )


def _vectors(n: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.random((n, 8)).astype("float32")


def test_segmented_append_search_and_freeze(runtime) -> None:
    manager = _manager(runtime)
    manager.create_shard("kb_all_L1")
    vectors = _vectors(5)
    manager.append("kb_all_L1", [101, 102], vectors[:2])
    # batch_max_vectors=2 → 第一次追加就冻结为 base 段
    manager.append("kb_all_L1", [103, 104, 105], vectors[2:])

    hits = manager.search("kb_all_L1", vectors[2], top_k=3)
    assert hits and hits[0][0] == 103
    assert len({cid for cid, _ in hits}) == len(hits)

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT vector_count FROM ekbs_shard WHERE shard_id = 'kb_all_L1'"
        ).fetchone()
    assert int(row["vector_count"]) == 5


def test_segmented_persistence_reload(runtime) -> None:
    manager = _manager(runtime)
    manager.create_shard("kb_all_L1")
    vectors = _vectors(3)
    manager.append("kb_all_L1", [201, 202, 203], vectors, personal=True)
    manager.flush_all()

    reloaded = ShardIndexManager(
        runtime.db, runtime.settings, dim=8, fingerprint="test-fingerprint"
    )
    hits = reloaded.search("kb_all_L1", vectors[0], top_k=3)
    assert hits and hits[0][0] == 201


def test_segmented_remove(runtime) -> None:
    manager = _manager(runtime)
    manager.create_shard("kb_all_L1")
    vectors = _vectors(3)
    manager.append("kb_all_L1", [301, 302, 303], vectors)
    removed = manager.remove("kb_all_L1", [302])
    assert removed == 1
    ids = [cid for cid, _ in manager.search("kb_all_L1", vectors[1], top_k=5)]
    assert 302 not in ids


def test_compaction_merges_small_segments(runtime) -> None:
    manager = _manager(runtime)
    manager.settings.index.segment.batch_max_vectors = 1
    manager.settings.index.segment.compaction_max_segments = 2
    manager.create_shard("kb_all_L1")
    vectors = _vectors(3)
    for i in range(3):
        manager.append("kb_all_L1", [401 + i], vectors[i : i + 1])

    shard = manager._get_segmented("kb_all_L1")
    assert len(shard.segments) >= 3
    before = manager.search("kb_all_L1", vectors[0], top_k=3)
    result = manager.compact("kb_all_L1")
    assert result["compacted"] is True
    shard = manager._get_segmented("kb_all_L1")
    assert len(shard.segments) <= 2
    after = manager.search("kb_all_L1", vectors[0], top_k=3)
    assert [cid for cid, _ in after] == [cid for cid, _ in before]
