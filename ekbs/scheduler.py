"""``python -m ekbs.scheduler`` —— APScheduler 单实例定时作业（`00` D13、`04` §7.1.4）。

为什么必须是**独立进程**：定时作业若挂在 uvicorn 上，多 worker 会重复执行；
本栈固定 ``workers=1`` 也只是「当前不炸」，不是设计。因此所有定时任务集中在这里，
单进程单写者，与 `01` §5.6 的归档路径「事务内 DROP 触发器 → 删除 → 重建」相容。

作业清单（`04` §7.1.4；组织全量只在管理台打开定时更新后按间隔跑）：

| 作业 | 频率 | 现状 |
|---|---|---|
| ``cache_cleanup`` | 10 min | ✅ 已实现（`ekbs_cache` 过期行） |
| ``metrics_collect`` | 60 s | ✅ 已实现（写 ``ekbs_metric``） |
| ``policy_version_mirror`` | 60 s | ✅ 已实现（只读镜像，权威在 SQLite） |
| ``session_ctx_cleanup`` | 30 min | ✅ 已实现（`ekbs_session_ctx` 过期） |
| ``rate_limit_cleanup`` | 10 min | ✅ 已实现（`ekbs_rate_limit` 滑窗回收） |
| ``msg_dedup_cleanup`` | 1 h | ✅ 已实现（`ekbs_msg_dedup` 7 天） |
| ``reconcile`` | 每日 01:30 | ◐ 部分实现（SQLite↔FTS5 计数对账；FAISS 侧复用自检） |
| ``audit_archive`` | 每日 03:00 | ✅ 已实现 |
| 组织同步 | 管理台开启后按小时间隔 | 与「从钉钉更新」相同：部门 + 人员。未开启时不跑 |
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ekbs.config import load_settings
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.logging import get_logger
from ekbs.observability.alerts import evaluate_all, record_alerts
from ekbs.observability.metrics import snapshot
from ekbs.observability.notify import AlertNotifier
from ekbs.runtime import Runtime

log = get_logger(component="scheduler")

SESSION_CONTEXT_TTL_DEFAULT = 7200
MSG_DEDUP_RETAIN_DAYS = 7
#: D16 inbox 终态行留存：failed 终态（死信对应行）保留 30 天供人工处置
MSG_INBOX_DEAD_RETAIN_DAYS = 30
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
    change = record_alerts(runtime.db, hits)
    if change.opened:
        # 只在**新开**告警时推送：持续命中不重复打扰，恢复由 change.closed 说明
        try:
            AlertNotifier(runtime.db, runtime.secrets).notify(change.opened)
        except Exception as exc:  # noqa: BLE001 - 推送失败绝不影响采集作业
            log.error("alert_notify_failed", error=str(exc)[:200])
    log.debug(
        "metrics_collected",
        host=values,
        in_process=len(in_process),
        alerts=len(hits),
        opened=len(change.opened),
        closed=len(change.closed),
    )


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
        # D16：inbox 终态行按留存期清理 —— done 7 天（对齐 dedup）；
        # failed 终态（重试耗尽）留 30 天供人工处置，死信表本身保留（量小，人工清理）。
        # 注意不能清「未终态」行：那会破坏 at-least-once 的重驱动。
        inbox_done = conn.execute(
            "DELETE FROM ekbs_msg_inbox WHERE status = 'done'"
            " AND updated_at < datetime('now', ?)",
            (f"-{MSG_DEDUP_RETAIN_DAYS} days",),
        )
        inbox_dead = conn.execute(
            "DELETE FROM ekbs_msg_inbox WHERE status = 'failed' AND next_retry_at IS NULL"
            " AND updated_at < datetime('now', ?)",
            (f"-{MSG_INBOX_DEAD_RETAIN_DAYS} days",),
        )
        conn.commit()
        if cursor.rowcount:
            log.info("msg_dedup_cleaned", removed=cursor.rowcount)
        if inbox_done.rowcount or inbox_dead.rowcount:
            log.info(
                "msg_inbox_cleaned", done_removed=inbox_done.rowcount,
                dead_removed=inbox_dead.rowcount,
            )


def job_alert_channel_heartbeat(runtime: Runtime) -> None:
    """每日 09:00 告警通道心跳（08 §5.2）：未送达即说明告警黑洞，必须人工介入。"""
    results = AlertNotifier(runtime.db, runtime.secrets).heartbeat()
    if results and not all(results.values()):
        log.error("alert_heartbeat_partial", results=results)


def job_quality_sentinel(runtime: Runtime) -> None:
    """每日质量哨兵（00 v4.0 D21）：抽样重估支撑率/拒答率，劣化写指标供告警。"""
    from ekbs.observability.sentinel import run_sentinel

    report = run_sentinel(runtime.db)
    if report.degraded:
        log.warning("quality_sentinel_degraded", **report.as_dict())


def job_reconcile(runtime: Runtime) -> None:
    """SQLite ↔ FTS5 ↔ FAISS 三方对账（`03` §5.4：差异率目标 <0.01%）。"""
    with runtime.db.sync_connect() as conn:
        chunk_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
                f"WHERE {RETRIEVABLE_PREDICATE} AND d.status = 'published'"
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


def job_trash_purge(runtime: Runtime) -> None:
    """回收站物理清理：软删除超过 30 天的文档连同磁盘资产一并清除。"""
    from ekbs.api.docs import purge_expired_trash

    report = purge_expired_trash(runtime)
    if report["purged"] or report["errors"]:
        log.info("trash_purged", **report)


async def job_org_sync(runtime: Runtime) -> None:
    """到点才拉。未开启、未到间隔、或上一轮还在跑，直接返回。"""
    from ekbs.dingtalk.sync import build_sync_service
    from ekbs.errors import ConfigError
    from ekbs.permission.org_schedule import claim_org_sync, finish_org_sync

    if not claim_org_sync(runtime.db):
        return
    try:
        service = build_sync_service(
            runtime.db, runtime.settings, runtime.secrets, cache=runtime.cache
        )
        report = await service.sync_full(mode="schedule")
        log.info(
            "org_sync_scheduled",
            departments=report.departments,
            users=report.users,
            created=report.created,
            updated=report.updated,
        )
    except ConfigError as exc:
        log.warning("org_sync_scheduled_skipped", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 定时失败记日志，下一轮再等一个间隔
        log.error("org_sync_scheduled_failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        finish_org_sync(runtime.db)


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
    scheduler.add_job(job_audit_archive, CronTrigger(hour=3, minute=0), args=[runtime],
                      id="audit_archive", replace_existing=True)
    # 质量哨兵：每日 03:10（08 §5.1；审计归档之后、流量低谷）
    scheduler.add_job(job_quality_sentinel, CronTrigger(hour=3, minute=10), args=[runtime],
                      id="quality_sentinel", replace_existing=True)
    # 告警通道心跳：每日 09:00（08 §5.2；上班即验证带外通道可达）
    scheduler.add_job(job_alert_channel_heartbeat, CronTrigger(hour=9, minute=0), args=[runtime],
                      id="alert_channel_heartbeat", replace_existing=True)
    scheduler.add_job(job_trash_purge, CronTrigger(hour=3, minute=30), args=[runtime],
                      id="trash_purge", replace_existing=True)
    scheduler.add_job(job_org_sync, IntervalTrigger(minutes=5), args=[runtime],
                      id="org_sync", replace_existing=True)
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
        ("audit_archive", job_audit_archive),
        ("quality_sentinel", job_quality_sentinel),
        ("alert_channel_heartbeat", job_alert_channel_heartbeat),
        ("trash_purge", job_trash_purge),
        ("org_sync", job_org_sync),
    ]
    try:
        if args.once:
            for name, job in jobs:
                try:
                    if name == "cache_cleanup":
                        asyncio.run(runtime.cache.cleanup_expired())
                    elif name == "org_sync":
                        asyncio.run(job_org_sync(runtime))
                    else:
                        job(runtime)
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
