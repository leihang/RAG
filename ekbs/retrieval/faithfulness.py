"""答案忠实度（生成侧防编造）—— 规则型支撑检查，不调用 LLM。

目标（P0）：

1. **无引用不成溯源**：答案里没有 ``[n]`` 时，不得把全部候选块挂成 citations。
2. **数字/量词须落在资料里**：从答案抽出关键 claim，必须在入选分块原文中出现；
   否则整答视为不可靠（``unsupported_claim``）。

不做 NLI / 语义蕴含（成本高、难测）；先把「明显胡编数字」挡掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

#: 答案里的引用标记（与 pipeline._CITATION_RE 同口径）
_CITATION_RE = re.compile(r"\[(\d+)\]")

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


def claim_supported(claim: str, evidence: str) -> bool:
    """claim 归一化后是否作为子串出现在证据里。"""
    needle = re.sub(r"\s+", "", claim)
    hay = re.sub(r"\s+", "", evidence or "")
    if not needle:
        return True
    if needle in hay:
        return True
    # 容忍千分位：1,234 ↔ 1234
    compact = needle.replace(",", "")
    if compact != needle and compact in hay.replace(",", ""):
        return True
    return False


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
