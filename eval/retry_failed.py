"""重试 RGB 评测库里未 published 的文档（并发灌库时偶发 embed 失败的兜底）。

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/retry_failed.py --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("EKBS_CONFIG", "config/application.eval.yaml")

from ekbs.config import load_settings  # noqa: E402
from ekbs.ingest.pipeline import IngestPipeline  # noqa: E402
from ekbs.runtime import Runtime  # noqa: E402

RGB = REPO / "var" / "eval" / "rgb"
SOURCE_PREFIX = "bench://rgb/"


async def main() -> int:
    parser = argparse.ArgumentParser(description="重试未入库成功的 RGB 文档")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--config", default="config/application.eval.yaml")
    parser.add_argument("--root", default=str(RGB))
    parser.add_argument("--kb-code", default="rgb_bench")
    parser.add_argument("--source-prefix", default=SOURCE_PREFIX)
    parser.add_argument(
        "--defer-faiss-save", action="store_true",
        help="评测加速：重试期间跳过逐篇全量写 FAISS（结束一次性落盘）",
    )
    args = parser.parse_args()

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="eval_retry", init_schema=False)
    defer_save = bool(args.defer_faiss_save)
    original_save = None
    if defer_save:
        from ekbs.index.faiss_store import ShardIndex

        original_save = ShardIndex.save

        def _defer_save(self, path=None, *, atomic=True):  # type: ignore[no-untyped-def]
            return None

        ShardIndex.save = _defer_save  # type: ignore[method-assign]
    manifest = json.loads((Path(args.root) / "manifest.json").read_text(encoding="utf-8"))
    docs = manifest["docs"]
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (args.kb_code,)).fetchone()
        bad = conn.execute(
            "SELECT doc_id, source_path, status FROM ekbs_document "
            "WHERE source_path LIKE ? AND status != 'published'",
            (f"{args.source_prefix}%",),
        ).fetchall()
    kb_id = int(kb["kb_id"])
    print(f"待重试 {len(bad)} 篇：{[(r['doc_id'], r['status']) for r in bad[:5]]} ...")
    pipeline = IngestPipeline(
        db=runtime.db, settings=settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, figure_index=runtime.figure_index,
    )
    sem = asyncio.Semaphore(max(1, args.concurrency))
    ok = 0
    errors: list[str] = []

    async def one(row) -> None:
        nonlocal ok
        h = str(row["source_path"]).rsplit("/", 1)[-1]
        meta = docs.get(h)
        if meta is None:
            errors.append(f"{h}: manifest 里没有")
            return
        async with sem:
            try:
                await pipeline.ingest_markdown(
                    title=str(meta["title"]),
                    markdown=(REPO / meta["path"]).read_text(encoding="utf-8"),
                    kb_id=kb_id,
                    level=1,
                    visible_dept_ids=[],
                    source_path=f"{args.source_prefix}{h}",
                    real_corpus=True,
                    existing_doc_id=int(row["doc_id"]),
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{h}: {type(exc).__name__}: {exc}"[:300])

    try:
        await asyncio.gather(*(one(r) for r in bad))
    finally:
        if defer_save and original_save is not None:
            for index in runtime.shards._loaded.values():
                original_save(index)
    with runtime.db.sync_connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) c FROM ekbs_document WHERE source_path LIKE ? AND status != 'published'",
            (f"{args.source_prefix}%",),
        ).fetchone()["c"]
        chunks = conn.execute("SELECT COUNT(*) c FROM ekbs_chunk").fetchone()["c"]
        vectors = conn.execute("SELECT vector_count FROM ekbs_shard WHERE status='active'").fetchone()[
            "vector_count"
        ]
    print(f"成功重试 {ok}，仍失败 {left}，errors={len(errors)}；chunks={chunks} vectors={vectors}")
    if errors:
        print("\n".join(errors[:10]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
