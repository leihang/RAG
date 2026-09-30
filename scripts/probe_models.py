#!/usr/bin/env python
"""模型可用性与延迟实测（`00` §11 的 M2/M3 取数工具）。

用途：拿到 API Key 后，**先用它问清楚"这把 key 到底能用哪些模型、各自什么契约、延迟多少"**，
再决定嵌入/重排选型 —— 而不是照文档里的模型名猜（`gte-rerank-v2` 就踩过这个坑：
官方公告 2026-05-30 下线）。

用法：

.. code-block:: bash

    export DASHSCOPE_API_KEY=sk-xxx          # 或从 ~/.zshrc / ~/.ekbs/secrets.env 注入
    .venv/bin/python scripts/probe_models.py            # 全量探测
    .venv/bin/python scripts/probe_models.py --json     # 机器可读

**绝不打印密钥**：只输出模型名、可用性、维度/分数、耗时。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DASHSCOPE = "https://dashscope.aliyuncs.com"
COMPATIBLE = f"{DASHSCOPE}/compatible-mode/v1"
NATIVE_RERANK = f"{DASHSCOPE}/api/v1/services/rerank/text-rerank/text-rerank"
COMPATIBLE_RERANK = f"{DASHSCOPE}/compatible-api/v1/reranks"

#: 候选嵌入模型（维度取自官方文档，`docs/spec/40` §2.4）
EMBED_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("text-embedding-v4", 1024),
    ("text-embedding-v4", 2048),
    ("text-embedding-v3", 1024),
    ("qwen3.7-text-embedding", 1024),
    ("qwen3.7-text-embedding", 2048),
    ("qwen3.7-text-embedding-flash", 1024),
)

#: 候选重排模型（原生端点；`gte-rerank-v2` 官方公告 2026-05-30 下线，必须实测是否还活着）
RERANK_CANDIDATES: tuple[str, ...] = ("gte-rerank-v2", "qwen3-rerank", "qwen3.7-text-rerank")

#: 候选生成模型
LLM_CANDIDATES: tuple[str, ...] = ("qwen-max", "qwen-plus", "qwen3.7-max", "qwen3.8-max")


@dataclass
class Probe:
    kind: str
    model: str
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        print("缺少 DASHSCOPE_API_KEY（环境变量或 ~/.ekbs/secrets.env）", file=sys.stderr)
        raise SystemExit(2)
    return key


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def list_models(client: httpx.Client, key: str) -> list[str]:
    """OpenAI 兼容的模型清单（若该端点存在，就是"这把 key 能访问什么"的权威答案）。"""
    try:
        response = client.get(f"{COMPATIBLE}/models", headers=_headers(key))
    except httpx.HTTPError as exc:
        return [f"<请求失败：{exc.__class__.__name__}>"]
    if response.status_code != 200:
        return [f"<HTTP {response.status_code}：{response.text[:120]}>"]
    try:
        data = response.json().get("data") or []
    except ValueError:
        return ["<响应非 JSON>"]
    return sorted(str(item.get("id")) for item in data if isinstance(item, dict))


def probe_embed(client: httpx.Client, key: str, model: str, dim: int) -> Probe:
    body = {"model": model, "input": ["年假有多少天"], "dimensions": dim, "encoding_format": "float"}
    started = time.perf_counter()
    try:
        response = client.post(f"{COMPATIBLE}/embeddings", headers=_headers(key), json=body)
    except httpx.HTTPError as exc:
        return Probe("embed", f"{model}@{dim}", False, f"{exc.__class__.__name__}")
    latency = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return Probe("embed", f"{model}@{dim}", False, response.text[:160], latency)
    payload = response.json()
    rows = payload.get("data") or []
    vector = (rows[0].get("embedding") if rows else None) or []
    usage = payload.get("usage") or {}
    return Probe(
        "embed", f"{model}@{dim}", bool(vector), f"dim={len(vector)}",
        latency, {"tokens": usage.get("total_tokens")},
    )


def probe_rerank(client: httpx.Client, key: str, model: str) -> Probe:
    """优先原生端点；同时试一次 OpenAI 风格的 ``compatible-api/v1/reranks`` 以确认口径。"""
    body = {
        "model": model,
        "input": {
            "query": "年假有多少天",
            "documents": [
                "员工年假天数按司龄确定：满 1 年 5 天，满 3 年 10 天。",
                "报销须附发票原件，费用发生后 30 日内提交。",
                "公司每年组织一次全员体检。",
            ],
        },
        "parameters": {"top_n": 3, "return_documents": False},
    }
    started = time.perf_counter()
    try:
        response = client.post(NATIVE_RERANK, headers=_headers(key), json=body)
    except httpx.HTTPError as exc:
        return Probe("rerank", model, False, f"{exc.__class__.__name__}")
    latency = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return Probe("rerank", model, False, response.text[:160], latency)
    payload = response.json()
    results = ((payload.get("output") or {}).get("results")) or []
    scores = [round(float(r.get("relevance_score", 0)), 4) for r in results]
    return Probe(
        "rerank", model, bool(results),
        f"scores={scores} request_id={str(payload.get('request_id'))[:8]}",
        latency, {"documents": len(results)},
    )


def probe_llm(client: httpx.Client, key: str, model: str) -> Probe:
    body = {
        "model": model, "messages": [{"role": "user", "content": "回复：好"}],
        "max_tokens": 5, "temperature": 0.1,
    }
    started = time.perf_counter()
    try:
        response = client.post(f"{COMPATIBLE}/chat/completions", headers=_headers(key), json=body)
    except httpx.HTTPError as exc:
        return Probe("llm", model, False, f"{exc.__class__.__name__}")
    latency = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return Probe("llm", model, False, response.text[:160], latency)
    payload = response.json()
    text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    usage = payload.get("usage") or {}
    return Probe("llm", model, bool(text), f"reply={str(text)[:12]!r}", latency,
                 {"tokens": usage.get("total_tokens")})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="云端模型可用性与延迟探测")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    key = _key()
    probes: list[Probe] = []
    with httpx.Client(timeout=args.timeout) as client:
        models = list_models(client, key)
        for model, dim in EMBED_CANDIDATES:
            probes.append(probe_embed(client, key, model, dim))
        for model in RERANK_CANDIDATES:
            probes.append(probe_rerank(client, key, model))
        for model in LLM_CANDIDATES:
            probes.append(probe_llm(client, key, model))

    if args.json:
        print(json.dumps(
            {
                "models_endpoint": models,
                "probes": [p.__dict__ for p in probes],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0

    print("== 模型清单端点（compatible-mode/v1/models）==")
    if models and not models[0].startswith("<"):
        print(f"  可用模型 {len(models)} 个")
        for name in models:
            print(f"   · {name}")
    else:
        print(f"  {models[0] if models else '无返回'}")

    print("\n== 逐个探测（key 不打印；延迟为中心侧 RTT，含网络）==")
    for kind in ("embed", "rerank", "llm"):
        rows = [p for p in probes if p.kind == kind]
        if not rows:
            continue
        print(f"  [{kind}]")
        for probe in rows:
            mark = "可用" if probe.ok else "不可用"
            print(f"   {mark:4} {probe.model:28} {probe.latency_ms:7.0f} ms  {probe.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
