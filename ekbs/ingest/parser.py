"""解析层：把各种文件变成「Markdown + 页码映射」（`01` §4、`00` M1/M12）。

为什么是可插拔的：

* `01` §4 用 **MinerU** 作中文制度主力（版面/表格/公式/OCR），但它要下 2–3 GB 模型，
  且「macOS 26 原生安装与速度」是**必须实测的未知项 M1**；
* 因此骨架阶段允许用轻量解析（``.md`` / ``.txt`` / ``.docx`` / ``.xlsx``）把
  「入库 → 检索 → 问答」整条链先跑通；MinerU 就绪后只需换 :func:`parse_file` 的分支。

**页码**：这是引用可溯源性的前提（`00` §2.2）。轻量解析拿不到真实页码时，``page_map``
留空、``page_from``/``page_to`` 为 NULL —— 此时引用卡片只显示文档名与标题路径，
不会伪造页码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Iterator, NamedTuple, Sequence

from ekbs.logging import get_logger

log = get_logger(component="parser")


class ParserUnavailable(RuntimeError):
    """解析引擎不可用（例如 MinerU 未安装）。**不允许静默降级为「解析成功但内容为空」**。"""


@dataclass
class ParsedFigure:
    """文档里抽出的图片（**已落盘**，不是临时文件）。

    ``name`` 是相对于 ``assets_dir`` 的相对路径（如 ``images/abc.jpg``）；
    上层会用 ``assets_dir`` 把它换算成**相对 data_dir** 的 ``ekbs_chunk.image_path``
    （``/assets/figure/{chunk_id}`` 正是按 data_dir 解析该字段）。
    """

    name: str
    caption: str = ""
    page: int | None = None
    ocr_text: str = ""
    figure_id: str = ""


@dataclass
class ParsedTable:
    """文档里抽出的表格（不摊进正文，独立成 ``chunk_type=table``）。"""

    body: str
    caption: str = ""
    page: int | None = None
    table_id: str = ""


@dataclass
class ParsedDocument:
    markdown: str
    engine: str
    source_path: str = ""
    #: ``[(char_offset, page_from, page_to), …]``；轻量解析为空
    page_map: list[tuple[int, int, int]] = field(default_factory=list)
    page_count: int | None = None
    #: 抽出的图片（仅在给了 ``assets_dir`` 时非空 —— 没落盘就没有可服务的图片）
    figures: list[ParsedFigure] = field(default_factory=list)
    #: 抽出的表格（确定性序列化后入索引，不重复写进 markdown）
    tables: list[ParsedTable] = field(default_factory=list)


def parse_markdown(text: str, *, engine: str = "text") -> ParsedDocument:
    return ParsedDocument(markdown=text, engine=engine)


from ekbs.ingest.code_chunker import CODE_SUFFIXES as _CODE_SUFFIXES

#: 扩展名 → 解析引擎。**这是"解析器真正支持的格式"的唯一权威**。
#:
#: 为什么单独提出来：原先这份映射内联在 :func:`parse_file` 里，而
#: ``ekbs/tools/import.DEFAULT_EXTENSIONS`` 与配置的 ``limits.file_ext_whitelist``
#: 各自维护一份 —— 结果**三方不一致**（CLI 默认漏了 pdf/png/jpg，配置里又多了
#: doc/ppt/xls 这些解析器根本没实现的格式）。import 的默认白名单现在直接引用这里。
#: 其余格式（doc/ppt/xls）需要各自的解析分支，见 README §四。
SUFFIX_ENGINES: dict[str, str] = {
    ".md": "text",
    ".markdown": "text",
    ".txt": "text",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pdf": "mineru",
    ".png": "mineru",
    ".jpg": "mineru",
    ".jpeg": "mineru",
    # 旧版 Office：都靠专门的转换/解析分支，**绝不能**落到 text（那会把二进制读成乱码）
    ".doc": "doc",      # macOS 自带 textutil 转 docx 后复用 docx 分支
    ".pptx": "pptx",    # python-pptx
    ".xls": "xls",      # xlrd（注意：xlrd 2.x 只支持旧版 .xls）
}

# 代码文件：按**文本**正确读入（二进制才需要专门分支），分块交给代码感知分块器
# （`code_chunker.chunk_code`，由入库管线按 source_path 判定）。
# 名单**直接取 `code_chunker.CODE_SUFFIXES`**（单一来源）：那边支持一门语言，
# 这里自动就能导入，不会再出现"白名单/解析器/分块器三处各写一份"的老问题。
SUFFIX_ENGINES.update({suffix: "text" for suffix in _CODE_SUFFIXES})


def normalize_suffix(filename: str) -> str:
    """从文件名取**归一化**的扩展名（不带点，小写）。

    为什么不能直接 ``PurePath(name).suffix``：真实文件名常带这些"看不见"的东西，
    而它们都会让一个 `.xlsx` 被判成"不支持的类型"（实测）：

    | 文件名 | 裸取法 | 归一化后 |
    |---|---|---|
    | ``报表.xlsx ``（尾部空格，Windows/邮件下载常见） | ``'xlsx '`` | ``xlsx`` |
    | ``报表．xlsx``（**全角**句点，中文输入法） | ``''`` | ``xlsx`` |
    | ``报表.xlsx.``（尾部多余点） | ``''`` | ``xlsx`` |

    白名单判定与"这个文件到底能不能解析"都依赖它，所以**入库侧与上传侧共用这一份**。
    """
    name = (filename or "").strip()
    # 全角句点/全角空格先归一到半角，否则后缀切不出来
    name = name.replace("．", ".").replace("\u3000", " ")
    name = name.strip().rstrip(". ")
    return PurePath(name).suffix.lower().lstrip(".")


def parse_file(
    path: str | Path, *, engine: str = "auto", assets_dir: Path | None = None
) -> ParsedDocument:
    """按扩展名选择解析器。

    ``assets_dir``：**持久化目录** —— 解析器抽出的图片（文档内插图）会复制到这里，
    并由 :class:`ParsedFigure` 报回。**不传就丢图**：MinerU 原本在临时目录里产出图片，
    函数一返回临时目录即被删除，markdown 里的 ``![](images/x.jpg)`` 全成死链
    （这是本仓库实测到的 bug，也是"文档里的图片有实际意义"这个需求之前不成立的根因）。
    """
    target = Path(path)
    if not target.is_file():
        raise ParserUnavailable(f"文件不存在：{target}")
    suffix = target.suffix.lower()
    if engine == "auto":
        engine = SUFFIX_ENGINES.get(suffix, "text")

    if engine == "text":
        return _parse_text(target, assets_dir=assets_dir)
    if engine == "docx":
        return _parse_docx(target, assets_dir=assets_dir)
    if engine == "xlsx":
        return _parse_xlsx(target, assets_dir=assets_dir)
    if engine == "doc":
        return _parse_doc(target, assets_dir=assets_dir)
    if engine == "pptx":
        return _parse_pptx(target, assets_dir=assets_dir)
    if engine == "xls":
        return _parse_xls(target, assets_dir=assets_dir)
    if engine == "mineru":
        return _parse_mineru(target, assets_dir=assets_dir)
    raise ParserUnavailable(f"未知解析引擎：{engine}")


def _rows_to_pipe_markdown(rows: list[list[str]]) -> str:
    """二维单元格 → Markdown 管道表（供 ``ParsedTable.body`` / 确定性序列化）。"""
    if not rows:
        return ""
    lines: list[str] = []
    for index, row in enumerate(rows):
        lines.append("| " + " | ".join(row) + " |")
        if index == 0:
            lines.append("|" + "---|" * len(row))
    return "\n".join(lines)


#: OOXML / 磁盘图片后缀（落到 ``figures`` 的才认；emf/wmf 无通用解码器，仍落盘供回源）
_IMAGE_EXTS = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "emf", "wmf",
})


def _parse_text(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """纯文本 / Markdown / 代码：读正文；Markdown 本地图片引用落盘为 figure，管道表升为 table。"""
    markdown = path.read_text(encoding="utf-8", errors="replace")
    tables: list[ParsedTable] = []
    figures: list[ParsedFigure] = []
    # 仅 Markdown 族抽表/图；``.py`` / ``.txt`` 里的 ``|`` 或路径不该当表格/配图
    if path.suffix.lower() in {".md", ".markdown"}:
        markdown, tables = _extract_markdown_pipe_tables(markdown)
        markdown, figures = _extract_markdown_local_figures(
            markdown, source_path=path, assets_dir=assets_dir
        )
    return ParsedDocument(
        markdown=markdown,
        engine="text",
        source_path=str(path),
        figures=figures,
        tables=tables,
    )


def _parse_docx(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.docx` —— 按文档顺序抽段落、表格与**嵌入图片**（与 pptx 同口径进 ``figures``）。"""
    try:
        import docx  # python-docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-docx 不可用：{exc}") from exc

    document = docx.Document(str(path))
    lines: list[str] = []
    tables: list[ParsedTable] = []
    figures: list[ParsedFigure] = []
    table_seq = 0
    seen_images: set[str] = set()
    ocr_budget = [_figure_ocr_budget()]
    textless: list[tuple[int, int]] = []

    def _ingest_images(element: Any) -> None:
        for blob, ext, caption in _docx_images_in_element(element, document):
            added = _register_figure_blob(
                blob, ext,
                assets_dir=assets_dir, figures=figures, seen=seen_images,
                caption=caption, page=None, ocr_budget=ocr_budget,
                log_file=path.name,
            )
            if added and not figures[-1].caption and not figures[-1].ocr_text:
                textless.append((len(figures) - 1, len(blob)))

    # 按文档顺序交错段落 / 表格 / 图片，避免「全文段落完再甩表/图」打乱阅读位置
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            _ingest_images(child)
            text = (paragraph.text or "").strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style else ""
            if style.startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                lines.append("#" * min(int(level or 1), 6) + " " + text)
            else:
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            _ingest_images(child)
            rows: list[list[str]] = []
            for row in table.rows:
                cells = [((cell.text or "").strip().replace("\n", " ")) for cell in row.cells]
                if any(cells):
                    rows.append(cells)
            body = _rows_to_pipe_markdown(rows)
            if not body:
                continue
            table_seq += 1
            tables.append(
                ParsedTable(body=body, caption="", page=None, table_id=str(table_seq))
            )
            lines.append(f"[表格 {table_seq}]")

    # 兜底：正文遍历漏掉的关系图（页眉/页脚/文本框等）仍要落盘，否则等于丢图
    for blob, ext, caption in _docx_images_from_rels(document):
        added = _register_figure_blob(
            blob, ext,
            assets_dir=assets_dir, figures=figures, seen=seen_images,
            caption=caption, page=None, ocr_budget=ocr_budget,
            log_file=path.name,
        )
        if added and not figures[-1].caption and not figures[-1].ocr_text:
            textless.append((len(figures) - 1, len(blob)))

    # 再兜底：页眉/页脚的图挂在 **header/footer part** 的关系上，不在 document.part.rels；
    # 实测交接班制度等 docx 的 logo 只在 word/header1.xml → word/media/，正文 blip=0。
    for section in document.sections:
        for part in (
            getattr(section, "header", None),
            getattr(section, "footer", None),
            getattr(section, "first_page_header", None),
            getattr(section, "first_page_footer", None),
            getattr(section, "even_page_header", None),
            getattr(section, "even_page_footer", None),
        ):
            if part is None:
                continue
            element = getattr(part, "_element", None)
            if element is None:
                continue
            for blob, ext, caption in _docx_images_in_element(element, part):
                added = _register_figure_blob(
                    blob, ext,
                    assets_dir=assets_dir, figures=figures, seen=seen_images,
                    caption=caption, page=None, ocr_budget=ocr_budget,
                    log_file=path.name,
                )
                if added and not figures[-1].caption and not figures[-1].ocr_text:
                    textless.append((len(figures) - 1, len(blob)))

    # zip 级兜底：任何 part 漏扫时仍能从 word/media/ 捞到栅格图（与 xlsx 同口径）
    for blob, ext in _ooxml_media_blobs(path, prefix="word/media/"):
        added = _register_figure_blob(
            blob, ext,
            assets_dir=assets_dir, figures=figures, seen=seen_images,
            caption="", page=None, ocr_budget=ocr_budget,
            log_file=path.name,
        )
        if added and not figures[-1].caption and not figures[-1].ocr_text:
            textless.append((len(figures) - 1, len(blob)))

    if textless:
        title = path.stem.strip()
        if title:
            biggest = max(textless, key=lambda item: item[1])[0]
            if not figures[biggest].caption and not figures[biggest].ocr_text:
                figures[biggest].caption = title

    if figures:
        log.info("docx_figures_extracted", file=path.name, figures=len(figures))

    return ParsedDocument(
        markdown="\n".join(lines),
        engine="docx",
        source_path=str(path),
        tables=tables,
        figures=figures,
    )


def _parse_xlsx(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.xlsx` —— 工作表 → 表格块；嵌入图片（``sheet._images`` / ``xl/media``）→ figure。

    图表（``workbook._charts`` / sheet 上的 chart）抽成 ``ParsedTable``，与 PPT 图表同口径。
    """
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"openpyxl 不可用：{exc}") from exc

    workbook = openpyxl.load_workbook(str(path), data_only=True)
    lines: list[str] = []
    tables: list[ParsedTable] = []
    figures: list[ParsedFigure] = []
    table_seq = 0
    seen_images: set[str] = set()
    ocr_budget = [_figure_ocr_budget()]

    for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
        lines.append(f"## {sheet.title}")
        rows: list[list[str]] = []
        for row in sheet.iter_rows(values_only=True):
            values = ["" if cell is None else str(cell).strip() for cell in row]
            if not any(v for v in values):
                continue
            rows.append(values)
        body = _rows_to_pipe_markdown(rows)
        if body:
            table_seq += 1
            tables.append(
                ParsedTable(
                    body=body,
                    caption=sheet.title,
                    page=sheet_index,
                    table_id=str(table_seq),
                )
            )
            lines.append(f"[表格 {table_seq}] {sheet.title}".rstrip())

        for image in getattr(sheet, "_images", []) or []:
            try:
                blob = image._data()  # noqa: SLF001 — openpyxl 无稳定公开 API
            except Exception as exc:
                log.warning("xlsx_image_unreadable", sheet=sheet.title, error=str(exc)[:80])
                continue
            ext = ""
            path_hint = getattr(image, "path", None) or ""
            if path_hint:
                ext = Path(str(path_hint)).suffix.lstrip(".")
            # 浮动图可能带旋转（openpyxl Image.rotation，单位度）
            try:
                rotate = float(getattr(image, "rotation", 0) or 0)
            except Exception:
                rotate = 0.0
            _register_figure_blob(
                blob, ext or "png",
                assets_dir=assets_dir, figures=figures, seen=seen_images,
                caption=sheet.title, page=sheet_index, ocr_budget=ocr_budget,
                log_file=path.name, rotate_degrees=rotate,
            )

        for chart_rows, caption in _xlsx_sheet_chart_tables(sheet):
            body = _rows_to_pipe_markdown(chart_rows)
            if not body:
                continue
            table_seq += 1
            tables.append(
                ParsedTable(
                    body=body,
                    caption=caption or sheet.title,
                    page=sheet_index,
                    table_id=str(table_seq),
                )
            )
            label = caption or sheet.title
            lines.append(f"[表格 {table_seq}] {label}".rstrip())
        lines.append("")

    # sheet._images 可能漏掉部分介质：再扫 zip 内 xl/media/（按内容摘要去重）
    for blob, ext in _ooxml_media_blobs(path, prefix="xl/media/"):
        _register_figure_blob(
            blob, ext,
            assets_dir=assets_dir, figures=figures, seen=seen_images,
            caption=path.stem, page=None, ocr_budget=ocr_budget,
            log_file=path.name,
        )

    if figures:
        log.info("xlsx_figures_extracted", file=path.name, figures=len(figures))

    return ParsedDocument(
        markdown="\n".join(lines),
        engine="xlsx",
        source_path=str(path),
        tables=tables,
        figures=figures,
    )


def _xlsx_sheet_chart_tables(sheet: Any) -> Iterator[tuple[list[list[str]], str]]:
    """从工作表上的 chart 尽量还原类别×系列数值表。"""
    charts = list(getattr(sheet, "_charts", None) or [])
    for chart in charts:
        try:
            title = ""
            title_obj = getattr(chart, "title", None)
            if title_obj is not None:
                # openpyxl Title.tx.rich 或 title 字符串，尽量宽松
                title = str(getattr(title_obj, "tx", None) or title_obj or "").strip()
                if "Title()" in title or title.startswith("<"):
                    title = ""
            series_list = list(getattr(chart, "series", None) or [])
            if not series_list:
                continue
            # 类别：取第一条 series 的 x 值；数值取 val
            categories: list[str] = []
            series_cols: list[tuple[str, list[str]]] = []
            for index, series in enumerate(series_list, start=1):
                name = str(getattr(series, "title", None) or f"系列{index}")
                vals = []
                try:
                    val = series.val
                    nums = getattr(val, "numRef", None) or getattr(val, "numLit", None)
                    if nums is not None and getattr(nums, "pt", None):
                        vals = [
                            "" if pt.v is None else str(pt.v)
                            for pt in nums.pt
                        ]
                    elif hasattr(series, "values"):
                        vals = ["" if v is None else str(v) for v in (series.values or [])]
                except Exception:
                    vals = []
                try:
                    cat_ref = series.tx if False else getattr(series, "cat", None)
                    if cat_ref is not None and not categories:
                        strref = getattr(cat_ref, "strRef", None) or getattr(cat_ref, "numRef", None)
                        pts = getattr(strref, "pt", None) if strref is not None else None
                        if pts:
                            categories = ["" if pt.v is None else str(pt.v) for pt in pts]
                except Exception:
                    pass
                series_cols.append((name, vals))
            if not series_cols:
                continue
            width = max((len(vals) for _, vals in series_cols), default=0)
            if width <= 0:
                continue
            if not categories:
                categories = [str(i) for i in range(1, width + 1)]
            header = ["类别"] + [name for name, _ in series_cols]
            rows: list[list[str]] = [header]
            for row_index in range(width):
                row = [categories[row_index] if row_index < len(categories) else ""]
                for _, vals in series_cols:
                    row.append(vals[row_index] if row_index < len(vals) else "")
                rows.append(row)
            yield rows, title
        except Exception as exc:
            log.warning("xlsx_chart_unreadable", error=str(exc)[:80])
            continue


def _cell_text(value: object) -> str:
    """单元格 → 文本；整数型 float 去掉多余小数（xlrd 会把 1 读成 1.0）。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value).strip()


def _parse_doc(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """旧版 `.doc`（Word 97-2003）—— 用 **macOS 自带 `textutil`** 转成 docx 后复用 docx 分支。

    为什么是"转换"而不是"直接解析"：`.doc` 是 OLE 复合文档，Python 侧没有可靠的纯实现
    （`olefile` 只能读容器，取正文要 antiword / catdoc / LibreOffice —— 本机都没有）。
    而 macOS 自带 `textutil`，零依赖，实测中文完整（15 段全部读出）。
    **不转就会落到 text 分支把二进制读成乱码**（实测 24 KB 的 .doc 读出 24450 字符乱码）。

    非 macOS（没有 `textutil`）时**明确报错**，不静默降级。

    **图片**：``textutil`` 转 docx 时常丢掉嵌入图。转换后若 ``figures`` 仍空，
    再对原 ``.doc`` 做 PNG/JPEG 魔数扫描作兜底（与 xls 同口径），避免「有图等于没图」。
    """
    import shutil
    import subprocess
    import tempfile

    binary = shutil.which("textutil")
    if not binary:
        raise ParserUnavailable(
            "解析 .doc 需要 textutil（macOS 自带）。当前系统没有它 —— "
            "请在 macOS 上运行，或先把 .doc 转成 .docx/.pdf 再入库。"
            "**不要**试图绕过：.doc 是二进制格式，按文本读只会得到乱码。"
        )

    with tempfile.TemporaryDirectory(prefix="ekbs-doc-") as tmp:
        converted = Path(tmp) / (path.stem + ".docx")
        completed = subprocess.run(
            [binary, "-convert", "docx", "-output", str(converted), str(path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if completed.returncode != 0 or not converted.is_file():
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-2:]
            raise ParserUnavailable(f"textutil 转换失败（.doc）：{path.name}；" + " / ".join(tail))
        parsed = _parse_docx(converted, assets_dir=assets_dir)

    figures = list(parsed.figures)
    if not figures and assets_dir is not None:
        # textutil 常丢图：从原 .doc 二进制里捞还能认的栅格图
        seen: set[str] = set()
        ocr_budget = [_figure_ocr_budget()]
        for blob, ext in _scan_embedded_raster_blobs(path.read_bytes()):
            _register_figure_blob(
                blob, ext,
                assets_dir=assets_dir, figures=figures, seen=seen,
                caption=path.stem, page=None, ocr_budget=ocr_budget,
                log_file=path.name,
            )
        if figures:
            log.info("doc_figures_scanned", file=path.name, figures=len(figures))

    # 引擎名保留 doc，便于日志/审计区分"走了旧格式转换"
    return ParsedDocument(
        markdown=parsed.markdown,
        engine="doc",
        source_path=str(path),
        page_map=parsed.page_map,
        page_count=parsed.page_count,
        figures=figures,
        tables=parsed.tables,
    )


def _parse_pptx(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.pptx` —— 用 `python-pptx` 按页抽文本、表格**与图片**。

    为什么必须专门实现：`.pptx` 是 zip 容器，落到 text 分支会把二进制当 UTF-8 读
    （实测 142 MB 的 pptx 会变成一大坨乱码，还白吃内存）。原先它只在白名单里、
    没有解析分支，属于"放进来再变垃圾"。

    **图片放置方式**（都应能抽）：
    * ``PICTURE`` / ``LINKED_PICTURE`` 直接图；
    * ``PLACEHOLDER`` 里插入的图（``shape.image`` 可读时）；
    * ``GROUP`` 组合内的图（递归 ``_iter_shapes``）；
    * 形状 ``rotation`` / EXIF 竖拍 —— 落盘前矫正；
    * ``ppt/media/`` zip 兜底（母版/版式漏扫时）；
    * 图表（``CHART``）→ ``ParsedTable``（数值表，不是截图）。

    竖向幻灯片（``slide_height > slide_width``）在页标题标 ``（竖向）``，便于检索区分。

    ⚠️ **不往 markdown 里写 ``![](images/..)`` 链接**：图片块是从 ``figures`` 建的，
    不看链接；而链接会原样进父块/子块的正文并被算进向量。
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-pptx 不可用：{exc}") from exc

    presentation = Presentation(str(path))
    portrait = bool(
        presentation.slide_height and presentation.slide_width
        and presentation.slide_height > presentation.slide_width
    )
    pages: list[str] = []
    figures: list[ParsedFigure] = []
    tables: list[ParsedTable] = []
    table_seq = 0
    ocr_budget = [_figure_ocr_budget()]
    seen_images: set[str] = set()
    skipped_pictures = 0

    for number, slide in enumerate(presentation.slides, start=1):
        heading = f"## 第 {number} 页" + ("（竖向）" if portrait else "")
        lines: list[str] = [heading]
        textless: list[tuple[int, int]] = []
        for shape in _iter_shapes(slide.shapes):
            # —— 图片：不限 PICTURE，占位符/链接图只要 .image 可读都抽 ——
            picture = _pptx_shape_picture(shape)
            if picture is not None:
                blob, ext = picture
                try:
                    rotate = float(getattr(shape, "rotation", 0.0) or 0.0)
                except Exception:
                    rotate = 0.0
                added = _register_figure_blob(
                    blob, ext,
                    assets_dir=assets_dir, figures=figures, seen=seen_images,
                    caption=_alt_text(shape), page=number, ocr_budget=ocr_budget,
                    log_file=path.name, rotate_degrees=rotate,
                )
                if added and not figures[-1].caption and not figures[-1].ocr_text:
                    textless.append((len(figures) - 1, len(blob)))
            else:
                # 明确是图片类形状但读不出字节（断链的链接图等）
                try:
                    st = shape.shape_type
                except Exception:
                    st = None
                if st in {
                    MSO_SHAPE_TYPE.PICTURE,
                    MSO_SHAPE_TYPE.LINKED_PICTURE,
                }:
                    skipped_pictures += 1
                    log.warning("pptx_picture_unreadable", slide=number, shape_type=str(st))

            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    lines.append(text)

            if getattr(shape, "has_table", False):
                table = shape.table
                rows: list[list[str]] = []
                for row in table.rows:
                    cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        rows.append(cells)
                body = _rows_to_pipe_markdown(rows)
                if body:
                    table_seq += 1
                    tables.append(
                        ParsedTable(
                            body=body, caption="", page=number, table_id=str(table_seq),
                        )
                    )
                    lines.append(f"[表格 {table_seq}]")

            # 图表 → 数值表（放置方式与「图片截图」不同，但问答同样要能引用）
            chart_rows = _pptx_chart_rows(shape)
            if chart_rows:
                body = _rows_to_pipe_markdown(chart_rows)
                if body:
                    table_seq += 1
                    title = _alt_text(shape) or ""
                    tables.append(
                        ParsedTable(
                            body=body, caption=title, page=number, table_id=str(table_seq),
                        )
                    )
                    lines.append(f"[表格 {table_seq}]{(' ' + title) if title else ''}".rstrip())

        notes = getattr(slide, "notes_slide", None)
        if notes is not None:
            note_text = (notes.notes_text_frame.text or "").strip()
            if note_text:
                lines.append(f"（备注）{note_text}")

        if textless:
            page_title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
            if page_title:
                biggest = max(textless, key=lambda item: item[1])[0]
                figures[biggest].caption = page_title
                log.info(
                    "pptx_figure_caption_fallback", slide=number,
                    figure=figures[biggest].figure_id, caption=page_title,
                    textless=len(textless),
                )
        pages.append("\n".join(lines))

    # zip 兜底：母版/版式/漏扫形状里的 media
    for blob, ext in _ooxml_media_blobs(path, prefix="ppt/media/"):
        _register_figure_blob(
            blob, ext,
            assets_dir=assets_dir, figures=figures, seen=seen_images,
            caption=path.stem if portrait else "", page=None, ocr_budget=ocr_budget,
            log_file=path.name,
        )

    if figures or skipped_pictures:
        log.info(
            "pptx_figures_extracted", file=path.name, figures=len(figures),
            unreadable=skipped_pictures, portrait=portrait,
            ocr_budget=_figure_ocr_budget(),
        )
    markdown = "\n\n".join(pages)
    return ParsedDocument(
        markdown=markdown, engine="pptx", source_path=str(path),
        page_map=page_map_from_pages(pages), page_count=len(pages),
        figures=figures,
        tables=tables,
    )


def _pptx_shape_picture(shape: Any) -> tuple[bytes, str] | None:
    """从形状取嵌入/占位图片字节。覆盖 PICTURE / LINKED_PICTURE / 含图的 PLACEHOLDER。"""
    try:
        image = shape.image
    except Exception:
        return None
    try:
        blob = image.blob
        ext = image.ext or "png"
    except Exception:
        return None
    if not blob:
        return None
    return blob, str(ext)


def _pptx_chart_rows(shape: Any) -> list[list[str]] | None:
    """图表系列 → 二维表；不是图就返回 None。"""
    if not getattr(shape, "has_chart", False):
        return None
    try:
        chart = shape.chart
        plot = chart.plots[0]
        categories = [
            "" if getattr(cat, "label", None) is None else str(cat.label)
            for cat in plot.categories
        ]
        series_list = list(plot.series)
        if not categories and not series_list:
            return None
        header = ["类别"] + [
            str(series.name or f"系列{index}")
            for index, series in enumerate(series_list, start=1)
        ]
        rows: list[list[str]] = [header]
        values_per_series = [list(series.values) for series in series_list]
        for index, category in enumerate(categories):
            row = [category]
            for values in values_per_series:
                value = values[index] if index < len(values) else ""
                row.append("" if value is None else str(value))
            rows.append(row)
        return rows if len(rows) > 1 else None
    except Exception as exc:
        log.warning("pptx_chart_unreadable", error=str(exc)[:80])
        return None


def _parse_xls(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """旧版 `.xls` —— 用 `xlrd` 按工作表抽成 Markdown 表格（与 xlsx 分支同口径）。

    注意：`xlrd` 2.x **只支持旧版 `.xls`**（xlsx 由 openpyxl 负责），两者正好互补。

    **图片**：xlrd 不暴露嵌入图 API。这里对文件字节做 PNG/JPEG 魔数扫描作兜底
    （与 ``.doc`` 同口径），能捞到的栅格图进 ``figures``；矢量/OLE 包装图仍会漏。
    """
    try:
        import xlrd
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(
            f"xlrd 不可用：{exc}；解析 .xls 需要它（pip install xlrd），"
            "或先把表格另存为 .xlsx。"
        ) from exc

    book = xlrd.open_workbook(str(path))
    lines: list[str] = []
    tables: list[ParsedTable] = []
    figures: list[ParsedFigure] = []
    table_seq = 0
    for sheet_index, sheet in enumerate(book.sheets(), start=1):
        lines.append(f"## {sheet.name}")
        rows: list[list[str]] = []
        for row_index in range(sheet.nrows):
            # xlrd 把所有数字读成 float，整数会显示成 "1.0" —— 归一化成 "1"
            values = [_cell_text(v) for v in sheet.row_values(row_index)]
            if not any(values):
                continue
            rows.append(values)
        body = _rows_to_pipe_markdown(rows)
        if body:
            table_seq += 1
            tables.append(
                ParsedTable(
                    body=body,
                    caption=sheet.name,
                    page=sheet_index,
                    table_id=str(table_seq),
                )
            )
            lines.append(f"[表格 {table_seq}] {sheet.name}".rstrip())
        lines.append("")

    if assets_dir is not None:
        seen: set[str] = set()
        ocr_budget = [_figure_ocr_budget()]
        for blob, ext in _scan_embedded_raster_blobs(path.read_bytes()):
            _register_figure_blob(
                blob, ext,
                assets_dir=assets_dir, figures=figures, seen=seen,
                caption=path.stem, page=None, ocr_budget=ocr_budget,
                log_file=path.name,
            )
        if figures:
            log.info("xls_figures_scanned", file=path.name, figures=len(figures))

    return ParsedDocument(
        markdown="\n".join(lines),
        engine="xls",
        source_path=str(path),
        tables=tables,
        figures=figures,
    )


#: MinerU CLI 可执行文件的环境变量覆盖（独立 venv 时指向 var/mineru-venv/bin/mineru）
MINERU_BIN_ENV = "EKBS_MINERU_BIN"

# python-pptx 的组合形状枚举（在 _iter_shapes 里用；这里存值避免每次导入 pptx）
MSO_SHAPE_TYPE_GROUP = 6
#: MinerU 模型来源（国内拉 HuggingFace 基本不通，实测必须用 modelscope）
MINERU_SOURCE_ENV = "MINERU_MODEL_SOURCE"

#: ModelScope 的缓存根（`modelscope_hub.utils.get_cache_dir` 认这个变量）。
#: 默认是 `~/.cache/modelscope` —— 本仓库把模型装在仓库内，见 :func:`_mineru_env`。
MODELSCOPE_CACHE_ENV = "MODELSCOPE_CACHE"


def _repo_mineru_models_dir() -> Path:
    """本仓库约定的模型缓存位置：`<repo>/var/mineru-models`。

    与 `var/mineru-venv`（MinerU 自己的 venv）成对出现 —— 部署文档里这两个路径是
    一起给的，所以这里一并自动发现，避免"装了 venv 却没告诉它模型在哪"。
    """
    return Path(__file__).resolve().parents[2] / "var" / "mineru-models"


def _mineru_env() -> dict[str, str]:
    """给 MinerU 子进程准备环境变量（解析与 OCR 共用一份，别再各写一遍）。

    两件事：

    1. ``MINERU_MODEL_SOURCE`` 默认 ``modelscope``（实测 HuggingFace 在国内不可达）；
    2. ``MODELSCOPE_CACHE`` 缺省指向 ``<repo>/var/mineru-models``（**目录存在才指**）。

    第 2 条是实打实踩出来的：MinerU 的模型默认下到 ``~/.cache/modelscope``，
    而那个路径在生产/沙箱里常常写不进去（实测报
    ``PermissionError: [Errno 1] Operation not permitted: '/Users/…/.cache/modelscope'``），
    表现却是"解析跑完但一个产物都没有" —— 极难定位。放进仓库目录后，
    「装了模型」与「找得到模型」是同一件事。

    显式设置的环境变量永远优先（生产可以把模型放在 `/data/ekbs/mineru-models`）。
    """
    import os

    env = dict(os.environ)
    env.setdefault(MINERU_SOURCE_ENV, "modelscope")
    if not env.get(MODELSCOPE_CACHE_ENV):
        candidate = _repo_mineru_models_dir()
        if candidate.is_dir():
            env[MODELSCOPE_CACHE_ENV] = str(candidate)
    return env
MINERU_TIMEOUT_ENV = "EKBS_MINERU_TIMEOUT"


def _mineru_bin() -> str:
    """定位 mineru 可执行文件。

    ⚠️ 查找顺序里**必须先看「与当前解释器同 venv」**：launchd 的 PATH 只有
    ``/usr/bin:/bin:/usr/sbin:/sbin``，直接 ``shutil.which("mineru")`` 在生产
    （``/opt/ekbs/venv/bin/mineru``）**找不到**，会误报「MinerU 未安装」。
    """
    import os
    import shutil
    import sys

    explicit = os.environ.get(MINERU_BIN_ENV)
    candidates = (
        [explicit]
        if explicit
        else [
            # ① 与当前解释器同一个 venv（用户按 01 §4 把 mineru 装进 .venv 时的情形）
            str(Path(sys.executable).parent / "mineru"),
            # ② **本仓库文档规定的独立 venv**：`docs/测试方案-按工作流.md:49` 与
            #    `deploy/requirements.txt` 都让人把 MinerU 装在 `<repo>/var/mineru-venv`，
            #    并 `export EKBS_MINERU_BIN=.../mineru`。少设一个环境变量的后果不是
            #    "降级"而是**全盘不可用**：`.pdf/.png/.jpg/.jpeg` 全部解析失败，
            #    图片 OCR 静默跳过 —— 实测本机 var/mineru-venv 已装好 2 GB 依赖，
            #    却因为没人在 secrets.env 里设这个变量，PDF 一个都进不来。
            #    所以这里把那个约定位置也纳入自动发现，env 仍是最高优先级。
            str(Path(__file__).resolve().parents[2] / "var" / "mineru-venv" / "bin" / "mineru"),
            "mineru",
        ]
    )
    for candidate in candidates:
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if resolved and Path(resolved).exists():
            return resolved
    if True:
        raise ParserUnavailable(
            "MinerU 未安装：真实 PDF/图片解析不可用（00 §11 的 M1）。"
            f"安装（01 §4，**注意必须带 extras**，只装基础包会在 get_device() 报 "
            f"NameError: torch is not defined）："
            f"python -m venv <venv> && <venv>/bin/pip install 'mineru[core]==2.1.10'；"
            f"国内还需 MINERU_MODEL_SOURCE=modelscope（实测 HuggingFace 不可达）。"
            f"装好后用 {MINERU_BIN_ENV}=<venv>/bin/mineru 指向它（或与解释器同 venv，会自动找到）。"
        )
    return resolved


def _parse_mineru(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """MinerU 解析（`01` §4 的 M1）—— **2026-09-27 按实测产物定稿**。

    实测契约（mineru 2.1.10 / macOS arm64）：

    * 命令 ``mineru -p <src> -o <out>``，产物在 ``<out>/<文件名>/auto/``；
    * ⚠️ **没有 ``structure.json``** —— `01` §4 与 `docs/spec/20` 的 T8 都提到了它，
      但该版本的实际产物是 ``<stem>_content_list.json`` / ``<stem>_middle.json`` /
      ``<stem>_model.json`` / ``<stem>.md``（本文按实测实现，异常时回落到 ``.md``）；
    * ``_content_list.json`` 是**块列表**，每块字段：``type``
      （``text`` / ``table`` / ``image`` / ``equation``）、``text``、``page_idx``
      （**0 基**，转页码要 +1）、可选的 ``text_level``（标题层级）与 ``img_path``；
    * 实测发现两处保真度问题，**不要**依赖 MinerU 输出还原结构：
      文档标题行可能被整块丢弃；``第X条`` 这类中文条文**不会**被识别成 heading
      （``text_level`` 缺失）→ 标题路径可能为空（`06` §3 的分块依赖它）。

    走 **CLI 子进程**而不是 ``import mineru``：MinerU 的依赖（torch / transformers /
    ultralytics）与内存峰值都很大，装进主 venv 会污染 web 服务的依赖树；
    子进程同时隔离了它在 16 GB 机器上的内存峰值。
    """
    import json
    import os
    import subprocess
    import tempfile

    binary = _mineru_bin()
    timeout = float(os.environ.get(MINERU_TIMEOUT_ENV) or 1800)
    env = _mineru_env()

    with tempfile.TemporaryDirectory(prefix="ekbs-mineru-") as out_dir:
        try:
            completed = subprocess.run(
                [binary, "-p", str(path), "-o", out_dir],
                capture_output=True, text=True, timeout=timeout, env=env, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParserUnavailable(f"MinerU 解析超时（>{timeout:.0f}s）：{path.name}") from exc
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
            raise ParserUnavailable(
                f"MinerU 解析失败（退出码 {completed.returncode}）：{path.name}；" + " / ".join(tail)
            )

        # 产物目录：<out>/<stem>/auto（无 auto 时退化为 <out>/<stem>）
        base = Path(out_dir) / path.stem
        auto = base / "auto"
        if not auto.is_dir():
            auto = base if base.is_dir() else Path(out_dir)
        content_list = _find_output(auto, f"{path.stem}_content_list.json")
        markdown_file = _find_output(auto, f"{path.stem}.md")

        if content_list is None:
            if markdown_file is None:
                raise ParserUnavailable(
                    f"MinerU 未产出可识别的结果（既无 content_list.json 也无 .md）：{path.name}"
                )
            # 回落：只有 Markdown（此时拿不到页码，page_map 留空 —— 不伪造页码）
            log.warning("mineru_content_list_missing", file=path.name, fallback="markdown")
            return ParsedDocument(
                markdown=markdown_file.read_text(encoding="utf-8", errors="replace"),
                engine="mineru", source_path=str(path),
            )

        blocks = json.loads(content_list.read_text(encoding="utf-8"))
        return _blocks_to_document(
            blocks, path, markdown_file, out_root=auto, assets_dir=assets_dir
        )


def _figure_ocr_budget() -> int:
    """本文件允许 OCR 的图片张数。

    **默认 0（关闭）** —— 这不是省事，是实测结论：

    2026-09-29 装好模型后实测，MinerU 对**独立图片**（PNG/JPG）走的是版面分析流水线，
    它把整张图判定为一个 figure 区域，产出的 markdown 就是**这张图自己的引用**：

        img-02561a4c.png  530 KB  32.1s → "![](images/2b6202af….jpg)"

    也就是说：每张图花 **~33 秒**、换回来一条指回原图的链接，**没有一个字**。
    一份 92 张图的 PPT 按旧默认（12 张上限）要空跑 6~7 分钟，且什么也拿不到。

    这正是方案 `00` §11 的 **M12 / `06` §4.11.2 的 O6-7** 留的问题——
    「MinerU 对纯图片输入的支持程度（决定独立图片走 MinerU 还是回落 PaddleOCR）」。
    **实测答案：不支持，须按方案写的回落 PaddleOCR PP-OCRv4。**

    所以这里默认关闭；接入 PaddleOCR 后再打开。要临时压测可以：
    ``EKBS_FIGURE_OCR_MAX=2`` 打开前 2 张（``EKBS_FIGURE_OCR=0`` 仍可一键关掉）。
    """
    import os

    if str(os.environ.get("EKBS_FIGURE_OCR", "1")).strip() in {"0", "false", "no"}:
        return 0
    try:
        return max(0, int(os.environ.get("EKBS_FIGURE_OCR_MAX", "0")))
    except ValueError:
        return 0


def _iter_shapes(shapes: Any) -> Iterator[Any]:
    """递归展开形状树：**组合（GROUP）里的图片挂在 ``shape.shapes`` 下**。

    只遍历 ``slide.shapes`` 会漏掉分组里的图 —— 现状恰好没漏（实测这份 38 页 PPT
    的 142 张图都在顶层），但只要有人把图片成组，图片就会静默消失，所以按树走。
    """
    for shape in shapes:
        yield shape
        try:
            if shape.shape_type == MSO_SHAPE_TYPE_GROUP:
                yield from _iter_shapes(shape.shapes)
        except Exception:  # 个别形状取 shape_type 会抛，跳过这一支而不是整篇失败
            continue


def _alt_text(shape: Any) -> str:
    """形状的替代文本（PowerPoint「Alt Text」）。python-pptx 没有公开属性。

    对幻灯片来说这是**唯一的原生文字线索**（实测这份 PPT 92 张唯一图片里只有 6 张有）。
    """
    try:
        return str(shape._element._nvXxPr.cNvPr.get("descr") or "").strip()
    except Exception:
        return ""


def _register_figure_blob(
    blob: bytes,
    ext: str,
    *,
    assets_dir: Path | None,
    figures: list[ParsedFigure],
    seen: set[str],
    caption: str = "",
    page: int | None = None,
    ocr_budget: list[int],
    log_file: str = "",
    rotate_degrees: float = 0.0,
) -> bool:
    """落盘并登记一张图。``seen`` 按**矫正后**内容摘要去重；``ocr_budget`` 为长度 1 的可变预算。

    返回是否**新登记**了一张（重复 / 未落盘 → False）。与 pptx 分支同口径，
    供 docx / xlsx / md / doc / xls 复用，避免再漏「有 media 却 figures=0」。

    ``rotate_degrees``：形状/控件上的显示旋转（PPT ``shape.rotation``），落盘时矫正，
    避免竖放照片被按横图 OCR。去重在矫正之后做，这样「同一张手机竖拍、
    EXIF 方向不同」只会进索引一次。
    """
    import hashlib

    if not blob:
        return False
    normalized, ext_norm = _normalize_raster_bytes(
        blob, ext, rotate_degrees=rotate_degrees
    )
    digest = hashlib.sha256(normalized).hexdigest()
    if digest in seen:
        return False
    seen.add(digest)
    # 已矫正，persist 时不再二次旋转/重编码
    managed = _persist_figure_bytes(
        normalized, ext_norm, assets_dir, normalize=False
    )
    if managed is None:
        log.warning("figure_not_persisted", file=log_file or None)
        return False
    ocr = ""
    if ocr_budget and ocr_budget[0] > 0 and assets_dir is not None:
        outcome = _ocr_figure(assets_dir / managed)
        ocr = outcome.text
        if outcome.attempted:
            ocr_budget[0] -= 1
        if not outcome.usable:
            log.warning(
                "figure_ocr_disabled_for_document", file=log_file or None,
                skipped_after=len(figures),
                hint="MinerU 不可用，本文件剩余图片不再尝试 OCR（图片本身已落盘）",
            )
            ocr_budget[0] = 0
    figures.append(
        ParsedFigure(
            name=managed,
            caption=(caption or "").strip(),
            page=page,
            ocr_text=ocr,
            figure_id=Path(managed).stem,
        )
    )
    return True


def _docx_images_in_element(element: Any, document: Any) -> Iterator[tuple[bytes, str, str]]:
    """从一段 / 一表 / 页眉的 OOXML 节点里抽出嵌入图 ``(blob, ext, caption)``。

    覆盖：
    * DrawingML ``a:blip``（``wp:inline`` 环绕 / ``wp:anchor`` 浮于文字上 都走 blip）；
    * 旧式 VML ``v:imagedata``（``w:pict``，早期 Word 仍常见）。
    """
    try:
        from docx.oxml.ns import qn
    except Exception:  # pragma: no cover
        return

    def _related_blob(r_id: str) -> tuple[bytes, str] | None:
        if not r_id:
            return None
        try:
            part = document.part.related_parts[r_id]
        except (KeyError, AttributeError):
            return None
        blob = getattr(part, "blob", None)
        if not blob:
            return None
        partname = str(getattr(part, "partname", "") or "")
        ext = Path(partname).suffix.lstrip(".").lower() or "png"
        if ext not in _IMAGE_EXTS:
            return None
        return blob, ext

    blip_tag = qn("a:blip")
    embed_attr = qn("r:embed")
    for blip in element.iter(blip_tag):
        r_id = blip.get(embed_attr)
        got = _related_blob(r_id)
        if not got:
            continue
        blob, ext = got
        caption = ""
        parent = blip.getparent()
        for _ in range(8):
            if parent is None:
                break
            for attr in ("descr", "title", "name"):
                value = parent.get(attr)
                if value and str(value).strip():
                    caption = str(value).strip()
                    break
            if caption:
                break
            parent = parent.getparent()
        yield blob, ext, caption

    # VML：``{urn:schemas-microsoft-com:vml}imagedata`` + ``r:id``
    vml_imagedata = "{urn:schemas-microsoft-com:vml}imagedata"
    r_id_attr = qn("r:id")
    for node in element.iter(vml_imagedata):
        r_id = node.get(r_id_attr) or node.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        got = _related_blob(r_id or "")
        if not got:
            continue
        blob, ext = got
        caption = (
            node.get("{urn:schemas-microsoft-com:office:office}title")
            or node.get("o:title")
            or ""
        )
        yield blob, ext, str(caption or "").strip()


def _docx_images_from_rels(document: Any) -> Iterator[tuple[bytes, str, str]]:
    """扫文档所有 image 关系（页眉/页脚/文本框等正文遍历会漏的）。"""
    try:
        rels = document.part.rels.values()
    except Exception:  # pragma: no cover
        return
    for rel in rels:
        reltype = str(getattr(rel, "reltype", "") or "")
        if "image" not in reltype.lower():
            continue
        try:
            part = rel.target_part
            blob = part.blob
        except Exception:
            continue
        if not blob:
            continue
        partname = str(getattr(part, "partname", "") or "")
        ext = Path(partname).suffix.lstrip(".").lower() or "png"
        if ext not in _IMAGE_EXTS:
            continue
        yield blob, ext, ""


def _ooxml_media_blobs(path: Path, *, prefix: str) -> Iterator[tuple[bytes, str]]:
    """从 OOXML zip（docx/xlsx/pptx）的 ``media/`` 目录枚举图片字节。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith(prefix) or name.endswith("/"):
                    continue
                ext = Path(name).suffix.lstrip(".").lower()
                if ext not in _IMAGE_EXTS:
                    continue
                try:
                    yield archive.read(name), ext
                except Exception as exc:
                    log.warning("ooxml_media_unreadable", entry=name, error=str(exc)[:80])
    except zipfile.BadZipFile:
        return


def _scan_embedded_raster_blobs(data: bytes) -> Iterator[tuple[bytes, str]]:
    """从 OLE/二进制容器里按魔数捞 PNG / JPEG（``.doc`` / ``.xls`` 的图兜底）。

    不保证完整率（矢量/OLE 包装会漏），但比 ``figures=0`` 强——同类型缺陷的最小补缺。
    """
    if not data:
        return
    png_sig = b"\x89PNG\r\n\x1a\n"
    iend = b"IEND\xaeB`\x82"
    offset = 0
    while True:
        start = data.find(png_sig, offset)
        if start < 0:
            break
        end = data.find(iend, start)
        if end < 0:
            break
        end += len(iend)
        blob = data[start:end]
        if len(blob) >= 67:  # 最小合法 PNG 量级
            yield blob, "png"
        offset = end

    # JPEG：``FFD8FF`` … ``FFD9``；限制单张上限避免把整文件当一张图
    jpeg_sig = b"\xff\xd8\xff"
    offset = 0
    max_jpeg = 20 * 1024 * 1024
    while True:
        start = data.find(jpeg_sig, offset)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 3)
        if end < 0:
            break
        end += 2
        if end - start <= max_jpeg and end - start >= 128:
            yield data[start:end], "jpg"
        offset = end


#: Markdown 本地 / 相对路径图片：``![alt](path)``（跳过 http(s) / data:）
_MD_LOCAL_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
#: GFM 管道表（表头 + 分隔行 + 至少一行数据）
_MD_PIPE_TABLE_RE = re.compile(
    r"(?:^[ \t]*\|.+\|[ \t]*\n^[ \t]*\|[-:| \t]+\|[ \t]*\n(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)


def _extract_markdown_pipe_tables(markdown: str) -> tuple[str, list[ParsedTable]]:
    """把 Markdown 管道表升为 ``ParsedTable``，正文改成 ``[表格 N]`` 锚点（与 Office 同口径）。"""
    tables: list[ParsedTable] = []
    if not markdown:
        return markdown, tables

    def replacer(match: re.Match[str]) -> str:
        body = match.group(0).rstrip()
        tables.append(
            ParsedTable(body=body, caption="", page=None, table_id=str(len(tables) + 1))
        )
        return f"[表格 {len(tables)}]\n"

    cleaned = _MD_PIPE_TABLE_RE.sub(replacer, markdown)
    return cleaned, tables


def _extract_markdown_local_figures(
    markdown: str,
    *,
    source_path: Path,
    assets_dir: Path | None,
) -> tuple[str, list[ParsedFigure]]:
    """Markdown 本地图片引用 → 落盘 ``figures``，正文改成短锚点（与 pptx/MinerU 同口径）。

    远程 ``http(s):`` / ``data:`` 保留原链接（不是入库资产，也落不了盘）。
    """
    figures: list[ParsedFigure] = []
    if not markdown:
        return markdown, figures
    if assets_dir is None:
        # 与 pptx 一致：没持久目录就不抽图、不改正文（预览场景）
        return markdown, figures
    seen: set[str] = set()
    ocr_budget = [_figure_ocr_budget()]
    base = source_path.parent
    replacements: list[tuple[int, int, str]] = []

    for match in _MD_LOCAL_IMAGE_RE.finditer(markdown):
        alt = (match.group(1) or "").strip()
        src = (match.group(2) or "").strip().strip("\"'")
        if not src or src.startswith(("http://", "https://", "data:", "mailto:")):
            continue
        src_path = src.split("#", 1)[0].split("?", 1)[0]
        candidate = Path(src_path)
        if not candidate.is_absolute():
            candidate = (base / candidate).resolve()
        if not candidate.is_file():
            log.warning("markdown_local_image_missing", src=src, file=source_path.name)
            continue
        ext = candidate.suffix.lstrip(".").lower() or "png"
        if ext not in _IMAGE_EXTS:
            continue
        try:
            blob = candidate.read_bytes()
        except OSError as exc:
            log.warning("markdown_local_image_unreadable", src=src, error=str(exc)[:80])
            continue
        before = len(figures)
        _register_figure_blob(
            blob, ext,
            assets_dir=assets_dir, figures=figures, seen=seen,
            caption=alt or candidate.stem, page=None, ocr_budget=ocr_budget,
            log_file=source_path.name,
        )
        if len(figures) > before:
            stem = figures[-1].figure_id
            caption = figures[-1].caption
            anchor = f"[图片 {stem[:12]}]{(' ' + caption) if caption else ''}".rstrip()
        else:
            # 重复图 / 未落盘：仍剥链接，避免噪声进向量
            anchor = f"[图片]{(' ' + alt) if alt else ''}".rstrip() or "[图片]"
        replacements.append((match.start(), match.end(), anchor))

    if replacements:
        parts: list[str] = []
        cursor = 0
        for start, end, anchor in replacements:
            parts.append(markdown[cursor:start])
            parts.append(anchor)
            cursor = end
        parts.append(markdown[cursor:])
        markdown = "".join(parts)

    if figures:
        log.info(
            "markdown_figures_extracted", file=source_path.name, figures=len(figures)
        )
    return markdown, figures


def _persist_figure_bytes(
    data: bytes,
    ext: str,
    assets_dir: Path | None,
    *,
    rotate_degrees: float = 0.0,
    normalize: bool = True,
) -> str | None:
    """把**容器内的**图片字节落盘到持久目录，返回相对 ``assets_dir`` 的路径。

    与 :func:`_persist_figure` 的区别：那个用于 MinerU 已经把图片写在磁盘上的情形，
    这个用于 pptx/docx 这类**图片只存在于 zip 容器里**的格式 —— 磁盘上从来没有过
    独立文件，不主动写出来就等于丢图。

    文件名用**内容摘要**（``img-<sha8>.<ext>``）而不是序号：
    实测这份 PPT 里同一张图最多出现 **33 次**（页眉 logo），按序号命名会落 33 份
    完全相同的文件；按内容命名天然去重，且"改了图再导"不会覆盖旧图。

    **方向**：默认落盘前做 EXIF Orientation 矫正，并可选叠加形状旋转角
    （PPT/Word 里「竖着放的手机照片」常见 —— 不矫正则 OCR/题注按横图读，答非所问）。
    ``normalize=False`` 供调用方已矫正过的路径，避免二次重编码。
    """
    if not data or assets_dir is None:
        return None
    import hashlib

    if normalize:
        data, ext = _normalize_raster_bytes(data, ext, rotate_degrees=rotate_degrees)
    digest = hashlib.sha256(data).hexdigest()[:8]
    suffix = f".{ext.lstrip('.').lower()}" if ext else ".png"
    target = assets_dir / "images" / f"img-{digest}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return f"images/{target.name}"


def _normalize_raster_bytes(
    data: bytes, ext: str, *, rotate_degrees: float = 0.0
) -> tuple[bytes, str]:
    """EXIF 方向矫正 + 可选旋转；失败则原样返回（矢量/损坏图不硬转）。"""
    ext_norm = (ext or "png").lstrip(".").lower()
    if ext_norm in {"emf", "wmf", "svg"}:
        return data, ext_norm
    try:
        import io

        from PIL import Image, ImageOps
    except Exception:  # pragma: no cover
        return data, ext_norm
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image) or image
            degrees = float(rotate_degrees or 0.0) % 360.0
            # PPT 形状旋转：顺时针；Pillow rotate 是逆时针 → 取负
            if degrees:
                # 只对接近直角的旋转做无损近似，斜角也转（expand 防裁切）
                image = image.rotate(-degrees, expand=True)
            buf = io.BytesIO()
            save_ext = ext_norm
            if image.mode not in {"RGB", "L", "RGBA", "P"}:
                image = image.convert("RGB")
                save_ext = "png" if save_ext not in {"jpg", "jpeg", "png", "webp"} else save_ext
            if save_ext in {"jpg", "jpeg"}:
                if image.mode == "RGBA":
                    image = image.convert("RGB")
                image.save(buf, format="JPEG", quality=92)
                return buf.getvalue(), "jpg"
            if save_ext == "webp":
                image.save(buf, format="WEBP")
                return buf.getvalue(), "webp"
            image.save(buf, format="PNG")
            return buf.getvalue(), "png"
    except Exception:
        return data, ext_norm


def _persist_figure(img_rel: str, out_root: Path | None, assets_dir: Path | None) -> str | None:
    """把 MinerU 抽出的图片**复制到持久目录**，返回相对 ``assets_dir`` 的路径。

    为什么必须复制：MinerU 的产出目录是临时目录，解析函数一返回就被删 ——
    只把 ``img_path`` 写进 markdown 会得到一堆**死链**（本仓库实测的 bug）。

    PDF/扫描件里手机竖拍图常带 EXIF Orientation；复制时矫正，与 Office 分支同口径。
    """
    if not img_rel or out_root is None or assets_dir is None:
        return None
    candidate = Path(img_rel)
    source = candidate if candidate.is_absolute() else (out_root / candidate)
    if not source.is_file():
        hits = [p for p in out_root.rglob(candidate.name) if p.is_file()]
        if not hits:
            log.warning("figure_source_missing", img=img_rel)
            return None
        source = hits[0]
    ext = source.suffix.lstrip(".").lower() or "png"
    try:
        raw = source.read_bytes()
    except OSError as exc:
        log.warning("figure_source_unreadable", img=img_rel, error=str(exc)[:80])
        return None
    normalized, ext_norm = _normalize_raster_bytes(raw, ext)
    # 文件名保留 MinerU 原名主干，扩展名跟矫正后的格式走
    stem = source.stem
    target = assets_dir / "images" / f"{stem}.{ext_norm}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(normalized)
    return f"images/{target.name}"


#: markdown 图片引用：```![alt](images/xxx.jpg)```
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _strip_markdown_images(text: str) -> str:
    """去掉 OCR 结果里的 markdown 图片引用，只留**文字**。

    为什么必须剥：MinerU 处理独立图片时会原样回吐
    ``![](images/2b6202af….jpg)``（实测，2026-09-29）。这串东西既不是图里的文字，
    又会被当成"有文本的图片"写进 figure 块的 `content` —— 于是检索里多出一批
    内容是"图片链接"的块，比没有更糟。剥完若只剩空白，就当作"这张图没识别出文字"。
    """
    cleaned = _MD_IMAGE_RE.sub(" ", text or "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip()).strip()


class OcrOutcome(NamedTuple):
    """一次图片 OCR 的结果。三个字段必须分开，全是踩出来的：

    * ``text`` —— 识别出的文字（空串 = 这张图确实没字）；
    * ``attempted`` —— 是否**真的调了** MinerU（小图按尺寸主动跳过时为 False）；
    * ``usable`` —— MinerU 是否**正常工作**（False = 能力不可用，调用方应停止后续尝试）。

    ``attempted`` 单独存在的原因：OCR 的预算是"允许调几次 MinerU"，而 MinerU
    每次要加载模型、实测 **~33 秒/张**。早先只有 `if ocr:` 才扣预算，于是
    "每张都识别不出文字"时预算**永远不减少** —— 一份 92 张图的 PPT 会一张接一张
    跑到天荒地老（实测卡了 4 分钟以上仍在跑、日志里一声不吭）。
    """

    text: str = ""
    attempted: bool = False
    usable: bool = True


def _ocr_figure(image: Path) -> OcrOutcome:
    """对单张图片跑一次 MinerU，取图中文字（让"有实际意义的图片"可被检索）。

    图片本身已落盘可用，所以失败不抛异常 —— 但**必须让调用方知道**（见 :class:`OcrOutcome`），
    否则"模型没装、每张图白跑一趟"与"图里确实没字"分不出来，就是上面那段注释里的故障。

    ⚠️ 实测结论（2026-09-29，模型装好之后）：MinerU 对**独立图片**走版面分析流水线，
    会把整张图判定成一个 figure 区域，回来的是**这张图自己的引用**而不是文字。
    所以本函数默认不启用（`_figure_ocr_budget()` 默认 0），详见那里的说明。
    """
    import os
    import subprocess
    import tempfile

    try:
        minimum = float(os.environ.get("EKBS_FIGURE_OCR_MIN_KB", "8")) * 1024
        if minimum > 0 and image.stat().st_size < minimum:
            # 小图（图标/logo）主动跳过：这是**正常**跳过，不是能力故障，也不消耗预算
            return OcrOutcome()
        binary = _mineru_bin()
    except ParserUnavailable as exc:  # MinerU 没装
        log.warning("figure_ocr_skipped", image=image.name, error=str(exc)[:80])
        return OcrOutcome(attempted=False, usable=False)
    except Exception as exc:  # 文件读不到等
        log.warning("figure_ocr_failed", image=image.name, error=str(exc)[:80])
        return OcrOutcome(attempted=True, usable=False)
    env = _mineru_env()
    try:
        with tempfile.TemporaryDirectory(prefix="ekbs-figure-ocr-") as tmp:
            completed = subprocess.run(
                [binary, "-p", str(image), "-o", tmp],
                capture_output=True, text=True, timeout=120, env=env, check=False,
            )
            if completed.returncode != 0:
                log.warning(
                    "figure_ocr_failed", image=image.name, returncode=completed.returncode,
                    stderr=(completed.stderr or "")[-200:],
                )
                return OcrOutcome(attempted=True, usable=False)
            produced = _find_output(Path(tmp), f"{image.stem}.md")
            if produced is None:
                hits = [p for p in Path(tmp).rglob("*.md") if p.is_file()]
                produced = hits[0] if hits else None
            if produced is None:
                # ⚠️ 退出码 0 但**一个产物都没有**：MinerU 装了但模型不可用
                # （实测本机就是这种：`~/.cache/modelscope` 下模型从未下载成功）。
                log.warning(
                    "figure_ocr_no_output", image=image.name,
                    hint="MinerU 未产出任何文件，OCR 实际不可用（多半是模型未安装）",
                )
                return OcrOutcome(attempted=True, usable=False)
            text = _strip_markdown_images(produced.read_text(encoding="utf-8", errors="replace"))
            return OcrOutcome(text=text, attempted=True, usable=True)
    except Exception as exc:
        log.warning("figure_ocr_failed", image=image.name, error=str(exc)[:80])
        return OcrOutcome(attempted=True, usable=False)


def _find_output(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    direct = directory / name
    if direct.is_file():
        return direct
    # MinerU 2.x 在不同版本里可能多套一层同名目录
    for candidate in directory.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def _blocks_to_document(
    blocks: Sequence[dict],
    path: Path,
    markdown_file: Path | None,
    *,
    out_root: Path | None = None,
    assets_dir: Path | None = None,
) -> ParsedDocument:
    """把 MinerU 的块列表拼成 Markdown，并按 ``page_idx`` 生成页码映射。

    图片块（``type == "image"``）会被**复制到 ``assets_dir``** 并登记成 :class:`ParsedFigure`；
    只给链接不落盘等于没保留（临时目录一删就全丢）。
    """
    lines: list[str] = []
    page_map: list[tuple[int, int, int]] = []
    seen_pages: set[int] = set()
    figures: list[ParsedFigure] = []
    tables: list[ParsedTable] = []
    ocr_budget = _figure_ocr_budget()
    offset = 0
    max_page = 0
    table_seq = 0

    def emit(text: str) -> None:
        nonlocal offset
        lines.append(text)
        offset += len(text) + 1

    for block in blocks:
        page_index = block.get("page_idx")
        page = int(page_index) + 1 if isinstance(page_index, int) else 1
        max_page = max(max_page, page)
        if page not in seen_pages:
            seen_pages.add(page)
            page_map.append((offset, page, page))

        kind = str(block.get("type") or "text")
        text = str(block.get("text") or "").strip()
        if kind == "table":
            body = str(block.get("table_body") or text).strip()
            caption = " ".join(str(c) for c in (block.get("table_caption") or []) if c).strip()
            if body or caption:
                table_seq += 1
                tables.append(
                    ParsedTable(
                        body=body,
                        caption=caption,
                        page=page,
                        table_id=str(table_seq),
                    )
                )
            # 正文只留一行锚点，避免整表再进 parent/child 双索引
            emit(f"[表格 {table_seq}]{(' ' + caption) if caption else ''}".rstrip())
        elif kind == "image":
            img = str(block.get("img_path") or "").strip()
            caption = " ".join(str(c) for c in (block.get("img_caption") or []) if c).strip()
            managed = _persist_figure(img, out_root, assets_dir) if img else None
            if managed is None:
                # 没给 assets_dir（或找不到源图）→ 只能留链接，且**明确告警**：这种链接会失效
                if img and assets_dir is None:
                    log.warning("figure_not_persisted", file=path.name, img=img)
                emit(f"![{caption or '图片'}]({img})" if img else (caption or "（图片）"))
            else:
                ocr = ""
                if ocr_budget > 0:
                    outcome = _ocr_figure(assets_dir / managed)
                    ocr = outcome.text
                    if outcome.attempted:
                        ocr_budget -= 1
                    if not outcome.usable:
                        log.warning(
                            "figure_ocr_disabled_for_document", file=path.name,
                            skipped_after=len(figures),
                            hint="MinerU 不可用，本文件剩余图片不再尝试 OCR（图片本身已落盘）",
                        )
                        ocr_budget = 0
                figures.append(
                    ParsedFigure(
                        name=managed, caption=caption, page=page,
                        ocr_text=ocr, figure_id=Path(managed).stem,
                    )
                )
                # 与 pptx 分支一致：**不要**把 `![](images/…)` 写进 markdown。
                # 图片块只从 ``figures`` 建；链接进正文会被切成 parent/child 并向量化，
                # 检索污染实测见开班典礼第 1 页（整段只有图片链接却双索引）。
                # 正文只留短锚点，方便对照页内有图。
                stem = Path(managed).stem
                emit(f"[图片 {stem[:12]}]{(' ' + caption) if caption else ''}".rstrip())
        elif kind == "equation":
            emit(text if text.startswith("$$") else f"$$ {text} $$")
        else:
            level = block.get("text_level")
            if isinstance(level, int) and 1 <= level <= 6 and text:
                emit("#" * level + " " + text)
            elif text:
                emit(text)

    markdown = "\n".join(lines)
    if not markdown.strip() and markdown_file is not None:
        markdown = markdown_file.read_text(encoding="utf-8", errors="replace")
    return ParsedDocument(
        markdown=markdown,
        engine="mineru",
        source_path=str(path),
        page_map=page_map,
        page_count=max_page or None,
        figures=figures,
        tables=tables,
    )


def page_map_from_pages(pages: Sequence[str]) -> list[tuple[int, int, int]]:
    """把「逐页文本」转成 ``[(char_offset, page_from, page_to), …]``。

    给「按页解析」的引擎（MinerU / PDF）使用：offset 为该页首字符在整篇 Markdown 中的位置。
    """
    page_map: list[tuple[int, int, int]] = []
    offset = 0
    for number, page_text in enumerate(pages, start=1):
        page_map.append((offset, number, number))
        offset += len(page_text) + 1
    return page_map
