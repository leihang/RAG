"""取数模式（`mode="numeric"`）—— 类型锁死 + 抄写校验（课程 19 口径）。

三层都要测，因为这条路的**全部保证**就靠这三层：

1. 解析层：千分位 / 欧陆小数 / 括号负数；万、千不换算。畸形输出一律 N/A；
2. 校验层：**值必须逐字来自它引用的原文片段** —— 这是比"事后对账"更强的结构验证；
3. 语义层：拒答必须与业务问答一致（`refused=True` + `citations=[]`），
   且两种模式**互不串答案**（缓存键含 mode）。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from ekbs.provider.base import LLMResult
from ekbs.retrieval.numeric import (
    normalize_number,
    parse_numeric_payload,
    render_answer,
    verify_copied,
)
from tests.conftest import user_id_of


def _chunk(content: str) -> SimpleNamespace:
    """分块替身。

    字段要**够全**：注入到召回结果里的替身会被引用构建（`_post_check_citations`）读到
    `page_from`/`page_to` 等，缺字段会在下游炸成 AttributeError（实测踩到）。
    """
    return SimpleNamespace(
        content=content,
        doc_title="t",
        doc_version="v1",
        heading_path="h",
        chunk_id=1,
        doc_id=1,
        page_from=None,
        page_to=None,
        rerank_score=None,
        image_path="",
        figure_id=None,
        shard_id="kb_all_L1",
    )


# ---------------------------------------------------------------- 解析层


@pytest.mark.parametrize(
    ("raw", "unit", "want"),
    [
        ("58,3", "%", 58.3),                 # 欧陆式逗号当小数点
        ("(2,124,837)", "CHF", -2124837.0),  # 括号 = 负数
        (4970.5, "千美元", 4970.5),
        ("1.2", "百万", 1.2),
        ("1,234,567", "元", 1234567.0),      # 千分位
        ("5", "天", 5.0),
    ],
)
def test_number_format_traps(raw, unit, want) -> None:
    assert normalize_number(raw, unit) == pytest.approx(want)


@pytest.mark.parametrize("raw", ["N/A", "", "无", "未知", "abc", None])
def test_unnormalizable_is_na(raw) -> None:
    assert normalize_number(raw, "") is None


def test_parse_tolerates_fence_and_noise() -> None:
    text = '好的：\n```json\n{"value": "19000", "unit": "元", "quote": "合同总价￥19000元"}\n```'
    parsed = parse_numeric_payload(text)
    assert parsed.value == 19000
    assert parsed.unit == "元"


@pytest.mark.parametrize("bad", ["这不是 JSON", '{"value": null}', "", "{}"])
def test_malformed_output_is_na(bad) -> None:
    assert parse_numeric_payload(bad).is_na


# ---------------------------------------------------------------- 校验层


def test_verify_accepts_true_copy() -> None:
    parsed = parse_numeric_payload(
        json.dumps({"value": "439200.00", "unit": "元", "quote": "维保费用439200.00元"})
    )
    ok, index = verify_copied(parsed, [_chunk("……维保费用439200.00元（大写：肆拾叁万玖仟贰佰元整）")])
    assert ok is True and index == 1


def test_verify_rejects_tampered_value() -> None:
    """片段是真的、数字被改了 —— 这正是"编数字"最典型的形态，必须拦下。"""
    parsed = parse_numeric_payload(
        json.dumps({"value": "888888", "unit": "元", "quote": "维保费用439200.00元"})
    )
    assert verify_copied(parsed, [_chunk("……维保费用439200.00元……")]) == (False, None)


def test_verify_rejects_fabricated_quote() -> None:
    """片段是编的（原文里根本没有）→ 也要拦下，否则等于绕过校验。"""
    parsed = parse_numeric_payload(
        json.dumps({"value": "439200.00", "unit": "元", "quote": "维保费用439200.00元"})
    )
    assert verify_copied(parsed, [_chunk("完全无关的另一段文字")]) == (False, None)


def test_verify_rejects_scaled_value() -> None:
    """原文写「1.2 万」，模型折成 12000 —— 这是换算，必须拦下。"""
    parsed = parse_numeric_payload(
        json.dumps({"value": 12000, "unit": "度", "quote": "当月用电量 1.2 万度"})
    )
    assert verify_copied(parsed, [_chunk("报告：当月用电量 1.2 万度")]) == (False, None)


def test_verify_keeps_original_surface() -> None:
    parsed = parse_numeric_payload(
        json.dumps({"value": 1.2, "unit": "度", "quote": "当月用电量 1.2 万度", "indicator": "用电量"})
    )
    ok, index = verify_copied(parsed, [_chunk("报告：当月用电量 1.2 万度")])
    assert ok is True and index == 1
    assert parsed.surface == "1.2 万"
    assert render_answer(parsed, 1) == "用电量：1.2 万 度 [1]"


def test_render_avoids_scientific_notation() -> None:
    """大数不能渲染成 2.6087e+07（人读的答案文本会进审计，也给人看）。"""
    parsed = parse_numeric_payload(json.dumps({"value": 26087023, "unit": "度", "indicator": "总用电量"}))
    assert render_answer(parsed, 1) == "总用电量：26,087,023 度 [1]"


def test_render_includes_page_and_marker() -> None:
    parsed = parse_numeric_payload(
        json.dumps({"value": 19000, "unit": "元", "page": 3, "indicator": "空调保养合同总价"})
    )
    assert render_answer(parsed, 2) == "空调保养合同总价：19,000 元（第 3 页） [2]"


# ---------------------------------------------------------------- 语义层（走管线）


async def _with_stub_llm(runtime, text: str, fn):
    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            return LLMResult(text=text, model="numeric-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        return await fn()
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]


async def _real_quote(runtime, alice, question: str) -> tuple[str, str]:
    """从真实召回的块里挑一个数字和它周围的片段，作为"抄写"的对象。"""
    retrieval = await runtime.pipeline.retrieve(question, alice)
    for chunk in retrieval.chunks:
        match = re.search(r"\d+(?:\.\d+)?", chunk.content or "")
        if match:
            start = max(0, match.start() - 10)
            return match.group(0), chunk.content[start : match.end() + 10].strip()
    pytest.skip("demo 语料未召回到含数字的块")


@pytest.mark.asyncio
async def test_numeric_mode_returns_verified_value(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")

    payload = {"value": value, "unit": "天", "page": 1, "quote": quote, "indicator": "年假天数"}
    result = await _with_stub_llm(
        runtime,
        json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False),
    )
    assert result.refused is False
    assert result.citations, "取数成功必须给出出处（否则与拒答无异）"
    assert result.numeric and result.numeric["verified"] is True
    assert str(value) in result.answer


@pytest.mark.asyncio
async def test_numeric_mode_na_refuses_without_citations(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    text = json.dumps({"value": "N/A", "reason": "资料没有直接给出该指标"}, ensure_ascii=False)
    result = await _with_stub_llm(
        runtime, text,
        lambda: runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False),
    )
    assert result.refused is True
    assert result.refusal_reason == "numeric_na"
    assert result.citations == [], "N/A 必须连引用一起清空（不留「有答案没出处」的中间态）"


@pytest.mark.asyncio
async def test_numeric_mode_unverified_value_refuses(seeded) -> None:
    """值不在它引用的片段里 → 拒答。这是本模式的核心防线。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    _, quote = await _real_quote(runtime, alice, "年假有多少天")
    payload = {"value": 999999, "unit": "天", "quote": quote, "indicator": "年假天数"}
    result = await _with_stub_llm(
        runtime,
        json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False),
    )
    assert result.refused is True
    assert result.refusal_reason == "numeric_unverified"
    assert result.citations == []


@pytest.mark.asyncio
async def test_prose_mode_untouched_by_numeric_mode(seeded) -> None:
    """显式 prose（或测试夹具关闭 auto_numeric）时答案形态与 faithfulness 不变。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await _with_stub_llm(
        runtime, "年假 5 天 [1]。",
        lambda: runtime.pipeline.answer(
            "年假有多少天", alice, mode="prose", use_cache=False
        ),
    )
    assert result.numeric is None, "业务问答不应带取数结构"
    assert result.mode_used == "prose"
    assert result.faithfulness.get("reason") != "numeric_copy_verified"


# ---------------------------------------------------------------- 格式修复（防线 5）


async def test_parse_error_triggers_one_repair_retry(seeded) -> None:
    """格式失败 → **只重试一次**，且重试时把 response_format 一起带上。

    课程防线 5 的"二次重解析"：模型偶发不吐 JSON 时，再要一次就好 ——
    但不该无限重试，也不该在"模型按规则返回 N/A"时重试（那是正常结果）。
    """
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")
    good = json.dumps({"value": value, "unit": "天", "quote": quote, "indicator": "年假天数"}, ensure_ascii=False)

    calls: list[dict] = []

    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            calls.append(kwargs)
            text = "抱歉，我先解释一下……" if len(calls) == 1 else good
            return LLMResult(text=text, model="numeric-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        result = await runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False)
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]

    assert len(calls) == 2, "格式失败应恰好重试一次"
    assert all(c.get("response_format") == {"type": "json_object"} for c in calls)
    assert result.refused is False and result.numeric and result.numeric["verified"] is True


async def test_na_does_not_trigger_retry(seeded) -> None:
    """模型按规则返回 N/A 是**正常结果**，不该重试（否则每次 N/A 都多烧一次调用）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    calls = 0

    class FakeLLM:
        ref = runtime.providers.llm.ref

        async def generate(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return LLMResult(text='{"value": "N/A", "reason": "资料没有"}', model="numeric-test")

    original = runtime.pipeline.providers.llm
    runtime.pipeline.providers.llm = FakeLLM()  # type: ignore[assignment]
    try:
        result = await runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False)
    finally:
        runtime.pipeline.providers.llm = original  # type: ignore[assignment]

    assert calls == 1
    assert result.refused is True and result.refusal_reason == "numeric_na"


def test_response_format_only_when_asked() -> None:
    """`_body` 只在显式传入时带 response_format —— 既有请求体逐字不变。"""
    from ekbs.provider.dashscope import OpenAICompatibleLLMProvider
    from ekbs.provider.base import ModelRef

    provider = OpenAICompatibleLLMProvider(
        ref=ModelRef(provider="dashscope", model="qwen-max"),
        api_key="k", base_url="https://example.invalid",
        temperature=0.1, top_p=0.8, max_tokens=16, timeout_s=5, max_retries=0, fallback=None,
    )
    plain = provider._body([{"role": "user", "content": "hi"}], stream=False, model="qwen-max")
    assert "response_format" not in plain, "默认不能带这个字段（否则所有既有调用都被改）"
    asked = provider._body(
        [{"role": "user", "content": "hi"}], stream=False, model="qwen-max",
        response_format={"type": "json_object"},
    )
    assert asked["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------- 图片旁路：别重复付费


def test_mm_embed_key_is_namespaced_apart_from_text_embed() -> None:
    """多模态查询向量的缓存键必须与文本嵌入**分开命名空间**。

    两者维度不同（1152 vs 1024），混用同一个键会把错误的向量喂进错误的索引 ——
    这种错误不会报错，只会静默搜不准。
    """
    from ekbs.cache.store import CacheStore

    assert CacheStore.mm_embed_key("同一个问题") != CacheStore.embed_key("同一个问题")
    assert CacheStore.mm_embed_key("x") == CacheStore.mm_embed_key("x")


async def test_multimodal_query_vector_is_cached(seeded) -> None:
    """同一问题重复检索，多模态查询向量只应打**一次**云端。

    这条曾经没有：文本向量走了 `query_embedding_ttl` 缓存，多模态那条每次直连 →
    连续提问把低配额端点打成持续 429（实测 858 次调用 / 882 次 429）。
    缓存本身不是可选优化，是"别重复付费"。
    """
    runtime, _ = seeded
    if runtime.pipeline.figure_index is None:
        pytest.skip("测试运行没有多模态索引")
    dim = int(runtime.pipeline.figure_index.dim)
    calls = 0

    class CountingMM:
        class _Ref:
            provider = "dashscope"
            model = "fake-mm"
        ref = _Ref()

        async def embed_text(self, text: str):
            nonlocal calls
            calls += 1
            # 维度必须与索引一致：维度不符时**刻意不缓存**（那是另一条用例）
            return [0.1] * dim

    mm = CountingMM()
    original = runtime.providers.multimodal
    runtime.providers.multimodal = mm  # type: ignore[assignment]
    try:
        first = await runtime.pipeline._multimodal_query_vector("年假有多少天")
        second = await runtime.pipeline._multimodal_query_vector("年假有多少天")
    finally:
        runtime.providers.multimodal = original  # type: ignore[assignment]

    assert calls == 1, "第二次必须命中缓存"
    assert first == second


async def test_multimodal_dim_mismatch_is_not_cached(seeded) -> None:
    """维度不符的向量**不能进缓存**（否则会把错的向量缓存一整天）。"""
    runtime, _ = seeded

    class BadMM:
        class _Ref:
            provider = "dashscope"
            model = "bad-mm"
        ref = _Ref()

        async def embed_text(self, text: str):
            return [0.1, 0.2]  # 维度远小于 figure_index.dim

    original = runtime.providers.multimodal
    original_index = runtime.pipeline.figure_index
    runtime.providers.multimodal = BadMM()  # type: ignore[assignment]
    try:
        if original_index is None:
            pytest.skip("测试运行没有多模态索引")
        got = await runtime.pipeline._multimodal_query_vector("只会命中同一个问题一次-维度错")
        assert got is None
        from ekbs.cache.store import CacheStore
        assert await runtime.pipeline.cache.get(CacheStore.mm_embed_key("只会命中同一个问题一次-维度错")) is None
    finally:
        runtime.providers.multimodal = original  # type: ignore[assignment]


# ---------------------------------------------------------------- 口径结构化（防线 3 的结构侧）


def test_scope_must_be_verifiable_from_the_cited_chunk() -> None:
    """口径必须能从引用的分块里逐字核到 —— 编出来的口径要拒，空口径放行。

    为什么单独核：数字抄对了、口径是编的（如把 2024 年说成 2026 年），
    等于**答非所问还显得权威**，比错的数字更难被发现（README §四 第 79 条）。
    """
    from ekbs.retrieval.numeric import verify_scope

    chunks = [_chunk("2026年\n总用电/度: 26087023")]
    ok = parse_numeric_payload(json.dumps({"value": 26087023, "scope": "2026年"}))
    assert verify_scope(ok, chunks, 1) is True

    empty = parse_numeric_payload(json.dumps({"value": 26087023, "scope": ""}))
    assert verify_scope(empty, chunks, 1) is True, "资料里确实没有限定词时留空应放行"

    faked = parse_numeric_payload(json.dumps({"value": 26087023, "scope": "2024年"}))
    assert verify_scope(faked, chunks, 1) is False, "口径不在引用分块里 → 必须拒"


def test_scope_alternatives_surface_the_other_row() -> None:
    """同一行标签下的其它数值要被披露出来（这正是"2024 vs 2026"那个实测事故）。"""
    from ekbs.retrieval.numeric import find_scope_alternatives

    chunks = [
        _chunk("2026年\n总用电/度: 26087023\n商铺住宅用电/度: 22149863"),
        _chunk("2024年\n总用电/度: 25178448\n商铺住宅用电/度: 20840408"),
    ]
    chunks[0].chunk_id, chunks[1].chunk_id = 11, 12
    alts = find_scope_alternatives(chunks, quote="总用电/度: 26087023", value=26087023)
    assert [a["value"] for a in alts] == [25178448.0]
    assert alts[0]["label"] == "总用电/度"


def test_scope_alternatives_ignore_same_value_repeats() -> None:
    """同一行在多处重复出现（值相同）不算歧义 —— 否则每个答案都会挂一堆"另有"。"""
    from ekbs.retrieval.numeric import find_scope_alternatives

    same = _chunk("总用电/度: 26087023")
    same.chunk_id = 2
    assert find_scope_alternatives(
        [_chunk("2026年\n总用电/度: 26087023"), same], quote="总用电/度: 26087023", value=26087023
    ) == []


def test_render_includes_scope() -> None:
    parsed = parse_numeric_payload(
        json.dumps({"value": 26087023, "unit": "度", "scope": "2026年", "indicator": "总用电量"})
    )
    assert render_answer(parsed, 3) == "2026年 总用电量：26,087,023 度 [3]"


async def test_numeric_mode_refuses_fabricated_scope(seeded) -> None:
    """管线上：口径核不过 → 拒答（引用同样清空）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")
    payload = {"value": value, "unit": "天", "quote": quote, "scope": "公元前1999年"}
    result = await _with_stub_llm(
        runtime,
        json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False),
    )
    assert result.refused is True
    assert result.refusal_reason == "numeric_invalid_scope"
    assert result.citations == []


async def test_numeric_mode_refuses_scope_alternatives(seeded, monkeypatch) -> None:
    """P1：同标签存在别的数值且问题未钉口径 → 拒答并反问（不放出任一数当唯一答案）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")

    monkeypatch.setattr(
        "ekbs.retrieval.pipeline.find_scope_alternatives",
        lambda *a, **k: [
            {"value": 25178448.0, "label": "总用电/度", "chunk_id": 12,
             "doc_title": "能耗分析", "page": 4}
        ],
    )
    payload = {"value": value, "unit": "天", "quote": quote, "scope": ""}
    result = await _with_stub_llm(
        runtime, json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer("年假有多少天", alice, mode="numeric", use_cache=False),
    )
    assert result.refused is True
    assert result.refusal_reason == "numeric_scope_ambiguous"
    assert result.citations == []
    assert "多个口径" in result.answer
    assert result.numeric and result.numeric.get("alternatives")


def test_resolve_answer_mode_auto_and_explicit() -> None:
    from ekbs.retrieval.numeric import resolve_answer_mode

    assert resolve_answer_mode("numeric", "factual") == "numeric"
    assert resolve_answer_mode("prose", "numeric") == "prose"
    assert resolve_answer_mode("auto", "numeric", auto_numeric=True) == "numeric"
    assert resolve_answer_mode("auto", "factual", auto_numeric=True) == "prose"
    assert resolve_answer_mode("auto", "numeric", auto_numeric=False) == "prose"


async def test_auto_mode_routes_numeric_intent(seeded, monkeypatch) -> None:
    """mode=auto + 取数意图 → 实际走 numeric（mode_used）。"""
    runtime, _ = seeded
    runtime.settings.qa = {**(runtime.settings.qa or {}), "auto_numeric": True}
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")
    payload = {"value": value, "unit": "天", "quote": quote, "scope": ""}
    result = await _with_stub_llm(
        runtime, json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer(
            "年假有多少天", alice, mode="auto", use_cache=False
        ),
    )
    assert result.intent == "numeric"
    assert result.mode_used == "numeric"
    assert result.refused is False
    assert "依据：" in result.answer or result.numeric


def test_question_pinning_suppresses_ambiguity_noise() -> None:
    """问题自带口径且与返回口径相符 → 不再提示"另有…"（用户已指明哪一行）。

    只做"相符就收声"：问题说 2026 而返回 2024 时披露照旧 ——
    那种情形更需要用户看见；模型到底选错没有属语义判断，不由规则断言。
    """
    from ekbs.retrieval.numeric import question_pins_scope

    assert question_pins_scope("2026年总用电量是多少度", "2026年") is True
    assert question_pins_scope("第4页的用电量", "第 4 页 用电分析表") is True
    assert question_pins_scope("2025年3月的电费", "2025年") is True
    # 问题没提口径 → 照旧披露（这正是最初那次 2024/2026 混答的场景）
    assert question_pins_scope("总用电量是多少度", "2026年") is False
    # 问题提的口径与返回口径不符 → 也照旧披露
    assert question_pins_scope("2024年总用电量", "2026年") is False
    assert question_pins_scope("电梯维保一年总费用", "") is False


async def test_pinned_question_does_not_disclose_alternatives(seeded, monkeypatch) -> None:
    """管线上：问题钉了口径 → 不披露、也不打降级标记。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    value, quote = await _real_quote(runtime, alice, "年假有多少天")
    monkeypatch.setattr(
        "ekbs.retrieval.pipeline.find_scope_alternatives",
        lambda *a, **k: [{"value": 999999.0, "label": "x", "chunk_id": 1, "doc_title": "t", "page": 1}],
    )
    payload = {"value": value, "unit": "天", "quote": quote, "scope": "2026年"}
    result = await _with_stub_llm(
        runtime, json.dumps(payload, ensure_ascii=False),
        lambda: runtime.pipeline.answer(
            "2026年的年假有多少天", alice, mode="numeric", use_cache=False
        ),
    )
    assert result.numeric and not result.numeric.get("alternatives")
    assert not any(d.startswith("numeric_scope_ambiguity") for d in result.degraded)


def test_verify_accepts_row_table_separator_variants() -> None:
    """Excel 行级块 `单价（元）=3.456` 与模型 quote `单价（元） 3.456` 应视为同一原文。"""
    parsed = parse_numeric_payload(json.dumps({
        "value": "3.456", "unit": "元", "quote": "单价（元） 3.456",
        "indicator": "9月用气单价",
    }))
    ok, index = verify_copied(parsed, [_chunk(
        "表名: 燃气分析\n相关表头: 类别 | 2026年9月用量 | 单价（元） | 金额\n"
        "类别=食堂7259882331; 2026年9月用量=181; 单价（元）=3.456; 金额=625.536"
    )])
    assert ok is True and index == 1


def test_verify_scope_accepts_multi_part_scope() -> None:
    """口径分散在表名/表头/数据行时，应按组件核对，而不是整串连续子串。"""
    from ekbs.retrieval.numeric import NumericAnswer, verify_scope
    chunk = _chunk(
        "表名: 燃气分析（熙街气分析）\n"
        "相关表头: 类别 | 2026年9月用量 | 单价（元） | 金额\n"
        "类别=食堂7259882331; 2026年9月用量=181; 单价（元）=3.456"
    )
    answer = NumericAnswer(value=3.456, unit="元", quote="单价（元） 3.456",
                          scope="2026年9月 燃气分析（熙街气分析） 食堂7259882331")
    assert verify_scope(answer, [chunk], 1) is True
