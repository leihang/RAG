"""把 RGB 评测语料灌进隔离的评测库（不碰业务 var/index）。

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/ingest_rgb.py --limit 20 --concurrency 2   # 小批量试跑
    .venv/bin/python eval/ingest_rgb.py                              # 全量（可断点续跑）

说明：
* 配置用 config/application.eval.yaml（data_dir=var/eval/runtime，隔离库）；
* 文档全部 L1（评测只看检索/答案质量，不引入权限变量）；
* 断点续跑：按 source_path='bench://rgb/<hash>' 判断已入库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("EKBS_CONFIG", "config/application.eval.yaml")

from ekbs.config import load_settings  # noqa: E402
from ekbs.ingest.pipeline import IngestPipeline  # noqa: E402
from ekbs.logging import configure_logging  # noqa: E402
from ekbs.runtime import Runtime  # noqa: E402

RGB = REPO / "var" / "eval" / "rgb"
KB_CODE = "rgb_bench"
SOURCE_PREFIX = "bench://rgb/"


def _ensure_kb(runtime: Runtime, kb_code: str = KB_CODE) -> int:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, description, default_level, status, created_at) "
            "VALUES (?, ?, ?, 1, 1, datetime('now')) ON CONFLICT(code) DO NOTHING",
            (kb_code, f"{kb_code} 评测语料", "公开基准 RGB 中文语料（仅评测）"),
        )
        row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (kb_code,)).fetchone()
        conn.commit()
    return int(row["kb_id"])


def _done_hashes(runtime: Runtime, source_prefix: str = SOURCE_PREFIX) -> set[str]:
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT source_path FROM ekbs_document WHERE source_path LIKE ?",
            (f"{source_prefix}%",),
        ).fetchall()
    return {str(r["source_path"]).rsplit("/", 1)[-1] for r in rows}


async def main() -> int:
    parser = argparse.ArgumentParser(description="RGB 语料灌入隔离评测库")
    parser.add_argument("--limit", type=int, default=0, help="只灌前 N 篇（试跑用）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发入库数（默认 1）")
    parser.add_argument("--config", default="config/application.eval.yaml")
    parser.add_argument("--root", default=str(RGB), help="语料目录（manifest.json + docs/）")
    parser.add_argument("--kb-code", default=KB_CODE)
    parser.add_argument("--source-prefix", default=SOURCE_PREFIX)
    parser.add_argument("--manifest", default="", help="自定义 manifest 路径（默认 <root>/manifest.json）")
    args = parser.parse_args()

    settings = load_settings(REPO / args.config)
    configure_logging(settings, "eval_ingest")
    runtime = Runtime.create(settings, service="eval_ingest", init_schema=True)
    kb_id = _ensure_kb(runtime, args.kb_code)
    pipeline = IngestPipeline(
        db=runtime.db,
        settings=settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )

    root = Path(args.root)
    manifest_path = Path(args.manifest) if args.manifest else (root / "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs: dict[str, dict[str, Any]] = manifest["docs"]
    done = _done_hashes(runtime, args.source_prefix)
    todo = [(h, m) for h, m in docs.items() if h not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待入库 {len(todo)} 篇（已完成 {len(done)}），并发 {args.concurrency}", flush=True)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    t0 = time.perf_counter()
    written = 0

    async def one(h: str, meta: dict[str, Any]) -> None:
        nonlocal written
        async with sem:
            text = (REPO / meta["path"]).read_text(encoding="utf-8")
            try:
                result = await pipeline.ingest_markdown(
                    title=str(meta["title"]),
                    markdown=text,
                    kb_id=kb_id,
                    level=1,
                    visible_dept_ids=[],
                    source_path=f"{args.source_prefix}{h}",
                    real_corpus=True,
                )
                results.append({"hash": h, **result.as_dict()})
            except Exception as exc:  # noqa: BLE001 - 单篇失败不拖垮整批
                errors.append({"hash": h, "error": f"{type(exc).__name__}: {exc}"[:300]})
            written += 1
            if written % 200 == 0:
                rate = written / max(1e-9, time.perf_counter() - t0)
                print(f"  {written}/{len(todo)}  {rate:.1f} 篇/s", flush=True)

    await asyncio.gather(*(one(h, m) for h, m in todo))

    elapsed = time.perf_counter() - t0
    report = {
        "ingested": len(results),
        "errors": errors,
        "elapsed_s": round(elapsed, 1),
        "docs_per_s": round(len(results) / max(1e-9, elapsed), 2),
        "config": args.config,
        "kb_id": kb_id,
    }
    (root / "ingest_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM ekbs_document").fetchone()
        chunks = conn.execute("SELECT COUNT(*) c FROM ekbs_chunk").fetchone()
        shards = conn.execute(
            "SELECT shard_id, vector_count, status FROM ekbs_shard WHERE status='active'"
        ).fetchall()
    print(
        f"完成：入库 {len(results)}，失败 {len(errors)}，耗时 {elapsed:.0f}s "
        f"({len(results)/max(1e-9, elapsed):.1f} 篇/s)；库里文档 {row['c']} / 分块 {chunks['c']}"
    )
    for s in shards:
        print(f"  shard {s['shard_id']} vectors={s['vector_count']} status={s['status']}")
    if errors:
        print("前 3 个错误：", json.dumps(errors[:3], ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
