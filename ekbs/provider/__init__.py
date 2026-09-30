"""模型 provider：抽象协议、注册表、离线 mock、云端实现。

导入约定：``from ekbs.provider import build_providers, Providers``。
云端实现（``ekbs.provider.dashscope``）按需懒加载，未配 key 的进程不会 import 它。
"""

from ekbs.provider.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResult,
    ModelRef,
    RerankDoc,
    RerankProvider,
    RerankResult,
    derive_version,
    model_ref,
)
from ekbs.provider.mock import MockEmbeddingProvider, MockLLMProvider, MockRerankProvider
from ekbs.provider.registry import Providers, build_providers

__all__ = [
    "EmbeddingProvider",
    "LLMProvider",
    "LLMResult",
    "MockEmbeddingProvider",
    "MockLLMProvider",
    "MockRerankProvider",
    "ModelRef",
    "Providers",
    "RerankDoc",
    "RerankProvider",
    "RerankResult",
    "build_providers",
    "derive_version",
    "model_ref",
]
