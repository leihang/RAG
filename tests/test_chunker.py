"""四层分块（`06` §3.2/§3.4、`docs/spec/20` §3）。"""

from __future__ import annotations

from collections import Counter

import pytest

from ekbs.ingest.chunker import (
    CHILD_MAX_TOKENS,
    CHILD_MIN_TOKENS,
    OVERLAP_MIN_RATIO,
    PARENT_MAX_TOKENS,
    PARENT_MIN_TOKENS,
    TABLE_CONTAINER_MAX_TOKENS,
    TABLE_LEAF_TOKENS,
    TableInput,
    chunk_markdown,
    content_coverage,
    count_tokens,
    ensure_heading_path,
    major_heading_path,
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
    # 检索单元是 child；聚合后块标题是**块起始**那条条款，其余条款标题保留在正文里
    assert result.children
    assert any("第一条" in n.content for n in result.children)
    assert any("第二条" in n.content for n in result.children)


def test_numbered_clause_with_inline_body_keeps_heading_short() -> None:
    """「编号 + 正文同行」的条款（如 ``5.4 主管人员作业前需…``）标题只保留编号。

    实测 GC-13：5.1~5.8 全是「编号+正文写在同一行」，旧实现把整行当标题，
    ``heading_path`` 变成一整段话，检索标题不可读；正文本身必须完整保留。
    """
    long_body = (
        "主管人员作业前需充分识别现场环境，对危险源进行提前排除并进行作业场地的围护标识，"
        "并向现场作业人员进行安全强调、作业环境、作业范围说明。"
    )
    doc = f"5． 方法和过程\n5.4 {long_body}\n5.5 器械要求:拆除施工严禁使用风镐。\n"
    sections = split_sections(doc)
    paths = [s.heading_path for s in sections]
    assert any(path.endswith("> 5.4") for path in paths), paths
    assert any(path.endswith("> 5.5") for path in paths), paths
    assert all(len(path) < 60 for path in paths), paths
    # 整行正文仍在（编号+正文同行时不能只留编号）
    assert any(long_body in s.text for s in sections)



def test_heading_path_never_empty_with_doc_title() -> None:
    result = chunk_markdown("没有任何标题的一段正文。", doc_title="差旅费管理办法")
    assert result.parents
    assert all(n.heading_path == "差旅费管理办法" for n in result.nodes)
    assert ensure_heading_path("", doc_title="") == "文档"


def test_short_slide_page_child_does_not_duplicate_index() -> None:
    """PPT 短页（远小于 child_min）：一个父亲容器 + 一个条款子块。

    双索引由「叶子块才是检索单元」（`ekbs/ingest/retrievable.py`）保证：
    父块被 child 覆盖 → 父块不进向量/FTS。所以这里不再靠「跳过 child」防重复。
    """
    md = (
        "# 第 4 页\n\n"
        "1.能耗管理1-7用电量（工程部）\n"
        "总用电量累计:2026比2025增长1.76%。\n"
    )
    result = chunk_markdown(md, doc_title="工程汇报")
    assert result.parents
    parents = [n for n in result.nodes if n.chunk_type == "parent"]
    children = result.children
    assert len(parents) == 1 and len(children) == 1
    assert children[0].parent_index == 0
    # 父容器覆盖子块（spec §3.1），但父块本身不是检索单元
    assert children[0].content.strip() in parents[0].content


def test_short_document_children_are_size_based() -> None:
    """短文档：小条款**按 token 聚合**成一个 child，父块一个覆盖它。

    以前按标题切 → 每条条款一个 10~30 token 的块，父块约等于子块、父子关系失效；
    现在子块聚合到 ``child_min`` 才收口，父块再按 ``parent_min`` 装箱。
    """
    result = chunk_markdown(SHORT_DOC)
    parents = [n for n in result.nodes if n.chunk_type == "parent"]
    children = result.children
    assert len(parents) == 1 and len(children) == 1
    assert parents[0].parent_index is None
    assert children[0].parent_index == 0
    assert "第一条" in children[0].content and "第二条" in children[0].content
    assert children[0].content.strip() in parents[0].content
    assert parents[0].token_count >= children[0].token_count



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


def test_small_sections_are_aggregated_not_heading_fragments() -> None:
    """小节短于 ``child_min`` 时**跨标题聚合**，不再"一个标题一个碎块"。

    实测（doc 196）：按标题切出 33 个 6~120 token 的块，父块 11~648、子块 6~101，
    父子块尺寸同一量级 → 父块扩展等于没扩展。
    """
    md = (
        "# 手册\n\n"
        "## 4. 制度\n\n"
        "4.1 值班人员应提前十五分钟到岗并完成交接检查。\n"
        "4.2 接班人员未按时到岗时，值班人员必须坚持岗位并上报。\n"
        "4.3 接班记录应在当班结束后立即填写并签字确认。\n"
    )
    result = chunk_markdown(md)
    parents = [n for n in result.nodes if n.chunk_type == "parent"]
    assert len(parents) == 1
    children = result.children
    assert len(children) == 1, "三条短条款应聚合成一个子块"
    assert all(k in children[0].content for k in ("4.1", "4.2", "4.3"))



def test_major_heading_path_strips_nested_dotted() -> None:
    assert major_heading_path("4． 制度 > 4.1 接班") == "4． 制度"
    assert major_heading_path("交接班 > 4． 制度 > 4.6 重要事项") == "交接班 > 4． 制度"
    assert major_heading_path("1． 目的") == "1． 目的"
    assert major_heading_path("4.1 孤儿条款") == "4．"


def test_long_clause_splits_but_short_ones_aggregate() -> None:
    """长条款在条内切开，短条款聚合成块 —— 切点仍落在条款边界，不拆行。"""
    long_41 = "4.1 " + ("接班人应提前到岗并完成检查。" * 30)
    md = f"""# 交接班

4． 制度
{long_41}
4.2 接班人员未能到岗，值班人员必须坚持岗位。
4.3 接班人员酗酒时交班人员应拒绝交班。
4.4 设备交班人向接班人介绍各系统运行状态。
"""
    result = chunk_markdown(md, doc_title="交接班管理制度")
    children = result.children
    assert len(children) >= 2, "长条款应切块、短条款聚合"
    joined = "\n".join(child.content for child in children)
    assert "介绍各系统运行状态" in joined, "短条款不得因聚合而丢字"
    ceiling = CHILD_MAX_TOKENS + int(CHILD_MAX_TOKENS * OVERLAP_MIN_RATIO) + 32
    assert all(child.token_count <= ceiling for child in children)
    assert all(child.heading_path for child in children)



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
    """回归开班典礼：后页内容不得被标成 page_from=1。"""
    # 每页正文都超过 child_min，保证按页形成独立子块
    md = (
        "# 开班典礼\n\n封面标题：开班典礼\n\n" + "封面介绍内容。" * 60 + "\n\n"
        "# 导师介绍\n\n导师背景正文\n\n" + "导师背景说明。" * 60 + "\n\n"
        "# 几种典型企业类别\n\n企业类别正文\n\n" + "企业类别说明。" * 60 + "\n"
    )
    page_map = [
        (0, 1, 1),
        (md.index("# 导师介绍"), 2, 2),
        (md.index("# 几种典型企业类别"), 3, 3),
    ]
    result = chunk_markdown(md, page_map=page_map, doc_title="开班典礼")

    def page_of(marker: str) -> int | None:
        node = next(n for n in result.children if marker in n.content)
        return node.page_from

    assert page_of("封面介绍内容") == 1
    assert page_of("导师背景说明") == 2
    assert page_of("企业类别说明") == 3
    assert all(
        n.page_from != 1 or "封面" in n.content or "开班典礼" in n.heading_path
        for n in result.children
    )



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
    # 聚合块里保留标题骨架：每个标题行都还能在正文里看到（否则引用不了）
    joined = "\n".join(f"{n.heading_path}\n{n.content}" for n in result.nodes)
    for heading in ("1. 目的", "2． 范围", "4． 方法和过程控制", "4.1", "4.3"):
        assert heading in joined, f"{heading} 在聚合块里丢了"
    # 「文件信息」不得污染其它章的小节标题（章节路径不许跨章拼接）
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


def test_placeholder_only_sections_merge_into_next() -> None:
    """``[表格 1]`` 这类空壳节不单独成块（跨文档重复、命中后没有任何信息）。"""
    md = (
        "# 制度\n\n"
        "## 6．质量记录\n\n[表格 1]\n\n"
        "## 7．附则\n\n" + "本制度自发布之日起执行。" * 10 + "\n\n"
    )
    result = chunk_markdown(md, doc_title="设备编号作业指导书")

    assert result.parents
    paths = [node.heading_path for node in result.parents]
    assert not any("[表格 1]" in path for path in paths)
    # 占位符并进正文，原文一行不丢
    joined = "\n".join(node.content for node in result.parents)
    assert "[表格 1]" in joined and "6．质量记录" in joined


def test_short_clauses_are_aggregated_with_heading_skeleton() -> None:
    """短条款聚合成块，但**每条条款的标题行仍保留在块内容里**（引用可溯源）。"""
    md = (
        "# 交接班\n\n"
        "4.2 接班人员未能到岗，值班人员必须坚持岗位。\n"
        "4.3 接班人员酗酒时交班人员应拒绝交班。\n"
        "4.4 设备交班人向接班人介绍各系统运行状态。\n"
    )
    result = chunk_markdown(md, doc_title="交接班管理制度")
    children = result.children
    assert len(children) == 1, "三条短条款应聚合"
    for clause in ("4.2", "4.3", "4.4"):
        assert clause in children[0].content, f"{clause} 的标题行丢了"



def test_table_heading_carries_doc_title() -> None:
    """表名块要带文档名 —— 「表 1」这种名字跨文档完全一样，靠文档名区分。"""
    result = chunk_markdown(
        "# 制度\n\n正文说明。\n",
        doc_title="月度履约评估表",
        tables=[TableInput(body="| 部门 | 得分 |\n| --- | --- |\n| 工程部 | 90 |", table_id="1")],
    )
    table_nodes = [node for node in result.nodes if node.chunk_type == "table"]

    assert table_nodes
    assert all("月度履约评估表" in node.heading_path for node in table_nodes)


# --- 父容器 / 子块结构（2026-11 修「0 child + 父块碎片化」）---------------------

def test_parents_are_packed_by_token_target() -> None:
    """父块按 token 目标装箱（可跨章）：父子块尺寸必须拉开量级。"""
    body = "公司制度条款内容说明。" * 60  # 每节 ≈ 360 token
    md = "# 员工手册\n\n" + "".join(
        f"## 第 {i} 章 规定事项\n\n{body}\n\n" for i in range(1, 6)
    )
    result = chunk_markdown(md)
    parents = [n for n in result.nodes if n.chunk_type == "parent"]
    children = result.children
    assert parents and children
    assert all(p.token_count <= PARENT_MAX_TOKENS for p in parents)
    assert any(p.token_count >= PARENT_MIN_TOKENS for p in parents), "父块要装到 parent_min"
    for child in children:
        assert child.parent_index is not None
        owner = result.nodes[child.parent_index]
        assert owner.chunk_type == "parent"
        assert child.token_count <= owner.token_count
    # 子块不再是 10~60 token 的标题碎片
    assert all(c.token_count >= CHILD_MIN_TOKENS // 2 for c in children)



def test_table_chunk_carries_section_heading_and_parent() -> None:
    """表块必须带所在章节的 heading_path，并挂到同一章的父容器上（spec §3.4）。"""
    md = (
        "# 能耗管理作业指导书\n\n"
        "## 5.2 能耗计划管理\n\n"
        "每年 12 月 30 日前提报年度计划。\n"
        "[表格 3]\n\n"
        "## 5.3 能耗分析管理\n\n"
        "每月 8 日前完成分析。\n"
    )
    result = chunk_markdown(
        md,
        doc_title="能耗管理作业指导书.docx",
        tables=[
            TableInput(
                body="| 级别 | 工作内容 |\n| --- | --- |\n| A | 能耗普查 |",
                table_id="3",
            )
        ],
    )
    tables = [n for n in result.nodes if n.chunk_type == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert "5.2 能耗计划管理" in table.heading_path, table.heading_path
    assert "能耗管理作业指导书.docx" in table.heading_path
    assert table.parent_index is not None, "表块必须挂到父容器（parent_id 不能是 NULL）"
    assert result.nodes[table.parent_index].chunk_type == "parent"


def test_table_row_chunks_only_when_table_exceeds_budget() -> None:
    """``table_row_chunks`` 只对超预算的大表生效；小表整表一块（spec §3.4）。

    表格来源是 **.xlsx**（一行=一条记录）：行级分块只对电子表格成立，见
    ``test_word_table_ignores_table_row_chunks_and_uses_row_groups``。
    """
    small = "| 项目 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |"
    small_result = chunk_markdown(
        "# 制度\n\n正文。\n",
        doc_title="小表",
        tables=[TableInput(body=small, table_id="1")],
        source_path="小表.xlsx",
        table_row_chunks=True,
    )
    small_tables = [n for n in small_result.nodes if n.chunk_type == "table"]
    assert len(small_tables) == 1, "小表不该逐行切块（三行样板会占掉 40% token）"
    assert "表名: 表 1" in small_tables[0].content

    rows = "\n".join(f"| 项目{i} | 数值{i} |" for i in range(300))
    big = f"| 项目 | 值 |\n| --- | --- |\n{rows}"
    big_result = chunk_markdown(
        "# 制度\n\n正文。\n",
        doc_title="大表",
        tables=[TableInput(body=big, table_id="2")],
        source_path="大表.xlsx",
        table_row_chunks=True,
    )
    big_tables = [n for n in big_result.nodes if n.chunk_type == "table"]
    assert len(big_tables) > 1, "超预算的大表才按行出块"
    # 超限表的容器分段带标记行，不再是裸「表名:」开头；叶子/分段的正文里都必须有表名+表头
    assert all("表名:" in node.content for node in big_tables), "每段/每叶都要自解释（表名+表头）"


def test_placeholder_merge_uses_leaf_heading_not_breadcrumb() -> None:
    """空壳节并入下一节时只带**最后一级标题**，不把整条 heading_path 写进正文。"""
    md = (
        "# 制度\n\n"
        "## 5. 工作程序和要求\n\n"
        "### 5.3.2 能耗录入及能耗报告\n\n"
        "[表格 5]\n\n"
        "### 5.3.3 能耗分析\n\n"
        "每月 8 日前完成分析。\n"
    )
    result = chunk_markdown(
        md,
        doc_title="能耗管理",
        tables=[TableInput(body="| 项目 | 值 |\n| --- | --- |\n| a | 1 |", table_id="5")],
    )
    joined = "\n".join(node.content for node in result.nodes)
    assert "5. 工作程序和要求 > 5.3.2 能耗录入及能耗报告" not in joined, "整条面包屑不得进正文"
    assert "5.3.2 能耗录入及能耗报告" in joined, "最后一级标题仍要保留（限定词不能丢）"


def test_small_table_folds_surrounding_prose_into_leaf() -> None:
    """小表：上下文（段落标题名 + 上方临近文字 + 下方结论）折进叶子，不再裸表一块。"""
    md = (
        "# 能耗管理作业指导书\n\n"
        "## 5.2 能耗计划管理\n\n"
        "每年 12 月 30 日前提报年度计划。\n"
        "[表格 3]\n"
        "注：节假日执行时间另行通知。\n\n"
        "## 5.3 能耗分析管理\n\n"
        "每月 8 日前完成分析。\n"
    )
    result = chunk_markdown(
        md,
        doc_title="能耗管理作业指导书.docx",
        tables=[TableInput(body="| 级别 | 工作内容 |\n| --- | --- |\n| A | 能耗普查 |", table_id="3")],
    )
    tables = [n for n in result.nodes if n.chunk_type == "table"]
    assert len(tables) == 1, "小表应整表一块（不另设父容器）"
    table = tables[0]
    assert "每年 12 月 30 日前提报年度计划" in table.content, "上方临近文字应折进叶子"
    assert "注：节假日执行时间另行通知" in table.content, "下方结论应折进叶子"
    assert "5.2 能耗计划管理" in table.content, "段落标题名应折进叶子（限定词不能丢）"
    # 只取最后一级标题，不把整条面包屑写进正文（面包屑泄漏回归）
    assert "能耗管理作业指导书 > 5.2" not in table.content


def test_large_table_gets_container_parent_and_row_leaves() -> None:
    """大表：父容器（上文 + 整表 + 下文）+ 行/组叶子。

    300 行 xlsx 的表格父容器远超 2048 → 必须按行**分段**（每段 ≤2048），叶子挂到
    各自分段上。上下文（段落标题/上文/下文）只在首段/末段出现一次，不重复。
    """
    rows = "\n".join(f"| 项目{i} | 数值{i} |" for i in range(300))
    big = f"| 项目 | 值 |\n| --- | --- |\n{rows}"
    md = (
        "# 制度\n\n"
        "## 6．质量记录\n\n"
        "下表列出全部质量记录。\n"
        "[表格 2]\n"
        "以上记录保存三年。\n"
    )
    result = chunk_markdown(
        md,
        doc_title="设备编号作业指导书",
        tables=[TableInput(body=big, table_id="2")],
        source_path="设备清单.xlsx",
        table_row_chunks=True,
    )
    segments, leaves = _table_segments_and_leaves(result)
    assert len(segments) > 1, "300 行的整表父容器 > 2048，必须分段"
    assert all(node.token_count <= TABLE_CONTAINER_MAX_TOKENS for node in segments)
    assert "下表列出全部质量记录" in segments[0].content, "上方临近文字进首段"
    assert "以上记录保存三年" in segments[-1].content, "下方结论进末段"
    assert "6．质量记录" in segments[0].content, "段落标题名进首段"
    assert len(leaves) > 1, "大表应按行出叶子"
    assert all(n.content.startswith("表名:") for n in leaves)
    assert all(
        result.nodes[n.parent_index].table_group == "t2" for n in leaves
    ), "每个叶子的父都必须是本表的分段"


def test_medium_table_splits_into_row_group_leaves_at_384_tokens() -> None:
    """整表超过 ``TABLE_LEAF_TOKENS``(384) 就拆「表格父容器 + 行组叶子」。

    实测 GC-12「表1-年度能耗管理计划编制及实施要求」（920 token / 十几行）整表一个
    向量时，查「A级能耗普查」检索不中 —— 内容被其他行稀释。这条钉住：
    * 出了表格父容器（整表 + 上下文，不嵌入）；
    * 叶子按行组拆、每叶 ≤384 + 单行余量，且每叶重复表头（自解释）。
    """
    rows = "\n".join(
        f"| 级别{chr(65 + i % 6)} | 工作内容{i}与年度能耗管理计划相关的说明文字 | 时间节点{i} | 责任人{i} |"
        for i in range(20)
    )
    medium = f"| 级别 | 工作内容 | 时间节点 | 责任人 |\n| --- | --- | --- | --- |\n{rows}"
    result = chunk_markdown(
        "# 制度\n\n## 5. 工作程序和要求\n\n表1-年度能耗管理计划\n[表格 1]\n\n",
        doc_title="能耗管理作业指导书",
        tables=[TableInput(body=medium, table_id="1", caption="表1-年度能耗管理计划")],
    )
    tables = [node for node in result.nodes if node.chunk_type == "table"]
    container_idx = {
        node.parent_index
        for node in tables
        if node.parent_index is not None
        and result.nodes[node.parent_index].chunk_type == "table"
    }
    assert len(container_idx) == 1, ">384 token 的表应出且只出一个表格父容器"
    leaves = [node for node in tables if node.parent_index in container_idx]
    assert len(leaves) > 1, "应拆成多个行组叶子（否则整表一个向量会被稀释）"
    assert all(node.content.startswith("表名:") for node in leaves), "每叶必须重复表头"
    # 每叶 ≤ 384（首行可能超一点，允许 CHILD_MAX 余量）
    assert all(count_tokens(node.content) <= TABLE_LEAF_TOKENS + CHILD_MAX_TOKENS for node in leaves)
    # 容器在子块覆盖后按叶子规则不嵌入（retrievable.py），这里只校验它含整表
    container = result.nodes[next(iter(container_idx))]
    assert "表1-年度能耗管理计划" in container.content
    assert "级别=级别A" in container.content


# --- 表格父容器硬上限 2048 + 超限分段（doc 206 表 3 暴露的缺口）--------------------
#
# 用户拍板：**容器 2048 + 超限分段展开** —— DB 里任何块 ≤2048；超限表格父容器按行
# 分成几段（同表共用 `table_group`），检索命中任一叶子时由 `_expand_parents` 把整表
# 全部分段展开回 prompt（内容完整）。叶子规则「无行重叠 + 每叶重复表头」不变。


def test_oversized_table_container_segments_and_every_segment_owns_leaves() -> None:
    """>2048 的表格父容器按行分段：每段 ≤2048、同组、且每段都必须有叶子。"""
    body, row_count = _oversized_table_body(4000)
    result = chunk_markdown(
        "# 制度\n\n"
        "## 6．质量记录\n\n"
        "下表列出全部质量记录。\n"
        "[表格 2]\n"
        "以上记录保存三年。\n",
        doc_title="设备编号作业指导书",
        tables=[TableInput(body=body, table_id="2", caption="大表")],
    )
    segments, leaves = _table_segments_and_leaves(result)
    assert len(segments) > 1, "序列化 >2048 的整表容器必须分段，不能塞成一个块"
    assert {node.table_group for node in segments} == {"t2"}, "同表分段必须共用一个组 id"
    assert all(node.token_count <= TABLE_CONTAINER_MAX_TOKENS for node in segments)
    # 分段标记：段号/总段数写在容器正文里，模型/审计看得出这是整表的第几段
    assert "本段为整表第 1/" in segments[0].content
    assert f"第 {len(segments)}/{len(segments)} 段" in segments[-1].content

    # 每个分段都必须有叶子：没有子块的 table 节点会变成可检索单元（retrievable.py）
    owned = Counter(node.parent_index for node in leaves)
    for index, node in enumerate(result.nodes):
        if node.table_group:
            assert owned[index] >= 1, f"分段 {index} 没有叶子，会自己变成可检索块"
    for leaf in leaves:
        assert leaf.parent_index is not None
        assert result.nodes[leaf.parent_index].table_group == "t2", "叶子必须挂在分段上"
        assert leaf.token_count <= TABLE_LEAF_TOKENS + CHILD_MAX_TOKENS, "叶子预算不变（384+单行余量）"
        assert leaf.content.startswith("表名:"), "每叶仍要重复表头"

    # 内容完整：全部数据行都出现在某个分段里（分段是行号的连续分区，不丢行）
    joined = "\n".join(node.content for node in segments)
    for index in (0, row_count // 2, row_count - 1):
        assert f"项目=项目{index}" in joined, f"第 {index} 行在分段里丢了"


def test_table_container_at_or_below_cap_stays_single_without_group() -> None:
    """≤2048 的表格父容器保持原行为：单容器 + 行组叶子、没有 table_group（doc 206 形态）。"""
    body, _row_count = _oversized_table_body(1500)
    result = chunk_markdown(
        "# 制度\n\n"
        "## 5. 工作程序和要求\n\n"
        "表3-维修作业等级划分\n"
        "[表格 3]\n\n",
        doc_title="XJWY-GC-09维修作业指导书",
        tables=[TableInput(body=body, table_id="3", caption="表3-维修作业等级划分")],
        source_path="维修作业指导书.docx",
        table_row_chunks=True,
    )
    assert not any(node.table_group for node in result.nodes), "未超上限不该分段"
    container, leaves = _table_container_leaves(result)
    assert container.token_count <= TABLE_CONTAINER_MAX_TOKENS
    assert len(leaves) > 1, "超 384 token 仍按行组出叶子"
    assert all(node.content.startswith("表名:") for node in leaves)


def test_unsplittable_table_falls_back_to_single_container() -> None:
    """单行本身就超过分段预算 → 回退单容器（内容优先，不能为了 2048 丢行）。

    这种表是病态输入（一个单元格 >2000 token），线上几乎不会出现；但必须有确定的
    兜底路径：回退后仍是「容器 + 行组叶子」，只是容器可能略超硬上限。
    若序列化已拆成多块 full_blocks，则按相关表头分组建父容器（可带 table_group）。
    """
    from ekbs.ingest.table_serialize import serialize_table_body

    huge = "超长单元格说明文字" * 400
    body = (
        "| 项目 | 说明 |\n| --- | --- |\n"
        f"| 项目0 | {huge} |\n| 项目1 | 短说明 |\n| 项目2 | 短说明 |"
    )
    full_blocks = serialize_table_body(caption="大表", body=body, table_id="2")
    serialized = sum(count_tokens(block) for block in full_blocks)
    assert serialized > TABLE_CONTAINER_MAX_TOKENS, "本用例前提：整表序列化超硬上限"
    result = chunk_markdown(
        "# 制度\n\n## 6．质量记录\n\n[表格 2]\n\n",
        doc_title="设备编号作业指导书",
        tables=[TableInput(body=body, table_id="2", caption="大表")],
    )
    joined = "\n".join(node.content for node in result.nodes if node.chunk_type == "table")
    assert "项目=项目0" in joined and "项目=项目2" in joined, "回退路径不得丢行"
    if len(full_blocks) == 1:
        assert not any(node.table_group for node in result.nodes), "单块不可分段时不得打组"
        container, leaves = _table_container_leaves(result)
        assert container.token_count > TABLE_CONTAINER_MAX_TOKENS
        assert len(leaves) > 1, "叶子规则不变：仍按 384 行组出叶子"
    else:
        # 多块 full_blocks：按相关表头分组的父容器路径
        assert any(node.table_group for node in result.nodes) or any(
            node.parent_index is not None for node in result.nodes
        )


def test_xlsx_row_chunks_keeps_one_leaf_per_row_after_segmentation() -> None:
    """分段只改变容器打包；xlsx「一行=一条记录」的叶子粒度不变（叶子数 = 数据行数）。

    这条对应"叶子规则不变"：分段前后，同一张表的可检索叶子数不因容器超限而减少或翻倍。
    """
    body, row_count = _oversized_table_body(4000)
    result = chunk_markdown(
        "# 制度\n\n正文。\n",
        doc_title="大表",
        tables=[TableInput(body=body, table_id="2")],
        source_path="大表.xlsx",
        table_row_chunks=True,
    )
    segments, leaves = _table_segments_and_leaves(result)
    assert len(segments) > 1
    assert len(leaves) == row_count, "xlsx 一行=一条记录：分段后仍是一行一叶"


# --- 表格来源决定粒度（2026-11 修「其它块太碎了，你规则有问题」）-----------------------
#
# doc 205（.docx）在 ``retrieval.table_row_chunks=true`` 下被切成 41 块 / 36 个表块，
# 表 6 的 962 token 变成 18 个 55~115 token 的叶子。根因：一行一块（RAGFlow
# spreadsheet 口径）被套到了大表上。行级分块只对「行=一条记录」的电子表格成立；
# Word/PDF/PPT 与未知来源必须走 384 token 行组叶子。

#: 约 900 token 的 Word 表格（4 列 × 26 行），复刻 doc 205 表1/表6 的量级。
_WORD_TABLE = (
    "| 级别 | 工作内容 | 时间节点 | 责任人 |\n| --- | --- | --- | --- |\n"
    + "\n".join(
        f"| 级别{chr(65 + i % 6)} | 工作内容{i}与年度能耗管理计划相关的说明文字 | 时间节点{i} | 责任人{i} |"
        for i in range(26)
    )
)


def _table_container_leaves(result):
    """返回 ``(表格父容器, 行/组叶子列表)``。"""
    tables = [node for node in result.nodes if node.chunk_type == "table"]
    container_idx = {
        node.parent_index
        for node in tables
        if node.parent_index is not None
        and result.nodes[node.parent_index].chunk_type == "table"
    }
    assert len(container_idx) == 1, ">384 token 的表应出且只出一个表格父容器"
    index = next(iter(container_idx))
    return result.nodes[index], [node for node in tables if node.parent_index == index]


def _oversized_table_body(min_tokens: int = 4000) -> tuple[str, int]:
    """构造序列化后 ≥``min_tokens`` 的 3 列表（带长说明文字），返回 ``(body, 数据行数)``。

    按 token 而不是按行数造夹具：token 口径换了之后同样的行数会算出不同预算，
    测试就会假失败（与 ``_long_doc`` 同理由）。
    """
    from ekbs.ingest.table_serialize import serialize_table_body

    rows: list[str] = []
    body = ""
    total = 0
    while total < min_tokens:
        index = len(rows)
        rows.append(
            f"| 项目{index} | 与年度能耗管理计划相关的说明文字第{index}条，"
            f"需记录责任人及时间节点并确保执行到位 | {index} |"
        )
        body = "| 项目 | 说明 | 数值 |\n| --- | --- | --- |\n" + "\n".join(rows)
        total = sum(
            count_tokens(block)
            for block in serialize_table_body(caption="大表", body=body, table_id="2")
        )
    return body, len(rows)


def _table_segments_and_leaves(result):
    """超限表：返回 ``(分段列表, 叶子列表)``（分段靠 ``table_group`` 识别）。"""
    tables = [node for node in result.nodes if node.chunk_type == "table"]
    segments = [node for node in tables if node.table_group]
    leaves = [node for node in tables if not node.table_group]
    return segments, leaves



def _chunk_word_table(source_path: str):
    return chunk_markdown(
        "# 制度\n\n## 5. 工作程序和要求\n\n表1-年度能耗管理计划\n[表格 1]\n\n",
        doc_title="XJWY-GC-12能耗管理作业指导书 - 定稿",
        tables=[TableInput(body=_WORD_TABLE, table_id="1", caption="表1-年度能耗管理计划")],
        source_path=source_path,
        table_row_chunks=True,
    )


def test_word_table_ignores_table_row_chunks_and_uses_row_groups() -> None:
    """Word 表格即使 ``table_row_chunks=True`` 也只按 384 行组切，不逐行碎块。"""
    assert count_tokens(_WORD_TABLE) >= 900, "样本要复刻 doc 205 的 ~900 token 表"
    container, leaves = _table_container_leaves(_chunk_word_table("能耗管理作业指导书 - 定稿.docx"))
    assert "表1-年度能耗管理计划" in container.content
    assert 1 < len(leaves) <= 6, f"行组叶子应是少数几块，而不是一行一块（实际 {len(leaves)}）"
    assert len(leaves) < 26, "不能一行一块（表格共 26 数据行）"
    assert all(node.content.startswith("表名:") for node in leaves), "每叶必须重复表头"
    assert all(
        "相关表头: 级别 | 工作内容 | 时间节点 | 责任人" in node.content for node in leaves
    )
    assert all(
        count_tokens(node.content) <= TABLE_LEAF_TOKENS + CHILD_MAX_TOKENS for node in leaves
    ), "每叶 ≤ 384 + 单行余量"

    # 漏传来源（未知来源）必须与 .docx 等价：默认退回行组，不因忘传参数而碎块
    _, forgot_leaves = _table_container_leaves(_chunk_word_table(""))
    assert [node.content for node in forgot_leaves] == [node.content for node in leaves]


def test_xlsx_source_still_chunks_one_row_per_block() -> None:
    """电子表格（.xlsx）一行=一条记录：``table_row_chunks=True`` 仍一行一块。"""
    table = (
        "| 项目 | 金额 | 备注 |\n| --- | --- | --- |\n"
        + "\n".join(f"| 项目{i} | {i * 100} | 费用说明{i}与执行口径 |" for i in range(60))
    )
    result = chunk_markdown(
        "# 月度费用表\n\n[表格 1]\n\n",
        doc_title="月度费用表",
        tables=[TableInput(body=table, table_id="1")],
        source_path="月度费用表.xlsx",
        table_row_chunks=True,
    )
    _, leaves = _table_container_leaves(result)
    assert len(leaves) == 60, "xlsx 一行=一条记录，应一行一块"
    assert all(node.content.startswith("表名:") for node in leaves)
    assert all("相关表头: 项目 | 金额 | 备注" in node.content for node in leaves)
    assert "项目=项目0; 金额=0" in leaves[0].content


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("报表.xlsx", True),
        ("报表．xlsx ", True),  # 全角句点 + 尾部空格：normalize_suffix 归一后仍是 xlsx
        ("旧表.XLS", True),
        ("宏表.xlsm", True),
        ("/var/raw/制度/能耗管理作业指导书.docx", False),
        ("扫描件.pdf", False),
        ("汇报.pptx", False),
        ("说明.md", False),
        ("导出.csv", False),  # csv 走 text 引擎、不产出 ParsedTable，不算表格来源
        ("service.py", False),
        ("能耗管理作业指导书", False),  # 无后缀（把标题当来源）
        ("", False),
    ],
)
def test_is_spreadsheet_source_only_spreadsheets(path: str, expected: bool) -> None:
    """行级表块只对电子表格开放；未知/空来源必须为 False（默认行组）。"""
    from ekbs.ingest.parser import is_spreadsheet_source

    assert is_spreadsheet_source(path) is expected

def test_doc_metadata_line_injects_containers_and_first_leaf() -> None:
    """文档元数据进上下文容器 + 第一个检索叶子；其余叶子保持纯净。"""
    line = "【文档信息】文件编号：XJWY-GC-09；生效日期：2025-01-01；截止日期：未标注"
    big_table = (
        "| 序号 | 项目 | 说明 |\n| --- | --- | --- |\n"
        + "\n".join(f"| {i} | 项目{i} | {'说明文字' * 8}{i} |" for i in range(30))
    )
    body = "正文内容说明。" * 60
    result = chunk_markdown(
        "# 制度\n\n## 1. 目的\n\n" + body + "\n\n## 2. 范围\n\n" + body + "\n",
        doc_title="测试制度",
        tables=[TableInput(body=big_table, table_id="1")],
        doc_metadata_line=line,
    )
    containers = {
        int(node.parent_index) for node in result.nodes if node.parent_index is not None
    }
    container_nodes = [result.nodes[i] for i in sorted(containers)]
    assert container_nodes, "应有父块/表格父容器"
    assert all(node.content.startswith("【文档信息】") for node in container_nodes)
    leaves = [node for index, node in enumerate(result.nodes) if index not in containers]
    assert leaves
    # 第一个检索叶子带元数据（元数据问句需要能命中它），其余叶子保持纯净
    assert leaves[0].content.startswith("【文档信息】")
    # 元数据作为第一个小节参与装箱，不能把第一个叶子撑出 child_max
    assert leaves[0].token_count <= 384, leaves[0].token_count
    assert all("【文档信息】" not in node.content for node in leaves[1:])



def test_empty_heading_container_does_not_leak_into_document_tail() -> None:
    """单行句式小节（``2.1适用于各部门。``）不能把父标题挤到文档末尾。

    实测 doc 208：``## 2．范围`` 下只有一行 ``2.1适用于各部门。``，这行曾被当成
    嵌套标题，导致 ``2．范围`` 空壳经兜底追加到 ``8.4`` 之后，正文里还残留
    ``## 2．范围`` / ``## 9．支持文件。``。本用例钉住：顺序不变、正文不出现 ``##``。
    """
    md = (
        "# 跑水预案\n\n## 1．目的\n\n目的正文。\n\n"
        "## 2．范围\n\n2.1适用于各部门。\n\n"
        "## 3．定义\n\n3.1定义正文。\n\n"
        "## 9．支持文件。\n\n9.1《工程部工作手册》\n\n"
        "## 10.记录表格\n\n10.1.9《突发事件报告》\n"
    )
    result = chunk_markdown(md, doc_title="跑水预案")
    texts = "\n".join(node.content for node in result.nodes)
    assert "##" not in texts
    assert texts.find("2.1适用于各部门") < texts.find("3．定义")
    assert texts.find("9.1《工程部工作手册》") > texts.find("3.1定义正文")
    assert texts.find("10.1.9《突发事件报告》") > texts.find("9.1《工程部工作手册》")
