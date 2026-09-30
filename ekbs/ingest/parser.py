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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ekbs.logging import get_logger

log = get_logger(component="parser")


class ParserUnavailable(RuntimeError):
    """解析引擎不可用（例如 MinerU 未安装）。**不允许静默降级为「解析成功但内容为空」**。"""


@dataclass
class ParsedDocument:
    markdown: str
    engine: str
    source_path: str = ""
    #: ``[(char_offset, page_from, page_to), …]``；轻量解析为空
    page_map: list[tuple[int, int, int]] = field(default_factory=list)
    page_count: int | None = None


def parse_markdown(text: str, *, engine: str = "text") -> ParsedDocument:
    return ParsedDocument(markdown=text, engine=engine)


def parse_file(path: str | Path, *, engine: str = "auto") -> ParsedDocument:
    """按扩展名选择解析器。``engine`` 可强制为 ``mineru`` / ``text`` / ``docx`` / ``xlsx``。"""
    target = Path(path)
    if not target.is_file():
        raise ParserUnavailable(f"文件不存在：{target}")
    suffix = target.suffix.lower()
    if engine == "auto":
        engine = {
            ".md": "text",
            ".markdown": "text",
            ".txt": "text",
            ".docx": "docx",
            ".xlsx": "xlsx",
            ".pdf": "mineru",
            ".png": "mineru",
            ".jpg": "mineru",
            ".jpeg": "mineru",
        }.get(suffix, "text")

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
    if engine == "mineru":
        return _parse_mineru(target)
    raise ParserUnavailable(f"未知解析引擎：{engine}")


def _parse_docx(path: Path) -> ParsedDocument:
    try:
        import docx  # python-docx
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"python-docx 不可用：{exc}") from exc

    document = docx.Document(str(path))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower()
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            lines.append("#" * min(int(level or 1), 6) + " " + text)
        else:
            lines.append(text)
    for table in document.tables:
        lines.append("")
        for row in table.rows:
            cells = [((cell.text or "").strip().replace("\n", " ")) for cell in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return ParsedDocument(markdown="\n".join(lines), engine="docx", source_path=str(path))


def _parse_xlsx(path: Path) -> ParsedDocument:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise ParserUnavailable(f"openpyxl 不可用：{exc}") from exc

    workbook = openpyxl.load_workbook(str(path), data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"## {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            values = ["" if cell is None else str(cell) for cell in row]
            if not any(v.strip() for v in values):
                continue
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    return ParsedDocument(markdown="\n".join(lines), engine="xlsx", source_path=str(path))


#: MinerU CLI 可执行文件的环境变量覆盖（独立 venv 时指向 var/mineru-venv/bin/mineru）
MINERU_BIN_ENV = "EKBS_MINERU_BIN"
#: MinerU 模型来源（国内拉 HuggingFace 基本不通，实测必须用 modelscope）
MINERU_SOURCE_ENV = "MINERU_MODEL_SOURCE"
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
        else [str(Path(sys.executable).parent / "mineru"), "mineru"]
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


def _parse_mineru(path: Path) -> ParsedDocument:
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
    env = dict(os.environ)
    env.setdefault(MINERU_SOURCE_ENV, "modelscope")

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
        return _blocks_to_document(blocks, path, markdown_file)


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


def _blocks_to_document(blocks: Sequence[dict], path: Path, markdown_file: Path | None) -> ParsedDocument:
    """把 MinerU 的块列表拼成 Markdown，并按 ``page_idx`` 生成页码映射。"""
    lines: list[str] = []
    page_map: list[tuple[int, int, int]] = []
    seen_pages: set[int] = set()
    offset = 0
    max_page = 0

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
            if caption:
                emit(caption)
            emit(body)
        elif kind == "image":
            img = str(block.get("img_path") or "").strip()
            caption = " ".join(str(c) for c in (block.get("img_caption") or []) if c).strip()
            emit(f"![{caption or '图片'}]({img})" if img else (caption or "（图片）"))
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
