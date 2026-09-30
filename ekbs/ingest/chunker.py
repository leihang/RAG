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

切分优先级（`20` §3.2）：Markdown ``#`` 标题 → 中文条款
（``第X章/节/条``）→ ``X.X`` 编号 → 句子聚合 → 长度兜底。

``heading_path`` **不得为空**：缺标题时用 ``doc_title`` / ``文档`` 作根。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ekbs.logging import get_logger

#: 分块参数（06 §3.2）
log = get_logger(component="chunker")

PARENT_MIN_TOKENS = 1024
PARENT_MAX_TOKENS = 1536
CHILD_MIN_TOKENS = 256
CHILD_MAX_TOKENS = 384
OVERLAP_MIN_RATIO = 0.12
OVERLAP_MAX_RATIO = 0.15
TABLE_SPLIT_TOKENS = 2048
FALLBACK_WINDOW_CHARS = 100

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
#: 中文制度条款行 → 当作标题层级（MinerU 常不给 text_level）
_CLAUSE_HEADING_RE = re.compile(
    r"^第([一二三四五六七八九十百千零〇两\d]+)([章节条款项])\s*(.*)$"
)
_DOTTED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+(\S.*)$")
_CLAUSE_LEVEL = {"章": 2, "节": 3, "条": 3, "款": 4, "项": 4}
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
    """
    heading = (heading_path or "").strip()
    body = content or ""
    if not heading:
        return body
    return f"{heading}\n{body}"


def ensure_heading_path(heading_path: str | None, *, doc_title: str = "") -> str:
    """``heading_path`` 永不空（`20` §3.3）：缺则用文档标题，再缺用「文档」。"""
    path = (heading_path or "").strip()
    if path:
        return path
    root = (doc_title or "").strip()
    return root or "文档"


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
class FigureInput:
    """文档里抽出的图片（**已落盘**），供分块器生成可检索的 figure 块。

    ``image_path`` 是**相对 ``data_dir``** 的路径 —— ``GET /assets/figure/{chunk_id}``
    正是按 data_dir 解析 ``ekbs_chunk.image_path``，必须同一基准；解析层报回的是相对
    ``assets_dir`` 的路径，换算由调用方（worker / import）负责。
    """

    image_path: str
    caption: str = ""
    ocr_text: str = ""
    page: int | None = None
    figure_id: str = ""


@dataclass
class TableInput:
    """文档里抽出的表格，供分块器生成可检索的 table 块。"""

    body: str
    caption: str = ""
    page: int | None = None
    table_id: str = ""


@dataclass
class ChunkingResult:
    nodes: list[ChunkNode] = field(default_factory=list)

    @property
    def parents(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type in {"parent", "table"}]

    @property
    def children(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type == "child"]

    @property
    def figures(self) -> list[ChunkNode]:
        """图片块：**与 child 一样可检索**（向量化 + 进 FTS），但保留自己的类型，
        以便 `/assets/figure/{chunk_id}` 能回源出图、前端把引用渲染成图。"""
        return [n for n in self.nodes if n.chunk_type == "figure"]

    @property
    def tables(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type == "table"]


def split_sections(markdown: str, *, page_map: Sequence[tuple[int, int, int]] | None = None) -> list[Section]:
    """按标题层级把 Markdown 拆成小节，并维护 ``heading_path``。

    标题来源（按优先级命中即切）：
    1. Markdown ``#`` … ``######``
    2. 中文条款行 ``第X章/节/条/款/项``（制度 PDF 最常见，MinerU 常不标 text_level）
    3. 点分编号 ``3.2 标题``

    ``page_map``：``[(char_offset, page_from, page_to), …]``；缺省则页码为 None。
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

    def push_heading(level: int, title: str, line_len: int) -> None:
        nonlocal buffer, current_heading, current_start
        flush(offset)
        buffer = []
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        current_heading = " > ".join(t for _, t in heading_stack)
        current_start = offset + line_len + 1

    for line in markdown.splitlines():
        stripped = line.strip()
        match = _HEADING_RE.match(stripped)
        clause = _CLAUSE_HEADING_RE.match(stripped) if not match else None
        dotted = _DOTTED_HEADING_RE.match(stripped) if not match and not clause else None
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            push_heading(level, title, len(line))
        elif clause:
            kind = clause.group(2)
            level = _CLAUSE_LEVEL.get(kind, 3)
            title = f"第{clause.group(1)}{kind}"
            rest = clause.group(3).strip()
            if rest:
                title = f"{title} {rest}"
            push_heading(level, title, len(line))
        elif dotted:
            title = f"{dotted.group(1)} {dotted.group(2).strip()}"
            push_heading(3, title, len(line))
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
    figures: Sequence[FigureInput] = (),
    tables: Sequence[TableInput] = (),
    doc_title: str = "",
) -> ChunkingResult:
    """Markdown → 父块 + 子块 + 图片块 + 表格块（`06` §3 的口径）。

    ``figures`` 由解析层给出（图片已落盘）。**只有"有文本"的图片才建索引块**：
    题注与 OCR 都为空时，图片仍留在磁盘上可被浏览，但不进向量/FTS ——
    给一堆无文字的图造近似重复的块，只会污染检索。

    ``tables`` 走确定性 ``information_block`` 序列化，整表独立成 ``chunk_type=table``，
    不与正文 parent/child 重复索引。

    ``doc_title``：小节无标题时注入 ``heading_path`` 根，避免检索用文本丢限定词。

    短节若只能切出与父块正文相同的一块 child，则**跳过 child**：父块作为叶子入向量
    （见 :mod:`ekbs.ingest.retrievable`），避免同文双索引。
    """
    from ekbs.ingest.table_serialize import serialize_table_body

    result = ChunkingResult()
    seq = 0
    root = ensure_heading_path("", doc_title=doc_title)
    for section in split_sections(markdown, page_map=page_map):
        heading = ensure_heading_path(section.heading_path, doc_title=doc_title)
        # 小节完全无标题路径时，用文档根；已有条款路径则保留
        if not section.heading_path.strip():
            heading = root
        for parent_text in _pack(section.text, parent_min, parent_max):
            seq += 1
            parent = ChunkNode(
                chunk_type="parent",
                seq=seq,
                content=parent_text,
                heading_path=heading,
                page_from=section.page_from,
                page_to=section.page_to,
            )
            result.nodes.append(parent)
            parent_index = len(result.nodes) - 1

            # 短节：子块只能切出 1 块且与父块正文相同 → 不建 child。
            # 父块本身就是叶子，按 retrievable 叶子规则入向量，避免同文双索引。
            overlap = int(child_max * OVERLAP_MIN_RATIO)
            child_texts = _pack(parent_text, child_min, child_max, overlap=overlap)
            if (
                len(child_texts) == 1
                and child_texts[0].strip() == parent_text.strip()
            ):
                continue
            for child_text in child_texts:
                seq += 1
                result.nodes.append(
                    ChunkNode(
                        chunk_type="child",
                        seq=seq,
                        content=child_text,
                        heading_path=heading,
                        page_from=section.page_from,
                        page_to=section.page_to,
                        parent_index=parent_index,
                    )
                )

    for figure in figures:
        content = _figure_search_text(figure)
        if not content:
            log.debug("figure_skipped_no_text", image_path=figure.image_path)
            continue
        seq += 1
        fig_heading = ensure_heading_path(figure.caption, doc_title=doc_title)
        result.nodes.append(
            ChunkNode(
                chunk_type="figure",
                seq=seq,
                content=content,
                heading_path=fig_heading,
                page_from=figure.page,
                page_to=figure.page,
                figure_id=figure.figure_id or None,
                image_path=figure.image_path,
            )
        )

    for table in tables:
        blocks = serialize_table_body(
            caption=table.caption, body=table.body, table_id=table.table_id
        )
        table_heading = ensure_heading_path(
            table.caption or (f"表 {table.table_id}" if table.table_id else ""),
            doc_title=doc_title,
        )
        for block in blocks:
            seq += 1
            result.nodes.append(
                ChunkNode(
                    chunk_type="table",
                    seq=seq,
                    content=block,
                    heading_path=table_heading,
                    page_from=table.page,
                    page_to=table.page,
                    figure_id=table.table_id or None,
                )
            )
    return result


def _figure_search_text(figure: FigureInput) -> str:
    """图片的可检索文本 = 题注 + 图中文字（OCR）；两者都空则返回空串（不建索引块）。"""
    parts: list[str] = []
    if figure.caption.strip():
        parts.append(f"[图片] {figure.caption.strip()}")
    if figure.ocr_text.strip():
        if not parts:
            parts.append("[图片]")
        parts.append(figure.ocr_text.strip())
    return "\n".join(parts).strip()


def _pack(text: str, min_tokens: int, max_tokens: int, *, overlap: int = 0) -> list[str]:
    """把文本按 token 区间打包；切点尽量落在句子边界（回退窗口 100 字符）。

    ``overlap > 0`` 时在**相邻块之间**注入尾部重叠（`20` §3.2 / TC-INDEX-003），
    不只在「单段超长硬切」时生效。
    """
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
    degenerate = max(32, min_tokens // 4)
    if len(merged) >= 2 and count_tokens(merged[-1]) < degenerate:
        merged[-2] = f"{merged[-2]}\n\n{merged[-1]}"
        merged.pop()

    if overlap > 0 and len(merged) >= 2:
        merged = _apply_child_overlap(merged, overlap)
    return merged


def _tail_by_tokens(text: str, token_budget: int) -> str:
    """取文本尾部约 ``token_budget`` token，尽量落在句首。"""
    if token_budget <= 0 or not text:
        return ""
    if count_tokens(text) <= token_budget:
        return text.strip()
    # 从尾部按字符回退，直到 token 预算够（CJK ≈ 1 token/字）
    start = max(0, len(text) - token_budget * 2)
    chunk = text[start:]
    while start > 0 and count_tokens(chunk) < token_budget:
        start = max(0, start - token_budget)
        chunk = text[start:]
    # 向前找到句末，使重叠从完整句开始
    for i, ch in enumerate(chunk):
        if ch in _SENTENCE_END and count_tokens(chunk[i + 1 :]) >= max(1, token_budget // 2):
            chunk = chunk[i + 1 :]
            break
    return chunk.strip()


def _apply_child_overlap(blocks: list[str], overlap_tokens: int) -> list[str]:
    """相邻子块注入尾部重叠：块 i 以块 i-1 的尾部作前缀。"""
    out = [blocks[0]]
    for i in range(1, len(blocks)):
        prev = blocks[i - 1]
        cur = blocks[i]
        tail = _tail_by_tokens(prev, overlap_tokens)
        if not tail:
            out.append(cur)
            continue
        # 已含相同前缀则不重复叠
        if cur.startswith(tail) or tail in cur[: min(len(cur), len(tail) + 32)]:
            out.append(cur)
        else:
            out.append(f"{tail}\n{cur}")
    return out


def _split_long(text: str, max_tokens: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_tokens)  # 中文接近 1 token/字，先按字符推进
        while (
            end < len(text)
            and text[end] not in _SENTENCE_END
            and end - (start + max_tokens) < FALLBACK_WINDOW_CHARS
        ):
            end += 1
        if end < len(text) and text[end] in _SENTENCE_END:
            end += 1
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]
