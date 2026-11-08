"""钉钉 sampleMarkdown 子集排版（对照官方 robot-message-type）。"""

from __future__ import annotations

from ekbs.dingtalk.markdown_format import (
    format_for_sample_markdown,
    markdown_preview_title,
)


def test_bracket_sections_become_headings_with_soft_breaks() -> None:
    raw = (
        "《方案》要点如下：\n"
        "\n"
        "- **主机改造**：按时段调温。\n"
        "\n"
        "【9:30 – 11:30】\n"
        "出水温度设定：11-12℃\n"
        "【11:30 – 14:00】\n"
        "出水温度设定：10℃\n"
        "\n"
        "【2025 年用电量（度）】\n"
        "公共风柜：464,500\n"
        "空调主机：771,520\n"
        "合计：1,283,960\n"
    )
    out = format_for_sample_markdown(raw)
    assert "#### 9:30 – 11:30" in out
    assert "#### 11:30 – 14:00" in out
    assert "#### 2025 年用电量（度）" in out
    assert "【" not in out
    # 软换行：非末行带两个尾随空格
    assert "公共风柜：464,500  " in out
    assert "空调主机：771,520  " in out
    assert out.rstrip().endswith("合计：1,283,960")
    assert "- **主机改造**" in out


def test_pipe_tables_become_heading_blocks() -> None:
    raw = (
        "明细：\n"
        "| 项目 | 9月用量 | 同比 |\n"
        "| --- | --- | --- |\n"
        "| 活动用电 | 1343.00 | 229.17% |\n"
    )
    out = format_for_sample_markdown(raw)
    assert "| 项目 |" not in out
    assert "#### 活动用电" in out
    assert "9月用量：1343.00" in out
    assert "同比：229.17%" in out


def test_references_and_suggestions_use_headings() -> None:
    raw = (
        "答案正文[1]。\n"
        "\n"
        "参考资料：\n"
        "[1] 中央空调IOT节能改造方案 · 工程部\n"
        "[2] 中央空调IOT节能改造方案 · 表 3\n"
        "\n"
        "你可以接着问：\n"
        "1. 预算是多少？\n"
        "2. 回收期多久？\n"
    )
    out = format_for_sample_markdown(raw)
    assert "### 参考资料" in out
    assert "- **[1]** 中央空调IOT节能改造方案 · 工程部" in out
    assert "- **[2]** 中央空调IOT节能改造方案 · 表 3" in out
    assert "### 你可以接着问" in out
    assert "1. 预算是多少？" in out


def test_help_section_uses_same_heading_style() -> None:
    raw = (
        "直接发送文字：\n"
        "1. 查询权限\n"
        "2. 问答示例 — 按你的权限生成示例问答\n"
        "\n"
        "也可以直接提问，例如：跑水事件环境部先做什么？\n"
    )
    out = format_for_sample_markdown(raw)
    assert "### 直接发送文字" in out
    assert "1. 查询权限" in out
    assert "2. 问答示例" in out


def test_strips_unsupported_fences_hrs_and_html() -> None:
    raw = (
        "说明如下：\n"
        "```\n"
        "code line\n"
        "```\n"
        "---\n"
        "<b>保留文字</b>\n"
    )
    out = format_for_sample_markdown(raw)
    assert "```" not in out
    assert "code line" in out
    assert "---" not in out
    assert "<b>" not in out
    assert "保留文字" in out


def test_preview_title_strips_markup() -> None:
    assert markdown_preview_title("#### 《方案》要点\n\n- a") == "《方案》要点"
    assert markdown_preview_title("**粗体标题**\n正文") == "粗体标题"
    assert markdown_preview_title("   \n") == "知识库回答"
