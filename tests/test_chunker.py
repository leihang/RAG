"""四层分块（`06` §3.2/§3.4、`docs/spec/20` §3）。"""

from __future__ import annotations

from ekbs.ingest.chunker import (
    CHILD_MAX_TOKENS,
    CHILD_MIN_TOKENS,
    OVERLAP_MIN_RATIO,
    PARENT_MAX_TOKENS,
    PARENT_MIN_TOKENS,
    chunk_markdown,
    content_coverage,
    count_tokens,
    ensure_heading_path,
    major_heading_path,
    pack_major_sections,
    split_sections,
)

SHORT_DOC = """# 年假管理制度

## 第一条 年假天数

员工年假天数按司龄确定：满 1 年 5 天，满 3 年 10 天。

## 第二条 申请流程

须提前 3 个工作日在系统提交申请。
"""

#: 无 Markdown # 标题、纯条款行 —— 制度 PDF / 纯文本常见形态
CLAUSE_PLAIN = """年假管理制度

第一条 年假天数
员工年假天数按司龄确定：满一年五天，满三年十天。另有补充说明若干。

第二条 申请流程
须提前三个工作日在系统提交申请，经主管审批后方可休假。
"""


def _long_doc(sections: int = 5, body_tokens: int = 1200) -> str:
    """每个小节的正文**按 token 造**（≈``body_tokens``），因此每个小节各自成为一个合规模块的父块。

    为什么按 token 而不是按字符：token 数取决于**当前生效的计数口径**（`02` TC-INDEX-004
    校准的那个口径）。按字符造会把夹具与该口径绑死 —— 换词表后同样的字符数算出不同 token，
    测试就会假失败（实测：换 Qwen 词表后 1600 字符只剩 960 token < `PARENT_MIN_TOKENS`）。
    """
    if body_tokens <= 0:
        raise ValueError("body_tokens 必须为正")
    unit = "公司制度条款内容说明。"
    body = ""
    while count_tokens(body) < body_tokens:
        body += unit
    parts = ["# 员工手册"]
    for index in range(sections):
        parts.append(f"\n## 第 {index + 1} 章 规定事项\n\n{body}\n")
    return "".join(parts)


def test_split_sections_builds_heading_path() -> None:
    sections = split_sections(SHORT_DOC)
    paths = [section.heading_path for section in sections]
    assert "年假管理制度" in paths[0]
    assert any("第一条 年假天数" in path for path in paths)


def test_clause_lines_become_heading_path() -> None:
    """无 # 时，``第X条`` 行仍应成为 heading_path（条款感知）。"""
    sections = split_sections(CLAUSE_PLAIN)
    paths = [s.heading_path for s in sections]
    assert any("第一条" in p for p in paths)
    assert any("第二条" in p for p in paths)
    result = chunk_markdown(CLAUSE_PLAIN, doc_title="年假管理制度")
    assert all(n.heading_path.strip() for n in result.nodes)
    assert any("第一条" in n.heading_path for n in result.parents)


def test_heading_path_never_empty_with_doc_title() -> None:
    result = chunk_markdown("没有任何标题的一段正文。", doc_title="差旅费管理办法")
    assert result.parents
    assert all(n.heading_path == "差旅费管理办法" for n in result.nodes)
    assert ensure_heading_path("", doc_title="") == "文档"


def test_short_slide_page_skips_identical_child() -> None:
    """PPT 短页（远小于 child_min）不得 parent+child 同文双索引。"""
    md = (
        "# 第 4 页\n\n"
        "1.能耗管理1-7用电量（工程部）\n"
        "总用电量累计:2026比2025增长1.76%。\n"
    )
    result = chunk_markdown(md, doc_title="工程汇报")
    assert result.parents
    assert not result.children
    # 父块是叶子：没有 child 指向它
    assert all(n.parent_index is None for n in result.nodes)


def test_short_document_leaves_parent_as_leaf() -> None:
    """短节只能切出与父块相同的一块时：不建 child，父块本身作叶子入向量。"""
    result = chunk_markdown(SHORT_DOC)
    assert result.parents, "至少一个父块"
    assert not result.children, "短节不应再造一份等同正文的 child"
    for parent in result.parents:
        assert parent.parent_index is None


def test_long_parent_still_splits_into_children() -> None:
    """够长的父块仍切出多个 child，并挂上 parent_index。"""
    result = chunk_markdown(_long_doc(sections=1, body_tokens=2400))
    assert result.children, "长小节应有子块"
    for child in result.children:
        assert child.parent_index is not None
        parent = result.nodes[child.parent_index]
        assert parent.chunk_type == "parent"
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
    # 相邻 overlap 会让子块略超 CHILD_MAX（≤ max + 12% 预算）
    child_ceiling = CHILD_MAX_TOKENS + int(CHILD_MAX_TOKENS * OVERLAP_MIN_RATIO) + 32
    assert all(node.token_count <= child_ceiling for node in result.children)
    # 每个父块最多允许一个「尾部余量」子块小于 CHILD_MIN：
    # 父块总长不足以切成两个合规子块时（例如 500 token），这在数学上无法避免。
    assert len(result.children) - len(result.parents) <= sum(
        1 for node in result.children if node.token_count >= CHILD_MIN_TOKENS
    )
    # 但绝不允许退化碎块（几十 token 的碎片会污染 rerank）
    assert all(node.token_count >= CHILD_MIN_TOKENS // 4 for node in result.children)


def test_adjacent_children_share_overlap() -> None:
    """同一父块下相邻子块必须有 12%+ 尾部重叠（TC-INDEX-003）。"""
    result = chunk_markdown(_long_doc(sections=1, body_tokens=2400))
    by_parent: dict[int, list] = {}
    for child in result.children:
        assert child.parent_index is not None
        by_parent.setdefault(child.parent_index, []).append(child)
    multi = next((kids for kids in by_parent.values() if len(kids) >= 2), None)
    assert multi is not None, "长小节应切出 ≥2 个子块以验证 overlap"
    prev, cur = multi[0], multi[1]
    # cur 应以 prev 尾部为前缀（或至少共享一段尾部文本）
    overlap_budget = int(CHILD_MAX_TOKENS * OVERLAP_MIN_RATIO)
    tail = prev.content[-max(40, overlap_budget) :]
    assert any(
        piece and piece in cur.content
        for piece in (tail, prev.content[-60:], prev.content.split("。")[-2] if "。" in prev.content else "")
    ), f"相邻子块未见 overlap：prev_tail={tail[:40]!r} cur_head={cur.content[:80]!r}"


def test_small_section_becomes_a_small_parent() -> None:
    """小节本身不足 1024 token 时**不跨中文条款/Markdown 标题合并**。

    此时父块就是该小节全文（小于 ``PARENT_MIN_TOKENS``），这是有意的取舍：
    检索精度与引用可溯源优先于「父块必须够大」。
    点分嵌套小节（``4.1``）另见 ``test_pack_major_dotted_sections``。
    """
    result = chunk_markdown(SHORT_DOC)
    assert result.parents
    assert all(node.token_count < PARENT_MIN_TOKENS for node in result.parents)
    # 「第一条 / 第二条」不得被大节优先误合并
    assert len(result.parents) >= 2


def test_major_heading_path_strips_nested_dotted() -> None:
    assert major_heading_path("4． 制度 > 4.1 接班") == "4． 制度"
    assert major_heading_path("交接班 > 4． 制度 > 4.6 重要事项") == "交接班 > 4． 制度"
    assert major_heading_path("1． 目的") == "1． 目的"
    assert major_heading_path("4.1 孤儿条款") == "4．"


def test_pack_major_dotted_sections() -> None:
    """4.1 / 4.2 各自成块，不按长度并进「4． 制度」。"""
    long_41 = "4.1 " + ("接班人应提前到岗并完成检查。" * 8)
    md = f"""# 交接班

4． 制度
{long_41}
4.2 接班人员未能到岗，值班人员必须坚持岗位。
4.3 接班人员酗酒时交班人员应拒绝交班。
4.4 设备交班人向接班人介绍各系统运行状态。
"""
    sections = pack_major_sections(split_sections(md))
    rule = [s for s in sections if "4." in s.heading_path or "4．" in s.heading_path]
    assert len(rule) >= 4, [s.heading_path for s in rule]
    result = chunk_markdown(md, doc_title="交接班管理制度")
    leaves = [n for n in result.nodes if n.chunk_type in {"parent", "child"}]
    assert any("4.1" in n.heading_path for n in leaves)
    assert any("4.4" in n.heading_path for n in leaves)
    for node in leaves:
        text = node.content
        assert not ("接班人应提前到岗" in text and "介绍各系统运行状态" in text)


def test_children_are_smaller_than_or_equal_to_parents() -> None:
    result = chunk_markdown(_long_doc())
    by_index = {index: node for index, node in enumerate(result.nodes)}
    slack = int(CHILD_MAX_TOKENS * OVERLAP_MIN_RATIO) + 32
    for child in result.children:
        parent = by_index[child.parent_index]
        assert child.token_count <= parent.token_count + slack


def test_count_tokens_is_positive_and_monotonic() -> None:
    assert count_tokens("") == 0
    assert count_tokens("年假") > 0
    assert count_tokens("年假" * 10) > count_tokens("年假")


def test_page_map_annotates_sections() -> None:
    page_map = [(0, 1, 1), (40, 2, 2)]
    result = chunk_markdown(SHORT_DOC, page_map=page_map)
    assert any(node.page_from == 1 for node in result.nodes)


def test_pages_for_uses_last_marker_not_in_range() -> None:
    """页标在小节起点之前时，仍应落到该页 —— 不能 fallback 成第 1 页。"""
    from ekbs.ingest.chunker import _pages_for

    page_map = [(0, 1, 1), (100, 2, 2), (200, 3, 3)]
    # 小节完全落在第 2 页正文内（起点在页界之后）
    assert _pages_for(101, 150, page_map) == (2, 2)
    # 跨页：从第 2 页写到第 3 页
    assert _pages_for(150, 250, page_map) == (2, 3)
    # 第 1 页
    assert _pages_for(0, 50, page_map) == (1, 1)
    # 空 page_map
    assert _pages_for(0, 10, None) == (None, None)


def test_chunk_markdown_does_not_collapse_later_pages_to_page_one() -> None:
    """回归开班典礼：后页小节不得因页标不在区间内而被标成 page_from=1。"""
    # 模拟三页：每页一段短正文 + 标题
    md = (
        "# 开班典礼\n\n封面标题\n\n"
        "# 导师介绍\n\n前知乎教育业务负责人\n\n"
        "# 几种典型企业类别\n\n创业型公司与一人公司\n"
    )
    # 页界刻意落在标题行之后，复现「小节 current_start > 页标 offset」
    page_map = [
        (0, 1, 1),
        (md.index("# 导师介绍"), 2, 2),
        (md.index("# 几种典型企业类别"), 3, 3),
    ]
    result = chunk_markdown(md, page_map=page_map, doc_title="开班典礼")
    by_heading = {n.heading_path: n for n in result.parents}
    assert by_heading["开班典礼"].page_from == 1
    assert by_heading["导师介绍"].page_from == 2
    assert by_heading["几种典型企业类别"].page_from == 3
    assert all(n.page_from != 1 or "开班典礼" in n.heading_path for n in result.parents)


# ---------------------------------------------------------------- 零丢失（2026-10-02 实测事故）


def test_single_line_clauses_are_not_swallowed() -> None:
    """``第X条 …`` 整条就是内容时，**必须也进正文**。

    实测事故：交接班制度 22 段里 11 段丢失（643 字符只剩 206），而空页率/乱码率/空 heading
    这些闸门**全是 PASS** —— 因为分块器把 `第X条 …` / `4.1 …` 整行当标题，连续多条时
    同级旧标题被弹出，**除最后一条外整行文字谁都不产出**。修复前这种三行文档产出 **0 个块**。
    """
    md = (
        "第一条 员工年假天数按司龄确定：满一年五天。\n"
        "第二条 申请须提前三个工作日提交。\n"
        "第三条 未休年假折算标准按月工资计算。\n"
    )
    result = chunk_markdown(md)
    assert result.nodes, "单行成条的制度必须产出块（修复前是 0 个块）"
    ratio, lost = content_coverage(md, result.nodes)
    assert ratio == 1.0 and not lost
    joined = "\n".join(node.content for node in result.nodes)
    assert "满一年五天" in joined and "三个工作日" in joined and "按月工资计算" in joined


def test_dotted_clauses_are_not_swallowed() -> None:
    """``4.1 …`` / ``4． …`` / ``1. 目的`` 都要切节，且长条款行不能丢正文。"""
    md = (
        "4． 制度\n"
        "4.1 值班人员应认真遵守公司的各项规范制度，接班人应提前15分钟到达现场。\n"
        "4.2 接班人员未能到岗，值班人员必须坚持岗位并汇报。\n"
    )
    result = chunk_markdown(md)
    ratio, lost = content_coverage(md, result.nodes)
    assert ratio == 1.0, f"覆盖率 {ratio:.0%}，丢失 {lost[:2]}"
    joined = "\n".join(node.content for node in result.nodes)
    assert "接班人应提前15分钟" in joined and "必须坚持岗位并汇报" in joined
    paths = {node.heading_path for node in result.nodes}
    assert any("4．" in p and "制度" in p for p in paths)
    # 短节合并后 4.1 可能落在正文而非 heading_path，词面仍须可检索
    assert any("4.1" in p for p in paths) or "4.1" in joined


def test_chapter_and_fullwidth_dotted_headings() -> None:
    """一级半角/全角编号要切章；子节挂在对应章下，且不被「文件信息」污染。"""
    md = (
        "## 文件信息\n"
        "文件编号：XJWY-GC-02\n"
        "\n"
        "1. 目的\n"
        "满足公共设备管理需要。\n"
        "2．范围\n"
        "适用于工程中心。\n"
        "4．方法和过程控制\n"
        "4.1 设备编号组成\n"
        "编号由四段组成。\n"
        "4.3 设备编号规则\n"
        "按类别编码。\n"
    )
    result = chunk_markdown(md)
    ratio, lost = content_coverage(md, result.nodes)
    assert ratio == 1.0 and not lost, f"覆盖率 {ratio:.0%}，丢失 {lost[:3]}"
    paths = [node.heading_path for node in result.nodes]
    assert any(p == "文件信息" or p.endswith("文件信息") for p in paths)
    assert any(p == "1. 目的" for p in paths)
    assert any(p == "2． 范围" for p in paths)
    # 4.1 / 4.3 各自留在标题路径里，不并进上一节
    assert any("4.1" in p for p in paths)
    assert any("4.3" in p for p in paths)
    joined = "\n".join(f"{n.heading_path}\n{n.content}" for n in result.nodes)
    assert "4.1" in joined and "4.3" in joined
    assert not any(p.startswith("文件信息 >") for p in paths if "4." in p or "目的" in p)


def test_short_clause_title_keeps_behaviour_without_duplication() -> None:
    """短标题（如 ``第一条 年假天数``）保持"只当标题" —— 不把标题文字复制进正文。

    判据是"这一行本身像不像内容"（带句读且 ≥12 字，或 ≥40 字）。短标题既省 token，
    也避免了 `heading_path` 与 `content` 无谓重复。
    """
    md = "第一条 年假天数\n员工年假天数按司龄确定：满一年五天。\n"
    result = chunk_markdown(md)
    assert result.nodes[0].content.strip() == "员工年假天数按司龄确定：满一年五天。"
    assert "第一条 年假天数" in result.nodes[0].heading_path


def test_content_coverage_flags_missing_substantive_lines() -> None:
    """覆盖率函数要把"丢了且像正文"的行报出来（页眉/页码这类不该报）。"""
    md = "一、值班要求\n接班人应提前十五分钟到达现场并完成检查。\n"
    result = chunk_markdown(md)
    ratio, lost = content_coverage(md, result.nodes)
    assert ratio == 1.0 and not lost

    ratio2, lost2 = content_coverage(
        md + "另一段必须在库里的实质内容，含句读。\n", result.nodes
    )
    assert ratio2 < 1.0
    assert any("另一段必须在库里" in line for line in lost2)


def test_bodyless_headings_still_land_somewhere() -> None:
    """**没有正文的标题**也必须落库（否则被同级标题顶掉就永久消失）。

    实测：`6.1 《设施设备台账/统计表》` 是文档最后一行、`### 设备分组设置` 后面直接跟
    另一个同级标题 —— 这类标题的文字只存在于 heading_stack 里，`pop()` 之后就查不到了。

    同时约束**不要过度产出**：H1（如 `# 员工手册`）会被后续每个块的 heading_path 带上，
    就不该再单独产一个碎片块。
    """
    md = (
        "## 项目背景\n"
        "系统建设说明。\n"
        "### 设备分组设置\n"
        "### 数据查看及告警消除\n"
    )
    result = chunk_markdown(md)
    ratio, lost = content_coverage(md, result.nodes)
    assert not lost, f"仍有丢失：{lost}"
    joined = "\n".join(node.content + node.heading_path for node in result.nodes)
    assert "设备分组设置" in joined and "数据查看及告警消除" in joined

    long_doc = chunk_markdown("# 员工手册\n## 第 1 章 规定事项\n" + "公司制度条款内容说明。" * 30 + "\n")
    assert all(
        node.content.strip() != "# 员工手册" for node in long_doc.nodes
    ), "H1 已被后续 heading_path 覆盖，不该再单独成块（避免碎片块）"
