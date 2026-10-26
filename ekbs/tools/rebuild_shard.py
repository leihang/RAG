"""``python -m ekbs.tools.rebuild_shard`` —— 重建分片索引（`01` §16、约束 C4）。

「旋转而非重建」的落地：**从 ``ekbs_chunk.content`` 重新生成向量**，不重新解析原文。

三个使用场景：

1. 换嵌入模型 / 换维度（分片登记的 ``embedding_fingerprint`` 与新模型不一致 → 启动被拒）；
2. 索引文件损坏或与登记值（``dim`` / ``vector_count``）不一致；
3. 想主动压紧索引文件（长期增删后 ``IndexIDMap2`` 内部有余量）。

⚠️ **不再是"重新解析后清理陈旧向量"的手段**：索引已换成 ``IndexIDMap2``，
重解析（``ingest._replace_existing``）与删除文档都会按 ``chunk_id`` **真删**旧向量。
按旧结论去跑重建 = 白跑一趟 + 真花钱重嵌一遍全部块。

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
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.logging import configure_logging, get_logger
from ekbs.provider.registry import build_embedding
from ekbs.runtime import Runtime

log = get_logger(component="tools.rebuild_shard")


async def rebuild(runtime: Runtime, shard_id: str, *, dry_run: bool = False) -> dict[str, object]:
    index_cfg = getattr(runtime.settings, "index", None)
    segmented = getattr(index_cfg, "layout", "single") == "segmented"
    embed = build_embedding(runtime.settings, Secrets())
    manager = ShardIndexManager(
        runtime.db, runtime.settings, dim=int(embed.ref.dim or 0), fingerprint=embed.ref.fingerprint
    )
    with runtime.db.sync_connect() as conn:
        # 必须与入库用**同一口径**（叶子块），否则重建一次就悄悄丢掉代码/图片向量
        rows = conn.execute(
            "SELECT c.chunk_id, c.content, c.heading_path FROM ekbs_chunk c "
            f"WHERE c.shard_id = ? AND {RETRIEVABLE_PREDICATE} ORDER BY c.seq",
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
        # 标签用 chunk_id（IDMap2）：重建不再"重排行号"，也就没有了
        # "重建完所有 faiss_row 全体错位"的风险 —— 这是本次换格式的主要收益之一。
        index.add(np.asarray(vectors, dtype="float32"), chunk_ids)
    if segmented:
        # 分段布局：重建为一个新的 base 段 + manifest（不保留旧段，避免重复向量）
        import time as _time

        from ekbs.index.segmented import SegmentInfo, ShardManifest, manifest_path, save_manifest

        directory = manager.segmented_dir(shard_id)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"seg_rebuild_{int(_time.time())}.faiss"
        index = ShardIndex.create(shard_id, directory / name, dim=dim, fingerprint=embed.ref.fingerprint)
        index.add(np.asarray(vectors, dtype="float32"), chunk_ids)
        index.save()
        for old in directory.glob("seg_*.faiss"):
            if old.name != name:
                old.unlink()
        save_manifest(directory, ShardManifest(
            shard_id=shard_id, dim=dim, embedding_fingerprint=embed.ref.fingerprint,
            segments=[SegmentInfo(id=name[:-6], path=name, count=index.vector_count)],
        ))
        faiss_path = str(manifest_path(directory))
    else:
        index.save()  # 原子替换
        faiss_path = str(index.path)

    with runtime.db.sync_connect() as conn:
        # 仍然先清空该分片所有行的 faiss_row：被剔除的块（例如已删除文档的残留）
        # 不该继续显示为"在索引里"
        conn.execute("UPDATE ekbs_chunk SET faiss_row = NULL WHERE shard_id = ?", (shard_id,))
        for chunk_id in chunk_ids:
            conn.execute(
                "UPDATE ekbs_chunk SET faiss_row = ?, embedding_fingerprint = ? WHERE chunk_id = ?",
                (int(chunk_id), embed.ref.fingerprint, chunk_id),
            )
        conn.execute(
            "UPDATE ekbs_shard SET dim = ?, embedding_fingerprint = ?, vector_count = ?, "
            "index_type = ?, faiss_path = ?, updated_at = datetime('now') WHERE shard_id = ?",
            (dim, embed.ref.fingerprint, index.vector_count, ShardIndex.INDEX_TYPE, faiss_path, shard_id),
        )
        conn.commit()
    manager.drop_from_cache(shard_id)
    if segmented:
        manager._segmented.pop(shard_id, None)
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
    # **非严格**加载：本工具正是用来修"指纹不一致"的 —— 严格模式会把修复手段本身拒之门外
    # （实测死锁：登记指纹 ≠ 当前模型 → Runtime.create 抛 EmbeddingFingerprintMismatch）
    runtime = Runtime.create(
        settings, service="tools", init_schema=True, strict_shards=False
    )
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
