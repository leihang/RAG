"""向量模型评测：C-MTEB DuRetrieval（中文检索，nDCG@10 / Recall@100）。

数据（mteb 版，含 qrels）放在 var/eval/datasets/mteb/{corpus,queries,data}/
    https://hf-mirror.com/datasets/mteb/DuRetrieval
    corpus: 100,001 docs / queries: 2,000 / qrels: 9,839 对

用与业务相同的嵌入口径（DashScope `qwen3.7-text-embedding-flash@1024 + asym`）：
文档侧 text_type=document，查询侧 text_type=query。

运行（在 eval venv 里，避免污染主 venv）：
    set -a; . ~/.ekbs/secrets.env; set +a
    var/eval/venv/bin/python eval/embedding_eval.py

注意：
* 100k 文档按 token 预算限速（默认 60 万 token/分钟），并发 6；
* 按 3200 条一个分片缓存到 var/eval/mteb/cache/，中断后重跑自动续跑；
* 首次约 1800 万 token、费用约 2–3 元、耗时约 20–30 分钟。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "var" / "eval" / "datasets" / "mteb"
CACHE = REPO / "var" / "eval" / "mteb" / "cache"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
MODEL = "qwen3.7-text-embedding-flash"
DIM = 1024
CHUNK = 3200


def _read_parquet(path: Path) -> list[dict]:
    table = pq.read_table(path)
    return [
        {name: table.column(j)[i].as_py() for j, name in enumerate(table.schema.names)}
        for i in range(table.num_rows)
    ]


class TokenLimiter:
    """按 token/分钟预算限速（DashScope embedding 的 TPM 是 1,000,000/分钟）。"""

    def __init__(self, tokens_per_min: int) -> None:
        self.tokens_per_min = max(1000, tokens_per_min)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int) -> None:
        wait_for = 0.0
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + tokens / self.tokens_per_min * 60.0
            wait_for = start - now
        if wait_for > 0:
            await asyncio.sleep(wait_for)


def _est_tokens(text: str) -> int:
    # 中文约 1.5 字符/token；英文更省。宁可高估（高估只会更慢，不会 429）
    return max(1, int(len(text) / 1.2))


async def _embed_batch(
    client: httpx.AsyncClient,
    texts: list[str],
    text_type: str,
    sem: asyncio.Semaphore,
    limiter: TokenLimiter,
    retries: int = 8,
) -> list[list[float]]:
    payload = {"model": MODEL, "input": texts, "dimensions": DIM, "text_type": text_type}
    headers = {"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"}
    await limiter.acquire(sum(_est_tokens(t) for t in texts))
    last_error = ""
    for attempt in range(retries):
        try:
            async with sem:
                resp = await client.post(BASE_URL, json=payload, headers=headers, timeout=90)
            if resp.status_code == 200:
                data = resp.json()["data"]
                data.sort(key=lambda x: x["index"])
                return [item["embedding"] for item in data]
            last_error = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
                await asyncio.sleep(min(60.0, delay) + 0.2 * attempt)
                continue
            raise RuntimeError(last_error)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(min(60.0, 2 ** attempt))
    raise RuntimeError(f"embedding 重试耗尽：{last_error}")


async def encode_cached(
    texts: list[str],
    text_type: str,
    *,
    batch: int,
    concurrency: int,
    tokens_per_min: int,
    rebuild: bool,
) -> np.ndarray:
    cache_dir = CACHE / text_type
    cache_dir.mkdir(parents=True, exist_ok=True)
    limiter = TokenLimiter(tokens_per_min)
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    started = time.perf_counter()
    done = 0

    async with httpx.AsyncClient(limits=limits) as client:
        for chunk_start in range(0, len(texts), CHUNK):
            chunk = texts[chunk_start : chunk_start + CHUNK]
            part = cache_dir / f"{chunk_start:06d}.npy"
            if part.exists() and not rebuild:
                existing = np.load(part)
                if existing.shape == (len(chunk), DIM):
                    done += len(chunk)
                    print(f"  复用 {part.name}（{done}/{len(texts)}）", flush=True)
                    continue
            arr = np.zeros((len(chunk), DIM), dtype=np.float32)

            async def one(offset: int) -> None:
                vectors = await _embed_batch(
                    client, chunk[offset : offset + batch], text_type, sem, limiter
                )
                arr[offset : offset + len(vectors)] = np.asarray(vectors, dtype=np.float32)

            await asyncio.gather(*(one(i) for i in range(0, len(chunk), batch)))
            np.save(part, arr)
            done += len(chunk)
            rate = done / max(1e-9, time.perf_counter() - started)
            print(f"  {done}/{len(texts)}  {rate:.0f} 条/s", flush=True)

    return np.concatenate([np.load(cache_dir / f) for f in sorted(p.name for p in cache_dir.glob("*.npy"))])


def _metrics(qrels: dict[str, dict[str, int]], ranked: dict[str, list[str]], k_recall: int = 100) -> dict:
    ndcg10: list[float] = []
    mrr10: list[float] = []
    recall100: list[float] = []
    for qid, rels in qrels.items():
        docs = ranked.get(qid) or []
        dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(docs[:10], 1) if rels.get(d, 0) > 0)
        ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rels), 10) + 1))
        ndcg10.append(dcg / ideal if ideal else 0.0)
        first = next((i for i, d in enumerate(docs, 1) if rels.get(d, 0) > 0), None)
        mrr10.append(1.0 / first if first else 0.0)
        hit = sum(1 for d in docs[:k_recall] if rels.get(d, 0) > 0)
        recall100.append(hit / len(rels) if rels else 0.0)
    n = len(ndcg10) or 1
    return {
        "queries": len(ndcg10),
        "ndcg_at_10": sum(ndcg10) / n,
        "mrr_at_10": sum(mrr10) / n,
        "recall_at_100": sum(recall100) / n,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="DuRetrieval 向量模型评测")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--tokens-per-min", type=int, default=600_000)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--topk", type=int, default=100)
    parser.add_argument("--max-docs", type=int, default=0, help="冒烟用：只编码前 N 篇（指标无意义）")
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("缺少 DASHSCOPE_API_KEY（先 source ~/.ekbs/secrets.env）", flush=True)
        return 2

    corpus = _read_parquet(DATA / "corpus" / "dev-00000-of-00001.parquet")
    queries = _read_parquet(DATA / "queries" / "dev-00000-of-00001.parquet")
    qrels_rows = _read_parquet(DATA / "data" / "dev-00000-of-00001.parquet")
    if args.max_docs:
        corpus = corpus[: args.max_docs]
        queries = queries[: args.max_docs]
    print(f"corpus={len(corpus)} queries={len(queries)} qrels={len(qrels_rows)}", flush=True)
    qrels: dict[str, dict[str, int]] = {}
    for row in qrels_rows:
        qrels.setdefault(str(row["query-id"]), {})[str(row["corpus-id"])] = int(row["score"])

    corpus_vecs = await encode_cached(
        [str(d["text"] or "") for d in corpus], "document",
        batch=args.batch, concurrency=args.concurrency,
        tokens_per_min=args.tokens_per_min, rebuild=args.rebuild,
    )
    query_vecs = await encode_cached(
        [str(q["text"] or "") for q in queries], "query",
        batch=args.batch, concurrency=args.concurrency,
        tokens_per_min=args.tokens_per_min, rebuild=args.rebuild,
    )

    corpus_vecs = np.asarray(corpus_vecs, dtype=np.float32)
    query_vecs = np.asarray(query_vecs, dtype=np.float32)
    corpus_vecs /= np.linalg.norm(corpus_vecs, axis=1, keepdims=True) + 1e-12
    query_vecs /= np.linalg.norm(query_vecs, axis=1, keepdims=True) + 1e-12
    corpus_ids = [str(d["_id"]) for d in corpus]

    ranked: dict[str, list[str]] = {}
    for start in range(0, len(queries), 64):
        chunk = query_vecs[start : start + 64]
        order = np.argsort(-(chunk @ corpus_vecs.T), axis=1)[:, : args.topk]
        for row, q in enumerate(queries[start : start + 64]):
            ranked[str(q["_id"])] = [corpus_ids[i] for i in order[row]]

    summary = _metrics(qrels, ranked, k_recall=args.topk)
    summary.update({"model": MODEL, "dim": DIM, "asym": True, "corpus": len(corpus), "topk": args.topk})
    out = REPO / "var" / "eval" / "mteb" / "report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
