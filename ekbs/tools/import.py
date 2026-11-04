"""``python -m ekbs.tools.import`` —— 首批数据批量入库（`01` §12）。

文档里给的用法（本工具逐字支持这些参数）：

.. code-block:: bash

    python -m ekbs.tools.import \\
      --dir /data/ekbs/raw/人力资源制度 \\
      --kb-code hr-policy \\
      --level 2 \\
      --dept-ids 12,135 \\
      --effective-date 2026-01-01 \\
      --shard-id kb_hr_L2 \\
      --parallel 2

流水线（`01` §12 与 `06`）：原文 → 解析（MinerU / Office / 文本）→ 清洗 → 四层分块 →
云端嵌入（批量，带重试）→ 写 FAISS 分片 + SQLite + FTS5 → 门禁 → ``status=published``。

**两道闸门照旧生效**：合规闸（真实语料 + 云端嵌入，`00` §7）与 L3 闸（C5 未验收前，`00` C5）。
批量导入的是**真实文件**，因此默认按真实语料处理 —— 用 ``--synthetic`` 才能绕过合规闸
（对应 `seed_demo` 那种合成样本）。

验收（`01` §12 的表）：本工具会算**解析成功率**并在低于 98% 时以退出码 1 结束，
以便接进 CI/上线检查；同时核对另外两条硬指标：
``embedding_fingerprint`` 一致性、分片向量数与 ``ekbs_chunk`` 行数差值 0。

⚠️ 模块名 ``import`` 与 Python 关键字同名：命令行 ``python -m ekbs.tools.import`` 正常，
但**不能**写成 ``from ekbs.tools.import import ...``（语法错误）。
测试与其它代码请用 ``importlib.import_module("ekbs.tools.import")``。
（名字是 `01` §12 定的，保持与文档逐字一致。）

⚠️ **开放项 T3 的处置**（`docs/spec/20` 的待确认清单）：权威文档未定「算法派生分片」与
``--shard-id`` 显式指定谁优先。本工具取**显式优先**（操作者最清楚这批文件该进哪片），
并在两者不一致时**打印告警**、同时为全部 ``--dept-ids`` 写 shard 级 ACL 授权。
定稿后应改文档并同步本工具。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ekbs.config import Settings, load_settings
from ekbs.errors import ConfigError, EkbsError
from ekbs.index.shard import parse_shard_id, resolve_shard_for_document
from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.parser import ParserUnavailable, parse_file
from ekbs.ingest.dir_import import (
    checksum_of,
    discover_files,
    existing_doc_by_checksum,
)
from ekbs.ingest.parser import SUFFIX_ENGINES
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.logging import configure_logging, get_logger
from ekbs.runtime import Runtime

log = get_logger(component="tools.import")

#: 默认识别的扩展名 —— **直接取解析器真正实现的格式**（`parser.SUFFIX_ENGINES`）。
#:
#: 修正历史：原先这里硬编码 `(".md",".markdown",".txt",".docx",".xlsx",".pptx")`，
#: 于是 ① **PDF 不在默认白名单里**，不加 `--ext .pdf` 就一个都发现不了（静默漏掉，
#: 连"跳过"都不显示）；② 反而把解析器**没实现**的 `.pptx` 放进来，会被当纯文本读成乱码。
#: 现在与解析器单点对齐，并由 `tests/test_parser_mineru.py` 的用例锁住不再漂移。
DEFAULT_EXTENSIONS: tuple[str, ...] = tuple(SUFFIX_ENGINES)

#: 解析成功率达标线（`01` §12 的入库验收表）
SUCCESS_RATE_TARGET = 0.98

#: `--parallel` 上限：16 GB 机器上解析峰值不能叠加（`00` §5）
MAX_PARALLEL = 4


@dataclass
class ImportItem:
    path: str
    status: str  # imported | skipped | failed
    doc_id: int | None = None
    shard_id: str = ""
    vectors: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "status": self.status, "doc_id": self.doc_id,
            "shard_id": self.shard_id, "vectors": self.vectors, "error": self.error,
        }


@dataclass
class ImportReport:
    items: list[ImportItem] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def imported(self) -> list[ImportItem]:
        return [item for item in self.items if item.status == "imported"]

    @property
    def failed(self) -> list[ImportItem]:
        return [item for item in self.items if item.status == "failed"]

    @property
    def skipped(self) -> list[ImportItem]:
        return [item for item in self.items if item.status == "skipped"]

    @property
    def attempted(self) -> int:
        return len(self.imported) + len(self.failed)

    @property
    def success_rate(self) -> float:
        if not self.attempted:
            return 1.0
        return len(self.imported) / self.attempted

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": len(self.imported),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "success_rate": round(self.success_rate, 4),
            "success_rate_target": SUCCESS_RATE_TARGET,
            "checks": self.checks,
            "items": [item.as_dict() for item in self.items],
        }


# ---------------------------------------------------------------------------
# 文件发现与去重
# ---------------------------------------------------------------------------


def _existing_by_checksum(runtime: Runtime, checksum: str) -> int | None:
    """内容是否已在库（**薄封装**：真正口径在 :mod:`ekbs.ingest.dir_import`）。

    管理台目录导入与 CLI 必须用同一份去重口径，否则会出现「界面说重复、CLI 说没有」
    这种没法排查的分歧。
    """
    return existing_doc_by_checksum(runtime.db, checksum)


# ---------------------------------------------------------------------------
# 知识库与授权
# ---------------------------------------------------------------------------


def ensure_kb(runtime: Runtime, code: str, *, level: int, name: str | None = None) -> int:
    """按 ``--kb-code`` 取知识库；不存在则建（试点便利，写入日志留痕）。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (code,)).fetchone()
        if row is not None:
            return int(row["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, description, default_level, status, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (code, name or code, "由 ekbs.tools.import 自动创建", int(level)),
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    log.warning("kb_auto_created", code=code, kb_id=kb_id, level=level)
    return kb_id


def grant_dept_shards(runtime: Runtime, shard_id: str, dept_ids: Sequence[str]) -> int:
    """为 ``--dept-ids`` 里的每个部门写 shard 级 ACL allow（`06` §2.2 的横向授权）。"""
    if not dept_ids:
        return 0
    written = 0
    with runtime.db.sync_connect() as conn:
        for dept in dept_ids:
            cursor = conn.execute(
                "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
                "permission, effect, created_at) "
                "VALUES ('shard', ?, 'dept', ?, 'read', 'allow', datetime('now')) "
                "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
                "DO NOTHING",
                (shard_id, str(dept)),
            )
            written += int(cursor.rowcount or 0)
        if written:
            from ekbs.permission.router import bump_policy_version

            # ACL 与 policy_version 必须同事务自增（06 §2.6）
            bump_policy_version(conn, reason=f"import:dept_shards:{shard_id}")
        conn.commit()
    return written


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


async def import_directory(
    settings: Settings,
    *,
    directory: Path,
    kb_code: str,
    level: int = 2,
    dept_ids: Sequence[str] = (),
    effective_date: str | None = None,
    expire_date: str | None = None,
    shard_id: str | None = None,
    extensions: Sequence[str] = DEFAULT_EXTENSIONS,
    recursive: bool = True,
    limit: int | None = None,
    parallel: int = 2,
    synthetic: bool = False,
    dry_run: bool = False,
    stop_on_error: bool = False,
) -> ImportReport:
    settings.ensure_dirs()
    runtime = Runtime.create(settings, service="tools", init_schema=True)
    report = ImportReport()
    try:
        files = discover_files(directory, extensions=extensions, recursive=recursive, limit=limit)
        if dry_run:
            for path in files:
                report.items.append(ImportItem(str(path), "skipped", error="dry-run"))
            report.checks["dry_run"] = True
            report.checks["files"] = len(files)
            return report

        # 闸门前置检查：配置类错误必须**整批快速失败**，而不是变成 N 条「每个文件都失败」。
        # 否则关着合规闸跑 200 个文件，操作者只会看到 200 条失败，看不出真正原因。
        settings.assert_l3_ingest_allowed(level)
        settings.assert_compliance_gate(real_corpus=not synthetic)

        kb_id = ensure_kb(runtime, kb_code, level=level)
        pipeline = IngestPipeline(
            db=runtime.db, settings=settings, providers=runtime.providers,
            shards=runtime.shards, fts=runtime.fts,
            figure_index=runtime.figure_index,
        )
        concurrency = max(1, min(int(parallel), MAX_PARALLEL))
        if int(parallel) > MAX_PARALLEL:
            log.warning("parallel_clamped", configured=int(parallel), used=concurrency,
                        reason="16 GB 机器上解析峰值不能叠加（00 §5）")
        semaphore = asyncio.Semaphore(concurrency)

        # 显式 --shard-id：先校验命名，再与派生结果比对（T3 的处置见模块 docstring）
        if shard_id:
            shard_dept, shard_level = parse_shard_id(shard_id)
            from ekbs.index.faiss_store import ensure_shard_for_placement

            ensure_shard_for_placement(
                runtime.shards, shard_id, level=shard_level, dept_code=shard_dept,
                fingerprint=runtime.providers.embed.ref.fingerprint,
            )
            derived = resolve_shard_for_document(
                level=level, visible_dept_ids=list(dept_ids), settings=settings
            ).primary_shard_id
            if derived != shard_id:
                log.warning(
                    "explicit_shard_overrides_derived",
                    explicit=shard_id, derived=derived,
                    note="开放项 T3：本工具以显式指定为准（docs/spec/20）",
                )

        async def one(path: Path) -> ImportItem:
            async with semaphore:
                return await _import_one(
                    runtime, pipeline, path,
                    kb_id=kb_id, level=level, dept_ids=list(dept_ids), shard_id=shard_id,
                    effective_date=effective_date, expire_date=expire_date,
                    synthetic=synthetic,
                )

        for path in files:
            try:
                item = await one(path)
            except ConfigError:
                # 配置/闸门类错误不允许被「降级成单个文件的失败」
                raise
            except Exception as exc:  # 单个文件失败不该中断整批（除非显式要求）
                log.error("import_item_crashed", path=str(path), error=str(exc))
                item = ImportItem(str(path), "failed", error=f"{type(exc).__name__}: {exc}")
                if stop_on_error:
                    report.items.append(item)
                    raise
            report.items.append(item)
            log.info("import_item", **item.as_dict())

        report.checks.update(_post_checks(runtime, report))
        return report
    finally:
        await runtime.aclose()


async def _import_one(
    runtime: Runtime,
    pipeline: IngestPipeline,
    path: Path,
    *,
    kb_id: int,
    level: int,
    dept_ids: list[str],
    shard_id: str | None,
    effective_date: str | None,
    expire_date: str | None,
    synthetic: bool,
) -> ImportItem:
    checksum = checksum_of(path)
    duplicate = _existing_by_checksum(runtime, checksum)
    if duplicate is not None:
        return ImportItem(str(path), "skipped", doc_id=duplicate, error="checksum 重复（秒传）")

    # 图片落盘目录：此刻还没建文档行（doc_id 在 ingest_markdown 里才生成），
    # 所以用**内容哈希**做稳定目录名 —— 同一份文件重跑会复用同一目录，不会无限堆积。
    assets_dir = runtime.settings.data_paths()["parse"] / f"sha-{checksum[:16]}"
    try:
        parsed = await asyncio.to_thread(parse_file, path, assets_dir=assets_dir)
    except ParserUnavailable as exc:
        return ImportItem(str(path), "failed", error=str(exc))

    figures = [
        FigureInput(
            image_path=str((assets_dir / item.name).relative_to(runtime.settings.data_dir)),
            caption=item.caption,
            ocr_text=item.ocr_text,
            page=item.page,
            figure_id=item.figure_id,
        )
        for item in parsed.figures
    ]
    tables = [
        TableInput(
            body=item.body,
            caption=item.caption,
            page=item.page,
            table_id=item.table_id,
            preserve_layout=item.preserve_layout,
        )
        for item in parsed.tables
    ]

    # 分片：显式优先，否则按 (level × 部门) 派生
    if shard_id:
        placement_shard = shard_id
        extra_grants: list[str] = list(dept_ids)
    else:
        placement = resolve_shard_for_document(
            level=level, visible_dept_ids=dept_ids, settings=runtime.settings
        )
        placement_shard = placement.primary_shard_id
        # 为**全部** --dept-ids 写 shard 级 allow，而不是只写「非主部门」：
        # 主部门能否路由到这片取决于 dept_code_map（开放项 O6-1，生产可能尚未配置）。
        # 多写一条 allow 不会放宽语义（该部门本来就该看到这批文档），却能让入库结果
        # 不依赖那张还没定稿的映射表。
        extra_grants = list(dept_ids)

    result = await pipeline.ingest_markdown(
        title=path.stem,
        markdown=parsed.markdown,
        kb_id=kb_id,
        level=level,
        visible_dept_ids=dept_ids,
        source_path=str(path),
        page_map=list(parsed.page_map),
        page_count=parsed.page_count,
        figures=figures,
        tables=tables,
        header_meta=parsed.header_meta,
        real_corpus=not synthetic,
        checksum=checksum,
    )
    if result.shard_id != placement_shard:
        # ingest 内部按 (level × 部门) 派生；显式指定时把 chunk 归到目标分片
        await asyncio.to_thread(
            _move_to_shard, runtime, result.doc_id, placement_shard
        )
        result.shard_id = placement_shard
    grant_dept_shards(runtime, placement_shard, extra_grants)
    _apply_dates(runtime, result.doc_id, effective_date=effective_date, expire_date=expire_date)
    return ImportItem(
        str(path), "imported", doc_id=result.doc_id, shard_id=placement_shard,
        vectors=result.vector_count,
    )


def _move_to_shard(runtime: Runtime, doc_id: int, shard_id: str) -> int:
    """把文档的分块改挂到指定分片（显式 ``--shard-id`` 路径）。

    只改 ``ekbs_chunk.shard_id`` 与 FTS 行；向量由 :meth:`IngestPipeline.ingest_markdown`
    已经写入派生分片，因此这里同时把登记值修正 —— 显式指定分片属于**试点期的人工兜底**，
    正式口径（T3）定稿后应改为「派生与显式必须一致，否则拒绝」。
    """
    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_chunk SET shard_id = ? WHERE doc_id = ?", (shard_id, doc_id))
        conn.commit()
    runtime.fts.delete_chunk_ids(
        [
            int(r["chunk_id"])
            for r in _chunk_ids(runtime, doc_id)
        ]
    )
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, c.content, c.heading_path FROM ekbs_chunk c "
            f"WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE} ORDER BY c.seq",
            (doc_id,),
        ).fetchall()
    runtime.fts.upsert_chunks(
        [(int(r["chunk_id"]), shard_id, str(r["content"]), r["heading_path"]) for r in rows]
    )
    return len(rows)


def _chunk_ids(runtime: Runtime, doc_id: int) -> list[Any]:
    with runtime.db.sync_connect() as conn:
        return list(
            conn.execute("SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)).fetchall()
        )


def _apply_dates(
    runtime: Runtime, doc_id: int, *, effective_date: str | None, expire_date: str | None
) -> None:
    if not effective_date and not expire_date:
        return
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET effective_date = COALESCE(?, effective_date), "
            "expire_date = COALESCE(?, expire_date), updated_at = datetime('now') "
            "WHERE doc_id = ?",
            (effective_date, expire_date, doc_id),
        )
        conn.commit()


def _post_checks(runtime: Runtime, report: ImportReport) -> dict[str, Any]:
    """`01` §12 验收表的另两条硬指标：向量数一致、指纹一致。"""
    imported_ids = [item.doc_id for item in report.imported if item.doc_id]
    checks: dict[str, Any] = {
        "success_rate": round(report.success_rate, 4),
        "success_rate_ok": report.success_rate >= SUCCESS_RATE_TARGET,
    }
    if not imported_ids:
        return checks
    placeholders = ",".join("?" for _ in imported_ids)
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.shard_id AS shard_id, COUNT(*) AS chunks, "
            "       COUNT(DISTINCT c.embedding_fingerprint) AS fingerprints "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            f"WHERE {RETRIEVABLE_PREDICATE} AND d.doc_id IN ({placeholders}) "
            "GROUP BY c.shard_id",
            tuple(imported_ids),
        ).fetchall()
    current = runtime.providers.embed.ref.fingerprint
    mismatches: list[str] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["shard_id"])] = int(row["chunks"])
        if int(row["fingerprints"]) > 1:
            mismatches.append(f"{row['shard_id']}: 同批存在多个嵌入指纹")
        registered = {r.shard_id: r for r in runtime.shards.registry()}
        shard = registered.get(str(row["shard_id"]))
        if shard is not None and shard.embedding_fingerprint != current:
            mismatches.append(f"{shard.shard_id}: 分片登记指纹与当前模型不一致")
    touched = set(counts)
    shard_checks = {c.shard_id: c for c in runtime.shards.self_check() if c.shard_id in touched}
    bad_shards = [f"{sid}: {c.detail}" for sid, c in shard_checks.items() if not c.ok]
    checks.update(
        imported_chunks=sum(counts.values()),
        shard_vector_total=runtime.shards.total_vectors(),
        shards_touched=sorted(touched),
        shard_registration_ok=not bad_shards,
        shard_registration_problems=bad_shards,
        fingerprint_consistent=not mismatches,
        fingerprint_problems=mismatches,
    )
    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量导入文档（01 §12）")
    parser.add_argument("--dir", required=True, help="待导入目录")
    parser.add_argument("--kb-code", required=True, help="知识库 code（不存在则创建）")
    parser.add_argument("--level", type=int, default=2, help="密级 1..4（默认 2）")
    parser.add_argument("--dept-ids", default="", help="可见部门 ID，逗号分隔，如 12,135")
    parser.add_argument("--effective-date", default=None, help="生效日期 YYYY-MM-DD")
    parser.add_argument("--expire-date", default=None, help="失效日期 YYYY-MM-DD")
    parser.add_argument("--shard-id", default=None, help="显式指定目标分片（优先于派生）")
    parser.add_argument("--ext", default=",".join(DEFAULT_EXTENSIONS), help="扩展名白名单")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="只导入前 N 个文件（先小批验证）")
    parser.add_argument("--parallel", type=int, default=2, help="并发文件数（上限 4）")
    parser.add_argument(
        "--synthetic", action="store_true",
        help="声明是合成/公开样本，绕过合规闸（00 §7）；真实语料不要加这个参数",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出将导入的文件")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    dept_ids = [d.strip() for d in args.dept_ids.split(",") if d.strip()]
    extensions = tuple(e.strip() if e.strip().startswith(".") else f".{e.strip()}"
                       for e in args.ext.split(",") if e.strip())

    try:
        report = asyncio.run(
            import_directory(
                settings,
                directory=Path(args.dir),
                kb_code=args.kb_code,
                level=args.level,
                dept_ids=dept_ids,
                effective_date=args.effective_date,
                expire_date=args.expire_date,
                shard_id=args.shard_id,
                extensions=extensions,
                recursive=not args.no_recursive,
                limit=args.limit,
                parallel=args.parallel,
                synthetic=args.synthetic,
                dry_run=args.dry_run,
                stop_on_error=args.stop_on_error,
            )
        )
    except (EkbsError, FileNotFoundError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 闸门拦截等
        print(f"导入中止：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"发现文件 {len(report.items)}；成功 {len(report.imported)}、失败 {len(report.failed)}、"
              f"跳过 {len(report.skipped)}")
        print(f"解析成功率 {report.success_rate:.2%}（达标线 {SUCCESS_RATE_TARGET:.0%}）")
        for item in report.failed:
            print(f"  [失败] {item.path}：{item.error}")
        print(f"验收核对：{report.checks}")

    if report.failed or report.success_rate < SUCCESS_RATE_TARGET:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
