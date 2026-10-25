"""CRUD-RAG 的 QuestEval 口径对齐（lite 版）。

官方 QuestEval：用 ground-truth 生成的 questions 去问「候选回答」，答不出记"无法推断"，
    quest_recall = 1 - 无法推断比例
本脚本用 CRUD 的 QuestAnswer* 题集里每个 entry 的 question/answers 做同样的事，
判官换成 DeepSeek（与线上判官一致），输出 quest_recall + 字符级 F1。

输入：var/eval/crud/results_wide_a05_ans.jsonl（最佳检索配置下的答案）
输出：var/eval/crud/questeval.jsonl + questeval_summary.json
运行：
    zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_questeval.py --concurrency 8'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = REPO / "var" / "eval" / "crud"
SRC = REPO / "var" / "eval" / "datasets" / "crud"
TASK_FILES = {
    "1doc": "QuestAnswer1Doc.json",
    "2docs": "QuestAnswer2Docs.json",
    "3docs": "QuestAnswer3Docs.json",
}

SYSTEM = (
    "你是严格的问答判官。只根据给定的“候选回答”回答下面的问题；"
    "候选回答没有明确信息时，必须回答“无法推断”。\n"
    "只输出 JSON：{\"answers\": [\"...\"]}，顺序与问题一致。"
)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _f1(pred: str, gold: str) -> float:
    pred, gold = (pred or "").strip(), (gold or "").strip()
    if not pred or not gold or pred == "无法推断":
        return 0.0
    p = list(pred)
    g = list(gold)
    from collections import Counter

    common = sum((Counter(p) & Counter(g)).values())
    if common == 0:
        return 0.0
    precision = common / len(p)
    recall = common / len(g)
    return 2 * precision * recall / (precision + recall)


async def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD QuestEval-lite")
    parser.add_argument("--results", default=str(ROOT / "results_wide_a05_ans.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "questeval.jsonl"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY（用 zsh -ic 运行）", file=sys.stderr)
        return 2

    entries: dict[str, dict[str, Any]] = {}
    for task, filename in TASK_FILES.items():
        data = json.loads((SRC / filename).read_text(encoding="utf-8"))
        for entry_id, value in data.items():
            entries[f"{task}-{entry_id}"] = value

    rows = _load(Path(args.results))
    if args.limit:
        rows = rows[: args.limit]
    out_path = Path(args.out)
    done = {r["id"] for r in _load(out_path)}
    todo = [r for r in rows if r["id"] not in done]
    print(f"待评 {len(todo)} / {len(rows)}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    lock = asyncio.Lock()
    written = 0

    async def one(row: dict, client: httpx.AsyncClient) -> None:
        nonlocal written
        parts = row["id"].split("-")
        entry_key = f"{row['subset']}-{parts[-2]}"  # crud-<task>-<entryid>-<idx>
        entry = entries.get(entry_key) or {}
        questions = [str(x) for x in entry.get("question") or []]
        gold = [str(x) for x in entry.get("answers") or []]
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": "候选回答：\n" + (row.get("answer") or "") + "\n\n问题：\n"
                    + json.dumps(questions, ensure_ascii=False),
                },
            ],
            "temperature": 0,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
        }
        answers: list[str] = []
        for attempt in range(6):
            try:
                async with sem:
                    resp = await client.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        json=payload, headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
                        timeout=90,
                    )
                if resp.status_code == 200:
                    answers = [str(x) for x in json.loads(resp.json()["choices"][0]["message"]["content"]).get("answers") or []]
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(2 ** attempt)
        n = len(questions) or 1
        padded = (answers + ["无法推断"] * n)[:n]
        undetermined = sum(1 for a in padded if a.strip() == "无法推断")
        recalls = 1 - undetermined / n
        f1 = sum(_f1(p, g) for p, g in zip(padded, gold)) / n
        row_out = {
            "id": row["id"], "subset": row["subset"],
            "questions": len(questions), "undetermined": undetermined,
            "quest_recall": recalls, "quest_f1": f1,
        }
        async with lock:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row_out, ensure_ascii=False) + "\n")
            written += 1
            if written % 50 == 0:
                print(f"  {written}/{len(todo)}", flush=True)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(r, client) for r in todo))

    judged = _load(out_path)
    summary: dict[str, Any] = {"n": len(judged), "by_task": {}}
    for task in ("1doc", "2docs", "3docs"):
        part = [r for r in judged if r["subset"] == task]
        if not part:
            continue
        summary["by_task"][task] = {
            "n": len(part),
            "quest_recall": sum(r["quest_recall"] for r in part) / len(part),
            "quest_f1": sum(r["quest_f1"] for r in part) / len(part),
        }
    summary["overall"] = {
        "n": len(judged),
        "quest_recall": sum(r["quest_recall"] for r in judged) / (len(judged) or 1),
        "quest_f1": sum(r["quest_f1"] for r in judged) / (len(judged) or 1),
    }
    (ROOT / "questeval_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
