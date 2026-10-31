"""融合函数（`ekbs.retrieval.rrf`）的契约。

``alpha_fuse`` 的用例在 ``test_course_landing.py``；这里补**加权 RRF**：
仍然只看名次、不做分数归一化，``weights`` 只调稠密/稀疏贡献比。
"""

from __future__ import annotations

import pytest

from ekbs.retrieval.rrf import rrf_fuse, wrrf_fuse


def test_wrrf_dense_dominant_when_weight_high() -> None:
    """稠密权重高时：两路都命中的排第一，仅稠密命中的次之。"""
    fused = wrrf_fuse([["a", "b"], ["b", "c"]], [0.75, 0.25], k=60)
    assert [item for item, _ in fused] == ["b", "a", "c"]


def test_wrrf_alpha_one_is_pure_dense() -> None:
    """权重 [1, 0] = 纯稠密；稀疏独有项得 0 分但仍保留在结果里。"""
    fused = dict(wrrf_fuse([["a", "b"], ["b", "c"]], [1.0, 0.0], k=60))
    assert fused["a"] > fused["b"] > fused["c"] == 0.0


def test_wrrf_alpha_zero_is_pure_sparse() -> None:
    """权重 [0, 1] = 纯稀疏；稠密独有项得 0 分。"""
    fused = dict(wrrf_fuse([["a", "b"], ["b", "c"]], [0.0, 1.0], k=60))
    assert fused["b"] > fused["c"] > fused["a"] == 0.0


def test_wrrf_equal_weights_equal_rrf() -> None:
    """两路等权时，加权 RRF 与普通 RRF 完全一致。"""
    rankings = [["a", "b", "c"], ["c", "a"]]
    assert wrrf_fuse(rankings, [1.0, 1.0], k=60) == rrf_fuse(rankings, k=60)


def test_wrrf_rejects_mismatched_weights() -> None:
    with pytest.raises(ValueError):
        wrrf_fuse([["a"], ["b"]], [1.0], k=60)


def test_wrrf_negative_weight_is_clamped_to_zero() -> None:
    """负权重按 0 处理，避免对某一路倒扣分。"""
    assert wrrf_fuse([["a"], ["b"]], [-1.0, 1.0], k=60) == wrrf_fuse(
        [["a"], ["b"]], [0.0, 1.0], k=60
    )
