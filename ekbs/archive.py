"""审计归档 —— `01` §5.6 的「唯一例外」+ `00` §3.5 的留存期。

背景：审计表是 append-only（12 个触发器硬拒 UPDATE/DELETE）。但留存期到了必须能删，
于是 `01` §5.6 定义了**唯一一条**例外路径：

    在事务内 DROP 掉该表的 DELETE 触发器 → 删除超期行 → 立刻重建触发器，
    并在删除前把「归档了多少行」写入 ekbs_perm_change_log

三条纪律（本模块逐条落地）：

1. **单进程独占**：这条路径只能由 ``com.ekbs.scheduler`` 执行（D13：APScheduler 单实例），
   事务内没有并发写者 —— 否则会出现「触发器被摘掉」的窗口被别人钻。
2. **先归档再删除**：把待删行导出成 JSONL 落到 ``data_dir/backup/archive/``，
   再执行删除。「删了但没留底」不叫归档。
3. **触发器必须复原**：删完立刻重建，并且**同一事务**内完成；任何一步失败全部回滚。
   自检项：归档后触发器数仍为 12（`01` §5.6）。

留存期（`00` §3.5）：

| 表 | 留存 |
|---|---|
| ``ekbs_qa_message`` / ``ekbs_qa_citation`` / ``ekbs_qa_session`` / ``ekbs_doc_access_log`` | ≥180 天 |
| ``ekbs_perm_change_log`` | ≥365 天（合规要求） |

⚠️ ``ekbs_perm_change_log`` 也带 DELETE 触发器，因此它的归档同样要走本路径；
但它是「权限变更的合规证据」，删除前必须有 JSONL 留底（见第 2 条）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="archive")

#: 留存期（天）—— 权威来源 `00` §3.5
RETENTION_DAYS: dict[str, int] = {
    "ekbs_qa_citation": 180,
    "ekbs_qa_message": 180,
    "ekbs_qa_session": 180,
    "ekbs_doc_access_log": 180,
    "ekbs_perm_change_log": 365,
}

#: 各表用于判断「超期」的时间列（qa_session 用 started_at，其余用 created_at）
_TIME_COLUMN: dict[str, str] = {
    "ekbs_qa_citation": "created_at",
    "ekbs_qa_message": "created_at",
    "ekbs_qa_session": "started_at",
    "ekbs_doc_access_log": "created_at",
    "ekbs_perm_change_log": "created_at",
}

#: 需要摘掉再重建的 DELETE 触发器（逐字取自 `01` §5.6，**不要改措辞**）
_DELETE_TRIGGERS: dict[str, str] = {
    "ekbs_qa_citation": (
        "CREATE TRIGGER IF NOT EXISTS trg_qa_citation_no_delete "
        "BEFORE DELETE ON ekbs_qa_citation "
        "BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_citation is append-only (DELETE denied)'); END;"
    ),
    "ekbs_qa_message": (
        "CREATE TRIGGER IF NOT EXISTS trg_qa_message_no_delete "
        "BEFORE DELETE ON ekbs_qa_message "
        "BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_message is append-only (DELETE denied)'); END;"
    ),
    "ekbs_qa_session": (
        "CREATE TRIGGER IF NOT EXISTS trg_qa_session_no_delete "
        "BEFORE DELETE ON ekbs_qa_session "
        "BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_session is append-only (DELETE denied)'); END;"
    ),
    "ekbs_doc_access_log": (
        "CREATE TRIGGER IF NOT EXISTS trg_doc_access_log_no_delete "
        "BEFORE DELETE ON ekbs_doc_access_log "
        "BEGIN SELECT RAISE(ABORT, 'audit table ekbs_doc_access_log is append-only (DELETE denied)'); END;"
    ),
    "ekbs_perm_change_log": (
        "CREATE TRIGGER IF NOT EXISTS trg_perm_change_log_no_delete "
        "BEFORE DELETE ON ekbs_perm_change_log "
        "BEGIN SELECT RAISE(ABORT, 'audit table ekbs_perm_change_log is append-only (DELETE denied)'); END;"
    ),
}

#: 删除顺序：先子后父，避免外键 CASCADE 在触发器摘除窗口里产生意外级联
DELETE_ORDER = (
    "ekbs_qa_citation",
    "ekbs_qa_message",
    "ekbs_qa_session",
    "ekbs_doc_access_log",
    "ekbs_perm_change_log",
)


@dataclass
class ArchiveResult:
    table: str
    deleted: int
    retention_days: int
    archive_path: str = ""
    dry_run: bool = False


@dataclass
class ArchiveReport:
    results: list[ArchiveResult] = field(default_factory=list)
    trigger_count_after: int = 0

    @property
    def total_deleted(self) -> int:
        return sum(item.deleted for item in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_deleted": self.total_deleted,
            "trigger_count_after": self.trigger_count_after,
            "tables": [
                {"table": r.table, "deleted": r.deleted, "retention_days": r.retention_days}
                for r in self.results
            ],
        }


class AuditArchiver:
    """审计/日志表的归档（留存期到期后导出 JSONL 再删除）。"""

    def __init__(self, db: Database, settings: Settings, *, archive_dir: Path | None = None) -> None:
        self.db = db
        self.settings = settings
        self.archive_dir = archive_dir or (settings.data_paths()["backup"] / "archive")

    # ------------------------------------------------------------------

    def archive(
        self, *, dry_run: bool = False, retention_days: dict[str, int] | None = None
    ) -> ArchiveReport:
        retention = {**RETENTION_DAYS, **(retention_days or {})}
        report = ArchiveReport()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        with self.db.sync_connect() as conn:
            # 显式事务：DROP/CREATE TRIGGER 与 DELETE 必须原子完成。
            # 注意 sync_connect 是 autocommit（isolation_level=None），
            # 所以这里手工 BEGIN —— 否则「触发器摘掉了、删除失败」会留下永久缺口。
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in DELETE_ORDER:
                    days = int(retention.get(table, 180))
                    time_column = _TIME_COLUMN[table]
                    rows = conn.execute(
                        f"SELECT * FROM {table} WHERE {time_column} < datetime('now', ?)",
                        (f"-{days} days",),
                    ).fetchall()
                    if not rows:
                        report.results.append(ArchiveResult(table, 0, days, dry_run=dry_run))
                        continue

                    if dry_run:
                        # 演练只统计，绝不动数据、不动触发器
                        report.results.append(
                            ArchiveResult(table, len(rows), days, dry_run=True)
                        )
                        continue

                    archive_path = str(self._write_jsonl(table, stamp, rows))

                    # ① 摘掉 DELETE 触发器（仅本事务内可见）
                    conn.execute(f"DROP TRIGGER IF EXISTS trg_{table[5:]}_no_delete")
                    # ② 删除超期行
                    cursor = conn.execute(
                        f"DELETE FROM {table} WHERE {time_column} < datetime('now', ?)",
                        (f"-{days} days",),
                    )
                    deleted = int(cursor.rowcount or 0)
                    # ③ 立刻重建触发器（同一事务）
                    conn.execute(_DELETE_TRIGGERS[table])

                    # ④ 把「归档了多少行」写进 ekbs_perm_change_log（01 §5.6）
                    conn.execute(
                        "INSERT INTO ekbs_perm_change_log(object_type, object_id, "
                        "change_type, field, old_value, new_value, reason) "
                        "VALUES ('audit', ?, 'archive', 'retention_days', ?, ?, ?)",
                        (
                            table, str(days), str(deleted),
                            f"归档并清理 {deleted} 行（留底 {archive_path}）",
                        ),
                    )
                    report.results.append(
                        ArchiveResult(table, deleted, days, archive_path, dry_run=dry_run)
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                log.error("archive_failed_rolled_back")
                raise

            report.trigger_count_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%'"
                ).fetchone()[0]
            )

        log.info("archive_done", **report.as_dict(), dry_run=dry_run)
        return report

    # ------------------------------------------------------------------

    def _write_jsonl(self, table: str, stamp: str, rows: list[sqlite3.Row]) -> Path:
        """把待删行导出为 JSONL（归档留底：删了但没留底不叫归档）。"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self.archive_dir / f"{table}-{stamp}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({k: row[k] for k in row.keys()}, ensure_ascii=False))
                handle.write("\n")
        return path

    # ------------------------------------------------------------------

    def pending(self, retention_days: dict[str, int] | None = None) -> dict[str, int]:
        """各表当前有多少行已超期（给监控用，不修改数据）。"""
        retention = {**RETENTION_DAYS, **(retention_days or {})}
        counts: dict[str, int] = {}
        with self.db.sync_connect() as conn:
            for table, days in retention.items():
                column = _TIME_COLUMN[table]
                counts[table] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} < datetime('now', ?)",
                        (f"-{days} days",),
                    ).fetchone()[0]
                )
        return counts
