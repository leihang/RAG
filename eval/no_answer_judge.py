"""回答侧「资料是否足够」判官（DeepSeek）—— 拒答能力改造的离线验证。

对每题只给 question + 检索到的 top-k contexts，让 DeepSeek 判断：
    {"sufficient": true|false, "reason": "..."}
* 可答题（answers.jsonl）：sufficient=false 会计为「误拒」；
* 负例题（rgb-neg/contexts.jsonl）：sufficient=false 会计为「拒答成功」。

用途：验证"在生成前/后加一道 no-answer 判官"能否把负例拒答 7.6% 提上去、
且不把可答题误拒。输出逐题决策 + 汇总，不修改业务代码。

运行（DEEPSEEK_API_KEY 在 ~/.zshrc）：
    zsh -ic 'cd "$PWD" && set -a; . ~/.ekbs/secrets.env; set +a; \
      var/eval/venv/bin/python eval/no_answer_judge.py --concurrency 8'
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

PROMPT_BASIC = (
    "你是企业知识库问答的“资料充分性”判官。只根据用户提供的资料判断："
    "这些资料是否足以回答问题。规则：\n"
    "1) 只要资料里能找到回答问题所需的关键事实/数字/条款，就判 sufficient=true；"
    "不要求资料包含完整答案全文，也不要求措辞一致；\n"
    "2) 如果问题问的主体、时间、数字或条款在资料里找不到，或资料与该问题无关，"
    "判 sufficient=false；\n"
    "3) 不要用资料之外的世界知识补全；\n"
    "4) 只输出 JSON：{\"sufficient\": true/false, \"reason\": \"一句话依据\"}。"
)

PROMPT_STRICT = (
    "你是企业知识库问答的严格判官。只依据用户提供的资料判断问题能否被**直接、完整**回答。\n"
    "规则：\n"
    "1) 问题所需的关键事实/数字/时间/主体/条款，必须能在资料中逐字找到依据；\n"
    "2) 只能从资料推断、或资料只覆盖问题的一部分，判 sufficient=false；\n"
    "3) 多跳问题（需要把两段资料合起来）只有在两段依据都能逐字找到时才判 true；\n"
    "4) 不要用资料之外的世界知识补全；\n"
    "5) 只输出 JSON：{\"sufficient\": true/false, \"evidence\": \"逐字引用依据（找不到则空）\", \"reason\": \"一句话\"}。"
)

SYSTEMS = {"basic": PROMPT_BASIC, "strict": PROMPT_STRICT}


def _load(path: Path) -> list[dict]:
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


async def _judge(client: httpx.AsyncClient, sem: asyncio.Semaphore, question: str, contexts: list[str], model: str, system: str = PROMPT_BASIC) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{_contexts_text(contexts)}\n\n问题：{question}"},
        ],
        "temperature": 0,
        "max_tokens": 120,
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
                text = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(text)
                return {"sufficient": bool(data.get("sufficient")), "reason": str(data.get("reason") or "")[:200]}
            last = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if resp.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue
            break
        except (httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2 ** attempt)
    return {"sufficient": None, "reason": f"ERR {last}"}


def _summ(rows: list[dict]) -> dict:
    n = len(rows) or 1
    valid = [r for r in rows if r["sufficient"] is not None]
    insuff = sum(1 for r in valid if r["sufficient"] is False)
    pipeline_refused = sum(1 for r in rows if r.get("pipeline_refused"))
    both_refused = sum(1 for r in rows if r.get("pipeline_refused") or r["sufficient"] is False)
    return {
        "n": len(rows),
        "valid": len(valid),
        "judge_refuse_rate": insuff / (len(valid) or 1),
        "pipeline_refused_rate": pipeline_refused / n,
        "combined_refused_rate": both_refused / n,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 资料充分性判官评测")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(RGB / "no_answer_judge.jsonl"))
    parser.add_argument("--prompt", default="basic", choices=["basic", "strict"])
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY（用 zsh -ic 运行）", file=sys.stderr)
        return 2

    positives = [r for r in _load(RGB / "answers.jsonl") if not r["holdout"]]
    negatives = _load(NEG / "contexts.jsonl")
    if args.limit:
        positives = positives[: args.limit]
        negatives = negatives[: args.limit]
    print(f"可答 {len(positives)} / 负例 {len(negatives)}")

    cases = []
    for r in positives:
        cases.append({"kind": "positive", **{k: r.get(k) for k in ("id", "subset", "query", "answers", "official_match", "refused")}, "contexts": r.get("contexts") or []})
    for r in negatives:
        cases.append({"kind": "negative", **{k: r.get(k) for k in ("id", "subset", "query", "refused", "top_score")}, "contexts": r.get("contexts") or []})

    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_judge(client, sem, c["query"], c["contexts"], args.model, SYSTEMS[args.prompt]) for c in cases)
        )
    rows = []
    for case, verdict in zip(cases, results):
        rows.append({**case, **verdict, "pipeline_refused": bool(case.get("refused"))})
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    pos = [r for r in rows if r["kind"] == "positive"]
    neg = [r for r in rows if r["kind"] == "negative"]
    correct_pos = [r for r in pos if r.get("official_match")]
    summary = {
        "model": args.model,
        "prompt": args.prompt,
        "positive": _summ(pos),
        "positive_correct_only": _summ(correct_pos),
        "negative": _summ(neg),
        "by_subset_positive": {
            s: _summ([r for r in pos if r["subset"] == s]) for s in ("refine", "int", "fact")
        },
        "by_subset_negative": {
            s: _summ([r for r in neg if r["subset"] == s]) for s in ("refine", "int", "fact")
        },
    }
    summary["negatives_currently_answered"] = {
        "n": sum(1 for r in neg if not r.get("pipeline_refused")),
        "judge_catch_rate": (
            sum(1 for r in neg if not r.get("pipeline_refused") and r["sufficient"] is False)
            / (sum(1 for r in neg if not r.get("pipeline_refused")) or 1)
        ),
    }
    out_summary = out.with_name("no_answer_judge_summary.json")
    out_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
