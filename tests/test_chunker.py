"""四层分块（`06` §3.2/§3.4、`docs/spec/20` §3）。"""

from __future__ import annotations

from ekbs.ingest.chunker import (
    CHILD_MAX_TOKENS,
    CHILD_MIN_TOKENS,
    OVERLAP_MIN_RATIO,
    PARENT_MAX_TOKENS,
    PARENT_MIN_TOKENS,
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
    """`table_row_chunks` 只对超预算的大表生效；小表整表一块（spec §3.4）。"""
    small = "| 项目 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |"
    small_result = chunk_markdown(
        "# 制度\n\n正文。\n",
        doc_title="小表",
        tables=[TableInput(body=small, table_id="1")],
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
        table_row_chunks=True,
    )
    big_tables = [n for n in big_result.nodes if n.chunk_type == "table"]
    assert len(big_tables) > 1, "超预算的大表才按行出块"
    assert all(node.content.startswith("表名:") for node in big_tables)


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
    """大表：父容器（上文 + 整表 + 下文）+ 行/组叶子，叶子 parent_index 指向容器。"""
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
        table_row_chunks=True,
    )
    tables = [n for n in result.nodes if n.chunk_type == "table"]
    container_idx = {
        n.parent_index
        for n in tables
        if n.parent_index is not None and result.nodes[n.parent_index].chunk_type == "table"
    }
    assert len(container_idx) == 1, "应恰有一个表父容器"
    container = result.nodes[next(iter(container_idx))]
    assert "下表列出全部质量记录" in container.content, "上方临近文字进父容器"
    assert "以上记录保存三年" in container.content, "下方结论进父容器"
    assert "6．质量记录" in container.content, "段落标题名进父容器"
    leaves = [n for n in tables if n.parent_index in container_idx]
    assert len(leaves) > 1, "大表应按行出叶子"
    assert all(n.content.startswith("表名:") for n in leaves)
