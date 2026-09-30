"""代码感知分块 —— 按 AST / 符号边界切分（代码知识库专用）。

**为什么不能沿用** :mod:`ekbs.ingest.chunker` **的定长/标题分块**：

* 制度文档按段落切没问题，但源码定长切会**把函数从中间切断** —— 前半段的局部变量与
  后半段的返回值各自都不可读，检索到的片段既解释不了「这个函数做什么」，也拼不出
  可用上下文，进模型只会污染上下文；
* 代码的天然边界是**符号**（类 / 函数 / 方法）。一个符号一个块，检索命中后能把整段
  代码原样交给模型，这是代码问答可用性的下限；
* ``heading_path`` 里**同时带上文件路径与符号名**（形如
  ``ekbs/ingest/chunker.py > chunk_markdown``）是代码检索最有效的词面信号：用户问
  「chunk_markdown 怎么切父块」时，稠密与稀疏召回都能落到这一块上；
* 符号本身超过 ``max_tokens`` 时，在该符号**内部**再切成带重叠的子块，父块保留全文
  —— 与 :func:`ekbs.ingest.chunker.chunk_markdown` 的「父块 + 子块」口径一致
  （父块供上下文扩展，子块供索引/rerank）。

两条分支：

* **Python**（``.py`` / ``.pyi`` / ``.pyw``，以及后缀未知的源码）走标准库 :mod:`ast`：
  精确、能拿到装饰器与真实符号名，且**语法错误不会抛出去** —— 捕获 :class:`SyntaxError`
  后退回启发式分支。
* **其它语言与语法错误的 Python** 走 :func:`_heuristic_segments`：**这是启发式、不是
  AST**，只在「缩进为 0 的形如 ``function`` / ``class`` / ``func`` / ``CREATE`` /
  ``public`` / ``def`` 的行首」认边界；认不出边界时退化为**带重叠的定长窗口**。

仅依赖标准库；类型直接复用 :class:`ekbs.ingest.chunker.ChunkNode` 与
:func:`ekbs.ingest.chunker.count_tokens`，不另造类型。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ekbs.ingest.chunker import ChunkNode, count_tokens

#: 模块级无归属代码的符号名（``heading_path`` 为 ``<path> > <module>``）
MODULE_SYMBOL = "<module>"
#: 超长符号切成子块时，子块 ``heading_path`` 追加的序号分隔符（如 ``Foo.bar#2``）
CHILD_SEQ_SEP = "#"
#: 单块内容相对 ``max_tokens`` 的硬上限。为什么是 1.5 倍：窗口按行贪心装入，
#: ``overlap`` 被夹到 ``max_tokens // 2``，因此「重叠前缀 + 一行超限行」最多
#: ``0.5 * max_tokens + max_tokens``。超过这个数的块必须继续切。
MAX_BLOCK_RATIO = 1.5

#: 明确按「非 Python」处理的后缀 —— 这些语言直接走启发式，不做 ``ast.parse`` 尝试。
#: 未列出的后缀（含空后缀、``.txt``）会**先试** :mod:`ast`，失败再回退启发式，
#: 这样「代码片段字符串」也能享受 AST 分支。
_OTHER_LANGUAGE_SUFFIXES = frozenset(
    {
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte",
        ".java", ".kt", ".kts", ".scala", ".groovy", ".cs", ".swift", ".m", ".mm",
        ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".dart",
        ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".ex", ".exs", ".erl", ".hs", ".clj",
        ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".proto",
    }
)


#: 会被"代码感知分块"处理的后缀（入库侧据此决定走 `chunk_code` 还是 `chunk_markdown`）。
#: 注意 `.py` 之外的语言走的是**启发式**（非 AST），见 :func:`_heuristic_segments`。
CODE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".pyi", ".pyw"} | set(_OTHER_LANGUAGE_SUFFIXES)
)


def is_code_source(path: str) -> bool:
    """该路径是否应按「代码」分块（而不是按 Markdown 段落）。"""
    return _suffix_of(str(path)) in CODE_SUFFIXES


def display_source_path(settings, source_path: str) -> str:
    """把「机器上的绝对路径」换成**可检索的稳定路径**，用于代码块的 ``heading_path``。

    为什么必须换（实测踩到）：worker 与 CLI 拿到的都是磁盘绝对路径，原样传给
    :func:`chunk_code` 后，每个代码块的 ``heading_path`` 变成
    ``/Users/<用户名>/…/var/raw/代码样例/order_service.py > OrderService``，三个后果：

    * 同一份代码在不同机器/不同 ``data_dir`` 下**检索语义不同**（绝对路径进了嵌入文本）；
    * 路径里的用户名、项目目录名混进向量与 FTS，纯属噪声；
    * 服务器目录结构随引用卡片泄漏给最终用户。

    口径：在 ``data_dir/raw`` 下 → ``raw/<相对路径>``；在 ``data_dir`` 其它位置 →
    相对 ``data_dir``；本来就是相对路径 → 原样保留（CLI ``--dir ekbs/ingest`` 的情形）；
    **落在数据目录之外** → 只留文件名（不泄漏机器布局）。
    """
    if not source_path:
        return ""
    raw_path = Path(source_path)
    if not raw_path.is_absolute():
        return raw_path.as_posix()
    resolved = raw_path.resolve()
    try:
        data_dir = Path(settings.data_dir).resolve()
        raw_root = Path(settings.data_paths()["raw"]).resolve()
    except Exception:  # pragma: no cover - 配置异常时退回文件名
        return raw_path.name
    for base, prefix in ((raw_root, "raw/"), (data_dir, "")):
        try:
            return prefix + resolved.relative_to(base).as_posix()
        except ValueError:
            continue
    return raw_path.name


def chunk_code(
    source: str,
    *,
    path: str,
    max_tokens: int = 800,
    min_tokens: int = 40,
    overlap: int = 80,
) -> list[ChunkNode]:
    """把源码切成「符号级」分块。

    参数
    ----
    source:
        源码全文。
    path:
        文件路径（也用于选分支）。它会**原样**写进每个块的 ``heading_path`` 前缀，
        因此调用方应传仓库相对路径（如 ``ekbs/ingest/chunker.py``），不要传绝对路径
        —— 绝对路径会让同一份代码在不同机器上产生不同的检索语义。
    max_tokens:
        一个符号块的目标上限。符号超过它就切成子块；窗口按此值装行。
    min_tokens:
        碎片下限（软约束）。**不会**用它去合并符号父块 —— 顶层 ``class`` / ``def``
        各有检索身份，合并会破坏「符号即块」的口径。它只用于：切窗口时把尾部的
        退化碎片并回前一块，避免出现几十 token 的碎块。
    overlap:
        子块 / 窗口之间的重叠 token 预算（内部夹到 ``max_tokens // 2``，保证切分必定
        向前推进）。

    返回
    ----
    ``list[ChunkNode]``，``seq`` 从 1 起按返回顺序递增；空文件 / 纯空白返回 ``[]``。

    行为口径
    --------
    * **Python（AST）**：顶层 ``class`` / ``def`` / ``async def`` 各一个 ``parent``；
      类本身一个 ``parent``，其**每个方法再各一个** ``parent``（``heading_path`` 为
      ``<path> > Class.method``）；模块级代码（import、常量、``if __name__`` 等无归属
      顶层语句）**聚成一个** ``<path> > <module>`` 的 ``parent``，位置按其中第一条
      语句在源码里的顺序。符号超过 ``max_tokens`` 时，保留其 ``parent`` 全文容器，
      并追加 ``child`` 块（``parent_index`` 指向该 ``parent`` 在返回列表中的下标，
      ``heading_path`` 为 ``父块路径#序号``，序号从 1 起）。
    * **其它语言 / 语法错误的 Python（启发式，不是 AST）**：按「缩进为 0 且行首形如
      ``function`` / ``class`` / ``func`` / ``fn`` / ``CREATE`` / ``public`` …
      的顶层边界」切段，认出的符号名进 ``heading_path``；段内超过 ``max_tokens``
      时与 AST 分支同样处理；**完全认不出边界**（或无法归属的前导代码）时退化为
      **带重叠的定长窗口**，窗口块本身即 ``parent``，``heading_path`` 只保证含
      ``<path>``。窗口按行拼回，全文不会丢（允许重叠）。
    * ``page_from`` / ``page_to`` 一律留 ``None``：代码没有页码，不伪造。
    * **护栏**：除「被切分的符号父容器」（它必须保留全文供上下文扩展）外，任何块的
      ``token_count`` 都不超过 ``max_tokens * 1.5``；``content`` 一律非空白、
      ``token_count > 0``（由 :class:`ChunkNode` 的 ``__post_init__`` 兜底）。
    """
    base = (path or "").strip() or "<source>"
    if not source or not source.strip():
        return []

    max_tokens = max(1, int(max_tokens))
    min_tokens = max(1, min(int(min_tokens), max_tokens))
    overlap = max(0, min(int(overlap), max_tokens // 2))

    segments: list[_Segment] | None = None
    if _suffix_of(base) not in _OTHER_LANGUAGE_SUFFIXES:
        try:
            segments = _python_segments(source, base)
        except SyntaxError:
            # 语法错误的 Python 不能让异常冒出去：退回启发式（见模块 docstring）
            segments = None
        except (ValueError, RecursionError):
            # NUL 字节 / 过深嵌套等 ast 也会拒绝的输入，同样走兜底
            segments = None
    if segments is None:
        segments = _heuristic_segments(source, base)

    nodes: list[ChunkNode] = []
    for segment in segments:
        _emit_segment(nodes, segment, max_tokens=max_tokens, min_tokens=min_tokens, overlap=overlap)
    return nodes


@dataclass(frozen=True)
class _Segment:
    """切分中间态：一段代码原文 + 它的 ``heading_path``。"""

    heading_path: str
    content: str
    #: 是否来自「认出来的符号边界」。为 False 的长段没有可表达的父容器，
    #: 直接按窗口切成多个顶层块。
    named: bool = True


# ---------------------------------------------------------------- Python（AST）


def _python_segments(source: str, base: str) -> list[_Segment]:
    """按 :mod:`ast` 把源码拆成符号段（模块级代码聚成一个段）。"""
    tree = ast.parse(source)
    lines = source.splitlines()
    segments: list[_Segment] = []
    module_parts: list[str] = []
    module_insert_at: int | None = None

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segments.append(_Segment(f"{base} > {node.name}", _node_text(lines, node)))
        elif isinstance(node, ast.ClassDef):
            segments.append(_Segment(f"{base} > {node.name}", _node_text(lines, node)))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    segments.append(
                        _Segment(f"{base} > {node.name}.{item.name}", _node_text(lines, item))
                    )
        else:  # import / 常量 / if __name__ … 等模块级语句
            if module_insert_at is None:
                module_insert_at = len(segments)
            module_parts.append(_node_text(lines, node))

    if module_parts:
        text = "\n".join(part for part in module_parts if part.strip()).strip()
        if text:
            at = module_insert_at if module_insert_at is not None else len(segments)
            segments.insert(at, _Segment(f"{base} > {MODULE_SYMBOL}", text))
    if not segments and source.strip():
        # 只有注释 / 空行时 AST 的 body 为空，但文件并非空文件 —— 不能整篇丢掉
        return [_Segment(base, source.strip(), named=False)]
    return segments


def _node_text(lines: Sequence[str], node: ast.AST) -> str:
    """取一个 AST 节点的源码原文（含装饰器，尽量含紧邻的注释）。

    为什么要往上多吃注释：Python 里「这段是干什么的」经常写在 ``#`` 注释里，
    AST 拿不到注释，而注释往往就在 ``def`` 上一行。遇到空行即停，避免把不相干的
    注释吸进来。
    """
    start = int(getattr(node, "lineno", 1) or 1)
    for decorator in getattr(node, "decorator_list", None) or ():
        start = min(start, int(getattr(decorator, "lineno", start) or start))
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    end = int(getattr(node, "end_lineno", None) or start)
    return "\n".join(lines[start - 1 : end]).strip()


# ------------------------------------------------- 启发式（非 AST，其它语言 / 兜底）

#: 行首符号识别规则（**启发式**，第一条命中即用）。分组 1 是符号名。
_SYMBOL_RULES: tuple[re.Pattern[str], ...] = (
    # JS / TS：function foo / export default async function foo
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    # 聚合类型：class Foo / public final class Foo / interface Foo / struct Foo / impl Foo
    re.compile(
        r"^(?:export\s+|default\s+|public\s+|private\s+|protected\s+|internal\s+|abstract\s+"
        r"|final\s+|open\s+|sealed\s+|static\s+|partial\s+|data\s+)*"
        r"(?:class|interface|enum|record|struct|trait|impl|object|union)\s+([A-Za-z_$][\w$]*)"
    ),
    # Go：func (r *T) Name(...) / func Name(...)
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)"),
    # Rust：fn foo / pub async unsafe fn foo
    re.compile(
        r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+|extern\s+\"[^\"]*\"\s+)*"
        r"fn\s+([A-Za-z_][\w]*)"
    ),
    # Python 风格 def（语法错误的 .py 落到这里时仍能认出边界）
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    # Java / C# / Kotlin 方法：public void foo(...) / private static int bar(...)
    re.compile(
        r"^(?:public|private|protected|internal|static|final|abstract|synchronized|virtual"
        r"|override|open|sealed|suspend|native)\b[^;{}=]*?\b([A-Za-z_$][\w$]*)\s*\("
    ),
    # C / C++ 函数定义：int main(...) / static void *alloc(...) / MyClass make(...)
    re.compile(
        r"^(?:static\s+|inline\s+|extern\s+|const\s+|virtual\s+|explicit\s+|unsigned\s+"
        r"|signed\s+)*(?:[A-Za-z_]\w*(?:::\w+)*(?:<[^;{}]*>)?[\s*&]+)+([A-Za-z_]\w*)\s*\("
    ),
)

#: SQL / 有头语句：``CREATE TABLE users`` 这种行首即边界，符号名取「关键字 + 表名」。
_SQL_RE = re.compile(
    r"^(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|INSERT|UPDATE|DELETE|SELECT|WITH"
    r"|BEGIN|DECLARE|MERGE|COMMENT)\b\s*(.*)$",
    re.IGNORECASE,
)
#: 行首修饰行（装饰器 / 注解 / 属性）：应与紧随其后的符号同块，而不是留给上一块
_DECORATOR_RE = re.compile(r"^(?:@\w|#\[|\[\s*[A-Za-z_]\w*\s*(?:\(|\]))")
#: 启发式符号名的最长保留长度（SQL 头一行可能很长）
_SYMBOL_MAX_CHARS = 48


def _heuristic_segments(source: str, base: str) -> list[_Segment]:
    """**启发式**（不是 AST）按「缩进为 0 的行首关键字」切段。

    认不出任何边界时返回**一段**（整篇），由 :func:`_window_text` 退化为带重叠的
    定长窗口 —— 这里不丢内容，是兜底路径存在的意义。
    """
    lines = source.splitlines()
    boundaries: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not line or line[0].isspace():  # 只看顶层（缩进为 0）
            continue
        name = _symbol_of(line)
        if name:
            boundaries.append((index, name))

    if not boundaries:
        text = source.strip()
        return [_Segment(base, text, named=False)] if text else []

    # 装饰器/注解行归到紧随其后的符号：把边界起点上移，但不得越过上一个边界
    starts: list[int] = []
    for position, (index, _) in enumerate(boundaries):
        lower = starts[position - 1] + 1 if position else 0
        while index > lower and _DECORATOR_RE.match(lines[index - 1]):
            index -= 1
        starts.append(index)

    segments: list[_Segment] = []
    preamble = "\n".join(lines[: starts[0]]).strip()
    if preamble:
        segments.append(_Segment(base, preamble, named=False))
    for position, (_, name) in enumerate(boundaries):
        end = starts[position + 1] if position + 1 < len(boundaries) else len(lines)
        text = "\n".join(lines[starts[position] : end]).strip()
        if text:
            segments.append(_Segment(f"{base} > {name}", text))
    return segments


def _symbol_of(line: str) -> str | None:
    """**启发式**判断一行是否是顶层符号边界，是则返回用于 ``heading_path`` 的符号名。

    SQL 先判：``CREATE TABLE users (`` 这类行也会被 C/C++ 规则误认成「返回类型 + 函数名」，
    得到 ``users`` 这种毫无信息量的符号名，因此 SQL 关键字优先。
    """
    sql = _SQL_RE.match(line)
    if sql:
        rest = re.sub(r"\s+", " ", sql.group(2)).strip()
        head = f"{sql.group(1).upper()} {rest}".rstrip("(;").strip()
        return head[:_SYMBOL_MAX_CHARS]
    for rule in _SYMBOL_RULES:
        match = rule.match(line)
        if match:
            return match.group(1)
    return None


# ------------------------------------------------------------------- 块的生产


def _emit_segment(
    nodes: list[ChunkNode], segment: _Segment, *, max_tokens: int, min_tokens: int, overlap: int
) -> None:
    """把一段代码落到 ``nodes``：超长则生成父容器 + 子块。"""
    content = segment.content.strip()
    if not content:
        return  # 不产出空的或只有空白的 content
    if count_tokens(content) <= max_tokens:
        _append(nodes, "parent", content, segment.heading_path)
        return

    pieces = _window_text(content, max_tokens, min_tokens, overlap)
    if not segment.named or len(pieces) < 2:
        # 没有可表达的「符号父容器」（或只是略微超限、尾块被并回后只剩一块）：
        # 窗口本身就是顶层块，长度仍在 1.5 倍护栏内。
        for piece in pieces:
            _append(nodes, "parent", piece, segment.heading_path)
        return

    _append(nodes, "parent", content, segment.heading_path)
    parent_index = len(nodes) - 1
    for number, piece in enumerate(pieces, start=1):
        _append(
            nodes,
            "child",
            piece,
            f"{segment.heading_path}{CHILD_SEQ_SEP}{number}",
            parent_index=parent_index,
        )


def _append(
    nodes: list[ChunkNode],
    chunk_type: str,
    content: str,
    heading_path: str,
    *,
    parent_index: int | None = None,
) -> ChunkNode:
    node = ChunkNode(
        chunk_type=chunk_type,
        seq=len(nodes) + 1,
        content=content,
        heading_path=heading_path,
        parent_index=parent_index,
    )
    nodes.append(node)
    return node


def _window_text(text: str, max_tokens: int, min_tokens: int, overlap: int) -> list[str]:
    """把一段原文按行切成带重叠的窗口（**不丢内容**，允许重叠）。

    为什么按行而不是按字符：代码的换行就是语句边界，按字符切会把一行语句劈成两半。
    只有「单行本身就超限」（压缩过的 JS、超长 SQL 语句、超长字符串）时才按字符硬切。
    """
    windows: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for line in text.splitlines():
        line_tokens = count_tokens(line) + 1  # +1 ≈ 换行符
        if line_tokens > max_tokens:  # 单行超限：硬切，且不与前后行混装
            if current:
                windows.append(current)
                current = []
                current_tokens = 0
            windows.extend([piece] for piece in _hard_split(line, max_tokens, overlap))
            continue
        if current and current_tokens + line_tokens > max_tokens:
            windows.append(current)
            current = _overlap_tail(current, overlap)
            current_tokens = sum(count_tokens(item) + 1 for item in current)
        current.append(line)
        current_tokens += line_tokens
    if current:
        windows.append(current)

    pieces = [piece for piece in ("\n".join(window).strip() for window in windows) if piece]
    pieces = _merge_tail(pieces, max_tokens, min_tokens)

    # 护栏：正常路径下块 ≤ max_tokens * 1.5（重叠前缀 + 一行超限行）；这里再兜一次，
    # 保证「单块不超过 1.5 倍」不会因 _merge_tail 或边界情形被突破。
    ceiling = int(max_tokens * MAX_BLOCK_RATIO)
    guarded: list[str] = []
    for piece in pieces:
        if count_tokens(piece) > ceiling:
            guarded.extend(_hard_split(piece, max_tokens, overlap))
        else:
            guarded.append(piece)
    return [piece for piece in guarded if piece.strip()]


def _overlap_tail(window: list[str], overlap: int) -> list[str]:
    """取窗口尾部「不超过 overlap token」的若干行，作为下一窗口的重叠前缀。"""
    if overlap <= 0:
        return []
    tail: list[str] = []
    used = 0
    for line in reversed(window):
        cost = count_tokens(line) + 1
        if cost > overlap and not tail:
            return []  # 单行就超过重叠预算：宁可不重叠，也不让新块头部被重复挤满
        if used + cost > overlap:
            break
        tail.insert(0, line)
        used += cost
    if len(tail) >= len(window) and len(tail) > 1:
        tail = tail[1:]  # 至少让出一个位置给新内容，保证切分推进
    return tail


def _merge_tail(pieces: list[str], max_tokens: int, min_tokens: int) -> list[str]:
    """把尾部的退化碎块并回前一块（避免几十 token 的碎片污染 rerank）。"""
    if len(pieces) >= 2:
        merged = f"{pieces[-2]}\n{pieces[-1]}"
        if count_tokens(pieces[-1]) < min_tokens and count_tokens(merged) <= int(
            max_tokens * MAX_BLOCK_RATIO
        ):
            pieces[-2] = merged
            pieces.pop()
    return pieces


def _hard_split(text: str, max_tokens: int, overlap: int) -> list[str]:
    """按 token 上限硬切一行超长文本（二进制搜索前缀，保留 ``overlap`` token 重叠）。"""
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = _max_prefix_end(text, start, max_tokens)
        pieces.append(text[start:end].strip())
        if end >= length:
            break
        next_start = _overlap_start(text, start, end, overlap) if overlap > 0 else end
        start = max(start + 1, next_start)
    return [piece for piece in pieces if piece]


def _max_prefix_end(text: str, start: int, max_tokens: int) -> int:
    """返回「从 ``start`` 起 token 数不超过 ``max_tokens`` 的最大结束下标」。

    搜索上界取 ``start + max_tokens * 8``：一个 token 至少覆盖 1 个字符，
    8 是 ASCII 文本 token/字符比的经验上界，超出部分不可能落在配额内。
    """
    low = start + 1
    high = min(len(text), start + max(1, max_tokens * 8))
    best = low
    while low <= high:
        middle = (low + high) // 2
        if count_tokens(text[start:middle]) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _overlap_start(text: str, start: int, end: int, overlap: int) -> int:
    """返回下一段起点：使 ``text[起点:end]`` 的 token 数不超过 ``overlap``（尽量靠后）。"""
    low = start + 1
    high = end
    best = end
    while low <= high:
        middle = (low + high) // 2
        if count_tokens(text[middle:end]) <= overlap:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _suffix_of(path: str) -> str:
    """取小写扩展名（含点）；无扩展名返回空串。"""
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""
