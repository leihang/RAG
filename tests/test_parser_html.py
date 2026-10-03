"""`.html` / `.htm` 解析（2026-10-03 新增，标准库实现，不引入依赖）。

为什么要它：`.html` 原先**没有解析分支**，落到 text 引擎后整行标签原样入库
（实测 `content` 就是 `<html><body><h1>交接班制度</h1>…`）—— 既污染检索词项又白占 token。
现在的契约：剥标签，但**保留标题层级、列表与表格**结构，且 `<head>` 整段丢弃。
"""

from __future__ import annotations

from pathlib import Path

from ekbs.ingest.parser import SUFFIX_ENGINES, parse_file

SAMPLE = """<!doctype html>
<html><head><title>不该进正文的标题</title><style>p{color:red}</style>
<script>var x = 1;</script></head>
<body>
<h1>交接班管理制度</h1>
<p>接班人应<b>提前15分钟</b>到场。</p>
<h2>4.1 值班要求</h2>
<ul><li>坚持岗位</li><li>上报领导</li></ul>
<table><tr><th>项目</th><th>要求</th></tr><tr><td>到场</td><td>15分钟</td></tr></table>
<p>费用&nbsp;100&nbsp;元 &amp; 税率</p>
</body></html>
"""


def _parse(tmp_path: Path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return parse_file(path)


def test_html_is_registered_for_both_suffixes() -> None:
    """.html 与 .htm 都要映射到 html 引擎（否则落回 text，标签又会进正文）。"""
    assert SUFFIX_ENGINES[".html"] == "html"
    assert SUFFIX_ENGINES[".htm"] == "html"


def test_html_keeps_structure_and_drops_tags(tmp_path: Path) -> None:
    """标题层级保留、标签剥净、列表分行、实体解码、`<head>` 整段丢弃。"""
    parsed = _parse(tmp_path, "示例.html", SAMPLE)
    assert parsed.engine == "html"
    lines = [line for line in parsed.markdown.splitlines() if line.strip()]

    assert lines[0] == "# 交接班管理制度", lines[:3]
    assert "接班人应提前15分钟到场。" in lines, "标签必须剥净（<b> 不能留）"
    assert "## 4.1 值班要求" in lines
    assert "坚持岗位" in lines and "上报领导" in lines, "列表项要各占一行"
    assert "费用 100 元 & 税率" in lines, "HTML 实体要解码"
    assert not any("<" in line for line in lines), f"还有残留标签：{lines}"
    assert "不该进正文的标题" not in parsed.markdown, "<head>/<title> 不该进正文"
    assert "var x = 1" not in parsed.markdown, "<script> 内容不该进正文"


def test_html_tables_go_to_the_table_channel(tmp_path: Path) -> None:
    """表格走独立通道（与 docx/xlsx 同口径）：正文留占位符，真实内容进 `tables`。"""
    parsed = _parse(tmp_path, "示例.html", SAMPLE)
    assert len(parsed.tables) == 1, "表格应被抽出"
    body = parsed.tables[0].body
    assert "| 项目 | 要求 |" in body and "| 到场 | 15分钟 |" in body
    assert "[表格 1]" in parsed.markdown, "正文要留占位符"
    # 表格文字**不能**同时出现在正文行里（实测踩过一次重复）
    plain = [line for line in parsed.markdown.splitlines() if line.strip()]
    assert "项目要求" not in plain and "到场15分钟" not in plain


def test_html_without_tables_is_plain_ok(tmp_path: Path) -> None:
    """没有表格的 html 也要正常（别为了表格把正文搞丢）。"""
    parsed = _parse(tmp_path, "简单.htm", "<html><body><h3>小标题</h3><p>正文一句。</p></body></html>")
    assert parsed.engine == "html"
    assert "### 小标题" in parsed.markdown and "正文一句。" in parsed.markdown
    assert parsed.tables == []
