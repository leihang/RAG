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
        return ParsedDocument(
            markdown=target.read_text(encoding="utf-8", errors="replace"),
            engine="text",
            source_path=str(target),
        )
    if engine == "docx":
        return _parse_docx(target)
    if engine == "xlsx":
        return _parse_xlsx(target)
    if engine == "doc":
        return _parse_doc(target)
    if engine == "pptx":
        return _parse_pptx(target, assets_dir=assets_dir)
    if engine == "xls":
        return _parse_xls(target)
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


def _parse_docx(path: Path) -> ParsedDocument:
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
    table_seq = 0

    # 按文档顺序交错段落与表格，避免「全文段落完再甩表」打乱阅读/锚点位置
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
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

    return ParsedDocument(
        markdown="\n".join(lines),
        engine="docx",
        source_path=str(path),
        tables=tables,
    )


def _parse_xlsx(path: Path) -> ParsedDocument:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"openpyxl 不可用：{exc}") from exc

    workbook = openpyxl.load_workbook(str(path), data_only=True)
    lines: list[str] = []
    tables: list[ParsedTable] = []
    table_seq = 0
    for sheet in workbook.worksheets:
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
                    page=None,
                    table_id=str(table_seq),
                )
            )
            lines.append(f"[表格 {table_seq}] {sheet.title}".rstrip())
        lines.append("")
    return ParsedDocument(
        markdown="\n".join(lines),
        engine="xlsx",
        source_path=str(path),
        tables=tables,
    )


def _cell_text(value: object) -> str:
    """单元格 → 文本；整数型 float 去掉多余小数（xlrd 会把 1 读成 1.0）。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value).strip()


def _parse_doc(path: Path) -> ParsedDocument:
    """旧版 `.doc`（Word 97-2003）—— 用 **macOS 自带 `textutil`** 转成 docx 后复用 docx 分支。

    为什么是"转换"而不是"直接解析"：`.doc` 是 OLE 复合文档，Python 侧没有可靠的纯实现
    （`olefile` 只能读容器，取正文要 antiword / catdoc / LibreOffice —— 本机都没有）。
    而 macOS 自带 `textutil`，零依赖，实测中文完整（15 段全部读出）。
    **不转就会落到 text 分支把二进制读成乱码**（实测 24 KB 的 .doc 读出 24450 字符乱码）。

    非 macOS（没有 `textutil`）时**明确报错**，不静默降级。
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
        parsed = _parse_docx(converted)
    # 引擎名保留 doc，便于日志/审计区分"走了旧格式转换"
    return ParsedDocument(
        markdown=parsed.markdown,
        engine="doc",
        source_path=str(path),
        page_map=parsed.page_map,
        page_count=parsed.page_count,
        figures=parsed.figures,
        tables=parsed.tables,
    )


def _parse_pptx(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.pptx` —— 用 `python-pptx` 按页抽文本、表格**与图片**。

    为什么必须专门实现：`.pptx` 是 zip 容器，落到 text 分支会把二进制当 UTF-8 读
    （实测 142 MB 的 pptx 会变成一大坨乱码，还白吃内存）。原先它只在白名单里、
    没有解析分支，属于"放进来再变垃圾"。

    **图片**：原先只认 ``has_text_frame`` / ``has_table``，对 ``PICTURE`` 形状视而不见 ——
    于是"整页都是图"的汇报 PPT 抽出来只有几行标题（实测：一份 22.5 MB、38 页、
    含 142 张图（92 张唯一）的工程汇报，只抽出 3151 字、``figures=0``）。
    这里把图片字节落盘并登记成 :class:`ParsedFigure`，后续由分块器决定要不要进索引
    （有题注/OCR 文字才进，见 ``chunker.chunk_markdown``）。

    ⚠️ **不往 markdown 里写 ``![](images/..)`` 链接**：图片块是从 ``figures`` 建的，
    不看链接；而链接会原样进父块/子块的正文并被算进向量（实测 ``chunk_markdown``
    不做剥离）。142 张图 = 142 条链接噪声，检索质量只会更差。
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-pptx 不可用：{exc}") from exc

    presentation = Presentation(str(path))
    pages: list[str] = []
    figures: list[ParsedFigure] = []
    tables: list[ParsedTable] = []
    table_seq = 0
    ocr_budget = _figure_ocr_budget()
    seen_images: set[str] = set()
    skipped_pictures = 0

    for number, slide in enumerate(presentation.slides, start=1):
        lines: list[str] = [f"## 第 {number} 页"]
        # 本页新增的"无文字"图：[(figures 下标, 字节数)]，页末挑最大的一张补页标题
        textless: list[tuple[int, int]] = []
        for shape in _iter_shapes(slide.shapes):
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    blob = image.blob
                    ext = image.ext
                except Exception as exc:
                    # 链接式（未嵌入）图片取不到字节：跳过并计数，不整篇失败
                    skipped_pictures += 1
                    log.warning("pptx_picture_unreadable", slide=number, error=str(exc)[:80])
                    continue
                import hashlib

                digest = hashlib.sha256(blob).hexdigest()
                if digest in seen_images:
                    # 同一张图重复出现（页眉/logo：实测最多重复 33 次）只登记一次，
                    # 否则会造出几十个内容相同的图片块
                    continue
                seen_images.add(digest)
                managed = _persist_figure_bytes(blob, ext, assets_dir)
                if managed is None:
                    # 没给 assets_dir（例如直接调 `parse_file` 做预览）→ 只能不落盘
                    log.warning("pptx_picture_not_persisted", slide=number, file=path.name)
                    continue
                caption = _alt_text(shape)
                ocr = ""
                if ocr_budget > 0:
                    # 预算管的是"允许调几次 MinerU"（实测 ~33s/次），**不是"识别成功几次"**：
                    # 只有按尺寸主动跳过的小图才不扣。早先按"有文字才扣"，于是每张都识别不出
                    # 文字时预算永不减少 —— 一份 92 张图的 PPT 会一张接一张跑到天荒地老。
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
                        name=managed, caption=caption, page=number,
                        ocr_text=ocr, figure_id=Path(managed).stem,
                    )
                )
                if not caption and not ocr:
                    textless.append((len(figures) - 1, len(blob)))
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
                            body=body,
                            caption="",
                            page=number,
                            table_id=str(table_seq),
                        )
                    )
                    lines.append(f"[表格 {table_seq}]")
        notes = getattr(slide, "notes_slide", None)
        if notes is not None:
            note_text = (notes.notes_text_frame.text or "").strip()
            if note_text:
                lines.append(f"（备注）{note_text}")

        # **兜底检索文本**：一张图如果有题注或 OCR 文字，它自己就能被检索；
        # 都没有时（实测量子：92 张图里 86 张）它此前**完全不进索引** —— 搜什么都搜不到它。
        # 这里给本页**最大的那张**无文字图补上页标题（幻灯片的第一行文字），
        # 于是"按主题/按页"提问能召回这一页的图。只补一张，避免同一页多张图造出
        # 一批几乎相同的块把检索结果挤满（chunker 当初就是为这个才跳过无文字图片的）。
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

    if figures or skipped_pictures:
        log.info(
            "pptx_figures_extracted", file=path.name, figures=len(figures),
            unreadable=skipped_pictures, ocr_budget=_figure_ocr_budget(),
        )
    markdown = "\n\n".join(pages)
    return ParsedDocument(
        markdown=markdown, engine="pptx", source_path=str(path),
        page_map=page_map_from_pages(pages), page_count=len(pages),
        figures=figures,
        tables=tables,
    )


def _parse_xls(path: Path) -> ParsedDocument:
    """旧版 `.xls` —— 用 `xlrd` 按工作表抽成 Markdown 表格（与 xlsx 分支同口径）。

    注意：`xlrd` 2.x **只支持旧版 `.xls`**（xlsx 由 openpyxl 负责），两者正好互补。
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
    table_seq = 0
    for sheet in book.sheets():
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
                    page=None,
                    table_id=str(table_seq),
                )
            )
            lines.append(f"[表格 {table_seq}] {sheet.name}".rstrip())
        lines.append("")
    return ParsedDocument(
        markdown="\n".join(lines),
        engine="xls",
        source_path=str(path),
        tables=tables,
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


def _persist_figure_bytes(data: bytes, ext: str, assets_dir: Path | None) -> str | None:
    """把**容器内的**图片字节落盘到持久目录，返回相对 ``assets_dir`` 的路径。

    与 :func:`_persist_figure` 的区别：那个用于 MinerU 已经把图片写在磁盘上的情形，
    这个用于 pptx/docx 这类**图片只存在于 zip 容器里**的格式 —— 磁盘上从来没有过
    独立文件，不主动写出来就等于丢图。

    文件名用**内容摘要**（``img-<sha8>.<ext>``）而不是序号：
    实测这份 PPT 里同一张图最多出现 **33 次**（页眉 logo），按序号命名会落 33 份
    完全相同的文件；按内容命名天然去重，且"改了图再导"不会覆盖旧图。
    """
    if not data or assets_dir is None:
        return None
    import hashlib

    digest = hashlib.sha256(data).hexdigest()[:8]
    suffix = f".{ext.lstrip('.').lower()}" if ext else ".png"
    target = assets_dir / "images" / f"img-{digest}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return f"images/{target.name}"


def _persist_figure(img_rel: str, out_root: Path | None, assets_dir: Path | None) -> str | None:
    """把 MinerU 抽出的图片**复制到持久目录**，返回相对 ``assets_dir`` 的路径。

    为什么必须复制：MinerU 的产出目录是临时目录，解析函数一返回就被删 ——
    只把 ``img_path`` 写进 markdown 会得到一堆**死链**（本仓库实测的 bug）。
    """
    import shutil

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
    target = assets_dir / "images" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
    return f"images/{source.name}"


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
                emit(f"![{caption or '图片'}]({managed})")
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
