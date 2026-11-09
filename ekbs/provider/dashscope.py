"""阿里云百炼（DashScope）实现 —— 契约逐条依据 `docs/spec/40-cloud-models.md`。

三条**关键结论**决定了本文件的形态（都不是猜的，是核实过的）：

1. **嵌入**：走 OpenAI 兼容模式 ``/compatible-mode/v1/embeddings``，维度参数是**复数** ``dimensions``；
   单请求条数上限**按模型不同**（``text-embedding-v4`` 是 10，`01` §7 写的 20 会 400；
   ``qwen3.7-text-embedding-flash`` 是 20），见 ``EMBED_MAX_BATCH_BY_MODEL``。
2. **重排**：**不能**走 OpenAI 兼容模式 —— ``gte-rerank-v2`` 只能用 DashScope 原生 HTTP
   ``/api/v1/services/rerank/text-rerank/text-rerank``（顶层 ``code``/``message`` 错误体、
   ``output.results[]`` 响应体）。⚠️ 该模型官方公告 **2026-05-30 下线**，换 ``qwen3-rerank``
   时需要改的是同一端点下的 ``model`` 与 ``parameters``，因此这里把模型名完全配置化。
3. **生成**：走 OpenAI 兼容模式 ``/compatible-mode/v1/chat/completions``，主模型失败切 ``fallback``。

另外两个已知陷阱已在代码里防住：

* **静默失败**：兼容模式下传 ``output_type=sparse`` 会返回 **200 + 空向量**。本项目用稠密向量、
  不传该参数，并**强制断言** ``len(embedding) == dimensions``。
* **``relevance_score`` 是「请求内相对分」**，官方明确不可跨请求比较 ⇒ ``min_score=0.30``
  只能当启发式阈值，必须用 `02` 的评测集实测校准（`00` §11 的 M3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx

from ekbs.errors import ModelUnavailable, RerankUnavailable
from ekbs.logging import get_logger
from ekbs.provider.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResult,
    ModelRef,
    RerankDoc,
    RerankProvider,
    RerankResult,
)
from ekbs.provider.http_client import (
    CloudError,
    request_with_retry,
    resolve_base_url,
    to_provider_error,
)

log = get_logger(component="dashscope")

#: 未知模型时的保守上限（`docs/spec/40` §2.0；超出报 `batch size is invalid`）
EMBED_MAX_BATCH = 10

#: **按模型区分**的单请求输入上限。原实现把 10 硬编码在所有模型上，
#: 于是换 `qwen3.7-text-embedding-flash` 后配置写 20 会被静默压回 10，
#: 并打出一条指名 text-embedding-v4 的误导性警告 —— 入库吞吐白丢一半。
#: 未列入的模型一律回退 `EMBED_MAX_BATCH`（宁可慢，不要 400）。
EMBED_MAX_BATCH_BY_MODEL: dict[str, int] = {
    "text-embedding-v4": 10,
    "text-embedding-v3": 10,
    "qwen3.7-text-embedding-flash": 20,  # 官方 20；2026-09-27 实测 20 条一次请求通过
}


def embed_max_batch(model: str) -> int:
    """该模型的单请求嵌入条数上限。"""
    return EMBED_MAX_BATCH_BY_MODEL.get(model, EMBED_MAX_BATCH)

#: 兼容模式的路径前缀（阿里云百炼 OpenAI 兼容）
COMPATIBLE_PATH = "/compatible-mode/v1"

#: DeepSeek 官方 OpenAI 兼容前缀（``https://api.deepseek.com/v1/chat/completions``）
DEEPSEEK_COMPATIBLE_PATH = "/v1"

#: 原生协议的 rerank 路径
RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def chat_compat_path(provider: str) -> str:
    """对话补全路径前缀：百炼用 ``/compatible-mode/v1``，DeepSeek 官方用 ``/v1``。"""
    if str(provider or "").strip().lower() == "deepseek":
        return DEEPSEEK_COMPATIBLE_PATH
    return COMPATIBLE_PATH


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _message_text(content: Any) -> str:
    """视觉模型有时把 content 分成多段（文本块列表），不能直接 str() 成 Python 列表。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content)


class DashScopeEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        *,
        ref: ModelRef,
        api_key: str,
        base_url: str | None = None,
        batch_size: int = EMBED_MAX_BATCH,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
        asymmetric: bool = False,
    ) -> None:
        self.ref = ref
        #: 查询/文档**不对称编码**（DashScope `text_type`）。实测该参数确实生效
        #: （`document` 与默认向量余弦 0.916），但**开启会改变全部 chunk 向量**：
        #: 必须全量重灌，且 `ref.variant` 要带上 `asym` 让指纹变化（C3：禁止混用向量）。
        self.asymmetric = bool(asymmetric)
        self.api_key = api_key
        self.base_url = resolve_base_url(base_url, DEFAULT_BASE_URL)
        self.retry_base_delay = retry_base_delay
        #: 云端真实 token 用量累计（`usage.total_tokens`）。校准 `TC-INDEX-004` 的唯一真值锚点：
        #: 调用本来就要发，**记录它零成本**，而不记录就只能靠估算（实测偏差 −35%）。
        self.usage_total = 0
        self.usage_calls = 0
        # 双保险：即便配置写大了也不会把非法请求发出去（01 §7 曾写 20）
        cap = embed_max_batch(ref.model)
        self.batch_size = max(1, min(int(batch_size), cap))
        if int(batch_size) > cap:
            log.warning(
                "embed_batch_clamped",
                configured=int(batch_size),
                used=self.batch_size,
                model=ref.model,
                reason=f"{ref.model} 单请求上限 {cap} 条（docs/spec/40 §2.0）",
            )
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self._client = client

    # ------------------------------------------------------------------

    def _http(self) -> tuple[httpx.AsyncClient, bool]:
        """返回 ``(client, 是否自建)``；自建的用完要关。"""
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self.timeout_s), True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed_with_type(texts, "document")

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]

    async def embed_query(self, text: str) -> list[float]:
        """查询侧嵌入：不对称编码开启时用 ``text_type=query``。"""
        vectors = await self._embed_with_type([text], "query")
        return vectors[0]

    async def _embed_with_type(
        self, texts: Sequence[str], text_type: str
    ) -> list[list[float]]:
        if isinstance(texts, str):
            # 防呆：传裸字符串会被当成"一串文本"逐字嵌入 —— 白烧钱且结果错得离谱。
            # 实测踩过一次（查询侧误传 question 字符串）。要单条请用 embed_one/embed_query。
            raise TypeError(
                "embed() 需要文本列表；单条请用 embed_one()/embed_query()"
                "（传裸字符串会逐字嵌入，既费钱又错）"
            )
        items = list(texts)
        if not items:
            return []
        vectors: list[list[float]] = []
        api_text_type = text_type if self.asymmetric else None
        client, owned = self._http()
        try:
            for start in range(0, len(items), self.batch_size):
                batch = items[start : start + self.batch_size]
                vectors.extend(await self._embed_batch(client, batch, text_type=api_text_type))
        finally:
            if owned:
                await client.aclose()
        return vectors

    def pop_usage(self) -> tuple[int, int]:
        """取走并清零累计的云端用量，返回 ``(total_tokens, calls)``。

        为什么是"取走"而不是"只读"：入库侧要按**文档**记账（校准是按文档做的），
        不清零就会把上一篇的用量算到这一篇头上。
        """
        total, calls = self.usage_total, self.usage_calls
        self.usage_total, self.usage_calls = 0, 0
        return total, calls

    async def _embed_batch(
        self,
        client: httpx.AsyncClient,
        batch: Sequence[str],
        *,
        text_type: str | None = None,
    ) -> list[list[float]]:
        url = f"{self.base_url}{COMPATIBLE_PATH}/embeddings"
        body: dict[str, Any] = {
            "model": self.ref.model,
            "input": list(batch),
            "dimensions": int(self.ref.dim or 1024),
            "encoding_format": "float",
        }
        if text_type:
            # 查询/文档不对称编码（DashScope 扩展字段，OpenAI 兼容端点接受）
            body["text_type"] = text_type
        try:
            response = await request_with_retry(
                client, "POST", url, provider="dashscope", endpoint="embeddings",
                kind="embed", max_retries=self.max_retries, json_body=body,
                headers=_auth_headers(self.api_key), base_delay=self.retry_base_delay,
            )
        except CloudError as exc:
            raise to_provider_error(exc, "embed") from exc

        payload = response.json()
        # 云端真实用量：`02` TC-INDEX-004 要求本地计数与它偏差 ≤5%，所以必须留下这个数
        usage = payload.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        if total_tokens:
            self.usage_total += total_tokens
            self.usage_calls += 1
        rows = payload.get("data") or []
        if not rows:
            raise to_provider_error(
                CloudError(
                    provider="dashscope", endpoint="embeddings", status_code=200,
                    code="EmptyEmbedding",
                    message="响应缺少 data（兼容模式下 output_type=sparse 会 200 + 空向量，"
                    "本项目不传该参数）",
                    retryable=False,
                ),
                "embed",
            )
        # 按 data[i].index 还原顺序：服务端不保证返回顺序与输入一致
        ordered: list[tuple[int, list[float]]] = []
        expected_dim = int(self.ref.dim or 0)
        for row in rows:
            vector = [float(v) for v in (row.get("embedding") or [])]
            # ⚠️ 静默失败防线：空向量/维度不符必须当场报错，不能流进 FAISS
            if expected_dim and len(vector) != expected_dim:
                raise to_provider_error(
                    CloudError(
                        provider="dashscope", endpoint="embeddings", status_code=200,
                        code="DimensionMismatch",
                        message=f"返回维度 {len(vector)} ≠ 期望 {expected_dim}（C3：禁止混用向量）",
                        retryable=False,
                    ),
                    "embed",
                )
            ordered.append((int(row.get("index", len(ordered))), vector))
        ordered.sort(key=lambda pair: pair[0])
        return [vector for _, vector in ordered]


class DashScopeRerankProvider(RerankProvider):
    """``gte-rerank-v2`` / ``qwen3-rerank`` 的原生 HTTP 实现（兼容模式下不可用）。"""

    def __init__(
        self,
        *,
        ref: ModelRef,
        api_key: str,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        return_documents: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ref = ref
        self.api_key = api_key
        self.base_url = resolve_base_url(base_url, DEFAULT_BASE_URL)
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = retry_base_delay
        # gte-rerank-v2 支持 return_documents；本项目不需要原文（候选文本本地已有），
        # 默认 false 省网络开销（docs/spec/40 §3.2）
        self.return_documents = return_documents
        self._client = client

    async def rerank(
        self, query: str, docs: Sequence[RerankDoc], top_n: int
    ) -> list[RerankResult]:
        items = list(docs)
        if not items:
            return []
        top_n = max(1, min(int(top_n), len(items)))
        url = f"{self.base_url}{RERANK_PATH}"
        body: dict[str, Any] = {
            "model": self.ref.model,
            "input": {"query": query, "documents": [doc.text for doc in items]},
            "parameters": {"return_documents": self.return_documents, "top_n": top_n},
        }
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, "POST", url, provider="dashscope", endpoint="text-rerank",
                kind="rerank", max_retries=self.max_retries, json_body=body,
                headers=_auth_headers(self.api_key), base_delay=self.retry_base_delay,
            )
        except CloudError as exc:
            raise to_provider_error(exc, "rerank") from exc
        finally:
            if owned:
                await client.aclose()

        payload = response.json()
        results = ((payload.get("output") or {}).get("results")) or []
        if not results:
            # 失败响应没有 output 字段（只有顶层 code/message）；到这儿说明服务端回了空结果。
            # 00 §2.1：返回条数不足即视为失败并走降级，**禁止补 0 分、禁止编造分数**。
            raise RerankUnavailable(
                "rerank 返回空结果（不编造分数，按 00 §2.1 走降级：跳过精排取 RRF Top-5）"
            )
        if len(results) < top_n:
            log.warning(
                "rerank_partial_results", requested=top_n, returned=len(results),
                note="只使用真实返回的分数，不补齐、不编造",
            )
        out: list[RerankResult] = []
        for row in results:
            index = int(row.get("index", -1))
            if index < 0 or index >= len(items):
                log.warning("rerank_index_out_of_range", index=index, documents=len(items))
                continue
            source = items[index]
            out.append(
                RerankResult(
                    chunk_id=source.chunk_id,
                    score=float(row.get("relevance_score", 0.0)),
                    shard_id=source.shard_id,
                    rrf_score=source.rrf_score,
                )
            )
        return out


class OpenAICompatibleLLMProvider(LLMProvider):
    """``/compatible-mode/v1/chat/completions`` + 主模型失败切 fallback（`00` §2.3）。"""

    def __init__(
        self,
        *,
        ref: ModelRef,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.1,
        top_p: float = 0.8,
        max_tokens: int = 2048,
        reasoning_effort: str = "",
        timeout_s: float = 120.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        fallback: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ref = ref
        self.api_key = api_key
        self.base_url = resolve_base_url(base_url, DEFAULT_BASE_URL)
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort.strip()
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = retry_base_delay
        self.fallback = fallback or {}
        self._client = client

    # ------------------------------------------------------------------

    def _body(
        self,
        messages: Sequence[dict[str, str]],
        *,
        stream: bool,
        model: str,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """请求体。

        ``response_format`` 只在**显式传入**时带上（如取数模式要 ``{"type": "json_object"}``）：
        默认 ``None`` = 不带该字段，既有调用（业务问答/流式）的请求体**逐字不变**
        —— 这是"加能力但不改行为"的边界。
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if response_format is not None:
            body["response_format"] = dict(response_format)
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    async def generate(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> LLMResult:
        #: 只认 ``response_format``（取数模式要 JSON 模式）；未知 kwargs 忽略，避免调用方
        #: 以为传了参数就一定生效（历史上就有"发 top_k 但后端只认 dense_top_k"的静默失效）。
        response_format = kwargs.get("response_format")
        targets = [(self.ref.provider, self.ref.model, self.base_url, self.api_key)]
        if self.fallback.get("provider") and self.fallback.get("model"):
            targets.append(
                (
                    self.fallback["provider"],
                    self.fallback["model"],
                    resolve_base_url(
                        self.fallback.get("base_url"),
                        DEEPSEEK_BASE_URL if self.fallback["provider"] == "deepseek"
                        else DEFAULT_BASE_URL,
                    ),
                    self.fallback.get("api_key", ""),
                )
            )
        last_error: ModelUnavailable | None = None
        for provider, model, base_url, api_key in targets:
            try:
                result = await self._generate_once(
                    messages, provider=provider, model=model, base_url=base_url, api_key=api_key,
                    stream=False,
                    response_format=response_format if isinstance(response_format, dict) else None,
                )
            except ModelUnavailable as exc:
                last_error = exc
                log.warning("llm_target_failed", provider=provider, model=model, error=str(exc))
                continue
            if provider != self.ref.provider:
                result.fallback_used = True
                log.warning("llm_fallback_used", provider=provider, model=model)
            return result
        assert last_error is not None
        raise last_error

    async def _generate_once(
        self,
        messages: Sequence[dict[str, str]],
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        stream: bool,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        url = f"{base_url}{chat_compat_path(provider)}/chat/completions"
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, "POST", url, provider=provider, endpoint="chat/completions",
                kind="llm", max_retries=self.max_retries,
                json_body=self._body(
                    messages, stream=stream, model=model, response_format=response_format
                ),
                headers=_auth_headers(api_key), base_delay=self.retry_base_delay,
            )
        except CloudError as exc:
            raise to_provider_error(exc, "llm") from exc
        finally:
            if owned:
                await client.aclose()

        payload = response.json()
        choices = payload.get("choices") or []
        text = ""
        finish_reason = ""
        if choices:
            text = _message_text((choices[0].get("message") or {}).get("content"))
            finish_reason = str(choices[0].get("finish_reason") or "")
        usage = payload.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(payload.get("model") or model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=finish_reason,
            raw={"id": payload.get("id", ""), "provider": provider},
        )

    async def generate_stream(
        self, messages: Sequence[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        """SSE 流式生成（钉钉卡片流式与 H5 共用）。

        首字延迟（TTFT）是 SLO 指标（`00` §4：P95 ≤5s），因此流式是**必需**而非优化。
        主模型在**首字前**失败才切 fallback：已经吐字后再切会让用户看到两段拼接的答案。
        """
        url = f"{self.base_url}{chat_compat_path(self.ref.provider)}/chat/completions"
        body = self._body(messages, stream=True, model=self.ref.model)
        timeout = httpx.Timeout(self.timeout_s, connect=10.0, read=self.timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST", url, json=body, headers=_auth_headers(self.api_key)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                error = CloudError(
                    provider=self.ref.provider, endpoint="chat/completions",
                    status_code=response.status_code,
                    message=(response.text or "")[:300],
                    retryable=response.status_code in {429, 500, 503},
                )
                raise to_provider_error(error, "llm")
            emitted = False
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                import json

                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        emitted = True
                        yield str(piece)
            if not emitted:
                log.warning("llm_stream_empty", model=self.ref.model)


# ---------------------------------------------------------------------------
# 多模态嵌入（课程 17 / tongyi-embedding-vision-plus）—— **无** OpenAI 兼容端点
# ---------------------------------------------------------------------------


def _image_data_uri(path: Path) -> str:
    import base64

    suffix = path.suffix.lstrip(".").lower() or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    raw = path.read_bytes()
    return f"data:image/{suffix};base64,{base64.b64encode(raw).decode('ascii')}"


def _mm_embedding_from_response(resp: Any, *, dim: int, what: str) -> list[float]:
    from http import HTTPStatus

    status = getattr(resp, "status_code", None)
    if status != HTTPStatus.OK:
        message = getattr(resp, "message", None) or getattr(resp, "code", "") or str(resp)
        raise ModelUnavailable(f"多模态嵌入失败（{what}）：{message}")
    output = getattr(resp, "output", None) or {}
    if isinstance(output, dict):
        embeddings = output.get("embeddings") or []
    else:
        embeddings = getattr(output, "embeddings", None) or []
    if not embeddings:
        raise ModelUnavailable(f"多模态嵌入空响应（{what}）")
    first = embeddings[0]
    vector = first.get("embedding") if isinstance(first, dict) else getattr(first, "embedding", None)
    if not isinstance(vector, list) or not vector:
        raise ModelUnavailable(f"多模态嵌入缺 embedding（{what}）")
    if dim and len(vector) != dim:
        raise ModelUnavailable(
            f"多模态嵌入维度 {len(vector)} ≠ 配置 dim {dim}（模型={what}）"
        )
    return [float(x) for x in vector]


class DashScopeMultimodalEmbeddingProvider:
    """DashScope ``MultiModalEmbedding``（与文本 embed 的 OpenAI 兼容端点无关）。"""

    def __init__(
        self,
        *,
        ref: ModelRef,
        api_key: str,
        timeout_s: float = 60.0,
    ) -> None:
        self.ref = ref
        self.api_key = api_key
        self.timeout_s = float(timeout_s)

    def _call(self, payload: list[dict[str, str]]) -> list[float]:
        import dashscope
        from dashscope import MultiModalEmbedding

        dashscope.api_key = self.api_key
        resp = MultiModalEmbedding.call(model=self.ref.model, input=payload, api_key=self.api_key)
        return _mm_embedding_from_response(
            resp, dim=int(self.ref.dim or 0), what=self.ref.model
        )

    async def embed_text(self, text: str) -> list[float]:
        import asyncio

        return await asyncio.to_thread(self._call, [{"text": text or ""}])

    async def embed_image(self, image_path: str | Path) -> list[float]:
        import asyncio

        path = Path(image_path)
        if not path.is_file():
            raise ModelUnavailable(f"多模态嵌入：图片不存在 {path}")
        return await asyncio.to_thread(self._call, [{"image": _image_data_uri(path)}])
