"""云端 provider 的**离线契约测试**（用 respx 拦截 httpx，不出网、不花钱）。

这些用例锁定的是 `docs/spec/40-cloud-models.md` 里**核实过的真实契约**，
因此它们的价值是：key 到位后第一次真实调用就不会踩这些坑 ——

* 嵌入走 OpenAI 兼容 + **复数** ``dimensions``，且单请求 ≤10 条；
* ``data[i].index`` 可能乱序，必须按 index 还原；
* 维度不符 / 空向量必须当场报错（兼容模式 ``output_type=sparse`` 会 200 + 空向量）；
* rerank **只能**走原生 HTTP，响应在 ``output.results[]``，错误体在**顶层** ``code``；
* 429 要退避重试，401/欠费/内容审核**不许**重试；
* 主模型失败切 fallback，且流式在主模型上解析 SSE。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ekbs.errors import EmbeddingUnavailable, ModelUnavailable, RerankUnavailable
from ekbs.provider.base import RerankDoc, model_ref
from ekbs.provider.dashscope import (
    COMPATIBLE_PATH,
    DEEPSEEK_COMPATIBLE_PATH,
    EMBED_MAX_BATCH,
    RERANK_PATH,
    DashScopeEmbeddingProvider,
    DashScopeRerankProvider,
    OpenAICompatibleLLMProvider,
    chat_compat_path,
)

BASE = "https://dashscope.aliyuncs.com"
EMBED_URL = f"{BASE}{COMPATIBLE_PATH}/embeddings"
RERANK_URL = f"{BASE}{RERANK_PATH}"
CHAT_URL = f"{BASE}{COMPATIBLE_PATH}/chat/completions"


def embed_provider(dim: int = 1024, **kwargs) -> DashScopeEmbeddingProvider:
    return DashScopeEmbeddingProvider(
        ref=model_ref("dashscope", "text-embedding-v4", dim=dim),
        api_key="sk-test",
        retry_base_delay=0.0,  # 测试里不退避等待
        **kwargs,
    )


@respx.mock
async def test_batch_cap_is_per_model_not_global() -> None:
    """**换模型后上限要跟着换**：原实现把 10 硬编码在所有模型上。

    后果不是报错而是静默降速 —— 配置写 `batch_size: 20` 被夹回 10，
    还打出一条指名 `text-embedding-v4` 的误导性警告，入库吞吐白丢一半。
    未列入 `EMBED_MAX_BATCH_BY_MODEL` 的模型必须回退保守值。
    """
    from ekbs.provider.dashscope import EMBED_MAX_BATCH, embed_max_batch

    assert embed_max_batch("qwen3.7-text-embedding-flash") == 20
    assert embed_max_batch("text-embedding-v4") == EMBED_MAX_BATCH
    assert embed_max_batch("某个没见过的模型") == EMBED_MAX_BATCH

    flash = DashScopeEmbeddingProvider(
        ref=model_ref("dashscope", "qwen3.7-text-embedding-flash", dim=1024),
        api_key="sk-test",
        batch_size=20,
        retry_base_delay=0.0,
    )
    assert flash.batch_size == 20, "flash 的 20 条上限不能被夹成 10"

    route = respx.post(EMBED_URL).mock(
        side_effect=lambda request: httpx.Response(
            200, json=_embedding_body(len(__import__("json").loads(request.content)["input"]))
        )
    )
    assert len(await flash.embed([f"文本{i}" for i in range(12)])) == 12
    assert route.call_count == 1, "12 条应当一次发完（若上限仍是 10 就会拆两次）"


def rerank_provider(**kwargs) -> DashScopeRerankProvider:
    return DashScopeRerankProvider(
        ref=model_ref("dashscope", "gte-rerank-v2"),
        api_key="sk-test",
        retry_base_delay=0.0,
        **kwargs,
    )


def llm_provider(**kwargs) -> OpenAICompatibleLLMProvider:
    return OpenAICompatibleLLMProvider(
        ref=model_ref("dashscope", "qwen-max"),
        api_key="sk-test",
        retry_base_delay=0.0,
        **kwargs,
    )


def _embedding_body(count: int, dim: int = 1024, *, reverse: bool = False) -> dict:
    rows = [
        {"embedding": [0.01 * (i + 1)] * dim, "index": i, "object": "embedding"}
        for i in range(count)
    ]
    if reverse:
        rows = list(reversed(rows))
    return {"data": rows, "model": "text-embedding-v4", "object": "list", "usage": {"total_tokens": 23}}


# ---------------------------------------------------------------------------
# 嵌入
# ---------------------------------------------------------------------------


@respx.mock
async def test_embed_success_and_request_shape() -> None:
    route = respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embedding_body(2))
    )
    provider = embed_provider()
    vectors = await provider.embed(["年假规定", "考勤规定"])
    assert len(vectors) == 2 and len(vectors[0]) == 1024

    request = route.calls[0].request
    import json

    body = json.loads(request.content)
    assert body["model"] == "text-embedding-v4"
    assert body["dimensions"] == 1024          # 复数，不是 dimension
    assert body["input"] == ["年假规定", "考勤规定"]
    assert "encoding_format" in body
    assert request.headers["Authorization"] == "Bearer sk-test"


@respx.mock
async def test_embed_restores_order_by_index() -> None:
    """服务端可能乱序返回，必须按 data[i].index 还原，否则向量与文本错位。"""
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embedding_body(3, dim=4, reverse=True))
    )
    provider = embed_provider(dim=4)
    vectors = await provider.embed(["a", "b", "c"])
    assert [round(v[0], 4) for v in vectors] == [0.01, 0.02, 0.03]


@respx.mock
async def test_embed_batch_is_capped_at_ten() -> None:
    """v4 单请求上限 10 条：配置写大了要被夹住并拆成多次请求（01 §7 曾写 20）。"""
    route = respx.post(EMBED_URL).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=_embedding_body(len(__import__("json").loads(request.content)["input"])),
        )
    )
    provider = embed_provider(batch_size=50)
    assert provider.batch_size == EMBED_MAX_BATCH
    vectors = await provider.embed([f"文本{i}" for i in range(12)])
    assert len(vectors) == 12
    assert route.call_count == 2  # 10 + 2
    import json

    assert len(json.loads(route.calls[0].request.content)["input"]) == 10


@respx.mock
async def test_embed_dimension_mismatch_is_rejected() -> None:
    """C3：维度不符的向量绝不能流进 FAISS。"""
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embedding_body(1, dim=512))
    )
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await embed_provider().embed_one("年假")
    assert "DimensionMismatch" in str(excinfo.value) or "维度" in str(excinfo.value)


@respx.mock
async def test_embed_empty_data_is_rejected() -> None:
    """兼容模式 output_type=sparse 会 200 + 空向量：必须当场报错而不是静默入库。"""
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "usage": {"total_tokens": 1}})
    )
    with pytest.raises(EmbeddingUnavailable):
        await embed_provider().embed_one("年假")


@respx.mock
async def test_embed_retries_on_429_then_succeeds() -> None:
    route = respx.post(EMBED_URL).mock(
        side_effect=[
            httpx.Response(429, json={"code": "Throttling.RateQuota", "message": "Requests rate limit exceeded"}),
            httpx.Response(200, json=_embedding_body(1)),
        ]
    )
    vectors = await embed_provider(max_retries=2).embed_one("年假")
    assert len(vectors) == 1024
    assert route.call_count == 2


@respx.mock
async def test_embed_does_not_retry_invalid_api_key() -> None:
    """401 不可重试：重试只是浪费配额，必须立刻失败。"""
    route = respx.post(EMBED_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}
        )
    )
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await embed_provider(max_retries=3).embed_one("年假")
    assert route.call_count == 1
    assert "API Key 非法" in str(excinfo.value)


@respx.mock
async def test_embed_arrears_is_fatal() -> None:
    route = respx.post(EMBED_URL).mock(
        return_value=httpx.Response(400, json={"code": "Arrearage", "message": "Access denied"})
    )
    with pytest.raises(EmbeddingUnavailable) as excinfo:
        await embed_provider(max_retries=3).embed_one("年假")
    assert route.call_count == 1
    assert "欠费" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 重排
# ---------------------------------------------------------------------------


@respx.mock
async def test_rerank_success_maps_index_to_chunk_id() -> None:
    respx.post(RERANK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 2, "relevance_score": 0.93},
                        {"index": 0, "relevance_score": 0.34},
                    ]
                },
                "usage": {"total_tokens": 79},
                "request_id": "req-1",
            },
        )
    )
    docs = [
        RerankDoc(chunk_id=101, text="文档一", shard_id="kb_all_L1", rrf_score=0.02),
        RerankDoc(chunk_id=102, text="文档二", shard_id="kb_hr_L2", rrf_score=0.01),
        RerankDoc(chunk_id=103, text="文档三", shard_id="kb_hr_L2", rrf_score=0.03),
    ]
    results = await rerank_provider().rerank("年假", docs, top_n=2)
    assert [r.chunk_id for r in results] == [103, 101]  # 按服务端顺序（高分在前）
    assert results[0].score == pytest.approx(0.93)
    assert results[0].shard_id == "kb_hr_L2"
    assert results[0].rrf_score == pytest.approx(0.03)


@respx.mock
async def test_rerank_request_uses_native_shape() -> None:
    route = respx.post(RERANK_URL).mock(
        return_value=httpx.Response(
            200, json={"output": {"results": [{"index": 0, "relevance_score": 0.5}]}}
        )
    )
    await rerank_provider().rerank("年假", [RerankDoc(chunk_id=1, text="x")], top_n=5)
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["input"]["query"] == "年假"
    assert body["input"]["documents"] == ["x"]
    assert body["parameters"]["top_n"] == 1          # 夹到文档数
    assert body["parameters"]["return_documents"] is False
    assert "compatible-mode" not in str(route.calls[0].request.url)


@respx.mock
async def test_rerank_empty_results_is_unavailable() -> None:
    """空结果 → 走降级（跳过精排取 RRF Top-5），**绝不编造分数**。"""
    respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={"output": {"results": []}}))
    with pytest.raises(RerankUnavailable):
        await rerank_provider().rerank("年假", [RerankDoc(chunk_id=1, text="x")], top_n=3)


@respx.mock
async def test_rerank_native_error_body_without_output() -> None:
    """原生协议的失败响应没有 output 字段，错误在顶层 code/message。"""
    respx.post(RERANK_URL).mock(
        return_value=httpx.Response(400, json={"code": "InvalidParameter", "message": "bad top_n"})
    )
    with pytest.raises(RerankUnavailable) as excinfo:
        await rerank_provider(max_retries=1).rerank("年假", [RerankDoc(chunk_id=1, text="x")], top_n=1)
    assert "InvalidParameter" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


@respx.mock
async def test_llm_success_parses_usage() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chat-1",
                "model": "qwen-max",
                "choices": [{"message": {"role": "assistant", "content": "年假 5 天 [1]"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 12},
            },
        )
    )
    result = await llm_provider().generate([{"role": "user", "content": "年假多少天"}])
    assert result.text == "年假 5 天 [1]"
    assert result.prompt_tokens == 120 and result.completion_tokens == 12
    assert result.fallback_used is False


def test_chat_compat_path_differs_for_deepseek_vs_dashscope() -> None:
    assert chat_compat_path("dashscope") == COMPATIBLE_PATH
    assert chat_compat_path("deepseek") == DEEPSEEK_COMPATIBLE_PATH


@respx.mock
async def test_llm_deepseek_primary_hits_official_v1_path() -> None:
    """主模型 provider=deepseek 时打官方 ``/v1/chat/completions``，不是百炼 compatible-mode。"""
    url = f"https://api.deepseek.com{DEEPSEEK_COMPATIBLE_PATH}/chat/completions"
    respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "官方直连"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )
    )
    provider = OpenAICompatibleLLMProvider(
        ref=model_ref("deepseek", "deepseek-flash"),
        api_key="sk-deepseek",
        base_url="https://api.deepseek.com",
        max_retries=0,
    )
    result = await provider.generate([{"role": "user", "content": "hi"}])
    assert result.text == "官方直连"
    assert result.fallback_used is False


@respx.mock
async def test_llm_falls_back_to_secondary_provider() -> None:
    """主模型连续失败 → 切 fallback（00 §2.3：用户无感，响应稍慢）。"""
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(500, json={"code": "InternalError", "message": "boom"})
    )
    deepseek_url = "https://api.deepseek.com/v1/chat/completions"
    respx.post(deepseek_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "来自 fallback"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    provider = llm_provider(
        max_retries=0,
        fallback={
            "provider": "deepseek", "model": "deepseek-chat",
            "base_url": "", "api_key": "sk-deepseek",
        },
    )
    result = await provider.generate([{"role": "user", "content": "年假多少天"}])
    assert result.text == "来自 fallback"
    assert result.fallback_used is True


@respx.mock
async def test_llm_raises_when_all_targets_fail() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(503, json={"code": "ModelUnavailable", "message": "down"})
    )
    with pytest.raises(ModelUnavailable):
        await llm_provider(max_retries=0).generate([{"role": "user", "content": "x"}])


@respx.mock
async def test_llm_stream_yields_deltas() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"年假"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" 5 天"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})
    )
    pieces = [piece async for piece in llm_provider().generate_stream(
        [{"role": "user", "content": "年假多少天"}]
    )]
    assert pieces == ["年假", " 5 天"]


@respx.mock
async def test_llm_stream_surfaces_error_status() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad key"}})
    )
    with pytest.raises(ModelUnavailable):
        async for _ in llm_provider().generate_stream([{"role": "user", "content": "x"}]):
            pass  # pragma: no cover
