"""离线 mock provider —— 让全链路在**没有任何云端 key** 的情况下可跑、可测。

设计原则：mock 不是「随便返回」，而是**确定性 + 可判别**：

* 嵌入：jieba 分词后的**哈希词袋**（每个 token 落到 ``dim`` 中的一维，符号由哈希决定），
  再做 L2 归一化。文本共享 token ⇒ 内积高，因此「已知问题能召回正确条款」这条
  在离线状态下也是真断言，而不是空跑。
* 精排：查询 token 对候选文本的**覆盖率** + 整串命中加成，归一到 ``[0, 1]``，
  与 ``retrieval.rerank.min_score=0.30`` 同一量纲。
* 生成：按上下文块编号产出带 ``[n]`` 引用的确定性答案，供引用后置校验用例使用。

mock 的嵌入指纹与真实模型不同（``mock/mock-embed-v1@v1#1024``），因此它天然会触发
「指纹不一致 → 拒绝启动」的路径 —— 这正是 C3 想要的行为，测试里会用它做反例。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Sequence

from ekbs.provider.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResult,
    ModelRef,
    RerankDoc,
    RerankResult,
    RerankProvider,
    model_ref,
)

_STOPWORDS = {"的", "了", "是", "在", "和", "与", "有", "多少", "什么", "怎么", "吗", "？", "?"}


def _tokenize(text: str) -> list[str]:
    """jieba 分词 + 去停用词 + 去纯标点（与 FTS5 侧同一分词口径）。

    注意**不要**过滤中文单字：jieba 通用词典会把「年假」切成「年/假」这类单字，
    把单字一律丢掉会让查询词集合变空、相似度恒为 0（实测踩过）。
    中文语境下单字本身携带语义，保留它们比「看起来干净」更重要；
    真正该丢的是停用词与纯标点。
    """
    import jieba  # 延迟导入：首次导入会构建词典缓存（约 1s），不拖慢进程启动

    tokens: list[str] = []
    for raw in jieba.lcut(text or ""):
        token = raw.strip()
        if not token or token in _STOPWORDS:
            continue
        if not any(ch.isalnum() for ch in token):
            continue  # 纯标点/空白
        tokens.append(token.lower())
    return tokens


def _hash_unit(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % dim
    sign = 1.0 if digest[8] & 1 else -1.0
    return index, sign


class MockEmbeddingProvider(EmbeddingProvider):
    """确定性哈希词袋嵌入（离线可跑，便于单测与权限红线测试）。"""

    def __init__(self, dim: int = 1024, provider: str = "mock", model: str = "mock-embed-v1") -> None:
        self.ref: ModelRef = model_ref(provider, model, dim=dim)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        dim = int(self.ref.dim or 1024)
        vector = [0.0] * dim
        tokens = _tokenize(text)
        if not tokens:
            # 空文本也要给出稳定的单位向量，避免下游除零
            vector[0] = 1.0
            return vector
        for token in tokens:
            index, sign = _hash_unit(token, dim)
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:  # 极端情况：token 哈希互相抵消
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


class MockRerankProvider(RerankProvider):
    """查询词覆盖率精排，量纲对齐 ``min_score=0.30``。"""

    def __init__(self, provider: str = "mock", model: str = "mock-rerank-v1") -> None:
        self.ref: ModelRef = model_ref(provider, model)

    async def rerank(
        self, query: str, docs: Sequence[RerankDoc], top_n: int
    ) -> list[RerankResult]:
        query_tokens = set(_tokenize(query))
        normalized_query = "".join((query or "").split())
        results: list[RerankResult] = []
        for doc in docs:
            doc_tokens = set(_tokenize(doc.text))
            if not query_tokens:
                coverage = 0.0
            else:
                coverage = len(query_tokens & doc_tokens) / len(query_tokens)
            bonus = 0.0
            if normalized_query and normalized_query in "".join((doc.text or "").split()):
                bonus = 0.25
            score = min(1.0, coverage + bonus)
            results.append(
                RerankResult(
                    chunk_id=doc.chunk_id,
                    score=round(score, 6),
                    shard_id=doc.shard_id,
                    rrf_score=doc.rrf_score,
                )
            )
        results.sort(key=lambda r: (-r.score, r.chunk_id))
        return results[: max(1, top_n)]


class MockLLMProvider(LLMProvider):
    """确定性生成：把上下文块编号写进答案，便于引用校验用例断言。"""

    def __init__(self, provider: str = "mock", model: str = "mock-llm-v1") -> None:
        self.ref: ModelRef = model_ref(provider, model)
        self.calls: list[list[dict[str, str]]] = []

    async def generate(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> LLMResult:
        self.calls.append([dict(m) for m in messages])
        context = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        question = ""
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                question = message.get("content", "")
                break

        # 上下文里每块形如「[n] 标题（第 x 页）: 正文」；按编号引用前两块
        indices = _extract_citation_indices(context) or []
        if not indices:
            return LLMResult(text="未找到可靠依据。", model=self.ref.model)

        cited = indices[:2]
        marks = "、".join(f"[{i}]" for i in cited)
        head = _head_of_block(context, cited[0])
        answer = f"根据企业知识库资料 {marks}：{head}"
        if question:
            answer = f"关于「{question.strip()[:40]}」，{answer}"
        return LLMResult(
            text=answer,
            model=self.ref.model,
            prompt_tokens=sum(len(m.get("content", "")) for m in messages),
            completion_tokens=len(answer),
        )


def _extract_citation_indices(context: str) -> list[int]:
    import re

    return [int(m) for m in re.findall(r"^\[(\d+)\]", context, flags=re.MULTILINE)]


def _head_of_block(context: str, index: int) -> str:
    import re

    match = re.search(rf"^\[{index}\]\s*(.+)$", context, flags=re.MULTILINE)
    if not match:
        return "（上下文缺失）"
    body = match.group(1).strip()
    return body[:60] + ("…" if len(body) > 60 else "")
