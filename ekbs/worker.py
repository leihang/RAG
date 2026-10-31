"""``python -m ekbs.worker`` —— 解析 worker（launchd ``com.ekbs.worker``，`01` §8.2）。

行为（`00` D13）：**常驻轮询** ``ekbs_document.status='pending'`` 的文档，
``parse.worker_concurrency=1`` 串行解析（16 GB 机器不能让 MinerU 的内存峰值叠加）。

部署契约：**同一数据库同一时刻只允许一个 worker 进程**（含 ``--once``）。
常驻已在跑时禁止再起 ``python -m ekbs.worker --once``——会与 MinerU 内存峰值叠加，
并触发 ``requeue_orphans`` 把对方正在解析的文档打回 ``pending``。进程级
``data_dir/worker.lock``（fcntl）抢不到则 exit(1)。

⚠️ MinerU 未安装时，PDF/图片以 :class:`~ekbs.ingest.parser.ParserUnavailable`
明确失败（不会把空内容当成功）。``.md/.txt/.docx/.xlsx`` 等走各自解析分支。

⚠️ Schema 没有 ``ekbs_document.error`` 列（`docs/spec/20` T13），失败原因只进日志。

解析清洗后一律停在 ``review``，**不分块、不发布**。管理台确认通过后才入库。
私有库文档（可见范围 ``user:<id>``）例外：解析后直接入库，只有本人能检索。
MinerU（PDF/图片）和路径里带「扫描」的，另外跳过视觉配图。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from ekbs.config import load_settings
from ekbs.errors import ConfigError
from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.figure_caption import caption_images
from ekbs.ingest.parser import ParserUnavailable, parse_file
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.ingest.scan_review import (
    figures_from_sidecar,
    load_review_markdown,
    load_review_sidecar,
    mark_document_review,
    page_map_from_sidecar,
    prepare_scan_draft,
    requires_human_review,
    save_review_draft,
    tables_from_sidecar,
)
from ekbs.personal import is_personal_scope
from ekbs.logging import get_logger
from ekbs.runtime import Runtime

log = get_logger(component="worker")

IDLE_SLEEP_SECONDS = 5.0

#: 进程级单例锁的打开文件句柄；必须挂在模块上，避免 GC 关闭导致 flock 释放。
_WORKER_LOCK_FH: TextIO | None = None

#: ``ps`` 命令行里识别 worker 入口；勿匹配 pytest / import 路径噪声。
_WORKER_CMD_RE = re.compile(r"(?:^|[\s/])-m\s+ekbs\.worker(?:\s|$)")


def _lock_holder_pid(lock_path: Path) -> int | None:
    """读 ``worker.lock`` 里记录的持有者 pid（仅提示用；以 flock / 进程扫描为准）。"""
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


class WorkerProcessScanError(RuntimeError):
    """无法扫描本机 worker 进程（``ps`` 失败/被拒）。启停必须失败，不能当成「没有 peer」。"""


def _peer_worker_pids(*, exclude_pid: int | None = None) -> list[int]:
    """扫描本机其它 ``python -m ekbs.worker`` 进程。

    flock 在「锁文件被 unlink 后另开新 inode」时会失效，进程扫描是第二道闸。
    **禁止**删除 ``worker.lock``：删了就会落到这个坑。

    ``ps`` 失败时抛 :class:`WorkerProcessScanError`（失败闭合），禁止把「扫不了」
    当成「没有其它 worker」。
    """
    exclude = os.getpid() if exclude_pid is None else exclude_pid
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorkerProcessScanError(f"ps 扫描 worker 失败: {exc}") from exc
    found: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == exclude:
            continue
        if _WORKER_CMD_RE.search(parts[1]):
            found.append(pid)
    return found


def _acquire_worker_lock(lock_path: Path) -> TextIO | None:
    """非阻塞抢 ``worker.lock``；成功返回需保持打开的文件句柄，失败返回 None。

    只 open/create，**永不 unlink**。unlink 会让第二进程 flock 到新 inode，
    与仍持有旧 inode 的进程并存——那是双 worker 的经典坑。
    inode 分裂靠 :func:`_reject_if_peer_workers` 兜底（在 ``main`` 里、抢锁成功后调用）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()}\n")
    fh.flush()
    return fh


def _reject_if_peer_workers(*, lock_path: Path, settle_seconds: float = 0.8) -> list[int]:
    """抢到 flock 后确认没有其它 ``ekbs.worker``。

    同时启动时输掉 flock 的进程还会短暂出现在 ``ps`` 里，先等待再判定；
    若仍有 peer，视为 inode 分裂或孤儿进程，调用方应释放锁并退出。

    返回非空 list = 应拒绝启动；扫描失败时抛 :class:`WorkerProcessScanError`。
    """
    peers = _peer_worker_pids()
    if not peers:
        return []
    time.sleep(settle_seconds)
    peers = _peer_worker_pids()
    if peers:
        log.error(
            "worker_peer_alive_despite_lock",
            peers=peers,
            path=str(lock_path),
            hint="疑似 worker.lock 曾被删除导致 flock inode 分裂，或 stop 未杀净；"
            "请 bash scripts/dev_services.sh stop worker，且不要删除 worker.lock",
        )
    return peers


def _claim_next_pending(runtime: Runtime) -> dict[str, Any] | None:
    """取一份待解析文档并**立即置为 parsing**。

    ``sync_connect`` 是 autocommit（``isolation_level=None``），必须手写
    ``BEGIN IMMEDIATE``，否则 SELECT/UPDATE 之间存在 TOCTOU，两个 worker 会同时
    拉起 MinerU 解析同一文件（24 GB 机器实测双峰叠加耗尽内存）。
    """
    with runtime.db.sync_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT doc_id, kb_id, title, source_path, level, visible_dept_ids, "
                "doc_version, checksum, status, parse_path "
                "FROM ekbs_document WHERE status IN ('pending', 'pending_index') "
                "ORDER BY doc_id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            claimed_status = "parsing" if str(row["status"]) == "pending" else "indexing"
            cur = conn.execute(
                "UPDATE ekbs_document SET status = ?, updated_at = datetime('now') "
                "WHERE doc_id = ? AND status = ?",
                (claimed_status, int(row["doc_id"]), str(row["status"])),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
            claimed = dict(row)
            claimed["_claimed_status"] = claimed_status
            return claimed
        except Exception:
            conn.execute("ROLLBACK")
            raise


async def _index_review_document(runtime: Runtime, ingest: IngestPipeline, doc: dict[str, Any]) -> None:
    """``pending_index`` 文档：对已分块（chunked）文档做向量入库。

    复核确认只落分块；本函数只读现有块做 embedding/分片/FTS/发布，
    **不重新分块**，避免覆盖用户在分块预览里的编辑。
    """
    doc_id = int(doc["doc_id"])
    await ingest.index_existing_document(doc_id)


async def process_once(runtime: Runtime, ingest: IngestPipeline) -> bool:
    """处理一份文档；返回是否真的处理了（False = 无待办）。"""
    doc = await asyncio.to_thread(_claim_next_pending, runtime)
    if doc is None:
        return False

    doc_id = int(doc["doc_id"])
    claimed_status = str(doc.pop("_claimed_status", "parsing"))
    started = time.perf_counter()
    if claimed_status == "indexing":
        try:
            await _index_review_document(runtime, ingest, doc)
            log.info("doc_indexed_from_queue", doc_id=doc_id)
        except Exception as exc:  # noqa: BLE001
            log.error("doc_index_failed", doc_id=doc_id, error=str(exc)[:300])
            await asyncio.to_thread(_mark_failed, runtime, doc_id)
        return True
    try:
        # 图片落盘目录：**不能**用临时目录（临时目录一删，文档内插图全成死链）。
        # 约定 <data_dir>/parse/<doc_id>/images/<文件>，与 /assets/figure 的解析基准一致。
        assets_dir = runtime.settings.data_paths()["parse"] / str(doc_id)
        parsed = await asyncio.to_thread(parse_file, doc["source_path"], assets_dir=assets_dir)

        # 路径含「扫描」，或引擎是 MinerU（PDF/图片 OCR）。后者不看文件名：
        # 同一份扫描件重新上传到 internal_public_document/ 时路径里没有「扫描」。
        scan_doc = requires_human_review(
            str(doc.get("source_path") or ""),
            str(doc.get("title") or ""),
            engine=parsed.engine,
        )

        # 扫描件 / MinerU 先停 review：跳过视觉配图（费钱且签署页多为印章噪声），等人审后再分块。
        captions: dict[str, str] = {}
        if (
            not scan_doc
            and parsed.figures
            and runtime.settings.parse.figure_caption
        ):
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
                        concurrency=runtime.settings.parse.figure_caption_concurrency,
                    )

        figures = [
            FigureInput(
                image_path=str((assets_dir / item.name).relative_to(runtime.settings.data_dir)),
                # 视觉模型描述优先；Alt Text 实测多是素材名
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
                preserve_layout=item.preserve_layout,
            )
            for item in parsed.tables
        ]

        if scan_doc:
            parse_path, clean_stats = await asyncio.to_thread(
                prepare_scan_draft,
                data_dir=runtime.settings.data_dir,
                doc_id=doc_id,
                raw_markdown=parsed.markdown,
                engine=parsed.engine,
                page_map=list(parsed.page_map),
                figures=figures,
                tables=tables,
                source_path=str(doc.get("source_path") or ""),
            )
        else:
            # Word / Excel / PPT / Markdown 同样先复核。不走扫描件清洗，避免误改正文。
            # 图片必须写进草稿：确认通过时分块器只认 sidecar，不认解析器临时结果。
            parse_path = await asyncio.to_thread(
                save_review_draft,
                data_dir=runtime.settings.data_dir,
                doc_id=doc_id,
                markdown=parsed.markdown,
                engine=parsed.engine,
                page_map=list(parsed.page_map),
                figures=figures,
                tables=tables,
            )
            clean_stats = {}
        if is_personal_scope(doc.get("visible_dept_ids")):
            markdown = load_review_markdown(runtime.settings.data_dir, doc_id)
            sidecar = load_review_sidecar(runtime.settings.data_dir, doc_id)
            visible = json.loads(doc.get("visible_dept_ids") or "[]")
            await ingest.ingest_markdown(
                title=str(doc.get("title") or ""),
                markdown=markdown,
                kb_id=int(doc["kb_id"]),
                level=int(doc.get("level") or 2),
                visible_dept_ids=list(visible),
                source_path=str(doc.get("source_path") or ""),
                doc_version=str(doc.get("doc_version") or "v1"),
                page_map=page_map_from_sidecar(sidecar),
                figures=figures_from_sidecar(sidecar),
                tables=tables_from_sidecar(sidecar),
                existing_doc_id=doc_id,
                checksum=(str(doc["checksum"]) if doc.get("checksum") else None),
                real_corpus=True,
                personal=True,
            )
            log.info("personal_doc_published", doc_id=doc_id)
            return True
        await asyncio.to_thread(mark_document_review, runtime.db, doc_id, parse_path)
        await asyncio.to_thread(
            _record_parse_ms, runtime, doc_id, int((time.perf_counter() - started) * 1000)
        )
        log.info(
            "doc_awaiting_review",
            doc_id=doc_id,
            engine=parsed.engine,
            chars=len(parsed.markdown),
            figures=len(figures),
            tables=len(tables),
            parse_path=parse_path,
            **(clean_stats or {}),
        )
        return True
    except ParserUnavailable as exc:
        log.error("parser_unavailable", doc_id=doc_id, error=str(exc))
        await asyncio.to_thread(_mark_failed, runtime, doc_id)
    except Exception as exc:
        log.error("doc_parse_failed", doc_id=doc_id, error=str(exc))
        await asyncio.to_thread(_mark_failed, runtime, doc_id)
    return True


def _record_parse_ms(runtime: Runtime, doc_id: int, parse_ms: int) -> None:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET parse_ms = ? WHERE doc_id = ?",
            (int(parse_ms), doc_id),
        )
        conn.commit()


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
        figure_index=runtime.figure_index,
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
    last_compact = 0.0
    while not stop.is_set():
        try:
            worked = await process_once(runtime, ingest)
        except Exception as exc:  # 循环不能因单份文档的异常退出（launchd 会重拉，但会丢上下文）
            log.error("worker_loop_error", error=str(exc))
            worked = False
        if not worked:
            # 分段索引：空闲时做 compaction（10 分钟一次，避免空闲轮询反复加载分片）
            if getattr(runtime.shards, "layout", "single") == "segmented" and time.monotonic() - last_compact > 600:
                try:
                    results = await asyncio.to_thread(runtime.shards.compact_all)
                    if results:
                        log.info("segmented_compacted", results=results)
                except Exception as exc:  # noqa: BLE001 - compaction 失败不影响入库
                    log.warning("segmented_compact_failed", error=str(exc)[:200])
                last_compact = time.monotonic()
            try:
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)
            except asyncio.TimeoutError:
                pass


def _release_worker_lock(lock_fh: TextIO) -> None:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lock_fh.close()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    global _WORKER_LOCK_FH
    parser = argparse.ArgumentParser(description="EKBS 解析 worker（MinerU / Office / 文本）")
    parser.add_argument("--once", action="store_true", help="只处理一轮后退出（联调用）")
    args = parser.parse_args(argv)

    settings = load_settings()

    # 抢锁必须在 Runtime.create / requeue_orphans 之前：
    # 1) --once 不能把常驻正在 parsing 的文档打回 pending；
    # 2) 第二进程不能先加载 FAISS/模型再撞锁（内存峰值叠加）。
    lock_path = settings.data_dir / "worker.lock"
    lock_fh = _acquire_worker_lock(lock_path)
    if lock_fh is None:
        try:
            peers_hint: list[int] | str = _peer_worker_pids()
        except WorkerProcessScanError as exc:
            peers_hint = f"scan_failed:{exc}"
        log.error(
            "worker_lock_busy",
            path=str(lock_path),
            lock_holder=_lock_holder_pid(lock_path),
            peers=peers_hint,
            hint="已有 worker 在跑（常驻或 --once）。先停常驻再联调，"
            "或只登记 pending 等常驻轮询；禁止与常驻并存再起第二个进程；"
            "禁止删除 worker.lock（删了会 flock inode 分裂）",
        )
        return 1
    try:
        peers = _reject_if_peer_workers(lock_path=lock_path)
    except WorkerProcessScanError as exc:
        log.error("worker_peer_scan_failed", error=str(exc), path=str(lock_path))
        _release_worker_lock(lock_fh)
        return 1
    if peers:
        _release_worker_lock(lock_fh)
        return 1
    _WORKER_LOCK_FH = lock_fh

    runtime = Runtime.create(settings, service="worker")
    ingest = IngestPipeline(
        db=runtime.db, settings=settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
        figure_index=runtime.figure_index,
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
