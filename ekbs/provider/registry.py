"""provider 注册表 —— 从配置构造模型能力（`00` 约束 C3 的「抽象」落点）。

provider 名字是配置里的开关（``models.embed.provider`` / ``retrieval.rerank.provider`` /
``models.llm.provider``）。新增一家云厂商只需在这里加一个分支，上层编排代码零改动。
"""

from __future__ import annotations

from dataclasses import dataclass

from ekbs.config import Secrets, Settings
from ekbs.errors import ConfigError
from ekbs.provider.base import (
    EmbeddingProvider,
    LLMProvider,
    MultimodalEmbeddingProvider,
    RerankProvider,
    model_ref,
)
from ekbs.provider.mock import (
    MockEmbeddingProvider,
    MockLLMProvider,
    MockMultimodalEmbeddingProvider,
    MockRerankProvider,
)

#: 已知 provider 名（用于配置校验与错误提示）
KNOWN_PROVIDERS = ("mock", "dashscope", "deepseek")


@dataclass
class Providers:
    """一次进程生命周期内的三件套；由 FastAPI lifespan / worker 启动时构造一次。"""

    embed: EmbeddingProvider
    rerank: RerankProvider | None
    llm: LLMProvider
    #: 视觉模型（给图片写描述）。**可选**：没配就退回"题注/页标题/OCR"那几条路径。
    vision: LLMProvider | None = None
    #: 多模态嵌入（figure 旁路索引）。``retrieval.figure_multimodal=false`` 或未配 model 时为 None。
    multimodal: MultimodalEmbeddingProvider | None = None


def build_embedding(settings: Settings, secrets: Secrets) -> EmbeddingProvider:
    cfg = settings.models.embed
    # 不对称编码开关 → 同时进 `variant`（改指纹，强制重建，避免新旧向量混用）
    asymmetric = bool(getattr(cfg, "asymmetry", False))
    variant = "asym" if asymmetric else ""
    ref = model_ref(cfg.provider, cfg.model, dim=cfg.dim, version=cfg.version, variant=variant)
    if cfg.provider == "mock":
        return MockEmbeddingProvider(dim=cfg.dim, provider=ref.provider, model=ref.model)
    if cfg.provider == "dashscope":
        secrets.require("DASHSCOPE_API_KEY")
        from ekbs.provider.dashscope import DashScopeEmbeddingProvider

        return DashScopeEmbeddingProvider(
            ref=ref, api_key=secrets.get("DASHSCOPE_API_KEY"), base_url=cfg.base_url,
            batch_size=cfg.batch_size, timeout_s=cfg.timeout_s, max_retries=cfg.max_retries,
            asymmetric=asymmetric,
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
    return _build_compatible_llm(settings.models.llm, secrets, what="LLM")


def build_query_expand_llm(settings: Settings, secrets: Secrets) -> LLMProvider | None:
    """快速模型，只把一条问法扩成两个检索角度。未配置则仍用规则变体。"""
    raw = (settings.retrieval.model_extra or {}).get("query_rewrite") or {}
    if not isinstance(raw, dict):
        return None
    model = str(raw.get("expand_model") or "").strip()
    if not model:
        return None
    parent = settings.models.llm
    cfg = parent.model_copy(update={
        "model": model,
        "max_tokens": int(raw.get("expand_max_tokens") or 64),
        "reasoning_effort": "",
        "timeout_s": int(raw.get("expand_timeout_s") or 8),
        "max_retries": 0,
        "temperature": 0.3,
        "fallback": parent.fallback.model_copy(update={"provider": "", "model": ""}),
    })
    return _build_compatible_llm(cfg, secrets, what="查询扩写")


def build_query_rewrite_llm(settings: Settings, secrets: Secrets) -> LLMProvider | None:
    """多轮上下文改写模型。未配 ``query_rewrite.model`` 时返回 None（调用方用回答 LLM）。

    与回答模型解耦：改写更吃指令遵循，可单独换稍强/专用模型而不抬高整路问答成本。
    """
    raw = (settings.retrieval.model_extra or {}).get("query_rewrite") or {}
    if not isinstance(raw, dict):
        return None
    model = str(raw.get("model") or "").strip()
    if not model:
        return None
    parent = settings.models.llm
    updates = {
        "model": model,
        "fallback": parent.fallback.model_copy(update={"provider": "", "model": ""}),
    }
    if raw.get("provider"):
        updates["provider"] = str(raw["provider"]).strip()
    if raw.get("base_url"):
        updates["base_url"] = str(raw["base_url"]).strip()
    if raw.get("timeout_s") is not None:
        updates["timeout_s"] = int(raw["timeout_s"])
    if raw.get("max_tokens") is not None:
        updates["max_tokens"] = int(raw["max_tokens"])
    cfg = parent.model_copy(update=updates)
    return _build_compatible_llm(cfg, secrets, what="查询改写")


def _build_compatible_llm(cfg, secrets: Secrets, *, what: str) -> LLMProvider:
    """按 `LLMCfg` 构造一个 OpenAI 兼容的对话模型（文本 LLM 与视觉模型共用）。

    抽出来是因为两者除了 `model` 完全一样（都是 `[provider, api_key, base_url, temperature,
    top_p, max_tokens, timeout, retries, fallback]`）—— 复制一遍必然漂移。
    """
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
            reasoning_effort=getattr(cfg, "reasoning_effort", ""),
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
            fallback=fallback,
        )
    raise ConfigError(f"未知的{what} provider：{cfg.provider}")


def build_vision(settings: Settings, secrets: Secrets) -> LLMProvider | None:
    """视觉模型（图片描述用）。没配 model 就返回 None —— 调用方据此跳过，不报错。"""
    cfg = settings.models.vision
    if not cfg.model:
        return None
    return _build_compatible_llm(cfg, secrets, what="视觉")


def build_multimodal(
    settings: Settings, secrets: Secrets
) -> MultimodalEmbeddingProvider | None:
    """figure 多模态嵌入。``retrieval.figure_multimodal=false`` 或未配 model → None。"""
    if not bool(getattr(settings.retrieval, "figure_multimodal", False)):
        return None
    cfg = settings.models.multimodal_embed
    if not cfg.model:
        return None
    ref = model_ref(cfg.provider, cfg.model, dim=cfg.dim, version=cfg.version)
    if cfg.provider == "mock":
        return MockMultimodalEmbeddingProvider(
            dim=cfg.dim, provider=ref.provider, model=ref.model
        )
    if cfg.provider == "dashscope":
        secrets.require("DASHSCOPE_API_KEY")
        from ekbs.provider.dashscope import DashScopeMultimodalEmbeddingProvider

        return DashScopeMultimodalEmbeddingProvider(
            ref=ref,
            api_key=secrets.get("DASHSCOPE_API_KEY"),
            timeout_s=cfg.timeout_s,
        )
    raise ConfigError(f"未知的多模态嵌入 provider：{cfg.provider}")


def build_providers(settings: Settings, secrets: Secrets | None = None) -> Providers:
    secrets = secrets or Secrets()
    return Providers(
        embed=build_embedding(settings, secrets),
        rerank=build_rerank(settings, secrets),
        llm=build_llm(settings, secrets),
        vision=build_vision(settings, secrets),
        multimodal=build_multimodal(settings, secrets),
    )
