"""清洗步骤（`06` §1.7）：公式规整 + 扫描印章/签字噪声 + 中文断字 + OCR 错字。

公式用例仍是主回归：MinerU 把 `100%` 拆成 `$1 0 0 \\%$` 原样入库会搜不到；
真公式（`\\frac` 等）必须保留。印章/断字见 ``test_seal_*`` / ``test_cjk_*``。
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


def test_seal_and_signature_noise_stripped() -> None:
    """扫描合同：公章 OCR / 签署乱码不得进正文（`20` §2 步 2）。"""
    from ekbs.ingest.clean import clean_figure_text

    raw = (
        "本合同经双方盖章之日起生效。\n"
        "ein高g甲方：重庆卓越城市运营服务有限责任公司经办人：日期：24年2月\n"
        "5001080423901\n"
        "乙方：重庆美日宝金制冷设备有限公司\n"
    )
    result = clean_markdown(raw)
    assert "ein高g" not in result.text
    assert "5001080423901" not in result.text
    assert "双方盖章之日起生效" in result.text
    assert "甲方：重庆卓越城市运营服务有限责任公司" in result.text
    assert result.stats.get("sign_latin_removed", 0) >= 1

    # 签署栏：印章残片 + 手写姓名误识不得进检索
    sign = clean_markdown(
        "甲方：重庆卓越城市营服务有限责任公司乙方：重庆欧标水处理工程技术有限公司"
        "食务有限章） 山A重14134197750011授权代表签字： 高信5001 01080423901 署日期：2026年4月2日"
    )
    assert "ein" not in sign.text.lower()
    assert "食务有限章" not in sign.text
    assert "14134197750011" not in sign.text
    assert "高信" not in sign.text
    assert "[手写签字]" in sign.text
    assert "城市运营服务" in sign.text
    assert "食务有限章" not in sign.text
    broken_co = clean_markdown("甲方： E動责任公司经办人：日期：2026年1月1日")
    assert "E動" not in broken_co.text
    assert "有限责任公司" in broken_co.text
    # 条款正文不得被手写占位误伤
    clause = clean_markdown("本责任协议书自签字盖章之日起执行，经授权代表签名或者盖章确认。")
    assert "签字盖章之日起" in clause.text
    assert "授权代表签名或者盖章" in clause.text
    assert "[手写签字]" not in clause.text

    party = clean_markdown("甲方单位：代表人：乙方单位：代表人：")
    assert "乙方单位" in party.text
    assert "[手写签字]" not in party.text
    assert "位：" not in party.text or "乙方单位" in party.text
    # 空白签署栏上的单字噪声不是姓名
    blank = clean_markdown("授权代表签字：高日期：年月日")
    assert "高" in blank.text
    assert "[手写签字]" not in blank.text

    fig = clean_figure_text(
        "甲方单位：\n代表人：唐机\n日期：2026.4.2"
    )
    assert "唐机" not in fig
    assert "[手写签字]" in fig
    assert "甲方单位" in fig

    fig2 = clean_figure_text(
        "甲方：重庆卓越城市运营服务有限责任公司\n"
        "（盖有红色印章，印章内容为“重庆卓越城市运营服务有限责任公司”及编号“5001080423901”）"
    )
    assert "盖有红色印章" not in fig2
    assert "5001080423901" not in fig2
    assert "甲方" in fig2

    account = clean_markdown(
        "乙方：重庆美日宝金制冷设备有限公司开户银行：中国工商银行股份有限公司重庆两路支行"
        "账号：310002240902450123联系人：蒋平\n"
    )
    assert "310002240902450123" in account.text
    assert "开户银行" in account.text


def test_cjk_ocr_gaps_and_typos() -> None:
    raw = "项目名 称=油过滤 器；委托方：有限责仕公司；公司庆两路支行"
    result = clean_markdown(raw)
    assert "项目名称=油过滤器" in result.text
    assert "有限责任公司" in result.text
    assert "公司重庆两路支行" in result.text
    assert "责仕" not in result.text


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
