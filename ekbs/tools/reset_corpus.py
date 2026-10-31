"""``python -m ekbs.tools.reset_corpus`` —— 清空语料库（"清空重来"的唯一入口）。

为什么单独做一个工具，而不是给「删文档」加级联：删文档是**按份**的操作，而
``ekbs_shard`` 是「(kb, 密级) → 分片」的路由注册单元、旧的 ``<shard>.faiss`` 是
分段布局之前的遗留索引 —— 它们都不是某一份文档的私有资产。把这些全库级的清理
塞进单份删除里，既语义不对，也容易在「删最后一份文档」时误伤路由。

本工具按层清理（顺序有讲究）：

1. ``ekbs_chunk`` / ``ekbs_chunk_fts`` / FAISS 段 / 图索引 —— 块与向量；
2. ``ekbs_acl_entry``（仅 ``object_type='document'``）—— 权限（多态表无法用外键级联）；
3. ``ekbs_cache`` —— 答案缓存；
4. ``ekbs_document``；
5. 分片段文件 + 空 manifest + ``ekbs_shard.vector_count = 0``（**保留分片注册行**，
   路由与重导都还要用）；
6. 已迁移分片的旧单文件 ``var/index/<shard>.faiss`` —— **移到 ``var/backup/``**，
   不直接删（留后路，且避免 ``manifest`` 丢失时被兜底分支自动"复活"）。

默认**演练**（只报告、不改动）；``--yes`` 才真正执行。

.. code-block:: bash

    export EKBS_CONFIG=config/application.cloud.yaml
    .venv/bin/python -m ekbs.tools.reset_corpus            # 演练
    .venv/bin/python -m ekbs.tools.reset_corpus --yes      # 真清
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ekbs.config import load_settings
from ekbs.index.segmented import ShardManifest, save_manifest
from ekbs.logging import get_logger
from ekbs.runtime import Runtime

log = get_logger(component="tools.reset_corpus")

LEGACY_BACKUP_PREFIX = "legacy-faiss"


@dataclass
class ResetReport:
    dry_run: bool = True
    documents: int = 0
    chunks: int = 0
    fts_rows: int = 0
    acl_entries: int = 0
    cache_rows: int = 0
    shards: list[str] = field(default_factory=list)
    legacy_indexes: list[str] = field(default_factory=list)
    legacy_backup_dir: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _counts(runtime: Runtime) -> tuple[int, int, int, int, int]:
    with runtime.db.sync_connect() as conn:
        documents = int(conn.execute("SELECT COUNT(*) AS n FROM ekbs_document").fetchone()["n"])
        chunks = int(conn.execute("SELECT COUNT(*) AS n FROM ekbs_chunk").fetchone()["n"])
        acl = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM ekbs_acl_entry WHERE object_type = 'document'"
            ).fetchone()["n"]
        )
        cache = int(conn.execute("SELECT COUNT(*) AS n FROM ekbs_cache").fetchone()["n"])
    return documents, chunks, int(runtime.fts.count()), acl, cache


def _legacy_indexes(runtime: Runtime) -> list[Path]:
    """已迁移到分段布局的分片，其旧的单文件 ``<shard>.faiss`` 是孤儿。"""
    out: list[Path] = []
    for row in runtime.shards.registry():
        single = runtime.shards.index_dir / f"{row.shard_id}.faiss"
        manifest = runtime.shards.segmented_dir(row.shard_id) / "manifest.json"
        if single.is_file() and manifest.is_file():
            out.append(single)
    return out


def _move_legacy_indexes(runtime: Runtime, paths: list[Path]) -> Path | None:
    if not paths:
        return None
    dest = (
        runtime.settings.data_paths()["backup"]
        / f"{LEGACY_BACKUP_PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    dest.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.move(str(path), str(dest / path.name))
    return dest


def _clear_shards(runtime: Runtime) -> list[str]:
    """删段文件 + 重写空 manifest + 分片计数归零（保留注册行）。"""
    shards: list[str] = []
    for row in runtime.shards.registry():
        shard_id = row.shard_id
        directory = runtime.shards.segmented_dir(shard_id)
        for seg in directory.glob("seg_*.faiss"):
            seg.unlink(missing_ok=True)
        save_manifest(
            directory,
            ShardManifest(
                shard_id=shard_id,
                dim=runtime.shards.dim,
                embedding_fingerprint=runtime.shards.fingerprint,
            ),
        )
        runtime.shards.drop_from_cache(shard_id)
        runtime.shards._segmented.pop(shard_id, None)  # noqa: SLF001 - 与 rebuild_shard 同口径
        shards.append(shard_id)
    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_shard SET vector_count = 0, updated_at = datetime('now')")
        conn.commit()
    return shards


def reset_corpus(runtime: Runtime, *, dry_run: bool = True) -> ResetReport:
    """清空语料库。``dry_run=True`` 只统计、不改动。"""
    documents, chunks, fts_rows, acl_entries, cache_rows = _counts(runtime)
    report = ResetReport(
        dry_run=dry_run,
        documents=documents,
        chunks=chunks,
        fts_rows=fts_rows,
        acl_entries=acl_entries,
        cache_rows=cache_rows,
        shards=[row.shard_id for row in runtime.shards.registry()],
        legacy_indexes=[str(p) for p in _legacy_indexes(runtime)],
    )
    if dry_run:
        return report

    with runtime.db.sync_connect() as conn:
        all_ids = [int(row["chunk_id"]) for row in conn.execute("SELECT chunk_id FROM ekbs_chunk")]

    # 先退出内存里的段索引并删段文件，再删 FTS/DB 行（各用独立连接，避免嵌套写锁）
    _clear_shards(runtime)
    if all_ids:
        runtime.fts.delete_chunk_ids(all_ids)
        if getattr(runtime, "figure_index", None) is not None:
            runtime.figure_index.remove(all_ids)

    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_chunk")
        conn.execute("DELETE FROM ekbs_document")
        conn.execute("DELETE FROM ekbs_acl_entry WHERE object_type = 'document'")
        conn.execute("DELETE FROM ekbs_cache")
        conn.commit()

    backup_dir = _move_legacy_indexes(runtime, _legacy_indexes(runtime))
    if backup_dir is not None:
        report.legacy_backup_dir = str(backup_dir)
    log.info(
        "corpus_reset",
        documents=documents,
        chunks=chunks,
        acl_entries=acl_entries,
        cache_rows=cache_rows,
        shards=len(report.shards),
        legacy_moved=len(report.legacy_indexes),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空语料库（块/权限/缓存/旧索引/分片计数）")
    parser.add_argument("--yes", action="store_true", help="真的执行（默认只演练）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)

    settings = load_settings()
    runtime = Runtime.create(settings, service="reset_corpus")
    try:
        report = reset_corpus(runtime, dry_run=not args.yes)
    finally:
        import asyncio

        asyncio.run(runtime.aclose())

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        mode = "演练（未改动）" if report.dry_run else "已执行"
        print(f"清空语料库：{mode}")
        print(f"  文档 {report.documents} / 块 {report.chunks} / FTS 行 {report.fts_rows}")
        print(f"  文档权限 {report.acl_entries} 条 / 缓存 {report.cache_rows} 行")
        print(f"  分片 {report.shards}")
        print(f"  旧单文件索引 {len(report.legacy_indexes)} 个")
        for path in report.legacy_indexes:
            print(f"    - {path}")
        if report.legacy_backup_dir:
            print(f"  旧索引已移到：{report.legacy_backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
