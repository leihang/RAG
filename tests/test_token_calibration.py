"""token 计数校准（`02` TC-INDEX-004 的落地件）。

为什么值得单测：本地 token 计数是云端 `usage.total_tokens` 的**代理**，偏差实测 −35%（换
Qwen 词表后 −11%），而它决定**分块尺寸**、质量指标与成本估算。校准能不能用，取决于两件事：

1. **真值能不能被记下来** —— 嵌入响应的 `usage.total_tokens` 必须累计，且能按文档"取走"
   （不然会把上一篇的用量算到这一篇头上）；
2. **系数能不能被约束住** —— 必须绑定 `embedding_fingerprint`（换模型自动失效）、
   必须够样本（单篇文档的文本类型可能很偏）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ekbs.ingest import token_calib
from ekbs.provider.dashscope import COMPATIBLE_PATH, DashScopeEmbeddingProvider
from ekbs.provider.base import model_ref
from tests.test_dashscope_provider import _embedding_body, embed_provider

EMBED_URL = f"https://dashscope.aliyuncs.com{COMPATIBLE_PATH}/embeddings"


# ---------------------------------------------------------------- 记录与聚合


def test_records_aggregate_into_a_factor(tmp_path) -> None:
    """系数 = Σ云端 / Σ本地（不是逐篇比例的平均）—— 按量加权才对得上总账。"""
    for index, (cloud, local) in enumerate(((100, 80), (200, 160), (300, 240)), start=1):
        token_calib.record(
            tmp_path, doc_id=index, fingerprint="fp-1", cloud_tokens=cloud, local_tokens=local
        )
    calibration = token_calib.summarize(tmp_path, "fp-1")
    assert calibration is not None
    assert calibration.samples == 3
    assert calibration.factor == pytest.approx(600 / 480)
    assert calibration.calibrated(240) == pytest.approx(300, abs=1)
    assert calibration.deviation_pct == pytest.approx((480 - 600) / 600 * 100)


def test_min_samples_guard_prevents_one_document_from_shifting_the_scale(tmp_path) -> None:
    """只有一两篇时**不写系数**：单篇的文本类型可能很偏（如全是短标题块）。"""
    token_calib.record(tmp_path, doc_id=1, fingerprint="fp", cloud_tokens=100, local_tokens=50)
    assert token_calib.summarize(tmp_path, "fp") is None
    assert token_calib.calibrated_tokens(50, tmp_path, "fp") == 50  # 无系数=原样


def test_factor_is_bound_to_fingerprint(tmp_path) -> None:
    """换嵌入模型（指纹变）后旧系数必须失效 —— 否则用上个模型的系数算这个模型的 token。"""
    for index in range(3):
        token_calib.record(
            tmp_path, doc_id=index, fingerprint="fp-old", cloud_tokens=100, local_tokens=50
        )
    calibration = token_calib.summarize(tmp_path, "fp-old")
    assert calibration is not None
    token_calib.write_factor(tmp_path, calibration)

    assert token_calib.load(tmp_path, "fp-old") is not None
    assert token_calib.load(tmp_path, "fp-new") is None, "指纹不符必须失效"
    assert token_calib.calibrated_tokens(50, tmp_path, "fp-new") == 50


def test_invalid_or_empty_records_are_skipped(tmp_path) -> None:
    """0 值与坏行不能污染系数（云端没上报 usage 时值为 0）。"""
    assert token_calib.record(tmp_path, doc_id=1, fingerprint="fp", cloud_tokens=0,
                             local_tokens=10) is None
    assert token_calib.record(tmp_path, doc_id=2, fingerprint="fp", cloud_tokens=10,
                             local_tokens=0) is None
    for index in range(3):
        token_calib.record(tmp_path, doc_id=index + 3, fingerprint="fp",
                           cloud_tokens=100, local_tokens=50)
    path = token_calib.records_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{坏行\n")
    calibration = token_calib.summarize(tmp_path, "fp")
    assert calibration is not None and calibration.samples == 3


# ---------------------------------------------------------------- 真值记账


@respx.mock
async def test_embedding_usage_is_accumulated_and_popped_per_document() -> None:
    """云端真值必须能累计、也能"取走清零" —— 按文档记账才不会张冠李戴。"""
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=_embedding_body(2)))
    provider = embed_provider()
    assert provider.pop_usage() == (0, 0)

    await provider.embed(["年假规定", "考勤规定"])
    total, calls = provider.pop_usage()
    assert total == 23 and calls == 1, "应记录云端 usage.total_tokens"
    assert provider.pop_usage() == (0, 0), "取走后必须清零"

    await provider.embed(["再嵌一次"])
    assert provider.pop_usage()[1] == 1


@respx.mock
async def test_missing_usage_does_not_break_embedding() -> None:
    """云端没返回 usage 时不能影响嵌入本身（只是记不到真值）。"""
    body = _embedding_body(1)
    body.pop("usage")
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=body))
    provider = embed_provider()
    vectors = await provider.embed(["年假规定"])
    assert vectors and len(vectors[0]) == 1024
    assert provider.pop_usage() == (0, 0)


def test_provider_usage_defaults_are_per_instance() -> None:
    """两个 provider 实例的账必须独立（避免一个实例的用量串到另一个）。"""
    first = DashScopeEmbeddingProvider(
        ref=model_ref("dashscope", "text-embedding-v4", dim=1024), api_key="k"
    )
    second = DashScopeEmbeddingProvider(
        ref=model_ref("dashscope", "text-embedding-v4", dim=1024), api_key="k"
    )
    first.usage_total = 7
    assert first.pop_usage() == (7, 0)
    assert second.pop_usage() == (0, 0)
