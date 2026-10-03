"""重建 FTS 预分词索引（`ekbs_chunk_fts.content_tok`）—— **纯本地，不动向量**。

## 什么时候要跑

`content_tok` 存的是**写入当时**的 jieba 分词结果（预分词表）。所以：

* 改了 `var/dict/userdict.txt`（用户词典）之后必须重建，否则库里还是旧切法；
* 而查询侧是**实时**分词的 —— 不重建就变成"写入侧旧口径、查询侧新口径"，
  同一个编号两边切法不同，**匹配不上**（这是最典型的静默错搜）。

**不需要**重灌向量、不调用云端（Doc2Query 问句是本地启发式生成的，可原样复原）。

⚠️ **重建后必须重启服务**：查询侧是**实时**分词的，而 jieba 的用户词典在进程内加载后**无法卸载**；
已经在跑的 api/worker 进程仍持有旧词典 → 会出现"库是新口径、查询是旧口径"的不一致。
重启后用 ``/health/deps.fts_userdict`` 确认（应显示词典路径与词条数，而不是 ``none``）。

## 用法

    .venv/bin/python -m ekbs.tools.rebuild_fts              # 全部文档
    .venv/bin/python -m ekbs.tools.rebuild_fts --doc 51     # 只重建某几篇
    .venv/bin/python -m ekbs.tools.rebuild_fts --dry-run    # 只报告将要重建什么

实现上**复用生产写入器**（`FtsIndex.upsert_retrievable_with_doc2query`），
与入库路径同一段代码 —— 避免"重建出来的行列结构与生产不一致"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ekbs.config import load_settings  # noqa: E402
from ekbs.db import Database  # noqa: E402
from ekbs.index.fts import FtsIndex, ensure_userdict, tokenize  # noqa: E402
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE  # noqa: E402


def rebuild(
    doc_ids: list[int] | None,
    *,
    dry_run: bool = False,
    settings: object | None = None,
    db: object | None = None,
) -> dict:
    """重建 FTS。``settings``/``db`` 可注入（测试必须注入，否则会连到仓库真实库）。"""
    settings = settings if settings is not None else load_settings()
    db = db if db is not None else Database(settings)  # type: ignore[arg-type]
    fts = FtsIndex(db)  # type: ignore[arg-type]

    with db.sync_connect() as conn:
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            docs = [
                int(r[0])
                for r in conn.execute(
                    f"SELECT doc_id FROM ekbs_document WHERE doc_id IN ({placeholders})", tuple(doc_ids)
                )
            ]
        else:
            docs = [int(r[0]) for r in conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id")]

        report = {"docs": len(docs), "chunks": 0, "rows_before": fts.count(), "rows_after": 0}
        for doc_id in docs:
            rows = conn.execute(
                "SELECT c.chunk_id, c.content, c.heading_path, c.chunk_type, c.shard_id "
                "FROM ekbs_chunk c "
                f"WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE} ORDER BY c.seq",
                (doc_id,),
            ).fetchall()
            if not rows:
                continue
            report["chunks"] += len(rows)
            if dry_run:
                continue
            chunk_ids = [int(r["chunk_id"]) for r in rows]
            shard_id = str(rows[0]["shard_id"] or "")
            fts.delete_chunk_ids(chunk_ids)
            written = fts.upsert_retrievable_with_doc2query(
                [
                    (
                        int(r["chunk_id"]),
                        str(r["shard_id"] or shard_id),
                        str(r["content"]),
                        r["heading_path"],
                        str(r["chunk_type"] or "child"),
                    )
                    for r in rows
                ],
                enabled=bool(getattr(settings.retrieval, "doc2query_enabled", True)),
                max_questions=int(getattr(settings.retrieval, "doc2query_max", 4) or 4),
            )
            report["rows_after"] += written
        report["rows_after"] = report["rows_after"] or fts.count()
        report["rows_final"] = fts.count()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重建 FTS 预分词（改用户词典后必须跑）")
    ap.add_argument("--doc", action="append", type=int, default=None, help="只重建这些 doc_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = ensure_userdict(force=True)
    print(f"  用户词典：{state}")
    if state == "none":
        print("  ⚠️ 没找到用户词典（var/dict/userdict.txt）—— 重建也能跑，但不会带来整词收益")
    # 抽样展示词典效果，便于确认"确实生效"
    for probe in ("XJWY-GC-01-01", "XJ-01-11-1F02", "值班记录"):
        print(f"    {probe:16} → {tokenize(probe)}")

    report = rebuild(args.doc, dry_run=args.dry_run)
    if args.dry_run:
        print(f"  [dry-run] 文档 {report['docs']} 篇 / 可检索块 {report['chunks']} 个；"
              f"当前 FTS 行 {report['rows_before']}")
        return 0
    print(f"  重建完成：文档 {report['docs']} 篇 / 块 {report['chunks']} 个；"
          f"FTS 行 {report['rows_before']} → {report['rows_final']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
