"""Schema 与审计表只写不改（`01` §5.6/§5.7、`02` TC-ENV-010）。"""

from __future__ import annotations

import sqlite3

import pytest

from ekbs.db import EXPECTED_TABLES, EXPECTED_TRIGGERS, Database


def test_apply_schema_is_idempotent(settings) -> None:
    database = Database(settings)
    first = database.apply_schema()
    assert first.tables == EXPECTED_TABLES
    assert first.triggers == EXPECTED_TRIGGERS
    # 重复执行不报错、不改变结构（DDL 全部 IF NOT EXISTS）
    second = database.apply_schema(force=True)
    assert second.tables == EXPECTED_TABLES
    assert second.triggers == EXPECTED_TRIGGERS
    assert second.ok


def test_schema_report_ok(db) -> None:
    report = db.verify_schema()
    assert report.ok, report.problems()
    assert report.policy_version == 1
    assert report.journal_mode.lower() == "wal"
    assert report.integrity == "ok"


def test_fts5_virtual_table_exists(db) -> None:
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ekbs_chunk_fts%'"
        ).fetchall()
    names = {str(r["name"]) for r in rows}
    assert "ekbs_chunk_fts" in names
    assert {"ekbs_chunk_fts_data", "ekbs_chunk_fts_idx"} <= names


def _insert_message(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint, "
        "clearance_at) VALUES ('s1', 1, 'web', 'fp', 2)"
    )
    conn.execute(
        "INSERT INTO ekbs_qa_message(session_id, turn, role, question, answer) "
        "VALUES ('s1', 1, 'assistant', 'q', 'a')"
    )


def test_audit_tables_reject_update_and_delete(db) -> None:
    """审计表 append-only：SQLite 没有 GRANT/REVOKE，只能靠触发器硬拒（01 §5.6）。"""
    with db.sync_connect() as conn:
        _insert_message(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ekbs_qa_message SET answer = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM ekbs_qa_message")
        # 触发器拒绝后事务仍可用（ABORT 只回滚当前语句）
        assert conn.execute("SELECT answer FROM ekbs_qa_message").fetchone()["answer"] == "a"


def test_qa_session_allows_only_two_fields(db) -> None:
    """``ekbs_qa_session`` 只允许改 ``ended_at`` / ``message_count``。

    其余字段一律拒绝 —— 且用的是 ``IS NOT`` 而不是 ``<>``：``<>`` 在任一侧为 NULL 时结果为
    NULL，``WHEN`` 子句会当成假，「从 NULL 改成有值」这种篡改就漏过去了
    （`01` §5.6 的注释与 ``deploy/README.md`` 都点明了这个坑）。
    """
    with db.sync_connect() as conn:
        _insert_message(conn)  # conversation_id 初始为 NULL
        conn.execute(
            "UPDATE ekbs_qa_session SET ended_at = datetime('now'), message_count = 2 "
            "WHERE session_id = 's1'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ekbs_qa_session SET perm_fingerprint = 'other'")
        # NULL → 值 的篡改同样必须被拦（IS NOT 的价值所在）
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ekbs_qa_session SET conversation_id = 'forged'")
        # 白名单字段仍可继续更新（归档/结束会话需要）
        conn.execute("UPDATE ekbs_qa_session SET message_count = 4 WHERE session_id = 's1'")
        assert (
            conn.execute(
                "SELECT message_count FROM ekbs_qa_session WHERE session_id = 's1'"
            ).fetchone()["message_count"]
            == 4
        )


def test_policy_version_seed_single_row(db) -> None:
    with db.sync_connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM ekbs_policy_version").fetchone()["c"]
    assert int(count) == 1


def _citation_columns(db) -> list[str]:
    with db.sync_connect() as conn:
        rows = conn.execute("PRAGMA table_info(ekbs_qa_citation)").fetchall()
    return [str(r["name"]) for r in rows]


def test_qa_citation_has_source_chunk_id_on_fresh_schema(db) -> None:
    """引用表同时存上下文块与精排来源块（task-3 的拆分身份）。"""
    cols = _citation_columns(db)
    assert "chunk_id" in cols
    assert "source_chunk_id" in cols


def test_apply_schema_patches_adds_source_chunk_id_idempotently(db) -> None:
    """存量库缺列时由启动补丁补齐；重复执行不报错、不重复加列。"""
    with db.sync_connect() as conn:
        conn.execute("ALTER TABLE ekbs_qa_citation DROP COLUMN source_chunk_id")
        conn.commit()
    assert "source_chunk_id" not in _citation_columns(db)

    db.apply_schema_patches()
    assert _citation_columns(db).count("source_chunk_id") == 1

    # 幂等：第二次是 no-op（PRAGMA table_info 守卫）
    db.apply_schema_patches()
    assert _citation_columns(db).count("source_chunk_id") == 1


def _message_columns(db) -> list[str]:
    with db.sync_connect() as conn:
        rows = conn.execute("PRAGMA table_info(ekbs_qa_message)").fetchall()
    return [str(r["name"]) for r in rows]


def test_qa_message_has_context_chunk_ids_on_fresh_schema(db) -> None:
    """消息行要能回答「模型实际看到了哪几块」（task-5：引用 ≠ 上下文全集）。"""
    assert "context_chunk_ids" in _message_columns(db)


def test_apply_schema_patches_adds_context_chunk_ids_idempotently(db) -> None:
    """存量库（没有该列）由启动补丁补齐；重复执行 no-op。"""
    with db.sync_connect() as conn:
        conn.execute("ALTER TABLE ekbs_qa_message DROP COLUMN context_chunk_ids")
        conn.commit()
    assert "context_chunk_ids" not in _message_columns(db)

    db.apply_schema_patches()
    assert _message_columns(db).count("context_chunk_ids") == 1

    db.apply_schema_patches()
    assert _message_columns(db).count("context_chunk_ids") == 1


def _chunk_columns(db) -> list[str]:
    with db.sync_connect() as conn:
        rows = conn.execute("PRAGMA table_info(ekbs_chunk)").fetchall()
    return [str(r["name"]) for r in rows]


def _chunk_table_group_index(db) -> list[str]:
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'ekbs_chunk' ORDER BY name"
        ).fetchall()
    return [str(r["name"]) for r in rows]


def test_chunk_has_table_group_on_fresh_schema(db) -> None:
    """超限表格的分段组 id 落在块上（task-7：容器 2048 + 超限分段展开）。"""
    assert "table_group" in _chunk_columns(db)
    assert "idx_chunk_table_group" in _chunk_table_group_index(db)


def test_apply_schema_patches_adds_table_group_idempotently(db) -> None:
    """存量库缺列/缺索引时由启动补丁补齐；重复执行 no-op。"""
    with db.sync_connect() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_chunk_table_group")
        conn.execute("ALTER TABLE ekbs_chunk DROP COLUMN table_group")
        conn.commit()
    assert "table_group" not in _chunk_columns(db)

    db.apply_schema_patches()
    assert _chunk_columns(db).count("table_group") == 1
    assert "idx_chunk_table_group" in _chunk_table_group_index(db)

    db.apply_schema_patches()
    assert _chunk_columns(db).count("table_group") == 1
    assert _chunk_table_group_index(db).count("idx_chunk_table_group") == 1
