"""CRUD 长文档实验：当前检索上下文 vs "整篇源文档"证据聚合。

对 773 题逐题检索（wide+alpha0.5 配置），记录：
- 当前上下文的 key_info 覆盖（与线上一致）；
- 把召回文档替换成**整篇文档**后的 key_info 覆盖（验证"切块是否藏了证据"）；
- 召回文档中是否包含全部已定位源文档（验证瓶颈是"选文档"还是"切块"）。

输出：var/eval/crud/aggregation.jsonl + aggregation_summary.json
运行：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/crud_aggregation.py --concurrency 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = REPO / "var" / "eval" / "crud"


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\s+", "", text)
    return text.replace(",", "").replace("，", "")


def _coverage(key_info: list[str], text: str) -> tuple[int, int]:
    joined = _norm(text)
    keys = [k for k in key_info if _norm(k)]
    covered = sum(1 for k in keys if _norm(k) in joined)
    return covered, len(keys)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD 证据聚合实验")
    parser.add_argument("--config", default="config/application.eval-crud-wide-a05.yaml")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="", help="逐题输出（默认 aggregation.jsonl）")
    parser.add_argument("--summary-out", default="", help="汇总输出（默认 aggregation_summary.json）")
    args = parser.parse_args()
    os.environ.setdefault("EKBS_CONFIG", args.config)

    from ekbs.config import load_settings
    from ekbs.permission.context import PermContext
    from ekbs.runtime import Runtime

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="crud_agg")
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = 'crud_bench'").fetchone()
        shards = conn.execute("SELECT shard_id FROM ekbs_shard WHERE status='active'").fetchall()
        doc_paths = {
            int(r["doc_id"]): str(r["source_path"]).replace("bench://crud/", "")
            for r in conn.execute("SELECT doc_id, source_path FROM ekbs_document WHERE source_path LIKE 'bench://crud/%'")
        }
    perm = PermContext(
        user_id=0, dingtalk_userid="bench", clearance=4, dept_chain=(),
        visible_shard_ids=tuple(sorted(str(r["shard_id"]) for r in shards)),
        visible_kb_ids=(int(kb["kb_id"]),), policy_version=1,
    )
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["docs"]
    sources = json.load(open(ROOT / "source_docs.json", encoding="utf-8"))["per_question"]
    questions = _load(ROOT / "questions.jsonl")
    if args.limit:
        questions = questions[: args.limit]
    out = Path(args.out) if args.out else (ROOT / "aggregation.jsonl")
    done = {r["id"] for r in _load(out)}
    todo = [q for q in questions if q["id"] not in done]
    print(f"待跑 {len(todo)} / {len(questions)}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    lock = asyncio.Lock()
    n = 0

    async def one(q: dict) -> None:
        nonlocal n
        async with sem:
            ret = await runtime.pipeline.retrieve(q["query"], perm)
            chunks = ret.chunks
            current_text = "\n".join(c.content for c in chunks)
            doc_ids = list(dict.fromkeys(int(c.doc_id) for c in chunks))
            full_texts = []
            for did in doc_ids:
                h = doc_paths.get(did, "")
                meta = manifest.get(h)
                if meta:
                    p = REPO / meta["path"]
                    if p.exists():
                        full_texts.append(p.read_text(encoding="utf-8", errors="ignore"))
            covered_cur, total = _coverage(q.get("key_info") or [], current_text)
            covered_full, _ = _coverage(q.get("key_info") or [], "\n".join(full_texts))
            src = set(sources.get(q["id"]) or [])
            hit_src = sum(1 for h in src if h in {doc_paths.get(d, "") for d in doc_ids})
            row = {
                "id": q["id"], "subset": q["subset"],
                "key_info_total": total,
                "covered_current": covered_cur,
                "covered_full_docs": covered_full,
                "unique_docs": len(doc_ids),
                "source_docs": len(src),
                "source_docs_hit": hit_src,
                "source_any_hit": 1 if hit_src else 0,
            }
            async with lock:
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
                if n % 25 == 0:
                    print(f"  {n}/{len(todo)}", flush=True)

    await asyncio.gather(*(one(q) for q in todo))

    rows = _load(out)
    by_task = {}
    for task in ("1doc", "2docs", "3docs"):
        part = [r for r in rows if r["subset"] == task]
        if not part:
            continue
        tot = sum(r["key_info_total"] for r in part) or 1
        by_task[task] = {
            "n": len(part),
            "coverage_current": sum(r["covered_current"] for r in part) / tot,
            "coverage_full_docs": sum(r["covered_full_docs"] for r in part) / tot,
            "source_doc_recall": sum(r["source_docs_hit"] for r in part) / (sum(r["source_docs"] for r in part) or 1),
            "source_any_hit_rate": sum(r.get("source_any_hit", 1 if r.get("source_docs_hit") else 0) for r in part) / len(part),
            "avg_unique_docs": sum(r["unique_docs"] for r in part) / len(part),
        }
    tot = sum(r["key_info_total"] for r in rows) or 1
    summary = {
        "n": len(rows),
        "overall": {
            "coverage_current": sum(r["covered_current"] for r in rows) / tot,
            "coverage_full_docs": sum(r["covered_full_docs"] for r in rows) / tot,
            "source_doc_recall": sum(r["source_docs_hit"] for r in rows) / (sum(r["source_docs"] for r in rows) or 1),
            "source_any_hit_rate": sum(r.get("source_any_hit", 1 if r.get("source_docs_hit") else 0) for r in rows) / len(rows),
            "avg_unique_docs": sum(r["unique_docs"] for r in rows) / len(rows),
        },
        "by_task": by_task,
    }
    summary_out = Path(args.summary_out) if args.summary_out else (ROOT / "aggregation_summary.json")
    summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
