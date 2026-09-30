"""内容安全闸门 —— `04` §6.4 的四层机制（文档里的示例模块名是
``ekbs/service/content_guard.py``，本仓库用扁平模块 ``ekbs/security.py``，职责一致）。

| 层 | 机制 | 处理动作 |
|---|---|---|
| L0 | 正则识别手机号/身份证/银行卡 | **打码后继续**（不拦截） |
| L1 | 本地敏感词库 + AC 自动机（``pyahocorasick``），**支持热加载** | 命中 → **拦截** + 提示 + 审计 |
| L3 | 提示词注入特征 | 命中 → **降级为纯检索模式**（不给生成） |
| L4 | 输出侧检测未授权文档名 | 命中 → 替换为「内容受保护」 |

两处对文档示例的**有意修正**（以 `04` §6.4 的表格为准，表格才是语义来源）：

1. 示例代码把「提示词注入」也归为 ``blocked=True``；表格写的是「降级为纯检索模式（不给生成）」。
   本实现返回独立的 ``retrieve_only`` 动作 —— 用户仍能拿到检索结果，只是不走 LLM。
2. 热加载在示例里只是注释提到；本实现按文件 ``mtime`` 自动重载，
   因为生产上改敏感词库不该重启 ``ekbs-api``。

**为什么这层必须存在**：它是唯一在「用户输入 → 云端 LLM」之间做本地拦截的环节。
没有它，命中敏感词的问题会直接出网到云端模型（`00` §7 的合规面）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ekbs.logging import get_logger

log = get_logger(component="security")

#: PII 打码规则（逐字取自 `04` §6.4）：捕获首尾、中间打码
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "mobile": re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"),
    "idcard": re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)"),
    "bankcard": re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"),
}

#: 提示词注入特征（逐字取自 `04` §6.4）
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"忽略(以上|上述|之前).{0,10}(规则|指令|提示)"),
    re.compile(r"(system\s*prompt|系统提示词|开发者消息)", re.IGNORECASE),
    re.compile(r"你现在是|扮演.{0,10}(管理员|超级用户)"),
    re.compile(r"列出(所有|全部).{0,6}(文档|知识库|文件)"),
]

#: 越过安全闸门时的占位文本
PROTECTED_PLACEHOLDER = "内容受保护"

ACTION_ALLOW = "allow"
ACTION_MASKED = "masked"
ACTION_BLOCKED = "blocked"
ACTION_RETRIEVE_ONLY = "retrieve_only"


@dataclass
class GuardDecision:
    action: str
    reason: str = ""
    hits: list[str] = field(default_factory=list)
    text: str = ""
    pii_masked: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_BLOCKED

    @property
    def retrieve_only(self) -> bool:
        return self.action == ACTION_RETRIEVE_ONLY

    @property
    def allowed(self) -> bool:
        return self.action in {ACTION_ALLOW, ACTION_MASKED}


class SensitiveWordFilter:
    """L1：AC 自动机敏感词过滤，按文件 mtime **热加载**。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._automaton = None
        self._mtime: float | None = None
        self.word_count = 0
        self.reload(force=True)

    # ------------------------------------------------------------------

    def reload(self, *, force: bool = False) -> bool:
        """文件变更时重建自动机；返回是否真的重载了。"""
        try:
            mtime = self.path.stat().st_mtime if self.path.is_file() else None
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False

        import ahocorasick  # pyahocorasick

        automaton = ahocorasick.Automaton()
        count = 0
        if mtime is not None:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                word = line.strip()
                if word and not word.startswith("#"):
                    automaton.add_word(word, word)
                    count += 1
        automaton.make_automaton()
        self._automaton = automaton
        self._mtime = mtime
        self.word_count = count
        log.info(
            "sensitive_filter_loaded",
            path=str(self.path),
            words=count,
            reloaded=not force,
        )
        return True

    def match(self, text: str) -> list[str]:
        """返回命中的词（去重、排序）。文件变更会自动重载。"""
        self.reload()
        if self._automaton is None or self.word_count == 0:
            return []
        return sorted({word for _, word in self._automaton.iter(text or "")})


def mask_pii(text: str) -> tuple[str, list[str]]:
    """L0：PII 打码（返回打码后文本与命中类别）。打码后**继续处理**，不拦截。"""
    masked = text or ""
    hits: list[str] = []
    for name, pattern in PII_PATTERNS.items():
        if pattern.search(masked):
            hits.append(name)
            replacement = r"\1****\2" if name == "mobile" else r"\1********\2"
            masked = pattern.sub(replacement, masked)
    return masked, hits


def detect_injection(text: str) -> list[str]:
    """L3：提示词注入特征命中（返回命中的正则原文，便于审计）。"""
    return [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(text or "")]


class ContentGuard:
    """四层闸门的门面（输入侧 + 输出侧）。"""

    def __init__(self, *, sensitive_words_path: str | Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self.filter = SensitiveWordFilter(sensitive_words_path)
        #: 未授权文档名缓存（仅在输出侧用，按需填充）
        self._unauthorized_titles: list[str] = []

    # ------------------------------------------------------------------
    # 输入侧
    # ------------------------------------------------------------------

    def check_input(self, text: str) -> GuardDecision:
        """L1 敏感词（拦截）→ L3 注入（降级）→ L0 PII（打码后继续）。"""
        if not self.enabled:
            return GuardDecision(ACTION_ALLOW, "disabled", text=text)

        hits = self.filter.match(text)
        if hits:
            return GuardDecision(
                ACTION_BLOCKED, f"CONTENT_BLOCKED: 命中敏感词 {hits[:3]}", hits=hits, text=text
            )

        injection = detect_injection(text)
        if injection:
            return GuardDecision(
                ACTION_RETRIEVE_ONLY,
                "INJECTION_SUSPECTED: 降级为纯检索模式（不启用生成）",
                hits=injection,
                text=text,
            )

        masked, pii = mask_pii(text)
        if pii:
            return GuardDecision(
                ACTION_MASKED, f"PII_MASKED: {pii}", hits=pii, text=masked, pii_masked=pii
            )
        return GuardDecision(ACTION_ALLOW, "ok", text=text)

    # ------------------------------------------------------------------
    # 输出侧
    # ------------------------------------------------------------------

    def check_output(
        self, answer: str, *, unauthorized_titles: Sequence[str] = ()
    ) -> GuardDecision:
        """L4：答案里出现**未授权文档名** → 替换为「内容受保护」。

        只在调用方给出未授权标题清单时有意义（清单由权限层按当前用户算，见
        :meth:`ekbs.permission.acl.DocumentGate.unauthorized_titles`）。
        """
        if not self.enabled or not answer:
            return GuardDecision(ACTION_ALLOW, "ok", text=answer)
        hits = [title for title in unauthorized_titles if title and title in answer]
        if not hits:
            return GuardDecision(ACTION_ALLOW, "ok", text=answer)
        masked = answer
        for title in hits:
            masked = masked.replace(title, PROTECTED_PLACEHOLDER)
        return GuardDecision(
            ACTION_MASKED, f"OUTPUT_TITLE_LEAK: {hits[:3]}", hits=list(hits), text=masked
        )
