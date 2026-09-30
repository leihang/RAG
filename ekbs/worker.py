"""``python -m ekbs.worker`` —— 解析 worker（launchd ``com.ekbs.worker``，`01` §8.2）。

行为（`00` D13）：**常驻轮询** ``ekbs_document.status='pending'`` 的文档，
``parse.worker_concurrency=1`` 串行解析（16 GB 机器不能让 MinerU 的内存峰值叠加）。

⚠️ 现状（骨架阶段）：MinerU 未安装时，PDF/图片解析会以
:class:`~ekbs.ingest.parser.ParserUnavailable` 明确失败并记录日志，
**不会**把「解析出空内容」当成功写入库。``.md/.txt/.docx/.xlsx`` 可以正常入库。

⚠️ Schema 没有 ``ekbs_document.error`` 列（`docs/spec/20` 的 T13 已记录该缺口），
因此失败原因只进日志与 ``ekbs_security_event``，库里只有 ``status='failed'``。
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

from ekbs.config import load_settings
from ekbs.ingest.parser import ParserUnavailable, parse_file
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.logging import get_logger
from ekbs.runtime import Runtime

log = get_logger(component="worker")

IDLE_SLEEP_SECONDS = 5.0


def _claim_next_pending(runtime: Runtime) -> dict[str, Any] | None:
    """取一份待解析文档并**立即置为 parsing**（单进程串行，无并发竞争）。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id, kb_id, title, source_path, level, visible_dept_ids, doc_version "
            "FROM ekbs_document WHERE status = 'pending' ORDER BY doc_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE ekbs_document SET status = 'parsing', updated_at = datetime('now') "
            "WHERE doc_id = ?",
            (int(row["doc_id"]),),
        )
        conn.commit()
    return dict(row)


async def process_once(runtime: Runtime, ingest: IngestPipeline) -> bool:
    """处理一份文档；返回是否真的处理了（False = 无待办）。"""
    doc = await asyncio.to_thread(_claim_next_pending, runtime)
    if doc is None:
        return False
    import json

    doc_id = int(doc["doc_id"])
    try:
        parsed = await asyncio.to_thread(parse_file, doc["source_path"])
        await ingest.ingest_markdown(
            title=str(doc["title"]),
            markdown=parsed.markdown,
            kb_id=int(doc["kb_id"]),
            level=int(doc["level"]),
            visible_dept_ids=json.loads(doc["visible_dept_ids"] or "[]"),
            source_path=str(doc["source_path"]),
            doc_version=str(doc["doc_version"]),
            page_map=list(parsed.page_map),
            existing_doc_id=doc_id,
        )
        log.info("doc_parsed", doc_id=doc_id, engine=parsed.engine, chars=len(parsed.markdown))
    except ParserUnavailable as exc:
        log.error("parser_unavailable", doc_id=doc_id, error=str(exc))
        await asyncio.to_thread(_mark_failed, runtime, doc_id)
    except Exception as exc:
        log.error("doc_parse_failed", doc_id=doc_id, error=str(exc))
        await asyncio.to_thread(_mark_failed, runtime, doc_id)
    return True


def _mark_failed(runtime: Runtime, doc_id: int) -> None:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'failed', updated_at = datetime('now') "
            "WHERE doc_id = ?",
            (doc_id,),
        )
        conn.commit()


async def run_forever(runtime: Runtime) -> None:
    ingest = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    stop = asyncio.Event()

    def _handle(signum: int, _frame: Any) -> None:
        log.info("worker_signal", signal=signum)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with_signal = True
        try:
            loop.add_signal_handler(sig, _handle, sig, None)
        except NotImplementedError:  # pragma: no cover
            with_signal = False
        if not with_signal:
            signal.signal(sig, _handle)

    log.info("worker_started", concurrency=runtime.settings.parse.worker_concurrency)
    while not stop.is_set():
        try:
            worked = await process_once(runtime, ingest)
        except Exception as exc:  # 循环不能因单份文档的异常退出（launchd 会重拉，但会丢上下文）
            log.error("worker_loop_error", error=str(exc))
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)
            except asyncio.TimeoutError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EKBS 解析 worker（MinerU / Office / 文本）")
    parser.add_argument("--once", action="store_true", help="只处理一轮后退出（联调用）")
    args = parser.parse_args(argv)

    settings = load_settings()
    runtime = Runtime.create(settings, service="worker")
    ingest = IngestPipeline(
        db=runtime.db, settings=settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    try:
        if args.once:
            worked = asyncio.run(process_once(runtime, ingest))
            log.info("worker_once_done", worked=worked)
            return 0
        asyncio.run(run_forever(runtime))
        return 0
    finally:
        asyncio.run(runtime.aclose())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
