"""真实语料表格问答冒烟（本地/评测环境用）。

在已有 DB 上对指定文档跑确定性表格查询，输出 PASS/FAIL。
不进入 pytest（依赖真实语料）；CI 用 tests/test_table_eval.py 的合成表。
"""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

from ekbs.config import load_settings
from ekbs.logging import configure_logging
from ekbs.retrieval.table_query import compute_table_comparison
from ekbs.runtime import Runtime

#: (doc_id, question, expected_totals)
CASES = [
    (
        215,
        "公司承担水费，2026年1-9月总水费较2025年1-9月总水费是增长还是减少",
        {2026: 277784.35, 2025: 211512.73},
    ),
]


async def _run() -> int:
    settings = load_settings()
    configure_logging(settings, "tools")
    runtime = Runtime.create(settings, service="tools", init_schema=True, strict_shards=False)
    failed = 0
    try:
        for doc_id, question, expected in CASES:
            with runtime.db.sync_connect() as conn:
                records = conn.execute(
                    "SELECT chunk_id, doc_id, chunk_type, content, heading_path "
                    "FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq",
                    (doc_id,),
                ).fetchall()
            chunks = [
                SimpleNamespace(
                    chunk_id=int(row["chunk_id"]),
                    doc_id=int(row["doc_id"]),
                    chunk_type=str(row["chunk_type"]),
                    content=str(row["content"] or ""),
                    heading_path=str(row["heading_path"] or ""),
                    doc_title="",
                )
                for row in records
            ]
            result = compute_table_comparison(question, chunks, db=runtime.db)
            ok = result is not None and all(
                abs(result.totals.get(year, 0.0) - value) <= 0.01
                for year, value in expected.items()
            )
            failed += 0 if ok else 1
            print(
                ("PASS" if ok else "FAIL"),
                f"doc={doc_id}",
                f"totals={result.totals if result else None}",
                f"expected={expected}",
            )
            if result is not None:
                print("  ", result.render())
        return 1 if failed else 0
    finally:
        await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
