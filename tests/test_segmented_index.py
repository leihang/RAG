"""方案 B / P0：分段索引 manifest 与单文件迁移的测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekbs.config import IndexCfg
from ekbs.errors import EmbeddingFingerprintMismatch
from ekbs.index.segmented import (
    MANIFEST_NAME,
    SegmentInfo,
    ShardManifest,
    load_manifest,
    manifest_path,
    migrate_single_to_segments,
    save_manifest,
    validate_manifest,
)


def test_index_config_defaults_and_validation() -> None:
    cfg = IndexCfg()
    assert cfg.layout == "single"
    assert cfg.writer == "worker"
    assert cfg.segment.batch_max_vectors > 0
    with pytest.raises(ValueError):
        IndexCfg(layout="bad")
    with pytest.raises(ValueError):
        IndexCfg(writer="bad")


def test_manifest_roundtrip_and_atomic_write(tmp_path: Path) -> None:
    target = tmp_path / "shard"
    manifest = ShardManifest(
        shard_id="kb_all_L1",
        dim=1024,
        embedding_fingerprint="fp",
        version=3,
        segments=[SegmentInfo(id="seg_base_0001", path="seg_base_0001.faiss", count=10)],
    )
    save_manifest(target, manifest)
    assert not (target / "manifest.json.tmp").exists()
    loaded = load_manifest(target)
    assert loaded is not None
    assert loaded.version == 3
    assert loaded.segments[0].count == 10
    assert json.loads(manifest_path(target).read_text())["shard_id"] == "kb_all_L1"


def test_missing_manifest_returns_none(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "none") is None


def test_validate_manifest_rejects_fingerprint_mismatch() -> None:
    manifest = ShardManifest(shard_id="s", dim=1024, embedding_fingerprint="a")
    validate_manifest(manifest, dim=1024, embedding_fingerprint="a")
    with pytest.raises(EmbeddingFingerprintMismatch):
        validate_manifest(manifest, dim=1024, embedding_fingerprint="b")
    with pytest.raises(EmbeddingFingerprintMismatch):
        validate_manifest(manifest, dim=512, embedding_fingerprint="a")


def _make_faiss_file(path: Path, dim: int = 8, n: int = 3) -> None:
    faiss = pytest.importorskip("faiss")
    import numpy as np

    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    vectors = np.random.default_rng(0).random((n, dim)).astype("float32")
    index.add_with_ids(vectors, np.arange(100, 100 + n, dtype="int64"))
    faiss.write_index(index, str(path))


def test_migrate_dry_run_does_not_write(tmp_path: Path) -> None:
    src = tmp_path / "kb_all_L1.faiss"
    _make_faiss_file(src)
    target = tmp_path / "shards" / "kb_all_L1"
    plan = migrate_single_to_segments(
        shard_id="kb_all_L1", single_faiss=src, target_dir=target,
        dim=8, embedding_fingerprint="fp", dry_run=True,
    )
    assert plan["vector_count"] == 3
    assert not target.exists()
    assert src.exists()


def test_migrate_apply_copies_base_segment(tmp_path: Path) -> None:
    src = tmp_path / "kb_all_L1.faiss"
    _make_faiss_file(src)
    target = tmp_path / "shards" / "kb_all_L1"
    plan = migrate_single_to_segments(
        shard_id="kb_all_L1", single_faiss=src, target_dir=target,
        dim=8, embedding_fingerprint="fp", dry_run=False,
    )
    assert plan["written"] is True
    assert (target / "seg_base_0001.faiss").exists()
    assert src.exists(), "迁移只复制，不删除原文件"
    manifest = load_manifest(target)
    assert manifest is not None
    assert manifest.segments[0].count == 3
    assert (target / MANIFEST_NAME).exists()
