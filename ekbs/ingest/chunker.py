"""四层分块 —— 父块 / 子块切分（`06` §3、`docs/spec/20` §3）。

参数（权威口径来自 `06` §3.2/§3.4）：

| 项 | 值 |
|---|---|
| 父块 | **1024 ~ 1536 token** |
| 子块 | **256 ~ 384 token**（±5 容差） |
| 相邻子块重叠 | **12% ~ 15%** |
| 回退窗口 | 100 字符（切不出完整句子时向后找边界） |
| 表格 | 整块不切；> 2048 token 时按行分组 |
| 子块上限 | 384（适配 rerank ``max_length=512``） |

父子关系落库到 ``ekbs_chunk.parent_id``；``seq`` 在文档内单调递增；
``heading_path`` 取「一级标题 > 二级标题 …」；``page_from`` / ``page_to`` 由调用方给出的
页码映射（MinerU 产物的页码来源见 `docs/spec/20` 的 T8，尚未定稿）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

#: 分块参数（06 §3.2）
PARENT_MIN_TOKENS = 1024
PARENT_MAX_TOKENS = 1536
CHILD_MIN_TOKENS = 256
CHILD_MAX_TOKENS = 384
OVERLAP_MIN_RATIO = 0.12
OVERLAP_MAX_RATIO = 0.15
TABLE_SPLIT_TOKENS = 2048
FALLBACK_WINDOW_CHARS = 100

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_END = "。！？；.!?;\n"


def count_tokens(text: str) -> int:
    """token 近似计数。

    优先用 tiktoken（`02` TC-INDEX-004 要求与云端 usage 校准）；不可用时退化为
    「中文按字、英文按词」的估算，并**不缓存**（估算函数必须便宜）。
    """
    if not text:
        return 0
    try:  # pragma: no cover - 取决于是否装 tiktoken 与网络
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return cjk + max(1, other // 4)


def retrieval_text(heading_path: str | None, content: str) -> str:
    """**检索用文本**：小节标题路径 + 正文（`ekbs_chunk.content` 本身不改）。

    为什么必须带上标题：制度文件里关键限定词常常只在小节标题里
    （「第二条 **住宿标准**」），正文写的是「一线城市每晚不超过 600 元…」。
    只嵌入正文时，查询「住宿标准」与这段正文**没有任何共享词**，
    稠密召回直接落空 —— 实测踩到过（差旅费办法导入后查不到）。

    ⚠️ **口径必须唯一**：入库（:mod:`ekbs.ingest.pipeline`）、分片迁移
    （:mod:`ekbs.permission.changes`）、索引重建（:mod:`ekbs.tools.rebuild_shard`）
    三处都用本函数；任何一处不同，同一个分片里就会混入两套向量语义。

    ⚠️ 这是对文档的**补充决策**（`06` §3 未规定嵌入文本是否含标题），
    已记入 `README.md` 的发现清单待架构负责人确认。
    """
    heading = (heading_path or "").strip()
    body = content or ""
    if not heading:
        return body
    return f"{heading}\n{body}"


@dataclass
class ChunkNode:
    """分块结果（尚未落库）。"""

    chunk_type: str  # parent | child | table | figure
    seq: int
    content: str
    heading_path: str = ""
    page_from: int | None = None
    page_to: int | None = None
    figure_id: str | None = None
    image_path: str | None = None
    #: 该块所属父块在结果列表中的下标（``chunk_type == 'child'`` 时非空）
    parent_index: int | None = None
    token_count: int = 0

    def __post_init__(self) -> None:
        if not self.token_count:
            self.token_count = count_tokens(self.content)


@dataclass
class Section:
    heading_path: str
    text: str
    page_from: int | None = None
    page_to: int | None = None


@dataclass
class ChunkingResult:
    nodes: list[ChunkNode] = field(default_factory=list)

    @property
    def parents(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type in {"parent", "table"}]

    @property
    def children(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type == "child"]


def split_sections(markdown: str, *, page_map: Sequence[tuple[int, int, int]] | None = None) -> list[Section]:
    """按标题层级把 Markdown 拆成小节，并维护 ``heading_path``。

    ``page_map``：``[(char_offset, page_from, page_to), …]``，用于给小节标注页码区间；
    缺省则页码为 None（此时引用卡片只显示文档名）。
    """
    sections: list[Section] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    current_heading = ""
    current_start = 0
    offset = 0

    def flush(end_offset: int) -> None:
        text = "\n".join(buffer).strip()
        if text:
            page_from, page_to = _pages_for(current_start, end_offset, page_map)
            sections.append(
                Section(
                    heading_path=current_heading, text=text, page_from=page_from, page_to=page_to
                )
            )

    for line in markdown.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            flush(offset)
            buffer = []
            level = len(match.group(1))
            title = match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_heading = " > ".join(t for _, t in heading_stack)
            current_start = offset + len(line) + 1
        else:
            buffer.append(line)
        offset += len(line) + 1
    flush(offset)
    return sections


def _pages_for(
    start: int, end: int, page_map: Sequence[tuple[int, int, int]] | None
) -> tuple[int | None, int | None]:
    if not page_map:
        return None, None
    pages = [p for offset, p, _ in page_map if start <= offset <= end] or [page_map[0][1]]
    return min(pages), max(pages)


def chunk_markdown(
    markdown: str,
    *,
    page_map: Sequence[tuple[int, int, int]] | None = None,
    parent_min: int = PARENT_MIN_TOKENS,
    parent_max: int = PARENT_MAX_TOKENS,
    child_min: int = CHILD_MIN_TOKENS,
    child_max: int = CHILD_MAX_TOKENS,
) -> ChunkingResult:
    """Markdown → 父块 + 子块（`06` §3 的口径）。"""
    result = ChunkingResult()
    seq = 0
    for section in split_sections(markdown, page_map=page_map):
        for parent_text in _pack(section.text, parent_min, parent_max):
            seq += 1
            parent = ChunkNode(
                chunk_type="parent",
                seq=seq,
                content=parent_text,
                heading_path=section.heading_path,
                page_from=section.page_from,
                page_to=section.page_to,
            )
            result.nodes.append(parent)
            parent_index = len(result.nodes) - 1

            overlap = int(child_max * OVERLAP_MIN_RATIO)
            for child_text in _pack(parent_text, child_min, child_max, overlap=overlap):
                seq += 1
                result.nodes.append(
                    ChunkNode(
                        chunk_type="child",
                        seq=seq,
                        content=child_text,
                        heading_path=section.heading_path,
                        page_from=section.page_from,
                        page_to=section.page_to,
                        parent_index=parent_index,
                    )
                )
    return result


def _pack(text: str, min_tokens: int, max_tokens: int, *, overlap: int = 0) -> list[str]:
    """把文本按 token 区间打包；切点尽量落在句子边界（回退窗口 100 字符）。"""
    paragraphs = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    blocks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if count_tokens(candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            blocks.append(current)
        if count_tokens(paragraph) <= max_tokens:
            current = paragraph
        else:  # 单段就超限：按句子硬切
            for piece in _split_long(paragraph, max_tokens, overlap):
                blocks.append(piece)
            current = ""
    if current:
        blocks.append(current)

    # 合并过小的块，避免出现 20 token 的碎块
    merged: list[str] = []
    for block in blocks:
        if merged and count_tokens(merged[-1]) < min_tokens:
            merged[-1] = f"{merged[-1]}\n\n{block}"
        else:
            merged.append(block)

    # 护栏：尾部余量若小到「退化碎块」程度，直接并入前一块。
    # 说明：尾部余量**允许**小于 min_tokens（父块总长不足以切成两个合规子块时，
    # 这是数学上无法避免的，例如 500 token 的父块无法切成两个都 ≥256 的子块）；
    # 但不允许小到几十 token 的碎片 —— 那种块进不了上下文的语义，只会污染 rerank。
    degenerate = max(32, min_tokens // 4)
    if len(merged) >= 2 and count_tokens(merged[-1]) < degenerate:
        merged[-2] = f"{merged[-2]}\n\n{merged[-1]}"
        merged.pop()
    return merged


def _split_long(text: str, max_tokens: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_tokens)  # 中文接近 1 token/字，先按字符推进
        while end < len(text) and text[end] not in _SENTENCE_END and end - (start + max_tokens) < FALLBACK_WINDOW_CHARS:
            end += 1
        if end < len(text) and text[end] in _SENTENCE_END:
            end += 1
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]
