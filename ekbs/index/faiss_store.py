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

    #: 索引格式标记（写进 ``ekbs_shard.index_type``）。换格式必须能被识别出来，
    #: 否则旧文件会被新代码按错误的语义读进来 —— 那是"静默算错"，比报错危险得多。
    INDEX_TYPE = "idmap2"
    #: 旧格式：纯 ``IndexFlatIP``，位置即行号，**不支持删除**（删文档只留孤儿向量）。
    LEGACY_INDEX_TYPE = "flat"

    @classmethod
    def create(cls, shard_id: str, path: Path, dim: int, fingerprint: str) -> "ShardIndex":
        """建空分片：``IndexIDMap2(IndexFlatIP)``。

        为什么包一层 ``IndexIDMap2``：纯 ``IndexFlatIP`` 没有 ``remove_ids``，
        删一个文档只能留着孤儿向量、靠重建分片回收。包上 IDMap2 之后
        **索引的标签就是 ``chunk_id``**，于是「删文档 → 真删那几条向量」成为可能，
        同时保留原本的暴力内积检索语义（分片小，不需要近似索引）。
        """
        faiss = _import_faiss()
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(int(dim)))
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
        faiss_mod = _import_faiss()
        if not isinstance(index, faiss_mod.IndexIDMap2):
            if int(index.ntotal) == 0:
                # 空的旧格式文件：没有任何数据，直接换成新格式，不必打扰用户
                log.warning("shard_index_upgraded_empty", shard_id=shard_id)
                index = faiss_mod.IndexIDMap2(faiss_mod.IndexFlatIP(int(index.d)))
            else:
                raise SchemaError(
                    f"{shard_id} 是旧格式索引（纯 IndexFlatIP，{int(index.ntotal)} 个向量）："
                    "它用行号当标识、不支持删除单条向量。请先迁移："
                    "`.venv/bin/python -m ekbs.tools.migrate_faiss_idmap`"
                    "（只重排标签，不重新嵌入，不会改变检索结果）"
                )
        if expected_dim is not None and int(index.d) != int(expected_dim):
            raise EmbeddingFingerprintMismatch(
                f"{shard_id} 维度不一致：索引 d={index.d}，当前模型 dim={expected_dim}"
            )
        return cls(shard_id, Path(path), int(index.d), expected_fingerprint or "", index)

    def add(self, vectors: np.ndarray, ids: Sequence[int]) -> list[int]:
        """按 ``chunk_id`` 追加向量，返回写入的标签（= 传入的 ids）。

        ``ids`` 就是 ``ekbs_chunk.chunk_id``：**它是真正的身份**，不是行号。
        这样删除、重建、跨进程重载都不会错位（旧实现用行号，一重建就全体错位）。
        """
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"向量形状 {vectors.shape} 与分片维度 {self.dim} 不符")
        labels = [int(i) for i in ids]
        if len(labels) != vectors.shape[0]:
            raise ValueError(f"ids 数量 {len(labels)} 与向量条数 {vectors.shape[0]} 不符")
        self.index.add_with_ids(
            normalize(vectors), np.asarray(labels, dtype="int64")
        )
        return labels

    def remove(self, ids: Sequence[int]) -> int:
        """**真删**这些 ``chunk_id`` 对应的向量，返回实际删掉的条数。

        这是换 ``IndexIDMap2`` 换来的能力：删文档不再留孤儿向量，
        也就不再需要"删完再重建分片"这套收尾动作。
        """
        labels = [int(i) for i in ids]
        if not labels or self.index.ntotal == 0:
            return 0
        before = int(self.index.ntotal)
        self.index.remove_ids(np.asarray(labels, dtype="int64"))
        return before - int(self.index.ntotal)

    def upsert(self, vectors: np.ndarray, ids: Sequence[int]) -> list[int]:
        """按 ``chunk_id`` 覆盖写入（**先删后加**）。

        为什么必须有这个而不是直接用 ``add``：FAISS 的 ``add_with_ids`` 对**重复 id
        不会替换而是累积**（实测：同一个 id 加两次 ntotal 从 1 变 2，搜索会把同一个
        chunk 返回两次）。而"编辑子块 / 换级重嵌"都是**同一个 chunk_id 重新嵌入**，
        直接 add 会让索引越长越大且召回重复。所以这类路径一律走 upsert。
        """
        self.remove(ids)
        return self.add(vectors, ids)

    def search(self, vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """返回 ``[(chunk_id, score), …]``，score 越大越相关。

        注意语义变化：旧实现返回的是**行号**（还要回表换算），
        现在直接返回 ``chunk_id`` —— 命中即是身份，少一次映射就少一处错位风险。
        """
        if self.index.ntotal == 0 or top_k <= 0:
            return []
        arr = np.asarray(vector, dtype="float32")
        if arr.ndim != 1:
            # 不校验的话会被下面那层 [ ] 包成 3 维，FAISS 抛的错误是
            # "too many values to unpack (expected 2)"，完全看不出是入参形状问题
            raise ValueError(
                f"search 只接受单条向量（1 维，长度 {self.dim}），实得形状 {arr.shape}"
            )
        query = normalize(np.asarray([arr], dtype="float32"))
        scores, labels = self.index.search(query, min(int(top_k), int(self.index.ntotal)))
        return [
            (int(label), float(score))
            for label, score in zip(labels[0], scores[0])
            if int(label) >= 0
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
        self,
        shard_id: str,
        *,
        fingerprint: str | None = None,
        index_type: str = ShardIndex.INDEX_TYPE,
    ) -> ShardIndex:
        """建空分片：写 ``.faiss`` 文件 + 登记 ``ekbs_shard``（`01` §6.3）。"""
        dept_code, level = parse_shard_id(shard_id)
        if index_type != ShardIndex.INDEX_TYPE:
            # 06 §2.3：ivf 仅在规模超过阈值后启用（开放项 O6-3）。
            # 默认格式现在是 idmap2（标签=chunk_id，可真删单条），不再是 flat。
            log.warning(
                "index_type_not_default", shard_id=shard_id,
                index_type=index_type, default=ShardIndex.INDEX_TYPE,
            )
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
        rows = index.add(vectors, chunk_ids)
        index.save()
        with self.db.sync_connect() as conn:
            # ``faiss_row`` 这一列**保留**（不动 schema、不动已有查询），但在 IDMap2 下
            # 它的值 = 索引标签 = ``chunk_id`` —— 不再是"第几行"。
            # 非空仍然表示"这个块在索引里"，所有既有的对账/巡检语义都成立。
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

    def upsert(
        self, shard_id: str, chunk_ids: Sequence[int], vectors: np.ndarray
    ) -> list[int]:
        """覆盖写入（先删后加）—— 用于"同一个 chunk_id 重新嵌入"的场景。

        与 :meth:`append` 的区别只有一个：append 假定这些 id 是**新的**（入库路径
        的 chunk_id 由自增主键保证不重复），upsert 则先把旧的删掉。
        """
        index = self.get(shard_id)
        if index is None:
            raise SchemaError(f"分片未登记：{shard_id}")
        labels = index.upsert(vectors, chunk_ids)
        index.save()
        with self.db.sync_connect() as conn:
            conn.executemany(
                "UPDATE ekbs_chunk SET faiss_row = ?, embedding_fingerprint = ?, shard_id = ? "
                "WHERE chunk_id = ?",
                [
                    (label, self.fingerprint, shard_id, int(chunk_id))
                    for label, chunk_id in zip(labels, chunk_ids)
                ],
            )
            conn.execute(
                "UPDATE ekbs_shard SET vector_count = ?, embedding_fingerprint = ?, "
                "dim = ?, updated_at = datetime('now') WHERE shard_id = ?",
                (index.vector_count, self.fingerprint, self.dim, shard_id),
            )
            conn.commit()
        return labels

    def remove(self, shard_id: str, chunk_ids: Sequence[int]) -> int:
        """从分片里**真删**这些块（``IndexIDMap2.remove_ids``），并回写登记向量数。

        删文档时调用。旧实现只能留着孤儿向量等重建，现在删完即回收。
        删不掉的（索引里本来就没有的 id）会被 FAISS 忽略，返回值是**实际删掉的条数**。
        """
        ids = [int(c) for c in chunk_ids]
        if not ids:
            return 0
        index = self.get(shard_id)
        if index is None:
            log.warning("shard_remove_skipped_missing", shard_id=shard_id, count=len(ids))
            return 0
        removed = index.remove(ids)
        if removed:
            index.save()  # 原子替换：其他进程按指纹自动重载，无需停服
            with self.db.sync_connect() as conn:
                conn.execute(
                    "UPDATE ekbs_shard SET vector_count = ?, updated_at = datetime('now') "
                    "WHERE shard_id = ?",
                    (index.vector_count, shard_id),
                )
                conn.commit()
            log.info("shard_vectors_removed", shard_id=shard_id, removed=removed)
        return removed

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
