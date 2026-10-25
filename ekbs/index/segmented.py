"""分段向量索引的 manifest 与迁移（方案 B 的 P0）。

设计背景
--------
现有实现每个分片只有一个 ``.faiss`` 文件，``ShardIndexManager.append()`` 每灌一篇就
``index.save()`` 全量重写该文件（O(N²) 写放大，8 万篇时索引 500MB+、速率掉到 3 篇/s）。

方案 B 把分片拆成"不可变段 + 活跃段"：新向量只进活跃段，活跃段写盘一次后冻结，
检索跨段归并 top-k，后台 compaction 合并小段。本模块只负责 P0 的落盘格式：

* ``manifest.json``：段清单（权威），原子替换；
* ``migrate_single_to_segments()``：把现有单文件安全复制为 ``seg_base_0001.faiss``
  并生成 manifest（**只复制不删除**，切换由 P1 灰度开关控制）；
* 校验 embedding fingerprint / 维度，拒绝新旧向量混用。

P0 不改变任何检索行为（配置默认 ``index.layout=single``）。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ekbs.errors import EmbeddingFingerprintMismatch

MANIFEST_NAME = "manifest.json"
DEFAULT_SEGMENT_NAME = "seg_base_0001.faiss"


@dataclass
class SegmentInfo:
    """一个不可变段（对应一个 ``.faiss`` 文件）。"""

    id: str
    path: str
    count: int = 0
    created_at: str = ""
    mode: str = "batch"  # batch | personal


@dataclass
class ShardManifest:
    """分片 manifest：段清单 + 指纹 + 版本。"""

    shard_id: str
    dim: int
    embedding_fingerprint: str
    version: int = 1
    segments: list[SegmentInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "dim": self.dim,
            "embedding_fingerprint": self.embedding_fingerprint,
            "version": self.version,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShardManifest:
        try:
            segments = [SegmentInfo(**s) for s in data.get("segments", [])]
            return cls(
                shard_id=str(data["shard_id"]),
                dim=int(data["dim"]),
                embedding_fingerprint=str(data["embedding_fingerprint"]),
                version=int(data.get("version", 1)),
                segments=segments,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"manifest 格式非法：{exc}") from exc


def manifest_path(target_dir: Path) -> Path:
    return Path(target_dir) / MANIFEST_NAME


def load_manifest(target_dir: Path) -> ShardManifest | None:
    """读 manifest；不存在返回 None（调用方按旧单文件布局处理）。"""
    path = manifest_path(target_dir)
    if not path.exists():
        return None
    return ShardManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_manifest(target_dir: Path, manifest: ShardManifest) -> Path:
    """原子写 manifest：``manifest.json.tmp`` → fsync → rename。"""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(target_dir)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def validate_manifest(
    manifest: ShardManifest,
    *,
    dim: int,
    embedding_fingerprint: str,
) -> None:
    """维度/指纹不一致时拒绝加载（00 §2.3 / C3：禁止新旧向量混用）。"""
    if manifest.dim != dim:
        raise EmbeddingFingerprintMismatch(
            f"{manifest.shard_id} manifest 维度 {manifest.dim} ≠ 当前 {dim}"
        )
    if manifest.embedding_fingerprint != embedding_fingerprint:
        raise EmbeddingFingerprintMismatch(
            f"{manifest.shard_id} manifest 嵌入指纹 {manifest.embedding_fingerprint[:12]}… "
            f"≠ 当前 {embedding_fingerprint[:12]}…"
        )


def _faiss_vector_count(path: Path) -> int:
    import faiss  # 延迟导入：只有迁移/对账时才需要

    return int(faiss.read_index(str(path)).ntotal)


def migrate_single_to_segments(
    *,
    shard_id: str,
    single_faiss: Path,
    target_dir: Path,
    dim: int,
    embedding_fingerprint: str,
    vector_count: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """把现有单文件迁移为 ``seg_base_0001.faiss`` + manifest。

    * 只**复制**，不移动/不删除原文件（灰度切换前旧布局仍可用）；
    * ``dry_run=True`` 只返回计划，不落盘；
    * ``vector_count`` 未给时读 FAISS 文件（需 faiss）。
    """
    single_faiss = Path(single_faiss)
    target_dir = Path(target_dir)
    if not single_faiss.exists():
        raise FileNotFoundError(f"单文件分片不存在：{single_faiss}")
    dest = target_dir / DEFAULT_SEGMENT_NAME
    count = int(vector_count) if vector_count is not None else _faiss_vector_count(single_faiss)
    manifest = ShardManifest(
        shard_id=shard_id,
        dim=int(dim),
        embedding_fingerprint=str(embedding_fingerprint),
        segments=[
            SegmentInfo(
                id="seg_base_0001",
                path=DEFAULT_SEGMENT_NAME,
                count=count,
                mode="batch",
            )
        ],
    )
    plan = {
        "shard_id": shard_id,
        "dry_run": dry_run,
        "source": str(single_faiss),
        "target_dir": str(target_dir),
        "segment": str(dest),
        "vector_count": count,
        "manifest": manifest.to_dict(),
    }
    if dry_run:
        return plan

    target_dir.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copyfile(single_faiss, dest)
    save_manifest(target_dir, manifest)
    plan["written"] = True
    return plan
