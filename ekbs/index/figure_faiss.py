"""图片多模态向量索引 —— 与文本 FAISS 分片**分开**（维度不同：1152 vs 1024）。

课程 17 口径：文本/图片进**同一多模态模型**的向量空间，查询用文本向量搜图。
本索引只存 ``chunk_type=figure`` 且带 ``image_path`` 的块；检索时作为 RRF 第三路。

权限：命中后仍按 ``chunk.shard_id ∈ visible_shard_ids`` 过滤（与稠密召回同 C1）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ekbs.index.faiss_store import _import_faiss, normalize
from ekbs.logging import get_logger

log = get_logger(component="figure_faiss")


class FigureIndex:
    """全局 figure 多模态索引（``index/figures.faiss``），标签 = ``chunk_id``。"""

    def __init__(self, path: Path, dim: int) -> None:
        self.path = Path(path)
        self.dim = int(dim)
        self.index = self._load_or_create()
        #: 上次读/写这份文件时的 ``(mtime_ns, size)``，用于发现**外部进程**的改动
        self.file_stamp = self._stat_stamp()

    def _stat_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload_if_changed(self) -> None:
        """跨进程可见性：文件被别的进程改过（或删了）就重载。

        ⚠️ **没有这个保护会静默累积孤儿向量**（2026-10-02 实测踩到）：
        `ekbs-api` 删文档时摘掉图向量并原子替换文件，而 `ekbs-worker` 仍持有启动时
        那份内存索引；worker 随后入库时在**陈旧副本**上追加 → 存盘后新旧两代向量并存
        （实测 `figures.faiss` 里有 402 个向量，而库里只有 201 个图片块，其中 201 个是孤儿）。

        文本分片早就有同一套指纹机制（`faiss_store.ShardIndexManager.get`）。
        这里补齐是为了**两边行为一致** —— 索引有内存副本的都得管跨进程可见性。
        """
        stamp = self._stat_stamp()
        if stamp == self.file_stamp:
            return
        log.info(
            "figure_index_reloaded_externally",
            path=str(self.path),
            had_vectors=int(self.index.ntotal),
        )
        self.index = self._load_or_create()
        self.file_stamp = self._stat_stamp()

    def _load_or_create(self) -> Any:
        faiss = _import_faiss()
        if self.path.is_file():
            index = faiss.read_index(str(self.path))
            if int(index.d) != self.dim:
                log.warning(
                    "figure_index_dim_mismatch",
                    path=str(self.path),
                    file_dim=int(index.d),
                    expected=self.dim,
                    hint="维度变化将重建空索引（需重解析带图文档）",
                )
                return self._new_index()
            return index
        return self._new_index()

    def _new_index(self) -> Any:
        faiss = _import_faiss()
        base = faiss.IndexFlatIP(self.dim)
        return faiss.IndexIDMap2(base)

    @property
    def vector_count(self) -> int:
        # 健康端点与指标读的就是这个数，同样不能报陈旧值
        self._reload_if_changed()
        return int(self.index.ntotal)

    def upsert(self, vectors: np.ndarray, chunk_ids: Sequence[int]) -> int:
        if vectors.size == 0:
            return 0
        self._reload_if_changed()
        if vectors.ndim != 2 or vectors.shape[1] != self.dim:
            raise ValueError(f"figure 向量形状 {vectors.shape} ≠ dim {self.dim}")
        labels = [int(i) for i in chunk_ids]
        if self.index.ntotal:
            self.index.remove_ids(np.asarray(labels, dtype="int64"))
        self.index.add_with_ids(normalize(vectors), np.asarray(labels, dtype="int64"))
        self.save()
        return len(labels)

    def remove(self, chunk_ids: Sequence[int]) -> int:
        self._reload_if_changed()
        labels = [int(i) for i in chunk_ids]
        if not labels or self.index.ntotal == 0:
            return 0
        before = int(self.index.ntotal)
        self.index.remove_ids(np.asarray(labels, dtype="int64"))
        removed = before - int(self.index.ntotal)
        if removed:
            self.save()
        return removed

    def contains(self, chunk_ids: Sequence[int]) -> set[int]:
        """返回 ``chunk_ids`` 里**确实在多模态索引中**的子集（管理台「已索引」标识用）。"""
        self._reload_if_changed()
        wanted = {int(i) for i in chunk_ids}
        if not wanted or self.index.ntotal == 0:
            return set()
        faiss = _import_faiss()
        try:
            ids = faiss.vector_to_array(self.index.id_map)
        except Exception:  # noqa: BLE001 — 极端旧索引形态兜底
            size = int(self.index.id_map.size())
            ids = np.asarray(
                [int(self.index.id_map.at(i)) for i in range(size)], dtype="int64"
            )
        return wanted.intersection(int(x) for x in ids)

    def search(self, vector: Sequence[float], top_k: int) -> list[tuple[int, float]]:
        self._reload_if_changed()
        if self.index.ntotal == 0 or top_k <= 0:
            return []
        arr = np.asarray(vector, dtype="float32")
        if arr.ndim != 1 or arr.shape[0] != self.dim:
            raise ValueError(f"查询向量形状 {arr.shape} ≠ dim {self.dim}")
        query = normalize(np.asarray([arr], dtype="float32"))
        scores, labels = self.index.search(query, min(int(top_k), int(self.index.ntotal)))
        return [
            (int(label), float(score))
            for label, score in zip(labels[0], scores[0], strict=False)
            if int(label) >= 0
        ]

    def save(self) -> None:
        faiss = _import_faiss()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".new")
        faiss.write_index(self.index, str(tmp))
        os.replace(tmp, self.path)
        # 记下自己写完后的指纹，免得下一次操作把自己的写入当成"外部改动"而白重载一次
        self.file_stamp = self._stat_stamp()
