"""钉钉 ``sampleMarkdown`` 子集排版。

官方消息类型说明（`sampleMarkdown`）：
https://open.dingtalk.com/document/development/robot-message-type

支持：一至六级标题、引用 ``>``、加粗/斜体、链接、图片、有序/无序列表；
换行建议 ``\\n`` 前后各加 2 个空格（软换行）。

不支持：管道表、代码围栏、水平线 ``---``、HTML。管道表在进入本函数前
应已转成栏目明示块（见 ``markdown_tables_to_labeled_blocks``）。
"""

from __future__ import annotations

import re

_BRACKET_HEADING_RE = re.compile(r"^【([^】]+)】\s*$")
_SECTION_PLAIN_RE = re.compile(
    r"^(参考资料|你可以接着问|你可以试试问|直接发送文字)[：:]\s*$"
)
_CITE_LINE_RE = re.compile(r"^\[(\d+)\]\s+(.+)$")
_FENCE_RE = re.compile(r"^```")
_HR_RE = re.compile(r"^\s*-{3,}\s*$")
_HTML_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_BOLD_ITALIC_RE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*")


def format_for_sample_markdown(text: str) -> str:
    """把答案收成钉钉 ``sampleMarkdown`` 可稳定渲染的子集。"""
    from ekbs.retrieval.answer_table_format import markdown_tables_to_labeled_blocks

    body = markdown_tables_to_labeled_blocks(str(text or ""))
    lines = [_HTML_RE.sub("", ln.rstrip()) for ln in body.splitlines()]
    lines = _strip_fences_and_hrs(lines)

    out: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            index += 1
            continue

        bracket = _BRACKET_HEADING_RE.match(stripped)
        if bracket:
            _ensure_blank_before_block(out)
            out.append(f"#### {bracket.group(1).strip()}")
            index += 1
            # 紧随的 key：value 行用软换行收成一块，避免钉钉合成一段
            kv_block: list[str] = []
            while index < len(lines):
                nxt = lines[index].strip()
                if not nxt:
                    break
                if (
                    _BRACKET_HEADING_RE.match(nxt)
                    or _SECTION_PLAIN_RE.match(nxt)
                    or nxt.startswith("#")
                    or nxt.startswith("- ")
                    or nxt.startswith("> ")
                    or _CITE_LINE_RE.match(nxt)
                    or re.match(r"^\d+\.\s+", nxt)
                ):
                    break
                kv_block.append(nxt)
                index += 1
            if kv_block:
                out.extend(_soft_break_lines(kv_block))
            continue

        section = _SECTION_PLAIN_RE.match(stripped)
        if section:
            _ensure_blank_before_block(out)
            out.append(f"### {section.group(1)}")
            index += 1
            continue

        cite = _CITE_LINE_RE.match(stripped)
        if cite:
            # 引用改无序列表，和正文 [n] 角标对应，列表在钉钉里更易扫
            out.append(f"- **[{cite.group(1)}]** {cite.group(2).strip()}")
            index += 1
            continue

        if _HR_RE.match(stripped):
            if out and out[-1] != "":
                out.append("")
            index += 1
            continue

        out.append(stripped)
        index += 1

    return _collapse_blank_lines(out).strip()


def markdown_preview_title(text: str, *, fallback: str = "知识库回答") -> str:
    """``title`` 是会话列表首屏透出文案，取首行并去掉 Markdown 标记。"""
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if not cleaned:
            continue
        cleaned = _BOLD_ITALIC_RE.sub(
            lambda m: m.group(1) or m.group(2) or "", cleaned
        )
        cleaned = cleaned.strip()
        if cleaned:
            return cleaned[:40] + ("…" if len(cleaned) > 40 else "")
    return fallback


def _strip_fences_and_hrs(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            if out and out[-1] != "":
                out.append("")
            continue
        if _HR_RE.match(line.strip()) and not in_fence:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return out


def _soft_break_lines(lines: list[str]) -> list[str]:
    """官方建议软换行：``\\n`` 前加两个空格，避免多行被粘成一段。"""
    if not lines:
        return []
    if len(lines) == 1:
        return [lines[0]]
    return [f"{line}  " for line in lines[:-1]] + [lines[-1]]


def _ensure_blank_before_block(out: list[str]) -> None:
    if out and out[-1] != "":
        out.append("")


def _collapse_blank_lines(lines: list[str]) -> str:
    out: list[str] = []
    blank = False
    for line in lines:
        if line == "":
            if blank:
                continue
            blank = True
            out.append("")
            continue
        blank = False
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
