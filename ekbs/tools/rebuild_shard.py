"""``python -m ekbs.tools.rebuild_shard`` —— 重建分片索引（`01` §16、约束 C4）。

「旋转而非重建」的落地：**从 ``ekbs_chunk.content`` 重新生成向量**，不重新解析原文。

三个使用场景：

1. 换嵌入模型 / 换维度（分片登记的 ``embedding_fingerprint`` 与新模型不一致 → 启动被拒）；
2. 重新解析某文档后清理 FAISS 里的陈旧向量（``IndexFlatIP`` 不支持删除）；
3. 索引文件损坏或与登记值（``dim`` / ``vector_count``）不一致。

实现：新建空索引 → 按 ``seq`` 顺序重嵌入该分片全部子块 → 原子替换文件 → 回写
``faiss_row`` / ``embedding_fingerprint`` / ``vector_count``。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ekbs.config import Secrets, load_settings
from ekbs.index.faiss_store import ShardIndex, ShardIndexManager
from ekbs.ingest.chunker import retrieval_text
from ekbs.logging import configure_logging, get_logger
from ekbs.provider.registry import build_embedding
from ekbs.runtime import Runtime

log = get_logger(component="tools.rebuild_shard")


async def rebuild(runtime: Runtime, shard_id: str, *, dry_run: bool = False) -> dict[str, object]:
    embed = build_embedding(runtime.settings, Secrets())
    manager = ShardIndexManager(
        runtime.db, runtime.settings, dim=int(embed.ref.dim or 0), fingerprint=embed.ref.fingerprint
    )
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, content, heading_path FROM ekbs_chunk "
            "WHERE shard_id = ? AND chunk_type = 'child' ORDER BY seq",
            (shard_id,),
        ).fetchall()
    chunk_ids = [int(r["chunk_id"]) for r in rows]
    # 与入库同口径：标题路径 + 正文（chunker.retrieval_text）
    texts = [retrieval_text(r["heading_path"], str(r["content"])) for r in rows]
    if dry_run:
        return {"shard_id": shard_id, "chunks": len(chunk_ids), "dry_run": True}

    import numpy as np

    dim = int(embed.ref.dim or 0)
    index = ShardIndex.create(
        shard_id, manager.faiss_path(shard_id), dim=dim, fingerprint=embed.ref.fingerprint
    )
    batch_size = max(1, int(runtime.settings.models.embed.batch_size))
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(await embed.embed(texts[start : start + batch_size]))
    if vectors:
        index.add(np.asarray(vectors, dtype="float32"))
    index.save()  # 原子替换

    with runtime.db.sync_connect() as conn:
        # 重建后行号重排：先清空该分片所有行的 faiss_row，再按新顺序回写
        conn.execute("UPDATE ekbs_chunk SET faiss_row = NULL WHERE shard_id = ?", (shard_id,))
        for row, chunk_id in enumerate(chunk_ids):
            conn.execute(
                "UPDATE ekbs_chunk SET faiss_row = ?, embedding_fingerprint = ? WHERE chunk_id = ?",
                (row, embed.ref.fingerprint, chunk_id),
            )
        conn.execute(
            "UPDATE ekbs_shard SET dim = ?, embedding_fingerprint = ?, vector_count = ?, "
            "updated_at = datetime('now') WHERE shard_id = ?",
            (dim, embed.ref.fingerprint, index.vector_count, shard_id),
        )
        conn.commit()
    manager.drop_from_cache(shard_id)
    return {
        "shard_id": shard_id,
        "chunks": len(chunk_ids),
        "vectors": index.vector_count,
        "fingerprint": embed.ref.fingerprint,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建分片索引（从 ekbs_chunk.content 重新嵌入）")
    parser.add_argument("--shard-id", required=True)
    parser.add_argument("--all", action="store_true", help="重建全部 active 分片")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    runtime = Runtime.create(settings, service="tools", init_schema=True)
    try:
        targets = (
            [row.shard_id for row in runtime.shards.registry()]
            if args.all
            else [args.shard_id]
        )
        for shard_id in targets:
            result = asyncio.run(rebuild(runtime, shard_id, dry_run=args.dry_run))
            print(result)
    finally:
        asyncio.run(runtime.aclose())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
