"""``python -m ekbs.tools.ingest_quality_report`` —— 解析/清洗 + 分片质量报告。

对齐 ``06`` §1.7 / ``01`` §12 准出线（空页率、乱码率、表格存活率；页码连续性 WARN）。
分片内在统计（长度、同文双索引、heading/figure）一并输出，便于投入测试盯回归。

用法：

.. code-block:: bash

    export EKBS_CONFIG=config/application.dev.yaml
    .venv/bin/python -m ekbs.tools.ingest_quality_report
    .venv/bin/python -m ekbs.tools.ingest_quality_report --doc-id 3 --json
    .venv/bin/python -m ekbs.tools.ingest_quality_report --status published --fail-on-gate
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.ingest.quality import Thresholds, assess_batch
from ekbs.logging import configure_logging, get_logger

log = get_logger(component="tools.ingest_quality_report")


def _load_docs(
    db: Database,
    *,
    doc_id: int | None,
    status: str | None,
    limit: int | None,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    clauses: list[str] = []
    params: list[object] = []
    if doc_id is not None:
        clauses.append("doc_id = ?")
        params.append(doc_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT doc_id, title, status FROM ekbs_document"
        + where
        + " ORDER BY doc_id"
    )
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"

    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    with db.sync_connect() as conn:
        docs = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for doc in docs:
            rows = conn.execute(
                "SELECT chunk_id, parent_id, chunk_type, content, token_count, "
                "heading_path, page_from, page_to, figure_id, image_path, faiss_row "
                "FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq, chunk_id",
                (int(doc["doc_id"]),),
            ).fetchall()
            out.append((doc, [dict(r) for r in rows]))
    return out


def _print_human(report) -> None:
    print(
        f"文档 {len(report.docs)} 篇；闸门失败 {len(report.failed_doc_ids)} 篇；"
        f"整体 {'PASS' if report.ok else 'FAIL'}"
    )
    print(
        "阈值："
        f"空页≤{report.thresholds['empty_page_rate_max']:.0%} / "
        f"乱码≤{report.thresholds['garbled_rate_max']:.1%} / "
        f"表格存活≥{report.thresholds['table_survival_min']:.0%}"
    )
    print("-" * 72)
    for item in report.docs:
        p, c = item.parse, item.chunks
        gate = "PASS" if p.ok else "FAIL"
        print(f"[{gate}] doc_id={p.doc_id}  {p.title or '(无标题)'}  status={p.status}")
        empty = (
            f"{p.empty_page_rate:.2%}（{p.empty_pages}/{p.page_count}）"
            if p.pages_applicable
            else "N/A"
        )
        table = (
            f"{p.table_survival_rate:.2%}（{p.table_nonempty}/{p.table_count}）"
            if p.table_survival_rate is not None
            else "N/A"
        )
        cont = (
            "ok"
            if not p.pages_applicable
            else ("ok" if p.page_continuity_ok else f"缺页 {p.missing_pages[:8]}")
        )
        print(
            f"  L1 乱码={p.garbled_rate:.3%}  空页={empty}  表格存活={table}  页码连续={cont}"
        )
        print(
            f"  L2 叶子={c.leaf_chunks}/{c.total_chunks}  "
            f"双索引={c.dual_index_pairs}（{c.dual_index_rate:.1%}）  "
            f"空heading={c.empty_heading_leaves}  "
            f"figure短文本={c.figure_short_text}/{c.figure_count}  "
            f"token p50/p90={c.token_p50}/{c.token_p90}"
        )
        for w in p.warnings:
            print(f"  ⚠ {w}")
    if report.failed_doc_ids:
        print("-" * 72)
        print(f"未过闸门的 doc_id：{report.failed_doc_ids}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析/清洗/分片质量报告（06 §1.7）")
    parser.add_argument("--doc-id", type=int, help="只评估指定文档")
    parser.add_argument(
        "--status",
        default="published",
        help="按文档状态过滤（默认 published；空字符串=不过滤）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多评估 N 篇（0=全部）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="任一文档硬闸未过则退出码 1（页码连续性仅 WARN，不计入）",
    )
    parser.add_argument(
        "--empty-page-max",
        type=float,
        default=None,
        help="覆盖空页率上限（默认 0.01）",
    )
    parser.add_argument(
        "--garbled-max",
        type=float,
        default=None,
        help="覆盖乱码率上限（默认 0.005）",
    )
    parser.add_argument(
        "--table-survival-min",
        type=float,
        default=None,
        help="覆盖表格存活率下限（默认 0.95）",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    db = Database(settings)
    # 只读报告：Schema 已存在即可；不自动建库，避免误在空目录初始化
    if not settings.db_path.exists():
        print(f"数据库不存在：{settings.db_path}", file=sys.stderr)
        return 2

    from dataclasses import replace

    thr = Thresholds()
    if args.empty_page_max is not None:
        thr = replace(thr, empty_page_rate_max=args.empty_page_max)
    if args.garbled_max is not None:
        thr = replace(thr, garbled_rate_max=args.garbled_max)
    if args.table_survival_min is not None:
        thr = replace(thr, table_survival_min=args.table_survival_min)

    status = args.status if args.status != "" else None
    pairs = _load_docs(
        db,
        doc_id=args.doc_id,
        status=status,
        limit=args.limit or None,
    )
    if not pairs:
        print("没有匹配的文档。", file=sys.stderr)
        return 1

    report = assess_batch(pairs, thresholds=thr)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    log.info(
        "ingest_quality_done",
        docs=len(report.docs),
        failed=len(report.failed_doc_ids),
        ok=report.ok,
    )
    if args.fail_on_gate and not report.ok:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
