"""清洗 —— 解析产物 → 分块之前的**显式一步**（`06` §1.7）。

## 为什么现在才补

在此之前，"清洗"在代码里**没有独立实现**：`pipeline.ingest_markdown` 的 docstring 写的是
"清洗与解析由调用方完成"，实际上各解析器吐出的 Markdown **原样**进了分块与嵌入。
方案 `06` §1.7 的"清洗 10 步"只落实了其中零散几条（表格序列化在 `table_serialize`、
页眉页脚等在解析器里各做各的）。

## 本轮补的第一条：公式/单位残留规整

MinerU 走数学模式时会把 `100%` 输出成 LaTeX，**原样入库就再也搜不到**：

.. code-block:: text

    原件里的人话            库里存的样子                        用户搜「覆盖率100%」
    ─────────────────────  ──────────────────────────────────  ──────────────────
    安全培训覆盖率 100%     $1 0 0 \\%$                          ❌ 关键词那一路召不回
    合格率 100%            $100 \\text{‰}$                       ❌ 连字符都对不上

所以这里把**"其实就是个数字/单位"的行内公式**还原成人话；**真正的公式一律不动**
（含 `\\frac`、`^`、`_`、`\\sum`、希腊字母等——它们不是"被拆散的数字"）。

## 规矩

* 只处理行内 `$...$` / `$$...$$`；
* 剥掉安全宏（`\\text{}` `\\mathrm{}` `\\mathbf{}` `\\%` `\\‰` 与 `\\,` `\\;` `\\!` 等间距宏）；
* 剥完**还剩反斜杠** → 说明是真公式，**整段原样保留**；
* 剥完只由「数字/空白/常见标点/中文单位」组成且含数字 → 采用；`％`→`%`、`－`→`-`；
* 数字之间被 MinerU 塞进的空格要合起来（`1 0 0` → `100`）。

其余清洗步骤（页眉页脚、重复行、PII 之外的规整）继续按需往这里加 ——
**入库链路上只此一处**，避免"改了写入侧、忘了重建侧"的老问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ekbs.logging import get_logger

log = get_logger(component="ingest.clean")

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


@dataclass
class CleanResult:
    """清洗结果 + 可观测统计（进日志与质量报告）。"""

    text: str
    stats: dict[str, Any] = field(default_factory=dict)


def clean_markdown(markdown: str) -> CleanResult:
    """入库前的清洗（目前只有"公式/单位残留规整"一条；后续步骤往这里加）。"""
    text, used, kept = normalize_math_in_text(markdown or "")
    stats: dict[str, Any] = {}
    if used or kept:
        stats = {"math_unwrapped": used, "math_preserved": kept}
    return CleanResult(text=text, stats=stats)
