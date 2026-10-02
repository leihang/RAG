"""RRF（Reciprocal Rank Fusion）—— `00` §2.1、`docs/spec/10` §8。

.. code-block:: text

    rrf(c) = Σ_{l ∈ {dense, sparse}} 1 / (k + rank_l(c)),   k = 60

两个刻意的设计：

* **只用名次，不用分数**：FAISS 内积与 BM25 的分数量纲不可比，做分数校准会引入
  一条需要长期维护的归一化曲线；RRF 的鲁棒性正来自「不比分数」。
* **rank 从 1 开始**（不是 0）：`k + rank` 的写法在 `00`/`05` 口径下 rank 为 1-based。
  K11 提到过起点歧义，这里取 1-based 并与 `02` 的用例对齐。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import TypeVar

T = TypeVar("T", bound=Hashable)

DEFAULT_RRF_K = 60


def rrf_fuse(
    rankings: Sequence[Sequence[T]], k: int = DEFAULT_RRF_K
) -> list[tuple[T, float]]:
    """多路召回结果融合。

    ``rankings``：每一路按相关性**降序**排列的 id 列表（本方案是 dense / sparse 两路）。
    返回 ``[(id, rrf_score), …]``，按分数降序、同分按 id 升序（保证结果可复现）。
    """
    scores: dict[T, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda pair: (-pair[1], str(pair[0])))


def alpha_fuse(
    dense: Sequence[tuple[T, float]],
    sparse: Sequence[tuple[T, float]],
    *,
    alpha: float = 0.5,
) -> list[tuple[T, float]]:
    """加权分数融合：``Score = α·dense_norm + (1-α)·sparse_norm``（课程 18）。

    * ``dense`` / ``sparse``：``(id, raw_score)``，**越高越相关**（BM25 调用方须先取负）。
    * 各路先 min-max 归一到 [0,1]；单路只有一个点时记 1.0。
    * ``alpha=1`` 纯向量，``alpha=0`` 纯稀疏；默认 0.5。
    """
    alpha = max(0.0, min(1.0, float(alpha)))
    dense_n = _minmax_norm(dense)
    sparse_n = _minmax_norm(sparse)
    ids = set(dense_n) | set(sparse_n)
    fused = {
        item: alpha * dense_n.get(item, 0.0) + (1.0 - alpha) * sparse_n.get(item, 0.0)
        for item in ids
    }
    return sorted(fused.items(), key=lambda pair: (-pair[1], str(pair[0])))


def _minmax_norm(scored: Sequence[tuple[T, float]]) -> dict[T, float]:
    if not scored:
        return {}
    values = [float(score) for _, score in scored]
    lo, hi = min(values), max(values)
    if hi <= lo:
        return {item: 1.0 for item, _ in scored}
    span = hi - lo
    return {item: (float(score) - lo) / span for item, score in scored}


def ranks_of(ranking: Sequence[T]) -> dict[T, int]:
    """id → 1-based 名次（用于审计与调试）。"""
    return {item: position for position, item in enumerate(ranking, start=1)}
