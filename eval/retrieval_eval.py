"""检索侧评测：在隔离评测库上跑 RGB 中文题集，算 Hit@k / Recall@k / MRR / nDCG@10。

用法（入库完成后）：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/retrieval_eval.py --k 10

指标口径：
* 文档级：同一个 doc_title 只算一次（父块扩展/多块命中不重复计数）；
* Recall@k：gold slot 覆盖数 / gold slot 总数（同一槽位的多个候选文档任一命中即算该槽命中）；
* nDCG@10：二值相关性、按唯一文档顺序折损；
* 负例拒答：holdout 题的 gold 文档未入库，应 refused=true；报"拒答率"与"答了但没依据"率。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("EKBS_CONFIG", "config/application.eval.yaml")

from ekbs.config import load_settings  # noqa: E402
from ekbs.permission.context import PermContext  # noqa: E402
from ekbs.runtime import Runtime  # noqa: E402

RGB = REPO / "var" / "eval" / "rgb"
KB_CODE = "rgb_bench"


def _title(h: str) -> str:
    return f"rgb-{h[:12]}"


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ndcg(ranked: list[str], gold: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, t in enumerate(ranked[:k], 1) if t in gold)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def _metrics(q: dict[str, Any], ranked: list[str], k: int) -> dict[str, Any]:
    slots = [[_title(h) for h in slot] for slot in q.get("gold_slots") or []]
    gold_all = {t for slot in slots for t in slot}
    first = None
    for i, t in enumerate(ranked, 1):
        if t in gold_all:
            first = i
            break
    hit = first is not None and first <= k
    covered = sum(1 for slot in slots if any(t in ranked[:k] for t in slot))
    return {
        "hit_at_k": bool(hit),
        "rank": first,
        "mrr": (1.0 / first) if first else 0.0,
        "recall_at_k": (covered / len(slots)) if slots else 0.0,
        "ndcg_at_10": _ndcg(ranked, gold_all, 10),
        "top_docs": ranked[:k],
        "gold_slots": len(slots),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="RGB 中文检索评测")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--subsets", default="refine,int,fact")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--config", default="config/application.eval.yaml")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="eval_retrieval")
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (KB_CODE,)).fetchone()
        shard_rows = conn.execute(
            "SELECT shard_id FROM ekbs_shard WHERE status = 'active'"
        ).fetchall()
    if kb is None:
        print("评测库还没有 RGB 语料（先跑 eval/ingest_rgb.py）", file=sys.stderr)
        return 2
    kb_id = int(kb["kb_id"])
    visible = tuple(sorted(str(r["shard_id"]) for r in shard_rows))
    perm = PermContext(
        user_id=0,
        dingtalk_userid="bench",
        clearance=4,
        dept_chain=(),
        visible_shard_ids=visible,
        visible_kb_ids=(kb_id,),
        policy_version=1,
    )

    wanted = {s.strip() for s in args.subsets.split(",") if s.strip()}
    questions = [q for q in _load(RGB / "questions.jsonl") if q["subset"] in wanted]
    if args.limit:
        questions = questions[: args.limit]

    results: list[dict[str, Any]] = [{} for _ in questions]
    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0

    async def one(idx: int, q: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            retrieval = await runtime.pipeline.retrieve(q["query"], perm)
            ranked: list[str] = []
            for chunk in retrieval.chunks:
                title = str(getattr(chunk, "doc_title", "") or "")
                if title and title not in ranked:
                    ranked.append(title)
            row = {
                "id": q["id"],
                "subset": q["subset"],
                "query": q["query"],
                "holdout": bool(q["holdout"]),
                "refused": bool(retrieval.refused),
                "refusal_reason": retrieval.refusal_reason,
                "top_score": retrieval.top_score,
                "n_chunks": len(retrieval.chunks),
                **_metrics(q, ranked, args.k),
            }
            if q.get("wrong_docs"):
                wrong = {_title(h) for h in q["wrong_docs"]}
                row["wrong_doc_seen"] = any(t in wrong for t in ranked[: args.k])
            results[idx] = row
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(questions)}", flush=True)

    await asyncio.gather(*(one(i, q) for i, q in enumerate(questions)))

    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [r for r in rows if not r["holdout"]]
        neg = [r for r in rows if r["holdout"]]
        n = len(scored) or 1
        out = {
            "questions": len(rows),
            "answerable": len(scored),
            "negative": len(neg),
            "hit_at_k": sum(r["hit_at_k"] for r in scored) / n,
            "mrr": sum(r["mrr"] for r in scored) / n,
            "recall_at_k": sum(r["recall_at_k"] for r in scored) / n,
            "ndcg_at_10": sum(r["ndcg_at_10"] for r in scored) / n,
            "refused_answerable_rate": sum(r["refused"] for r in scored) / n,
            "negative_refused_rate": (sum(r["refused"] for r in neg) / len(neg)) if neg else None,
        }
        if any(r.get("wrong_doc_seen") is not None for r in rows):
            fact = [r for r in rows if r.get("wrong_doc_seen") is not None]
            out["wrong_doc_seen_rate"] = sum(r["wrong_doc_seen"] for r in fact) / len(fact)
        return out

    summary = {"overall": agg(results)}
    by_subset: dict[str, dict[str, Any]] = {}
    for subset in sorted({r["subset"] for r in results}):
        by_subset[subset] = agg([r for r in results if r["subset"] == subset])
    summary["by_subset"] = by_subset
    summary["k"] = args.k
    summary["config"] = args.config
    summary["visible_shards"] = list(visible)

    (RGB / "retrieval_report.json").write_text(
        json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n== 检索评测 ==")
    for name, a in [("overall", summary["overall"]), *by_subset.items()]:
        neg_rate = a["negative_refused_rate"]
        neg_text = f"{neg_rate:.3f}" if neg_rate is not None else "n/a"
        print(
            f"[{name}] n={a['questions']} 可答={a['answerable']} 负例={a['negative']} | "
            f"Hit@{args.k}={a['hit_at_k']:.3f} MRR={a['mrr']:.3f} "
            f"Recall@{args.k}={a['recall_at_k']:.3f} nDCG@10={a['ndcg_at_10']:.3f} | "
            f"可答误拒={a['refused_answerable_rate']:.3f} 负例拒答={neg_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
