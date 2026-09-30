"""provider 注册表 —— 从配置构造模型能力（`00` 约束 C3 的「抽象」落点）。

provider 名字是配置里的开关（``models.embed.provider`` / ``retrieval.rerank.provider`` /
``models.llm.provider``）。新增一家云厂商只需在这里加一个分支，上层编排代码零改动。
"""

from __future__ import annotations

from dataclasses import dataclass

from ekbs.config import Secrets, Settings
from ekbs.errors import ConfigError
from ekbs.provider.base import EmbeddingProvider, LLMProvider, RerankProvider, model_ref
from ekbs.provider.mock import MockEmbeddingProvider, MockLLMProvider, MockRerankProvider

#: 已知 provider 名（用于配置校验与错误提示）
KNOWN_PROVIDERS = ("mock", "dashscope", "deepseek")


@dataclass
class Providers:
    """一次进程生命周期内的三件套；由 FastAPI lifespan / worker 启动时构造一次。"""

    embed: EmbeddingProvider
    rerank: RerankProvider | None
    llm: LLMProvider


def build_embedding(settings: Settings, secrets: Secrets) -> EmbeddingProvider:
    cfg = settings.models.embed
    ref = model_ref(cfg.provider, cfg.model, dim=cfg.dim, version=cfg.version)
    if cfg.provider == "mock":
        return MockEmbeddingProvider(dim=cfg.dim, provider=ref.provider, model=ref.model)
    if cfg.provider == "dashscope":
        secrets.require("DASHSCOPE_API_KEY")
        from ekbs.provider.dashscope import DashScopeEmbeddingProvider

        return DashScopeEmbeddingProvider(
            ref=ref, api_key=secrets.get("DASHSCOPE_API_KEY"), base_url=cfg.base_url,
            batch_size=cfg.batch_size, timeout_s=cfg.timeout_s, max_retries=cfg.max_retries,
        )
    raise ConfigError(
        f"未知的嵌入 provider：{cfg.provider}（可选：{'/'.join(KNOWN_PROVIDERS)}；"
        "新增 provider 请在 ekbs/provider/ 下实现并在此注册）"
    )


def build_rerank(settings: Settings, secrets: Secrets) -> RerankProvider | None:
    cfg = settings.retrieval.rerank
    if not cfg.enabled:
        return None
    ref = model_ref(cfg.provider, cfg.model)
    if cfg.provider == "mock":
        return MockRerankProvider(provider=ref.provider, model=ref.model)
    if cfg.provider == "dashscope":
        secrets.require("DASHSCOPE_API_KEY")
        from ekbs.provider.dashscope import DashScopeRerankProvider

        return DashScopeRerankProvider(
            ref=ref, api_key=secrets.get("DASHSCOPE_API_KEY"), base_url=cfg.base_url,
            timeout_s=30, max_retries=2,
        )
    raise ConfigError(f"未知的 rerank provider：{cfg.provider}")


def build_llm(settings: Settings, secrets: Secrets) -> LLMProvider:
    cfg = settings.models.llm
    ref = model_ref(cfg.provider, cfg.model, version=cfg.version)
    if cfg.provider == "mock":
        return MockLLMProvider(provider=ref.provider, model=ref.model)
    if cfg.provider in {"dashscope", "deepseek"}:
        key_name = "DASHSCOPE_API_KEY" if cfg.provider == "dashscope" else "DEEPSEEK_API_KEY"
        secrets.require(key_name)
        from ekbs.provider.dashscope import OpenAICompatibleLLMProvider

        fallback = None
        if cfg.fallback.provider:
            fallback_key = (
                "DASHSCOPE_API_KEY" if cfg.fallback.provider == "dashscope" else "DEEPSEEK_API_KEY"
            )
            if secrets.is_configured(fallback_key):
                fallback = {
                    "provider": cfg.fallback.provider,
                    "model": cfg.fallback.model,
                    "base_url": cfg.fallback.base_url,
                    "api_key": secrets.get(fallback_key),
                }
        return OpenAICompatibleLLMProvider(
            ref=ref,
            api_key=secrets.get(key_name),
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=cfg.max_tokens,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
            fallback=fallback,
        )
    raise ConfigError(f"未知的 LLM provider：{cfg.provider}")


def build_providers(settings: Settings, secrets: Secrets | None = None) -> Providers:
    secrets = secrets or Secrets()
    return Providers(
        embed=build_embedding(settings, secrets),
        rerank=build_rerank(settings, secrets),
        llm=build_llm(settings, secrets),
    )
