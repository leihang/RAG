"""把 Word / Excel / PPT / 文本原件转成可嵌入对照页的 HTML。

PDF 和图片仍走原件字节（浏览器自己渲染）。这里只处理浏览器不能内嵌的办公格式，
让复核左栏看到的是正文和表格，而不是「请下载」。

macOS 上 Word 优先用系统 ``textutil``（保留段落和表格样式）。没有它时，
``.docx`` 退回 python-docx 抽正文。``.doc`` 没有 textutil 就明确失败。
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path

# Word/Excel/文本：整份转 HTML，过大则页面放不下。
_MAX_BYTES = 30 * 1024 * 1024
# PPT 预览只抽幻灯片文字（不嵌底图），工程汇报类原件常 40–70MB，单独放宽。
_MAX_SLIDE_BYTES = 80 * 1024 * 1024

_WORD = {".doc", ".docx", ".rtf", ".odt"}
_SHEET = {".xls", ".xlsx"}
_SLIDE = {".ppt", ".pptx"}
_TEXT = {".txt", ".md", ".csv", ".html", ".htm"}
PREVIEW_SUFFIXES = _WORD | _SHEET | _SLIDE | _TEXT

_BLOCK = re.compile(
    r"<(script|iframe|object|embed|base|link)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_VOID = re.compile(
    r"<(script|iframe|object|embed|base|link)\b[^>]*/?>",
    re.IGNORECASE,
)
_ON_ATTR = re.compile(
    r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_JS_URL = re.compile(r"(?<=[\"'])\s*javascript:[^\"']*", re.IGNORECASE)


class OfficePreviewError(Exception):
    """这个原件转不成可内嵌的页面。"""


def render_office_preview(path: Path) -> str:
    """返回完整 HTML。不支持的后缀、过大或转换失败抛 ``OfficePreviewError``。"""
    suffix = path.suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise OfficePreviewError(f"该格式请直接看原件（{suffix or '未知'}）")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OfficePreviewError("原件无法读取") from exc
    limit = _MAX_SLIDE_BYTES if suffix in _SLIDE else _MAX_BYTES
    if size > limit:
        limit_mb = limit // (1024 * 1024)
        raise OfficePreviewError(f"原件超过 {limit_mb}MB，页面里放不下")

    if suffix in _WORD:
        body = _word_html(path)
    elif suffix in _SHEET:
        body = _sheet_html(path)
    elif suffix in _SLIDE:
        body = _slide_html(path)
    else:
        body = _text_html(path)
    return _sanitize(body)


def _word_html(path: Path) -> str:
    converted = _textutil_html(path)
    if converted:
        return converted
    if path.suffix.lower() == ".docx":
        return _docx_html(path)
    raise OfficePreviewError(
        "预览 Word 需要 textutil（macOS 自带）。当前系统没有它。"
    )


def _textutil_html(path: Path) -> str | None:
    binary = shutil.which("textutil")
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "-convert", "html", "-stdout", str(path)],
            capture_output=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if path.suffix.lower() == ".docx":
            return None
        raise OfficePreviewError(f"textutil 预览失败：{path.name}") from exc
    if completed.returncode != 0 or not completed.stdout:
        if path.suffix.lower() == ".docx":
            return None
        tail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise OfficePreviewError(f"textutil 预览失败：{path.name} {tail[:180]}")
    return completed.stdout.decode("utf-8", errors="replace")


def _docx_html(path: Path) -> str:
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception as exc:  # pragma: no cover
        raise OfficePreviewError(f"python-docx 不可用：{exc}") from exc

    document = docx.Document(str(path))
    parts: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(f"<p>{html.escape(text)}</p>")
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows = []
            for row in table.rows:
                cells = "".join(
                    f"<td>{html.escape(cell.text)}</td>" for cell in row.cells
                )
                rows.append(f"<tr>{cells}</tr>")
            if rows:
                parts.append("<table>" + "".join(rows) + "</table>")
    if not parts:
        parts.append("<p>（文档没有可提取的文字）</p>")
    return _wrap("".join(parts))


def _sheet_html(path: Path) -> str:
    if path.suffix.lower() == ".xls":
        return _xls_html(path)
    return _xlsx_html(path)


def _xlsx_html(path: Path) -> str:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise OfficePreviewError(f"openpyxl 不可用：{exc}") from exc
    # 不用 read_only：合并单元格和 number_format 在只读模式不可靠；原件预览必须
    # 和解析侧同一套 Excel 显示口径（否则百分比会并排显示成小数）。
    from ekbs.ingest.parser import xlsx_sheet_html

    workbook = openpyxl.load_workbook(str(path), data_only=True)
    parts: list[str] = []
    try:
        for sheet in workbook.worksheets:
            parts.append(f"<h2>{html.escape(sheet.title)}</h2>")
            body = xlsx_sheet_html(sheet)
            parts.append(body or "<p>（空表）</p>")
    finally:
        workbook.close()
    return _wrap("".join(parts) or "<p>（工作簿是空的）</p>")


def _xls_html(path: Path) -> str:
    try:
        import xlrd
    except Exception as exc:  # pragma: no cover
        raise OfficePreviewError(f"xlrd 不可用：{exc}") from exc
    book = xlrd.open_workbook(str(path))
    parts: list[str] = []
    for sheet in book.sheets():
        parts.append(f"<h2>{html.escape(sheet.name)}</h2>")
        rows = []
        for row_index in range(sheet.nrows):
            values = [sheet.cell_value(row_index, col) for col in range(sheet.ncols)]
            if all(value is None or str(value).strip() == "" for value in values):
                continue
            cells = "".join(f"<td>{html.escape(_cell_text(value))}</td>" for value in values)
            rows.append(f"<tr>{cells}</tr>")
        parts.append("<table>" + "".join(rows) + "</table>" if rows else "<p>（空表）</p>")
    return _wrap("".join(parts) or "<p>（工作簿是空的）</p>")


def _slide_html(path: Path) -> str:
    if path.suffix.lower() == ".ppt":
        raise OfficePreviewError("旧版 .ppt 不能在页面里排版，请先转成 .pptx 或 PDF。")
    try:
        from pptx import Presentation
    except Exception as exc:  # pragma: no cover
        raise OfficePreviewError(f"python-pptx 不可用：{exc}") from exc
    presentation = Presentation(str(path))
    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        parts.append(f"<h2>第 {index} 页</h2>")
        wrote = False
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                rows = []
                for row in shape.table.rows:
                    cells = "".join(
                        f"<td>{html.escape(cell.text)}</td>" for cell in row.cells
                    )
                    rows.append(f"<tr>{cells}</tr>")
                parts.append("<table>" + "".join(rows) + "</table>")
                wrote = True
            elif getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    parts.append(f"<p>{html.escape(text)}</p>")
                    wrote = True
        if not wrote:
            parts.append("<p>（本页没有可提取的文字）</p>")
    return _wrap("".join(parts) or "<p>（演示文稿是空的）</p>")


def _text_html(path: Path) -> str:
    if path.suffix.lower() in {".html", ".htm"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raw = path.read_text(encoding="utf-8", errors="replace")
    return _wrap(f"<pre>{html.escape(raw)}</pre>")


def _cell_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def _wrap(body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
        "<style>"
        "body{margin:16px;color:#111;background:#fff;"
        "font:14px/1.65 'PingFang SC','Songti SC','Noto Serif CJK SC',serif;}"
        "table{border-collapse:collapse;margin:8px 0 16px;}"
        "td,th{border:1px solid #bfbfbf;padding:4px 8px;vertical-align:top;}"
        "h2{font-size:16px;margin:18px 0 8px;}"
        "pre{white-space:pre-wrap;font:13px/1.55 ui-monospace,Menlo,monospace;}"
        "</style></head><body>"
        f"{body}</body></html>"
    )


def _sanitize(raw: str) -> str:
    text = _BLOCK.sub("", raw)
    text = _BLOCK_VOID.sub("", text)
    text = _ON_ATTR.sub("", text)
    return _JS_URL.sub("#", text)
