"""CRUD 答案侧 A/B：同一检索结果（wide+a05），对比默认生成 vs "全覆盖" prompt。

直接调 DashScope qwen-max（不改业务代码），用于判断"答案不完整"是否来自生成提示词。
输入：var/eval/crud/results_wide_a05_ans.jsonl（含 contexts）
输出：var/eval/crud/results_enh_prompt.jsonl（可直接送 crud_eval --stage judge）

运行：
    set -a; . ~/.ekbs/secrets.env; set +a
    .venv/bin/python eval/crud_answer_ab.py --concurrency 6
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
ROOT = REPO / "var" / "eval" / "crud"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen-max"

SYSTEM = (
    "你是企业知识库问答助手。请**只依据给定资料**回答问题，要求：\n"
    "1) 先把问题拆成它问到的每个要点，逐点回答，尽量覆盖资料中所有相关关键信息"
    "（数字、时间、主体、条款都要保留）；\n"
    "2) 用分点/短句列出，避免只给一个概括性结论；\n"
    "3) 每个事实后标注引用编号，如 [1][2]；\n"
    "4) 资料确实不足时才回复“未找到可靠依据”。"
)


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contexts(rows: list[str], limit: int = 10, per: int = 1200, total: int = 9000) -> str:
    parts, used = [], 0
    for i, text in enumerate(rows[:limit], 1):
        piece = str(text or "").strip()[:per]
        if not piece:
            continue
        block = f"[{i}] {piece}"
        if used + len(block) > total:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) or "（无资料）"


async def _gen(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{_contexts(row.get('contexts') or [])}\n\n问题：{row['query']}"},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    key = os.environ["DASHSCOPE_API_KEY"]
    last = ""
    for attempt in range(6):
        try:
            async with sem:
                resp = await client.post(BASE_URL, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=120)
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"].strip()
                return {"answer": text, "refused": False, "refusal_reason": ""}
            last = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if resp.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue
            break
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2 ** attempt)
    return {"answer": "生成服务暂时不可用，请稍后重试。", "refused": True, "refusal_reason": "llm_unavailable", "error": last}


async def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD 答案 prompt A/B")
    parser.add_argument("--results", default=str(ROOT / "results_wide_a05_ans.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "results_enh_prompt.jsonl"))
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("缺少 DASHSCOPE_API_KEY", file=sys.stderr)
        return 2
    rows = _load(Path(args.results))
    if args.limit:
        rows = rows[: args.limit]
    out_path = Path(args.out)
    done = {r["id"] for r in _load(out_path)} if out_path.exists() else set()
    todo = [r for r in rows if r["id"] not in done]
    print(f"待生成 {len(todo)} / {len(rows)}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    written = 0
    async with httpx.AsyncClient() as client:
        for start in range(0, len(todo), 20):
            batch = todo[start : start + 20]
            results = await asyncio.gather(*(_gen(client, sem, r) for r in batch))
            with out_path.open("a", encoding="utf-8") as fh:
                for row, verdict in zip(batch, results):
                    out = {**row, **verdict}
                    fh.write(json.dumps(out, ensure_ascii=False) + "\n")
            written += len(batch)
            print(f"  {written}/{len(todo)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
