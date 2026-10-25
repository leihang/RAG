"""CRUD-RAG 评测：检索 key_info 覆盖率 + 答案生成 + DeepSeek 判官。

阶段：
    run   ：检索 + 生成，落 contexts/answer/key_info 覆盖指标（需 DASHSCOPE_API_KEY）
    judge ：DeepSeek 逐条判 key_info 是否被答案覆盖 + 回答与参考答案一致性

指标：
    retrieval_keyinfo_recall@10 —— top-10 上下文覆盖的 key_info 比例（字符串归一口径）
    retrieval_hit_rate          —— 至少命中一条 key_info 的题目比例
    answer_keyinfo_recall       —— DeepSeek 判定答案覆盖的 key_info 比例
    answer_correct_rate         —— DeepSeek 判定"与参考答案一致"的比例
    refused_rate                —— 管道最终拒答率

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/crud_eval.py --stage run --concurrency 4
    zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_eval.py --stage judge'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = REPO / "var" / "eval" / "crud"

JUDGE_SYSTEM = (
    "你是企业知识库问答的判分员。给你问题、参考答案、关键信息点列表、候选回答，逐条判断：\n"
    "1) 候选回答是否覆盖了每个关键信息点（不要求措辞一致，事实一致即可）；\n"
    "2) 候选回答与参考答案是否一致（正确/部分正确/错误）。\n"
    "只输出 JSON：{\"key_info_covered\": [true/false...], \"correct\": 0/0.5/1, \"reason\": \"一句话\"}。"
)


def _norm(text: str) -> str:
    text = text or ""
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    text = text.replace(",", "").replace("，", "")
    return text


def _coverage(key_info: list[str], contexts: list[str]) -> dict[str, Any]:
    joined = _norm("\n".join(contexts))
    covered = [ki for ki in key_info if _norm(ki) and _norm(ki) in joined]
    return {
        "key_info_total": len([ki for ki in key_info if _norm(ki)]),
        "key_info_covered": len(covered),
        "coverage": (len(covered) / len([ki for ki in key_info if _norm(ki)])) if key_info else 0.0,
        "hit": bool(covered),
    }


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _run(args: argparse.Namespace) -> int:
    os.environ.setdefault("EKBS_CONFIG", "config/application.eval-crud.yaml")
    from ekbs.config import load_settings
    from ekbs.permission.context import PermContext
    from ekbs.runtime import Runtime

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="crud_eval")
    with runtime.db.sync_connect() as conn:
        kb = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (args.kb_code,)).fetchone()
        shards = conn.execute("SELECT shard_id FROM ekbs_shard WHERE status='active'").fetchall()
    if kb is None:
        print("CRUD 评测库还没灌语料", file=sys.stderr)
        return 2
    perm = PermContext(
        user_id=0, dingtalk_userid="bench", clearance=4, dept_chain=(),
        visible_shard_ids=tuple(sorted(str(r["shard_id"]) for r in shards)),
        visible_kb_ids=(int(kb["kb_id"]),), policy_version=1,
    )
    questions = _load(ROOT / "questions.jsonl")
    if args.limit:
        questions = questions[: args.limit]
    out_path = Path(args.out) if args.out else (ROOT / "results.jsonl")
    done = {r["id"] for r in _load(out_path)}
    todo = [q for q in questions if q["id"] not in done]
    print(f"待跑 {len(todo)} / {len(questions)}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    lock = asyncio.Lock()
    written = 0

    async def one(q: dict) -> None:
        nonlocal written
        async with sem:
            retrieval = await runtime.pipeline.retrieve(q["query"], perm)
            if args.retrieval_only:
                answer = None
            else:
                answer = await runtime.pipeline.generate(q["query"], retrieval, perm)
            contexts = [c.content for c in retrieval.chunks]
            row = {
                "id": q["id"],
                "subset": q["subset"],
                "query": q["query"],
                "reference": q.get("reference") or "",
                "key_info": q.get("key_info") or [],
                "answer": answer.answer if answer is not None else "",
                "refused": retrieval.refused if answer is None else answer.refused,
                "refusal_reason": retrieval.refusal_reason if answer is None else answer.refusal_reason,
                "top_score": retrieval.top_score,
                "contexts": contexts,
                **_coverage(q.get("key_info") or [], contexts),
            }
            async with lock:
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                if written % 25 == 0:
                    print(f"  {written}/{len(todo)}", flush=True)

    await asyncio.gather(*(one(q) for q in todo))
    summary_path = Path(args.summary_out) if args.summary_out else (ROOT / "summary.json")
    _summarize(out_path, summary_path)
    return 0


def _summarize(path: Path, out: Path) -> None:
    rows = _load(path)
    summary: dict[str, Any] = {"n": len(rows)}
    by_task: dict[str, dict[str, Any]] = {}
    for task in ("1doc", "2docs", "3docs"):
        part = [r for r in rows if r["subset"] == task]
        if part:
            by_task[task] = {
                "n": len(part),
                "retrieval_keyinfo_recall@10": sum(r["coverage"] for r in part) / len(part),
                "retrieval_hit_rate": sum(1 for r in part if r["hit"]) / len(part),
                "refused_rate": sum(1 for r in part if r["refused"]) / len(part),
            }
    summary["by_task"] = by_task
    summary["overall"] = {
        "n": len(rows),
        "retrieval_keyinfo_recall@10": sum(r["coverage"] for r in rows) / (len(rows) or 1),
        "retrieval_hit_rate": sum(1 for r in rows if r["hit"]) / (len(rows) or 1),
        "refused_rate": sum(1 for r in rows if r["refused"]) / (len(rows) or 1),
    }
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def _judge(args: argparse.Namespace) -> int:
    import httpx

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    rows = _load(Path(args.results) if args.results else (ROOT / "results.jsonl"))
    if args.limit:
        rows = rows[: args.limit]
    out_path = Path(args.judge_out) if args.judge_out else (ROOT / "judge.jsonl")
    done = {r["id"] for r in _load(out_path)}
    todo = [r for r in rows if r["id"] not in done]
    print(f"待判 {len(todo)} / {len(rows)}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    lock = asyncio.Lock()
    written = 0

    async def one(row: dict, client: httpx.AsyncClient) -> None:
        nonlocal written
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"问题：{row['query']}\n参考答案：{row['reference']}\n"
                        f"关键信息点：{json.dumps(row['key_info'], ensure_ascii=False)}\n"
                        f"候选回答：{row['answer']}"
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"}
        verdict: dict[str, Any] = {"key_info_covered": [], "correct": None, "reason": "ERR"}
        for attempt in range(6):
            try:
                async with sem:
                    resp = await client.post("https://api.deepseek.com/v1/chat/completions", json=payload, headers=headers, timeout=90)
                if resp.status_code == 200:
                    data = json.loads(resp.json()["choices"][0]["message"]["content"])
                    verdict = {
                        "key_info_covered": [bool(x) for x in (data.get("key_info_covered") or [])],
                        "correct": data.get("correct"),
                        "reason": str(data.get("reason") or "")[:200],
                    }
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2 ** attempt)
        n = len(row["key_info"]) or 1
        covered = verdict["key_info_covered"]
        row_out = {
            "id": row["id"], "subset": row["subset"],
            "answer_keyinfo_recall": (sum(covered) / n) if covered else None,
            "correct": verdict["correct"], "reason": verdict["reason"],
        }
        async with lock:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row_out, ensure_ascii=False) + "\n")
            written += 1
            if written % 25 == 0:
                print(f"  {written}/{len(todo)}", flush=True)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(row, client) for row in todo))

    judged = _load(out_path)
    summary: dict[str, Any] = {"n": len(judged)}
    by_task = {}
    for task in ("1doc", "2docs", "3docs"):
        part = [r for r in judged if r["subset"] == task and r["answer_keyinfo_recall"] is not None]
        if part:
            by_task[task] = {
                "n": len(part),
                "answer_keyinfo_recall": sum(r["answer_keyinfo_recall"] for r in part) / len(part),
                "answer_correct_rate": sum(1 for r in part if r.get("correct") in (1, 1.0)) / len(part),
                "partial_rate": sum(1 for r in part if r.get("correct") in (0.5,)) / len(part),
            }
    valid = [r for r in judged if r["answer_keyinfo_recall"] is not None]
    summary["by_task"] = by_task
    summary["overall"] = {
        "n": len(valid),
        "answer_keyinfo_recall": sum(r["answer_keyinfo_recall"] for r in valid) / (len(valid) or 1),
        "answer_correct_rate": sum(1 for r in valid if r.get("correct") in (1, 1.0)) / (len(valid) or 1),
        "partial_rate": sum(1 for r in valid if r.get("correct") in (0.5,)) / (len(valid) or 1),
    }
    summary_path = out_path.with_name(f"judge_summary_{out_path.stem}.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD-RAG 评测")
    parser.add_argument("--stage", choices=["run", "judge"], required=True)
    parser.add_argument("--config", default="config/application.eval-crud.yaml")
    parser.add_argument("--kb-code", default="crud_bench")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retrieval-only", action="store_true", help="只跑检索（不生成答案），用于参数实验")
    parser.add_argument("--out", default="", help="结果文件（默认 <root>/results.jsonl）")
    parser.add_argument("--summary-out", default="", help="汇总文件")
    parser.add_argument("--results", default="", help="judge 阶段输入（默认 <root>/results.jsonl）")
    parser.add_argument("--judge-out", default="", help="judge 阶段输出（默认 <root>/judge.jsonl）")
    args = parser.parse_args()
    if args.stage == "run":
        return asyncio.run(_run(args))
    return asyncio.run(_judge(args))


if __name__ == "__main__":
    raise SystemExit(main())
