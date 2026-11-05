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

import html
import re
import subprocess
from datetime import date
from dataclasses import dataclass, field
from html.parser import HTMLParser
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
    #: 表体是**原生 Word 版式**（HTML 带 colspan/rowspan）。清洗阶段（表修复/续页合并）
    #: 会按行重拼 HTML、丢掉跨度 —— 这类表必须跳过重拼，否则合并单元格被拍平。
    preserve_layout: bool = False


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
    #: 页眉/页脚里的**文档管控信息**（文件名称/文件编号/生效日期/版本/页数/编制/审核/批准人…）。
    #: 这些是**元数据**，不是正文：不再作为「文件信息」章节写进 markdown（否则每篇制度
    #: 都会生成一段高度同质的正文块，入库后互相竞争）。入库时用于填
    #: ``ekbs_document.effective_date / expire_date``，侧车 JSON 也保留一份供人工复核。
    header_meta: dict[str, str] = field(default_factory=dict)


def parse_markdown(text: str, *, engine: str = "text") -> ParsedDocument:
    return ParsedDocument(markdown=text, engine=engine)


from ekbs.ingest.code_chunker import CODE_SUFFIXES as _CODE_SUFFIXES  # noqa: E402 —— 延迟导入：parser 被 chunker 等模块反引，放顶层会拉起重依赖

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
    ".html": "html",
    ".htm": "html",
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


#: 电子表格来源 —— 一行就是一条记录，RAGFlow 的「表格一行一块」口径只对它们成立。
#: 为什么**不收 ``csv``**：`SUFFIX_ENGINES` 里没有 ``.csv``，它落到 ``text`` 引擎的
#: 纯文本分支，解析产物里根本不产生 ``ParsedTable`` —— 给一个不会产出表格的来源开
#: 行级分块没有意义（真要支持 csv 表格，先让解析层把它当表解析，再来改这里）。
SPREADSHEET_SUFFIXES: frozenset[str] = frozenset({"xlsx", "xls", "xlsm"})


def is_spreadsheet_source(path: str) -> bool:
    """来源是否是**电子表格**（一行=一条记录）—— 决定表块按行还是按行组切。

    **默认 False（行组叶子）是刻意的安全默认**：行级分块只对「行是记录」的电子表格
    成立；套到 Word/PDF/PPT 的制度表格上会把每行切成 55~115 token 的碎片（实测
    doc 205：41 块 / 36 个表块，表 6 的 962 token 被拆成 18 叶）。调用方漏传来源时
    必须回到行组，不能因为忘传参数就退化成碎块。
    """
    return normalize_suffix(str(path)) in SPREADSHEET_SUFFIXES


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
    if engine == "html":
        return _parse_html(target, assets_dir=assets_dir)
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


class _HtmlStructuredParser(HTMLParser):
    """把 HTML 转成"带结构的纯文本"：保留标题层级/列表/表格，剥掉其余标签。

    为什么需要它（实测缺口）：`.html` 原先没有专门解析器，落到 `text` 引擎后
    **整行标签原样入库**（实测 `content` 里就是 `<html><body><h1>交接班制度</h1>…`），
    既污染检索词项又占 token。这里用标准库解析，**不引入新依赖**。
    """

    #: 这些标签里的内容整段丢弃（脚本/样式不是正文）
    #: `<head>` 整段跳过（title/meta/link/style/script 都不是正文）
    _SKIP = {"head", "script", "style", "noscript", "template"}
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    #: 这些标签起新的一行
    _BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._in_cell = False
        self._row: list[str] = []
        self._table: list[list[str]] | None = None
        self._cell: list[str] = []

    # -- 小工具 -------------------------------------------------------
    @staticmethod
    def _normalize(text: str) -> str:
        """归一化空白：`&nbsp;` 解出来的是 **NBSP(\xa0)**，留着他会导致
        "费用\xa0100" 这类文本在检索里匹配不上（词项被切断），所以换成普通空格并压掉连续空白。"""
        return re.sub(r"[ \t\xa0]+", " ", text).strip()

    def _flush(self, *, level: int = 0) -> None:
        text = self._normalize("".join(self._buffer))
        self._buffer = []
        if not text:
            return
        self.lines.append(("#" * level + " " + text) if level else text)

    # -- HTMLParser 回调 ---------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._HEADINGS:
            self._flush()
            self._heading_level = self._HEADINGS[tag]
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self._HEADINGS:
            self._flush(level=self._heading_level or 1)
            self._heading_level = 0
        elif tag in {"td", "th"}:
            self._row.append(self._normalize("".join(self._cell)))
            self._cell = []
            self._in_cell = False
        elif tag == "tr" and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(list(self._row))
            self._row = []
        elif tag == "table":
            if self._table:
                self.tables.append(self._table)
            self._table = None
            self._flush()
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._in_cell:
            # 单元格文字只进表格：否则会**同时**出现在正文与表格里（实测重复）
            self._cell.append(data)
            return
        self._buffer.append(data)

    def close(self) -> None:  # noqa: D102 - 收尾
        super().close()
        self._flush()


def _parse_html(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.html` / `.htm` —— 剥标签，保留标题、列表与表格结构。

    输出与其它解析器同一契约：``markdown`` 里表格用 ``[表格 N]`` 占位，
    真实表格进 ``tables``（序列化由 pipeline 统一做）。
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - 文件读不到
        raise ParserUnavailable(f"读取 .html 失败：{exc}") from exc

    parser = _HtmlStructuredParser()
    parser.feed(raw)
    parser.close()

    lines = list(parser.lines)
    tables: list[ParsedTable] = []
    for seq, rows in enumerate(parser.tables, start=1):
        body = _rows_to_pipe_markdown(rows)
        if not body:
            continue
        caption = rows[0][0] if rows and rows[0] else ""
        tables.append(ParsedTable(body=body, caption=caption, page=None, table_id=str(seq)))
        lines.append(f"[表格 {seq}]")
    figures: list[ParsedFigure] = []  # 内联 <img> 暂不落盘（本地/相对路径不可信）
    markdown = "\n".join(lines).strip() + "\n" if lines else ""
    return ParsedDocument(
        markdown=markdown, engine="html", source_path=str(path),
        figures=figures, tables=tables,
    )


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


#: 页眉「页数：1 / 2」→ 总页数；用于补 ``page_count``（Word 轻量解析无真实分页时）
_DOCX_HEADER_PAGE_TOTAL = re.compile(
    r"页\s*数\s*[：:]\s*\d+\s*/\s*(\d+)", re.IGNORECASE
)
#: 页眉行拆键值：全角/半角分号；键值之间全角/半角冒号
_HEADER_KV_SPLIT = re.compile(r"[；;]")
_HEADER_KV = re.compile(r"^\s*([^：:]{1,24})\s*[：:]\s*(.+?)\s*$")
#: 中文/短横线/点号/斜杠日期：2025年1月1日 / 2025-1-1 / 2025.1.1 / 2025/1/1
_DATE_PATTERN = re.compile(r"(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?")


def parse_cn_dates(text: str) -> list[str]:
    """抽出一段文字里的全部日期并归一化为 ``YYYY-MM-DD``（无效日期跳过，保序去重）。"""
    out: list[str] = []
    for match in _DATE_PATTERN.finditer(str(text or "")):
        year, month, day = (int(part) for part in match.groups())
        try:
            date(year, month, day)
        except ValueError:
            continue
        value = f"{year:04d}-{month:02d}-{day:02d}"
        if value not in out:
            out.append(value)
    return out


def parse_cn_date(text: str) -> str | None:
    """取文字里的**第一个**日期（``YYYY-MM-DD``）；没有/无效返回 ``None``。"""
    dates = parse_cn_dates(text)
    return dates[0] if dates else None


def parse_header_meta(lines: Sequence[str]) -> dict[str, str]:
    """把页眉/页脚文字解析成 ``{键: 值}``（键去掉空格，保留中文原名）。"""
    meta: dict[str, str] = {}
    for line in lines:
        for part in _HEADER_KV_SPLIT.split(str(line or "")):
            match = _HEADER_KV.match(part.strip())
            if not match:
                continue
            key = re.sub(r"[\s\u3000]+", "", match.group(1))
            value = match.group(2).strip()
            if key and value and key not in meta:
                meta[key] = value
    return meta


#: 页眉装饰小图阈值：短边与长边都偏小时视为 logo，不进正文 figure
_DOCX_DECORATIVE_MAX_EDGE = 120


def _collapse_docx_row_cells(cells: Sequence[str]) -> list[str]:
    """页眉表常合并单元格，相邻重复值压成一条（``文件名称|书名|书名`` → ``文件名称|书名``）。"""
    out: list[str] = []
    for raw in cells:
        text = (raw or "").strip().replace("\n", " ")
        if not text:
            continue
        if out and out[-1] == text:
            continue
        out.append(text)
    return out


def _logical_row_cells(row: Any) -> list[str]:
    """读一行的**逻辑**单元格：水平合并（gridSpan）的重复网格只算一个。

    ⚠️ 不能直接 ``[cell.text for cell in row.cells]``：python-docx 对合并单元格会按
    **网格列**重复返回同一个 ``_Cell``。实测《客户满意度调查表》里 4 列合并的
    「申请装修内容…」「服务评价…」被复制成 4 份 —— 表格重度重复且无从去重
    （真实语料里相邻两列恰好同值的情况也存在，比如并排的 ☑/□，不能按文本去重）。

    做法：用底层 ``<w:tc>`` 元素身份去重。同一个 tc 的多个网格位只取一次；
    不同 tc 即使文本相同也照常保留。docx / pptx 的合并表结构一致，两处共用。

    去重后**按网格宽度补空**（合并文本占第一格、其余补 ``""``）：这样一行仍覆盖原来的
    列数，序列化时能识别「整行合并 = 标题行」，也不会让后面的行错列。
    """
    texts: list[str] = []
    spans: list[int] = []
    position: dict[int, int] = {}
    for cell in row.cells:
        tc = getattr(cell, "_tc", None)
        key = id(tc) if tc is not None else None
        if key is not None and key in position:
            spans[position[key]] += 1
            continue
        if key is not None:
            position[key] = len(texts)
        if tc is not None and _is_docx_element(tc):
            raw_text = _docx_cell_text(tc, _docx_qn())
        else:
            raw_text = getattr(cell, "text", "") or ""
        texts.append(raw_text.strip().replace("\n", " "))
        spans.append(1)

    cells: list[str] = []
    for text, span in zip(texts, spans):
        cells.append(text)
        cells.extend([""] * (span - 1))
    return cells


#: Word 符号字体（``w:sym``）里常见的复选框/勾 → Unicode。
#: 符号存在 ``w:sym``、**不在 ``w:t``**；只读 ``w:t`` 会整段丢失 —— 实测
#: 《客户满意度调查表》18 个 Wingdings 复选框全部消失，变成
#: "一至三次三至五次五次以上"（用户反馈"符号被解析掉了"）。
_DOCX_SYMBOLS: dict[tuple[str, int], str] = {
    ("wingdings", 0xA8): "□",
    ("wingdings", 0xA1): "☐",
    ("wingdings", 0xFE): "☑",
    ("wingdings", 0xFD): "☒",
    ("wingdings", 0xFC): "✓",
    ("symbol", 0xD8): "√",
}


def _docx_symbol_char(sym: Any, qn: Any) -> str:
    """``w:sym`` → 可读字符；未收录的 PUA 码位返回空串（不伪造字符）。"""
    font = str(sym.get(qn("w:font")) or "").strip().lower()
    raw = str(sym.get(qn("w:char")) or "").strip()
    if not raw:
        return ""
    try:
        code = int(raw, 16)
    except ValueError:
        return ""
    return _DOCX_SYMBOLS.get((font, code)) or _DOCX_SYMBOLS.get((font, code & 0xFF)) or ""


def _docx_element_text(element: Any, qn: Any) -> str:
    """按文档顺序读一段 XML 的文字：``w:t`` 取文本、``w:sym`` 还原符号。

    只读 ``w:t`` 会丢掉符号字体里的复选框/勾/箭头（Word 把它们存成 ``w:sym``）。
    """
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:sym"):
            parts.append(_docx_symbol_char(node, qn))
        elif tag == qn("w:tab"):
            parts.append(" ")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _docx_qn() -> Any:
    """python-docx 的 qn（延迟导入，避免模块级依赖）。"""
    from docx.oxml.ns import qn

    return qn


def _is_docx_element(tc: Any) -> bool:
    tag = str(getattr(tc, "tag", ""))
    return "wordprocessingml" in tag


def _docx_cell_text(cell: Any, qn: Any) -> str:
    """单元格文本：**逐段**取文、段间以「；」分隔。

    为什么不能整格拼所有 ``w:t``：Word 一个单元格里常放多个编号段
    （实测「…现场使用情况检查」+「2、总水表…」），拼成「…检查2、总水表…」
    后编号与上一条粘连，下游按行切表时无处可切。段内仍走
    :func:`_docx_element_text`（保留 ``w:sym`` / ``w:tab`` / ``w:br`` 语义）。
    """
    parts: list[str] = []
    for child in cell.iterchildren():
        text = _docx_element_text(child, qn).strip()
        if text:
            parts.append(text)
    return "；".join(parts)


#: OOXML ``w:`` 命名空间的 Clark 记法前缀。用字符串常量而不是 python-docx 的
#: ``qn``：numbering.xml 直接从 zip 读（``xml.etree``），不必先 import docx。
_DOCX_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class _DocxNumberingLevel(NamedTuple):
    """``word/numbering.xml`` 里一层的编号定义。"""

    start: int
    num_fmt: str
    lvl_text: str
    suffix: str


@dataclass
class _DocxNumberingList:
    """一个 ``numId`` 的编号定义。

    不同 ``numId`` 即使共用同一个 abstractNum 也**独立计数**（Word 里就是不同列表，
    实测本文件表 3 单元格里 numId=2/3/4 各从 1 起）。
    """

    levels: dict[int, _DocxNumberingLevel]
    start_overrides: dict[int, int] = field(default_factory=dict)


def _docx_val(parent: Any, tag: str, default: str = "") -> str:
    """读 ``<w:tag w:val="…"/>`` 子元素的值（numbering.xml 的通用形状）。"""
    node = parent.find(_DOCX_W + tag)
    if node is None:
        return default
    return str(node.get(_DOCX_W + "val") or default)


def _docx_int(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _read_docx_numbering(path: Path) -> dict[str, _DocxNumberingList]:
    """读 docx 的 ``word/numbering.xml``：``numId`` → 各层 (start, numFmt, lvlText, suff)。

    为什么从 zip 直接读：``document.part.numbering_part`` 在不同 python-docx 版本
    行为不一，且无编号表时会抛异常；zip 里缺失/坏掉只该是"没有编号"，不能让解析
    失败 —— ``log.debug`` 后返回 ``{}``，正文原样保留。
    """
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/numbering.xml")
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        log.debug("docx_numbering_unavailable", file=path.name, error=str(exc))
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.debug("docx_numbering_unparsable", file=path.name, error=str(exc))
        return {}

    abstracts: dict[str, dict[int, _DocxNumberingLevel]] = {}
    for abstract in root.findall(_DOCX_W + "abstractNum"):
        abstract_id = str(abstract.get(_DOCX_W + "abstractNumId") or "")
        if not abstract_id:
            continue
        levels: dict[int, _DocxNumberingLevel] = {}
        for lvl in abstract.findall(_DOCX_W + "lvl"):
            ilvl = _docx_int(lvl.get(_DOCX_W + "ilvl"))
            if ilvl is None:
                continue
            levels[ilvl] = _DocxNumberingLevel(
                start=_docx_int(_docx_val(lvl, "start", "1")) or 1,
                num_fmt=_docx_val(lvl, "numFmt", "decimal").lower(),
                lvl_text=_docx_val(lvl, "lvlText"),
                suffix=_docx_val(lvl, "suff", "tab").lower(),
            )
        abstracts[abstract_id] = levels

    numbering: dict[str, _DocxNumberingList] = {}
    for num in root.findall(_DOCX_W + "num"):
        num_id = str(num.get(_DOCX_W + "numId") or "")
        levels = abstracts.get(_docx_val(num, "abstractNumId"))
        if not num_id or not levels:
            continue
        overrides: dict[int, int] = {}
        for override in num.findall(_DOCX_W + "lvlOverride"):
            ilvl = _docx_int(override.get(_DOCX_W + "ilvl"))
            start = _docx_int(_docx_val(override, "startOverride"))
            if ilvl is not None and start is not None:
                overrides[ilvl] = start
        numbering[num_id] = _DocxNumberingList(levels=levels, start_overrides=overrides)
    return numbering


def _format_docx_number(value: int, num_fmt: str) -> str:
    """计数器 → 编号文字；未支持的 numFmt 退回十进制（丢编号比格式不对更糟）。"""
    if num_fmt == "decimalzero":
        return f"{value:02d}"
    return str(value)


def _docx_number_label(
    definition: _DocxNumberingList, counters: dict[int, int], ilvl: int
) -> str:
    """按 Word 语义推进计数并渲染编号；返回 ``""`` 表示该层没有可见编号。"""
    level = definition.levels.get(ilvl)
    if level is None or level.num_fmt in {"bullet", "none"} or not level.lvl_text:
        return ""
    start = definition.start_overrides.get(ilvl, level.start)
    current = counters.get(ilvl)
    counters[ilvl] = start if current is None else current + 1
    # 较浅层级递增时，更深层级的编号从头开始（Word 的列表语义）
    for deeper in [key for key in counters if key > ilvl]:
        del counters[deeper]

    def counter_at(index: int) -> int:
        if index not in counters:
            nested = definition.levels.get(index)
            counters[index] = definition.start_overrides.get(
                index, nested.start if nested is not None else 1
            )
        return counters[index]

    label = level.lvl_text
    for index in range(9):
        placeholder = f"%{index + 1}"
        if placeholder not in label:
            continue
        nested = definition.levels.get(index)
        num_fmt = nested.num_fmt if nested is not None else "decimal"
        label = label.replace(placeholder, _format_docx_number(counter_at(index), num_fmt))
    return label


def _insert_docx_numbering_run(paragraph: Any, label: str, separator: str, qn: Any) -> None:
    """在段首（``w:pPr`` 之后）插入编号 run —— 与 Word 把编号显示在正文前一致。"""
    run = paragraph.makeelement(qn("w:r"), {})
    node = run.makeelement(qn("w:t"), {})
    node.text = label + separator
    # 尾随空格不能被 XML 处理吃掉（"5. " 靠它和正文分开）
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.append(node)
    p_pr = paragraph.find(qn("w:pPr"))
    if p_pr is None:
        paragraph.insert(0, run)
    else:
        paragraph.insert(list(paragraph).index(p_pr) + 1, run)


def _expand_docx_numbering(
    document: Any, qn: Any, numbering: dict[str, _DocxNumberingList]
) -> int:
    """把 ``w:numPr`` 的自动编号展开成段首文字（返回展开的段落数）。

    Word 把列表编号存在 numbering.xml + 段落的 ``w:numPr`` 里，正文 ``w:t`` 中
    **没有编号** —— 不展开就等于丢编号。实测《XJWY-GC-12》的「工作程序和要求」
    实为第 5 章（abstractNum start=5），解析稿没有「5.」，整章下沉到第 4 章下。

    计数按 ``numId`` 隔离；原文手打同样编号的段落（如字面「3、」）跳过不重复前置。
    """
    if not numbering:
        return 0
    counters: dict[str, dict[int, int]] = {}
    expanded = 0
    for paragraph in document.element.body.iter(qn("w:p")):
        p_pr = paragraph.find(qn("w:pPr"))
        num_pr = p_pr.find(qn("w:numPr")) if p_pr is not None else None
        if num_pr is None:
            continue
        num_id_el = num_pr.find(qn("w:numId"))
        num_id = num_id_el.get(qn("w:val")) if num_id_el is not None else ""
        ilvl_el = num_pr.find(qn("w:ilvl"))
        ilvl = _docx_int(ilvl_el.get(qn("w:val")) if ilvl_el is not None else None)
        num_id = str(num_id or "").strip()
        definition = numbering.get(num_id)
        # numId=0 是 OOXML 里「本段取消编号」的写法
        if not num_id or num_id == "0" or ilvl is None or definition is None:
            continue
        existing = _docx_element_text(paragraph, qn).strip()
        label = _docx_number_label(definition, counters.setdefault(num_id, {}), ilvl)
        if not label:
            continue
        if not existing:
            continue  # 空段只推进计数，不往正文塞一个孤零零的编号
        if existing.startswith(label):
            continue  # 原文已手打同样编号，不重复前置
        separator = "" if definition.levels[ilvl].suffix == "nothing" else " "
        _insert_docx_numbering_run(paragraph, label, separator, qn)
        expanded += 1
    return expanded


def _docx_table_html(table: Any) -> str:
    """docx 表 → 带 ``colspan``/``rowspan`` 的 HTML —— **所有 Word 表格的统一口径**。

    为什么不再摊平成管道表：Word 里"整行合并"的单元格（合并标题、说明行、评价区）
    摊平后要么按列重复文本、要么变成"1 格有字 + 3 格空"，复核页看到的版式与原件
    不一致。直接输出带合并属性的 HTML，复核页 iframe 渲染即还原原表；下游
    ``parse_table_html`` 只取单元格文本、忽略跨度，检索口径不受影响（每格只出现一次）。

    合并语义（OOXML）：
    * ``w:gridSpan`` —— 水平合并，跨 N 列；
    * ``w:vMerge val="restart"`` —— 纵向合并起始格，向下数 ``continue`` 行得到 rowspan；
    * ``w:vMerge``（无 val 或 ``continue``）—— 纵向合并延续格，内容在起始格，跳过。
    """
    from docx.oxml.ns import qn

    grid: list[list[dict[str, Any]]] = []
    for row in table.rows:
        cells: list[dict[str, Any]] = []
        for tc in row._tr.tc_lst:
            text = _docx_cell_text(tc, qn).strip().replace("\n", " ")
            span = 1
            vmerge = ""
            tc_pr = tc.tcPr
            if tc_pr is not None:
                span_el = tc_pr.find(qn("w:gridSpan"))
                if span_el is not None:
                    span = max(1, int(span_el.get(qn("w:val")) or 1))
                vmerge_el = tc_pr.find(qn("w:vMerge"))
                if vmerge_el is not None:
                    vmerge = str(vmerge_el.get(qn("w:val")) or "continue")
            cells.append({"text": text, "span": span, "vmerge": vmerge})
        grid.append(cells)

    rows_html: list[str] = []
    for row_index, cells in enumerate(grid):
        parts: list[str] = []
        for cell_index, cell in enumerate(cells):
            if cell["vmerge"] == "continue":
                continue  # 被上方 restart 的 rowspan 覆盖
            rowspan = 1
            if cell["vmerge"] == "restart":
                for below in range(row_index + 1, len(grid)):
                    row = grid[below]
                    same = row[cell_index] if cell_index < len(row) else None
                    if same is None or same["vmerge"] != "continue":
                        break
                    rowspan += 1
            attrs = ""
            if cell["span"] > 1:
                attrs += f' colspan="{cell["span"]}"'
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            tag = "th" if row_index == 0 else "td"
            parts.append(f"<{tag}{attrs}>{html.escape(str(cell['text']))}</{tag}>")
        if parts:
            rows_html.append("<tr>" + "".join(parts) + "</tr>")
    return ("<table>" + "".join(rows_html) + "</table>") if rows_html else ""


def _format_docx_header_row(cells: Sequence[str]) -> str | None:
    """把一行页眉表格式化成可检索的键值串。

    合并单元格后行长常为奇数（``文件名称|书名|文件编号：X…``）：
    已含冒号的格子当整段键值；其余按「标签 → 值」成对吞掉。
    """
    cleaned = _collapse_docx_row_cells(cells)
    if not cleaned:
        return None
    atoms: list[str] = []
    index = 0
    while index < len(cleaned):
        current = cleaned[index]
        if "：" in current or ":" in current:
            atoms.append(current)
            index += 1
            continue
        if index + 1 < len(cleaned):
            atoms.append(f"{current}：{cleaned[index + 1]}")
            index += 2
            continue
        atoms.append(current)
        index += 1
    return "；".join(atoms)


def _docx_header_meta(document: Any) -> tuple[list[str], int | None]:
    """抽取页眉/页脚**文字**一次（制度文件的文件名、编号、版本、页数等）。

    方案 ``20`` §2 步 1「页眉页脚剔除」针对的是 **PDF 每页重复出现的装饰行**；
    Word 里管控信息往往**只住在 header 表**、正文没有 —— 原先只抽页眉图、不抽字，
    等于把文件编号/生效日期洗掉。这里抽一次挂到文首「文件信息」，不按页重复注入。
    """
    lines: list[str] = []
    seen: set[str] = set()
    page_total: int | None = None
    parts: list[Any] = []
    for section in getattr(document, "sections", ()) or ():
        for attr in (
            "header",
            "footer",
            "first_page_header",
            "first_page_footer",
            "even_page_header",
            "even_page_footer",
        ):
            part = getattr(section, attr, None)
            if part is not None:
                parts.append(part)
    for part in parts:
        for table in getattr(part, "tables", ()) or ():
            for row in table.rows:
                try:
                    cells = _logical_row_cells(row)
                except Exception:  # noqa: BLE001 — 坏表行跳过
                    continue
                line = _format_docx_header_row(cells)
                if not line or line in seen:
                    continue
                seen.add(line)
                lines.append(line)
                match = _DOCX_HEADER_PAGE_TOTAL.search(line)
                if match:
                    page_total = int(match.group(1))
        for paragraph in getattr(part, "paragraphs", ()) or ():
            text = (getattr(paragraph, "text", None) or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            lines.append(text)
            match = _DOCX_HEADER_PAGE_TOTAL.search(text)
            if match:
                page_total = int(match.group(1))
    return lines, page_total


def _docx_raster_is_decorative(blob: bytes, *, max_edge: int = _DOCX_DECORATIVE_MAX_EDGE) -> bool:
    """页眉 logo 一类小图：进 figure 只会污染检索（实测设备编号指导书 89×26）。"""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # pragma: no cover
        return False
    try:
        with Image.open(BytesIO(blob)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001
        return False
    return max(int(width), int(height)) < int(max_edge)


@dataclass
class _DocxState:
    """`.docx` 解析过程中的可变状态（把巨型函数的状态收拢，便于分阶段抽取）。"""

    assets_dir: Path | None
    log_file: str
    lines: list[str] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)
    seen_images: set[str] = field(default_factory=set)
    ocr_budget: list[int] = field(default_factory=lambda: [_figure_ocr_budget()])
    #: (figure_index, blob_size, nearby_text) —— 无字图用上文题注，避免只剩「图1」搜不到「二维码」
    textless: list[tuple[int, int, str]] = field(default_factory=list)
    recent_texts: list[str] = field(default_factory=list)
    table_seq: int = 0


def _docx_nearby_caption(recent_texts: list[str]) -> str:
    """优先带「二维码/扫描/下载」的近邻段，再退到 APP/图片，最后取最近正文。"""
    for keys in (
        ("二维码", "二维碼", "扫码", "扫描", "下载"),
        ("图片", "图示", "示意图"),
        ("APP", "App", "app"),
    ):
        for text in reversed(recent_texts):
            if any(k in text for k in keys):
                return text
    return recent_texts[-1] if recent_texts else ""


def _register_docx_figure(state: _DocxState, blob: bytes, ext: str, caption: str) -> None:
    """落盘一张图；无字图先记账，等全部抽完再统一挂题注。"""
    added = _register_figure_blob(
        blob, ext,
        assets_dir=state.assets_dir, figures=state.figures, seen=state.seen_images,
        caption=caption, page=None, ocr_budget=state.ocr_budget,
        log_file=state.log_file,
    )
    if added and not state.figures[-1].caption and not state.figures[-1].ocr_text:
        state.textless.append(
            (len(state.figures) - 1, len(blob), _docx_nearby_caption(state.recent_texts))
        )


def _ingest_docx_images(element: Any, document: Any, state: _DocxState) -> None:
    for blob, ext, caption in _docx_images_in_element(element, document):
        _register_docx_figure(state, blob, ext, caption)


#: 表题注段：「表1-年度能耗管理计划编制及实施要求」这类紧邻表格的**普通正文段**。
#: Word 里题注不一定是 caption 样式，只靠样式识别会丢；不识别则表块名与原文表题错位。
_DOCX_TABLE_CAPTION = re.compile(r"^表\s*\d+")
#: 题注段长度上限：超过这个长度的「表…」段落大概率是正文（说明/引用），不该当表名。
_DOCX_TABLE_CAPTION_MAX_LEN = 60


def _walk_docx_body(document: Any, state: _DocxState, qn: Any) -> None:
    """按文档顺序交错段落 / 表格 / 图片，避免「全文段落完再甩表/图」打乱阅读位置。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    # 表格前最近的非空正文段（题注候选）；被别的表格或正文段打断就清空
    pending_caption = ""
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            # 用 sym-aware 取文（paragraph.text 会丢掉 w:sym 里的符号）
            text = _docx_element_text(child, qn).strip()
            # 先记上文再落图：空段里的二维码才能挂上「扫描二维码下载」
            if text:
                state.recent_texts.append(text)
                del state.recent_texts[:-6]
            _ingest_docx_images(child, document, state)
            if not text:
                continue
            if _DOCX_TABLE_CAPTION.match(text) and len(text) <= _DOCX_TABLE_CAPTION_MAX_LEN:
                pending_caption = text
            else:
                pending_caption = ""
            style = (paragraph.style.name or "").lower() if paragraph.style else ""
            if style.startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                state.lines.append("#" * min(int(level or 1), 6) + " " + text)
            else:
                state.lines.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            _ingest_docx_images(child, document, state)
            # 统一口径：带 colspan/rowspan 的 HTML（保留 Word 原表版式）
            body = _docx_table_html(table)
            # 题注只给紧邻的第一张表；空表也消费掉，避免跨表错挂
            caption, pending_caption = pending_caption, ""
            if not body:
                continue
            state.table_seq += 1
            state.tables.append(
                ParsedTable(
                    body=body, caption=caption, page=None, table_id=str(state.table_seq),
                    preserve_layout=True,
                )
            )
            state.lines.append(f"[表格 {state.table_seq}]")


def _collect_docx_fallback_images(path: Path, document: Any, state: _DocxState) -> None:
    """兜底扫图：正文关系、页眉/页脚 part、zip media 三处，漏一处等于丢图。"""
    # 1) 正文遍历漏掉的关系图（页眉/页脚/文本框等）
    for blob, ext, caption in _docx_images_from_rels(document):
        if not (caption or "").strip() and _docx_raster_is_decorative(blob):
            continue
        _register_docx_figure(state, blob, ext, caption)
    # 2) 页眉/页脚的图挂在 **header/footer part** 的关系上，不在 document.part.rels；
    #    实测交接班制度等 docx 的 logo 只在 word/header1.xml → word/media/，正文 blip=0。
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
                if not (caption or "").strip() and _docx_raster_is_decorative(blob):
                    continue
                _register_docx_figure(state, blob, ext, caption)
    # 3) zip 级兜底：任何 part 漏扫时仍能从 word/media/ 捞到栅格图（与 xlsx 同口径）
    for blob, ext in _ooxml_media_blobs(path, prefix="word/media/"):
        if _docx_raster_is_decorative(blob):
            continue
        _register_docx_figure(state, blob, ext, "")


def _attach_textless_captions(state: _DocxState, title: str) -> None:
    """全部无字图都挂题注（旧逻辑只给最大一张 → 其余被 chunker 当「无文本」丢掉）。"""
    if not state.textless:
        return
    # OCR 默认关闭时教程截图常两张都无字；优先挂「上文」让「二维码/下载」能被检索到。
    for rank, (idx, _size, nearby) in enumerate(state.textless, start=1):
        if state.figures[idx].caption or state.figures[idx].ocr_text:
            continue
        nearby_bit = " ".join((nearby or "").split())
        if len(nearby_bit) > 36:
            nearby_bit = nearby_bit[:36].rstrip() + "…"
        parts = [p for p in (title, nearby_bit) if p]
        if len(state.textless) > 1:
            parts.append(f"图{rank}")
        state.figures[idx].caption = " · ".join(parts) if parts else f"图{rank}"


def _parse_docx(path: Path, *, assets_dir: Path | None = None) -> ParsedDocument:
    """`.docx` —— 按文档顺序抽段落、表格与**嵌入图片**（与 pptx 同口径进 ``figures``）。

    页眉/页脚：**文字**抽一次写入文首「文件信息」；**装饰小图**不进 figure，
    较大的页眉图仍落盘（历史夹具与交接班制度 logo 兜底保留）。
    """
    try:
        import docx  # python-docx
        from docx.oxml.ns import qn
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-docx 不可用：{exc}") from exc

    document = docx.Document(str(path))
    # 自动编号住在 numbering.xml + 段落的 w:numPr 里，正文 w:t 中看不到；先展开再遍历
    numbering_expanded = _expand_docx_numbering(document, qn, _read_docx_numbering(path))
    header_lines, header_page_count = _docx_header_meta(document)
    # 页眉管控信息是**元数据**，不是正文：抽成结构化 header_meta 交给入库阶段
    # （effective_date/expire_date + 侧车），不再写「## 文件信息」章节。旧做法会让
    # 每篇制度多出一段高度同质的正文块，检索时跨文档互相竞争（实测「文件信息」四连发）。
    header_meta = parse_header_meta(header_lines)
    state = _DocxState(assets_dir=assets_dir, log_file=path.name)
    _walk_docx_body(document, state, qn)
    _collect_docx_fallback_images(path, document, state)
    _attach_textless_captions(state, path.stem.strip())

    if numbering_expanded:
        log.info("docx_numbering_expanded", file=path.name, paragraphs=numbering_expanded)
    if state.figures:
        log.info("docx_figures_extracted", file=path.name, figures=len(state.figures))
    if header_meta:
        log.info(
            "docx_header_meta_extracted",
            file=path.name,
            lines=len(header_meta),
            page_count=header_page_count,
        )
    return ParsedDocument(
        markdown="\n".join(state.lines).rstrip() + ("\n" if state.lines else ""),
        engine="docx",
        source_path=str(path),
        tables=state.tables,
        figures=state.figures,
        page_count=header_page_count,
        header_meta=header_meta,
    )


def _split_rows_at_image_anchors(
    rows_with_nums: list[tuple[int, list[str]]],
    header_idx: int,
    anchor_rows: list[int],
) -> list[list[tuple[int, list[str]]]]:
    """按图片锚点切表段；每段重复表头（RAGFlow spreadsheet 口径）。"""
    if not rows_with_nums or header_idx < 0 or header_idx >= len(rows_with_nums):
        return [rows_with_nums]
    header = rows_with_nums[header_idx]
    data = rows_with_nums[header_idx + 1 :]
    cuts = sorted({a for a in anchor_rows if a > header[0] and a <= (data[-1][0] if data else -1)})
    if not cuts or not data:
        return [rows_with_nums]
    parts: list[list[tuple[int, list[str]]]] = []
    current: list[tuple[int, list[str]]] = []
    cut_set = set(cuts)
    for row in data:
        if row[0] in cut_set and current:
            parts.append([header, *current])
            current = []
        current.append(row)
    if current:
        parts.append([header, *current])
    return parts or [rows_with_nums]


def _is_section_title_row(values: list[str]) -> bool:
    """单格短标题行（如 "年用气量统计对比（m³)"）应切成新表段。

    复杂 Excel 一个 sheet 常含多张表，表之间不是空行，而是一行单格分节标题。
    不切段会把后面的表错套第一张表的表头（实测：锅炉 2023 年被当成 2026年9月用量）。
    """
    nonempty = [str(v).strip() for v in values if str(v).strip()]
    return len(nonempty) == 1 and len(nonempty[0]) <= 40 and len(str(values)) >= 1


def _convert_date_serials_in_header(values: list[str]) -> list[str]:
    """无日期格式的 Excel 日期序列号：仅当本行含“日期/date”时按日期转换。

    实测《2026年9月能耗分析》表头 B3:D3 = 46295/45930/46265，单元格格式是 General，
    openpyxl 不会标记为日期；不转换会让模型把三列当成三个无名字段。
    """
    if not any(("日期" in v) or ("date" in v.lower()) for v in values):
        return values
    out = list(values)
    for i, value in enumerate(out):
        text = str(value).strip()
        if not text.isdigit():
            continue
        n = int(text)
        # Excel 日期序列：约 1954-2119 年；只在该行明确是日期行时启用
        if not (20000 <= n <= 80000):
            continue
        try:
            from openpyxl.utils.datetime import from_excel

            out[i] = from_excel(n).strftime("%Y-%m-%d")
        except Exception:
            continue
    return out


#: 百分比格式（``0.00%`` / ``0.0%`` / ``0%``）：Excel 的 0.0513 显示成 5.13%。
_PERCENT_NUMBER_FORMAT = re.compile(r"(?<!\\)0(?:\.(0+))?\s*%")


def _xlsx_percent_text(value: Any, number_format: str) -> str | None:
    """按 Excel 显示格式把小数还原成百分比文本；不是百分比格式返回 None。

    用户实测：表格里显示 ``-5.14%``，入库后序列化却输出 ``-0.051353...``。
    Excel 的 ``number_format='0.00%'`` 是**显示格式**，``cell.value`` 仍是小数；
    不还原格式，LLM 会把百分比当小数回答。
    """
    fmt = str(number_format or "")
    if "%" not in fmt or isinstance(value, bool):
        return None
    match = _PERCENT_NUMBER_FORMAT.search(fmt)
    if not match:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    decimals = min(6, len(match.group(1) or ""))
    # 格式里带千分位（如 #,##0.00%）才加逗号；否则保持 Excel 默认
    if "," in fmt:
        return f"{number * 100:,.{decimals}f}%"
    return f"{number * 100:.{decimals}f}%"


#: 数字格式里取「正数段的小数位/千分位」；只覆盖常用格式，复杂格式退回原值。
_NUMBER_FORMAT_RE = re.compile(r"[#0?][#0?,]*(?:\.([0#?]+))?")


def _xlsx_number_text(value: Any, number_format: str) -> str | None:
    """按 Excel ``number_format`` 还原数字的显示文本（0.00 / #,##0.00 / 0%）。"""
    fmt = str(number_format or "")
    if not fmt or fmt.lower() in {"general", "@"}:
        return None
    if "%" in fmt:
        return _xlsx_percent_text(value, fmt)
    if re.search(r"[eE][+-]0", fmt):
        return None
    positive = fmt.split(";", 1)[0]
    if "(" in fmt and ";" in fmt:  # (0.00) 负数段：括号表示负
        negative = fmt.split(";", 1)[1]
    else:
        negative = ""
    match = _NUMBER_FORMAT_RE.search(positive)
    if not match:
        return None
    decimals = min(6, len(match.group(1) or ""))
    use_comma = "," in match.group(0) or "," in positive
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 and "(" in negative:
        body = f"{abs(number):,.{decimals}f}" if use_comma else f"{abs(number):.{decimals}f}"
        return f"({body})"
    if use_comma:
        return f"{number:,.{decimals}f}"
    return f"{number:.{decimals}f}"


def _xlsx_cell_text(cell: Any) -> str:
    """Excel 单元格 → 文本；日期/百分比/常用数字格式按 Excel 显示还原。"""
    value = getattr(cell, "value", None)
    if value is None:
        return ""
    number_format = str(getattr(cell, "number_format", "") or "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number_text = _xlsx_number_text(value, number_format)
        if number_text is not None:
            return number_text
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return str(value).strip()
    if isinstance(value, (int, float)) and getattr(cell, "is_date", False):
        try:
            from openpyxl.utils.datetime import from_excel

            return from_excel(value).strftime("%Y-%m-%d")
        except Exception:
            pass
    return str(value).strip()


def _fill_xlsx_merged_header(
    sheet: Any,
    rows_with_nums: list[tuple[int, list[str]]],
    header_row: int,
) -> None:
    """把表头行里的合并单元格主格文字继承到从格（RAGFlow 同口径）。

    只填与 header_row 相交的合并区；不碰正文行，避免合并标题/字段被复制到整行。
    """
    try:
        from openpyxl.utils import range_boundaries

        ranges = list(sheet.merged_cells.ranges)
    except Exception:
        return
    index = {row_number: i for i, (row_number, _values) in enumerate(rows_with_nums)}
    for merged in ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(merged))
        if not (min_row <= header_row <= max_row):
            continue
        master = sheet.cell(min_row, min_col).value
        if master is None:
            continue
        text = str(master).strip()
        if not text:
            continue
        for row_number in range(min_row, max_row + 1):
            idx = index.get(row_number)
            if idx is None:
                continue
            values = rows_with_nums[idx][1]
            width = max_col
            if len(values) < width:
                values.extend([""] * (width - len(values)))
            for col in range(min_col, max_col + 1):
                pos = col - 1
                if not values[pos]:
                    values[pos] = text


@dataclass
class _XlsxState:
    """`.xlsx` 解析过程中的可变状态（把巨型函数的状态收拢，便于分阶段抽取）。"""

    assets_dir: Path | None
    log_file: str
    lines: list[str] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)
    seen_images: set[str] = field(default_factory=set)
    ocr_budget: list[int] = field(default_factory=lambda: [_figure_ocr_budget()])
    table_seq: int = 0


def _xlsx_split_blocks(sheet: Any) -> list[list[tuple[int, list[str]]]]:
    """按空行 / 单格分节标题把 sheet 切成连续数据块（一个 sheet 可能有多张表）。"""
    blocks: list[list[tuple[int, list[str]]]] = []
    buf: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(sheet.iter_rows(), start=1):
        values = _convert_date_serials_in_header([_xlsx_cell_text(cell) for cell in row])
        if not any(v for v in values):
            if buf:
                blocks.append(buf)
                buf = []
            continue
        # 单格分节标题：当前块已有内容时，先结束上一张表，标题归到下一段
        if len(buf) >= 2 and _is_section_title_row(values):
            blocks.append(buf)
            buf = []
        buf.append((row_number, values))
    if buf:
        blocks.append(buf)
    return blocks


def _xlsx_merge_single_row_blocks(
    blocks: list[list[tuple[int, list[str]]]],
) -> list[list[tuple[int, list[str]]]]:
    """单行块通常是标题行：并到下一块，避免产出只有标题的「表」。"""
    merged: list[list[tuple[int, list[str]]]] = []
    pending: list[tuple[int, list[str]]] = []
    for block in blocks:
        if len(block) == 1 and not pending:
            pending = block
            continue
        merged.append([*pending, *block] if pending else block)
        pending = []
    if pending:
        merged.append(pending)
    return merged


def _xlsx_listobject_header_rows(sheet: Any) -> list[int]:
    """ListObject（Excel 内建表）直接给出的表头行号；没有则空。"""
    header_rows: list[int] = []
    try:
        from openpyxl.utils import range_boundaries

        for tbl in (getattr(sheet, "tables", {}) or {}).values():
            try:
                header_rows.append(int(range_boundaries(tbl.ref)[1]))
            except Exception:
                continue
    except Exception:  # pragma: no cover - 无 tables 属性
        return []
    return header_rows


def _xlsx_image_anchor_rows(sheet: Any) -> list[int]:
    rows: list[int] = []
    for image in getattr(sheet, "_images", []) or []:
        try:
            rows.append(int(image.anchor._from.row) + 1)
        except Exception:
            continue
    return rows


def _xlsx_span_maps(
    sheet: Any,
    row_numbers: list[int],
    width: int,
) -> tuple[dict[tuple[int, int], tuple[int, int, int, int]], set[tuple[int, int]]]:
    """返回块内合并单元格的 master→(min_row,min_col,max_row,max_col) 与覆盖格集合。"""
    from openpyxl.utils import range_boundaries

    first_row, last_row = row_numbers[0], row_numbers[-1]
    masters: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    covered: set[tuple[int, int]] = set()
    try:
        ranges = list(sheet.merged_cells.ranges)
    except Exception:
        ranges = []
    for merged in ranges:
        try:
            min_col, min_row, max_col, max_row = range_boundaries(str(merged))
        except Exception:
            continue
        if max_row < first_row or min_row > last_row or min_col > width:
            continue
        min_row_c = max(min_row, first_row)
        max_row_c = min(max_row, last_row)
        max_col_c = min(max_col, width)
        if min_col > max_col_c or min_row_c > max_row_c:
            continue
        masters[(min_row_c, min_col)] = (min_row_c, min_col, max_row_c, max_col_c)
        for r in range(min_row_c, max_row_c + 1):
            for c in range(min_col, max_col_c + 1):
                if (r, c) != (min_row_c, min_col):
                    covered.add((r, c))
    return masters, covered


def _xlsx_rows_to_html(
    sheet: Any,
    rows_with_nums: list[tuple[int, list[str]]],
    *,
    header_row: int | None = None,
) -> str:
    """把 Excel 数据块还原成带 colspan/rowspan 的 HTML 表格（复核页对照用）。

    xlsx 的语义不只在单元格文本里：合并标题、分组表头、百分比/金额显示格式都在
    ``sheet`` 上。之前这里输出 Markdown 管道表，复核页 iframe 里显示的是 ``| a | b |``
    文本，和原件表格对不上；改成 HTML 后复核页可逐格对照，检索侧仍由
    ``parse_table_html`` 摊平单元格。
    """
    if not rows_with_nums:
        return ""
    row_numbers = [n for n, _values in rows_with_nums]
    width = 0
    for row_number, _values in rows_with_nums:
        for col in range(1, max(len(_values), 1) + 1):
            if sheet.cell(row_number, col).value is not None:
                width = max(width, col)
    if width <= 0:
        return ""

    masters, covered = _xlsx_span_maps(sheet, row_numbers, width)
    # 表头里的 General 日期序列号（46295）也要转成可读日期；与行文本口径一致。
    header_text: dict[int, str] = {}
    if header_row is not None and header_row in row_numbers:
        raw_header = [
            _xlsx_cell_text(sheet.cell(header_row, col)) for col in range(1, width + 1)
        ]
        header_text = {
            col: text for col, text in enumerate(_convert_date_serials_in_header(raw_header), 1)
        }
    rows_html: list[str] = []
    for row_number in row_numbers:
        cells: list[str] = []
        for col in range(1, width + 1):
            if (row_number, col) in covered:
                continue
            cell = sheet.cell(row_number, col)
            text = (
                header_text.get(col, "")
                if row_number == header_row
                else _xlsx_cell_text(cell)
            ).replace("\n", "<br>")
            tag = "th" if header_row is not None and row_number == header_row else "td"
            attrs = ""
            master = masters.get((row_number, col))
            if master is not None:
                _min_r, _min_c, max_r, max_c = master
                rowspan = max_r - row_number + 1
                colspan = max_c - col + 1
                if rowspan > 1:
                    attrs += f' rowspan="{rowspan}"'
                if colspan > 1:
                    attrs += f' colspan="{colspan}"'
            cells.append(f"<{tag}{attrs}>{html.escape(text)}</{tag}>")
        if cells:
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
    return ("<table>" + "".join(rows_html) + "</table>") if rows_html else ""


def xlsx_sheet_html(sheet: Any, *, header_row: int | None = None) -> str:
    """整张 xlsx 工作表 → HTML（复核页左栏「原件」预览用）。

    和解析侧共用 ``_xlsx_rows_to_html``：合并单元格、number_format（百分比/金额）都
    按 Excel 显示口径输出。原件预览与解析稿必须同一套显示格式，否则双栏对比会把
    「原件 -5.14%」和「预览 -0.0513」并排，用户无法判断是哪边错。
    """
    rows_with_nums: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(sheet.iter_rows(), start=1):
        values = [_xlsx_cell_text(cell) for cell in row]
        if any(str(value).strip() for value in values):
            rows_with_nums.append((row_number, values))
    return _xlsx_rows_to_html(sheet, rows_with_nums, header_row=header_row)


def _xlsx_serialize_blocks(
    sheet: Any,
    merged_blocks: list[list[tuple[int, list[str]]]],
    table_header_rows: list[int],
    state: _XlsxState,
    sheet_index: int,
) -> None:
    """逐块确定表头（ListObject > 结构启发式）→ 填合并表头 → 按图片锚点拆段 → 出表格。"""
    from ekbs.ingest.table_serialize import _looks_like_form, _pick_header_index

    anchor_rows = _xlsx_image_anchor_rows(sheet)
    for block_index, rows_with_nums in enumerate(merged_blocks, start=1):
        rows = [values for _n, values in rows_with_nums]
        row_nums = [n for n, _values in rows_with_nums]
        form_like = _looks_like_form(rows)
        header_abs: int | None = None
        if not form_like:
            in_block = [n for n in table_header_rows if n in row_nums]
            if in_block:
                header_abs = in_block[0]
            else:
                idx = _pick_header_index(rows)
                if 0 <= idx < len(row_nums):
                    header_abs = row_nums[idx]
        if header_abs is not None:
            _fill_xlsx_merged_header(sheet, rows_with_nums, header_abs)
            rows_with_nums = [(n, list(v)) for n, v in rows_with_nums]
        header_idx = row_nums.index(header_abs) if header_abs in row_nums else 0
        if not form_like and header_abs is not None:
            parts = _split_rows_at_image_anchors(rows_with_nums, header_idx, anchor_rows)
        else:
            parts = [rows_with_nums]
        for part_index, part in enumerate(parts, start=1):
            part_header = header_abs if any(n == header_abs for n, _v in part) else None
            body = _xlsx_rows_to_html(sheet, part, header_row=part_header)
            if not body:
                continue
            state.table_seq += 1
            base_caption = (
                sheet.title if len(merged_blocks) == 1
                else f"{sheet.title}（第{block_index}段）"
            )
            caption = base_caption if len(parts) == 1 else f"{base_caption}（图片段{part_index}）"
            state.tables.append(
                ParsedTable(
                    body=body,
                    caption=caption,
                    page=sheet_index,
                    table_id=str(state.table_seq),
                    preserve_layout=True,
                )
            )
            state.lines.append(f"[表格 {state.table_seq}] {caption}".rstrip())


def _xlsx_collect_sheet_images(sheet: Any, state: _XlsxState, sheet_index: int) -> None:
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
            assets_dir=state.assets_dir, figures=state.figures, seen=state.seen_images,
            caption=sheet.title, page=sheet_index, ocr_budget=state.ocr_budget,
            log_file=state.log_file, rotate_degrees=rotate,
        )


def _xlsx_collect_chart_tables(sheet: Any, state: _XlsxState, sheet_index: int) -> None:
    for chart_rows, caption in _xlsx_sheet_chart_tables(sheet):
        body = _rows_to_pipe_markdown(chart_rows)
        if not body:
            continue
        state.table_seq += 1
        state.tables.append(
            ParsedTable(
                body=body,
                caption=caption or sheet.title,
                page=sheet_index,
                table_id=str(state.table_seq),
            )
        )
        state.lines.append(
            f"[表格 {state.table_seq}] {caption or sheet.title}".rstrip()
        )


def _xlsx_collect_zip_media(path: Path, state: _XlsxState) -> None:
    """``sheet._images`` 可能漏掉部分介质：再扫 zip 内 ``xl/media/``（按内容摘要去重）。"""
    for blob, ext in _ooxml_media_blobs(path, prefix="xl/media/"):
        _register_figure_blob(
            blob, ext,
            assets_dir=state.assets_dir, figures=state.figures, seen=state.seen_images,
            caption=path.stem, page=None, ocr_budget=state.ocr_budget,
            log_file=state.log_file,
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
    state = _XlsxState(assets_dir=assets_dir, log_file=path.name)
    for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
        state.lines.append(f"## {sheet.title}")
        merged_blocks = _xlsx_merge_single_row_blocks(_xlsx_split_blocks(sheet))
        _xlsx_serialize_blocks(
            sheet, merged_blocks, _xlsx_listobject_header_rows(sheet), state, sheet_index
        )
        _xlsx_collect_sheet_images(sheet, state, sheet_index)
        _xlsx_collect_chart_tables(sheet, state, sheet_index)
        state.lines.append("")
    _xlsx_collect_zip_media(path, state)

    if state.figures:
        log.info("xlsx_figures_extracted", file=path.name, figures=len(state.figures))
    return ParsedDocument(
        markdown="\n".join(state.lines),
        engine="xlsx",
        source_path=str(path),
        tables=state.tables,
        figures=state.figures,
    )


def _chart_title(chart: Any) -> str:
    """尽量宽松地取图标题；openpyxl 的 Title 对象/字符串都认。"""
    title_obj = getattr(chart, "title", None)
    if title_obj is None:
        return ""
    # openpyxl Title.tx.rich 或 title 字符串
    title = str(getattr(title_obj, "tx", None) or title_obj or "").strip()
    if "Title()" in title or title.startswith("<"):
        return ""
    return title


def _chart_series_values(series: Any) -> list[str]:
    try:
        val = series.val
        nums = getattr(val, "numRef", None) or getattr(val, "numLit", None)
        if nums is not None and getattr(nums, "pt", None):
            return ["" if pt.v is None else str(pt.v) for pt in nums.pt]
        if hasattr(series, "values"):
            return ["" if v is None else str(v) for v in (series.values or [])]
    except Exception:
        return []
    return []


def _chart_categories(series: Any) -> list[str]:
    try:
        cat_ref = getattr(series, "cat", None)
        if cat_ref is not None:
            strref = getattr(cat_ref, "strRef", None) or getattr(cat_ref, "numRef", None)
            pts = getattr(strref, "pt", None) if strref is not None else None
            if pts:
                return ["" if pt.v is None else str(pt.v) for pt in pts]
    except Exception:
        pass
    return []


def _chart_series_cols(
    series_list: list[Any],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """取类别（首条 series 的 x 值）与各系列数值列。"""
    categories: list[str] = []
    series_cols: list[tuple[str, list[str]]] = []
    for index, series in enumerate(series_list, start=1):
        name = str(getattr(series, "title", None) or f"系列{index}")
        vals = _chart_series_values(series)
        if not categories:
            categories = _chart_categories(series)
        series_cols.append((name, vals))
    return categories, series_cols


def _xlsx_sheet_chart_tables(sheet: Any) -> Iterator[tuple[list[list[str]], str]]:
    """从工作表上的 chart 尽量还原类别×系列数值表。"""
    for chart in list(getattr(sheet, "_charts", None) or []):
        try:
            series_list = list(getattr(chart, "series", None) or [])
            if not series_list:
                continue
            categories, series_cols = _chart_series_cols(series_list)
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
            yield rows, _chart_title(chart)
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
        # textutil 常丢图：从原 .doc 二进制里捞还能认的栅格图。
        # ⚠️ 必须跳过装饰小图（页眉 logo 常见 89×26）：此前未过滤，维保记录等
        # .doc 会把 logo 当正文 figure 入库，污染检索（chunk「重庆熙街 SUNNY BLOCK」）。
        # 也不要用 path.stem 当 caption —— 会挡住「无题注+装饰」的跳过逻辑。
        seen: set[str] = set()
        ocr_budget = [_figure_ocr_budget()]
        skipped_decorative = 0
        for blob, ext in _scan_embedded_raster_blobs(path.read_bytes()):
            if _docx_raster_is_decorative(blob):
                skipped_decorative += 1
                continue
            _register_figure_blob(
                blob, ext,
                assets_dir=assets_dir, figures=figures, seen=seen,
                caption="", page=None, ocr_budget=ocr_budget,
                log_file=path.name,
            )
        if figures or skipped_decorative:
            log.info(
                "doc_figures_scanned",
                file=path.name,
                figures=len(figures),
                skipped_decorative=skipped_decorative,
            )

    # 引擎名保留 doc，便于日志/审计区分"走了旧格式转换"
    return ParsedDocument(
        markdown=parsed.markdown,
        engine="doc",
        source_path=str(path),
        page_map=parsed.page_map,
        page_count=parsed.page_count,
        figures=figures,
        tables=parsed.tables,
        header_meta=dict(parsed.header_meta or {}),
    )


@dataclass
class _PptxState:
    """`.pptx` 解析过程中的可变状态（把巨型函数的状态收拢，便于分阶段抽取）。"""

    assets_dir: Path | None
    log_file: str
    figures: list[ParsedFigure] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    ocr_budget: list[int] = field(default_factory=lambda: [_figure_ocr_budget()])
    seen_images: set[str] = field(default_factory=set)
    table_seq: int = 0
    skipped_pictures: int = 0


def _pptx_register_picture(shape: Any, state: _PptxState, number: int,
                           textless: list[tuple[int, int]]) -> None:
    """抽 shape 的图片；断链的链接图只告警、不崩。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    picture = _pptx_shape_picture(shape)
    if picture is not None:
        blob, ext = picture
        try:
            rotate = float(getattr(shape, "rotation", 0.0) or 0.0)
        except Exception:
            rotate = 0.0
        added = _register_figure_blob(
            blob, ext,
            assets_dir=state.assets_dir, figures=state.figures, seen=state.seen_images,
            caption=_alt_text(shape), page=number, ocr_budget=state.ocr_budget,
            log_file=state.log_file, rotate_degrees=rotate,
        )
        if added and not state.figures[-1].caption and not state.figures[-1].ocr_text:
            textless.append((len(state.figures) - 1, len(blob)))
        return
    # 明确是图片类形状但读不出字节（断链的链接图等）
    try:
        shape_type = shape.shape_type
    except Exception:
        shape_type = None
    if shape_type in {MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE}:
        state.skipped_pictures += 1
        log.warning("pptx_picture_unreadable", slide=number, shape_type=str(shape_type))


def _pptx_append_content(shape: Any, state: _PptxState, number: int, lines: list[str]) -> None:
    """抽 shape 的正文文本、内嵌表格、图表（图表转数值表）。"""
    if getattr(shape, "has_text_frame", False):
        text = (shape.text_frame.text or "").strip()
        if text:
            lines.append(text)
    if getattr(shape, "has_table", False):
        rows: list[list[str]] = []
        for row in shape.table.rows:
            cells = _logical_row_cells(row)
            if any(cells):
                rows.append(cells)
        body = _rows_to_pipe_markdown(rows)
        if body:
            state.table_seq += 1
            state.tables.append(
                ParsedTable(body=body, caption="", page=number, table_id=str(state.table_seq))
            )
            lines.append(f"[表格 {state.table_seq}]")
    # 图表 → 数值表（放置方式与「图片截图」不同，但问答同样要能引用）
    chart_rows = _pptx_chart_rows(shape)
    if chart_rows:
        body = _rows_to_pipe_markdown(chart_rows)
        if body:
            state.table_seq += 1
            title = _alt_text(shape) or ""
            state.tables.append(
                ParsedTable(body=body, caption=title, page=number, table_id=str(state.table_seq))
            )
            lines.append(f"[表格 {state.table_seq}]{(' ' + title) if title else ''}".rstrip())


def _pptx_ingest_shape(shape: Any, state: _PptxState, number: int,
                       lines: list[str], textless: list[tuple[int, int]]) -> None:
    _pptx_register_picture(shape, state, number, textless)
    _pptx_append_content(shape, state, number, lines)


def _pptx_slide_lines(slide: Any, number: int, portrait: bool, state: _PptxState) -> list[str]:
    """抽一页：页标题 + 形状内容 + 备注 + 无字图题注兜底。"""
    lines = [f"## 第 {number} 页" + ("（竖向）" if portrait else "")]
    textless: list[tuple[int, int]] = []
    for shape in _iter_shapes(slide.shapes):
        _pptx_ingest_shape(shape, state, number, lines, textless)
    notes = getattr(slide, "notes_slide", None)
    if notes is not None:
        note_text = (notes.notes_text_frame.text or "").strip()
        if note_text:
            lines.append(f"（备注）{note_text}")
    if textless:
        page_title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        if page_title:
            biggest = max(textless, key=lambda item: item[1])[0]
            state.figures[biggest].caption = page_title
            log.info(
                "pptx_figure_caption_fallback", slide=number,
                figure=state.figures[biggest].figure_id, caption=page_title,
                textless=len(textless),
            )
    return lines


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
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-pptx 不可用：{exc}") from exc

    presentation = Presentation(str(path))
    portrait = bool(
        presentation.slide_height and presentation.slide_width
        and presentation.slide_height > presentation.slide_width
    )
    state = _PptxState(assets_dir=assets_dir, log_file=path.name)
    pages = [
        "\n".join(_pptx_slide_lines(slide, number, portrait, state))
        for number, slide in enumerate(presentation.slides, start=1)
    ]
    # zip 兜底：母版/版式/漏扫形状里的 media
    for blob, ext in _ooxml_media_blobs(path, prefix="ppt/media/"):
        _register_figure_blob(
            blob, ext,
            assets_dir=state.assets_dir, figures=state.figures, seen=state.seen_images,
            caption=path.stem if portrait else "", page=None, ocr_budget=state.ocr_budget,
            log_file=state.log_file,
        )
    if state.figures or state.skipped_pictures:
        log.info(
            "pptx_figures_extracted", file=path.name, figures=len(state.figures),
            unreadable=state.skipped_pictures, portrait=portrait,
            ocr_budget=_figure_ocr_budget(),
        )
    markdown = "\n\n".join(pages)
    return ParsedDocument(
        markdown=markdown, engine="pptx", source_path=str(path),
        page_map=page_map_from_pages(pages), page_count=len(pages),
        figures=state.figures,
        tables=state.tables,
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


def _fill_xls_merged_header(
    sheet: Any,
    rows_with_nums: list[tuple[int, list[str]]],
    header_row: int,
) -> None:
    """xlrd (.xls) 的合并单元格表头继承（与 xlsx 同口径）。"""
    try:
        merged = list(getattr(sheet, "merged_cells", []) or [])
    except Exception:
        return
    index = {row_number: i for i, (row_number, _values) in enumerate(rows_with_nums)}
    for rlo, rhi, clo, chi in merged:
        # xlrd: rlo/rhi/clo/chi 是 0-based、右开区间
        first_row, last_row = rlo + 1, rhi
        if not (first_row <= header_row <= last_row):
            continue
        try:
            master = _cell_text(sheet.cell_value(rlo, clo))
        except Exception:
            continue
        if not master:
            continue
        for row_number in range(first_row, last_row + 1):
            idx = index.get(row_number)
            if idx is None:
                continue
            values = rows_with_nums[idx][1]
            if len(values) < chi:
                values.extend([""] * (chi - len(values)))
            for col in range(clo, chi):
                if not values[col]:
                    values[col] = master


@dataclass
class _XlsState:
    """旧版 `.xls` 解析过程中的可变状态。"""

    assets_dir: Path | None
    log_file: str
    lines: list[str] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)
    table_seq: int = 0


def _xls_row_values(sheet: Any, book: Any, xlrd: Any, row_index: int) -> list[str]:
    """xlrd 把所有数字读成 float（整数显示成 "1.0"）——归一化成 "1"；日期按 XL_CELL_DATE 转可读。"""
    values: list[str] = []
    for col_index in range(sheet.ncols):
        cell = sheet.cell(row_index, col_index)
        if cell.ctype == xlrd.XL_CELL_DATE:
            try:
                values.append(
                    xlrd.xldate_as_datetime(cell.value, book.datemode).strftime("%Y-%m-%d")
                )
            except Exception:
                values.append(_cell_text(cell.value))
        else:
            values.append(_cell_text(cell.value))
    return _convert_date_serials_in_header(values)


def _xls_split_blocks(sheet: Any, book: Any, xlrd: Any) -> list[list[tuple[int, list[str]]]]:
    """与 xlsx 同口径：按空行 / 单格分节标题切连续块。"""
    blocks: list[list[tuple[int, list[str]]]] = []
    buf: list[tuple[int, list[str]]] = []
    for row_index in range(sheet.nrows):
        values = _xls_row_values(sheet, book, xlrd, row_index)
        if not any(values):
            if buf:
                blocks.append(buf)
                buf = []
            continue
        if len(buf) >= 2 and _is_section_title_row(values):
            blocks.append(buf)
            buf = []
        buf.append((row_index + 1, values))
    if buf:
        blocks.append(buf)
    return blocks


def _xls_serialize_blocks(
    sheet: Any, merged_blocks: list[list[tuple[int, list[str]]]],
    state: _XlsState, sheet_index: int,
) -> None:
    from ekbs.ingest.table_serialize import _looks_like_form, _pick_header_index

    for block_index, rows_with_nums in enumerate(merged_blocks, start=1):
        rows = [values for _n, values in rows_with_nums]
        row_nums = [n for n, _values in rows_with_nums]
        if not _looks_like_form(rows):
            idx = _pick_header_index(rows)
            if 0 <= idx < len(row_nums):
                _fill_xls_merged_header(sheet, rows_with_nums, row_nums[idx])
                rows = [values for _n, values in rows_with_nums]
        body = _rows_to_pipe_markdown(rows)
        if not body:
            continue
        state.table_seq += 1
        caption = sheet.name if len(merged_blocks) == 1 else f"{sheet.name}（第{block_index}段）"
        state.tables.append(
            ParsedTable(body=body, caption=caption, page=sheet_index, table_id=str(state.table_seq))
        )
        state.lines.append(f"[表格 {state.table_seq}] {caption}".rstrip())


def _xls_scan_figures(path: Path, state: _XlsState) -> None:
    """xlrd 不暴露嵌入图 API：对文件字节做 PNG/JPEG 魔数扫描作兜底（与 ``.doc`` 同口径）。"""
    if state.assets_dir is None:
        return
    seen: set[str] = set()
    ocr_budget = [_figure_ocr_budget()]
    for blob, ext in _scan_embedded_raster_blobs(path.read_bytes()):
        _register_figure_blob(
            blob, ext,
            assets_dir=state.assets_dir, figures=state.figures, seen=seen,
            caption=path.stem, page=None, ocr_budget=ocr_budget,
            log_file=state.log_file,
        )
    if state.figures:
        log.info("xls_figures_scanned", file=path.name, figures=len(state.figures))


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
    state = _XlsState(assets_dir=assets_dir, log_file=path.name)
    for sheet_index, sheet in enumerate(book.sheets(), start=1):
        state.lines.append(f"## {sheet.name}")
        # 与 xlsx 共用「单行块并到下一块」的规则
        merged_blocks = _xlsx_merge_single_row_blocks(_xls_split_blocks(sheet, book, xlrd))
        _xls_serialize_blocks(sheet, merged_blocks, state, sheet_index)
        state.lines.append("")
    _xls_scan_figures(path, state)
    return ParsedDocument(
        markdown="\n".join(state.lines),
        engine="xls",
        source_path=str(path),
        tables=state.tables,
        figures=state.figures,
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
    # ONNX Runtime 1.30 在进程退出时拆遥测会 SIGABRT（macOS 弹「Python 意外退出」）。
    # 崩溃栈停在 PosixTelemetry::Shutdown，产物已经写完。子进程起来之前关掉遥测。
    env.setdefault("ORT_DISABLE_TELEMETRY", "1")
    if not env.get(MODELSCOPE_CACHE_ENV):
        candidate = _repo_mineru_models_dir()
        if candidate.is_dir():
            env[MODELSCOPE_CACHE_ENV] = str(candidate)
    return env
MINERU_TIMEOUT_ENV = "EKBS_MINERU_TIMEOUT"
#: MinerU 失败重试次数（默认 2；偶发失败实测存在，见 `_parse_mineru` 注释）
MINERU_ATTEMPTS_ENV = "EKBS_MINERU_ATTEMPTS"


def _subprocess_tail(completed: "subprocess.CompletedProcess[str]", *, lines: int = 3) -> str:
    """子进程 stderr/stdout 的末尾几行 —— 拼进异常消息，别把真实原因吞掉。"""
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return " / ".join(text.splitlines()[-lines:])[:400] or "（无输出）"


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
        # **重试是必要的**：MinerU 的失败是偶发的（实测同一份 PDF 第 1 次"未产出任何产物"、
        # 第 2 次完全正常；16 份批量跑里也出现过 2 次）。而失败信息原先只报"未产出可识别的结果"，
        # 把真实原因（权限/模型/内存）全吞了 —— 排查时极易误判成"没装/模型不在"。
        # 所以：① 每次失败都把子进程输出的尾部带进异常；② 默认重试一次。
        attempts = max(1, int(os.environ.get(MINERU_ATTEMPTS_ENV) or 2))
        last_reason = ""
        content_list = None
        markdown_file = None
        auto = Path(out_dir)
        for attempt in range(1, attempts + 1):
            try:
                completed = subprocess.run(
                    [binary, "-p", str(path), "-o", out_dir],
                    capture_output=True, text=True, timeout=timeout, env=env, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_reason = f"MinerU 解析超时（>{timeout:.0f}s）"
                if attempt < attempts:
                    log.warning("mineru_retry", file=path.name, attempt=attempt, reason=last_reason)
                    continue
                raise ParserUnavailable(f"{last_reason}：{path.name}") from exc

            tail = _subprocess_tail(completed)
            # 产物目录：<out>/<stem>/auto（无 auto 时退化为 <out>/<stem>）
            # ⚠️ 必须先找产物再看退出码：macOS 上 MinerU 常在写完 content_list
            # 后以 SIGABRT（returncode -6）崩掉；旧逻辑整页重跑，解析时间翻倍。
            base = Path(out_dir) / path.stem
            auto = base / "auto" if (base / "auto").is_dir() else (base if base.is_dir() else Path(out_dir))
            content_list = _find_output(auto, f"{path.stem}_content_list.json")
            markdown_file = _find_output(auto, f"{path.stem}.md")
            if content_list is not None or markdown_file is not None:
                if completed.returncode != 0:
                    log.warning(
                        "mineru_nonzero_exit_but_output_ok",
                        file=path.name,
                        returncode=completed.returncode,
                        attempt=attempt,
                        hint="产物已落盘，采纳结果、不再重试",
                    )
                break
            if completed.returncode != 0:
                last_reason = f"MinerU 解析失败（退出码 {completed.returncode}）：{tail}"
            else:
                last_reason = f"MinerU 未产出可识别的结果（既无 content_list.json 也无 .md）：{tail}"
            if attempt < attempts:
                log.warning("mineru_retry", file=path.name, attempt=attempt, reason=last_reason[:200])
        if content_list is None and markdown_file is None:
            raise ParserUnavailable(
                f"{last_reason}｜文件：{path.name}｜"
                f"（已尝试 {attempts} 次；模型缓存 {env.get(MODELSCOPE_CACHE_ENV) or '未设置'}）"
            )

        if content_list is None:
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


def _docx_related_blob(document: Any, r_id: str) -> tuple[bytes, str] | None:
    """按关系 id 取图片 part 的 ``(blob, ext)``；非图片/缺失返回 None。"""
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


def _blip_caption(blip: Any) -> str:
    """向上找 8 层父节点，取第一个非空 ``descr/title/name`` 作题注。"""
    parent = blip.getparent()
    for _ in range(8):
        if parent is None:
            break
        for attr in ("descr", "title", "name"):
            value = parent.get(attr)
            if value and str(value).strip():
                return str(value).strip()
        parent = parent.getparent()
    return ""


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

    blip_tag = qn("a:blip")
    embed_attr = qn("r:embed")
    for blip in element.iter(blip_tag):
        got = _docx_related_blob(document, blip.get(embed_attr))
        if not got:
            continue
        blob, ext = got
        yield blob, ext, _blip_caption(blip)

    # VML：``{urn:schemas-microsoft-com:vml}imagedata`` + ``r:id``
    vml_imagedata = "{urn:schemas-microsoft-com:vml}imagedata"
    r_id_attr = qn("r:id")
    for node in element.iter(vml_imagedata):
        r_id = node.get(r_id_attr) or node.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        got = _docx_related_blob(document, r_id or "")
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


def _emit_seal_account(emit, text: str) -> None:
    """公章图不进正文，但章下压住的银行账号要留在签署行里。"""
    raw = (text or "").strip()
    if not raw or not re.search(r"账号|开户银行", raw):
        return
    if not re.search(r"\d{8,}", raw):
        return
    emit(raw)


@dataclass
class _MineruState:
    """MinerU 块列表 → Markdown 过程中的可变状态。"""

    path: Path
    out_root: Path | None
    assets_dir: Path | None
    lines: list[str] = field(default_factory=list)
    page_map: list[tuple[int, int, int]] = field(default_factory=list)
    seen_pages: set[int] = field(default_factory=set)
    figures: list[ParsedFigure] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    ocr_budget: int = field(default_factory=_figure_ocr_budget)
    table_seq: int = 0
    offset: int = 0
    max_page: int = 0


def _append_table_block(state: _MineruState, block: dict, page: int, emit) -> None:
    body = str(block.get("table_body") or str(block.get("text") or "").strip()).strip()
    caption = " ".join(str(c) for c in (block.get("table_caption") or []) if c).strip()
    if body or caption:
        state.table_seq += 1
        state.tables.append(
            ParsedTable(body=body, caption=caption, page=page, table_id=str(state.table_seq))
        )
    # 正文只留一行锚点，避免整表再进 parent/child 双索引
    emit(f"[表格 {state.table_seq}]{(' ' + caption) if caption else ''}".rstrip())


def _append_image_block(state: _MineruState, block: dict, page: int, emit) -> None:
    from ekbs.ingest.seal_filter import is_seal_image

    img = str(block.get("img_path") or "").strip()
    caption = " ".join(str(c) for c in (block.get("img_caption") or []) if c).strip()
    managed = _persist_figure(img, state.out_root, state.assets_dir) if img else None
    if managed is None:
        if is_seal_image(None, caption=caption):
            log.info("seal_figure_dropped", file=state.path.name, img=img, page=page)
            _emit_seal_account(emit, caption)
            return
        # 没给 assets_dir（或找不到源图）→ 只能留链接，且**明确告警**：这种链接会失效
        if img and state.assets_dir is None:
            log.warning("figure_not_persisted", file=state.path.name, img=img)
        emit(f"![{caption or '图片'}]({img})" if img else (caption or "（图片）"))
        return
    image_file = state.assets_dir / managed
    # 先按画面/题注丢掉公章，避免对印章浪费 OCR，也不写进附图
    if is_seal_image(image_file, caption=caption):
        log.info("seal_figure_dropped", file=state.path.name, img=managed, page=page)
        _emit_seal_account(emit, caption)
        return
    ocr = ""
    if state.ocr_budget > 0:
        outcome = _ocr_figure(image_file)
        ocr = outcome.text
        if outcome.attempted:
            state.ocr_budget -= 1
        if not outcome.usable:
            log.warning(
                "figure_ocr_disabled_for_document", file=state.path.name,
                skipped_after=len(state.figures),
                hint="MinerU 不可用，本文件剩余图片不再尝试 OCR（图片本身已落盘）",
            )
            state.ocr_budget = 0
    if is_seal_image(image_file, caption=caption, ocr=ocr):
        log.info(
            "seal_figure_dropped", file=state.path.name, img=managed, page=page, via="ocr",
        )
        _emit_seal_account(emit, caption or ocr)
        return
    state.figures.append(
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


def _append_text_block(state: _MineruState, block: dict, kind: str, emit) -> None:
    text = str(block.get("text") or "").strip()
    if kind == "equation":
        emit(text if text.startswith("$$") else f"$$ {text} $$")
        return
    level = block.get("text_level")
    if isinstance(level, int) and 1 <= level <= 6 and text:
        emit("#" * level + " " + text)
    elif text:
        emit(text)


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
    state = _MineruState(path=path, out_root=out_root, assets_dir=assets_dir)

    def emit(text: str) -> None:
        state.lines.append(text)
        state.offset += len(text) + 1

    for block in blocks:
        page_index = block.get("page_idx")
        page = int(page_index) + 1 if isinstance(page_index, int) else 1
        state.max_page = max(state.max_page, page)
        if page not in state.seen_pages:
            state.seen_pages.add(page)
            state.page_map.append((state.offset, page, page))
        kind = str(block.get("type") or "text")
        if kind == "table":
            _append_table_block(state, block, page, emit)
        elif kind == "image":
            _append_image_block(state, block, page, emit)
        else:
            _append_text_block(state, block, kind, emit)

    markdown = "\n".join(state.lines)
    if not markdown.strip() and markdown_file is not None:
        markdown = markdown_file.read_text(encoding="utf-8", errors="replace")
    # 跨页续表合并（清洗步 4）：MinerU 常把一表拆成多块
    from ekbs.ingest.table_merge import merge_continued_tables

    markdown, tables, merge_stats = merge_continued_tables(markdown, state.tables)
    if merge_stats:
        log.info("mineru_tables_merged", file=path.name, **merge_stats)
    return ParsedDocument(
        markdown=markdown,
        engine="mineru",
        source_path=str(path),
        page_map=state.page_map,
        page_count=state.max_page or None,
        figures=state.figures,
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
