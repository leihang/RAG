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
