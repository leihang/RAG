"""确定性 Doc2Query —— 为检索块生成「假设问句」写入 FTS（不写 FAISS）。

背景（课程 18 实测）：口语问法与制度陈述错位时，原文 BM25 召回弱；
为每个 chunk 生成多样化问题再建稀疏索引，准确率可从 ~67% 提到接近满分。

本实现**不调用 LLM**（可从 chunk 内容重建，解决 K16「FTS 重建丢问题行」）：
用标题路径 + 正文关键词套模板生成问句；入库与 FTS 重建同一函数即可复现。
"""

from __future__ import annotations

import re
from typing import Sequence

#: 从正文抽关键词时丢掉的虚词
_STOP = frozenset(
    {
        "的", "了", "和", "与", "或", "及", "等", "是", "在", "为", "对", "按",
        "将", "把", "被", "由", "其", "本", "该", "各", "每", "一", "二", "三",
        "根据", "按照", "规定", "办法", "制度", "条款", "内容", "如下", "以上",
    }
)

_SENT_SPLIT = re.compile(r"[。！？；;\n]+")
_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_\-]{1,24}")


def generate_questions(
    *,
    content: str,
    heading_path: str | None = None,
    chunk_type: str = "child",
    max_questions: int = 4,
) -> list[str]:
    """为一块可检索内容生成假设问句（去重、保序）。

    模板覆盖：是什么 / 如何规定 / 多少 / 谁负责 / 图片索取（figure）。
    """
    heading = _leaf_heading(heading_path)
    keywords = _keywords(content, limit=6)
    questions: list[str] = []

    if heading:
        questions.append(f"{heading}是什么？")
        questions.append(f"{heading}有哪些规定？")
        questions.append(f"关于{heading}怎么处理？")

    if chunk_type == "figure":
        caption = (content or "").strip().splitlines()[0] if content else ""
        caption = re.sub(r"^\[图片[^\]]*\]\s*", "", caption).strip() or heading or "现场照片"
        questions.extend(
            [
                f"{caption}的照片",
                f"有没有{caption}的图片",
                f"请出示{caption}",
            ]
        )
    elif chunk_type == "table":
        subject = _table_subject(content) or heading
        if subject:
            questions.extend(
                [
                    f"{subject}标准是多少？",
                    f"{subject}表格里有什么？",
                    f"查一下{subject}",
                ]
            )

    for word in keywords[:4]:
        if heading and word in heading:
            continue
        questions.append(f"{word}怎么规定的？")
        questions.append(f"{word}是多少？")

    # 首句改写成「……吗？」
    first = _first_sentence(content)
    if first and 8 <= len(first) <= 40:
        questions.append(first.rstrip("。！？") + "吗？")

    return _dedupe(questions, limit=max_questions)


def fts_doc2query_rows(
    rows: Sequence[tuple[int, str, str, str | None, str]],
    *,
    max_questions: int = 4,
) -> list[tuple[int, str, str, str | None]]:
    """``(chunk_id, shard_id, content, heading, chunk_type)`` → 额外 FTS 行。

    规格（06 §5.4）：``content_tok = jieba(问题 + 原文)``，同 ``chunk_id``，不写 FAISS。
    """
    extra: list[tuple[int, str, str, str | None]] = []
    for chunk_id, shard_id, content, heading, chunk_type in rows:
        for question in generate_questions(
            content=content,
            heading_path=heading,
            chunk_type=chunk_type,
            max_questions=max_questions,
        ):
            # 问题在前：口语查询更容易命中；原文垫底保关键词
            blended = f"{question}\n{content}"
            extra.append((int(chunk_id), shard_id, blended, heading))
    return extra


def _leaf_heading(heading_path: str | None) -> str:
    if not heading_path:
        return ""
    parts = [p.strip() for p in str(heading_path).replace(">", "/").split("/") if p.strip()]
    if not parts:
        return ""
    leaf = parts[-1]
    # 去掉「第二条」之类前缀编号，保留主题词
    leaf = re.sub(r"^第[一二三四五六七八九十百千零〇两\d]+[条款项章节]\s*", "", leaf)
    leaf = re.sub(r"^\d+(\.\d+)*\s*", "", leaf)
    return leaf.strip()[:24]


def _table_subject(content: str) -> str:
    for line in (content or "").splitlines():
        if line.startswith("表名:"):
            return line.split(":", 1)[1].strip()[:24]
        if line.startswith("核心实体:"):
            return line.split(":", 1)[1].strip()[:24]
    return ""


def _first_sentence(content: str) -> str:
    for part in _SENT_SPLIT.split(content or ""):
        text = part.strip()
        if len(text) >= 6:
            return text[:60]
    return ""


def _keywords(content: str, *, limit: int) -> list[str]:
    counts: dict[str, int] = {}
    for token in _TOKEN.findall(content or ""):
        if token in _STOP or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [w for w, _ in ranked[:limit]]


def _dedupe(items: Sequence[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out
