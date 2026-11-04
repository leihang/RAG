"""SQLite 数据访问层 —— `01` §5.0（连接参数）+ §5.7（初始化与校验）。

分工：

* **Schema 应用与校验**用标准库 ``sqlite3``（一次性、同步、可被 CLI 与测试直接调）；
* **在线查询**用 SQLAlchemy 2.0 ``AsyncSession`` + aiosqlite（`04` §5.1 口径），
  PRAGMA 由 ``connect`` 事件监听器在**每次建连**时设置 —— ``foreign_keys`` 是连接级开关，
  ``journal_mode`` 是持久化到库文件的开关，两者都不适合写进一次性初始化脚本（`01` §5.0）。

所有对外入口都会把失败变成 :class:`~ekbs.errors.SchemaError`，因为「库不对」必须在上线前
被发现：``00`` §2.3 把「FAISS 未加载」「嵌入指纹不一致」都定为**拒绝服务**，Schema 同理。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ekbs.config import Settings
from ekbs.errors import SchemaError

#: `01` §5 的权威表数（含 FTS5 虚拟表 ``ekbs_chunk_fts``，不含 5 张 FTS5 影子表）
EXPECTED_TABLES = 33

#: `01` §5.6 的审计触发器数（5 张表 × UPDATE+DELETE + ``ekbs_qa_session`` 的 UPDATE 守卫 = 12）
EXPECTED_TRIGGERS = 12

#: smoke.sh 与 `01` §5.7 使用的表数查询（必须排除 FTS5 影子表，否则得到 34）
_TABLE_COUNT_SQL = (
    "SELECT COUNT(*) FROM sqlite_master "
    "WHERE type IN ('table','view') AND name LIKE 'ekbs%' "
    "AND name NOT LIKE 'ekbs_chunk_fts_%'"
)

_TRIGGER_COUNT_SQL = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_%'"


@dataclass(frozen=True)
class SchemaReport:
    """Schema 自检结果（供启动日志、``/readyz`` 与冒烟脚本使用）。"""

    path: str
    exists: bool
    tables: int
    triggers: int
    policy_version: int | None
    journal_mode: str
    integrity: str

    @property
    def ok(self) -> bool:
        return (
            self.exists
            and self.tables == EXPECTED_TABLES
            and self.triggers == EXPECTED_TRIGGERS
            and self.policy_version is not None
            and self.integrity == "ok"
            and self.journal_mode.lower() == "wal"
        )

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.exists:
            return [f"数据库文件不存在：{self.path}"]
        if self.tables != EXPECTED_TABLES:
            issues.append(f"表数 {self.tables} ≠ {EXPECTED_TABLES}（01 §5）")
        if self.triggers != EXPECTED_TRIGGERS:
            issues.append(f"审计触发器数 {self.triggers} ≠ {EXPECTED_TRIGGERS}（01 §5.6）")
        if self.policy_version is None:
            issues.append("ekbs_policy_version 无数据（01 §5.3 种子缺失）")
        if self.integrity != "ok":
            issues.append(f"PRAGMA integrity_check = {self.integrity}")
        if self.journal_mode.lower() != "wal":
            issues.append(f"journal_mode = {self.journal_mode}，期望 wal（01 §5.0）")
        return issues


def _apply_pragmas(conn: sqlite3.Connection, wal: bool = True) -> None:
    """`01` §5.0 的连接参数，**每次建连都要设**。"""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")


class Database:
    """SQLite 门面：Schema 生命周期 + 异步会话 + 同步连接。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.database.path)
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def schema_sql(self) -> str:
        sql_path = self.settings.schema_sql_path
        if not sql_path.is_file():
            raise SchemaError(f"Schema 文件不存在：{sql_path}（权威定义见 01 §5）")
        return sql_path.read_text(encoding="utf-8")

    def apply_schema(self, *, force: bool = False) -> SchemaReport:
        """在空库上建立全部对象；幂等（DDL 全部 ``IF NOT EXISTS``）。

        ``force=True`` 时即便已初始化也重跑一遍 —— 用于验收「可重复执行不报错」。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        with self.sync_connect() as conn:
            before = conn.execute(_TABLE_COUNT_SQL).fetchone()[0]
            if force or before != EXPECTED_TABLES:
                conn.executescript(self.schema_sql())
                conn.commit()
        self.apply_schema_patches()
        return self.verify_schema()

    def apply_schema_patches(self) -> None:
        """对已有库做**向前兼容**的轻量补丁（``ALTER TABLE`` 幂等）。

        新建库靠 ``001_init.sql`` 已含这些列；存量库在 init_db / 服务启动时补齐，
        避免「必须重建库才能上软删除」。
        """
        if not self.path.is_file():
            return
        with self.sync_connect() as conn:
            cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ekbs_document)").fetchall()
            }
            if "deleted_at" not in cols:
                conn.execute("ALTER TABLE ekbs_document ADD COLUMN deleted_at TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_doc_deleted "
                    "ON ekbs_document(deleted_at) WHERE deleted_at IS NOT NULL"
                )
                conn.commit()
            if "parse_ms" not in cols:
                conn.execute("ALTER TABLE ekbs_document ADD COLUMN parse_ms INTEGER")
                conn.commit()
            # 表格父容器分段组：超 2048 的整表按行分段后共用组 id，检索时一次展开回全表。
            # 存量库缺这列时 INSERT 直接失败、父块扩展也取不回整表 —— 与 deleted_at 一样
            # 必须在启动自检后补齐；新库由 001_init.sql 建好。
            chunk_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ekbs_chunk)").fetchall()
            }
            if "table_group" not in chunk_cols:
                conn.execute("ALTER TABLE ekbs_chunk ADD COLUMN table_group TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chunk_table_group "
                    "ON ekbs_chunk(doc_id, table_group)"
                )
                conn.commit()
            # 引用身份拆分：chunk_id（喂给 LLM 的上下文块）与 source_chunk_id（精排来源块）。
            # 存量库缺这列会让审计 INSERT 直接失败，必须像 deleted_at 一样在启动自检后补齐。
            citation_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ekbs_qa_citation)").fetchall()
            }
            if "source_chunk_id" not in citation_cols:
                conn.execute(
                    "ALTER TABLE ekbs_qa_citation ADD COLUMN source_chunk_id INTEGER"
                )
                conn.commit()
            # 「模型上下文」落审计：引用只说明引用了哪块，不等于喂了哪块。
            # 存量库缺该列时审计 INSERT 会直接失败，故与 source_chunk_id 同样在启动时补齐。
            message_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ekbs_qa_message)").fetchall()
            }
            if "context_chunk_ids" not in message_cols:
                conn.execute(
                    "ALTER TABLE ekbs_qa_message ADD COLUMN context_chunk_ids TEXT"
                )
                conn.commit()
            policy_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ekbs_policy_version)").fetchall()
            }
            additions = (
                ("org_sync_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("org_sync_interval_hours", "INTEGER NOT NULL DEFAULT 24"),
                ("org_sync_armed_at", "TEXT"),
                ("org_sync_running_at", "TEXT"),
            )
            for name, ddl in additions:
                if name not in policy_cols:
                    conn.execute(f"ALTER TABLE ekbs_policy_version ADD COLUMN {name} {ddl}")
            # 推荐问答 tier-3：文档级「代表问句」表（新库由 001_init.sql 建，存量库在此补建）。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ekbs_doc_recommend ("
                " doc_id INTEGER PRIMARY KEY REFERENCES ekbs_document(doc_id),"
                " question TEXT NOT NULL,"
                " rerank_score REAL,"
                " status TEXT NOT NULL DEFAULT 'verified',"
                " created_by INTEGER,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                " updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_doc_recommend_status "
                "ON ekbs_doc_recommend(status)"
            )
            conn.commit()

    def verify_schema(self) -> SchemaReport:
        """按 `01` §5.7 与 ``deploy/scripts/smoke.sh`` 的同一口径自检。"""
        if not self.path.is_file():
            return SchemaReport(str(self.path), False, 0, 0, None, "unknown", "missing")
        with self.sync_connect() as conn:
            tables = int(conn.execute(_TABLE_COUNT_SQL).fetchone()[0])
            triggers = int(conn.execute(_TRIGGER_COUNT_SQL).fetchone()[0])
            row = conn.execute("SELECT version FROM ekbs_policy_version WHERE id = 1").fetchone()
            policy_version = int(row[0]) if row else None
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        return SchemaReport(
            path=str(self.path),
            exists=True,
            tables=tables,
            triggers=triggers,
            policy_version=policy_version,
            journal_mode=journal_mode,
            integrity=integrity,
        )

    def require_ready(self) -> SchemaReport:
        """启动自检：不合格即拒绝服务（`00` §2.3 的「拒绝启动」类故障）。"""
        report = self.verify_schema()
        if not report.ok:
            raise SchemaError("Schema 自检未通过：" + "；".join(report.problems()))
        # 补丁在自检通过后跑：存量库缺 deleted_at 不算 Schema 坏，但必须能软删。
        self.apply_schema_patches()
        return report

    def ensure_schema(self) -> tuple[SchemaReport, bool]:
        """库缺失或表数不符时建库；返回 ``(报告, 是否新建/补建)``。

        **只由显式入口调用**（``ekbs.tools.*`` 与 ``init_db``）。在线服务（api/stream/
        worker/scheduler）仍然走 :meth:`require_ready` 严格失败 —— 生产环境建库是
        ``deploy/scripts/install.sh`` 的职责（`01` §5.7），静默建库会掩盖部署事故。
        """
        report = self.verify_schema()
        if report.exists and report.tables == EXPECTED_TABLES:
            self.apply_schema_patches()
            return report, False
        return self.apply_schema(), True

    # ------------------------------------------------------------------
    # 同步连接（CLI / 工具 / 索引写入 / 测试）
    # ------------------------------------------------------------------

    @contextmanager
    def sync_connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn, wal=self.settings.database.wal)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 异步会话（FastAPI 请求路径）
    # ------------------------------------------------------------------

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.path}",
                future=True,
                pool_pre_ping=True,
            )
            wal = self.settings.database.wal

            @event.listens_for(engine.sync_engine, "connect")
            def _on_connect(dbapi_connection: Any, _record: Any) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys = ON")
                    cursor.execute("PRAGMA busy_timeout = 5000")
                    if wal:
                        cursor.execute("PRAGMA journal_mode = WAL")
                        cursor.execute("PRAGMA synchronous = NORMAL")
                finally:
                    cursor.close()

            self._engine = engine
            self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        return self._engine

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        self.engine()
        assert self._sessionmaker is not None
        return self._sessionmaker

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        """事务边界：正常提交，异常回滚。"""
        async with self.session_factory()() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        """``/healthz`` 用的轻量探活（不出网）。"""
        try:
            async with self.engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
