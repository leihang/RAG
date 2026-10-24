"""答案侧评测（阶段 A：生成 + 官方口径匹配，阶段 B 由 RAGAS 脚本做 judge）。

阶段 A（主 venv，cloud 配置）：
    检索 → pipeline.generate → 逐题保存 answer / citations / refused / faithfulness
    同时用 RGB 官方 `checkanswer` 的口径（参考答案字符串是否出现在回答里）算准确率；
    holdout 题应被拒答（refused=true）。
输出：var/eval/rgb/answers.jsonl + summary.json

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/answer_eval.py [--limit N] [--subsets refine,int,fact]
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _official_match(answer: str, ground_truth: list[str]) -> bool:
    """RGB 官方 checkanswer 口径：任一参考答案（不区分大小写）出现在回答里即算对。"""
    text = (answer or "").lower()
    if not ground_truth:
        return False
    for gt in ground_truth:
        if gt is None:
            continue
        if isinstance(gt, list):
            if any(str(g).lower() in text for g in gt):
                return True
        elif str(gt).lower() in text:
            return True
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="RGB 答案生成评测（阶段 A）")
    parser.add_argument("--subsets", default="refine,int,fact")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--config", default="config/application.eval.yaml")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--root", default=str(RGB))
    parser.add_argument("--kb-code", default=KB_CODE)
    args = parser.parse_args()

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="eval_answer")
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (args.kb_code,)).fetchone()
        shard_rows = conn.execute("SELECT shard_id FROM ekbs_shard WHERE status = 'active'").fetchall()
    if kb is None:
        print("评测库还没有 RGB 语料", file=sys.stderr)
        return 2
    perm = PermContext(
        user_id=0,
        dingtalk_userid="bench",
        clearance=4,
        dept_chain=(),
        visible_shard_ids=tuple(sorted(str(r["shard_id"]) for r in shard_rows)),
        visible_kb_ids=(int(kb["kb_id"]),),
        policy_version=1,
    )

    root = Path(args.root)
    wanted = {s.strip() for s in args.subsets.split(",") if s.strip()}
    questions = [q for q in _load(root / "questions.jsonl") if q["subset"] in wanted]
    if args.limit:
        questions = questions[: args.limit]

    out_path = root / "answers.jsonl"
    done: set[str] = set()
    if out_path.exists():
        for row in _load(out_path):
            done.add(row["id"])
    print(f"待生成 {len(questions) - len(done & {q['id'] for q in questions})} / {len(questions)}")

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    written = 0
    todo = [q for q in questions if q["id"] not in done]

    async def one(q: dict[str, Any]) -> None:
        nonlocal written
        async with sem:
            retrieval = await runtime.pipeline.retrieve(q["query"], perm)
            answer = await runtime.pipeline.generate(q["query"], retrieval, perm)
            row = {
                "id": q["id"],
                "subset": q["subset"],
                "query": q["query"],
                "holdout": bool(q["holdout"]),
                "answers": q["answers"],
                "fake_answers": q.get("fake_answers") or [],
                "answer": answer.answer,
                "refused": answer.refused,
                "refusal_reason": answer.refusal_reason,
                "citations": [c.chunk_id for c in answer.citations],
                # RAGAS 需要的 retrieved_contexts（保留检索到的全部上下文，不只是引用）
                "contexts": [c.content for c in retrieval.chunks],
                "n_citations": len(answer.citations),
                "degraded": list(answer.degraded),
                "faithfulness": answer.faithfulness,
                "top_score": retrieval.top_score,
                "official_match": _official_match(answer.answer, q["answers"]),
                "fake_answer_seen": any(
                    str(f).lower() in (answer.answer or "").lower() for f in (q.get("fake_answers") or [])
                ),
            }
            async with lock:
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                if written % 25 == 0:
                    print(f"  {written}/{len(todo)}", flush=True)

    await asyncio.gather(*(one(q) for q in todo))

    rows = _load(out_path)
    scored = [r for r in rows if not r["holdout"]]
    neg = [r for r in rows if r["holdout"]]

    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows) or 1
        return {
            "questions": len(rows),
            "official_accuracy": sum(r["official_match"] for r in rows) / n,
            "refused_rate": sum(r["refused"] for r in rows) / n,
            "fake_answer_rate": (
                sum(r["fake_answer_seen"] for r in rows) / len(rows)
                if any(r["subset"] == "fact" for r in rows)
                else None
            ),
            "mean_citations": sum(r["n_citations"] for r in rows) / n,
        }

    summary = {"overall": agg(scored), "negative_holdout": agg(neg)}
    for subset in ("refine", "int", "fact"):
        subset_rows = [r for r in scored if r["subset"] == subset]
        if subset_rows:
            summary[subset] = agg(subset_rows)
    # fact：反事实题的"答错文档"与"被假答案带偏"分开看
    fact_rows = [r for r in scored if r["subset"] == "fact"]
    if fact_rows:
        summary["fact"]["official_accuracy_on_correct"] = sum(r["official_match"] for r in fact_rows) / len(fact_rows)
        summary["fact"]["fake_answer_seen_rate"] = sum(r["fake_answer_seen"] for r in fact_rows) / len(fact_rows)
    (root / "answer_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
