"""IndexIDMap2 迁移与「真删除」能力（方案 06 §2.4 的可更新性）。

这组用例盯三件事，每件都对应一个**会让数据静默出错**的坑：

1. 删除必须**真的删掉向量**（旧实现只留孤儿，靠重建回收）；
2. 用同一个 chunk_id 重新嵌入必须**替换**而不是累积 —— FAISS 的
   `add_with_ids` 对重复 id 是累积的（实测 ntotal 1→2、同一 chunk 召回两次）；
3. 旧格式分片必须**要么迁移、要么明确报错**，绝不能被新代码按错误语义读进来。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ekbs.index.faiss_store import ShardIndex, _import_faiss


@pytest.fixture
def shard(tmp_path):
    faiss = _import_faiss()
    index = ShardIndex.create("kb_test_L1", tmp_path / "kb_test_L1.faiss", dim=4, fingerprint="fp")
    return faiss, index


def v(*values: float) -> np.ndarray:
    """单条向量（1 维）—— search 用这个。"""
    return np.asarray(list(values), dtype="float32")


def vs(*rows) -> np.ndarray:
    """多条向量（2 维）—— add/upsert 用这个。"""
    return np.asarray([list(r) for r in rows], dtype="float32")


def test_create_makes_idmap2(shard) -> None:
    faiss, index = shard
    assert isinstance(index.index, faiss.IndexIDMap2)


def test_remove_really_deletes_vectors(shard) -> None:
    """删掉就是删掉：ntotal 减少，且再也搜不到。"""
    _faiss, index = shard
    index.add(vs((1, 0, 0, 0), (0, 1, 0, 0)), [11, 22])
    assert index.vector_count == 2

    assert index.remove([11]) == 1
    assert index.vector_count == 1, "删除后向量数必须减少（旧实现是留孤儿）"
    hits = index.search(v(1, 0, 0, 0), 5)
    assert [h[0] for h in hits] == [22], f"被删的 11 不该再被召回：{hits}"


def test_remove_unknown_id_is_noop(shard) -> None:
    _faiss, index = shard
    index.add(vs((1, 0, 0, 0)), [7])
    assert index.remove([999]) == 0
    assert index.vector_count == 1


def test_add_with_ids_duplicates_accumulate_so_we_must_upsert(shard) -> None:
    """**这条是本次改造的核心动因**：证明裸 `add` 会累积，所以必须有 upsert。"""
    _faiss, index = shard
    index.add(vs((1, 0, 0, 0)), [7])
    index.add(vs((0, 1, 0, 0)), [7])
    assert index.vector_count == 2, "FAISS 对重复 id 是累积而不是替换"
    hits = index.search(v(1, 0, 0, 0), 5)
    assert [h[0] for h in hits].count(7) == 2, "同一个 chunk 被召回两次"


def test_upsert_replaces_instead_of_accumulating(shard) -> None:
    _faiss, index = shard
    index.add(vs((1, 0, 0, 0)), [7])
    index.upsert(vs((0, 1, 0, 0)), [7])

    assert index.vector_count == 1, "upsert 后不该留下旧向量"
    hits = index.search(v(0, 1, 0, 0), 5)
    assert [h[0] for h in hits] == [7]
    # 旧向量确实被换掉了：用旧向量搜，分数应低于用新向量搜
    old_score = index.search(v(1, 0, 0, 0), 1)[0][1]
    new_score = index.search(v(0, 1, 0, 0), 1)[0][1]
    assert new_score > old_score


def test_search_returns_chunk_id_not_row_number(shard) -> None:
    """语义变化：返回的是标签（chunk_id），不是行号。"""
    _faiss, index = shard
    index.add(vs((1, 0, 0, 0), (0, 1, 0, 0)), [101, 202])
    hits = index.search(v(0, 1, 0, 0), 2)
    assert hits[0][0] == 202


def test_save_load_roundtrip_keeps_ids(shard, tmp_path) -> None:
    """落盘再加载，标签必须还在（否则重启后删除/重建会错位）。"""
    faiss, index = shard
    index.add(vs((1, 0, 0, 0), (0, 1, 0, 0)), [5, 9])
    index.save()

    reloaded = ShardIndex.load("kb_test_L1", index.path, expected_dim=4)
    assert isinstance(reloaded.index, faiss.IndexIDMap2)
    assert [h[0] for h in reloaded.search(v(1, 0, 0, 0), 1)] == [5]


def test_legacy_flat_index_is_refused_with_actionable_message(tmp_path) -> None:
    """旧格式**必须报错**并给出迁移命令，不能被当成新格式读进来。"""
    faiss = _import_faiss()
    path = tmp_path / "legacy.faiss"
    old = faiss.IndexFlatIP(4)
    old.add(vs((1, 0, 0, 0)))
    faiss.write_index(old, str(path))

    from ekbs.errors import SchemaError

    with pytest.raises(SchemaError) as excinfo:
        ShardIndex.load("kb_legacy_L1", path, expected_dim=4)
    assert "migrate_faiss_idmap" in str(excinfo.value)


def test_empty_legacy_index_is_upgraded_silently(tmp_path) -> None:
    """空索引没有数据，直接升级，不必打扰用户。"""
    faiss = _import_faiss()
    path = tmp_path / "empty.faiss"
    faiss.write_index(faiss.IndexFlatIP(4), str(path))
    loaded = ShardIndex.load("kb_empty_L1", path, expected_dim=4)
    assert isinstance(loaded.index, faiss.IndexIDMap2)
    assert loaded.vector_count == 0
