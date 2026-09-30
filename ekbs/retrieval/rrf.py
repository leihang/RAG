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


def ranks_of(ranking: Sequence[T]) -> dict[T, int]:
    """id → 1-based 名次（用于审计与调试）。"""
    return {item: position for position, item in enumerate(ranking, start=1)}
