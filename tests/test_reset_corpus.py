"""清空语料库（`ekbs.tools.reset_corpus`）的契约。

重点不是"删干净了"，而是**删对层级**：块/权限/缓存/旧索引/分片计数全清，
但分片注册行、知识库、用户必须保留（重导与路由都要用）。
"""

from __future__ import annotations

from pathlib import Path

from ekbs.tools.reset_corpus import reset_corpus


def _count(runtime, table: str) -> int:
    with runtime.db.sync_connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _doc_acl_count(runtime) -> int:
    with runtime.db.sync_connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM ekbs_acl_entry WHERE object_type = 'document'"
            ).fetchone()["n"]
        )


async def test_dry_run_reports_without_touching(seeded) -> None:
    """演练只统计，不改任何数据。"""
    runtime, _summary = seeded
    before = (_count(runtime, "ekbs_document"), _count(runtime, "ekbs_chunk"))

    report = reset_corpus(runtime, dry_run=True)

    assert report.dry_run is True
    assert report.documents > 0 and report.chunks > 0
    assert (_count(runtime, "ekbs_document"), _count(runtime, "ekbs_chunk")) == before


async def test_reset_clears_all_layers_but_keeps_registry(seeded) -> None:
    """块/权限/缓存/旧索引/分片计数全清；分片注册行、用户、知识库保留。"""
    runtime, _summary = seeded
    with runtime.db.sync_connect() as conn:
        doc_id = int(conn.execute("SELECT doc_id FROM ekbs_document LIMIT 1").fetchone()["doc_id"])
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect, created_at) VALUES "
            "('document', ?, 'dept', '97486022', 'read', 'allow', datetime('now'))",
            (str(doc_id),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ekbs_cache(cache_key, value, expires_at) "
            "VALUES ('reset-test', 'v', 9999999999)"
        )
        conn.commit()

    shard_id = runtime.shards.registry()[0].shard_id
    legacy = runtime.shards.index_dir / f"{shard_id}.faiss"
    legacy.write_bytes(b"legacy-single-file")

    report = reset_corpus(runtime, dry_run=False)

    assert report.documents > 0
    assert _count(runtime, "ekbs_document") == 0
    assert _count(runtime, "ekbs_chunk") == 0
    assert runtime.fts.count() == 0
    assert _count(runtime, "ekbs_cache") == 0
    assert _doc_acl_count(runtime) == 0

    with runtime.db.sync_connect() as conn:
        assert conn.execute("SELECT SUM(vector_count) AS n FROM ekbs_shard").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM ekbs_shard").fetchone()["n"] > 0
        assert conn.execute("SELECT COUNT(*) AS n FROM ekbs_kb").fetchone()["n"] > 0
        assert conn.execute("SELECT COUNT(*) AS n FROM ekbs_user").fetchone()["n"] > 0

    # 旧单文件索引被移到 backup，不再留在 index 目录里被兜底分支捡起
    assert not legacy.exists()
    assert report.legacy_backup_dir
    assert Path(report.legacy_backup_dir, legacy.name).is_file()


async def test_reset_is_idempotent(seeded) -> None:
    """再清一次不报错，且计数全为 0。"""
    runtime, _summary = seeded
    reset_corpus(runtime, dry_run=False)

    report = reset_corpus(runtime, dry_run=False)

    assert report.documents == 0
    assert report.chunks == 0
    assert report.acl_entries == 0
    assert report.cache_rows == 0
