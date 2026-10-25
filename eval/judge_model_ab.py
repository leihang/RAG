"""拒答判官模型横向对比：DeepSeek / qwen-max / MiniMax（含思考模式）。

同一 prompt（claim 分解核验）、同一批题（RGB 可答 + 负例题抽样），比较：
- 可答误拒（positive false refusal）
- 负例拒答（negative catch）
- JSON 合法率、延迟、token 用量

输出：var/eval/judge_ab/<config>.jsonl + summary.json
运行（三个 key 都在 ~/.zshrc + secrets.env）：
    zsh -ic 'cd "$PWD" && set -a; . ~/.ekbs/secrets.env; set +a; \
      var/eval/venv/bin/python eval/judge_model_ab.py --pos 120 --neg 120'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
RGB = REPO / "var" / "eval" / "rgb"
NEG = REPO / "var" / "eval" / "rgb-neg"
OUT = REPO / "var" / "eval" / "judge_ab"

SYSTEM = (
    "你是企业知识库问答的答案核验判官。给你问题、资料片段和候选回答，请做 claim 分解核验。\n"
    "步骤：\n"
    "1) 把候选回答拆成若干条事实性 claim（数字/日期/主体/条款/结论各算一条）；\n"
    "2) 对每条 claim，从资料中找出能逐字支持它的原文；找不到就标 supported=false；\n"
    "3) 只要有一条关键 claim 没有原文依据，整体 supported=false；\n"
    "4) 答非所问/资料不足同样 supported=false；\n"
    "5) 只输出 JSON：{\"supported\": true/false, \"claims\": [{\"claim\": \"...\", \"supported\": true/false, \"quote\": \"原文\"}], \"reason\": \"一句话\"}。"
)

CONFIGS = [
    {"name": "deepseek-chat", "base": "https://api.deepseek.com/v1", "model": "deepseek-chat", "key": "DEEPSEEK_API_KEY"},
    {"name": "deepseek-reasoner", "base": "https://api.deepseek.com/v1", "model": "deepseek-reasoner", "key": "DEEPSEEK_API_KEY", "max_tokens": 4000},
    {"name": "qwen-max", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-max", "key": "DASHSCOPE_API_KEY"},
    {"name": "minimax-m3-off", "concurrency": 1, "delay": 2.0, "base": "https://api.minimaxi.com/v1", "model": "MiniMax-M3", "key": "MINIMAX_API_KEY", "thinking": {"type": "disabled"}, "max_tokens": 1500},
    {"name": "minimax-m3-adaptive", "concurrency": 1, "delay": 2.0, "base": "https://api.minimaxi.com/v1", "model": "MiniMax-M3", "key": "MINIMAX_API_KEY", "thinking": {"type": "adaptive"}, "max_tokens": 8000},
    {"name": "minimax-m31flash-adaptive", "concurrency": 1, "delay": 2.0, "base": "https://api.minimaxi.com/v1", "model": "MiniMax-M3.1-Flash", "key": "MINIMAX_API_KEY", "thinking": {"type": "adaptive"}, "max_tokens": 4000},
    {"name": "minimax-m27-off", "concurrency": 2, "base": "https://api.minimaxi.com/v1", "model": "MiniMax-M2.7", "key": "MINIMAX_API_KEY", "thinking": {"type": "disabled"}, "max_tokens": 1500},
]


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contexts_text(contexts: list[str], limit: int = 5, per: int = 900, total: int = 4500) -> str:
    parts, used = [], 0
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


def _parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    # 去掉 <think>...</think> 推理段和 markdown 围栏
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    # 取最后一个花括号块（推理文本可能在前面）
    blocks = re.findall(r"\{.*?\}", text, re.S)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    # 最后兜底：直接抓 supported 布尔
    m = re.search(r'"?supported"?\s*[:：]\s*(true|false)', text, re.I)
    if m:
        return {"supported": m.group(1).lower() == "true"}
    return None


async def run_config(cfg: dict, cases: list[dict], concurrency: int, timeout: int) -> list[dict]:
    key = os.environ.get(cfg["key"])
    if not key:
        print(f"[{cfg['name']}] 缺少 {cfg['key']}", file=sys.stderr)
        return []
    out_path = OUT / f"{cfg['name']}.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)
    rows = _load(out_path) if out_path.exists() else []
    done = {r["id"] for r in rows}
    todo = [c for c in cases if c["id"] not in done]
    sem = asyncio.Semaphore(int(cfg.get("concurrency", concurrency)))
    lock = asyncio.Lock()
    done_n = 0

    async def one(case: dict, client: httpx.AsyncClient) -> None:
        nonlocal done_n
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"{_contexts_text(case['contexts'])}\n\n问题：{case['query']}\n\n候选回答：{case.get('answer') or ''}"},
            ],
            "temperature": 0,
            "max_tokens": cfg.get("max_tokens", 800),
        }
        if "thinking" in cfg:
            payload["thinking"] = cfg["thinking"]
        headers = {"Authorization": f"Bearer {key}"}
        result = {"id": case["id"], "kind": case["kind"], "subset": case["subset"], "supported": None,
                  "parse_ok": False, "latency": None, "total_tokens": None, "error": ""}
        for attempt in range(4):
            try:
                async with sem:
                    t0 = time.time()
                    resp = await client.post(cfg["base"] + "/chat/completions", json=payload, headers=headers, timeout=timeout)
                    result["latency"] = round(time.time() - t0, 2)
                if resp.status_code == 200:
                    body = resp.json()
                    msg = body["choices"][0]["message"]
                    data = _parse_json(msg.get("content") or "")
                    result["total_tokens"] = (body.get("usage") or {}).get("total_tokens")
                    if data is not None and isinstance(data.get("supported"), bool):
                        result["supported"] = data["supported"]
                        result["parse_ok"] = True
                    else:
                        result["error"] = "bad_json"
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(3 + 2 ** attempt)
                    continue
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:160]}"
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(2 ** attempt)
        if cfg.get("delay"):
            await asyncio.sleep(float(cfg["delay"]))
        if result["supported"] is None and not result["error"]:
            result["error"] = "retry_exhausted"
        async with lock:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            done_n += 1

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(c, client) for c in todo))
    return _load(out_path)


def _summary(rows: list[dict]) -> dict:
    pos = [r for r in rows if r["kind"] == "positive" and r["supported"] is not None]
    neg = [r for r in rows if r["kind"] == "negative" and r["supported"] is not None]
    lat = sorted(r["latency"] for r in rows if r["latency"])
    tok = [r["total_tokens"] for r in rows if r["total_tokens"]]

    def pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        i = min(len(values) - 1, max(0, round(p * (len(values) - 1))))
        return values[i]

    return {
        "n": len(rows),
        "valid": len(pos) + len(neg),
        "parse_ok_rate": sum(1 for r in rows if r["parse_ok"]) / (len(rows) or 1),
        "positive_false_refusal": sum(1 for r in pos if r["supported"] is False) / (len(pos) or 1),
        "negative_catch": sum(1 for r in neg if r["supported"] is False) / (len(neg) or 1),
        "latency_mean": round(sum(lat) / len(lat), 2) if lat else None,
        "latency_p50": pct(lat, 0.5),
        "latency_p90": pct(lat, 0.9),
        "tokens_mean": round(sum(tok) / len(tok), 1) if tok else None,
        "errors": sum(1 for r in rows if r["error"]),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="拒答判官模型横向对比")
    parser.add_argument("--pos", type=int, default=120)
    parser.add_argument("--neg", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--configs", default="")
    parser.add_argument("--only", default="", help="只跑指定配置（逗号分隔）")
    parser.add_argument("--subset-from", default="", help="从某配置的 jsonl 取前 N 条正/负例做子集")
    parser.add_argument("--subset-n", type=int, default=30)
    args = parser.parse_args()

    positives = [r for r in _load(RGB / "answers.jsonl") if not r["holdout"]]
    negatives = _load(NEG / "answers.jsonl")
    rng = random.Random(42)
    pos_sample = rng.sample(positives, min(args.pos, len(positives)))
    neg_sample = rng.sample(negatives, min(args.neg, len(negatives)))
    cases: list[dict] = []
    for r in pos_sample:
        cases.append({"id": r["id"], "kind": "positive", "subset": r["subset"], "query": r["query"],
                      "answer": r.get("answer") or "", "contexts": r.get("contexts") or []})
    for r in neg_sample:
        cases.append({"id": r["id"], "kind": "negative", "subset": r["subset"], "query": r["query"],
                      "answer": r.get("answer") or "", "contexts": r.get("contexts") or []})
    print(f"样板：可答 {len(pos_sample)} / 负例 {len(neg_sample)}；配置 {len(CONFIGS)} 个")

    if args.subset_from:
        ref = _load(OUT / f"{args.subset_from}.jsonl")
        keep_ids = {r["id"] for r in ref if r["kind"] == "positive"} | {r["id"] for r in ref if r["kind"] == "negative"}
        pos_ids = [r["id"] for r in ref if r["kind"] == "positive"][: args.subset_n]
        neg_ids = [r["id"] for r in ref if r["kind"] == "negative"][: args.subset_n]
        keep_ids = set(pos_ids) | set(neg_ids)
        cases = [c for c in cases if c["id"] in keep_ids]
        print(f"子集：可答 {len(pos_ids)} / 负例 {len(neg_ids)}")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cfgs = [c for c in CONFIGS if not only or c["name"] in only]
    results = await asyncio.gather(*(run_config(c, cases, args.concurrency, args.timeout) for c in cfgs))
    summary = {c["name"]: _summary(rows) for c, rows in zip(cfgs, results)}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
