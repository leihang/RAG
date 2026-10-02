"""模型 provider 抽象（`00` 约束 C3）。

上层代码**不得**引用具体模型名或维度：所有能力都通过本模块的 Protocol 取用，
模型身份只体现在 :class:`ModelRef` 上。

嵌入指纹是 C3 的另一半：``sha256(模型标识)`` 随每个向量记录，检索时校验一致，
防止「换了模型还用旧向量」。启动自检发现不一致必须**拒绝启动**（`00` §2.3）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

#: `00` §3.1 的模型标识：``<provider>/<model>@<version>#<dim>``
MODEL_IDENT_TEMPLATE = "{provider}/{model}@{version}#{dim}"

_VERSION_SUFFIX_RE = re.compile(r"[-_](v\d+(?:\.\d+)?)$", re.IGNORECASE)


def derive_version(model: str) -> str:
    """从模型名推导 ``@<version>``（配置里没有 version 键，见 `01` §7）。

    ``text-embedding-v4`` → ``v4``；无版本后缀时用模型名本身（例如 ``qwen-max`` → ``qwen-max``）。
    这是**推导规则**而非文档口径，已记入 ``docs/spec/10`` 的实现待确认项，定稿前不要当成契约。
    """
    match = _VERSION_SUFFIX_RE.search(model)
    return match.group(1) if match else model


@dataclass(frozen=True)
class ModelRef:
    """模型身份；:attr:`ident` 与 :attr:`fingerprint` 是 C3 的载体。"""

    provider: str
    model: str
    version: str = ""
    dim: int | None = None

    @property
    def ident(self) -> str:
        return MODEL_IDENT_TEMPLATE.format(
            provider=self.provider, model=self.model, version=self.version, dim=self.dim
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.ident.encode("utf-8")).hexdigest()


def model_ref(provider: str, model: str, *, dim: int | None = None, version: str | None = None) -> ModelRef:
    return ModelRef(
        provider=provider, model=model, version=version or derive_version(model), dim=dim
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class RerankDoc:
    """rerank 的输入单元（只带 rerank 需要的字段）。"""

    chunk_id: int
    text: str
    shard_id: str = ""
    rrf_score: float = 0.0
    dense_rank: int | None = None
    sparse_rank: int | None = None


@dataclass
class RerankResult:
    chunk_id: int
    score: float
    shard_id: str = ""
    rrf_score: float = 0.0


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_used: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 能力协议
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本 → 归一化向量。``ref.dim`` 必须与向量长度一致。"""

    ref: ModelRef

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_one(self, text: str) -> list[float]: ...


@runtime_checkable
class MultimodalEmbeddingProvider(Protocol):
    """多模态统一空间：文本查询与图片进同一维度（课程 17 / tongyi-embedding-vision）。"""

    ref: ModelRef

    async def embed_text(self, text: str) -> list[float]: ...

    async def embed_image(self, image_path: str | Path) -> list[float]: ...


@runtime_checkable
class RerankProvider(Protocol):
    """候选精排。返回值按分数降序，条数不足即视为失败（禁止补 0 分）。"""

    ref: ModelRef

    async def rerank(
        self, query: str, docs: Sequence[RerankDoc], top_n: int
    ) -> list[RerankResult]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """对话生成。``stream=True`` 时返回异步迭代器（SSE / 钉钉卡片流式）。"""

    ref: ModelRef

    async def generate(
        self, messages: Sequence[dict[str, str]], **kwargs: Any
    ) -> LLMResult: ...


def cosine_to_inner_product_note() -> str:
    """占位文档：说明为什么 FAISS 用内积而不是 L2（向量已归一化，IP == cosine）。"""
    return "写入 FAISS 前统一 faiss.normalize_L2，故 IndexFlatIP 的内积即余弦相似度（06 §2.3）。"
