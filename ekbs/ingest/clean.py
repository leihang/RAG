"""清洗 —— 解析产物 → 分块之前的**显式一步**（`06` §1.7）。

## 为什么现在才补

在此之前，"清洗"在代码里**没有独立实现**：`pipeline.ingest_markdown` 的 docstring 写的是
"清洗与解析由调用方完成"，实际上各解析器吐出的 Markdown **原样**进了分块与嵌入。
方案 `06` §1.7 的"清洗 10 步"只落实了其中零散几条（表格序列化在 `table_serialize`、
页眉页脚等在解析器里各做各的）。

## 本轮规则

1. **公式/单位残留规整**：MinerU 数学模式把 `100%` 拆成 `$1 0 0 \\%$` → 还原成人话。
2. **扫描件印章/签字噪声**（`20` §2 步 2「水印与印章不进正文」）：
   公章 OCR 碎片、统一社会信用代码尾号、视觉模型「盖有红色印章…」描述、
   签署栏拉丁乱码（``ein高g`` / ``01g80423001``）从签署行与图注里剔除
   （只在「标签+冒号」的签署字段行上剥，正文的字母+汉字混排不动）。
3. **中文断字合回**：扫描 OCR 常见 ``项目名 称`` / ``离心 机`` → ``项目名称`` / ``离心机``。
4. **高发 OCR 错字**：如 ``有限责仕公司`` → ``有限责任公司``（盖章压字后误识）。

## 规则按来源分域（2026-11）

上面 1~4 条都是**扫描件/OCR**才成立的问题。Word/Excel/PPT 是原生文本，套用这些规则会
**静默改字**（实测把「按ABC分级分类」删成「按分类」、「MALL营业」删成「营业」）。所以
清洗入口按来源分 ``profile``：

* ``scan``（默认）：PDF/扫描图片/未知来源，跑全部规则；
* ``office``：docx/doc/xlsx/xls/pptx，**只做空白与控制字符归一** —— 错字、印章、LaTeX
  残留在 Word 里就是原文，要改改源文件，不能由清洗层臆改。

## 规矩

* 只做**确定性**规则，不调用 LLM、不做语义改写；
* 真公式一律不动（含 `\\frac`/`^`/字母）；
* 清洗发生在 checksum **之后**，秒传/去重键仍基于解析原始产物。
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from ekbs.logging import get_logger

log = get_logger(component="ingest.clean")

#: 清洗 profile：扫描件（OCR/版面）与 Office 原生文档用**不同规则集**。
#: 见模块头「规则按来源分域」。
SCAN_PROFILE = "scan"
OFFICE_PROFILE = "office"
_PROFILES = (SCAN_PROFILE, OFFICE_PROFILE)

#: 这些后缀由 Office 解析器产出**原生文本**（`python-docx` / `openpyxl` / `python-pptx`），
#: 没有 OCR 噪声，清洗层只做空白归一。
OFFICE_SUFFIXES = frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx"})

#: Office 文档只需要这几条：多空格/全角空格压一个、行尾空格、3+ 空行、零宽/控制字符。
_OFFICE_HSPACE = re.compile(r"[ \t\u3000]{2,}")
_OFFICE_TRAILING = re.compile(r"(?m)[ \t\u3000]+$")
_OFFICE_BLANK_LINES = re.compile(r"\n{3,}")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")

#: 行内/行间公式（非贪婪，`$$` 优先匹配，避免把 `$a$ $b$` 连成一段）
_MATH_SPAN = re.compile(r"\$\$(?P<body>.+?)\$\$|\$(?P<body2>.+?)\$", re.DOTALL)

#: 安全宏：只剩这些内容时，公式其实就是"被拆散的数字+单位"
_TEXT_MACRO = re.compile(r"\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
_SPACING_MACRO = re.compile(r"\\[,;:! ]|\\quad|\\qquad")
_ESCAPED_UNIT = re.compile(r"\\([%‰])")
#: 比较符宏 → 直接换成符号：合同里的门槛写成 `$\geqslant 9 0$`（≥90分）时，
#: 用户问「合格分数线」既搜不到 `≥90`，取数也核不出这个标签。
#: 注意顺序：`leqslant|leq` 必须排在 `le` 前面，否则 `\leq` 会匹配到 `le` 却因后面
#: 还有字母而卡在词边界上（实测踩到：`$\leq 3 0$` 一直没被规整）
_COMPARE_MACRO = re.compile(r"\\(?:geqslant|geq|leqslant|leq|neq|approx|le)\b")
_COMPARE_MAP = {
    "geqslant": "≥", "geq": "≥", "leqslant": "≤", "leq": "≤", "le": "≤",
    "neq": "≠", "approx": "≈",
}
#: 比较符与数字之间的空格：`≥ 90` → `≥90`
_SYMBOL_GAP = re.compile(r"(?<=[<>≥≤≠≈=])[ \t\u3000]+(?=\d)")

#: 采用判据：只含数字/空白/常见标点/单位符号/中文单位，且必须含数字。
#: **不含字母** —— `$m$`、`$x^2$` 这类宁可保留原文，也不冒险把真公式改成乱码。
_PLAIN_OK = re.compile(r"^[\d\s.,:;%‰()/\-+％－℃℉°<>≥≤≠≈=\u4e00-\u9fff]+$")
_HAS_DIGIT = re.compile(r"\d")
#: MinerU 常在数字之间插空格：`1 0 0` → `100`（注意：只在**数字与数字之间**合）
#: 数字与数字/小数点之间的空格一律合掉：`1 0 0` → `100`、`4 2 . 6` → `42.6`
#: （MinerU 逐字符输出时会把空格塞在任意两个数字/小数点之间）
_DIGIT_GAP = re.compile(r"(?<=[\d.])[ \t\u3000]+(?=[\d.,])")
#: 数字与单位符号之间的空格也要去掉：`100 %` → `100%`、`25 ℃` → `25℃`
_UNIT_GAP = re.compile(r"(?<=\d)[ \t\u3000]+(?=[%‰％℃℉°])")
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")

#: 汉字之间的 OCR 断字空格：`项目名 称` → `项目名称`（扫描合同表头高频）
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")

#: 视觉模型 / OCR 对红色公章的描述句（进索引只会污染检索）
_SEAL_DESC = re.compile(
    r"[（(]\s*盖有红色印章[^）)\n]{0,120}[）)]"
    r"|印章内容为[「\"“]?[^」\"”\n]{0,80}[」\"”]?"
    r"|盖有红色印章[^\n]{0,80}"
)

#: 签署栏旁的公章编号碎片（**整行纯数字**或紧贴「盖章」；不动账号/税号字段）
_SEAL_CODE_LINE = re.compile(r"(?m)^[\s\u3000]*\d{8,18}[\s\u3000]*$")
_SEAL_CODE_INLINE = re.compile(
    r"（盖章）\s*\d{5,18}"
    r"|\d{10,18}\s*（盖章）"
)

#: 签署栏拉丁乱码：`ein高g` / `E動` / `01g80423001` / `01g8042`
_SIGN_LATIN_JUNK = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"[A-Za-z]{1,4}[\u4e00-\u9fff]{1,2}[A-Za-z]{0,3}"  # ein高g / E動 / E動责任
    r"|[A-Za-z]*\d{2,}[A-Za-z]+\d{4,}"  # 01g80423001
    r"|[A-Za-z]\d{6,}"  # g80423001 一类
    r"|[山a-zA-Z]{1,3}重?\d{8,}"  # 山A重14134197750011
    r")"
    r"(?![A-Za-z0-9])"
)

#: 公章压进**签署行**的信用代码/编号碎片（不扫整篇，避免误伤电话/账号）
_SEAL_CODE_FRAG = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"5001\d{0,2}\s*0?1080423901"  # 常见重庆信用代码尾
    r"|50010\d{0,12}"
    r"|(?<![元￥$])\d{13,18}(?![.\d])"  # 信用代码长度；排除带小数金额
    r")"
    r"(?!\d)"
)
#: `_SIGN_LATIN_JUNK` 的作用域门：只剥**签署字段**行（标签紧跟冒号，如
#: ``甲方：`` / ``授权代表签字：`` / ``甲方（盖章）：``）。
#: 不能只认关键词：租赁合同条款正文里「乙方确认…」「…的乙方」几乎每行都有，
#: 会把正文的 ``POS机`` / ``POP规格`` / ``熙熙MALL`` 当乱码删掉（doc 175 实测）。
#: 也不能复用 `_SIGN_LINE` —— 它要求行内出现「盖章）」这类后缀，没有后缀的
#: ``授权代表签字：…`` 会漏剥。
_SIGN_LINE_FOR_LATIN = re.compile(
    r"(?:甲方|乙方|代表人|授权代表|经办人|签字|签署(?:日期)?|盖章)"
    r"\s*(?:[（(][^）)\n]{0,10}[）)])?\s*[：:]"
)
_SIGN_LINE = re.compile(
    r"(?m)^.*(甲方|乙方|代表人|授权代表|经办人|签字|签署日期|盖章）).*$"
)

#: 盖章压字后的残片：`食务有限章）` / `有服务有限限责任`
_SEAL_GARBLED = re.compile(
    r"食务有限章[）\)\uff09]?"
    r"|有服务有限限责任"
    r"|港有服务"
    r"|有限限责任"
    r"|股份有公(?!司)"
    r"|分公司分公司"
    r"|[）\)\uff09]\s*授权代表授权代表"
)

#: 签署栏手写姓名：仅匹配「代表人：张三」这类带冒号字段，避免误伤「签字盖章之日起」。
#: 姓名至少 2 字。甲/乙/方/单位是栏目标题，不是人名——「代表人：乙方单位」曾被收成「[手写签字]位」。
_HANDWRITE_NAME = re.compile(
    r"(?P<label>(?:代表人|授权代表|经办人|签字)[：:]\s*)"
    r"(?P<name>[\u4e00-\u9fffA-Za-z]{2,4})"
    r"(?=(?:\s|$|[日年甲方乙方单位]|\d))"
)
_NOT_A_PERSON_NAME = re.compile(r"^(?:甲|乙|方|单|位|甲方|乙方|单位|公司|日期|盖章|签字)")

#: 高发 OCR 错字（盖章/扫描压字后）—— 只改**明确错、改了更对**的词
_OCR_TYPO_MAP: tuple[tuple[str, str], ...] = (
    ("有限责仕公司", "有限责任公司"),
    ("责仕公司", "责任公司"),
    ("联系电处", "联系电话"),
    ("公司庆两路支行", "公司重庆两路支行"),
    ("重公司分公司", "重庆分公司"),
    ("乙万违约", "乙方违约"),
    ("城市营服务", "城市运营服务"),
    ("城市运营服务港有服务有限限责任公司", "城市运营服务有限责任公司"),
    ("E動责任公司", "有限责任公司"),
)


def _unwrap_body(body: str) -> tuple[str, bool]:
    """返回 (规整后的文本, 是否采用)。不采用时调用方保留原文。"""
    text = body
    # `\text{X}` 之类先掏空成 X；嵌套一层就够（`\text{‰}`）
    for _ in range(3):
        replaced = _TEXT_MACRO.sub(r"\1", text)
        if replaced == text:
            break
        text = replaced
    text = _SPACING_MACRO.sub("", text)
    text = _ESCAPED_UNIT.sub(r"\1", text)
    # 比较符宏 → 符号（`\geqslant 9 0` → `≥90`）
    text = _COMPARE_MACRO.sub(lambda m: _COMPARE_MAP[m.group(0)[1:]], text)
    # MinerU 用 `${...}$` 包住符号组：花括号是分组记号，纯文本里没有意义
    text = text.replace("{", "").replace("}", "")
    if "\\" in text:  # 还剩宏 → 真公式（\frac、\sum、\alpha、^、_ 等）
        return body, False
    text = _DIGIT_GAP.sub("", text)
    text = _UNIT_GAP.sub("", text)
    text = _SYMBOL_GAP.sub("", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    if not _HAS_DIGIT.search(text) or not _PLAIN_OK.match(text):
        return body, False
    text = text.replace("％", "%").replace("－", "-")
    return text, True


def normalize_math_in_text(text: str) -> tuple[str, int, int]:
    """把"其实就是数字+单位"的行内公式还原成人话。返回 (文本, 采用数, 保留数)。"""
    if not text or "$" not in text:
        return text, 0, 0
    used = 0
    kept = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal used, kept
        body = match.group("body") or match.group("body2") or ""
        unwrapped, ok = _unwrap_body(body)
        if ok:
            used += 1
            return unwrapped
        kept += 1
        return match.group(0)

    return _MATH_SPAN.sub(_sub, text), used, kept


def collapse_cjk_gaps(text: str) -> tuple[str, int]:
    """合并汉字之间的 OCR 断字空格。返回 (文本, 合并次数)。"""
    if not text:
        return text, 0
    count = 0

    def _sub(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return ""

    return _CJK_GAP.sub(_sub, text), count


def apply_ocr_typos(text: str) -> tuple[str, int]:
    """高发 OCR 错字替换。返回 (文本, 替换次数)。"""
    if not text:
        return text, 0
    n = 0
    out = text
    for bad, good in _OCR_TYPO_MAP:
        if bad == good:
            continue
        if bad in out:
            hits = out.count(bad)
            out = out.replace(bad, good)
            n += hits
    return out, n


def _mask_handwritten_names(text: str) -> tuple[str, int]:
    """签署栏手写姓名 OCR 不可靠 → 占位，避免错误人名进检索。"""
    if not text:
        return text, 0
    n = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal n
        name = match.group("name")
        # 空名、占位，或栏目标题（乙方/单位）都不是手写姓名
        if not name or name in ("手写签字", "[手写签字]") or _NOT_A_PERSON_NAME.match(name):
            return match.group(0)
        n += 1
        return f"{match.group('label')}[手写签字]"

    return _HANDWRITE_NAME.sub(_sub, text), n


def _strip_sign_latin_junk(text: str) -> tuple[str, int]:
    """只在签署行上剥拉丁乱码。返回 (文本, 删除次数)。

    正文里「按ABC分级分类」「GB标准执行」「MALL营业」是正常字母+汉字混排，
    全文替换会把它删成「按分类」「执行」（全库 38 份解析稿里 13 份命中）。
    """
    removed = 0
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if not _SIGN_LINE_FOR_LATIN.search(line):
            lines.append(line)
            continue
        cleaned_line, n = _SIGN_LATIN_JUNK.subn("", line)
        removed += n
        lines.append(cleaned_line)
    return "".join(lines), removed


def strip_seal_and_signature_noise(text: str) -> tuple[str, dict[str, int]]:
    """去掉扫描件印章/签署栏 OCR 噪声（`20` §2 步 2）。

    保留「经双方盖章之日起生效」这类**条款正文**；只剥印章描述、编号碎片与乱码。
    """
    if not text:
        return text, {}
    stats = {
        "seal_desc_removed": 0,
        "seal_code_removed": 0,
        "sign_latin_removed": 0,
        "handwrite_masked": 0,
    }
    out = text

    def _count_sub(pattern: re.Pattern[str], key: str, repl: str = "") -> None:
        nonlocal out
        found = pattern.findall(out)
        if found:
            stats[key] += len(found)
            out = pattern.sub(repl, out)

    _count_sub(_SEAL_DESC, "seal_desc_removed")
    _count_sub(_SEAL_CODE_LINE, "seal_code_removed")
    # 紧贴「盖章」的编号整篇都剥，但账号/开户/银行行上的数字是尾部账户，留下。
    inline_removed = 0
    kept_lines: list[str] = []
    for line in out.splitlines(keepends=True):
        if re.search(r"账号|开户|银行", line):
            kept_lines.append(line)
            continue
        new_line, n = _SEAL_CODE_INLINE.subn("", line)
        inline_removed += n
        kept_lines.append(new_line)
    if inline_removed:
        stats["seal_code_removed"] += inline_removed
        out = "".join(kept_lines)
    # 拉丁乱码只在签署行上剥：正文里的「按ABC分级分类」「GB标准执行」是正常
    # 字母+汉字混排，全文替换会把「ABC」删成「按分类」。
    out, latin_removed = _strip_sign_latin_junk(out)
    if latin_removed:
        stats["sign_latin_removed"] += latin_removed
    _count_sub(_SEAL_GARBLED, "seal_desc_removed")

    # 仅在签署行剥信用代码碎片。带账号/开户/银行的行是尾部账户信息，数字留下。
    # 条款正文里的电话/账号不走这条（_SIGN_LINE 不含「账号」单独成行）。
    def _scrub_sign_line(match: re.Match[str]) -> str:
        line = match.group(0)
        if re.search(r"账号|开户|银行", line):
            return line
        cleaned_line, n = _SEAL_CODE_FRAG.subn("", line)
        if n:
            stats["seal_code_removed"] += n
        return cleaned_line

    out = _SIGN_LINE.sub(_scrub_sign_line, out)

    out, hw_n = _mask_handwritten_names(out)
    if hw_n:
        stats["handwrite_masked"] = hw_n

    # 图注整段只剩「盖章」噪声时，换成中性占位，避免 dig 进向量
    if "盖章" in out and len(out) < 80:
        compact = re.sub(r"\s+", "", out)
        if re.fullmatch(r"(?:\[图片\])?[^。]{0,20}（?盖章）?[^。]{0,20}", compact) or (
            "盖章" in compact and not any(k in compact for k in ("之日起", "后生效", "双方", "合同"))
            and len(compact) < 40
        ):
            stats["seal_desc_removed"] += 1
            out = "[签署/盖章区]"

    # 压缩清洗后留下的多余空行/空格
    out = re.sub(r"[ \t\u3000]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # 签署栏「授权代表授权代表」重复；OCR 把「签署日期」撕成「署日期」
    out = re.sub(r"(授权代表){2,}", "授权代表", out)
    out = re.sub(r"签字：\s*署日期", "签署日期", out)
    out = re.sub(r"(?<![签日])署日期", "签署日期", out)
    out = re.sub(r"授权代表\s*授权代表", "授权代表", out)
    return out, {k: v for k, v in stats.items() if v}


def normalize_office_text(text: str) -> tuple[str, dict[str, int]]:
    """Office 原生文本的归一：**只动空白/控制字符，不猜内容**。

    为什么 Office 不走 OCR 规则：Word 里「有限责仕公司」就是作者写错了，不是盖章压字；
    静默替换成「有限责任公司」等于改了原文（检索、引用、审计对不上）。同理 Word 的
    ``$`` 是货币符号，不是 MinerU 的数学模式。
    """
    if not text:
        return text, {}
    stats: dict[str, int] = {}
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\t" in out:
        n = out.count("\t")
        out = out.replace("\t", " ")
        stats["tabs_normalized"] = n
    found = _ZERO_WIDTH.findall(out)
    if found:
        stats["zero_width_removed"] = len(found)
        out = _ZERO_WIDTH.sub("", out)
    found = _OFFICE_HSPACE.findall(out)
    if found:
        stats["space_normalized"] = len(found)
        out = _OFFICE_HSPACE.sub(" ", out)
    found = _OFFICE_TRAILING.findall(out)
    if found:
        stats["trailing_space_removed"] = len(found)
        out = _OFFICE_TRAILING.sub("", out)
    found = _OFFICE_BLANK_LINES.findall(out)
    if found:
        stats["blank_lines_collapsed"] = len(found)
        out = _OFFICE_BLANK_LINES.sub("\n\n", out)
    return out, stats


def clean_profile_for_path(source_path: str) -> str:
    """按源文件后缀选清洗 profile；未知后缀按**扫描件**处理（保持旧行为）。"""
    suffix = pathlib.Path(str(source_path or "")).suffix.lower()
    return OFFICE_PROFILE if suffix in OFFICE_SUFFIXES else SCAN_PROFILE


#: 水印相机 / 巡检水印模板常见壳子。成片出现时说明图注是「拍照元数据」而非正文。
_FIGURE_WATERMARK_MARKERS: tuple[str, ...] = (
    "水印相机",
    "今日水印",
    "自定义水印",
    "物业巡检水印",
    "水印枢机",
    "防伪",
    "相机真实可验",
    "真实时间",
    "巡检人：请输入",
    "请输入巡检人",
)

#: 水印模板里的空字段行（无实质内容）。
_FIGURE_WATERMARK_EMPTY_FIELDS: tuple[str, ...] = (
    "问题备注",
    "巡检人",
    "拍摄人",
    "备注",
)

#: 手写表格式视觉抄录常见重复噪声（实测 PPT 漏保检查表：「正常正常是」连片）。
_HANDWRITE_OCR_MARKERS: tuple[str, ...] = (
    "正常正常是",
    "正常正常",
)


def _is_handwrite_ocr_caption(blob: str) -> bool:
    if any(mark in blob for mark in _HANDWRITE_OCR_MARKERS):
        return True
    if blob.count("正常") >= 5:
        return True
    tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", blob)
    if not tokens:
        return False
    from collections import Counter

    _token, freq = Counter(tokens).most_common(1)[0]
    return freq >= 8 and freq / max(len(tokens), 1) >= 0.35


def _is_watermark_boilerplate_line(line: str) -> bool:
    if any(m in line for m in _FIGURE_WATERMARK_MARKERS):
        return True
    if re.search(r"请输入", line):
        return True
    bare = re.sub(r"[\s:：\-—_]+$", "", line)
    if bare in _FIGURE_WATERMARK_EMPTY_FIELDS:
        return True
    if re.fullmatch(
        r"(?:\d{1,2}:\d{2}.*|\d{4}[./年-]\d{1,2}[./月-]\d{1,2}.*|"
        r".*?(?:晴|多云|阴)\s*\d{1,2}\s*℃?.*|经纬度[:：].*)",
        line,
    ):
        return True
    return bool(re.fullmatch(r"(?:地点[:：].*|\d{1,6}|…+.*?…+)", line))


def is_low_quality_figure_caption(text: str) -> bool:
    """图注是否不适合作为**文本检索单元**（仍可多模态回源）。

    两类实测会淹没 PPT 正文召回：
    1. 手写表 OCR：短词连片重复（「正常正常是…」），语义接近「做了检查」却无工作要点；
    2. 水印相机壳：大半行是天气/防伪/「请输入巡检人」，正文只有地名型号碎片。
    """
    blob = (text or "").strip()
    if not blob:
        return False
    if _is_handwrite_ocr_caption(blob):
        return True
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    if not lines:
        return False
    mark_hits = sum(1 for m in _FIGURE_WATERMARK_MARKERS if m in blob)
    if mark_hits == 0:
        return False
    boilerplate = sum(1 for line in lines if _is_watermark_boilerplate_line(line))
    ratio = boilerplate / len(lines)
    if len(blob) <= 280 and ratio >= 0.45:
        return True
    if mark_hits >= 2 and ratio >= 0.5:
        return True
    if mark_hits >= 2 and (len(lines) - boilerplate) <= 3 and len(blob) <= 280:
        return True
    return bool("物业巡检水印" in blob and len(blob) <= 120)


def clean_figure_text(text: str) -> str:
    """图题注 / OCR 专用清洗（印章页视觉描述不得进检索）。"""
    if not text:
        return text
    result = clean_markdown(text)
    cleaned = result.text.strip()
    # 视觉模型常输出「无可读文字 / 盖有红色印章」—— 这类图注不如占位
    if any(
        mark in cleaned
        for mark in ("无可读文字", "盖有红色印章", "纯白背景", "没有任何可识别")
    ):
        if "甲方" in cleaned or "乙方" in cleaned or "单位" in cleaned:
            # 保留甲乙方标签行，去掉印章描述与编号
            lines = []
            for line in cleaned.splitlines():
                s = line.strip()
                if not s:
                    continue
                if any(m in s for m in ("无可读文字", "盖有红色印章", "纯白背景", "场景描述")):
                    continue
                if re.fullmatch(r"\d{8,18}", s):
                    continue
                lines.append(s)
            cleaned = "\n".join(lines).strip() or "[签署/盖章区]"
        else:
            cleaned = "[签署/盖章区]"
    # 签署页图：公司名被印章撕成碎片时整段降级
    if (
        cleaned
        and len(cleaned) < 60
        and re.search(r"市棉|股份有限\s*$|理工程技术", cleaned)
        and "甲方" not in cleaned
        and "乙方" not in cleaned
    ):
        cleaned = "[签署/盖章区]"
    # 手写表 / 水印相机壳：降成短占位，避免进稠密/BM25 后抢走幻灯正文
    if cleaned and is_low_quality_figure_caption(cleaned):
        if any(m in cleaned for m in _HANDWRITE_OCR_MARKERS) or cleaned.count("正常") >= 5:
            cleaned = "[手写表格照片]"
        else:
            cleaned = "[现场照片]"
    return cleaned


@dataclass
class CleanResult:
    """清洗结果 + 可观测统计（进日志与质量报告）。"""

    text: str
    stats: dict[str, Any] = field(default_factory=dict)
    #: 本次用的规则集（``scan`` / ``office``），写日志与质量报告用
    profile: str = SCAN_PROFILE


def clean_markdown(markdown: str, *, profile: str = SCAN_PROFILE) -> CleanResult:
    """入库前的清洗。``profile`` 决定规则集：``scan``（默认）或 ``office``。

    ``office`` 只做空白/控制字符归一（见 :func:`normalize_office_text`）：
    OCR 修复规则（公式残留/断字/错字/印章）对 Word/Excel/PPT 是**改原文**，不能跑。
    """
    if profile not in _PROFILES:
        raise ValueError(f"未知清洗 profile：{profile!r}（可选 {', '.join(_PROFILES)}）")
    text = markdown or ""
    stats: dict[str, Any] = {}

    if profile == OFFICE_PROFILE:
        text, stats = normalize_office_text(text)
        return CleanResult(text=text, stats=stats, profile=profile)

    text, used, kept = normalize_math_in_text(text)
    if used or kept:
        stats["math_unwrapped"] = used
        stats["math_preserved"] = kept

    # 先合断字，再错字/印章：否则「食 务有限章」匹配不到清洗规则
    text, cjk_n = collapse_cjk_gaps(text)
    if cjk_n:
        stats["cjk_gaps_collapsed"] = cjk_n

    # 错字先于印章剥离：如「E動责任公司」整词替换，避免先剥成「任公司」
    text, typo_n = apply_ocr_typos(text)
    if typo_n:
        stats["ocr_typos_fixed"] = typo_n

    text, seal_stats = strip_seal_and_signature_noise(text)
    stats.update(seal_stats)

    return CleanResult(text=text, stats=stats, profile=profile)
