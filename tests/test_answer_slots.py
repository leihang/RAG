"""答案模版槽位：解析、回填、— 占位、出口闸门。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.answer_slots import (
    AnswerSlot,
    fallback_slotted_table,
    fill_answer_template,
    parse_answer_template,
    resolve_slot_values,
    verify_slotted_answer,
)


def test_parse_answer_template_and_backfill_slots_from_body() -> None:
    raw = (
        '{"template":"风柜加装温控；预算 [[改造预算]] 元，节约 [[预计年节约电费]] 元。",'
        '"slots":[{"id":"改造预算","op":"extract","columns":["改造预算"]}],'
        '"reason":"方案+数字"}'
    )
    parsed = parse_answer_template(raw)
    assert parsed is not None
    assert "[[改造预算]]" in parsed.template
    # 模版里多出的槽应自动补进 slots
    assert {s.id for s in parsed.slots} == {"改造预算", "预计年节约电费"}


def test_fill_answer_template_uses_dash_for_missing() -> None:
    text = fill_answer_template(
        "预算 [[改造预算]]，回收期 [[投资回收期]] 个月",
        {"改造预算": "22000"},
    )
    assert text == "预算 22000，回收期 — 个月"


def test_resolve_slot_values_from_scheme_kv() -> None:
    content = (
        "改造预算=22,000 元; 预计年节约电费=约 40,848 元; 投资回收期=约 7 个月\n"
    )
    chunk = SimpleNamespace(chunk_id=1, doc_id=236, content=content, chunk_type="table")
    slots = [
        AnswerSlot(id="改造预算", columns=["改造预算"]),
        AnswerSlot(id="预计年节约电费", columns=["预计年节约电费"]),
        AnswerSlot(id="投资回收期", columns=["投资回收期"]),
        AnswerSlot(id="不存在列", columns=["不存在列"]),
    ]
    values = resolve_slot_values(slots, [chunk], prefer_doc_ids=[236])
    assert values["改造预算"] == "22000"
    assert values["预计年节约电费"] == "40848"
    assert values["投资回收期"] == "7"
    assert values["不存在列"] == "—"


def test_verify_slotted_answer_rejects_invented_and_leftover_slots() -> None:
    from ekbs.retrieval.answer_slots import scrub_unverified_numbers

    values = {"改造预算": "22000", "预计年节约电费": "40848"}
    ok = "改造预算 22000 元，年节约 40848 元。"
    assert verify_slotted_answer(ok, values)
    assert not verify_slotted_answer("改造预算 [[改造预算]] 元", values)
    assert not verify_slotted_answer("改造预算 99999 元", values)
    # 年份叙述可放行
    assert verify_slotted_answer("2026年方案预算 22000 元", values)
    # 检索原文里有的电价可放行（模版偶发写死）
    ctx = ["按电价 0.773 元/度计算，预计节约能耗 10%"]
    with_price = "预算 22000 元，电价 0.773 元/度，节约 10%。"
    assert verify_slotted_answer(
        with_price, values, context_snippets=ctx
    )
    # 闸门失败时洗未核定数字，保留正文
    scrubbed = scrub_unverified_numbers(
        "方案：风柜改造。预算 22000，伪造 99999。",
        values,
        context_snippets=ctx,
    )
    assert "风柜改造" in scrubbed
    assert "22000" in scrubbed
    assert "99999" not in scrubbed
    assert "—" in scrubbed


def test_layout_slotted_answer_sections_for_dingtalk_and_ui() -> None:
    from ekbs.retrieval.answer_slots import layout_slotted_answer
    from ekbs.dingtalk.markdown_format import format_for_sample_markdown

    blob = (
        "2026年中央空调节能改造采用物联网技术，计划工期30天。"
        "改造方案包括：对公区风柜在末端出风口加装温度采样装置，保守估计节约能耗10%；"
        "对中央空调主机根据室外温度自动调节出水温度，保守估计主机能耗下降1%；"
        "对冷却塔实施物联网控制改造，保守估计冷却塔能耗降低1%。"
        "改造成本方面，本次改造总成本预估22000元，预算尚余37610元未使用。"
        "节能效益方面，按电价0.773元/度计算，预计每年节约电费约40848元，"
        "投资回收期约7个月。[1]"
    )
    out = layout_slotted_answer(blob)
    assert "【概述】" in out
    assert "【改造方案】" in out
    assert "【改造成本】" in out
    assert "【节能效益】" in out
    assert "- **风柜**" in out or "- **公区风柜**" in out
    assert "22000" in out and "40848" in out and "0.773" in out
    assert "47940" not in out
    # 已排版则不再二次拆
    assert layout_slotted_answer(out) == out
    md = format_for_sample_markdown(out)
    assert "#### 概述" in md
    assert "#### 改造方案" in md
    assert "- **风柜**" in md or "- **公区风柜**" in md


def test_resolve_slot_values_prose_price_and_ratios() -> None:
    from types import SimpleNamespace

    from ekbs.retrieval.answer_slots import AnswerSlot, resolve_slot_values

    content = (
        "相关表头: 改造预算 | 预计年节约电费\n"
        "改造预算=22,000 元; 预计年节约电费=约 40,848 元\n"
        "项目=2025 年用电量（度）; 冷却塔=47,940; 合计=1,283,960\n"
        "预期效果：保守估计节约能耗 10%。主机能耗下降 1%。冷却塔能耗降低 1%。\n"
        "按电价 0.773 元/度计算。该预算尚余 37,610 元未使用。\n"
    )
    chunk = SimpleNamespace(
        chunk_id=1, doc_id=236, content=content, chunk_type="table"
    )
    vals = resolve_slot_values(
        [
            AnswerSlot(id="改造预算", columns=["改造预算"]),
            AnswerSlot(id="电价", columns=["电价"]),
            AnswerSlot(id="预算余额", columns=["预算余额"]),
            AnswerSlot(id="风柜节约比例", columns=["风柜节约比例"]),
            AnswerSlot(id="主机节约比例", columns=["主机节约比例"]),
            AnswerSlot(id="冷却塔节约比例", columns=["冷却塔节约比例"]),
        ],
        [chunk],
        prefer_doc_ids=[236],
    )
    assert vals["改造预算"] == "22000"
    assert vals["电价"] == "0.773"
    assert vals["预算余额"] == "37610"
    assert vals["风柜节约比例"] == "10%"
    assert vals["主机节约比例"] == "1%"
    assert vals["冷却塔节约比例"] == "1%"
    assert "47940" not in vals.values()


def test_fallback_slotted_table_notes_missing() -> None:
    text = fallback_slotted_table({"改造预算": "22000", "投资回收期": "—"})
    assert "改造预算=22000" in text
    assert "投资回收期=—" in text
    assert "未在资料中找到" in text


def test_stated_value_word_goes_prose_not_slots_or_sum() -> None:
    from ekbs.retrieval.structured_query import (
        is_document_stated_value_intent,
        is_numeric_calc_intent,
        is_structured_query_intent,
        wants_document_prose,
    )

    q = "2026年中央空调节能改造，改造方案、成本、节约费用"
    assert is_document_stated_value_intent(q)
    assert wants_document_prose(q)
    assert not is_structured_query_intent(q), "Word/方案走散文 LLM，不进旁路槽位"
    assert not is_numeric_calc_intent(q)
    assert is_numeric_calc_intent("2026年9月公共用电量合计")
