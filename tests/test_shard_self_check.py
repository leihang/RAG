"""分片自检要同时支持单文件与分段两种布局（2026-10-31 回归）。

背景：分段布局下 ``ekbs_shard.faiss_path`` 指向 ``manifest.json``；旧实现把它当
``.faiss`` 去读，报 ``Index type 0x20200a7b not recognized``（``0x20200a7b`` 就是
JSON 开头的 ``{``），导致管理台分片页全红、``reconcile`` 每分钟 mismatch、
导入验收失败。
"""

from __future__ import annotations

from ekbs.index.segmented import manifest_path


def _migrate_to_segmented(runtime, shard_id: str) -> None:
    """把分片迁到分段布局（与线上迁移同口径：单文件复制为 base 段 + 写 manifest）。"""
    manager = runtime.shards
    shard = manager._get_segmented(shard_id)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_shard SET faiss_path = ?, vector_count = ? WHERE shard_id = ?",
            (str(manifest_path(manager.segmented_dir(shard_id))), shard.count, shard_id),
        )
        conn.commit()
    manager.drop_from_cache(shard_id)
    manager._segmented.pop(shard_id, None)


async def test_self_check_ok_for_single_file_layout(seeded) -> None:
    runtime, _ = seeded
    checks = runtime.shards.self_check()

    assert checks
    assert all(check.ok for check in checks), [check.detail for check in checks]


async def test_self_check_ok_for_segmented_layout(seeded) -> None:
    runtime, _ = seeded
    for row in runtime.shards.registry():
        _migrate_to_segmented(runtime, row.shard_id)
    with runtime.db.sync_connect() as conn:
        paths = [str(row["faiss_path"]) for row in conn.execute("SELECT faiss_path FROM ekbs_shard")]
    assert paths and all(path.endswith("manifest.json") for path in paths)

    checks = runtime.shards.self_check()

    assert all(check.ok for check in checks), [check.detail for check in checks]
    assert any("向量" in check.detail for check in checks)


async def test_self_check_reports_count_mismatch_in_segmented(seeded) -> None:
    """分段布局下改登记的 vector_count，自检要报"不一致"，而不是"读取失败"。"""
    runtime, _ = seeded
    shard_id = runtime.shards.registry()[0].shard_id
    _migrate_to_segmented(runtime, shard_id)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_shard SET vector_count = vector_count + 1 WHERE shard_id = ?",
            (shard_id,),
        )
        conn.commit()

    checks = {check.shard_id: check for check in runtime.shards.self_check()}

    assert checks[shard_id].ok is False
    assert "ntotal" in checks[shard_id].detail and "登记" in checks[shard_id].detail
