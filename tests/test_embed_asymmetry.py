"""查询/文档**不对称编码**（DashScope `text_type`）。

## 为什么要有这个开关

查询和文档天生不对称（短问句 vs 长段落），有些模型的训练目标就是"给两类文本不同的编码路径"，
从而让问句更靠近它对应的答案段。DashScope 用 `text_type=query|document` 暴露这件事。

**实测（2026-10-03）**：该参数确实生效 —— `document` 与默认向量的余弦只有 0.916
（不是被忽略）。但**开启会改变全部 chunk 向量**，所以必须：

1. 有开关（默认关，不改变现状）；
2. **进指纹**（`ModelRef.variant` → `#1024+asym`）——否则新旧向量会在同一分片里混用，
   踩 C3「禁止混用向量」，而且 `fingerprint_match` 还显示 True（静默错搜）；
3. **查询向量缓存键也要带指纹** —— 否则开关切换后会命中"另一种编码"算出来的旧向量。

这四条一起才叫"有效而不矛盾"；下面逐个钉住。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from ekbs.cache.store import CacheStore
from ekbs.provider.base import ModelRef, model_ref
from ekbs.provider.dashscope import COMPATIBLE_PATH, DashScopeEmbeddingProvider

EMBED_URL = f"https://dashscope.aliyuncs.com{COMPATIBLE_PATH}/embeddings"


def _provider(*, asymmetric: bool, model: str = "qwen3.7-text-embedding-flash"):
    variant = "asym" if asymmetric else ""
    return DashScopeEmbeddingProvider(
        ref=model_ref("dashscope", model, dim=1024, variant=variant),
        api_key="sk-test",
        retry_base_delay=0.0,
        asymmetric=asymmetric,
    )


def _body(count: int = 1) -> dict:
    return {
        "data": [
            {"embedding": [0.1] * 1024, "index": i, "object": "embedding"} for i in range(count)
        ],
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
        "object": "list",
    }


def test_symmetric_mode_sends_no_text_type() -> None:
    """默认（对称）**不发** `text_type` —— 请求体逐字不变，现有部署不受影响。"""
    with respx.mock:
        route = respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=_body()))
        asyncio.run(_provider(asymmetric=False).embed(["文档片段"]))
        body = json.loads(route.calls[0].request.content)
    assert "text_type" not in body


def test_asymmetric_mode_tags_document_and_query() -> None:
    """开启后：入库走 `document`、查询走 `query`（两侧必须不同，否则等于没开）。"""
    provider = _provider(asymmetric=True)
    with respx.mock:
        route = respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=_body()))
        asyncio.run(provider.embed(["文档片段"]))        # 入库侧
        asyncio.run(provider.embed_query("用户问题"))     # 查询侧
        sent = [json.loads(c.request.content) for c in route.calls]
    assert sent[0]["text_type"] == "document"
    assert sent[1]["text_type"] == "query"


def test_asymmetry_changes_the_fingerprint() -> None:
    """变体必须进指纹：否则新旧向量混用（C3），且 `fingerprint_match` 还显示 True。"""
    symmetric = model_ref("dashscope", "qwen3.7-text-embedding-flash", dim=1024)
    asymmetric = model_ref("dashscope", "qwen3.7-text-embedding-flash", dim=1024, variant="asym")
    assert "+asym" in asymmetric.ident
    assert symmetric.ident != asymmetric.ident
    assert symmetric.fingerprint != asymmetric.fingerprint


def test_query_embedding_cache_key_is_variant_scoped() -> None:
    """查询向量缓存键要按变体隔离，否则开关切换后会命中另一种编码的旧向量。"""
    text = "电梯一年要花多少保养费"
    assert CacheStore.embed_key(text) != CacheStore.embed_key(text, variant="fp-asym")
    assert CacheStore.embed_key(text, variant="fp-a") != CacheStore.embed_key(text, variant="fp-b")


def test_model_ref_default_variant_keeps_ident_unchanged() -> None:
    """不传变体时 `ident` 必须与历史格式完全一致（现有分片指纹不能被动到）。"""
    ref = ModelRef(provider="dashscope", model="qwen3.7-text-embedding-flash",
                   version="qwen3.7-text-embedding-flash", dim=1024)
    assert ref.ident == "dashscope/qwen3.7-text-embedding-flash@qwen3.7-text-embedding-flash#1024"


def test_rollout_policy_local_on_production_template_off() -> None:
    """发布策略：**本机测试环境已开（2026-10-03 A/B 后），生产模板仍关**。

    为什么这条要钉住：开启会改变全部 chunk 向量 ⇒ 必须重灌/重建分片。
    生产模板（`deploy/`）在跑完**完整语料**的 A/B 之前不许悄悄跟着开。
    """
    from ekbs.config import load_settings

    assert load_settings("deploy/config/application.yaml").models.embed.asymmetry is False
    assert load_settings("config/application.cloud.yaml").models.embed.asymmetry is True


def test_migration_tool_can_start_despite_fingerprint_mismatch(settings) -> None:
    """换模型/编码变体后，**修复工具**必须还能启动 —— 否则死锁。

    实测死锁：分片登记的是对称指纹，当前模型是 `+asym` → `Runtime.create` 在
    `load_active(strict=True)` 抛 `EmbeddingFingerprintMismatch`，
    于是服务起不来、`rebuild_shard` 也起不来（连 `--dry-run` 都不行）。
    修法：`Runtime.create(strict_shards=False)` **只给迁移工具**用；服务永远严格。
    """
    import pytest

    from ekbs.db import Database
    from ekbs.errors import EmbeddingFingerprintMismatch
    from ekbs.runtime import Runtime

    db = Database(settings)
    db.apply_schema()
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_shard (shard_id, level, dept_code, dim, embedding_fingerprint, "
            "faiss_path, vector_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("kb_all_L1", 1, "all", 1024, "stale-fingerprint", "var/index/kb_all_L1.faiss", 0, "active"),
        )
        conn.commit()

    with pytest.raises(EmbeddingFingerprintMismatch):
        Runtime.create(settings, service="test", strict_shards=True)

    runtime = Runtime.create(settings, service="test", strict_shards=False)
    try:
        assert runtime.shards.registry(), "注册表仍应可见（工具要靠它列出待重建分片）"
    finally:
        import asyncio

        asyncio.run(runtime.aclose())


def test_bare_string_is_rejected_instead_of_embedded_per_character() -> None:
    """防呆：把裸字符串传给 `embed()` 会被当成"一串文本"**逐字**嵌入 —— 又费钱又错。

    实测踩到过：查询侧误写 `embed(question)`，云端收到的是把问题拆成单字的一批文本，
    返回一堆向量，下游余弦计算直接类型错。
    """
    import pytest

    with pytest.raises(TypeError, match="文本列表"):
        asyncio.run(_provider(asymmetric=False).embed("交接班制度"))  # type: ignore[arg-type]
    # 单条接口照常可用
    with respx.mock:
        respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=_body()))
        assert len(asyncio.run(_provider(asymmetric=False).embed_one("交接班制度"))) == 1024
