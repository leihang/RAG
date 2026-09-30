"""审计归档（`00` §3.5 留存期 + `01` §5.6 的触发器摘除例外）。

这组用例盯的是**风险最高的那一步**：为了删除超期行必须临时摘掉 append-only 触发器，
所以必须证明 ① 只删超期行 ② 删前有留底 ③ 触发器一定复原 ④ 出任何错都整体回滚。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ekbs.archive import RETENTION_DAYS, AuditArchiver
from ekbs.db import EXPECTED_TRIGGERS


@pytest.fixture
def archiver(runtime) -> AuditArchiver:
    return AuditArchiver(runtime.db, runtime.settings)


def _insert_turn(runtime, session_id: str, *, age_days: int) -> None:
    """插入一轮问答的审计三件套，并把时间回拨 age_days 天。"""
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint, "
            "clearance_at, started_at) VALUES (?, 1, 'web', 'fp', 2, "
            "datetime('now', ?))",
            (session_id, f"-{age_days} days"),
        )
        conn.execute(
            "INSERT INTO ekbs_qa_message(session_id, turn, role, question, answer, created_at) "
            "VALUES (?, 1, 'assistant', 'q', 'a', datetime('now', ?))",
            (session_id, f"-{age_days} days"),
        )
        message_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO ekbs_qa_citation(message_id, cite_index, chunk_id, doc_id, created_at) "
            "VALUES (?, 1, 1, 1, datetime('now', ?))",
            (message_id, f"-{age_days} days"),
        )
        conn.commit()


def _count(runtime, table: str) -> int:
    with runtime.db.sync_connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _trigger_count(runtime) -> int:
    with runtime.db.sync_connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%'"
            ).fetchone()[0]
        )


def test_archive_removes_only_expired_rows_and_keeps_jsonl(runtime, archiver, tmp_path) -> None:
    _insert_turn(runtime, "old-session", age_days=200)
    _insert_turn(runtime, "fresh-session", age_days=10)

    report = archiver.archive()

    assert _count(runtime, "ekbs_qa_session") == 1
    assert _count(runtime, "ekbs_qa_message") == 1
    assert _count(runtime, "ekbs_qa_citation") == 1
    with runtime.db.sync_connect() as conn:
        remaining = conn.execute("SELECT session_id FROM ekbs_qa_session").fetchone()["session_id"]
    assert remaining == "fresh-session"

    # 留底：被删的行必须能在 JSONL 里找到
    archived = [r for r in report.results if r.deleted]
    assert archived, report.as_dict()
    files = sorted(archiver.archive_dir.glob("*.jsonl"))
    assert files, "归档必须留下 JSONL 底档"
    lines = [json.loads(line) for path in files for line in path.read_text().splitlines()]
    assert any(item.get("session_id") == "old-session" for item in lines)

    # 「归档了多少行」必须写进 ekbs_perm_change_log（01 §5.6）
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT change_type, object_id, new_value FROM ekbs_perm_change_log "
            "WHERE change_type = 'archive' ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row["change_type"] == "archive"


def test_triggers_are_restored_after_archive(runtime, archiver) -> None:
    """摘掉触发器是临时动作：归档后 append-only 保护必须完好如初。"""
    _insert_turn(runtime, "old-session", age_days=400)
    assert _trigger_count(runtime) == EXPECTED_TRIGGERS

    report = archiver.archive()
    assert report.trigger_count_after == EXPECTED_TRIGGERS
    assert _trigger_count(runtime) == EXPECTED_TRIGGERS

    # BEFORE DELETE 触发器是**逐行**触发的：表空时删不出错，不能证明触发器还在。
    # 因此先补一行新数据，再验保护是否完好。
    _insert_turn(runtime, "fresh-session", age_days=1)
    with runtime.db.sync_connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM ekbs_qa_message")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ekbs_qa_message SET answer = 'x'")


def test_dry_run_changes_nothing(runtime, archiver) -> None:
    _insert_turn(runtime, "old-session", age_days=400)
    before = _count(runtime, "ekbs_qa_message")
    report = archiver.archive(dry_run=True)
    assert report.total_deleted >= 1
    assert _count(runtime, "ekbs_qa_message") == before
    assert not list(archiver.archive_dir.glob("*.jsonl")) if archiver.archive_dir.exists() else True
    assert _trigger_count(runtime) == EXPECTED_TRIGGERS


def test_perm_change_log_retention_is_365_days(runtime, archiver) -> None:
    """权限变更日志是合规证据，留存期 365 天 —— 180 天的不能被清掉。"""
    assert RETENTION_DAYS["ekbs_perm_change_log"] == 365
    with runtime.db.sync_connect() as conn:
        for age, marker in ((200, "keep"), (400, "drop")):
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(object_type, object_id, change_type, field, "
                "old_value, new_value, reason, created_at) "
                "VALUES ('document', ?, 'level', 'level', 'L2', 'L3', ?, datetime('now', ?))",
                (marker, marker, f"-{age} days"),
            )
        conn.commit()
    archiver.archive()
    with runtime.db.sync_connect() as conn:
        rows = [str(r["object_id"]) for r in conn.execute("SELECT object_id FROM ekbs_perm_change_log").fetchall()]
    assert "keep" in rows and "drop" not in rows


def test_archive_rolls_back_atomically_on_failure(runtime, archiver, monkeypatch) -> None:
    """归档中途失败必须整体回滚：不能出现「触发器摘了、行没删」或「删了一半」。"""
    _insert_turn(runtime, "old-session", age_days=400)
    before = _count(runtime, "ekbs_qa_message")

    def boom(*args, **kwargs):
        raise RuntimeError("模拟归档写盘失败")

    monkeypatch.setattr(AuditArchiver, "_write_jsonl", boom)
    with pytest.raises(RuntimeError):
        archiver.archive()

    assert _count(runtime, "ekbs_qa_message") == before, "失败后行不能被删掉"
    assert _count(runtime, "ekbs_qa_session") == 1
    assert _trigger_count(runtime) == EXPECTED_TRIGGERS, "回滚后触发器必须还在"


def test_pending_reports_expired_counts(runtime, archiver) -> None:
    _insert_turn(runtime, "old-session", age_days=200)
    pending = archiver.pending()
    assert pending["ekbs_qa_session"] == 1
    assert pending["ekbs_qa_message"] == 1
    assert pending["ekbs_perm_change_log"] == 0
