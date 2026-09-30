"""四层分块（`06` §3.2/§3.4、`docs/spec/20` §3）。"""

from __future__ import annotations

from ekbs.ingest.chunker import (
    CHILD_MAX_TOKENS,
    CHILD_MIN_TOKENS,
    PARENT_MAX_TOKENS,
    PARENT_MIN_TOKENS,
    chunk_markdown,
    count_tokens,
    split_sections,
)

SHORT_DOC = """# 年假管理制度

## 第一条 年假天数

员工年假天数按司龄确定：满 1 年 5 天，满 3 年 10 天。

## 第二条 申请流程

须提前 3 个工作日在系统提交申请。
"""


def _long_doc(sections: int = 5, body_chars: int = 1600) -> str:
    """每个小节的正文都足够大（≈1160 token），因此每个小节各自成为一个合规模块的父块。"""
    body = "公司制度条款内容说明。" * (body_chars // 10)
    parts = ["# 员工手册"]
    for index in range(sections):
        parts.append(f"\n## 第 {index + 1} 章 规定事项\n\n{body}\n")
    return "".join(parts)


def test_split_sections_builds_heading_path() -> None:
    sections = split_sections(SHORT_DOC)
    paths = [section.heading_path for section in sections]
    assert "年假管理制度" in paths[0]
    assert any("第一条 年假天数" in path for path in paths)


def test_short_document_produces_linked_parent_and_children() -> None:
    result = chunk_markdown(SHORT_DOC)
    assert result.parents, "至少一个父块"
    assert result.children, "至少一个子块"
    for child in result.children:
        assert child.parent_index is not None
        parent = result.nodes[child.parent_index]
        assert parent.chunk_type == "parent"
        # 子块必须落在父块文本内（父子关系可回溯）
        assert child.content.strip()[:12] in parent.content


def test_seq_is_monotonic_and_unique() -> None:
    result = chunk_markdown(_long_doc())
    seqs = [node.seq for node in result.nodes]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_long_document_respects_token_bounds() -> None:
    result = chunk_markdown(_long_doc())
    assert len(result.parents) >= 3, "长文档应切出多个父块"
    assert all(PARENT_MIN_TOKENS <= node.token_count <= PARENT_MAX_TOKENS for node in result.parents)
    assert all(node.token_count <= CHILD_MAX_TOKENS for node in result.children)
    # 每个父块最多允许一个「尾部余量」子块小于 CHILD_MIN：
    # 父块总长不足以切成两个合规子块时（例如 500 token），这在数学上无法避免。
    assert len(result.children) - len(result.parents) <= sum(
        1 for node in result.children if node.token_count >= CHILD_MIN_TOKENS
    )
    # 但绝不允许退化碎块（几十 token 的碎片会污染 rerank）
    assert all(node.token_count >= CHILD_MIN_TOKENS // 4 for node in result.children)


def test_small_section_becomes_a_small_parent() -> None:
    """小节本身不足 1024 token 时**不跨标题合并** —— 合并会破坏 heading_path 与页码归属。

    此时父块就是该小节全文（小于 ``PARENT_MIN_TOKENS``），这是有意的取舍：
    检索精度与引用可溯源优先于「父块必须够大」。
    """
    result = chunk_markdown(SHORT_DOC)
    assert result.parents
    assert all(node.token_count < PARENT_MIN_TOKENS for node in result.parents)


def test_children_are_smaller_than_or_equal_to_parents() -> None:
    result = chunk_markdown(_long_doc())
    by_index = {index: node for index, node in enumerate(result.nodes)}
    for child in result.children:
        parent = by_index[child.parent_index]
        assert child.token_count <= parent.token_count


def test_count_tokens_is_positive_and_monotonic() -> None:
    assert count_tokens("") == 0
    assert count_tokens("年假") > 0
    assert count_tokens("年假" * 10) > count_tokens("年假")


def test_page_map_annotates_sections() -> None:
    page_map = [(0, 1, 1), (40, 2, 2)]
    result = chunk_markdown(SHORT_DOC, page_map=page_map)
    assert any(node.page_from == 1 for node in result.nodes)
