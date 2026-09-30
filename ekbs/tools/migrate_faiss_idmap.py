"""把旧格式分片（纯 ``IndexFlatIP``）迁移成 ``IndexIDMap2``。

为什么要迁移：旧格式用**行号**标识向量，于是「删一个文档」只能把向量留成孤儿、
靠重建分片回收；新格式把 ``chunk_id`` 直接作为索引标签，删除即真删。

**不重新嵌入**：只是把已有的向量重新贴上 ``chunk_id`` 标签，所以
不需要调用嵌入模型、不花 token、检索结果也不变。

映射怎么来：旧索引第 N 行对应 ``ekbs_chunk.faiss_row = N`` 的那条记录 ——
这正是旧实现维护的那张表。所以迁移是确定性的：

    行号 0,1,2…  →  faiss_row=0,1,2… 的 chunk_id

用法：
    .venv/bin/python -m ekbs.tools.migrate_faiss_idmap            # 看会迁什么（dry-run）
    .venv/bin/python -m ekbs.tools.migrate_faiss_idmap --apply    # 真迁移
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.errors import SchemaError
from ekbs.index.faiss_store import ShardIndex, _import_faiss
from ekbs.logging import get_logger

log = get_logger(component="tools.migrate_faiss_idmap")


def migrate_shard(settings, db: Database, shard_id: str, faiss_path: Path, *, apply: bool) -> dict:
    """迁移单个分片；返回一份说明性结果（不抛异常，由调用方汇总）。"""
    import numpy as np

    faiss = _import_faiss()
    if not faiss_path.is_file():
        return {"shard_id": shard_id, "status": "skipped", "reason": "索引文件不存在"}

    index = faiss.read_index(str(faiss_path))
    if isinstance(index, faiss.IndexIDMap2):
        return {"shard_id": shard_id, "status": "skipped", "reason": "已是 idmap2"}
    if int(index.ntotal) == 0:
        return {"shard_id": shard_id, "status": "skipped", "reason": "空索引（加载时会自动升级）"}

    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, faiss_row FROM ekbs_chunk "
            "WHERE shard_id = ? AND faiss_row IS NOT NULL ORDER BY faiss_row",
            (shard_id,),
        ).fetchall()
    row_to_chunk = {int(r["faiss_row"]): int(r["chunk_id"]) for r in rows}
    missing = [i for i in range(int(index.ntotal)) if i not in row_to_chunk]
    if missing:
        # 行号对不齐说明库里已经有孤儿或缺行：硬迁会把向量贴到错的 chunk 上，
        # 那是**静默错数据**，比迁移失败严重得多 —— 所以这里拒绝并让人去重建。
        return {
            "shard_id": shard_id,
            "status": "refused",
            "reason": f"索引 {int(index.ntotal)} 行中有 {len(missing)} 行在库里找不到对应 chunk"
                      "（存在孤儿/缺行）。请改用 rebuild_shard 重建该分片，不要迁移。",
        }

    ids = np.asarray([row_to_chunk[i] for i in range(int(index.ntotal))], dtype="int64")
    vectors = index.reconstruct_n(0, int(index.ntotal))
    if not apply:
        return {
            "shard_id": shard_id,
            "status": "would_migrate",
            "vectors": int(index.ntotal),
            "first_ids": [int(x) for x in ids[:5]],
        }

    new_index = faiss.IndexIDMap2(faiss.IndexFlatIP(int(index.d)))
    new_index.add_with_ids(np.asarray(vectors, dtype="float32"), ids)
    tmp = faiss_path.with_suffix(faiss_path.suffix + ".migrating")
    faiss.write_index(new_index, str(tmp))
    os.replace(tmp, faiss_path)  # 原子替换：其他进程按文件指纹自动重载，无需停服
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_shard SET index_type = ?, vector_count = ?, updated_at = datetime('now') "
            "WHERE shard_id = ?",
            (ShardIndex.INDEX_TYPE, int(new_index.ntotal), shard_id),
        )
        conn.commit()
    log.info("shard_migrated", shard_id=shard_id, vectors=int(new_index.ntotal))
    return {"shard_id": shard_id, "status": "migrated", "vectors": int(new_index.ntotal)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把旧格式 FAISS 分片迁到 IndexIDMap2（不重新嵌入）")
    parser.add_argument("--apply", action="store_true", help="真迁移（默认只报告）")
    parser.add_argument("--shard-id", default=None, help="只迁指定分片")
    args = parser.parse_args(argv)

    settings = load_settings()
    db = Database(settings)
    db.apply_schema()
    with db.sync_connect() as conn:
        shards = conn.execute(
            "SELECT shard_id, faiss_path, index_type FROM ekbs_shard WHERE status = 'active' "
            "ORDER BY shard_id"
        ).fetchall()

    results = []
    for row in shards:
        shard_id = str(row["shard_id"])
        if args.shard_id and shard_id != args.shard_id:
            continue
        results.append(
            migrate_shard(settings, db, shard_id, Path(str(row["faiss_path"])), apply=args.apply)
        )

    for item in results:
        mark = {"migrated": "✅", "would_migrate": "→", "skipped": "·", "refused": "⛔"}.get(
            str(item["status"]), "?"
        )
        detail = item.get("reason") or f"{item.get('vectors')} 个向量"
        print(f"  {mark} {item['shard_id']:20} {item['status']:14} {detail}")

    refused = [r for r in results if r["status"] == "refused"]
    if refused:
        print("\n  被拒绝的分片请改用：.venv/bin/python -m ekbs.tools.rebuild_shard --shard-id <id>")
    if not args.apply and any(r["status"] == "would_migrate" for r in results):
        print("\n  （当前只是报告；加 --apply 才真迁移）")
    if not results:
        print("  没有 active 分片需要处理")
    _ = SchemaError
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
