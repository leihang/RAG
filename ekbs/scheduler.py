"""``python -m ekbs.scheduler`` —— APScheduler 单实例定时作业（`00` D13、`04` §7.1.4）。

为什么必须是**独立进程**：定时作业若挂在 uvicorn 上，多 worker 会重复执行；
本栈固定 ``workers=1`` 也只是「当前不炸」，不是设计。因此所有定时任务集中在这里，
单进程单写者，与 `01` §5.6 的归档路径「事务内 DROP 触发器 → 删除 → 重建」相容。

作业清单（8 项，`04` §7.1.4）：

| 作业 | 频率 | 现状 |
|---|---|---|
| ``cache_cleanup`` | 10 min | ✅ 已实现（`ekbs_cache` 过期行） |
| ``metrics_collect`` | 60 s | ✅ 已实现（写 ``ekbs_metric``） |
| ``policy_version_mirror`` | 60 s | ✅ 已实现（只读镜像，权威在 SQLite） |
| ``session_ctx_cleanup`` | 30 min | ✅ 已实现（`ekbs_session_ctx` 过期） |
| ``rate_limit_cleanup`` | 10 min | ✅ 已实现（`ekbs_rate_limit` 滑窗回收） |
| ``msg_dedup_cleanup`` | 1 h | ✅ 已实现（`ekbs_msg_dedup` 7 天） |
| ``reconcile`` | 每日 01:30 | ◐ 部分实现（SQLite↔FTS5 计数对账；FAISS 侧复用自检） |
| ``org_sync`` / ``audit_archive`` | 每日 | ⏳ 需钉钉凭据 / 归档实现（见 `04` §6.3） |
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ekbs.config import load_settings
from ekbs.logging import get_logger
from ekbs.observability.alerts import evaluate_all, record_alerts
from ekbs.observability.metrics import snapshot
from ekbs.runtime import Runtime

log = get_logger(component="scheduler")

SESSION_CONTEXT_TTL_DEFAULT = 7200
MSG_DEDUP_RETAIN_DAYS = 7
RATE_LIMIT_WINDOW_SECONDS = 600


def job_cache_cleanup(runtime: Runtime) -> None:
    asyncio.get_event_loop().create_task(runtime.cache.cleanup_expired())


def job_metrics_collect(runtime: Runtime) -> None:
    """采集指标并**做告警判定**（`04` §7.7：阈值只在 ``observability/alerts.py`` 定义一处）。

    两步：① 主机侧指标 + 进程内登记表快照 → 写 ``ekbs_metric``；
    ② 按阈值表逐条比对窗口，命中写 ``ekbs_security_event``（P1/P2），P3 只记日志。
    """
    values = {
        "faiss_shards": float(runtime.shards.count_active()),
        "faiss_vectors": float(runtime.shards.total_vectors()),
        "disk_used_pct": float(runtime.disk_used_pct()),
    }
    with runtime.db.sync_connect() as conn:
        cache_rows = int(conn.execute("SELECT COUNT(*) FROM ekbs_cache").fetchone()[0])
        doc_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_document WHERE status = 'published'"
            ).fetchone()[0]
        )
    values["cache_rows"] = float(cache_rows)
    values["published_docs"] = float(doc_rows)

    # 进程内指标快照（计数器读后清零、仪表量保留 —— 与 Prometheus 语义一致）
    in_process = snapshot()
    rows: list[tuple[str, float, str | None]] = [
        (name, value, None) for name, value in values.items()
    ]
    for name, value, labels in in_process:
        import json as _json

        rows.append((name, float(value), _json.dumps(labels, ensure_ascii=False) if labels else None))
    with runtime.db.sync_connect() as conn:
        conn.executemany(
            "INSERT INTO ekbs_metric(name, value, labels, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            rows,
        )
        conn.commit()

    # 告警判定（阈值唯一来源：observability/alerts.py 的 ALERT_RULES）
    hits = evaluate_all(runtime.db)
    if hits:
        record_alerts(runtime.db, hits)
    log.debug("metrics_collected", host=values, in_process=len(in_process), alerts=len(hits))


def job_policy_version_mirror(runtime: Runtime) -> None:
    """``ekbs:perm:policy_version`` 只读镜像，TTL 60s（权威永远是 SQLite，`04` §4.6）。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT version FROM ekbs_policy_version WHERE id = 1").fetchone()
        version = int(row["version"]) if row else 0
    runtime.cache.l1_set("ekbs:perm:policy_version", version, ttl=60)
    runtime.cache._l2_set("ekbs:perm:policy_version", version, ttl=60)  # noqa: SLF001


def job_session_ctx_cleanup(runtime: Runtime) -> None:
    ttl = int(runtime.settings.session.get("context_ttl_seconds", SESSION_CONTEXT_TTL_DEFAULT))
    cutoff = int(time.time()) - ttl
    with runtime.db.sync_connect() as conn:
        cursor = conn.execute(
            "DELETE FROM ekbs_session_ctx WHERE strftime('%s', updated_at) < ?", (cutoff,)
        )
        conn.commit()
        if cursor.rowcount:
            log.info("session_ctx_cleaned", removed=cursor.rowcount)


def job_rate_limit_cleanup(runtime: Runtime) -> None:
    cutoff = int(time.time()) - RATE_LIMIT_WINDOW_SECONDS
    with runtime.db.sync_connect() as conn:
        cursor = conn.execute("DELETE FROM ekbs_rate_limit WHERE window_start < ?", (cutoff,))
        conn.commit()
        if cursor.rowcount:
            log.info("rate_limit_cleaned", removed=cursor.rowcount)


def job_msg_dedup_cleanup(runtime: Runtime) -> None:
    with runtime.db.sync_connect() as conn:
        cursor = conn.execute(
            "DELETE FROM ekbs_msg_dedup WHERE created_at < datetime('now', ?)",
            (f"-{MSG_DEDUP_RETAIN_DAYS} days",),
        )
        conn.commit()
        if cursor.rowcount:
            log.info("msg_dedup_cleaned", removed=cursor.rowcount)


def job_reconcile(runtime: Runtime) -> None:
    """SQLite ↔ FTS5 ↔ FAISS 三方对账（`03` §5.4：差异率目标 <0.01%）。"""
    with runtime.db.sync_connect() as conn:
        chunk_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE c.chunk_type = 'child' AND d.status = 'published'"
            ).fetchone()[0]
        )
    fts_rows = runtime.fts.count()
    faiss_vectors = runtime.shards.total_vectors()
    checks = runtime.shards.self_check()
    bad = [c for c in checks if not c.ok]
    diff = abs(chunk_rows - fts_rows)
    ratio = (diff / chunk_rows) if chunk_rows else 0.0
    log.info(
        "reconcile_done",
        published_children=chunk_rows,
        fts_rows=fts_rows,
        faiss_vectors=faiss_vectors,
        diff_ratio=round(ratio, 6),
        shard_problems=len(bad),
    )
    if ratio > 0.0001 or bad:
        log.error(
            "reconcile_mismatch",
            diff_ratio=round(ratio, 6),
            shard_problems=[c.detail for c in bad][:5],
        )


def job_org_sync(runtime: Runtime) -> None:
    """组织同步（04 §4）：全量 + 离职回看。

    凭据缺失时**只告警不报错** —— 开发环境没有钉钉凭据是正常状态；
    但一旦有凭据，这里就是保证「权限回收 ≤5 分钟」的那条路径（04 §4.5）。
    """
    import asyncio

    from ekbs.dingtalk.sync import build_sync_service
    from ekbs.errors import ConfigError

    try:
        service = build_sync_service(runtime.db, runtime.settings, runtime.secrets, cache=runtime.cache)
    except ConfigError as exc:
        log.warning("org_sync_skipped_no_credentials", reason=str(exc))
        return
    try:
        report = asyncio.run(service.sync_full())
        leave = asyncio.run(service.sync_leave_records(days=1))
        log.info("org_sync_done", **report.as_dict(), leave_deleted=leave.deleted)
    except Exception as exc:
        log.error("org_sync_failed", error=f"{type(exc).__name__}: {exc}")


def job_audit_archive(runtime: Runtime) -> None:
    """审计归档（00 §3.5 留存期 + 01 §5.6 的触发器摘除例外）。

    独占执行：本进程是 APScheduler 单实例（D13），因此事务内没有其他写者。
    """
    from ekbs.archive import AuditArchiver

    report = AuditArchiver(runtime.db, runtime.settings).archive()
    if report.total_deleted:
        log.info("audit_archived", **report.as_dict())
    if report.trigger_count_after != 12:
        # 触发器没复原 = append-only 保护出现缺口，必须告警（01 §5.6）
        log.error(
            "audit_trigger_count_unexpected", count=report.trigger_count_after, expected=12
        )


def build_scheduler(runtime: Runtime) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=runtime.settings.app.timezone)
    scheduler.add_job(job_cache_cleanup, IntervalTrigger(minutes=10), args=[runtime],
                      id="cache_cleanup", replace_existing=True)
    scheduler.add_job(job_metrics_collect, IntervalTrigger(seconds=60), args=[runtime],
                      id="metrics_collect", replace_existing=True)
    scheduler.add_job(job_policy_version_mirror, IntervalTrigger(seconds=60), args=[runtime],
                      id="policy_version_mirror", replace_existing=True)
    scheduler.add_job(job_session_ctx_cleanup, IntervalTrigger(minutes=30), args=[runtime],
                      id="session_ctx_cleanup", replace_existing=True)
    scheduler.add_job(job_rate_limit_cleanup, IntervalTrigger(minutes=10), args=[runtime],
                      id="rate_limit_cleanup", replace_existing=True)
    scheduler.add_job(job_msg_dedup_cleanup, IntervalTrigger(hours=1), args=[runtime],
                      id="msg_dedup_cleanup", replace_existing=True)
    scheduler.add_job(job_reconcile, CronTrigger(hour=1, minute=30), args=[runtime],
                      id="reconcile", replace_existing=True)
    scheduler.add_job(job_org_sync, CronTrigger(hour=2, minute=30), args=[runtime],
                      id="org_sync", replace_existing=True)
    scheduler.add_job(job_audit_archive, CronTrigger(hour=3, minute=0), args=[runtime],
                      id="audit_archive", replace_existing=True)
    return scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EKBS 定时作业调度器（APScheduler 单实例）")
    parser.add_argument("--once", action="store_true", help="立即跑一遍全部作业后退出（联调/巡检）")
    args = parser.parse_args(argv)

    settings = load_settings()
    runtime = Runtime.create(settings, service="scheduler")
    jobs = [
        ("cache_cleanup", job_cache_cleanup),
        ("metrics_collect", job_metrics_collect),
        ("policy_version_mirror", job_policy_version_mirror),
        ("session_ctx_cleanup", job_session_ctx_cleanup),
        ("rate_limit_cleanup", job_rate_limit_cleanup),
        ("msg_dedup_cleanup", job_msg_dedup_cleanup),
        ("reconcile", job_reconcile),
        ("org_sync", job_org_sync),
        ("audit_archive", job_audit_archive),
    ]
    try:
        if args.once:
            for name, job in jobs:
                try:
                    job(runtime) if name != "cache_cleanup" else asyncio.run(
                        runtime.cache.cleanup_expired()
                    )
                except Exception as exc:
                    log.error("job_failed", job=name, error=str(exc))
            log.info("scheduler_once_done", jobs=len(jobs))
            return 0

        scheduler = build_scheduler(runtime)
        scheduler.start()
        log.info(
            "scheduler_started",
            jobs=[job.id for job in scheduler.get_jobs()],
            timezone=str(settings.app.timezone),
        )
        asyncio.get_event_loop().run_forever()
        return 0
    finally:
        asyncio.run(runtime.aclose())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
