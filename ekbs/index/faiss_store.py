"""FAISS 分片索引（`06` §2.3/§2.4、`docs/spec/20` §5）。

要点：

* 度量：``IndexFlatIP`` + 写入前 ``faiss.normalize_L2`` ⇒ 内积即余弦（`06` §2.3）；
* 一个 ``(部门 × 密级)`` 一个索引文件 ``index/kb_<dept>_L<level>.faiss``；
* ``ekbs_chunk.faiss_row`` ↔ ``index`` 内行号一一对应；``ekbs_shard.vector_count`` 是登记值，
  启动自检与 ``smoke.sh`` 都拿它与 ``idx.ntotal`` 比对；
* **换嵌入模型 = 重建**（C4「旋转而非重建」）：从 ``ekbs_chunk.content`` 重新生成向量，
  不重新解析原文；
* 重建/重载用 ``.faiss.new`` + 原子 ``os.replace``，避免读到半个索引（`06` §2.4）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import EmbeddingFingerprintMismatch, SchemaError
from ekbs.index.shard import make_shard_id, parse_shard_id
from ekbs.logging import get_logger

log = get_logger(component="faiss")


def _import_faiss() -> Any:
    try:
        import faiss
    except Exception as exc:  # pragma: no cover - 依赖缺失时的清晰报错
        raise SchemaError(f"faiss 不可用：{exc}（Apple Silicon 只装 faiss-cpu，见 01 §3）") from exc
    return faiss


def normalize(vectors: np.ndarray) -> np.ndarray:
    """L2 归一化（写入与查询两侧都要做，否则内积不等于余弦）。"""
    faiss = _import_faiss()
    array = np.ascontiguousarray(vectors, dtype="float32")
    faiss.normalize_L2(array)
    return array


@dataclass(frozen=True)
class ShardRow:
    shard_id: str
    dept_code: str
    level: int
    faiss_path: str
    dim: int
    embedding_fingerprint: str
    vector_count: int
    index_type: str
    status: str


class ShardIndex:
    """单个分片索引的封装（内存中的 faiss index + 元数据）。"""

    def __init__(
        self,
        shard_id: str,
        path: Path,
        dim: int,
        fingerprint: str,
        index: Any,
    ) -> None:
        self.shard_id = shard_id
        self.path = Path(path)
        self.dim = int(dim)
        self.fingerprint = fingerprint
        self.index = index
        #: 加载时索引文件的 (mtime_ns, size)：用于**跨进程**失效判断（见 ShardIndexManager.get）
        self.file_stamp: tuple[int, int] | None = None

    def stat_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    @property
    def vector_count(self) -> int:
        return int(self.index.ntotal)

    @classmethod
    def create(cls, shard_id: str, path: Path, dim: int, fingerprint: str) -> "ShardIndex":
        faiss = _import_faiss()
        index = faiss.IndexFlatIP(int(dim))
        return cls(shard_id, path, dim, fingerprint, index)

    @classmethod
    def load(
        cls, shard_id: str, path: Path, *, expected_dim: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> "ShardIndex":
        faiss = _import_faiss()
        if not Path(path).is_file():
            raise SchemaError(f"分片索引文件缺失：{path}（shard_id={shard_id}）")
        index = faiss.read_index(str(path))
        if expected_dim is not None and int(index.d) != int(expected_dim):
            raise EmbeddingFingerprintMismatch(
                f"{shard_id} 维度不一致：索引 d={index.d}，当前模型 dim={expected_dim}"
            )
        return cls(shard_id, Path(path), int(index.d), expected_fingerprint or "", index)

    def add(self, vectors: np.ndarray) -> list[int]:
        """追加向量，返回它们在索引中的行号（即 ``faiss_row``）。"""
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"向量形状 {vectors.shape} 与分片维度 {self.dim} 不符")
        start = int(self.index.ntotal)
        self.index.add(normalize(vectors))
        return list(range(start, start + vectors.shape[0]))

    def search(self, vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """返回 ``[(faiss_row, score), …]``，score 越大越相关。"""
        if self.index.ntotal == 0 or top_k <= 0:
            return []
        query = normalize(np.asarray([vector], dtype="float32"))
        scores, rows = self.index.search(query, min(int(top_k), int(self.index.ntotal)))
        return [
            (int(row), float(score))
            for row, score in zip(rows[0], scores[0])
            if int(row) >= 0
        ]

    def save(self, path: Path | None = None, *, atomic: bool = True) -> None:
        faiss = _import_faiss()
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if atomic:
            tmp = target.with_suffix(target.suffix + ".new")
            faiss.write_index(self.index, str(tmp))
            os.replace(tmp, target)  # 原子替换，读者不会看到半个索引（06 §2.4）
        else:
            faiss.write_index(self.index, str(target))
        self.path = target


@dataclass(frozen=True)
class ShardCheck:
    shard_id: str
    ok: bool
    detail: str


class ShardIndexManager:
    """分片索引的加载、创建、追加与自检（与 ``ekbs_shard`` 表联动）。"""

    def __init__(self, db: Database, settings: Settings, *, dim: int, fingerprint: str) -> None:
        self.db = db
        self.settings = settings
        self.dim = int(dim)
        self.fingerprint = fingerprint
        self._loaded: dict[str, ShardIndex] = {}

    # ------------------------------------------------------------------
    # 登记表
    # ------------------------------------------------------------------

    @property
    def index_dir(self) -> Path:
        path = self.settings.data_paths()["index"]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def faiss_path(self, shard_id: str) -> Path:
        return self.index_dir / f"{shard_id}.faiss"

    def registry(self, *, status: str | None = "active") -> list[ShardRow]:
        sql = (
            "SELECT shard_id, dept_code, level, faiss_path, dim, embedding_fingerprint, "
            "vector_count, index_type, status FROM ekbs_shard"
        )
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY shard_id"
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            ShardRow(
                shard_id=str(r["shard_id"]),
                dept_code=str(r["dept_code"]),
                level=int(r["level"]),
                faiss_path=str(r["faiss_path"]),
                dim=int(r["dim"]),
                embedding_fingerprint=str(r["embedding_fingerprint"]),
                vector_count=int(r["vector_count"]),
                index_type=str(r["index_type"]),
                status=str(r["status"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 创建 / 加载
    # ------------------------------------------------------------------

    def create_shard(
        self, shard_id: str, *, fingerprint: str | None = None, index_type: str = "flat"
    ) -> ShardIndex:
        """建空分片：写 ``.faiss`` 文件 + 登记 ``ekbs_shard``（`01` §6.3）。"""
        dept_code, level = parse_shard_id(shard_id)
        if index_type != "flat":
            # 06 §2.3：ivf 仅在规模超过阈值后启用；阈值本身是开放项 O6-3
            log.warning("index_type_not_flat", shard_id=shard_id, index_type=index_type)
        index = ShardIndex.create(shard_id, self.faiss_path(shard_id), self.dim, fingerprint or self.fingerprint)
        index.save()
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_shard(shard_id, dept_code, level, faiss_path, dim, "
                "embedding_fingerprint, vector_count, index_type, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'active', datetime('now')) "
                "ON CONFLICT(shard_id) DO UPDATE SET faiss_path = excluded.faiss_path, "
                "dim = excluded.dim, embedding_fingerprint = excluded.embedding_fingerprint, "
                "index_type = excluded.index_type, status = 'active', updated_at = datetime('now')",
                (
                    shard_id, dept_code, level, str(index.path), self.dim,
                    str(fingerprint or self.fingerprint), index_type,
                ),
            )
            conn.commit()
        self._loaded[shard_id] = index
        log.info("shard_created", shard_id=shard_id, dim=self.dim, path=str(index.path))
        return index

    def get(self, shard_id: str, *, row: ShardRow | None = None) -> ShardIndex | None:
        """懒加载单个分片；未登记或文件缺失 → 返回 None 并告警。

        ⚠️ **跨进程可见性**：``ekbs-worker`` 入库、``ekbs.tools.*`` 重建、分块编辑都会
        追加向量并**原子替换** ``.faiss`` 文件，而 ``ekbs-api`` 持有自己的内存索引。
        若不检查文件变更，API 会一直用启动时那份索引 —— 表现为「刚入库的文档搜不到」
        （本仓库实测踩到过）。这里按 ``(mtime_ns, size)`` 判断，变了就重载；
        ``06`` §2.4 提到的 reload 机制即此，只是用文件指纹代替了显式的 reload 调用。
        """
        cached = self._loaded.get(shard_id)
        if cached is not None:
            if cached.stat_stamp() == cached.file_stamp:
                return cached
            log.info("shard_reloaded_externally", shard_id=shard_id)
            self._loaded.pop(shard_id, None)
        if row is None:
            rows = [r for r in self.registry() if r.shard_id == shard_id]
            if not rows:
                return None
            row = rows[0]
        if row.dim != self.dim:
            raise EmbeddingFingerprintMismatch(
                f"{shard_id} 登记维度 {row.dim} ≠ 当前嵌入维度 {self.dim}（00 §2.3：拒绝加载）"
            )
        if row.embedding_fingerprint != self.fingerprint:
            raise EmbeddingFingerprintMismatch(
                f"{shard_id} 嵌入指纹与当前模型不一致：登记 {row.embedding_fingerprint[:12]}… "
                f"≠ 当前 {self.fingerprint[:12]}…（C3/C4：需按新模型重建该分片，禁止混用向量）"
            )
        index = ShardIndex.load(
            shard_id, Path(row.faiss_path), expected_dim=self.dim,
            expected_fingerprint=self.fingerprint,
        )
        index.file_stamp = index.stat_stamp()
        self._loaded[shard_id] = index
        return index

    def load_active(self, *, strict: bool = True) -> dict[str, ShardIndex]:
        """加载全部 active 分片。

        ``strict=True``：任一分片维度/指纹不一致即抛
        :class:`~ekbs.errors.EmbeddingFingerprintMismatch`（`00` §2.3「拒绝启动」）。
        """
        loaded: dict[str, ShardIndex] = {}
        problems: list[str] = []
        for row in self.registry():
            try:
                index = self.get(row.shard_id, row=row)
                if index is not None:
                    loaded[row.shard_id] = index
            except EmbeddingFingerprintMismatch as exc:
                problems.append(str(exc))
        if problems and strict:
            raise EmbeddingFingerprintMismatch("；".join(problems))
        for message in problems:
            log.error("shard_load_rejected", detail=message)
        return loaded

    # ------------------------------------------------------------------
    # 追加与检索
    # ------------------------------------------------------------------

    def append(
        self, shard_id: str, chunk_ids: Sequence[int], vectors: np.ndarray
    ) -> list[int]:
        """追加向量并回写 ``ekbs_chunk.faiss_row`` 与 ``ekbs_shard.vector_count``。"""
        index = self.get(shard_id)
        if index is None:
            raise SchemaError(f"分片未登记：{shard_id}（先用 ekbs.tools.new_shard 建分片）")
        rows = index.add(vectors)
        index.save()
        with self.db.sync_connect() as conn:
            conn.executemany(
                "UPDATE ekbs_chunk SET faiss_row = ?, embedding_fingerprint = ?, shard_id = ? "
                "WHERE chunk_id = ?",
                [
                    (row, self.fingerprint, shard_id, int(chunk_id))
                    for row, chunk_id in zip(rows, chunk_ids)
                ],
            )
            conn.execute(
                "UPDATE ekbs_shard SET vector_count = ?, embedding_fingerprint = ?, "
                "dim = ?, updated_at = datetime('now') WHERE shard_id = ?",
                (index.vector_count, self.fingerprint, self.dim, shard_id),
            )
            conn.commit()
        return rows

    def search(self, shard_id: str, vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        index = self.get(shard_id)
        if index is None:
            log.warning("shard_missing_skip", shard_id=shard_id)
            return []
        return index.search(vector, top_k)

    def drop_from_cache(self, shard_id: str) -> None:
        """重建/重载后清掉内存副本（`06` §2.4 的 reload 路径）。"""
        self._loaded.pop(shard_id, None)

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------

    def self_check(self) -> list[ShardCheck]:
        """与 ``smoke.sh`` 同一口径：读文件比对 ``dim`` / ``ntotal`` / 指纹。"""
        checks: list[ShardCheck] = []
        for row in self.registry():
            try:
                index = ShardIndex.load(
                    row.shard_id, Path(row.faiss_path), expected_dim=row.dim,
                    expected_fingerprint=row.embedding_fingerprint,
                )
            except Exception as exc:
                checks.append(ShardCheck(row.shard_id, False, f"读取失败：{exc}"))
                continue
            if index.vector_count != row.vector_count:
                checks.append(
                    ShardCheck(
                        row.shard_id, False,
                        f"ntotal {index.vector_count} ≠ 登记 {row.vector_count}",
                    )
                )
            elif index.dim != row.dim:
                checks.append(
                    ShardCheck(row.shard_id, False, f"dim {index.dim} ≠ 登记 {row.dim}")
                )
            elif not row.embedding_fingerprint:
                checks.append(ShardCheck(row.shard_id, False, "登记缺少 embedding_fingerprint"))
            elif row.embedding_fingerprint != self.fingerprint:
                checks.append(
                    ShardCheck(
                        row.shard_id, False,
                        "嵌入指纹与当前模型不一致（C3：需重建该分片）",
                    )
                )
            else:
                checks.append(
                    ShardCheck(row.shard_id, True, f"{index.vector_count} 向量，dim={index.dim}")
                )
        return checks

    def count_active(self) -> int:
        return len(self.registry())

    def total_vectors(self) -> int:
        return sum(row.vector_count for row in self.registry())


def ensure_shard_for_placement(
    manager: ShardIndexManager,
    shard_id: str,
    *,
    level: int,
    dept_code: str,
    fingerprint: str | None = None,
) -> ShardIndex:
    """幂等建分片（入库路径用）：不存在则建，存在则直接加载。"""
    existing = manager.get(shard_id)
    if existing is not None:
        return existing
    return manager.create_shard(shard_id, fingerprint=fingerprint)


def shard_ids_for_dept(dept_code: str, levels: Iterable[int]) -> list[str]:
    return [make_shard_id(dept_code, level) for level in levels]
