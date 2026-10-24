"""采集负例库的检索上下文（供 no_answer_judge.py 判断"资料是否足够"）。

复用业务检索管道（同一配置/阈值），只把 top-k 上下文落盘，不生成答案。
输出：var/eval/rgb-neg/contexts.jsonl
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
os.environ.setdefault("EKBS_CONFIG", "config/application.eval-neg.yaml")

from ekbs.config import load_settings  # noqa: E402
from ekbs.permission.context import PermContext  # noqa: E402
from ekbs.runtime import Runtime  # noqa: E402


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main() -> int:
    parser = argparse.ArgumentParser(description="采集负例检索上下文")
    parser.add_argument("--root", default=str(REPO / "var" / "eval" / "rgb-neg"))
    parser.add_argument("--kb-code", default="rgb_neg")
    parser.add_argument("--config", default="config/application.eval-neg.yaml")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    root = Path(args.root)
    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="eval_ctx")
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (args.kb_code,)).fetchone()
        shard_rows = conn.execute("SELECT shard_id FROM ekbs_shard WHERE status='active'").fetchall()
    if kb is None:
        print("评测库缺 KB", file=sys.stderr)
        return 2
    perm = PermContext(
        user_id=0, dingtalk_userid="bench", clearance=4, dept_chain=(),
        visible_shard_ids=tuple(sorted(str(r["shard_id"]) for r in shard_rows)),
        visible_kb_ids=(int(kb["kb_id"]),), policy_version=1,
    )
    questions = _load(root / "questions.jsonl")
    out_path = root / "contexts.jsonl"
    rows: list[dict] = [{} for _ in questions]
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    async def one(i: int, q: dict) -> None:
        nonlocal done
        async with sem:
            retrieval = await runtime.pipeline.retrieve(q["query"], perm)
            rows[i] = {
                "id": q["id"], "subset": q["subset"], "query": q["query"],
                "holdout": True,
                "refused": bool(retrieval.refused),
                "refusal_reason": retrieval.refusal_reason,
                "top_score": retrieval.top_score,
                "contexts": [c.content for c in retrieval.chunks[: args.k]],
            }
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(questions)}", flush=True)

    await asyncio.gather(*(one(i, q) for i, q in enumerate(questions)))
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"写好 {out_path}（{len(rows)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
