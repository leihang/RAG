"""Word 自动编号 / 单元格多段落 / 表题注的解析口径（全离线，纯本地 docx 夹具）。

回归背景（doc_id=196《XJWY-GC-12 能耗管理作业指导书》）：

* 第 5 章「工作程序和要求」的编号住在 ``word/numbering.xml``（abstractNum
  start=5 / lvlText="%1."），正文文字里**没有**「5.」→ 解析稿里整章掉到第 4 章下；
* 表格单元格里一段一段的编号文字被整格拼接 →「…检查2、总水表…」粘连；
* 正文「表1-…」「表2-…」是普通段落而不是 caption 样式，``ParsedTable.caption``
  恒空 → 表块名与原文表题错位。

口径是 **Word 通用**的，不是给某份文件打补丁。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from ekbs.ingest.parser import _parse_docx

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _lvl(ilvl: int, start: int, lvl_text: str, *, num_fmt: str = "decimal", suff: str = "") -> str:
    suffix = f'<w:suff w:val="{suff}"/>' if suff else ""
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/>'
        f'<w:numFmt w:val="{num_fmt}"/>{suffix}'
        f'<w:lvlText w:val="{lvl_text}"/></w:lvl>'
    )


def _abstract(abstract_id: int, *levels: str) -> str:
    return f'<w:abstractNum w:abstractNumId="{abstract_id}">{"".join(levels)}</w:abstractNum>'


def _num(num_id: int, abstract_id: int) -> str:
    return f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="{abstract_id}"/></w:num>'


def _numbering_xml(*parts: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:numbering xmlns:w="{_W}">{"".join(parts)}</w:numbering>'
    )


def _rewrite_numbering(path: Path, numbering_xml: str) -> None:
    """替换 docx 里的 ``word/numbering.xml``（默认模板自带的编号定义与夹具无关）。"""
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == "word/numbering.xml":
                data = numbering_xml.encode("utf-8")
            archive.writestr(name, data)


def _drop_numbering(path: Path) -> None:
    """删掉 ``word/numbering.xml``（连同 content-type 与关系），模拟没有编号表的文档。"""
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == "word/numbering.xml":
                continue
            if name == "[Content_Types].xml":
                data = re.sub(rb'<Override PartName="/word/numbering.xml"[^>]*/>', b"", data)
            elif name == "word/_rels/document.xml.rels":
                data = re.sub(rb'<Relationship[^>]*Target="numbering.xml"[^>]*/>', b"", data)
            archive.writestr(name, data)


def _number_paragraph(paragraph: object, num_id: int, ilvl: int = 0) -> None:
    """给段落挂 ``w:numPr``（python-docx 没有公开 API，直接改 pPr）。"""
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()  # noqa: SLF001 —— 夹具需要底层 XML
    num_pr = p_pr.makeelement(qn("w:numPr"), {})
    num_pr.append(num_pr.makeelement(qn("w:ilvl"), {qn("w:val"): str(ilvl)}))
    num_pr.append(num_pr.makeelement(qn("w:numId"), {qn("w:val"): str(num_id)}))
    p_pr.append(num_pr)


def test_docx_numbering_expanded_from_numbering_xml(tmp_path: Path) -> None:
    """abstractNum start=5 / lvlText="%1." → 正文段落实际是「5. 工作程序和要求」。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    _number_paragraph(document.add_paragraph("工作程序和要求"), 1)
    document.add_paragraph("5.1 管理内容")
    path = tmp_path / "numbering.docx"
    document.save(str(path))
    _rewrite_numbering(path, _numbering_xml(_abstract(0, _lvl(0, 5, "%1.")), _num(1, 0)))

    parsed = _parse_docx(path)

    assert "5. 工作程序和要求" in parsed.markdown, parsed.markdown
    # 后续手打「5.1」段落不受影响
    assert "5.1 管理内容" in parsed.markdown, parsed.markdown


def test_docx_numbering_multilevel_counter_and_reset(tmp_path: Path) -> None:
    """多级编号：%1.%2 用各层当前计数；回到浅层时更深层级计数复位。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    _number_paragraph(document.add_paragraph("章一"), 1, 0)
    _number_paragraph(document.add_paragraph("节一"), 1, 1)
    _number_paragraph(document.add_paragraph("节二"), 1, 1)
    _number_paragraph(document.add_paragraph("章二"), 1, 0)
    _number_paragraph(document.add_paragraph("节三"), 1, 1)
    path = tmp_path / "multilevel.docx"
    document.save(str(path))
    _rewrite_numbering(
        path,
        _numbering_xml(
            _abstract(0, _lvl(0, 5, "%1."), _lvl(1, 1, "%1.%2", suff="space")),
            _num(1, 0),
        ),
    )

    parsed = _parse_docx(path)

    assert "5. 章一" in parsed.markdown, parsed.markdown
    assert "5.1 节一" in parsed.markdown, parsed.markdown
    assert "5.2 节二" in parsed.markdown, parsed.markdown
    assert "6. 章二" in parsed.markdown, parsed.markdown
    assert "6.1 节三" in parsed.markdown, parsed.markdown


def test_docx_numbering_num_start_override(tmp_path: Path) -> None:
    """``w:num/lvlOverride/startOverride`` → 该列表从覆盖值起编（Word 的「重新编号」）。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    _number_paragraph(document.add_paragraph("一"), 1)
    _number_paragraph(document.add_paragraph("二"), 1)
    path = tmp_path / "start-override.docx"
    document.save(str(path))
    _rewrite_numbering(
        path,
        _numbering_xml(
            _abstract(0, _lvl(0, 1, "%1、", suff="nothing")),
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="4"/></w:lvlOverride></w:num>',
        ),
    )

    parsed = _parse_docx(path)

    assert "4、一" in parsed.markdown, parsed.markdown
    assert "5、二" in parsed.markdown, parsed.markdown


def test_docx_cell_paragraphs_joined_with_separator(tmp_path: Path) -> None:
    """单元格内两个自动编号段 → 「1、…」「2、…」，段间有分隔、不粘连。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "工作内容"
    cell = table.cell(0, 1)
    first = cell.paragraphs[0]
    first.add_run("甲乙")
    _number_paragraph(first, 2)
    second = cell.add_paragraph()
    second.add_run("丙丁")
    _number_paragraph(second, 2)
    path = tmp_path / "cell.docx"
    document.save(str(path))
    _rewrite_numbering(
        path,
        _numbering_xml(_abstract(1, _lvl(0, 1, "%1、", suff="nothing")), _num(2, 1)),
    )

    parsed = _parse_docx(path)

    body = parsed.tables[0].body
    assert "1、甲乙" in body, body
    assert "2、丙丁" in body, body
    assert "1、甲乙；2、丙丁" in body, body
    assert "甲乙2、丙丁" not in body, body


def test_docx_caption_paragraph_attached_to_following_table(tmp_path: Path) -> None:
    """紧邻表格的「表N-…」正文段写进 ``ParsedTable.caption``，且正文保留该段。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("表1-年度能耗管理计划编制及实施要求")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "内容"
    path = tmp_path / "caption.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert parsed.tables[0].caption == "表1-年度能耗管理计划编制及实施要求"
    assert "表1-年度能耗管理计划编制及实施要求" in parsed.markdown, parsed.markdown


def test_docx_caption_not_attached_across_body_text(tmp_path: Path) -> None:
    """题注与表格之间隔着正文段 → 不错挂；相邻两张表时题注只挂第一张。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("表2-抄表员检查内容")
    document.add_paragraph("这一段是正文说明，题注与表格之间隔了正文，不能错挂。")
    first = document.add_table(rows=1, cols=1)
    first.cell(0, 0).text = "内容一"
    second = document.add_table(rows=1, cols=1)
    second.cell(0, 0).text = "内容二"
    path = tmp_path / "caption-gap.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert parsed.tables[0].caption == ""
    assert parsed.tables[1].caption == ""


def test_docx_caption_not_attached_to_second_adjacent_table(tmp_path: Path) -> None:
    """题注段与表格之间不能有另一个表格：相邻两表只有第一张继承题注。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("表3-设施设备开关机控制时间表")
    first = document.add_table(rows=1, cols=1)
    first.cell(0, 0).text = "第一张"
    second = document.add_table(rows=1, cols=1)
    second.cell(0, 0).text = "第二张"
    path = tmp_path / "caption-adjacent.docx"
    document.save(str(path))

    parsed = _parse_docx(path)

    assert len(parsed.tables) == 2
    assert parsed.tables[0].caption == "表3-设施设备开关机控制时间表"
    assert parsed.tables[1].caption == ""


def test_docx_numbering_not_prepended_twice(tmp_path: Path) -> None:
    """段落文字已手打「3、」时不再重复前置编号。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    for text in ("一", "二", "3、三"):
        _number_paragraph(document.add_paragraph(text), 3)
    path = tmp_path / "dup-number.docx"
    document.save(str(path))
    _rewrite_numbering(
        path,
        _numbering_xml(_abstract(2, _lvl(0, 1, "%1、", suff="nothing")), _num(3, 2)),
    )

    parsed = _parse_docx(path)

    assert "1、一" in parsed.markdown, parsed.markdown
    assert "2、二" in parsed.markdown, parsed.markdown
    assert "3、三" in parsed.markdown, parsed.markdown
    assert "1、3、三" not in parsed.markdown, parsed.markdown


def test_docx_without_numbering_part_is_safe(tmp_path: Path) -> None:
    """没有 ``word/numbering.xml`` 的 docx：不抛异常、文本不变。"""
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("工作程序和要求")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "内容"
    path = tmp_path / "no-numbering.docx"
    document.save(str(path))
    _drop_numbering(path)

    parsed = _parse_docx(path)

    assert "工作程序和要求" in parsed.markdown, parsed.markdown
    assert parsed.tables[0].body == "<table><tr><th>内容</th></tr></table>"
