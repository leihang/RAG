"""清洗步骤（`06` §1.7）—— 目前实现的是「公式/单位残留规整」。

为什么值得单独测：MinerU 走数学模式会把 `100%` 输出成 `$1 0 0 \\%$`，**原样入库就再也搜不到**。
这不是美观问题，是**关键词那一路直接召不回**（用户搜「覆盖率100%」，库里是 `1 0 0 \\%`）。
同时必须保证**真公式一律不动** —— 宁可保留 LaTeX，也不能把 `\\frac{1}{2}` 改成乱码。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ekbs.ingest.clean import clean_markdown, normalize_math_in_text
from ekbs.ingest.table_serialize import serialize_information_blocks


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # 实测样例：MinerU 把百分号、千分号拆成字符并转义
        ("安全培训覆盖率 $1 0 0 \\%$ ，合格率", "安全培训覆盖率 100% ，合格率"),
        ("$6 \\%$", "6%"),
        ("$5 0 \\%$", "50%"),
        ("$100 \\text{‰}$", "100‰"),
        ("温度 $2 5 \\text{ ℃}$", "温度 25℃"),
        ("增值税 $1 3 \\%$ 另加", "增值税 13% 另加"),
    ],
)
def test_math_that_is_really_a_number_is_unwrapped(raw: str, want: str) -> None:
    got, used, _kept = normalize_math_in_text(raw)
    assert got == want
    assert used == 1


@pytest.mark.parametrize(
    "raw",
    [
        "$\\frac{1}{2}$ 的份额",
        "$x^2 + y^2 = z^2$",
        "$\\alpha$ 系数",
        "$\\sum_{i=1}^{n} x_i$",
        "$m$",  # 单个字母：可能是单位也可能是变量 → 保守保留
    ],
)
def test_real_formulas_are_left_untouched(raw: str) -> None:
    """真公式必须原样保留 —— 规整只针对「被拆散的数字+单位」。"""
    got, used, kept = normalize_math_in_text(raw)
    assert got == raw
    assert used == 0 and kept == 1


def test_text_without_math_is_unchanged() -> None:
    text = "这是一段没有公式的话，价格 19000 元。"
    assert normalize_math_in_text(text)[0] == text


def test_multiple_spans_and_idempotency() -> None:
    text = "用电 $4 2 . 6 \\%$ ，电费 $5 0 \\%$ ，公式 $E=mc^2$"
    once, used, kept = normalize_math_in_text(text)
    assert once == "用电 42.6% ，电费 50% ，公式 $E=mc^2$"
    assert (used, kept) == (2, 1)
    assert normalize_math_in_text(once)[0] == once, "规整必须幂等（重复入库不抖动）"


def test_clean_markdown_reports_stats() -> None:
    result = clean_markdown("覆盖率 $1 0 0 \\%$")
    assert result.text == "覆盖率 100%"
    assert result.stats == {"math_unwrapped": 1, "math_preserved": 0}
    assert clean_markdown("没有公式").stats == {}


def test_native_table_cells_are_normalized_too() -> None:
    """xlsx/docx 的原生表格不经 Markdown 那一路，单元格也要规整（否则同样的 LaTeX 会漏网）。"""
    blocks = serialize_information_blocks(
        caption="考核",
        rows=[["项目", "比例"], ["安全培训覆盖率", "$1 0 0 \\%$"]],
    )
    # 行式格式是 `列名=值` 成对：表头「比例」那列的值被规整成 100%
    assert "比例=100%" in blocks[0]
    assert "项目=安全培训覆盖率" in blocks[0]
    assert "$" not in blocks[0]


async def test_ingest_stores_cleaned_text(seeded) -> None:
    """接线断言：入库后**库里存的**就是清洗后的文本（不是只在单元测试里好看）。"""
    from ekbs.ingest.pipeline import IngestPipeline

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    result = await pipeline.ingest_markdown(
        title="清洗自检.md",
        markdown="# 安全\n\n安全培训覆盖率 $1 0 0 \\%$ ，事故率 $0 \\%$。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        source_path="清洗自检.md",
        real_corpus=False,
    )
    assert result.status == "published", result.status
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT content FROM ekbs_chunk WHERE doc_id = ?", (result.doc_id,)
        ).fetchall()
    joined = "\n".join(str(r["content"]) for r in rows)
    assert "100%" in joined and "0%" in joined
    assert "\\%" not in joined, "清洗后不应再有 LaTeX 转义残留"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # 合同里的门槛写法：`$\geqslant 9 0$` —— 用户问「合格分数线」时，
        # 搜 `≥90` 搜不到、取数也核不出这个标签（实测 doc 42 维保合同）
        ("$\\geqslant 9 0$ 分为合格", "≥90 分为合格"),
        ("$< 9 0$ 分为不合格", "<90 分为不合格"),
        ("$\\leq 3 0$ 天", "≤30 天"),
        ("直梯 ${ > } 5$ 层站", "直梯 >5 层站"),
    ],
)
def test_comparison_macros_become_symbols(raw: str, want: str) -> None:
    """比较符宏 → 符号（合同门槛是最常被取数的数字之一，不能留在 LaTeX 里）。"""
    assert normalize_math_in_text(raw)[0] == want
