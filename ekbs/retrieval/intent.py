"""多意图拆分（P0）：把「一次问多个问题」拆成独立子问。

设计要点：
- **保守**：宁可少拆，不可误拆。拆不出来就退回单问（单问链路零影响）。
- **确定性**：规则拆分不依赖模型，离线可测；无需网络/付费。
- 可配置：``retrieval.intent_split``（enabled / strategy / max_questions）。

batch 1 只落「规则拆分」；``strategy: llm`` 暂按规则兜底（LLM 接线留作后续）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ekbs.logging import get_logger

log = get_logger(component="retrieval.intent")

#: 编号列表：``1.`` / ``1、`` / ``1)`` / ``(1)`` / ``一、`` / ``（一）``
_NUMBERED_RE = re.compile(
    r"(?:^|[\s；;。])\s*"
    r"(?:\d{1,2}\s*[.、)）]|[（(]\d{1,2}[)）]|[一二三四五六七八九十]{1,2}\s*[、.])"
)
#: 连接词（要求两侧都足够长，避免把短语误拆）。
#: **不含「分别」**：「预算、节约电费和回收期分别是多少」里的「分别」是「各自/分别对应」，
#: 不是另起一问；拆开会得到孤儿子问「是多少？」（实测 IOT 改造三问）。
_CONJUNCTION_RE = re.compile(r"以及|还有|另外|其次|然后")
#: 「分别」后若只剩短谓语，整句保留（双保险；即便以后再把「分别」加回连接词也不误拆）
_RESPECTIVELY_TAIL_RE = re.compile(
    r"^(?:是多少|是什么|为多少|有哪些|如何|怎么样|怎么算|怎么申请)[？?。.!！]*$"
)
_QUESTION_MARK_RE = re.compile(r"[？?]+")
_SEMICOLON_RE = re.compile(r"[；;]+")

_MIN_PART_CHARS = 2


@dataclass
class SubQuestion:
    """一个子问。``rewritten`` 由调用方（pipeline）填入上下文改写结果。"""

    index: int
    text: str
    source: str = "single"  # single | rules
    rewritten: str = ""
    confidence: float = 1.0


class IntentSplitter:
    """多意图拆分器（规则版）。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        cfg = getattr(getattr(settings, "retrieval", None), "intent_split", None)
        self.enabled = bool(getattr(cfg, "enabled", False)) if cfg is not None else False
        self.strategy = str(getattr(cfg, "strategy", "rules") or "rules")
        self.max_questions = max(1, int(getattr(cfg, "max_questions", 5) or 5))
        if self.enabled and self.strategy == "llm":
            # batch 1 未接线 LLM；显式记录，避免「配置写了 llm 但实际走规则」的静默偏差
            log.info("intent_split_llm_not_wired_fallback_rules")

    def split(self, question: str) -> list[SubQuestion]:
        """拆分问题；无法拆时返回单元素列表（单问快路径）。"""
        text = (question or "").strip()
        if not text:
            return []
        if not self.enabled or self.strategy == "off":
            return [SubQuestion(0, text, "single")]

        parts = self._rule_split(text)
        if len(parts) <= 1:
            return [SubQuestion(0, text, "single")]
        parts = parts[: self.max_questions]
        return [SubQuestion(i, part, "rules") for i, part in enumerate(parts)]

    # ------------------------------------------------------------------

    def _rule_split(self, text: str) -> list[str]:
        numbered = self._split_numbered(text)
        if len(numbered) > 1:
            return numbered
        by_question = self._split_by_question_marks(text)
        if len(by_question) > 1:
            return by_question
        return self._split_by_conjunction(text)

    @staticmethod
    def _split_numbered(text: str) -> list[str]:
        marks = list(_NUMBERED_RE.finditer(text))
        if len(marks) < 2:
            return [text]
        parts: list[str] = []
        for i, match in enumerate(marks):
            start = match.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            part = text[start:end].strip()
            if len(part) >= _MIN_PART_CHARS:
                parts.append(part)
        return parts if len(parts) > 1 else [text]

    @staticmethod
    def _split_by_question_marks(text: str) -> list[str]:
        parts = [p.strip() for p in _QUESTION_MARK_RE.split(text)]
        parts = [p for p in parts if len(p) >= _MIN_PART_CHARS]
        if len(parts) <= 1:
            parts = [p.strip() for p in _SEMICOLON_RE.split(text)]
            parts = [p for p in parts if len(p) >= _MIN_PART_CHARS]
        return parts if len(parts) > 1 else [text]

    @staticmethod
    def _split_by_conjunction(text: str) -> list[str]:
        # 「A、B和C分别是多少」：分别=各自对应，不是第二问
        if "分别" in text:
            head, _, tail = text.partition("分别")
            if head.strip() and _RESPECTIVELY_TAIL_RE.match(tail.strip()):
                return [text]
        parts = [p.strip() for p in _CONJUNCTION_RE.split(text)]
        # 连接词拆分的每段都必须够长，否则视为普通短语、不拆
        if len(parts) > 1 and all(len(p) >= 4 for p in parts):
            return parts
        return [text]
