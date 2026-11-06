"""清洗步骤（`06` §1.7）：公式规整 + 扫描印章/签字噪声 + 中文断字 + OCR 错字。

公式用例仍是主回归：MinerU 把 `100%` 拆成 `$1 0 0 \\%$` 原样入库会搜不到；
真公式（`\\frac` 等）必须保留。印章/断字见 ``test_seal_*`` / ``test_cjk_*``。
"""

from __future__ import annotations


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


def test_figure_handwrite_and_watermark_captions_demoted() -> None:
    """PPT 现场照图注：手写误识 / 水印相机壳不得原样进检索（doc 221）。"""
    from ekbs.ingest.clean import clean_figure_text, is_low_quality_figure_caption

    handwrite = (
        "正常正常是 2026.10.26 已更换·是决\n"
        "正常正常是\n正常正常是 2026.10.26 黄伟\n"
        "正常是 2026.10.26 未更换漏保已告知家长·孙纳\n"
        "正常是 2026.10.26 未更换漏保已告知家长·杨梦\n"
    )
    assert is_low_quality_figure_caption(handwrite)
    assert clean_figure_text(handwrite) == "[手写表格照片]"

    watermark = (
        "[图片] 物业巡检水印\n2026.10.13 星期二 15:39\n天气：晴 26℃\n"
        "巡检人：请输入巡检人\n问题备注：\n地点：重庆市沙坪坝区虎溪街道美丽时光\n"
        "DIY型多用户电表\n03 000000\n长兴街\n…马克…\n水印枢机\n真实时间\n"
        "防伪 TUEZAP559QHM2"
    )
    assert is_low_quality_figure_caption(watermark)
    assert clean_figure_text(watermark) == "[现场照片]"

    # 长告知牌：含水印壳但正文足够 → 保留
    plaque = (
        "重庆·熙街\n生化池有限空间安全风险告知牌\n"
        "主要危险有害因素：缺氧、有毒气体、易燃易爆\n"
        "安全防范措施：（一）严格履行作业审批\n"
        "（二）作业前必须进行通风换气和气体检测\n"
        "应急处置措施：发现异常立即停止作业\n"
        "报警急救电话：119、120\n"
        + ("措施细则补充。\n" * 8)
        + "今日水印\n防伪 ABC123"
    )
    assert not is_low_quality_figure_caption(plaque)
    assert "生化池" in clean_figure_text(plaque)


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


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        # 全库 38 份解析稿里 13 份命中过：正文里的「字母+汉字」被当签署栏乱码删掉
        ("3.4 按ABC分级分类", "按ABC分级分类"),
        ("A级为重要，能耗占比最高收益影响大。", "A级为重要"),
        ("A栋电梯每周检查一次。", "A栋电梯"),
        ("GB标准执行", "GB标准"),
        ("MALL营业时间调整", "MALL营业"),
        ("POP展架摆放规范", "POP展架"),
        ("POS机刷卡", "POS机"),
        ("ZR阻燃电缆", "ZR阻燃"),
        ("BIM手册", "BIM手册"),
    ],
)
def test_latin_cjk_mix_in_body_is_not_signature_noise(raw: str, kept: str) -> None:
    """正文不是签署栏：字母+汉字是正常混排，不得被 `_SIGN_LATIN_JUNK` 删掉。"""
    result = clean_markdown(raw)
    assert kept in result.text, f"正文被误删：{raw!r} -> {result.text!r}"
    assert result.stats.get("sign_latin_removed", 0) == 0


def test_signature_line_latin_junk_still_stripped() -> None:
    """签署行上的乱码仍要剥；关键词集合不能照抄 `_SIGN_LINE`（它只认「盖章）」等后缀）。"""
    party = clean_markdown(
        "甲方：ein高g重庆卓越城市运营服务有限责任公司经办人：日期：24年2月"
    )
    assert "ein高g" not in party.text
    assert "重庆卓越城市运营服务有限责任公司" in party.text
    assert party.stats.get("sign_latin_removed", 0) >= 1

    broken = clean_markdown("乙方：E動责任公司经办人：日期：2026年1月1日")
    assert "E動" not in broken.text

    sign = clean_markdown("授权代表签字：高信5001 01080423901 署日期：2026年4月2日")
    assert "高信" not in sign.text
    assert "01080423901" not in sign.text
    assert "[手写签字]" in sign.text
    assert "签署日期" in sign.text


def test_signature_cleaning_is_idempotent() -> None:
    """同一份混合文本跑两次 clean_markdown，第二次不再变化（重复入库不抖动）。"""
    raw = (
        "3.4 按ABC分级分类\n"
        "A级为重要，能耗占比最高收益影响大。\n"
        "甲方：ein高g重庆卓越城市运营服务有限责任公司经办人：日期：24年2月\n"
        "乙方：重庆美日宝金制冷设备有限公司\n"
    )
    once = clean_markdown(raw)
    twice = clean_markdown(once.text)
    assert once.text.count("ABC") == 1 and "A级为重要" in once.text
    assert "ein高g" not in once.text
    assert twice.text == once.text
    assert twice.stats.get("sign_latin_removed", 0) == 0


# --- 规则按来源分域：scan（扫描件） vs office（Word/Excel/PPT 原生文本） --------
# 事故（doc 196 / doc 175 等 13 份）：OCR 清洗规则对 Word 也全量跑，把正文的
# 「按ABC分级分类」删成「按分类」、「MALL营业」删成「营业」——Word 里那是原文，
# 不是盖章压字，清洗层不能臆改。

def test_office_profile_does_not_apply_ocr_repairs() -> None:
    from ekbs.ingest.clean import OFFICE_PROFILE, SCAN_PROFILE, clean_markdown

    raw = (
        "由有限责仕公司按ABC分级分类管理。\n"
        "安全培训覆盖率 $1 0 0 \\%$ 。\n"
        "食务有限章）\n"
    )
    office = clean_markdown(raw, profile=OFFICE_PROFILE)
    scan = clean_markdown(raw, profile=SCAN_PROFILE)

    # office：原文一字不改（只做空白归一）
    assert "有限责仕公司" in office.text
    assert "按ABC分级分类" in office.text
    assert "食务有限章" in office.text
    assert office.profile == OFFICE_PROFILE

    # scan：错字/公式/印章碎片都被修（这是扫描件该有的行为）
    assert "有限责任公司" in scan.text
    assert "100%" in scan.text and "\\%" not in scan.text
    assert "食务有限章" not in scan.text


def test_office_profile_normalizes_whitespace_only() -> None:
    from ekbs.ingest.clean import OFFICE_PROFILE, clean_markdown

    raw = "标题\u3000\u3000栏目\t\t值  \n\n\n\n正文\r\n"
    result = clean_markdown(raw, profile=OFFICE_PROFILE)
    assert result.text.rstrip("\n") == "标题 栏目 值\n\n正文"
    assert result.stats["tabs_normalized"] == 2
    assert result.stats["space_normalized"] >= 2
    assert result.stats["blank_lines_collapsed"] == 1


def test_scan_profile_is_default_for_backcompat() -> None:
    from ekbs.ingest.clean import SCAN_PROFILE, clean_markdown

    raw = "安全培训覆盖率 $1 0 0 \\%$ ，有限责仕公司。"
    default = clean_markdown(raw)
    explicit = clean_markdown(raw, profile=SCAN_PROFILE)
    assert default.text == explicit.text
    assert default.profile == SCAN_PROFILE


def test_unknown_clean_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        clean_markdown("正文", profile="ocr")


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("制度.docx", "office"),
        ("旧格式.doc", "office"),
        ("台账.xlsx", "office"),
        ("旧表.xls", "office"),
        ("汇报.pptx", "office"),
        ("扫描件.pdf", "scan"),
        ("合同.md", "scan"),
        ("正文.txt", "scan"),
        ("", "scan"),
    ],
)
def test_clean_profile_for_path_follows_suffix(path: str, want: str) -> None:
    from ekbs.ingest.clean import clean_profile_for_path

    assert clean_profile_for_path(path) == want


async def test_ingest_office_source_skips_ocr_cleaning(seeded) -> None:
    """接线断言：.docx 来源入库**不跑** OCR 错字替换（.md 来源仍会跑）。"""
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

    async def _ingest(source_path: str) -> str:
        result = await pipeline.ingest_markdown(
            title=f"来源自检{source_path}",
            markdown="# 制度\n\n由有限责仕公司按ABC分级分类管理，覆盖率 100%。\n",
            kb_id=int(summary["kb_id"]),
            level=1,
            visible_dept_ids=[],
            source_path=source_path,
            real_corpus=False,
        )
        with runtime.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT content FROM ekbs_chunk WHERE doc_id = ?", (result.doc_id,)
            ).fetchall()
        return "\n".join(str(r["content"]) for r in rows)

    office_text = await _ingest("制度.docx")
    assert "有限责仕公司" in office_text, "Word 原生文本里的错字是原文，不该被清洗层替换"
    assert "按ABC分级分类" in office_text

    scan_text = await _ingest("制度.md")
    assert "有限责任公司" in scan_text, "非 Office 来源仍按扫描件规则修错字"
