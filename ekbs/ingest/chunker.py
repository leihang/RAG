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

**条款各自成块**：``第X条``、``4.1`` 这种已经切开的条款不再按长度并进同一块。
一条写得很长时，仍只在这一条内部按 1024～1536 / 256～384 切开。

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
#: 只有标题/占位符、没有正文的节（如 ``[表格 1]``、``[图片]``）
_PLACEHOLDER_ONLY_RE = re.compile(r"^\[(?:表格|图片)\s*\d*\]$")
TABLE_SPLIT_TOKENS = 2048
#: 表格**叶子**预算（与正文 child 上限 384 对齐）：整表超过这个就拆「行组叶子」，
#: 每叶重复表头、逐块嵌入。整表一个向量时十几行数据会互相稀释（实测 920 token 的
#: 「表1-年度能耗管理计划编制及实施要求」查「A级能耗普查」检索不中）。
TABLE_LEAF_TOKENS = 384
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

        def _collect(indices: range) -> str:
            parts: list[str] = []
            for i in indices:
                line = lines[i].strip()
                if not line or _is_boundary(line):
                    break
                parts.append(line)
            return " ".join(parts).strip()[:max_chars]

        return _collect(range(idx - 1, -1, -1)), _collect(range(idx + 1, len(lines)))
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


def _table_leaf_blocks(
    table: TableInput, full_blocks: Sequence[str], *, row_chunks: bool
) -> Sequence[str]:
    """表格叶子块：≤``TABLE_LEAF_TOKENS`` 用整表一块；超过就按行组拆（每叶重复表头）。

    整表一个向量时十几行数据会互相稀释（实测 920 token 的「表1-年度能耗管理计划编制
    及实施要求」查「A级能耗普查」检索不中）。``row_chunks=True`` 时退化为一行一块
    （配置项 ``ingest.table_row_chunks``，默认关）。
    """
    # 延迟导入：table_serialize 反向依赖本模块的 count_tokens，模块级导入会成环
    from ekbs.ingest.table_serialize import serialize_table_body

    if sum(count_tokens(block) for block in full_blocks) <= TABLE_LEAF_TOKENS:
        return full_blocks
    return serialize_table_body(
        caption=table.caption,
        body=table.body,
        table_id=table.table_id,
        row_chunks=row_chunks,
        max_tokens=TABLE_LEAF_TOKENS,
    )


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


def _build_child_drafts(
    sections: Sequence[Section], *, child_min: int, child_max: int
) -> list[_ChildDraft]:
    """按 token 目标把小节**跨标题**聚合成子块（`20` §3.2 规则 3/4）。

    为什么不再"一个标题一个子块"：制度/作业指导书每个标题下往往只有一两行，按标题切
    出来的块只有 10~60 token，父子块全部退化成碎片（实测 doc 196 的 33 节 → 33 个
    6~120 token 的块，父子关系名存实亡）。聚合到 ``child_min`` 才收口；收口点落在
    标题边界上（小节是切分的最小单位，不会把一行拆两半）。
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
            drafts.append(
                _ChildDraft(
                    content=body,
                    body=body,
                    heading_path=buf[0].heading_path,
                    page_from=min(starts) if starts else None,
                    page_to=max(ends) if ends else None,
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
        if buf and buf_tokens + section_tokens > child_max:
            flush()
        buf.append(section)
        buf_tokens += section_tokens
        if buf_tokens >= child_min:
            flush()
    flush()
    return drafts


def _merge_small_tail_child(drafts: list[_ChildDraft], *, child_min: int, child_max: int) -> None:
    """收尾的碎块（< child_min/2）并入前一块 —— 尾部余量不该单独成块。

    尾部是否成块取决于总长能否整除目标，无法从算法上避免；但 10~60 token 的孤立尾块
    会污染 rerank（同一篇里块大小差一个量级），所以超小尾块并回前一块（不超 child_max 时）。
    """
    if len(drafts) < 2:
        return
    last, prev = drafts[-1], drafts[-2]
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
    drafts: Sequence[_ChildDraft], *, parent_min: int, parent_max: int
) -> list[list[_ChildDraft]]:
    """把子块按 ``parent_min``~``parent_max`` 装箱成父块（可跨章跨节，`20` §3.1）。

    父子关系要有意义，父块就必须比子块大一个量级：按标题/章分组时，2 页文档的「章」
    只有几十到几百 token，父块约等于子块 → 父块扩展等于没扩展（用户实测反馈）。
    """
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


def merge_short_sections_into_next(sections: Sequence[Section]) -> list[Section]:
    """把**空壳节**（无正文 / 只有占位符）并入下一节（行尾连续的并入上一节）。

    为什么只并空壳、不做"短节按长度合并"：条款（``第X条`` / ``4.1``）和编号小节各自
    独立成块是既有契约（`test_pack_major_dotted_sections` /
    `test_chapter_and_fullwidth_dotted_headings`），按长度合并会把不同规定混进同一个块，
    检索精度反而变差。这里只清掉**没有正文可检索**的碎片块（实测 ``[表格 1]``、
    ``6．质量记录`` 这类块会跨文档重复出现，命中后什么信息也没有）。
    """
    merged: list[Section] = []
    buffered: list[Section] = []
    for section in sections:
        if _is_placeholder_only(section):
            buffered.append(section)
            continue
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
        merged[-1] = _join_trailing(merged[-1], buffered)
    return merged


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
        """图片块：**与 child 一样可检索**（向量化 + 进 FTS），但保留自己的类型，
        以便 `/assets/figure/{chunk_id}` 能回源出图、前端把引用渲染成图。"""
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
    for title, path, source, head_off in heading_log:
        if not source.strip() or title in " ".join(emitted_paths):
            continue
        if _strip_md_marks(source) in emitted_text:
            continue
        page_from, page_to = _pages_for(head_off, head_off + len(source), page_map)
        sections.append(
            Section(
                heading_path=path or title,
                text=source.strip(),
                page_from=page_from,
                page_to=page_to,
            )
        )


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
                    heading_path=current_heading, text=text, page_from=page_from, page_to=page_to
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
        if source_line and _looks_like_content(source_line.strip()):
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
    table_row_chunks: bool = False,
) -> ChunkingResult:
    """Markdown → 父块 + 子块 + 图片块 + 表格块（`06` §3 的口径）。

    ``figures`` 由解析层给出（图片已落盘）。**只有"有文本"的图片才建索引块**：
    题注与 OCR 都为空时，图片仍留在磁盘上可被浏览，但不进向量/FTS ——
    给一堆无文字的图造近似重复的块，只会污染检索。

    ``tables`` 走确定性 ``information_block`` 序列化，整表独立成 ``chunk_type=table``，
    不与正文 parent/child 重复索引。

    ``doc_title``：小节无标题时注入 ``heading_path`` 根，避免检索用文本丢限定词。

    结构（`20` §3.1/§3.2）：**按 token 目标切，不按标题切**。
    制度/作业指导书每个标题下常常只有一两行，按标题切出来的块只有 10~60 token ——
    父块和子块同时退化成碎片，父子关系名存实亡（用户实测反馈）。

    * **子块**（检索单元）：把连续小节聚合到 ``child_min``~``child_max``（收口点落在
      标题边界，不拆行），相邻子块注入 12%~15% 尾部重叠；
    * **父块**（上下文容器）：再把子块按 ``parent_min``~``parent_max`` 装箱，可跨章；
    * 同一段文字不会双索引：父块被子块覆盖后按「叶子块才是检索单元」规则不入向量
      （见 :mod:`ekbs.ingest.retrievable`）。
    """
    from ekbs.ingest.table_serialize import serialize_table_body

    result = ChunkingResult()
    seq = 0
    root = ensure_heading_path("", doc_title=doc_title)
    raw_sections = pack_major_sections(split_sections(markdown, page_map=page_map))
    # 表块要挂回它**所在小节**的路径：必须在空壳节合并前取。合并后 heading 变成下一节的，
    # 5.3.2 的表会被记成 5.3.3（实测 doc 196 的表 5）。
    table_section_path: dict[str, str] = {}
    for section in raw_sections:
        for table_id in _placeholder_table_ids(section.text):
            table_section_path.setdefault(table_id, section.heading_path)
    sections = merge_short_sections_into_next(raw_sections)
    drafts = _build_child_drafts(sections, child_min=child_min, child_max=child_max)
    _merge_small_tail_child(drafts, child_min=child_min, child_max=child_max)
    _apply_draft_overlap(drafts, overlap_tokens=int(child_max * OVERLAP_MIN_RATIO))
    table_container: dict[str, int] = {}
    for group in _pack_parent_drafts(drafts, parent_min=parent_min, parent_max=parent_max):
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

    for fig_index, figure in enumerate(figures, start=1):
        content = _figure_search_text(
            figure, doc_title=doc_title, figure_index=fig_index
        )
        if not content:
            log.debug("figure_skipped_no_text", image_path=figure.image_path)
            continue
        seq += 1
        fig_heading = ensure_heading_path(figure.caption or doc_title, doc_title=doc_title)
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
        # 整表先序列化一次（不拆）：拿「表格本身」全文给父块用。
        full_blocks = serialize_table_body(
            caption=table.caption,
            body=table.body,
            table_id=table.table_id,
        )
        if not full_blocks:
            continue
        # 表名单独成块时没有文档上下文（「表 1」这种名字跨文档完全一样），
        # 把文档名并进 heading：UI 里一眼看得出是哪篇的表，精排文本也自带身份。
        table_name = (table.caption or "").strip() or (
            f"表 {table.table_id}" if table.table_id else ""
        )
        section_path = (table_section_path.get(str(table.table_id)) or "").strip()
        if section_path:
            base = f"{doc_title} > {section_path}" if doc_title else section_path
            table_heading = ensure_heading_path(
                f"{base} · {table_name}" if table_name else base, doc_title=doc_title
            )
        else:
            table_heading = ensure_heading_path(
                f"{doc_title} · {table_name}" if doc_title and table_name else table_name,
                doc_title=doc_title,
            )
        above, below = _table_adjacent_prose(raw_sections, str(table.table_id))
        prose_parent = table_container.get(str(table.table_id))

        # 叶子预算：整表超过 384 token 就拆「行组叶子」。
        # 920 token 的表（十几行数据）塞进同一个向量会被互相稀释 —— 查某一行
        # 的语义（如「A级能耗普查」）时整表向量对不上，检索直接落空。
        leaf_blocks = _table_leaf_blocks(table, full_blocks, row_chunks=table_row_chunks)

        if len(full_blocks) == 1 and len(leaf_blocks) <= 1:
            # 小表（整表一块；或单行就超预算、拆不出第二块）：上下文折进叶子，
            # 不另设父容器（多一层只会重复）。
            seq += 1
            result.nodes.append(
                ChunkNode(
                    chunk_type="table",
                    seq=seq,
                    content=_table_parent_content(
                        section_path=section_path,
                        above=above,
                        full_table=full_blocks[0],
                        below=below,
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
            continue

        # 大表：出「父容器（上文 + 整表 + 下文）+ 行/组叶子」。父容器被子块覆盖后按
        # 叶子规则不入向量（retrievable.py）；叶子命中后经 `parent_expand` 扩成整表父块。
        seq += 1
        container_index = len(result.nodes)
        result.nodes.append(
            ChunkNode(
                chunk_type="table",
                seq=seq,
                content=_table_parent_content(
                    section_path=section_path,
                    above=above,
                    full_table="\n\n".join(full_blocks),
                    below=below,
                ),
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
    return result


def _figure_search_text(
    figure: FigureInput,
    *,
    doc_title: str = "",
    figure_index: int = 0,
) -> str:
    """图片的可检索文本 = 题注 + 图中文字（OCR）。

    两者都空时：若有 ``doc_title`` 则用 ``[图片] 文档名 · 图N`` 兜底（OCR 默认关闭时
    教程截图常见无字，仍须建块以便多模态检索与钉钉回源）；连文档名也没有才跳过。
    """
    parts: list[str] = []
    if figure.caption.strip():
        parts.append(f"[图片] {figure.caption.strip()}")
    if figure.ocr_text.strip():
        if not parts:
            parts.append("[图片]")
        parts.append(figure.ocr_text.strip())
    if parts:
        return "\n".join(parts).strip()
    title = (doc_title or "").strip()
    if not title:
        return ""
    if figure_index > 0:
        return f"[图片] {title} · 图{figure_index}"
    return f"[图片] {title}"


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
