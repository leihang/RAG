"""答案忠实度（生成侧防编造）—— 规则型支撑检查，不调用 LLM。

目标（P0）：

1. **无引用不成溯源**：答案里没有 ``[n]`` 时，不得把全部候选块挂成 citations。
2. **数字/量词须落在资料里**：从答案抽出关键 claim，必须在入选分块原文中出现；
   否则记为 ``unsupported_claim``。

⚠️ 关于第 2 条的**判定强度**（实测教训）：比对必须容忍单位与格式化差异，否则
**表格类语料会把好答案整篇误杀** —— 见 ``_SPECIFIC_NUMBER_RE`` 的说明。
而且 ``unsupported_claim`` **不再等同"整答拒答"**（调用方按 ``mostly_unsupported``
决定是否硬拒）：归因差异可降级，整篇对不上才拒。

不做 NLI / 语义蕴含（成本高、难测）；先把「明显胡编数字」挡掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Sequence

#: 答案里的引用标记（与 pipeline._CITATION_RE 同口径）
_CITATION_RE = re.compile(r"\[(\d+)\]")

#: 百分比（用于「7.69% ↔ 0.0769」这类换算：表格里常把百分比存成小数）
_PERCENT_RE = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*[%％]")

#: 「够特异」的数字主体：带千分位、带小数点、或 ≥3 位整数。
#:
#: 为什么需要它：答案里的数字**带单位/经过格式化**，而资料里是**原始单元格值**。
#: 实测（`2026年7月能耗分析.xlsx`）：答案写「474.004万元」，表格单元格里是 `474.004`；
#: 答案写「7.69%」，单元格里是 `0.0769` 或 `7.69`。按整串子串比对必然全部失败，
#: 结果是**好答案被判"编造"**（实测 25 条 claim 里误杀 9 条）。
#:
#: 刻意**只取**这些够特异的形态：1~2 位整数（如「28万元」里的 28）在长文里几乎必中，
#: 拿它做匹配等于把校验放空 —— 那几条就让它留在 unsupported 里（配合降级策略不误杀整答）。
_SPECIFIC_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,}")

_UNITS = (
    r"天|日|年|月|周|小时|分钟|元|万元|亿元|%|％|页|条|款|项|章|节|次|人|名|份|篇|级"
)

#: 可核对 claim：带千分位/小数/≥2 位整数（可带单位）；单位数必须带单位
_CLAIM_RE = re.compile(
    rf"(?:"
    rf"\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?(?:\s*(?:{_UNITS}))?|"
    rf"\d+\.\d+(?:\s*(?:{_UNITS}))?|"
    rf"\d{{2,}}(?:\s*(?:{_UNITS}))?|"
    rf"[1-9]\s*(?:{_UNITS})"
    rf")"
)

REFUSE_MISSING_CITATION = "missing_citation"
REFUSE_UNSUPPORTED_CLAIM = "unsupported_claim"
REFUSE_CLAUSE_MISMATCH = "clause_mismatch"

#: 软降级的标记前缀（写进 ``AnswerResult.degraded``，供界面提示与审计分组）
DEGRADED_UNSUPPORTED = "unsupported_claim"

#: `unsupported` 占比达到该值、且 claim 数不少于下限时，**仍按拒答处理**：
#: 降级不等于放行 —— 归因差异（单位/千分位）可以容忍，"整篇数字都对不上"必须挡住。
UNSUPPORTED_REFUSE_RATIO = 0.6
UNSUPPORTED_REFUSE_MIN_CLAIMS = 3



@dataclass
class ClaimCheck:
    text: str
    supported: bool

    def as_dict(self) -> dict[str, object]:
        return {"text": self.text, "supported": self.supported}


@dataclass
class FaithfulnessReport:
    """调试台 / 审计用的忠实度报告。"""

    ok: bool
    cited_indices: list[int] = field(default_factory=list)
    claims: list[ClaimCheck] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    reason: str = ""  # "" | missing_citation | unsupported_claim

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "cited_indices": list(self.cited_indices),
            "claims": [c.as_dict() for c in self.claims],
            "unsupported": list(self.unsupported),
            "reason": self.reason,
        }


def cited_indices(answer: str) -> list[int]:
    """答案中出现的引用编号（去重、保序）。"""
    seen: set[int] = set()
    out: list[int] = []
    for match in _CITATION_RE.finditer(answer or ""):
        index = int(match.group(1))
        if index not in seen:
            seen.add(index)
            out.append(index)
    return out


def strip_citations(answer: str) -> str:
    return _CITATION_RE.sub("", answer or "")


def extract_claims(answer: str) -> list[str]:
    """从答案抽出待核对 claim（已去掉引用标记）。"""
    text = strip_citations(answer)
    found: list[str] = []
    seen: set[str] = set()
    for match in _CLAIM_RE.finditer(text):
        raw = match.group(0).strip()
        if not raw:
            continue
        key = re.sub(r"\s+", "", raw)
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return found


def build_evidence(chunks: Sequence[object]) -> str:
    """入选分块拼成可检索证据串（标题路径 + 正文）。"""
    parts: list[str] = []
    for chunk in chunks:
        heading = str(getattr(chunk, "heading_path", "") or "")
        content = str(getattr(chunk, "content", "") or "")
        title = str(getattr(chunk, "doc_title", "") or "")
        parts.append(f"{title}\n{heading}\n{content}")
    return "\n".join(parts)


def _plain(value: Decimal) -> str:
    """Decimal → 普通十进制字符串（去掉多余的 0 与科学计数法）。"""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def numeric_cores(claim: str) -> list[str]:
    """claim 里「够特异」的数字主体（含百分比的等价值），用于单位/格式无关的匹配。

    ``474.004万元`` → ``["474.004"]``；``7.69%`` → ``["7.69", "0.0769"]``；
    ``5,960,922度`` → ``["5960922"]``；``28万元`` → ``[]``（见 _SPECIFIC_NUMBER_RE 的说明）。
    """
    cores: list[str] = []
    for match in _SPECIFIC_NUMBER_RE.finditer(claim or ""):
        core = match.group(0).replace(",", "")
        if core and core not in cores:
            cores.append(core)
    for match in _PERCENT_RE.finditer(claim or ""):
        raw = match.group(1).replace(",", "")
        try:
            frac = Decimal(raw) / Decimal(100)
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
        for candidate in (_plain(frac),):
            if candidate and candidate not in cores:
                cores.append(candidate)
    return cores


def claim_supported(claim: str, evidence: str) -> bool:
    """claim 归一化后是否**可被资料支撑**。

    三级判定（由严到宽）：

    1. 原串（去空白）子串 —— 最强；
    2. 去掉千分位后子串 —— 容忍 ``1,234`` ↔ ``1234``；
    3. **数字主体在资料里出现过** —— 容忍单位与格式化差异（``474.004万元`` ↔ ``474.004``、
       ``7.69%`` ↔ ``0.0769``）。第 3 级只看"够特异"的数字，见 _SPECIFIC_NUMBER_RE。
    """
    needle = re.sub(r"\s+", "", claim)
    hay = re.sub(r"\s+", "", evidence or "")
    if not needle:
        return True
    if needle in hay:
        return True
    compact = needle.replace(",", "")
    hay_nocomma = hay.replace(",", "")
    if compact != needle and compact in hay_nocomma:
        return True
    for core in numeric_cores(claim):
        if core in hay_nocomma:
            return True
    return False


def mostly_unsupported(report: FaithfulnessReport) -> bool:
    """`unsupported` 占比是否高到"不该放行"（整篇数字对不上 = 大概是编的）。"""
    total = len(report.claims)
    if total < UNSUPPORTED_REFUSE_MIN_CLAIMS:
        return False
    return (len(report.unsupported) / total) >= UNSUPPORTED_REFUSE_RATIO



def assess_faithfulness(answer: str, chunks: Sequence[object]) -> FaithfulnessReport:
    """对生成答案做规则型忠实度评估。

    * 无 ``[n]`` 且答案非空 → ``reason=missing_citation``，``ok=False``
    * 有 claim 不在证据中 → ``reason=unsupported_claim``，``ok=False``
    * 其余 → ``ok=True``
    """
    text = (answer or "").strip()
    indices = cited_indices(text)
    evidence = build_evidence(chunks)
    claims_raw = extract_claims(text)
    checks = [
        ClaimCheck(text=c, supported=claim_supported(c, evidence)) for c in claims_raw
    ]
    unsupported = [c.text for c in checks if not c.supported]

    if not text:
        return FaithfulnessReport(ok=True, cited_indices=indices, claims=checks)

    if not indices:
        return FaithfulnessReport(
            ok=False,
            cited_indices=[],
            claims=checks,
            unsupported=unsupported,
            reason=REFUSE_MISSING_CITATION,
        )
    if unsupported:
        return FaithfulnessReport(
            ok=False,
            cited_indices=indices,
            claims=checks,
            unsupported=unsupported,
            reason=REFUSE_UNSUPPORTED_CLAIM,
        )
    return FaithfulnessReport(
        ok=True, cited_indices=indices, claims=checks, unsupported=[]
    )


_CLAUSE_RE = re.compile(
    r"第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*([章节条款项])"
)
_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """条款序号：阿拉伯数字或常见中文数字（到千）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    total = 0
    pending = 0
    seen = False
    for ch in raw:
        if ch in _CN_DIGIT:
            pending = _CN_DIGIT[ch]
            seen = True
            continue
        unit = {"十": 10, "百": 100, "千": 1000}.get(ch)
        if unit is None:
            return None
        total += (pending or 1) * unit
        pending = 0
        seen = True
    if not seen:
        return None
    return total + pending


def _clause_keys(text: str) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for match in _CLAUSE_RE.finditer(text or ""):
        number = _cn_to_int(match.group(1))
        if number is None:
            continue
        keys.add((match.group(2), number))
    return keys


def clause_mismatches(answer: str, chunks: Sequence[object]) -> list[str]:
    """答案里写出的「第X章/节/条/款/项」若不在所引用块的**证据**里，列出来。

    没写条款号则通过。写了就必须能在被 ``[n]`` 点到的块里找到同一个序号
    （``第五条`` 与 ``第5条`` 视为同一条）。

    证据 = ``heading_path`` **+ 正文**：分块按 token 聚合后，一个块可能覆盖多条条款
    （标题行补在正文里），此时条号只出现在正文、不在块标题路径里 —— 只查标题会把
    正确的条款引用误判成幻觉。反过来，正文里没有的条号仍然照拒。
    """
    mentions = _clause_keys(strip_citations(answer))
    if not mentions:
        return []
    indices = cited_indices(answer)
    allowed: set[tuple[str, int]] = set()
    for index in indices:
        if not 1 <= index <= len(chunks):
            continue
        chunk = chunks[index - 1]
        allowed |= _clause_keys(str(getattr(chunk, "heading_path", "") or ""))
        allowed |= _clause_keys(str(getattr(chunk, "content", "") or ""))
    bad = sorted(mentions - allowed)
    return [f"第{number}{kind}" for kind, number in bad]
