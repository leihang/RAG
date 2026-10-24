"""动作1续：answer-then-verify（先答，再核验依据；核验不过才拒答）。

输入：
    var/eval/rgb/answers.jsonl       可答题（已有 answer + contexts）
    var/eval/rgb-neg/answers.jsonl   负例题（answer_eval.py --root var/eval/rgb-neg 生成）
判官：DeepSeek，检查候选回答里的关键事实（数字/日期/主体/条款）能否在上下文中逐字找到；
答非所问 / 资料不足 → supported=false（即应拒答）。

输出：var/eval/rgb/answer_verify.jsonl + answer_verify_summary.json
运行（DEEPSEEK_API_KEY 在 ~/.zshrc）：
    zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/answer_verify.py --concurrency 8'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
RGB = REPO / "var" / "eval" / "rgb"
NEG = REPO / "var" / "eval" / "rgb-neg"

SYSTEM = (
    "你是企业知识库问答的答案核验判官。给你问题、资料片段和候选回答，判断候选回答能否被资料支持。\n"
    "规则：\n"
    "1) 回答里的关键事实（数字、日期、主体、条款编号、结论）必须能在资料中逐字找到依据；\n"
    "2) 不要求措辞一致，但核心事实必须有对应原文；\n"
    "3) 答非所问、资料与问题无关、资料不足、或回答声称“未找到依据” → supported=false；\n"
    "4) 不要用资料之外的世界知识补全；\n"
    "5) 只输出 JSON：{\"supported\": true/false, \"unsupported\": [\"不支持的表述\"], \"reason\": \"一句话\"}。"
)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contexts_text(contexts: list[str], limit: int = 5, per: int = 900, total: int = 4500) -> str:
    parts: list[str] = []
    used = 0
    for i, text in enumerate(contexts[:limit], 1):
        piece = str(text or "").strip()[:per]
        if not piece:
            continue
        block = f"[资料{i}] {piece}"
        if used + len(block) > total:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) or "（无资料）"


async def _verify(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{_contexts_text(row.get('contexts') or [])}\n\n"
                    f"问题：{row['query']}\n\n候选回答：{row.get('answer') or ''}"
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"}
    last = ""
    for attempt in range(6):
        try:
            async with sem:
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    json=payload, headers=headers, timeout=90,
                )
            if resp.status_code == 200:
                data = json.loads(resp.json()["choices"][0]["message"]["content"])
                return {
                    "supported": bool(data.get("supported")),
                    "unsupported": [str(x)[:120] for x in (data.get("unsupported") or [])][:5],
                    "reason": str(data.get("reason") or "")[:200],
                }
            last = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if resp.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue
            break
        except (httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2 ** attempt)
    return {"supported": None, "unsupported": [], "reason": f"ERR {last}"}


def _summ(rows: list[dict]) -> dict:
    n = len(rows) or 1
    valid = [r for r in rows if r["supported"] is not None]
    refuse = sum(1 for r in valid if r["supported"] is False)
    base = sum(1 for r in rows if r.get("pipeline_refused"))
    combined = sum(1 for r in rows if r.get("pipeline_refused") or r["supported"] is False)
    return {
        "n": len(rows),
        "valid": len(valid),
        "verify_refuse_rate": refuse / (len(valid) or 1),
        "pipeline_refused_rate": base / n,
        "combined_refused_rate": combined / n,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="answer-then-verify 判官评测")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(RGB / "answer_verify.jsonl"))
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY（用 zsh -ic 运行）", file=sys.stderr)
        return 2

    positives = [r for r in _load(RGB / "answers.jsonl") if not r["holdout"]]
    negatives = _load(NEG / "answers.jsonl")
    if args.limit:
        positives = positives[: args.limit]
        negatives = negatives[: args.limit]
    print(f"可答 {len(positives)} / 负例 {len(negatives)}")

    cases: list[dict] = []
    for r in positives:
        cases.append({"kind": "positive", **{k: r.get(k) for k in ("id", "subset", "query", "answer", "contexts", "official_match", "refused")}})
    for r in negatives:
        cases.append({"kind": "negative", **{k: r.get(k) for k in ("id", "subset", "query", "answer", "contexts", "refused")}})

    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient() as client:
        verdicts = await asyncio.gather(
            *(_verify(client, sem, c, args.model) for c in cases)
        )
    rows = [{**case, **verdict, "pipeline_refused": bool(case.get("refused"))} for case, verdict in zip(cases, verdicts)]
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    pos = [r for r in rows if r["kind"] == "positive"]
    neg = [r for r in rows if r["kind"] == "negative"]
    summary = {
        "model": args.model,
        "positive": _summ(pos),
        "positive_correct": _summ([r for r in pos if r.get("official_match")]),
        "negative": _summ(neg),
        "by_subset_negative": {s: _summ([r for r in neg if r["subset"] == s]) for s in ("refine", "int", "fact")},
        "by_subset_positive": {s: _summ([r for r in pos if r["subset"] == s]) for s in ("refine", "int", "fact")},
    }
    (out.with_name("answer_verify_summary.json")).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
