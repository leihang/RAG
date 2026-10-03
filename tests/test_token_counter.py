"""本地 token 计数口径（`02` TC-INDEX-004 的前置件）。

为什么要专门测：`token_count` 决定**分块尺寸判定**、质量报告的 p50/p90、以及成本估算，
而它只是云端 `usage.total_tokens` 的**代理**。代理选错词表会系统性偏差 ——
实测用 OpenAI 的 `cl100k_base` 给 Qwen 系嵌入模型计数，200 段真实文本偏低 **−35%**
（中文散文 +9%、数字/表格行 −42%），换成 Qwen 词表后收敛到 −11%。

这些用例只测**口径选择与降级行为**（纯本地、不花钱）；"与云端真值的偏差"由
`ekbs.tools.token_calibration`（会出网）按 TC-INDEX-004 判定，不在默认测试集里跑。
"""

from __future__ import annotations

import pathlib

import pytest

from ekbs.ingest.chunker import (
    active_token_counter,
    count_tokens,
    reset_tokenizer_cache,
    tokenizer_path,
)


def test_default_counter_is_qwen_when_tokenizer_present() -> None:
    """词表就位时用 Qwen 口径（仓库内 `var/models/qwen-tokenizer.json`）。"""
    if tokenizer_path() is None:
        pytest.skip("本机没放词表文件（部署后跑 ekbs.tools.fetch_tokenizer）")
    assert active_token_counter().startswith("qwen:")


def test_explicit_env_disables_tokenizer_and_falls_back(monkeypatch) -> None:
    """`EKBS_TOKENIZER_PATH` 显式指定即权威：指向不存在的路径 = 主动禁用 → 回退 tiktoken。

    这条同时保证了"降级是可见的"：`active_token_counter()` 会显示实际口径，
    `/health/deps` 也会暴露它，避免词表没就位却悄悄用一个偏差 35% 的口径。
    """
    monkeypatch.setenv("EKBS_TOKENIZER_PATH", "/nonexistent/tokenizer.json")
    # 口径在进程内只解析一次：测试里必须先清缓存，否则会拿到上一条用例已加载的 Qwen
    reset_tokenizer_cache()
    assert tokenizer_path() is None
    assert active_token_counter() == "tiktoken:cl100k_base"
    assert count_tokens("为了保障工程部各班工作交接全面。") > 0


def test_empty_text_is_zero_under_every_counter() -> None:
    assert count_tokens("") == 0
    assert count_tokens(None or "") == 0


def test_counter_is_deterministic_and_positive() -> None:
    text = "值班人员应认真遵守公司的各项规范制度，接班人应提前15分钟到达现场。"
    first = count_tokens(text)
    assert first > 0
    assert count_tokens(text) == first, "同一文本必须稳定（用于尺寸判定）"


def test_qwen_counter_is_smaller_than_cl100k_for_chinese_prose() -> None:
    """Qwen 词表对中文散文的切法比 cl100k 更"整"（这是它更接近云端的原因）。

    注：这条只锁定"两者确属不同口径"这一事实，不作为精度判据 —— 精度必须拿云端真值判
    （`ekbs.tools.token_calibration`）。
    """
    if tokenizer_path() is None:
        pytest.skip("本机没放词表文件")
    tiktoken = pytest.importorskip("tiktoken")
    text = "重庆卓越城市运营服务有限责任公司熙街管理处"
    qwen_count = count_tokens(text)
    cl100k_count = len(tiktoken.get_encoding("cl100k_base").encode(text))
    assert qwen_count != cl100k_count
    assert qwen_count < cl100k_count, "中文散文上 Qwen 词表应比 cl100k 更少 token"


def test_fetch_tool_default_dest_is_under_var_models() -> None:
    """词表是**部署物**不是源码：默认落到 var/models/（已被 .gitignore 覆盖）。"""
    from ekbs.tools.fetch_tokenizer import DEFAULT_DEST

    assert DEFAULT_DEST.parts[-3:] == ("var", "models", "qwen-tokenizer.json")
    assert pathlib.Path("var").exists()


# ---------------------------------------------------------------- 嵌入模型的官方参数边界


def test_embed_dim_must_be_supported_by_the_model() -> None:
    """维度必须落在官方支持集合内 —— 传不支持的值**不会报错**，API 静默按 1024 返回。

    实测（2026-10-03）：`dimensions=1536/2048` → 返回 1024；`256/512/768/1024` 正常。
    若不拦，配置写 1536 而向量是 1024 → C3「禁止混用向量」被踩，且要等检索异常才暴露。
    """
    from ekbs.config import ConfigError, validate_embed_model_settings

    model = "qwen3.7-text-embedding-flash"
    for good in (1024, 768, 512, 256):
        validate_embed_model_settings(model, good, 16)  # 不抛即通过
    for bad in (1536, 2048, 4096):
        with pytest.raises(ConfigError, match="不支持"):
            validate_embed_model_settings(model, bad, 16)


def test_embed_batch_size_cannot_exceed_documented_max_rows() -> None:
    """批量不能超过官方「最大行数」（flash=20；实测 API 容忍 25，但以文档为准）。"""
    from ekbs.config import ConfigError, validate_embed_model_settings

    validate_embed_model_settings("qwen3.7-text-embedding-flash", 1024, 20)
    with pytest.raises(ConfigError, match="最大行数"):
        validate_embed_model_settings("qwen3.7-text-embedding-flash", 1024, 21)
    # v4 更严（10 行）
    with pytest.raises(ConfigError, match="最大行数"):
        validate_embed_model_settings("text-embedding-v4", 1024, 11)


def test_unknown_model_skips_embed_validation() -> None:
    """未知模型（自研/测试 provider）不挡 —— 校验只覆盖官方清单里的模型。"""
    from ekbs.config import validate_embed_model_settings

    validate_embed_model_settings("某个自研模型", 999, 999)


def test_shipped_config_uses_headroom_below_documented_max_rows() -> None:
    """随仓库交付的配置**不顶满**官方上限：batch_size 应 < 官方最大行数。"""
    from ekbs.config import EMBED_MAX_ROWS, load_settings

    settings = load_settings()
    model = settings.models.embed.model
    limit = EMBED_MAX_ROWS.get(model)
    if limit is None:
        pytest.skip(f"{model} 不在官方清单里")
    assert settings.models.embed.batch_size < limit, "运行时留余量，别贴官方上限"


# ---------------------------------------------------------------- 官方词表（2026-10-03）


def test_official_sdk_vocab_is_the_default_counter() -> None:
    """默认口径必须来自**官方 dashscope SDK 自带词表**（随包、离线、与 SDK 同版本）。

    为什么改成它：实测它与 HF 的 Qwen3-Embedding `tokenizer.json` **计数逐条相同**，
    却省掉一个部署物（11 MB 词表）+ 一次下载步骤（`ekbs.tools.fetch_tokenizer` 变为可选）。
    """
    from ekbs.ingest.chunker import sdk_vocab_file

    monkeypatch_free = sdk_vocab_file()
    if monkeypatch_free is None:
        pytest.skip("本机 dashscope 包未带 resources/qwen.tiktoken")
    reset_tokenizer_cache()
    counter = active_token_counter()
    assert counter.startswith("qwen:sdk:"), counter
    assert "qwen.tiktoken" in counter


def test_env_override_still_wins_over_official_vocab(monkeypatch) -> None:
    """`EKBS_TOKENIZER_PATH` 仍是**最高优先级**（可固定到 HF 词表，或指向不存在的路径来禁用）。"""
    monkeypatch.setenv("EKBS_TOKENIZER_PATH", "/nonexistent/tokenizer.json")
    reset_tokenizer_cache()
    assert active_token_counter() == "tiktoken:cl100k_base"


def test_official_and_hf_vocab_agree_when_both_present() -> None:
    """两种词表（官方 `.tiktoken` vs HF `.json`）计数应完全一致 —— 不一致就说明同源假设破了。

    这条是"能不能安心换口径"的证据：本机两者都在时逐条比对。
    """
    import pathlib

    from ekbs.ingest.chunker import _QwenCounter, sdk_vocab_file

    hf_vocab = (
        pathlib.Path(__file__).resolve().parents[1] / "var" / "models" / "qwen-tokenizer.json"
    )
    sdk_vocab = sdk_vocab_file()
    if sdk_vocab is None or not hf_vocab.is_file():
        pytest.skip("本机缺少其中一个词表（官方 .tiktoken 或 var/models/qwen-tokenizer.json）")
    tokenizers = pytest.importorskip("tokenizers")
    from dashscope.tokenizers.qwen_tokenizer import QwenTokenizer

    sdk = _QwenCounter("sdk", sdk_vocab, QwenTokenizer(str(sdk_vocab)))
    hf = _QwenCounter("file", hf_vocab, tokenizers.Tokenizer.from_file(str(hf_vocab)))
    samples = [
        "接班人员未能到岗，值班人员必须坚持岗位，并向领导汇报。",
        "XJWY-GC-01-01 XJ-01-11-1F02 XIWY-QR-GC-002",
        "| 司龄 | 天数 |\n|---|---|\n| 1年 | 5 |",
        "The quick brown fox jumps over the lazy dog.",
        "def handover(shift):\n    return shift",
    ]
    for text in samples:
        assert sdk.count(text) == hf.count(text), f"两种词表不一致：{text[:24]}"
