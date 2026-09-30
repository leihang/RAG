"""云端代码路径的**端到端**验证（respx 模拟百炼三端点，不出网）。

为什么值得单独写一组：provider 的单元契约（`tests/test_dashscope_provider.py`）只能证明
「请求/响应对得上」，证明不了「整条链换到云端还能跑」。这里把 respx 的响应**按真实契约**
构造出来（OpenAI 兼容 embeddings / 原生 rerank / 兼容 chat），然后跑

    入库（云端嵌入）→ 分片 → 检索（FAISS+FTS5+RRF）→ 云端 rerank → C5 → 生成 → 引用校验

因此拿到 key 后要做的只剩「把 provider 从 mock 改成 dashscope」，链路本身已被验证过。

模拟向量用的是与离线 mock 相同的确定性哈希词袋，所以召回质量与离线用例一致，
断言才有意义（不是「随便返回点什么都能过」）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.provider.dashscope import COMPATIBLE_PATH, RERANK_PATH
from ekbs.provider.mock import MockEmbeddingProvider, _tokenize
from ekbs.runtime import Runtime
from ekbs.tools.seed_demo import seed
from tests.conftest import DEV_CONFIG, user_id_of

BASE = "https://dashscope.aliyuncs.com"
EMBED_URL = f"{BASE}{COMPATIBLE_PATH}/embeddings"
RERANK_URL = f"{BASE}{RERANK_PATH}"
CHAT_URL = f"{BASE}{COMPATIBLE_PATH}/chat/completions"


def _cloud_settings(tmp_path):
    settings = load_settings(
        DEV_CONFIG, data_dir=tmp_path / "var", db_path=tmp_path / "var" / "index" / "ekbs.db"
    )
    # 把三个能力都切到云端代码路径（key 由环境变量注入）
    settings.models.embed.provider = "dashscope"
    settings.models.embed.model = "text-embedding-v4"
    settings.models.embed.dim = 1024
    settings.models.embed.batch_size = 10
    settings.retrieval.rerank.provider = "dashscope"
    settings.retrieval.rerank.model = "gte-rerank-v2"
    settings.models.llm.provider = "dashscope"
    settings.models.llm.model = "qwen-max"
    return settings


def _embed_handler(embedder: MockEmbeddingProvider):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        # 契约校验：单请求 ≤10 条（v4 上限）
        assert len(inputs) <= 10, f"单请求 {len(inputs)} 条超过 v4 上限"
        dim = int(body["dimensions"])
        rows = [
            {"embedding": embedder._embed_one(text), "index": i, "object": "embedding"}
            for i, text in enumerate(inputs)
        ]
        for row in rows:
            assert len(row["embedding"]) == dim
        return httpx.Response(
            200, json={"data": rows, "model": body["model"], "object": "list",
                       "usage": {"total_tokens": sum(len(t) for t in inputs)}}
        )

    return handler


def _rerank_handler() -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = body["input"]["query"]
        documents = body["input"]["documents"]
        top_n = int(body["parameters"].get("top_n") or len(documents))
        query_tokens = set(_tokenize(query))
        scored = []
        for index, text in enumerate(documents):
            doc_tokens = set(_tokenize(text))
            coverage = (len(query_tokens & doc_tokens) / len(query_tokens)) if query_tokens else 0.0
            scored.append((index, round(coverage, 6)))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        results = [
            {"index": index, "relevance_score": score} for index, score in scored[:top_n]
        ]
        return httpx.Response(
            200, json={"output": {"results": results}, "usage": {"total_tokens": 79},
                       "request_id": "req-test"}
        )

    return handler


@respx.mock
async def test_full_chain_runs_on_cloud_code_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    embedder = MockEmbeddingProvider(dim=1024)
    respx.post(EMBED_URL).mock(side_effect=_embed_handler(embedder))
    respx.post(RERANK_URL).mock(side_effect=_rerank_handler())
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chat-1",
                "model": "qwen-max",
                "choices": [
                    {"message": {"role": "assistant", "content": "员工年假按司龄为 5/10/15 天 [1]"}}
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 20},
            },
        )
    )

    settings = _cloud_settings(tmp_path)
    Database(settings).apply_schema()
    runtime = Runtime.create(settings, service="test")
    try:
        assert type(runtime.providers.embed).__name__ == "DashScopeEmbeddingProvider"
        assert type(runtime.providers.rerank).__name__ == "DashScopeRerankProvider"
        assert type(runtime.providers.llm).__name__ == "OpenAICompatibleLLMProvider"

        # ① 入库走云端嵌入（respx 拦截）
        summary = await seed(settings, runtime=runtime)
        assert len(summary["documents"]) == 3
        assert all(doc["vector_count"] > 0 for doc in summary["documents"])
        assert runtime.shards.count_active() == 3

        # ② 检索走云端嵌入 + 云端 rerank
        alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
        retrieval = await runtime.pipeline.retrieve("年假有多少天", alice)
        assert retrieval.refused is False, retrieval.refusal_reason
        assert any(chunk.doc_title == "年假管理制度" for chunk in retrieval.chunks)
        assert retrieval.top_score and retrieval.top_score > 0.3

        # ③ 生成走云端 chat，并完成引用后置校验
        answer = await runtime.pipeline.generate("年假有多少天", retrieval, alice)
        assert answer.refused is False
        assert answer.model == "qwen-max"
        assert answer.citations, "引用必须落库"
        assert answer.citations[0].acl_result == "pass"
        assert answer.citations[0].page_check in {"pass", "out_of_range"}
        assert answer.prompt_tokens == 200 and answer.completion_tokens == 20

        # ④ 红线在云端路径上同样成立：HR 用户召不回财务 L3
        finance = await runtime.pipeline.retrieve("财务专用章由谁保管", alice)
        assert all("财务" not in chunk.doc_title for chunk in finance.chunks)
    finally:
        await runtime.aclose()


@respx.mock
async def test_cloud_retry_then_success_is_transparent(tmp_path, monkeypatch) -> None:
    """云端抖动（429 → 200）对上层透明：检索仍然成功，用户无感（00 §2.3）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    embedder = MockEmbeddingProvider(dim=1024)
    ok_handler = _embed_handler(embedder)
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, json={"code": "Throttling.BurstRate", "message": "increase too quickly"}
            )
        return ok_handler(request)

    respx.post(EMBED_URL).mock(side_effect=flaky)
    respx.post(RERANK_URL).mock(side_effect=_rerank_handler())
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"model": "qwen-max", "choices": [{"message": {"content": "ok [1]"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )
    )

    settings = _cloud_settings(tmp_path)
    settings.models.embed.max_retries = 3
    Database(settings).apply_schema()
    runtime = Runtime.create(settings, service="test")
    try:
        # 首次入库会先吃一个 429，再重试成功
        settings_dict = runtime.providers.embed.__dict__
        runtime.providers.embed.retry_base_delay = 0.0
        assert settings_dict["max_retries"] >= 1
        summary = await seed(settings, runtime=runtime)
        assert all(doc["vector_count"] > 0 for doc in summary["documents"])
        assert calls["n"] >= 2, "应发生过一次重试"
    finally:
        await runtime.aclose()


async def test_missing_api_key_fails_loudly(monkeypatch) -> None:
    """没配 key 时必须在构造 provider 阶段就报错，而不是等到第一次问答（04 §2.6）。"""
    from ekbs.config import Secrets
    from ekbs.errors import ConfigError
    from ekbs.provider.registry import build_providers

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    settings = load_settings(DEV_CONFIG)
    settings.models.embed.provider = "dashscope"
    with pytest.raises(ConfigError) as excinfo:
        build_providers(settings, Secrets())
    assert "DASHSCOPE_API_KEY" in str(excinfo.value)
