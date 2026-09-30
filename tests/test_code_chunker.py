"""代码感知分块（`ekbs.ingest.code_chunker`）。

覆盖三类口径：

* Python：AST 符号边界（类 / 类方法 / 顶层函数 / 模块级代码）；
* 超长符号：父容器 + 子块（``parent_index`` 必须指向返回列表中的父块下标）；
* 其它语言与「语法错误的 Python」：**启发式**边界 + 重叠窗口兜底，且绝不抛异常。
"""

from __future__ import annotations

import pytest

from ekbs.ingest.chunker import ChunkNode, count_tokens
from ekbs.ingest.code_chunker import chunk_code

PYTHON_SOURCE = '''"""示例模块：两个类、三个顶层函数。"""

import os

MAX_RETRY = 3


class Alpha:
    """第一个类。"""

    def first(self):
        return 1

    def second(self):
        return 2


class Beta:
    def only(self):
        return 3


def top_one():
    return "one"


def top_two():
    return "two"


async def top_three():
    return "three"
'''

#: 2 个类（1 个含 2 个方法）+ 3 个顶层函数 + 1 个模块级代码块
EXPECTED_PYTHON_HEADINGS = [
    "ekbs/sample.py > <module>",
    "ekbs/sample.py > Alpha",
    "ekbs/sample.py > Alpha.first",
    "ekbs/sample.py > Alpha.second",
    "ekbs/sample.py > Beta",
    "ekbs/sample.py > Beta.only",
    "ekbs/sample.py > top_one",
    "ekbs/sample.py > top_two",
    "ekbs/sample.py > top_three",
]

BROKEN_PYTHON = """def broken(
    x
    return x


class Unclosed:
    def method(self)
        pass
"""

JUNK_SOURCE = """this is not python !!!
(((( ]]}}
"unterminated
"""

JS_SOURCE = """import { readFile } from "node:fs/promises";

export async function loadConfig(path) {
  const raw = await readFile(path, "utf8");
  return JSON.parse(raw);
}

export class Config {
  constructor(values) {
    this.values = values;
  }
}
"""

SQL_SOURCE = """CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE INDEX idx_users_name ON users (name);
"""

GO_SOURCE = """package main

func (s *Server) Start() error {
    return nil
}

func main() {
    s := &Server{}
    _ = s.Start()
}
"""


def _long_python_function(lines: int = 300) -> str:
    """人工凑一个远超 max_tokens 的函数体（每行 ≈13 token）。"""
    body = "\n".join(f"    total += compute_{index}(total) * {index}" for index in range(lines))
    return f"def giant_function(total):\n{body}\n    return total\n"


def _long_js_function(lines: int = 200) -> str:
    body = "\n".join(f"  total += compute{index}(total) * {index};" for index in range(lines))
    return f"export function giantJs(total) {{\n{body}\n  return total;\n}}\n"


def _assert_common_invariants(
    nodes: list[ChunkNode], *, path: str, max_tokens: int, require_heading_path: bool = True
) -> None:
    """所有分支都必须成立的公共口径。"""
    assert [node.seq for node in nodes] == list(range(1, len(nodes) + 1)), "seq 必须从 1 连续递增"
    container_indices = {node.parent_index for node in nodes if node.chunk_type == "child"}
    for node in nodes:
        assert node.content.strip(), f"不允许空白块：{node.heading_path!r}"
        assert node.token_count > 0, f"token_count 必须为正：{node.heading_path!r}"
        assert node.token_count == count_tokens(node.content), "token_count 必须与内容一致"
        assert node.heading_path, "heading_path 不能为空"
        if require_heading_path:
            assert node.heading_path.startswith(path), "heading_path 必须包含文件路径"
        if node.chunk_type == "child":
            assert node.parent_index is not None
            assert 0 <= node.parent_index < len(nodes)
            assert nodes[node.parent_index].chunk_type == "parent"
            assert node.heading_path.startswith(nodes[node.parent_index].heading_path + "#")
        elif node.seq - 1 not in container_indices:
            # 被切分的「符号父容器」保留全文，允许超过护栏；其余块必须 ≤ 1.5 倍
            assert node.token_count <= max_tokens * 1.5, f"超长块未被继续切：{node.heading_path!r}"


# ------------------------------------------------------------------ Python（AST）


def test_python_symbols_become_parent_chunks() -> None:
    nodes = chunk_code(PYTHON_SOURCE, path="ekbs/sample.py")

    assert len(nodes) == 9, "2 类 + 3 方法 + 3 顶层函数 + 1 模块级块"
    assert all(node.chunk_type == "parent" for node in nodes), "短符号不应产生子块"
    assert [node.heading_path for node in nodes] == EXPECTED_PYTHON_HEADINGS
    _assert_common_invariants(nodes, path="ekbs/sample.py", max_tokens=800)


def test_python_heading_path_is_path_then_symbol() -> None:
    nodes = chunk_code(PYTHON_SOURCE, path="ekbs/sample.py")

    for node in nodes:
        assert " > " in node.heading_path, "必须有「路径 > 符号」两段"
        assert node.heading_path.split(" > ", 1)[0] == "ekbs/sample.py"

    method = next(node for node in nodes if node.heading_path.endswith("Alpha.first"))
    assert method.content.startswith("def first(self)")
    assert method.parent_index is None


def test_python_module_level_code_is_aggregated() -> None:
    nodes = chunk_code(PYTHON_SOURCE, path="ekbs/sample.py")

    module = next(node for node in nodes if node.heading_path.endswith("<module>"))
    assert "import os" in module.content
    assert "MAX_RETRY = 3" in module.content
    assert module.content.startswith('"""示例模块')  # 模块 docstring 也属于模块级代码


def test_unknown_suffix_python_source_still_uses_ast() -> None:
    """后缀认不出来时先试 ast —— 代码片段字符串也能享受符号级切分。"""
    nodes = chunk_code("def solo():\n    return 1\n", path="snippet")

    assert [node.heading_path for node in nodes] == ["snippet > solo"]


def test_class_parent_contains_its_methods() -> None:
    nodes = chunk_code(PYTHON_SOURCE, path="ekbs/sample.py")

    alpha = next(node for node in nodes if node.heading_path.endswith("> Alpha"))
    assert "def first(self)" in alpha.content
    assert "def second(self)" in alpha.content


# ------------------------------------------------------------ 超长符号 → 子块


def test_oversized_function_is_split_into_children() -> None:
    source = _long_python_function()
    assert count_tokens(source) > 800, "用例前提：这个函数必须远超 max_tokens"

    nodes = chunk_code(source, path="big.py", max_tokens=200, min_tokens=20, overlap=40)
    parents = [node for node in nodes if node.chunk_type == "parent"]
    children = [node for node in nodes if node.chunk_type == "child"]

    assert len(parents) == 1, "只有超长函数本身（无模块级代码）"
    parent = parents[0]
    parent_index = nodes.index(parent)
    assert parent.heading_path == "big.py > giant_function"
    assert len(children) >= 2, "超长函数必须被切成多个子块"

    assert {child.parent_index for child in children} == {parent_index}
    assert [child.heading_path for child in children] == [
        f"big.py > giant_function#{number}" for number in range(1, len(children) + 1)
    ], "子块 heading_path 必须是「父块路径#序号」，序号从 1 起"

    _assert_common_invariants(nodes, path="big.py", max_tokens=200)


def test_children_cover_parent_content_with_overlap() -> None:
    source = _long_python_function()
    nodes = chunk_code(source, path="big.py", max_tokens=200, min_tokens=20, overlap=40)
    children = [node for node in nodes if node.chunk_type == "child"]
    joined = "\n".join(child.content for child in children)

    # 兜底窗口「不丢内容」：父块里的每一句都能在某个子块里找到
    assert all(f"compute_{index}(" in joined for index in range(300))
    # 相邻子块必须有重叠（否则切点正好落在语义中间时两边都读不懂）
    assert children[0].content.splitlines()[-1] in children[1].content


def test_oversized_heuristic_function_is_split_into_children() -> None:
    """启发式分支同样做「父容器 + 子块」，不能只靠定长窗口。"""
    nodes = chunk_code(_long_js_function(), path="src/big.js", max_tokens=150, min_tokens=20, overlap=30)
    parents = [node for node in nodes if node.chunk_type == "parent"]
    children = [node for node in nodes if node.chunk_type == "child"]

    assert len(parents) == 1
    assert parents[0].heading_path == "src/big.js > giantJs"
    assert len(children) >= 2
    assert {child.parent_index for child in children} == {nodes.index(parents[0])}
    _assert_common_invariants(nodes, path="src/big.js", max_tokens=150)


def test_short_symbols_never_produce_children() -> None:
    nodes = chunk_code(PYTHON_SOURCE, path="ekbs/sample.py", max_tokens=64, min_tokens=8, overlap=8)

    assert all(node.chunk_type == "parent" for node in nodes), "小块不切子块"
    assert all(node.token_count <= 64 for node in nodes)


# ------------------------------------------------- 语法错误 / 其它语言（启发式）


def test_syntax_error_python_falls_back_without_raising() -> None:
    nodes = chunk_code(BROKEN_PYTHON, path="bad.py")

    assert nodes, "语法错误不能让异常冒出去，也不能返回空"
    assert all(node.content.strip() for node in nodes)
    assert any(node.heading_path == "bad.py > broken" for node in nodes)
    assert any("class Unclosed" in node.content for node in nodes)
    _assert_common_invariants(nodes, path="bad.py", max_tokens=800)


def test_heuristic_fallback_covers_unrecognisable_source() -> None:
    """完全认不出边界时退化为带重叠的定长窗口，且不丢内容。"""
    nodes = chunk_code(JUNK_SOURCE, path="unknown.txt", max_tokens=16, min_tokens=4, overlap=4)

    assert nodes, "兜底路径也必须产出分块"
    assert all(node.heading_path == "unknown.txt" for node in nodes), "无符号时 heading_path 只含路径"
    assert any("not python" in node.content for node in nodes)
    joined = "\n".join(node.content for node in nodes)
    assert "unterminated" in joined, "窗口必须覆盖全文（允许重叠）"
    _assert_common_invariants(nodes, path="unknown.txt", max_tokens=16)


@pytest.mark.parametrize(
    ("source", "path", "expected_symbols"),
    [
        (JS_SOURCE, "src/config.js", ["loadConfig", "Config"]),
        (SQL_SOURCE, "db/schema.sql", ["CREATE TABLE users", "CREATE INDEX"]),
        (GO_SOURCE, "cmd/server.go", ["Start", "main"]),
    ],
)
def test_heuristic_languages_produce_named_chunks(
    source: str, path: str, expected_symbols: list[str]
) -> None:
    nodes = chunk_code(source, path=path)

    assert nodes
    assert all(node.heading_path.startswith(path) for node in nodes), "heading_path 至少含文件路径"
    for symbol in expected_symbols:
        assert any(symbol in node.heading_path for node in nodes), f"没认出符号：{symbol}"
    _assert_common_invariants(nodes, path=path, max_tokens=800)


def test_heuristic_keeps_indented_members_with_their_class() -> None:
    nodes = chunk_code(JS_SOURCE, path="src/config.js")

    config = next(node for node in nodes if node.heading_path.endswith("> Config"))
    assert "constructor(values)" in config.content, "缩进成员不单独成块，归入所属类"


# ------------------------------------------------------------------- 通用口径


def test_empty_and_blank_source_returns_empty_list() -> None:
    assert chunk_code("", path="empty.py") == []
    assert chunk_code("   \n\n\t\n", path="blank.py") == []
    assert chunk_code("\n\n", path="blank.sql") == []


def test_comment_only_python_file_is_not_dropped() -> None:
    """AST 的 body 为空（只有注释）时，文件内容仍然不能丢。"""
    nodes = chunk_code("# 只有注释\n# 第二行\n", path="c.py")

    assert nodes
    assert all(node.heading_path == "c.py" for node in nodes)
    assert "# 只有注释" in "\n".join(node.content for node in nodes)


@pytest.mark.parametrize(
    ("source", "path"),
    [("\x00abc", "x.bin"), (JUNK_SOURCE, "junk.js"), (JS_SOURCE, "src/config.js")],
)
def test_pathological_parameters_stay_bounded(source: str, path: str) -> None:
    """极端参数（窗口 4 token / 巨大重叠、非法字节）不能死循环或失控。"""
    nodes = chunk_code(source, path=path, max_tokens=4, min_tokens=1, overlap=10**6)

    assert nodes
    _assert_common_invariants(nodes, path=path, max_tokens=4)


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (PYTHON_SOURCE, "ekbs/sample.py"),
        (BROKEN_PYTHON, "bad.py"),
        (JUNK_SOURCE, "unknown.txt"),
        (JS_SOURCE, "src/config.js"),
        (SQL_SOURCE, "db/schema.sql"),
        (GO_SOURCE, "cmd/server.go"),
        (_long_python_function(), "big.py"),
    ],
)
def test_global_invariants(source: str, path: str) -> None:
    nodes = chunk_code(source, path=path)

    assert nodes
    _assert_common_invariants(nodes, path=path, max_tokens=800)
    # 符号块必须是可检索的：路径 + 符号名（模块块为 <module>）
    assert all(node.heading_path.split(" > ", 1)[0] == path for node in nodes)
