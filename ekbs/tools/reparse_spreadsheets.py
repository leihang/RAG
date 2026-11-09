"""重新解析并入库 Excel 文档（修复表格序列化/切片后使用）。

只处理 .xls/.xlsx；保留 doc_id、权限、密级、KB，替换分块与向量。
默认 dry-run；--apply 真执行。执行前应停 worker/api（单写者）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ekbs.config import load_settings
from ekbs.ingest.chunker import TableInput
from ekbs.ingest.figure_input import figure_inputs_from_parsed
from ekbs.ingest.parser import parse_file
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.logging import configure_logging, get_logger
from ekbs.runtime import Runtime

log = get_logger(component="tools.reparse_spreadsheets")


async def _run(apply: bool, doc_ids: list[int] | None) -> int:
    settings = load_settings()
    configure_logging(settings, "tools")
    runtime = Runtime.create(settings, service="tools", init_schema=True, strict_shards=False)
    try:
        with runtime.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, kb_id, title, source_path, level, visible_dept_ids, "
                "doc_version, checksum FROM ekbs_document "
                "WHERE status IN ('published','offline','failed') "
                "AND (lower(source_path) LIKE '%.xls' OR lower(source_path) LIKE '%.xlsx') "
                "ORDER BY doc_id"
            ).fetchall()
        targets = [dict(r) for r in rows]
        if doc_ids:
            targets = [r for r in targets if int(r["doc_id"]) in set(doc_ids)]
        print(f"待重解析 {len(targets)} 篇：{[r['doc_id'] for r in targets]}")
        if not apply:
            for r in targets:
                print(r["doc_id"], r["title"][:70], r["source_path"])
            return 0

        ingest = IngestPipeline(
            db=runtime.db, settings=settings, providers=runtime.providers,
            shards=runtime.shards, fts=runtime.fts, figure_index=runtime.figure_index,
        )
        failed: list[tuple[int, str]] = []
        for r in targets:
            doc_id = int(r["doc_id"])
            source = Path(str(r["source_path"]))
            try:
                assets_dir = settings.data_paths()["parse"] / str(doc_id)
                parsed = await asyncio.to_thread(parse_file, source, assets_dir=assets_dir)
                figures = figure_inputs_from_parsed(
                    parsed.figures,
                    data_dir=settings.data_dir,
                    assets_dir=assets_dir,
                )
                tables = [
                    TableInput(
                        body=t.body, caption=t.caption, page=t.page, table_id=t.table_id,
                        preserve_layout=t.preserve_layout,
                    )
                    for t in parsed.tables
                ]
                visible = json.loads(r["visible_dept_ids"] or "[]")
                result = await ingest.ingest_markdown(
                    title=str(r["title"]), markdown=parsed.markdown,
                    kb_id=int(r["kb_id"]), level=int(r["level"] or 2),
                    visible_dept_ids=list(visible), source_path=str(source),
                    doc_version=str(r["doc_version"] or "v1"),
                    page_map=list(parsed.page_map), figures=figures, tables=tables,
                    existing_doc_id=doc_id,
                    checksum=(str(r["checksum"]) if r["checksum"] else None),
                    real_corpus=True,
                )
                print(f"OK doc={doc_id} tables={len(tables)} vectors={result.vector_count} "
                      f"parents={result.parent_count} children={result.child_count}")
            except Exception as exc:  # noqa: BLE001
                log.error("reparse_failed", doc_id=doc_id, error=str(exc)[:300])
                failed.append((doc_id, str(exc)[:200]))
        if failed:
            print("失败：", failed)
            return 1
        return 0
    finally:
        await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--doc-id", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.apply, args.doc_id or None))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
