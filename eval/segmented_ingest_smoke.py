"""分段索引真机 smoke（P3）：小批量入库 → 段冻结/flush → compaction → 检索可用。

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/segmented_ingest_smoke.py --limit 20 --personal
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = REPO / "var" / "eval" / "crud"
CONFIG = "config/application.eval-segtest.yaml"
KB_CODE = "crud_bench"


async def main() -> int:
    parser = argparse.ArgumentParser(description="分段索引入库 smoke")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--personal", action="store_true", help="按个人库模式（单篇 flush）")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    os.environ["EKBS_CONFIG"] = CONFIG

    from ekbs.config import load_settings
    from ekbs.ingest.pipeline import IngestPipeline
    from ekbs.permission.context import PermContext
    from ekbs.runtime import Runtime

    settings = load_settings(REPO / CONFIG)
    runtime = Runtime.create(settings, service="seg_smoke", init_schema=True)
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    docs = list(manifest["docs"].items())[: args.limit]
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, description, default_level, status, created_at) "
            "VALUES (?, ?, ?, 1, 1, datetime('now')) ON CONFLICT(code) DO NOTHING",
            (KB_CODE, "segtest", "分段索引 smoke"),
        )
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (KB_CODE,)).fetchone()
        conn.commit()
    kb_id = int(kb["kb_id"])

    pipeline = IngestPipeline(
        db=runtime.db, settings=settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, figure_index=runtime.figure_index,
    )
    sem = asyncio.Semaphore(args.concurrency)
    ok = 0
    errors: list[str] = []
    t0 = time.perf_counter()

    async def one(h: str, meta: dict) -> None:
        nonlocal ok
        async with sem:
            try:
                await pipeline.ingest_markdown(
                    title=str(meta["title"]),
                    markdown=(REPO / meta["path"]).read_text(encoding="utf-8"),
                    kb_id=kb_id, level=1, visible_dept_ids=[],
                    source_path=f"bench://segtest/{h}", real_corpus=True,
                    personal=args.personal,
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{h}: {type(exc).__name__}: {exc}"[:200])

    await asyncio.gather(*(one(h, m) for h, m in docs))
    elapsed = time.perf_counter() - t0
    if errors:
        print("ERRORS:", json.dumps(errors[:5], ensure_ascii=False, indent=2), flush=True)

    with runtime.db.sync_connect() as conn:
        docs_n = int(conn.execute("SELECT COUNT(*) c FROM ekbs_document").fetchone()["c"])
        chunks = int(conn.execute("SELECT COUNT(*) c FROM ekbs_chunk").fetchone()["c"])
        shard = conn.execute(
            "SELECT shard_id, vector_count FROM ekbs_shard WHERE status='active'"
        ).fetchone()
    shard_id = str(shard["shard_id"])
    seg = runtime.shards._get_segmented(shard_id)
    print(json.dumps({
        "personal": args.personal, "ok": ok, "errors": errors[:3],
        "elapsed_s": round(elapsed, 1), "docs_per_s": round(ok / max(1e-9, elapsed), 2),
        "documents": docs_n, "chunks": chunks,
        "segments": len(seg.segments), "active_vectors": int(seg.active.vector_count if seg.active else 0),
        "vector_count": seg.count,
    }, ensure_ascii=False, indent=2))

    compact = runtime.shards.compact_all()
    seg = runtime.shards._get_segmented(shard_id)
    print(json.dumps({"compact": compact, "segments_after": len(seg.segments)}, ensure_ascii=False))
    shards = [(r.shard_id, r.vector_count) for r in runtime.shards.registry()]
    perm = PermContext(
        user_id=0, dingtalk_userid="bench", clearance=4, dept_chain=(),
        visible_shard_ids=tuple(s for s, _ in shards),
        visible_kb_ids=(kb_id,), policy_version=1,
    )
    result = await runtime.pipeline.retrieve("哪个城市发放了体育类电子消费券？", perm)
    print(json.dumps({"retrieved": len(result.chunks), "top_score": result.top_score,
                      "refused": result.refused}, ensure_ascii=False))
    await runtime.aclose()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
