"""四层分块 —— 父块 / 子块切分（`06` §3、`docs/spec/20` §3）。

参数（权威口径来自 `06` §3.2/§3.4）：

| 项 | 值 |
|---|---|
| 父块 | **1024 ~ 2048 token**（与表格容器同上限） |
| 子块 | **300 ~ 500 token**（±5 容差） |
| 相邻子块重叠 | **12% ~ 15%** |
| 回退窗口 | 100 字符（切不出完整句子时向后找边界） |
| 表格 | 整块不切；> 2048 token 时按行分组 |
| 子块上限 | 500（正文；表格叶子仍见 ``TABLE_LEAF_TOKENS``） |

切分优先级（`20` §3.2）：Markdown ``#`` 标题 → 中文条款
（``第X章/节/条``）→ ``X.X`` 编号 → 句子聚合 → 长度兜底。

**条款各自成块**：``第X条``、``4.1`` 这种已经切开的条款不再按长度并进同一块。
一条写得很长时，仍只在这一条内部按 1024～2048 / 300～500 切开。

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
PARENT_MAX_TOKENS = 2048
CHILD_MIN_TOKENS = 300
CHILD_MAX_TOKENS = 500
OVERLAP_MIN_RATIO = 0.12
OVERLAP_MAX_RATIO = 0.15
#: 只有标题/占位符、没有正文的节（如 ``[表格 1]``、``[图片]``）
_PLACEHOLDER_ONLY_RE = re.compile(r"^\[(?:表格|图片)\s*\d*\]$")
TABLE_SPLIT_TOKENS = 2048
#: 表格**叶子**预算（独立于正文 child；正文已调到 300~500，表叶仍用 384 防稀释）：
#: 整表超过这个就拆「行组叶子」，每叶重复表头、逐块嵌入。
TABLE_LEAF_TOKENS = 384
#: 表格父容器**硬上限**（与 ``table_serialize.TABLE_MAX_TOKENS`` 对齐）：DB 里任何块都
#: 不得超过它。整表序列化 + 上下文超限时按行/表头打满分段；检索命中叶子时只展开
#: **直系父段**（不再把同 ``table_group`` 兄妹段一并塞进 prompt）。
TABLE_CONTAINER_MAX_TOKENS = 2048
#: 分段预算的安全余量：分段标记行的长度随「第 i/n 段」变化，留一点防越界。
_TABLE_SEGMENT_MARGIN_TOKENS = 16
FALLBACK_WINDOW_CHARS = 100

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
#: 中文制度条款行 → 当作标题层级（MinerU 常不给 text_level）
_CLAUSE_HEADING_RE = re.compile(
    r"^第([一二三四五六七八九十百千零〇两\d]+)([章节条款项])\s*(.*)$"
)
#: 点分/一级编号标题：``4.1 设备`` / ``4．1 设备`` / ``1. 目的`` / ``2．范围``
#: （一级编号必须带点号，避免把 ``123 某某`` 误当成标题）
_DOTTED_HEADING_RE = re.compile(
    r"^(\d+(?:[.．]\d+)+|\d+[.．])\s*(\S.*)$"
)
_CLAUSE_LEVEL = {"章": 2, "节": 3, "条": 3, "款": 4, "项": 4}
_SENTENCE_END = "。！？；.!?;\n"

#: 条款/编号行 —— 这类行**本身就是内容**，最容易被"当标题"吞掉
_CLAUSE_LIKE_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千零〇两\d]+[章节条款项]|\d+(?:[.．]\d+)+\s|\d+[．.、])"
)
#: 「标题行同时也是正文」的判据：短标题（如 `第一条 年假天数`）只当标题，
#: 而"整条就是内容"的行（带句读/很长）必须**同时**进正文，否则整行文字会消失。
_CONTENT_PUNCT = "。；！？：，,;"
#: 句末标点：无 Markdown 标记、且以句末标点收尾的行**就是正文**（不是标题）。
#: 实测 ``2.1适用于各部门。`` 只有 10 字、短于 ``_HEADING_BODY_MIN_CHARS``，被当成
#: 嵌套标题 → ``2．范围`` 空壳被塞到文档末尾。标题行（``## 9．支持文件。``）带 ``#``
#: 不适用本规则。
_SENTENCE_END_PUNCT = "。！？；"


def _is_unmarked_sentence(line: str) -> bool:
    stripped = (line or "").strip()
    return bool(stripped) and not stripped.startswith("#") and stripped[-1] in _SENTENCE_END_PUNCT
_HEADING_BODY_MIN_CHARS = 12
_HEADING_BODY_LONG_CHARS = 40


def _looks_like_content(stripped: str) -> bool:
    """这一行是否"本身就是内容"（而不是纯标题）。"""
    if len(stripped) >= _HEADING_BODY_LONG_CHARS:
        return True
    return len(stripped) >= _HEADING_BODY_MIN_CHARS and any(
        ch in stripped for ch in _CONTENT_PUNCT
    )


def is_substantive_line(line: str) -> bool:
    """丢了就应算缺陷的"实质内容行"（页眉/页码/水印不会命中）。"""
    stripped = line.strip()
    if len(stripped) < 8:
        return False
    if _CLAUSE_LIKE_RE.match(stripped):
        return True
    return len(stripped) >= 12 and any(ch in stripped for ch in _CONTENT_PUNCT)


#: Markdown 标记（`## 标题` / `- 列表` / `1. 序号`）——比对前要剥掉：
#: 分块器把 `## 项目背景` 存进 heading_path 时**不带 `##`**，不剥就会把每条标题都误判成丢失。
_MD_MARK_RE = re.compile(r"^[\s>]*(#{1,6}|[-*+]|\d+[.)])[\s]+")


def _strip_md_marks(text: str) -> str:
    """逐行剥掉 Markdown 标记并去掉空白，得到可比对的内容串。"""
    return re.sub(
        r"[\s\u3000]", "", "".join(_MD_MARK_RE.sub("", line) for line in (text or "").splitlines())
    )


def content_coverage(
    source_text: str, nodes: Sequence["ChunkNode"]
) -> tuple[float, tuple[str, ...]]:
    """**零丢失不变量**：源文本每行是否都出现在某个块的 ``content ∪ heading_path`` 里。

    返回 ``(覆盖率, 丢失的实质内容行)``。为什么要有它：分块把整行当标题时文字会**静默消失**
    （实测交接班制度 22 段丢 11 段：643 字符只剩 206），而空页率/乱码率/空 heading 这些
    闸门**全是 PASS** —— 只有逐行核对能发现。入库时会用它当守卫（见 `ingest/pipeline`）。

    比对前**逐行剥掉 Markdown 标记**：分块器把 ``## 项目背景`` 存进 heading_path 时
    不带 ``##``，不剥就会把每条 Markdown 标题都误判成"丢失"（守卫天天喊狼来了就没人看了）。
    """
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    if not lines:
        return 1.0, ()
    stored = "".join(
        _strip_md_marks((node.content or "") + " " + (node.heading_path or "")) for node in nodes
    )
    lost = [
        line for line in lines if is_substantive_line(line) and _strip_md_marks(line) not in stored
    ]
    kept = sum(1 for line in lines if _strip_md_marks(line) in stored)
    return kept / len(lines), tuple(lost)


#: 词表文件（~11 MB）属于**部署物**不是源码，放 `var/` 下（git 忽略）。
#: 取不到时**不报错**：回退到 tiktoken 并告警一次，保证分块链路不被词表问题打断。
_TOKENIZER_ENV = "EKBS_TOKENIZER_PATH"

_QWEN_TOKENIZER = None
_QWEN_UNAVAILABLE_LOGGED = False
#: 当前生效口径的标识（`qwen:sdk:<path>` 等），见 `active_token_counter()`
_QWEN_COUNTER_LABEL = ""


def sdk_vocab_file():
    """官方 `dashscope` SDK **随包自带**的 Qwen 词表（`dashscope/resources/qwen.tiktoken`）。

    为什么优先用它：**离线、零下载、与 SDK 同版本**。实测与 HF 的 Qwen3-Embedding
    `tokenizer.json` 在语料/英文/代码/表格/编号/emoji 上**计数逐条相同**（同一套词表），
    所以改用它可以替掉一个部署物（11 MB 词表）+ 一个依赖（`tokenizers`）+ 一次下载步骤。
    """
    import pathlib

    try:
        import dashscope  # 嵌入/生成都靠它，已在 requirements 里
    except Exception:  # noqa: BLE001
        return None
    path = pathlib.Path(dashscope.__file__).resolve().parent / "resources" / "qwen.tiktoken"
    return path if path.is_file() else None


def tokenizer_source():
    """返回 ``(词表文件, 口径标识)``；取不到返回 ``None``。

    优先级：① `EKBS_TOKENIZER_PATH` **显式指定即权威**（指向不存在的路径 = 主动禁用）；
    ② 官方 SDK 自带词表；③ 本地部署的 `tokenizer.json`（老装机兼容）。
    """
    import os
    import pathlib

    env = os.environ.get(_TOKENIZER_ENV, "").strip()
    if env:
        path = pathlib.Path(env).expanduser()
        return (path, f"qwen:env:{path}") if path.is_file() else None
    candidate = sdk_vocab_file()
    if candidate is not None:
        return candidate, f"qwen:sdk:{candidate}"
    for fallback in (
        pathlib.Path(__file__).resolve().parents[2] / "var" / "models" / "qwen-tokenizer.json",
        pathlib.Path.home() / ".ekbs" / "qwen-tokenizer.json",
    ):
        if fallback.is_file():
            return fallback, f"qwen:file:{fallback}"
    return None


def tokenizer_path():
    """当前词表文件路径（取不到返回 ``None``）—— 保留给工具与测试用。"""
    source = tokenizer_source()
    return source[0] if source else None


class _QwenCounter:
    """统一两种词表格式的计数接口。

    * `.tiktoken`（官方格式）→ SDK 自带实现 `QwenTokenizer.encode()`，返回 **id 列表**
    * `.json`（HF 格式）→ `tokenizers.Tokenizer.encode().ids`
    """

    def __init__(self, kind: str, path, impl) -> None:
        self.kind = kind
        self.path = path
        self.impl = impl

    def count(self, text: str) -> int:
        if self.kind == "sdk":
            return len(self.impl.encode(text))
        return len(self.impl.encode(text, add_special_tokens=False).ids)


def _qwen_encoding():
    """懒加载 Qwen 计数器（进程内只加载一次）；不可用返回 ``None``。"""
    global _QWEN_TOKENIZER, _QWEN_UNAVAILABLE_LOGGED, _QWEN_COUNTER_LABEL
    if _QWEN_TOKENIZER is not None:
        return _QWEN_TOKENIZER
    source = tokenizer_source()
    if source is None:
        return None
    path, label = source
    try:
        if path.suffix == ".tiktoken":
            from dashscope.tokenizers.qwen_tokenizer import QwenTokenizer

            _QWEN_TOKENIZER = _QwenCounter("sdk", path, QwenTokenizer(str(path)))
        else:
            from tokenizers import Tokenizer

            _QWEN_TOKENIZER = _QwenCounter("file", path, Tokenizer.from_file(str(path)))
        _QWEN_COUNTER_LABEL = label
        log.info("token_counter_ready", counter=label)
        return _QWEN_TOKENIZER
    except Exception as exc:  # noqa: BLE001 - 词表问题不该打断入库
        if not _QWEN_UNAVAILABLE_LOGGED:
            log.warning(
                "token_counter_qwen_unavailable", path=str(path), error=str(exc)[:160],
                hint="回退 tiktoken；官方词表随 dashscope 包，损坏时重装该包即可",
            )
            _QWEN_UNAVAILABLE_LOGGED = True
        return None


def active_token_counter() -> str:
    """当前生效的计数口径（排障与 TC-INDEX-004 校准都要看它）。

    形如 `qwen:sdk:<词表路径>` / `qwen:file:<路径>` / `qwen:env:<路径>`，
    或回退时的 `tiktoken:cl100k_base` / `heuristic:cjk`。
    **它同时是校准的绑定项**：换词表口径 = 旧校准失效（见 `ekbs.ingest.token_calib`）。
    """
    if _qwen_encoding() is not None:
        return _QWEN_COUNTER_LABEL
    try:  # pragma: no cover
        import tiktoken  # noqa: F401

        return "tiktoken:cl100k_base"
    except Exception:  # pragma: no cover
        return "heuristic:cjk"


def reset_tokenizer_cache() -> None:
    """清空词表缓存（**测试用**；口径在进程内只解析一次，生产环境不需要重解析）。"""
    global _QWEN_TOKENIZER, _QWEN_UNAVAILABLE_LOGGED, _QWEN_COUNTER_LABEL
    _QWEN_TOKENIZER = None
    _QWEN_UNAVAILABLE_LOGGED = False
    _QWEN_COUNTER_LABEL = ""


def count_tokens(text: str) -> int:
    """本地 token 估算（尺寸判定/指标/成本用，**不参与向量计算**）。

    为什么需要它：分块决策发生在**解析上传时**（那时还没有云端答案），质量指标又要对每个块
    都算 —— 不可能每切一个候选就调一次嵌入 API。所以本地必须有一个**便宜且可离线**的估算，
    代价是它只是**代理**，必须按 `02` TC-INDEX-004 与云端 `usage.total_tokens` 校准。

    取词顺序（从上到下，命中即用）：

    1. **Qwen 词表**：默认取官方 `dashscope` SDK 自带的 `qwen.tiktoken`（离线、零下载、随包）；
       也可用 `EKBS_TOKENIZER_PATH` 指向 HF 的 `tokenizer.json`。两种词表实测**计数逐条相同**，与线上嵌入模型同源家族，把偏差从 −35% 收敛到 −11%。
    2. `tiktoken` 的 `cl100k_base`（**回退**，会告警一次）——它是 OpenAI 嵌入模型的同源词表，
       对 Qwen 系模型系统性偏低（200 段真实文本实测 −35%，中文散文 +9%、数字表格 −42%）；
    3. 「中文按字、英文按词」的启发式（最后兜底，偏差最大，仅保证有数可用）。

    换嵌入模型时必须重做校准：`ekbs_shard.embedding_fingerprint` 就是那个锚点。
    """
    if not text:
        return 0
    qwen = _qwen_encoding()
    if qwen is not None:
        return qwen.count(text)
    try:  # pragma: no cover - 取决于是否装 tiktoken
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return cjk + max(1, other // 4)


def retrieval_text(heading_path: str | None, content: str, *, doc_title: str) -> str:
    """**检索用文本**：文档名 + 小节标题路径 + 正文（``ekbs_chunk.content`` 本身不改）。

    为什么三样都要：

    * **文档名**：「文件信息 / 1. 目的 / 2. 范围 / 3. 职责」这类模板小节在每篇制度里
      几乎一样，不带文档名时**两篇不同文件的同模板块向量几乎相同**（实测 3 篇文档
      共享同一句「重庆卓越城市运营服务有限责任公司熙街管理处」）；查询里带文档名/
      编号时也无法把同模板块区分开。
    * **小节路径**：关键限定词常只在小节标题里（「第二条 **住宿标准**」），正文写的是
      「一线城市每晚不超过 600 元…」，只嵌入正文时查询「住宿标准」与这段正文**没有
      任何共享词**，稠密召回直接落空 —— 实测踩到过（差旅费办法导入后查不到）。

    ⚠️ **口径必须唯一**：入库 / 分片迁移 / 索引重建 / 编辑重嵌 / 精排 五处都调用本函数。
    ``doc_title`` 是**必填关键字**：漏传会在调用点直接 ``TypeError``，而不是悄悄混入
    两套向量语义（这类"同一分片里两套嵌入口径"的排查成本极高）。
    """
    title = (doc_title or "").strip()
    heading = (heading_path or "").strip()
    body = content or ""
    if title and heading.startswith(title):
        # 小节路径本身已含文档名（如标题块），不重复拼
        title = ""
    prefix = "\n".join(part for part in (title, heading) if part)
    return f"{prefix}\n{body}" if prefix else body


def ensure_heading_path(heading_path: str | None, *, doc_title: str = "") -> str:
    """``heading_path`` 永不空（`20` §3.3）：缺则用文档标题，再缺用「文档」。

    ⚠️ 这里**只做兜底**，不把文档名拼进小节路径：``heading_path`` 是「文档结构」，
    文档身份由 :func:`retrieval_text` 在**检索用文本**里补（见那里的说明）。
    """
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
    #: 该块所属父块在结果列表中的下标（``child`` / 绑图 ``figure`` 时非空）
    parent_index: int | None = None
    #: 表格父容器**分段组**（``t<文档内表 id>``）：同表所有分段共用一个组 id，
    #: 检索命中任一叶子时按组一次展开回全表；未分段（≤2048）的表块为 ``None``。
    table_group: str | None = None
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
    #: 该节在原文中的字符起点：兜底补出的标题节要插回**原文顺序**（而不是一律追加到末尾，
    #: 否则 ``2．范围`` 会跑到 ``8.4`` 之后，用户实测「范围跑到第 8 条」）。
    start_offset: int = 0


#: 嵌套点分小节：末级形如 ``4.1 …`` / ``4.1.2 …``（至少两段数字），可并入所属大节。
_NESTED_DOTTED_LEAF_RE = re.compile(r"^(\d+(?:[.．]\d+)+)\s*(\S.*)?$")


def _heading_parts(heading_path: str) -> list[str]:
    return [p.strip() for p in (heading_path or "").split(" > ") if p.strip()]


def _is_nested_dotted_leaf(title: str) -> bool:
    return bool(_NESTED_DOTTED_LEAF_RE.match((title or "").strip()))


def major_heading_path(heading_path: str) -> str:
    """把嵌套点分小节收拢到所属大节路径。

    * ``4． 制度 > 4.1 接班…`` → ``4． 制度``
    * ``交接班 > 4． 制度 > 4.6 …`` → ``交接班 > 4． 制度``
    * ``1． 目的`` / ``## 第一条 年假`` → 原样（本身就是大节，不跨节合并）
    * 无大节壳的孤儿 ``4.1`` / ``4.2`` → 合成键 ``4．``（只用于识别所属章，不再并块）
    """
    parts = _heading_parts(heading_path)
    if not parts:
        return ""
    while len(parts) >= 2 and _is_nested_dotted_leaf(parts[-1]):
        parts.pop()
    if len(parts) == 1 and _is_nested_dotted_leaf(parts[0]):
        num = _NESTED_DOTTED_LEAF_RE.match(parts[0])
        assert num is not None
        head = re.split(r"[.．]", num.group(1))[0]
        return f"{head}．"
    return " > ".join(parts)


def pack_major_sections(sections: Sequence[Section]) -> list[Section]:
    """每条条款保持独立，不再把 ``4.1`` / ``4.2`` 拼进同一个父块。

    以前按大节把嵌套点分小节收成一段再按 token 装箱，一条检索块里会同时出现
    好几条。现在切分边界就是条款边界；单条过长仍由后面的 ``_pack`` 在条内切开。
    """
    return list(sections)


def merge_short_sections(sections: Sequence[Section], **_kwargs: object) -> list[Section]:
    """兼容旧名。条款不再按大节并块。"""
    return pack_major_sections(sections)


def _section_with_heading(section: Section) -> str:
    """短节并入时保留它的小节标题（否则「1. 目的」这层限定词会丢）。

    只取**最后一级**标题，不拼整条 ``heading_path``：实测空壳节（正文只有
    ``[表格 5]``）并入下一节时，把整条面包屑 ``4. 岗位职责 > 5.3 能耗分析管理 > 5.3.2 …``
    当正文写进 chunk —— 既是重复信息，又让读者误以为这一节属于 5.3.2。
    """
    text = (section.text or "").strip()
    heading = (section.heading_path or "").strip().rsplit(" > ", 1)[-1]
    if text:
        return f"{heading}\n{text}".strip() if heading else text
    return heading


def _join_trailing(last: Section, trailing: Sequence[Section]) -> Section:
    starts = [s.page_from for s in (last, *trailing) if s.page_from is not None]
    ends = [s.page_to for s in (last, *trailing) if s.page_to is not None]
    return Section(
        heading_path=last.heading_path,
        text="\n\n".join(
            [part for part in [last.text, *(_section_with_heading(s) for s in trailing)] if part]
        ).strip(),
        page_from=min(starts) if starts else None,
        page_to=max(ends) if ends else None,
    )


def _is_placeholder_only(section: Section) -> bool:
    """空壳节：正文为空，或只有一个 ``[表格 N]`` / ``[图片]`` 占位。"""
    text = (section.text or "").strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_ONLY_RE.match(text))


#: 章节容器键：末尾若是条款层（``第X条``/``第X款``/``第X项``），装进上层容器。
_CLAUSE_LEAF_RE = re.compile(r"^第[一二三四五六七八九十百千零〇两\d]+[条款项]")
#: 正文里的表格占位符：``[表格 3]`` → 表 id ``3``（用于把表块挂回它所在的章节）。
_TABLE_PLACEHOLDER_RE = re.compile(r"\[表格\s*(\d+)\]")
#: 正文里的图片占位符（``[图片 …]``）：抽取「临近表格文字」时当段落边界。
_FIGURE_PLACEHOLDER_RE = re.compile(r"^\[图片")


def container_heading_path(heading_path: str) -> str:
    """父容器的小节路径：``5. 工作程序和要求 > 5.1 管理内容 > 5.1.1 …`` → ``5. 工作程序和要求``。

    父块只是**上下文容器**（叶子规则下不入向量），检索单元仍是条款子块。把同章条款
    装进一个父块，父块扩展（``retrieval.parent_expand``）才有内容可用（`20` §3.1）。
    """
    parts = _heading_parts(major_heading_path(heading_path))
    while len(parts) >= 2 and _CLAUSE_LEAF_RE.match(parts[-1]):
        parts.pop()
    return " > ".join(parts)


def _placeholder_table_ids(text: str) -> tuple[str, ...]:
    """正文里的 ``[表格 N]`` → 表 id 列表（保序去重）。"""
    return tuple(dict.fromkeys(_TABLE_PLACEHOLDER_RE.findall(text or "")))


def _table_adjacent_prose(
    sections: Sequence[Section], table_id: str, *, max_chars: int = 240
) -> tuple[str, str]:
    """表格占位符前/后紧邻的正文字，作为表父块的「上文 / 下文」。

    「临近」= 同一小节内、占位符行上/下紧邻的正文行；只取最近一段，遇到空行或
    另一占位符（表格/图片）即停，各截断到 ``max_chars``。段落标题名与表名不在此
    返回（分别由 ``table_section_path`` 与表格序列化的 preamble 承担）。
    """
    marker = f"[表格 {table_id}]"
    for section in sections:
        text = section.text or ""
        if marker not in text:
            continue
        lines = text.splitlines()

        def _is_boundary(line: str) -> bool:
            return bool(
                _TABLE_PLACEHOLDER_RE.search(line) or _FIGURE_PLACEHOLDER_RE.match(line.strip())
            )

        idx = next((i for i, line in enumerate(lines) if marker in line), None)
        if idx is None:
            return "", ""
        # 默认参数绑定当前迭代的 lines，避免闭包捕获循环变量（B023）
        section_lines = lines

        def _collect(indices: range, bound_lines: list[str] = section_lines) -> str:
            parts: list[str] = []
            for i in indices:
                line = bound_lines[i].strip()
                if not line or _is_boundary(line):
                    break
                parts.append(line)
            return " ".join(parts).strip()[:max_chars]

        return _collect(range(idx - 1, -1, -1)), _collect(range(idx + 1, len(section_lines)))
    return "", ""


def _table_parent_content(
    *, section_path: str, above: str, full_table: str, below: str
) -> str:
    """表块/表父块的上下文：段落标题名 + 上方临近文字 + 表格 + 下方临近文字。

    表名已在表格序列化的 preamble 里（``表名: …``），不重复拼；段落标题名是序列化
    里没有的限定，必须显式带上。只取**最后一级**标题（``rsplit(" > ", 1)[-1]``），
    否则整条面包屑写进正文 —— 实测 doc 196 的 ``5.3.2 能耗录入及能耗报告`` 曾因此
    把 ``5. 工作程序和要求 > 5.3.2 …`` 整段写进块内容（面包屑泄漏）。
    """
    heading = (section_path or "").strip().rsplit(" > ", 1)[-1]
    parts = [p for p in (heading, above) if p]
    parts.append(full_table)
    if below:
        parts.append(below)
    return "\n\n".join(parts)


def _resolve_leaf_sample_max(
    table: TableInput,
    *,
    row_chunks: bool,
    leaf_policy: str,
    sample_max: int,
    sample_min_rows: int,
) -> int | None:
    """B 型大表明细：返回叶子抽样上限；其它表型 / 关抽样 → ``None``（全量）。"""
    if not row_chunks:
        return None
    policy = (leaf_policy or "auto").strip().lower()
    if policy == "full":
        return None
    from ekbs.ingest.table_serialize import coerce_rows_with_headers
    from ekbs.ingest.table_types import classify_table_type

    rows, _header_n = coerce_rows_with_headers(table.body)
    # 含表头
    data_n = max(0, len(rows) - 1)
    ttype = classify_table_type(caption=table.caption, body=table.body)
    if ttype != "B":
        return None
    if policy == "sample" or (policy == "auto" and data_n >= sample_min_rows):
        return max(1, int(sample_max))
    return None


def _table_leaf_blocks(
    table: TableInput,
    full_blocks: Sequence[str],
    *,
    row_chunks: bool,
    default_year: str | None = None,
    leaf_policy: str = "auto",
    leaf_sample_max: int = 64,
    leaf_sample_min_rows: int = 200,
) -> Sequence[str]:
    """表格叶子块：≤``TABLE_LEAF_TOKENS`` 用整表一块；超过就按行组拆（每叶重复表头）。

    整表一个向量时十几行数据会互相稀释（实测 920 token 的「表1-年度能耗管理计划编制
    及实施要求」查「A级能耗普查」检索不中）。``row_chunks=True`` 时退化为一行一块
    （``retrieval.table_row_chunks``）—— 但**只有电子表格**能走到这里，来源判定在
    :func:`chunk_markdown`（Word/PDF 的表行不是记录，逐行切会碎成 55~115 token 的块）。

    月份宽表（``row_chunks`` 且可识别）：即使整表 ≤384 也强制走 tidy 长叶子，否则
    「9 月谁最高」只能靠散文模型在宽行上猜，全局排序不稳。

    B 型收费明细大表：按 ``leaf_policy`` 抽样进 FAISS（全量行仍写观测表）。
    """
    # 延迟导入：table_serialize 反向依赖本模块的 count_tokens，模块级导入会成环
    from ekbs.ingest.table_serialize import serialize_table_body, table_body_is_month_series

    melt = bool(row_chunks) and table_body_is_month_series(
        caption=table.caption, body=table.body, default_year=default_year
    )
    if not melt and sum(count_tokens(block) for block in full_blocks) <= TABLE_LEAF_TOKENS:
        return full_blocks
    sample_max = _resolve_leaf_sample_max(
        table,
        row_chunks=row_chunks,
        leaf_policy=leaf_policy,
        sample_max=leaf_sample_max,
        sample_min_rows=leaf_sample_min_rows,
    )
    return serialize_table_body(
        caption=table.caption,
        body=table.body,
        table_id=table.table_id,
        row_chunks=True if melt else row_chunks,
        max_tokens=TABLE_LEAF_TOKENS,
        default_year=default_year,
        leaf_sample_max=sample_max,
    )


@dataclass
class _TableContainerSegment:
    """超限表格的一个分段：容器正文（标记 + 本段行块 + 段首/段尾上下文）+ 本段自己的叶子。"""

    content: str
    leaves: list[str]


def _table_segment_marker(index: int, total: int) -> str:
    """分段容器里的单行标记：让模型/审计看得出这是整表的第几段。"""
    return f"本段为整表第 {index}/{total} 段"


def _segment_table_container(
    *,
    table: TableInput,
    section_path: str,
    above: str,
    below: str,
    row_chunks: bool,
) -> list[_TableContainerSegment] | None:
    """把超 ``TABLE_CONTAINER_MAX_TOKENS`` 的表格父容器按行分段（每段 ≤ 硬上限）。

    返回 ``None`` = 无法安全分段（单行本身就超过预算），调用方回退为原来的单容器
    （并打 ``table_container_unsplittable`` 警告）—— 宁可保留旧行为，也不能丢行。

    为什么**按段派生叶子**而不是先全局 384 行组再按首行映射：全局行组会跨段，
    映射后可能出现「某段没有任何叶子」；而 ``retrievable`` 规则是「没有子块的块
    才是检索单元」，没有叶子的分段会自己变成一个可检索的上下文容器（污染向量/FTS）。
    按段调用 span 序列化（``row_range`` 限定该段行号）能保证每段 ≥1 个叶子。
    """
    from ekbs.ingest.table_serialize import serialize_table_body_spans

    marker = _table_segment_marker(1, 2)
    reserve = count_tokens(
        _table_parent_content(
            section_path=section_path, above=above, full_table=marker, below=below
        )
    ) + _TABLE_SEGMENT_MARGIN_TOKENS
    budget = max(512, TABLE_CONTAINER_MAX_TOKENS - reserve)
    # 预算是「表格正文」的，实际分段正文还要加标记行；一段仍超限就缩预算重试，
    # 4 次后仍失败说明连最小预算都排不下 → 交给调用方回退。
    for _attempt in range(4):
        spans = serialize_table_body_spans(
            caption=table.caption,
            body=table.body,
            table_id=table.table_id,
            max_tokens=budget,
        )
        # 首行即超预算时序列化器会先吐一个只有 preamble 的退化块（空区间）：它不含数据行，
        # 分段时跳过；preamble 在每段正文里都会重复，不丢内容。
        row_spans = [span for span in spans if span[2] >= span[1]]
        if len(row_spans) <= 1:
            return None
        total = len(row_spans)
        segments: list[_TableContainerSegment] = []
        for index, (block, first_row, last_row) in enumerate(row_spans):
            content = _table_parent_content(
                section_path=section_path,
                above=above if index == 0 else "",
                full_table=f"{_table_segment_marker(index + 1, total)}\n{block}",
                below=below if index == total - 1 else "",
            )
            if count_tokens(content) > TABLE_CONTAINER_MAX_TOKENS:
                break
            leaves = [
                text
                for text, _first, _last in serialize_table_body_spans(
                    caption=table.caption,
                    body=table.body,
                    table_id=table.table_id,
                    row_chunks=row_chunks,
                    max_tokens=TABLE_LEAF_TOKENS,
                    row_range=(first_row, last_row),
                )
            ]
            if not leaves:
                break
            segments.append(_TableContainerSegment(content=content, leaves=leaves))
        else:
            return segments
        budget = max(512, budget * 3 // 4)
    return None


@dataclass
class _ChildDraft:
    """待落库的子块草稿（父块装箱时用）。"""

    content: str  # 带 12%~15% 邻块重叠，进向量/FTS
    body: str  # 不带重叠，父块内容用它拼（避免父块里重复一遍重叠段）
    heading_path: str
    page_from: int | None
    page_to: int | None
    table_ids: tuple[str, ...] = ()


def _squash(text: str) -> str:
    """去掉全部空白，用于判断「标题行是否已经在正文里」。"""
    return re.sub(r"\s+", "", text or "")


def _child_body(buf: Sequence[Section]) -> str:
    """把若干小节拼成一个子块正文；跨节时补回**标题骨架**。

    两个理由（都实测过）：

    * 第X条类文档的小节正文不含条号（条号只在 heading_path 里），聚合后若不留标题行，
      块内容里就找不到「第二条」——答案引用条号时 ``clause_mismatches`` 会判成幻觉；
    * 聚合会跨章，若只留第一小节的 heading_path，中间的父级标题（``4． 方法和过程控制``）
      会**彻底消失**（覆盖率掉到 91%）——补回「相对上一小节新增的标题层级」即可。
    """
    parts: list[str] = []
    prev_parts: list[str] = []
    for index, section in enumerate(buf):
        path = _heading_parts(section.heading_path)
        body = (section.text or "").strip()
        if len(buf) > 1:
            if index == 0:
                new_headings = path[-1:]
            else:
                common = 0
                while (
                    common < min(len(path), len(prev_parts))
                    and path[common] == prev_parts[common]
                ):
                    common += 1
                new_headings = path[common:]
            for heading in new_headings:
                if not heading or _squash(heading) in _squash(body):
                    continue
                if any(_squash(heading) == _squash(part) for part in parts[-2:]):
                    continue
                parts.append(heading)
        if body:
            parts.append(body)
        prev_parts = path
    return "\n\n".join(parts)


_SLIDE_PAGE_HEADING_RE = re.compile(r"^第\s*(\d+)\s*页\s*$")


def slide_page_map_from_markdown(markdown: str) -> list[tuple[int, int, int]]:
    """从 ``## 第 N 页`` 重建 page_map（清洗改字后旧偏移会错位，幻灯片必须重扫）。"""
    out: list[tuple[int, int, int]] = []
    for match in re.finditer(r"^#{1,6}\s*第\s*(\d+)\s*页\s*$", markdown or "", re.MULTILINE):
        page = int(match.group(1))
        out.append((match.start(), page, page))
    return out


def _slide_page_from_heading(heading_path: str) -> int | None:
    """从 heading_path 任一段提取「第 N 页」（含 ``第 14 页 > 2. 设备运维``）。"""
    for part in _heading_parts(heading_path):
        marker = _SLIDE_PAGE_HEADING_RE.match(part.strip())
        if marker:
            return int(marker.group(1))
    return None


def _force_slide_section_pages(sections: Sequence[Section]) -> list[Section]:
    """幻灯片小节页码以「第 N 页」标题为准；其下条款继承该页，杜绝 page_map 渗色。"""
    current: int | None = None
    out: list[Section] = []
    for section in sections:
        marked = _slide_page_from_heading(section.heading_path)
        if marked is not None:
            current = marked
        page = current if current is not None else section.page_from
        out.append(
            Section(
                heading_path=section.heading_path,
                text=section.text,
                page_from=page,
                page_to=page,
                start_offset=section.start_offset,
            )
        )
    return out


def _build_child_drafts(
    sections: Sequence[Section],
    *,
    child_min: int,
    child_max: int,
    page_hard_cut: bool = False,
) -> list[_ChildDraft]:
    """按 token 目标把小节**跨标题**聚合成子块（`20` §3.2 规则 3/4）。

    为什么不再"一个标题一个子块"：制度/作业指导书每个标题下往往只有一两行，按标题切
    出来的块只有 10~60 token，父子块全部退化成碎片（实测 doc 196 的 33 节 → 33 个
    6~120 token 的块，父子关系名存实亡）。聚合到 ``child_min`` 才收口；收口点落在
    标题边界上（小节是切分的最小单位，不会把一行拆两半）。

    ``page_hard_cut``：幻灯片一页一屏，**禁止跨页聚合**（否则挂图/问句都会漂到邻页）。
    """
    drafts: list[_ChildDraft] = []
    buf: list[Section] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        body = _child_body(buf)
        if body:
            starts = [s.page_from for s in buf if s.page_from is not None]
            ends = [s.page_to for s in buf if s.page_to is not None]
            page_from = min(starts) if starts else None
            page_to = max(ends) if ends else None
            # 幻灯片一块只标一页：page_map 边界渗色不影响挂图/引用页码
            if page_hard_cut and page_from is not None:
                page_to = page_from
            drafts.append(
                _ChildDraft(
                    content=body,
                    body=body,
                    heading_path=buf[0].heading_path,
                    page_from=page_from,
                    page_to=page_to,
                    table_ids=tuple(
                        table_id
                        for section in buf
                        for table_id in _placeholder_table_ids(section.text)
                    ),
                )
            )
        buf = []
        buf_tokens = 0

    for section in sections:
        text = (section.text or "").strip()
        if not text:
            continue
        section_tokens = count_tokens(text)
        if section_tokens > child_max:
            flush()
            for piece in _pack(text, child_min, child_max):
                drafts.append(
                    _ChildDraft(
                        content=piece,
                        body=piece,
                        heading_path=section.heading_path,
                        page_from=section.page_from,
                        page_to=section.page_to,
                        table_ids=_placeholder_table_ids(section.text),
                    )
                )
            continue
        if page_hard_cut and buf and section.page_from is not None:
            # 以缓冲**起始页**为准。page_map 边界常让上一节 page_to=下一页，
            # 若用「页集合相交」判断会把第 N/N+1 页粘在一起（实测 36 页→10 块）。
            anchor = buf[0].page_from
            if anchor is not None and int(section.page_from) != int(anchor):
                flush()
        if buf and buf_tokens + section_tokens > child_max:
            flush()
        buf.append(section)
        buf_tokens += section_tokens
        # 幻灯片：同页继续攒、跨页才收口（上面已 flush）；短页也不等 child_min。
        # 非幻灯片仍按 child_min 聚合。
        if not page_hard_cut and buf_tokens >= child_min:
            flush()
    flush()
    return drafts


def _merge_small_tail_child(
    drafts: list[_ChildDraft],
    *,
    child_min: int,
    child_max: int,
    page_hard_cut: bool = False,
) -> None:
    """收尾的碎块（< child_min/2）并入前一块 —— 尾部余量不该单独成块。

    尾部是否成块取决于总长能否整除目标，无法从算法上避免；但 10~60 token 的孤立尾块
    会污染 rerank（同一篇里块大小差一个量级），所以超小尾块并回前一块（不超 child_max 时）。
    幻灯片禁止跨页并尾。
    """
    if len(drafts) < 2:
        return
    last, prev = drafts[-1], drafts[-2]
    if page_hard_cut and (
        last.page_from != prev.page_from or last.page_to != prev.page_to
    ):
        return
    last_tokens = count_tokens(last.content)
    if last_tokens >= child_min // 2:
        return
    if count_tokens(prev.content) + last_tokens > child_max:
        return
    merged = _ChildDraft(
        content=f"{prev.content}\n\n{last.body}",
        body=f"{prev.body}\n\n{last.body}",
        heading_path=prev.heading_path,
        page_from=min(p for p in (prev.page_from, last.page_from) if p is not None)
        if (prev.page_from is not None or last.page_from is not None)
        else None,
        page_to=max(p for p in (prev.page_to, last.page_to) if p is not None)
        if (prev.page_to is not None or last.page_to is not None)
        else None,
        table_ids=tuple(dict.fromkeys((*prev.table_ids, *last.table_ids))),
    )
    drafts[-2] = merged
    drafts.pop()


def _apply_draft_overlap(drafts: list[_ChildDraft], *, overlap_tokens: int) -> None:
    """相邻子块注入 12%~15% 尾部重叠（`20` §3.1 / TC-INDEX-003）。"""
    if overlap_tokens <= 0:
        return
    for index in range(1, len(drafts)):
        tail = _tail_by_tokens(drafts[index - 1].content, overlap_tokens)
        if not tail:
            continue
        current = drafts[index].content
        if current.startswith(tail) or tail in current[: min(len(current), len(tail) + 32)]:
            continue
        drafts[index].content = f"{tail}\n{current}"


def _parent_heading(drafts: Sequence[_ChildDraft], *, root: str) -> str:
    """父块标题：组内子块**同章**时用章名；跨章时退回文档根（父块只是上下文容器）。"""
    keys = {container_heading_path(draft.heading_path) for draft in drafts}
    keys.discard("")
    if len(keys) == 1:
        return keys.pop()
    return root


def _pack_parent_drafts(
    drafts: Sequence[_ChildDraft],
    *,
    parent_min: int,
    parent_max: int,
    page_hard_cut: bool = False,
) -> list[list[_ChildDraft]]:
    """把子块按 ``parent_min``~``parent_max`` 装箱成父块（可跨章跨节，`20` §3.1）。

    父子关系要有意义，父块就必须比子块大一个量级：按标题/章分组时，2 页文档的「章」
    只有几十到几百 token，父块约等于子块 → 父块扩展等于没扩展（用户实测反馈）。

    幻灯片：每页一个父块，图才能挂到同页 parent（跨页装箱会让 38 张图共挂一个父）。
    """
    if page_hard_cut:
        return [[draft] for draft in drafts]

    groups: list[list[_ChildDraft]] = []
    current: list[_ChildDraft] = []
    tokens = 0
    for draft in drafts:
        # 按 **body** 计：父块内容就是 body（不含子块的重叠前缀），按 content 算会
        # 系统性低估 12% 左右 → 父块实际只有 900 token 上下，够不到 parent_min。
        draft_tokens = count_tokens(draft.body)
        if current and tokens + draft_tokens > parent_max:
            groups.append(current)
            current, tokens = [], 0
        current.append(draft)
        tokens += draft_tokens
        if tokens >= parent_min:
            groups.append(current)
            current, tokens = [], 0
    if current:
        groups.append(current)
    # 尾部小组（不够 parent_min）能装进前一组就并进去，避免出现 10 token 的父块
    if len(groups) >= 2:
        tail_tokens = sum(count_tokens(draft.body) for draft in groups[-1])
        prev_tokens = sum(count_tokens(draft.body) for draft in groups[-2])
        if tail_tokens < parent_min and prev_tokens + tail_tokens <= parent_max:
            groups[-2] = [*groups[-2], *groups[-1]]
            groups.pop()
    return groups


def merge_short_sections_into_next(
    sections: Sequence[Section], *, page_hard_cut: bool = False
) -> list[Section]:
    """把**空壳节**（无正文 / 只有占位符）并入下一节（行尾连续的并入上一节）。

    为什么只并空壳、不做"短节按长度合并"：条款（``第X条`` / ``4.1``）和编号小节各自
    独立成块是既有契约（`test_pack_major_dotted_sections` /
    `test_chapter_and_fullwidth_dotted_headings`），按长度合并会把不同规定混进同一个块，
    检索精度反而变差。这里只清掉**没有正文可检索**的碎片块（实测 ``[表格 1]``、
    ``6．质量记录`` 这类块会跨文档重复出现，命中后什么信息也没有）。

    幻灯片禁止跨页并空壳（``[表格 N]`` 页不能并进下一页正文）。
    """
    merged: list[Section] = []
    buffered: list[Section] = []

    def _emit_placeholder(item: Section) -> None:
        text = _section_with_heading(item)
        if not text:
            return
        merged.append(
            Section(
                heading_path=item.heading_path,
                text=text,
                page_from=item.page_from,
                page_to=item.page_to,
                start_offset=item.start_offset,
            )
        )

    for section in sections:
        if _is_placeholder_only(section):
            buffered.append(section)
            continue
        if page_hard_cut and buffered:
            same: list[Section] = []
            for item in buffered:
                if (
                    item.page_from is not None
                    and section.page_from is not None
                    and int(item.page_from) != int(section.page_from)
                ):
                    if merged and merged[-1].page_from == item.page_from:
                        merged[-1] = _join_trailing(merged[-1], [item])
                    else:
                        _emit_placeholder(item)
                else:
                    same.append(item)
            buffered = same
        starts = [s.page_from for s in (*buffered, section) if s.page_from is not None]
        ends = [s.page_to for s in (*buffered, section) if s.page_to is not None]
        pieces = [_section_with_heading(item) for item in buffered]
        pieces.append((section.text or "").strip())
        merged.append(
            Section(
                heading_path=section.heading_path,
                text="\n\n".join(part for part in pieces if part).strip(),
                page_from=min(starts) if starts else None,
                page_to=max(ends) if ends else None,
            )
        )
        buffered = []
    if buffered:
        if not merged:
            return list(sections)
        if page_hard_cut:
            for item in buffered:
                if merged[-1].page_from == item.page_from:
                    merged[-1] = _join_trailing(merged[-1], [item])
                else:
                    _emit_placeholder(item)
        else:
            merged[-1] = _join_trailing(merged[-1], buffered)
    return merged


@dataclass
class FigureInput:
    """文档里抽出的图片（**已落盘**），供分块器生成**附件型** figure 块。

    图片不入向量/FTS：只挂到对应标题/章节 parent 下，文字命中后再回源。
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
    #: 表体是**原生 Word 版式**（HTML 带 colspan/rowspan）。表修复/续页合并会按行重拼
    #: HTML、丢掉跨度 —— 这类表必须跳过重拼，否则合并单元格被拍平。
    preserve_layout: bool = False


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
        """图片块：资产行（``image_path`` / ``figure_id``），经 ``parent_id`` 挂在
        章节文字下；**不入向量/FTS**，由 `/assets/figure/{chunk_id}` 回源。"""
        return [n for n in self.nodes if n.chunk_type == "figure"]

    @property
    def tables(self) -> list[ChunkNode]:
        return [n for n in self.nodes if n.chunk_type == "table"]


def _dotted_level(num: str) -> int:
    """``1.`` → 2；``4.1`` → 3；``4.1.2`` → 4（与章/节层级对齐，便于同级弹出）。"""
    parts = [p for p in re.split(r"[.．]", num.rstrip(".．")) if p]
    return min(1 + max(len(parts), 1), 6)


def _parse_heading_line(stripped: str) -> tuple[int, str] | None:
    """识别一行是不是标题（Markdown ``#`` / 中文条款 / 点分编号），返回 ``(level, title)``。

    「编号 + 正文同行」的行（如 ``5.4 主管人员作业前需…``）整行本身就是正文：
    此时标题**只保留编号**（``5.4``），整行原文由 ``push_heading`` 收进本节正文。
    否则 ``heading_path`` 会变成一整段话（实测 GC-13 的 ``5.4`` 让检索标题不可读）。
    """
    match = _HEADING_RE.match(stripped)
    if match:
        return len(match.group(1)), match.group(2).strip()
    body_like = _looks_like_content(stripped)
    clause = _CLAUSE_HEADING_RE.match(stripped)
    if clause:
        kind = clause.group(2)
        title = f"第{clause.group(1)}{kind}"
        rest = clause.group(3).strip()
        if rest and not body_like:
            title = f"{title} {rest}"
        return _CLAUSE_LEVEL.get(kind, 3), title
    dotted = _DOTTED_HEADING_RE.match(stripped)
    if dotted:
        num = dotted.group(1).strip()
        if body_like:
            return _dotted_level(num), num
        return _dotted_level(num), f"{num} {dotted.group(2).strip()}"
    return None


def _emit_heading_fallbacks(sections: list[Section], heading_log: list, page_map) -> None:
    """收尾兜底：有些标题（文档末尾的、被同级标题顶掉的）**没有正文**，文字只存在于
    heading_stack 里 —— 一旦被 pop 就永久消失。这里只补真正没落库的那些：
    若该标题已经出现在某个块的 heading_path 中（H1 通常如此），就不重复产出，
    免得制造一堆 5 token 的碎片块。
    """
    emitted_paths = [section.heading_path for section in sections]
    emitted_text = _strip_md_marks("".join(section.text for section in sections))
    log_paths = [path for _title, path, _source, _offset in heading_log]
    for title, path, source, head_off in heading_log:
        if not source.strip() or title in " ".join(emitted_paths):
            continue
        # 这行标题下面还有子标题（如 ``2．范围 > 2.1 …``）：它的文字已经作为子节
        # heading_path 的前缀保留，不能再用原文 ``## 2．范围`` 兜底 —— 旧实现会把
        # 空壳父标题追加到文档末尾，用户会看到「范围」跑到最后、正文里还带 ``##``。
        if any(other.startswith(f"{path} > ") for other in log_paths if other != path):
            continue
        if _strip_md_marks(source) in emitted_text:
            continue
        page_from, page_to = _pages_for(head_off, head_off + len(source), page_map)
        sections.append(
            Section(
                heading_path=path or title,
                text=_strip_md_marks(source),
                page_from=page_from,
                page_to=page_to,
                start_offset=head_off,
            )
        )
    # 兜底节按**原文字符偏移**插回文档顺序（正常节本身已是有序的，sort 保持稳定）。
    sections.sort(key=lambda section: section.start_offset)


def split_sections(markdown: str, *, page_map: Sequence[tuple[int, int, int]] | None = None) -> list[Section]:
    """按标题层级把 Markdown 拆成小节，并维护 ``heading_path``。

    标题来源（按优先级命中即切）：
    1. Markdown ``#`` … ``######``
    2. 中文条款行 ``第X章/节/条/款/项``（制度 PDF 最常见，MinerU 常不标 text_level）
    3. 点分/一级编号 ``3.2 标题`` / ``1. 目的`` / ``2．范围``（半角/全角点）

    ``page_map``：``[(char_offset, page_from, page_to), …]``；缺省则页码为 None。
    """
    sections: list[Section] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    current_heading = ""
    current_start = 0
    offset = 0
    #: 每个标题的 (标题文字, 该标题当时的完整路径, 该行原文, 行偏移) —— 收尾时用它兜底
    #: 找回"从未出现在任何块 heading_path 里"的标题（否则被 pop 掉就永久消失：
    #: 实测 `6.1 《设施设备台账/统计表》`、`### 设备分组设置` 都是这样丢的）。
    heading_log: list[tuple[str, str, str, int]] = []
    heading_emitted = False

    def flush(end_offset: int) -> None:
        nonlocal heading_emitted
        text = "\n".join(buffer).strip()
        if text:
            page_from, page_to = _pages_for(current_start, end_offset, page_map)
            sections.append(
                Section(
                    heading_path=current_heading,
                    text=text,
                    page_from=page_from,
                    page_to=page_to,
                    start_offset=current_start,
                )
            )
            heading_emitted = True

    def push_heading(
        level: int, title: str, line_len: int, *, source_line: str | None = None
    ) -> None:
        """开一个新小节。

        ``source_line``：这一行的**原文**。中文制度/合同里 ``第X条 …``、``4.1 …``
        常常整条就是正文 —— 此时整行文字**同时**进新小节正文（否则同级旧标题被弹出后，
        整行文字谁都不产出，静默丢失：实测交接班制度 22 段丢 11 段）。
        即便这一行只是纯标题，它的原文也会被记下来，供"无正文时兜底落库"。
        """
        nonlocal buffer, current_heading, current_start, heading_emitted
        flush(offset)
        buffer = []
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        current_heading = " > ".join(t for _, t in heading_stack)
        if source_line is not None:
            heading_log.append((title, current_heading, source_line, offset))
        heading_emitted = False
        if source_line and (
            _looks_like_content(source_line.strip()) or _is_unmarked_sentence(source_line)
        ):
            buffer.append(source_line)
            current_start = offset  # 本行就是正文首行 → 小节起点就是它
        else:
            current_start = offset + line_len + 1

    for line in markdown.splitlines():
        stripped = line.strip()
        parsed = _parse_heading_line(stripped)
        if parsed is None:
            buffer.append(line)
        else:
            level, title = parsed
            push_heading(level, title, len(line), source_line=line)
        offset += len(line) + 1
    flush(offset)
    _emit_heading_fallbacks(sections, heading_log, page_map)
    return sections


def _pages_for(
    start: int, end: int, page_map: Sequence[tuple[int, int, int]] | None
) -> tuple[int | None, int | None]:
    """把字符区间映射到页码。

    ``page_map`` 存的是「每一页正文的起始偏移」``(offset, page, page)``（见
    :func:`ekbs.ingest.parser._blocks_to_document`）。正确语义是：

        位置 pos 所属页 = 最后一个满足 ``offset <= pos`` 的页码

    旧实现写成 ``start <= offset <= end``（页起点必须落在小节区间内），
    小节若从页界之后几个字符开始（标题行切分后的常态），就一个页标都框不住，
    再 fallback 到 ``page_map[0]`` → **整段被标成第 1 页**。
    开班典礼实测：大量后页内容的 ``page_from`` 都变成 1，并出现虚假的 1–2 跨页。
    """
    if not page_map:
        return None, None
    if end < start:
        start, end = end, start

    def page_at(pos: int) -> int:
        page = int(page_map[0][1])
        for offset, p, _ in page_map:
            if int(offset) <= pos:
                page = int(p)
            else:
                break
        return page

    # end 是 flush 时的排他终点（下一行起始）；取 end-1 落在正文最后一个字符上
    last = end - 1 if end > start else start
    return page_at(start), page_at(last)


def _table_heading_and_section(
    table: TableInput, *, doc_title: str, table_section_path: dict[str, str]
) -> tuple[str, str]:
    """表块 heading 与所在小节路径。

    表名单独成块时没有文档上下文（「表 1」这种名字跨文档完全一样），把文档名并进
    heading：UI 里一眼看得出是哪篇的表，精排文本也自带身份。
    """
    table_name = (table.caption or "").strip() or (
        f"表 {table.table_id}" if table.table_id else ""
    )
    section_path = (table_section_path.get(str(table.table_id)) or "").strip()
    if section_path:
        base = f"{doc_title} > {section_path}" if doc_title else section_path
        heading = ensure_heading_path(
            f"{base} · {table_name}" if table_name else base, doc_title=doc_title
        )
    else:
        heading = ensure_heading_path(
            f"{doc_title} · {table_name}" if doc_title and table_name else table_name,
            doc_title=doc_title,
        )
    return heading, section_path


def _relevant_header_line(text: str) -> str:
    """序列化块里的 ``相关表头:`` 行；宽表按指标组切分后用它把父块与叶子对齐。"""
    for line in (text or "").splitlines():
        if line.startswith("相关表头:"):
            return line.strip()
    return ""


_TABLE_META_LINE_PREFIXES = ("表名:", "核心实体:", "相关表头:", "本段为整表")


def _is_serialized_table_data_line(line: str) -> bool:
    """序列化表块里的数据行（``列=值; …``），用于父块超限时按行再切。"""
    text = (line or "").strip()
    if not text or text.startswith(_TABLE_META_LINE_PREFIXES):
        return False
    return "=" in text


def _split_serialized_block_to_parent_budget(
    block: str,
    *,
    section_path: str,
    above: str,
    below: str,
    marker_pad: str,
) -> list[str]:
    """单块（常是一个月）加标题/标记后仍超 2048 时，按数据行再切，每片自带 preamble。"""

    def _fits(body: str, *, with_above: bool, with_below: bool) -> bool:
        wrapped = _table_parent_content(
            section_path=section_path,
            above=above if with_above else "",
            full_table=f"{marker_pad}\n{body}",
            below=below if with_below else "",
        )
        return count_tokens(wrapped) <= TABLE_CONTAINER_MAX_TOKENS

    if _fits(block, with_above=True, with_below=True):
        return [block]

    lines = str(block).splitlines()
    preamble: list[str] = []
    data_lines: list[str] = []
    for line in lines:
        if _is_serialized_table_data_line(line):
            data_lines.append(line)
        elif data_lines:
            # 数据区之后偶发空行/脚注，并进当前数据流，避免拆丢
            data_lines.append(line)
        else:
            preamble.append(line)
    if not data_lines:
        return [block]

    def _compose(rows: Sequence[str]) -> str:
        parts = [p for p in ("\n".join(preamble), "\n".join(rows)) if p]
        return "\n".join(parts)

    pieces: list[str] = []
    cur_rows: list[str] = []
    for line in data_lines:
        trial = cur_rows + [line]
        body = _compose(trial)
        # 与打包同一口径：按首/末段上下文预留，避免落库后再涨
        if cur_rows and not _fits(body, with_above=True, with_below=True):
            pieces.append(_compose(cur_rows))
            cur_rows = [line]
            # 单行本身加壳仍超限：不得不收下（与行向 unsplittable 同口径，不丢行）
            if not _fits(_compose(cur_rows), with_above=True, with_below=True):
                pieces.append(_compose(cur_rows))
                cur_rows = []
        else:
            cur_rows = trial
    if cur_rows:
        pieces.append(_compose(cur_rows))
    return pieces or [block]


def _leaves_matching_blocks(
    blocks: Sequence[str], candidates: Sequence[str]
) -> tuple[list[str], list[str]]:
    """按数据行归属把叶子分给本段；未匹配的留给后续段（避免同叶挂多个父块）。"""
    batch_text = "\n\n".join(blocks)
    taken: list[str] = []
    left: list[str] = []
    for leaf in candidates:
        data_lines = [
            ln.strip()
            for ln in str(leaf).splitlines()
            if _is_serialized_table_data_line(ln)
        ]
        if data_lines and any(ln in batch_text for ln in data_lines):
            taken.append(str(leaf))
        else:
            left.append(str(leaf))
    return taken, left


def _segments_by_relevant_header(
    *,
    full_blocks: Sequence[str],
    leaf_blocks: Sequence[str],
    section_path: str,
    above: str,
    below: str,
) -> list[_TableContainerSegment]:
    """宽表/多块回退：按「相关表头」保序，父块统一打满 ``TABLE_CONTAINER_MAX_TOKENS``。

    旧行为「一组表头（常是一个月）就开一个父容器」会让熙丰月段只 ~600、水费同月
    顶到 ~2000，父块观感两套尺。现规则与行向分段一致：**父块唯一预算 2048**——
    多月/多组能并就并，超了再开下一段；单月加壳仍超限则按行再切。叶子按数据行
    挂到「含该行」的那段（同月被切开时不会整组重复挂两个父块）。

    行向 ``_segment_table_container`` 在指标组切分后会让同一行号跨月重复出现，
    ``row_range`` 叶子会串组，所以超限宽表仍走本函数（表头对齐），只改打包粒度。
    """
    from collections import OrderedDict

    groups: OrderedDict[str, list[str]] = OrderedDict()
    for block in full_blocks:
        key = _relevant_header_line(block) or f"#{len(groups)}"
        groups.setdefault(key, []).append(str(block))

    # 最坏标记长度（位数多一点），避免「试算不过、落库越界」
    marker_pad = _table_segment_marker(99, 99)

    def _fits(blocks: list[str], *, with_above: bool, with_below: bool) -> bool:
        wrapped = _table_parent_content(
            section_path=section_path,
            above=above if with_above else "",
            full_table=f"{marker_pad}\n" + "\n\n".join(blocks),
            below=below if with_below else "",
        )
        return count_tokens(wrapped) <= TABLE_CONTAINER_MAX_TOKENS

    # 保序展开；单块加壳超限先按行切开，再贪心打满 2048
    ordered: list[str] = []
    for _key, blocks in groups.items():
        for block in blocks:
            ordered.extend(
                _split_serialized_block_to_parent_budget(
                    block,
                    section_path=section_path,
                    above=above,
                    below=below,
                    marker_pad=marker_pad,
                )
            )
    if not ordered:
        return []

    raw_batches: list[list[str]] = []
    cur_blocks: list[str] = []
    for block in ordered:
        trial_blocks = cur_blocks + [block]
        # 试算按「可能是首段/末段」留上下文余量，偏保守；空批次也要过预算
        if cur_blocks and not _fits(trial_blocks, with_above=True, with_below=True):
            raw_batches.append(cur_blocks)
            cur_blocks = [block]
        elif not cur_blocks and not _fits(trial_blocks, with_above=True, with_below=True):
            # 切开后仍单片超限（极端宽行）：照收，避免丢内容
            raw_batches.append(trial_blocks)
            cur_blocks = []
        else:
            cur_blocks = trial_blocks
    if cur_blocks:
        raw_batches.append(cur_blocks)

    total = len(raw_batches)
    remaining_leaves = [str(leaf) for leaf in leaf_blocks]
    segments: list[_TableContainerSegment] = []
    for index, batch in enumerate(raw_batches):
        marker = _table_segment_marker(index + 1, total)
        content = _table_parent_content(
            section_path=section_path,
            above=above if index == 0 else "",
            full_table=f"{marker}\n" + "\n\n".join(batch),
            below=below if index == total - 1 else "",
        )
        part_leaves, remaining_leaves = _leaves_matching_blocks(batch, remaining_leaves)
        if not part_leaves:
            part_leaves = list(batch)
        segments.append(_TableContainerSegment(content=content, leaves=part_leaves))
    # 未匹配叶子挂到最后一段，避免检索侧丢叶
    if remaining_leaves and segments:
        segments[-1].leaves.extend(remaining_leaves)
    return segments


def _append_table_segments(
    result: ChunkingResult,
    seq: int,
    *,
    table: TableInput,
    segments: Sequence[_TableContainerSegment],
    table_heading: str,
    prose_parent: int | None,
    page: int | None,
) -> int:
    """超限表：逐段落节点（每段一个 table 容器 + 该段自己的叶子）。"""
    # 表 id 是文档内计数器，正常非空；兜底用首段 seq 保证同文档不同表不撞组。
    group_key = str(table.table_id).strip() or f"seq{seq + 1}"
    table_group = f"t{group_key}"
    for segment in segments:
        seq += 1
        segment_index = len(result.nodes)
        result.nodes.append(
            ChunkNode(
                chunk_type="table",
                seq=seq,
                content=segment.content,
                heading_path=table_heading,
                page_from=page,
                page_to=page,
                figure_id=None,
                parent_index=prose_parent,
                table_group=table_group,
            )
        )
        for block in segment.leaves:
            seq += 1
            result.nodes.append(
                ChunkNode(
                    chunk_type="table",
                    seq=seq,
                    content=block,
                    heading_path=table_heading,
                    page_from=page,
                    page_to=page,
                    figure_id=None,
                    parent_index=segment_index,
                )
            )
    return seq


def _append_table_nodes(
    result: ChunkingResult,
    seq: int,
    table: TableInput,
    *,
    doc_title: str,
    table_section_path: dict[str, str],
    raw_sections: Sequence[Section],
    table_container: dict[str, int],
    row_chunks: bool,
    default_year: str | None = None,
    leaf_policy: str = "auto",
    leaf_sample_max: int = 64,
    leaf_sample_min_rows: int = 200,
) -> int:
    """把一张表落成节点，返回新的 ``seq``。

    * 小表（整表 ≤2048 且拆不出第二块）：上下文折进唯一叶子，不另设父容器；
    * 大表：父容器（上文 + 整表 + 下文）+ 行组叶子；
    * 父容器 > :data:`TABLE_CONTAINER_MAX_TOKENS`（统一 **2048**）：按行或按表头保序
      **打满 2048 再切段**（不再「一个月一个小父块」），每段自带叶子；同表共用
      ``table_group`` 标识同表分段（检索只扩直系父段，不按组拼整表）。
    """
    from ekbs.ingest.table_serialize import serialize_table_body

    # 整表先序列化一次（不拆）：拿「表格本身」全文给父块用。
    full_blocks = serialize_table_body(
        caption=table.caption, body=table.body, table_id=table.table_id
    )
    if not full_blocks:
        return seq
    table_heading, section_path = _table_heading_and_section(
        table, doc_title=doc_title, table_section_path=table_section_path
    )
    above, below = _table_adjacent_prose(raw_sections, str(table.table_id))
    prose_parent = table_container.get(str(table.table_id))
    # 叶子预算：整表超过 384 token 就拆「行组叶子」。
    # 920 token 的表（十几行数据）塞进同一个向量会被互相稀释 —— 查某一行
    # 的语义（如「A级能耗普查」）时整表向量对不上，检索直接落空。
    leaf_blocks = _table_leaf_blocks(
        table,
        full_blocks,
        row_chunks=row_chunks,
        default_year=default_year,
        leaf_policy=leaf_policy,
        leaf_sample_max=leaf_sample_max,
        leaf_sample_min_rows=leaf_sample_min_rows,
    )

    if len(full_blocks) == 1 and len(leaf_blocks) <= 1:
        # 小表（整表一块；或单行就超预算、拆不出第二块）：上下文折进叶子，
        # 不另设父容器（多一层只会重复）。
        seq += 1
        result.nodes.append(
            ChunkNode(
                chunk_type="table",
                seq=seq,
                content=_table_parent_content(
                    section_path=section_path, above=above, full_table=full_blocks[0], below=below
                ),
                heading_path=table_heading,
                page_from=table.page,
                page_to=table.page,
                # ⚠️ 表格**不要**写 `figure_id`。这里原本塞的是 `table.table_id`，
                # 而那是"文档内第几张表"的计数器（1、2、3…）。后果是引用表格块时
                # 前端看到 figure_id 有值就去渲染图片 → 取不到 → 显示"图不可用"
                # （实测 164 个表格块全中）。表格的标识在正文里（`表名: …`），
                # 若要单独追踪，应另开 `table_id` 字段而不是借 figure_id。
                figure_id=None,
                parent_index=prose_parent,
            )
        )
        return seq

    # 大表：出「父容器（上文 + 整表 + 下文）+ 行/组叶子」。父容器被子块覆盖后按
    # 叶子规则不入向量（retrievable.py）；叶子命中后经 `parent_expand` 扩成整表父块。
    container_content = _table_parent_content(
        section_path=section_path,
        above=above,
        full_table="\n\n".join(full_blocks),
        below=below,
    )
    # 硬上限：表格父容器 > 2048 token 时**分段**（DB 里任何块都不得超过 2048，
    # 用户已拍板「容器 2048 + 超限分段展开」）。未超限 / 不可分段时保持原单容器行为。
    segments: list[_TableContainerSegment] | None = None
    if count_tokens(container_content) > TABLE_CONTAINER_MAX_TOKENS:
        distinct_headers = {
            h for b in full_blocks if (h := _relevant_header_line(b))
        }
        # 指标组宽表：多段「相关表头」→ 按表头分组建父容器（避免行号跨月串组）
        if len(distinct_headers) > 1:
            segments = _segments_by_relevant_header(
                full_blocks=full_blocks,
                leaf_blocks=leaf_blocks,
                section_path=section_path,
                above=above,
                below=below,
            )
        else:
            segments = _segment_table_container(
                table=table,
                section_path=section_path,
                above=above,
                below=below,
                row_chunks=row_chunks,
            )
        if segments is None:
            if len(full_blocks) > 1:
                segments = _segments_by_relevant_header(
                    full_blocks=full_blocks,
                    leaf_blocks=leaf_blocks,
                    section_path=section_path,
                    above=above,
                    below=below,
                )
            else:
                log.warning(
                    "table_container_unsplittable",
                    table_id=str(table.table_id),
                    caption=(table.caption or "")[:80],
                    container_tokens=count_tokens(container_content),
                    hint="单行即超分段预算，回退为单容器（不丢内容）",
                )
    if segments:
        return _append_table_segments(
            result,
            seq,
            table=table,
            segments=segments,
            table_heading=table_heading,
            prose_parent=prose_parent,
            page=table.page,
        )

    seq += 1
    container_index = len(result.nodes)
    result.nodes.append(
        ChunkNode(
            chunk_type="table",
            seq=seq,
            content=container_content,
            heading_path=table_heading,
            page_from=table.page,
            page_to=table.page,
            figure_id=None,
            parent_index=prose_parent,
        )
    )
    for block in leaf_blocks:
        seq += 1
        result.nodes.append(
            ChunkNode(
                chunk_type="table",
                seq=seq,
                content=block,
                heading_path=table_heading,
                page_from=table.page,
                page_to=table.page,
                figure_id=None,
                parent_index=container_index,
            )
        )
    return seq


def _inject_doc_metadata_line(nodes: list["ChunkNode"], line: str) -> None:
    """文档元数据行：注入上下文容器 + **每篇文档的第一个检索叶子**。

    只在父块里放元数据不够：文档第一个子块（正文 1.目的/第一张表等）才是多数
    问句命中的检索单元，元数据问句（编制人/批准人/生效日期）若只命中表格叶子，
    ``parent_expand`` 不会把正文父块带进来 → LLM 看不到元数据（doc 206 实测）。
    所以额外只给**第一个叶子**加一行（不是每个叶子）：既让元数据可检索，又避免
    100 篇文档 × 每块重复同值元数据、制造大量同质检索块。
    """
    if not line:
        return
    container_indexes = {
        int(node.parent_index) for node in nodes if node.parent_index is not None
    }
    for index in sorted(container_indexes):
        if index < 0 or index >= len(nodes):
            continue
        node = nodes[index]
        if node.chunk_type not in {"parent", "table"}:
            continue
        if node.content.startswith(line):
            continue
        content = f"{line}\n\n{node.content}"
        tokens = count_tokens(content)
        # 表格容器有硬上限：加元数据后若越界，就放弃这一张表的注入（不破坏 2048 不变量）。
        if node.chunk_type == "table" and tokens > TABLE_CONTAINER_MAX_TOKENS:
            log.warning(
                "doc_metadata_skipped_for_table_container",
                tokens=tokens,
                limit=TABLE_CONTAINER_MAX_TOKENS,
            )
            continue
        node.content = content
        node.token_count = tokens

    # 第一个检索叶子也带元数据：它通常是「1.目的/第一张表」，是元数据问句最该命中的块。
    first_leaf = next(
        (
            index
            for index, node in enumerate(nodes)
            if index not in container_indexes and node.chunk_type in {"child", "table"}
        ),
        None,
    )
    if first_leaf is None:
        return
    node = nodes[first_leaf]
    if node.content.startswith(line):
        return
    content = f"{line}\n\n{node.content}"
    tokens = count_tokens(content)
    if tokens > CHILD_MAX_TOKENS * 2:
        log.warning(
            "doc_metadata_first_leaf_too_long",
            chunk_id=node.chunk_id if hasattr(node, "chunk_id") else None,
            tokens=tokens,
            child_max=CHILD_MAX_TOKENS,
        )
    node.content = content
    node.token_count = tokens


def _default_year_for_month_series(
    *,
    source_path: str,
    infer_from_filename: bool,
) -> str | None:
    """仅在显式开关打开时从文件名推年；默认关，避免跨年错位。"""
    if not infer_from_filename or not source_path:
        return None
    from ekbs.ingest.table_types import infer_year_from_source

    return infer_year_from_source(source_path=source_path)


def chunk_markdown(  # noqa: C901 — 结构切分/装箱主路径，历史债
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
    source_path: str = "",
    table_row_chunks: bool = False,
    doc_metadata_line: str = "",
    month_series_infer_year_from_filename: bool = False,
    table_leaf_policy: str = "auto",
    table_leaf_sample_max: int = 64,
    table_leaf_sample_min_rows: int = 200,
) -> ChunkingResult:
    """Markdown → 父块 + 子块 + 图片块 + 表格块（`06` §3 的口径）。

    ``figures`` 由解析层给出（图片已落盘）。每张图建一条 ``chunk_type=figure``
    **资产行**（占位正文 ``[图片]``），``parent_index`` 绑到同页/同节 parent；
    **不入向量/FTS**（见 :mod:`ekbs.ingest.retrievable`）。不做题注/OCR 检索文。

    ``tables`` 走确定性 ``information_block`` 序列化，整表独立成 ``chunk_type=table``，
    不与正文 parent/child 重复索引。

    ``doc_title``：小节无标题时注入 ``heading_path`` 根，避免检索用文本丢限定词。

    ``doc_metadata_line``：从解析层页眉元数据生成的一行文档信息（文件编号/版本/
    生效日期/截止日期，如 ``【文档信息】文件编号：XJWY-GC-09；生效日期：2025-01-01；
    截止日期：未标注``）。它只注入**上下文容器**（有子块的父块/表格父容器）的正文，
    不写进叶子块——否则又会变成每篇文档都一样的可检索文本。容器不进向量/FTS，
    命中子块后经 ``parent_expand`` 带进 prompt，日期类问题才有依据。

    ``source_path``：原始文件路径，用于两处判定 ——
    ① ``table_row_chunks`` 只对电子表格（xlsx/xls/xlsm）生效；
    ② ``.ppt/.pptx`` 开启**按页硬切**（子块/父块都不跨页，挂图才能跟同页走）。
    Word/PDF 与**漏传来源**仍按 token 聚合；忘传 ppt 路径时退回聚合，不能误伤制度文档。

    结构（`20` §3.1/§3.2）：**按 token 目标切，不按标题切**。
    制度/作业指导书每个标题下常常只有一两行，按标题切出来的块只有 10~60 token ——
    父块和子块同时退化成碎片，父子关系名存实亡（用户实测反馈）。

    * **子块**（检索单元）：把连续小节聚合到 ``child_min``~``child_max``（收口点落在
      标题边界，不拆行），相邻子块注入 12%~15% 尾部重叠；
    * **父块**（上下文容器）：再把子块按 ``parent_min``~``parent_max`` 装箱，可跨章；
    * 同一段文字不会双索引：父块被子块覆盖后按「叶子块才是检索单元」规则不入向量
      （见 :mod:`ekbs.ingest.retrievable`）。
    """

    # 表行分块（一行一块）**只对电子表格成立**：xlsx/xls/xlsm 的一行就是一条记录
    # （RAGFlow spreadsheet 口径，实测 top1 有增益）；Word/PDF/PPT 的表行只是单元格
    # 网格，逐行切会把制度表格碎成 55~115 token 的块。规则放在分块器内而不是调用点：
    # ``source_path`` 漏传时默认 False（行组叶子），调用方忘传也不能退化。
    # 延迟导入：parser → code_chunker → chunker 模块级会成环。
    from ekbs.ingest.parser import is_slide_source, is_spreadsheet_source

    row_chunks = bool(table_row_chunks) and is_spreadsheet_source(source_path)
    # 幻灯片硬按页切：短页也独立成块，父块一页一个，图挂同页。
    page_hard_cut = is_slide_source(source_path)
    default_year = _default_year_for_month_series(
        source_path=source_path,
        infer_from_filename=month_series_infer_year_from_filename,
    )

    result = ChunkingResult()
    seq = 0
    root = ensure_heading_path("", doc_title=doc_title)
    # 幻灯片：清洗会改偏移，必须从「第 N 页」标题重扫 page_map，再强制小节页码=标题页。
    effective_page_map = page_map
    if page_hard_cut:
        rebuilt = slide_page_map_from_markdown(markdown)
        if rebuilt:
            effective_page_map = rebuilt
    raw_sections = pack_major_sections(
        split_sections(markdown, page_map=effective_page_map)
    )
    if page_hard_cut:
        raw_sections = _force_slide_section_pages(raw_sections)
    # 表块要挂回它**所在小节**的路径：必须在空壳节合并前取。合并后 heading 变成下一节的，
    # 5.3.2 的表会被记成 5.3.3（实测 doc 196 的表 5）。
    table_section_path: dict[str, str] = {}
    for section in raw_sections:
        for table_id in _placeholder_table_ids(section.text):
            table_section_path.setdefault(table_id, section.heading_path)
    sections = merge_short_sections_into_next(
        raw_sections, page_hard_cut=page_hard_cut
    )
    # 文档元数据行作为**第一个小节**参与子块装箱：这样它计入 child_max 预算，
    # 第一个子块不会因为后置注入而越界（实测后置注入 370~422 > 384）。
    metadata_heading = sections[0].heading_path if sections else ""
    if doc_metadata_line and sections:
        sections = [
            Section(heading_path="", text=doc_metadata_line, start_offset=0),
            *sections,
        ]
    drafts = _build_child_drafts(
        sections,
        child_min=child_min,
        child_max=child_max,
        page_hard_cut=page_hard_cut,
    )
    if doc_metadata_line and drafts and not drafts[0].heading_path:
        # 元数据小节没有自己的 heading；第一个子块的 heading_path 仍取原来的第一小节，
        # 引用卡片才显示「1.目的」而不是文档名。
        drafts[0].heading_path = metadata_heading
    _merge_small_tail_child(
        drafts,
        child_min=child_min,
        child_max=child_max,
        page_hard_cut=page_hard_cut,
    )
    # 幻灯片页间不做重叠：重叠前缀会把邻页正文/页码边界弄脏
    if not page_hard_cut:
        _apply_draft_overlap(drafts, overlap_tokens=int(child_max * OVERLAP_MIN_RATIO))
    table_container: dict[str, int] = {}
    for group in _pack_parent_drafts(
        drafts,
        parent_min=parent_min,
        parent_max=parent_max,
        page_hard_cut=page_hard_cut,
    ):
        parent_text = "\n\n".join(draft.body for draft in group)
        if not parent_text:
            continue
        starts = [draft.page_from for draft in group if draft.page_from is not None]
        ends = [draft.page_to for draft in group if draft.page_to is not None]
        seq += 1
        result.nodes.append(
            ChunkNode(
                chunk_type="parent",
                seq=seq,
                content=parent_text,
                heading_path=ensure_heading_path(
                    _parent_heading(group, root=root), doc_title=doc_title
                ),
                page_from=min(starts) if starts else None,
                page_to=max(ends) if ends else None,
            )
        )
        parent_index = len(result.nodes) - 1
        for draft in group:
            for table_id in draft.table_ids:
                table_container.setdefault(table_id, parent_index)
            seq += 1
            result.nodes.append(
                ChunkNode(
                    chunk_type="child",
                    seq=seq,
                    content=draft.content,
                    heading_path=ensure_heading_path(draft.heading_path, doc_title=doc_title),
                    page_from=draft.page_from,
                    page_to=draft.page_to,
                    parent_index=parent_index,
                )
            )

    for figure in figures:
        if not str(figure.image_path or "").strip():
            log.debug("figure_skipped_no_path", figure_id=figure.figure_id or "")
            continue
        parent_index = _parent_index_for_figure(result.nodes, page=figure.page)
        # 无同页 parent 时挂最后一个 parent（docx 无页码常见）；再没有则仍落资产行。
        if parent_index is None:
            for idx in range(len(result.nodes) - 1, -1, -1):
                if result.nodes[idx].chunk_type == "parent":
                    parent_index = idx
                    break
        seq += 1
        parent_heading = (
            result.nodes[parent_index].heading_path
            if parent_index is not None
            else ""
        )
        # 题注/页标题进 heading：挂图回源按问句打分时要跟栏目走，不能只剩文档名。
        caption = str(getattr(figure, "caption", "") or "").strip()
        fig_heading = ensure_heading_path(
            caption or parent_heading or doc_title, doc_title=doc_title
        )
        result.nodes.append(
            ChunkNode(
                chunk_type="figure",
                seq=seq,
                content="[图片]",
                heading_path=fig_heading,
                page_from=figure.page,
                page_to=figure.page,
                figure_id=figure.figure_id or None,
                image_path=figure.image_path,
                parent_index=parent_index,
            )
        )

    for table in tables:
        seq = _append_table_nodes(
            result,
            seq,
            table,
            doc_title=doc_title,
            table_section_path=table_section_path,
            raw_sections=raw_sections,
            table_container=table_container,
            row_chunks=row_chunks,
            default_year=default_year,
            leaf_policy=table_leaf_policy,
            leaf_sample_max=table_leaf_sample_max,
            leaf_sample_min_rows=table_leaf_sample_min_rows,
        )
    # 一行文档元数据只进上下文容器（不制造可检索的同质块，见 _inject_doc_metadata_line）
    _inject_doc_metadata_line(result.nodes, doc_metadata_line)
    return result


def _parent_index_for_figure(
    nodes: Sequence[ChunkNode], *, page: int | None
) -> int | None:
    """按页码把图挂到覆盖该页的 parent；多候选时取页距最窄的（更贴章节）。"""
    if page is None:
        return None
    best: tuple[int, int] | None = None  # (span, index)
    for idx, node in enumerate(nodes):
        if node.chunk_type != "parent":
            continue
        start = node.page_from
        end = node.page_to if node.page_to is not None else node.page_from
        if start is None:
            continue
        if end is None:
            end = start
        if int(start) <= int(page) <= int(end):
            span = int(end) - int(start)
            if best is None or span < best[0]:
                best = (span, idx)
    return best[1] if best is not None else None


def _figure_search_text(
    figure: FigureInput,
    *,
    doc_title: str = "",
    figure_index: int = 0,
) -> str:
    """兼容旧调用：图片不再用题注/OCR 做检索文，一律占位 ``[图片]``。"""
    del figure, doc_title, figure_index
    return "[图片]"


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
