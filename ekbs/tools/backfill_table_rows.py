"""为已入库文档重建 ``table_row_index``（表格行结构化索引）。

用法：
    python -m ekbs.tools.backfill_table_rows --apply
    python -m ekbs.tools.backfill_table_rows --apply --doc-id 215
"""

from __future__ import annotations

import argparse
import asyncio

from ekbs.config import load_settings
from ekbs.ingest.table_index import replace_document_table_rows
from ekbs.logging import configure_logging
from ekbs.runtime import Runtime


async def _run(apply: bool, doc_ids: list[int] | None) -> int:
    settings = load_settings()
    configure_logging(settings, "tools")
    runtime = Runtime.create(settings, service="tools", init_schema=True, strict_shards=False)
    try:
        with runtime.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE status IN ('published','review','chunked') "
                "ORDER BY doc_id"
            ).fetchall()
        targets = [int(row["doc_id"]) for row in rows]
        if doc_ids:
            targets = [doc_id for doc_id in targets if doc_id in set(doc_ids)]
        print(f"待回填 {len(targets)} 篇：{targets}")
        if not apply:
            return 0
        total = 0
        for doc_id in targets:
            total += replace_document_table_rows(runtime.db, doc_id)
        print(f"完成，写入表格行 {total} 条")
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
    raise SystemExit(main())
