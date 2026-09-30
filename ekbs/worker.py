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
from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.parser import ParserUnavailable, parse_file
from ekbs.ingest.figure_caption import caption_images
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.logging import get_logger
from ekbs.runtime import Runtime

log = get_logger(component="worker")

IDLE_SLEEP_SECONDS = 5.0


def _claim_next_pending(runtime: Runtime) -> dict[str, Any] | None:
    """取一份待解析文档并**立即置为 parsing**（单进程串行，无并发竞争）。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id, kb_id, title, source_path, level, visible_dept_ids, doc_version, "
            "checksum "
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
        # 图片落盘目录：**不能**用临时目录（临时目录一删，文档内插图全成死链）。
        # 约定 <data_dir>/parse/<doc_id>/images/<文件>，与 /assets/figure 的解析基准一致。
        assets_dir = runtime.settings.data_paths()["parse"] / str(doc_id)
        parsed = await asyncio.to_thread(parse_file, doc["source_path"], assets_dir=assets_dir)
        # 图片描述（`06` §4.11）：视觉模型能给"图意"写检索文本，这是 OCR 做不到的
        # （MinerU 对独立图片只会回吐图片自己的引用，见 README 第 66 条）。
        # 默认关闭；打开后过合规闸（会把图片发到云端），并且描述按内容哈希缓存，不重复计费。
        captions: dict[str, str] = {}
        if parsed.figures and runtime.settings.parse.figure_caption:
            if runtime.providers.vision is None:
                log.warning(
                    "figure_caption_skipped", doc_id=doc_id,
                    reason="parse.figure_caption=true 但未配置 models.vision",
                )
            else:
                try:
                    runtime.settings.assert_compliance_gate(real_corpus=True)
                except ConfigError as exc:
                    log.warning("figure_caption_blocked", doc_id=doc_id, error=str(exc)[:120])
                else:
                    captions = await caption_images(
                        data_dir=runtime.settings.data_dir,
                        vision=runtime.providers.vision,
                        images=[
                            (item.name, assets_dir / item.name) for item in parsed.figures
                        ],
                    )

        figures = [
            FigureInput(
                image_path=str((assets_dir / item.name).relative_to(runtime.settings.data_dir)),
                # 视觉模型的描述优先：它描述的是图的内容；Alt Text 实测多是素材名
                # （`5506745资源 1@2x`），页标题兜底只是"没有别的办法"时的退路。
                caption=captions.get(item.name) or item.caption,
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
            )
            for item in parsed.tables
        ]
        await ingest.ingest_markdown(
            title=str(doc["title"]),
            markdown=parsed.markdown,
            kb_id=int(doc["kb_id"]),
            level=int(doc["level"]),
            visible_dept_ids=json.loads(doc["visible_dept_ids"] or "[]"),
            source_path=str(doc["source_path"]),
            doc_version=str(doc["doc_version"]),
            page_map=list(parsed.page_map),
            # ⚠️ **必须把 figures 传下去**：上面刚拼好的图片块（含 image_path / 题注 / OCR 文字）
            # 如果不传，分块器就只在 markdown 里看到 `![](…)` 链接（而且 pptx 分支刻意不写链接），
            # 结果是图片**全部静默丢掉**：没有 figure 块、`image_path` 全空、
            # `/assets/figure/{chunk_id}` 无块可指 —— 92 张图躺在磁盘上却谁也看不见。
            figures=figures,
            tables=tables,
            existing_doc_id=doc_id,
            # **必须把文件内容哈希传下去**：不传的话 pipeline 会改用「markdown 内容哈希」
            # 回写 ekbs_document.checksum（pipeline.ingest_markdown 的 `checksum or …`），
            # 把上传登记时存的文件 SHA-256 覆盖掉 —— 于是秒传/去重口径在解析完那一刻就变了，
            # 同一份文件再传一次会「查不到重复」而重复入库。
            checksum=(str(doc["checksum"]) if doc["checksum"] else None),
        )
        log.info(
            "doc_parsed",
            doc_id=doc_id,
            engine=parsed.engine,
            chars=len(parsed.markdown),
            figures=len(figures),
            tables=len(tables),
        )
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


def requeue_orphans(runtime: Runtime) -> int:
    """把上一个进程留下的 `parsing` / `indexing` 文档放回 `pending`，返回条数。

    为什么必须有：worker 被 kill / 崩溃 / 部署重启时，正在解析的那份文档会**永远卡在
    `parsing`** —— 取消接口只接受 `pending`（原话："Schema 无 cancelled 状态，解析中的
    任务需等 worker 结束"），重排接口又拒绝 `parsing`，于是既不能取消也不能重试。
    用户实测撞上过：日志里没有 `doc_parsed`、界面一直显示"解析中"，没人能救它。

    为什么放在**启动时一次**而不是定时扫描：本进程刚起来，不可能有自己在跑的解析，
    所以此刻处于 `parsing`/`indexing` 的行**一定是上一个进程留下的**——判定精确，
    不需要"超过 N 分钟算超时"这种会跟正常长解析打架的启发式。代价是它依赖
    「同一数据库只有一个 worker 进程」这个部署契约（`parse.worker_concurrency`
    与进程内权限缓存都要求如此，见 04 §1.5 / §7.2）。
    """
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, status FROM ekbs_document WHERE status IN ('parsing', 'indexing')"
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE ekbs_document SET status = 'pending', updated_at = datetime('now') "
            "WHERE status IN ('parsing', 'indexing')"
        )
        conn.commit()
    log.warning(
        "parse_orphans_requeued",
        count=len(rows),
        doc_ids=[int(r["doc_id"]) for r in rows],
        hint="上一个 worker 进程在解析中途退出，这些文档此前既不能取消也不能重排",
    )
    return len(rows)


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

    requeue_orphans(runtime)
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
            requeue_orphans(runtime)
            worked = asyncio.run(process_once(runtime, ingest))
            log.info("worker_once_done", worked=worked)
            return 0
        asyncio.run(run_forever(runtime))
        return 0
    finally:
        asyncio.run(runtime.aclose())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
