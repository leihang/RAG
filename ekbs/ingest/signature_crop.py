"""签署栏手写截图。

认不出的手写姓名不写进正文。复核时把签署带渲成一张图给人对照：
末页底部有字就裁底部；末页底部是空白（签署在前一页、手写在末页上方）
则拼前一页下半和末页顶部。题注固定，不把画面里的字送进检索。

末页上半是正文、底部只是「举报电话 / 承诺方（盖章）」这类印刷页脚时，
不把页脚截成手写图。PDF 自带文字层时，用文字层补回被 OCR 丢掉的页脚，
并改回被误当成姓名的签署栏。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ekbs.ingest.chunker import FigureInput
from ekbs.logging import get_logger

log = get_logger(component="ingest.signature_crop")

HANDWRITE_FIGURE_ID = "handwrite-sign"
HANDWRITE_CAPTION = "签署栏手写，未识别"
_PLACEHOLDER = "[手写签字]"

_RENDER = r"""
import sys
import pypdfium2 as pdfium
from PIL import Image

src, dest = sys.argv[1], sys.argv[2]
doc = pdfium.PdfDocument(src)
if len(doc) < 1:
    raise SystemExit(2)

def render(index):
    # 负索引在部分扫描 PDF 上会 PdfiumError，只用正索引。
    return doc[index].render(scale=2).to_pil().convert("RGB")

def dark_count(image, y0, y1):
    gray = image.convert("L")
    px = gray.load()
    width, _height = gray.size
    count = 0
    for y in range(max(0, y0), min(y1, gray.size[1]), 3):
        for x in range(0, width, 3):
            if px[x, y] < 200:
                count += 1
    return count

last = render(len(doc) - 1)
width, height = last.size
bottom_top = int(height * 0.65)
top_ink = dark_count(last, 0, int(height * 0.55))
bottom_ink = dark_count(last, bottom_top, height)
# 末页底部几乎空白时，签署栏在倒数第二页，手写落在末页上方。
if len(doc) >= 2 and bottom_ink < 80:
    prev = render(len(doc) - 2)
    pw, ph = prev.size
    prev_band = prev.crop((0, int(ph * 0.55), pw, ph))
    last_band = last.crop((0, 0, width, max(8, int(height * 0.28))))
    canvas = Image.new(
        "RGB",
        (max(prev_band.width, last_band.width), prev_band.height + last_band.height),
        "white",
    )
    canvas.paste(prev_band, (0, 0))
    canvas.paste(last_band, (0, prev_band.height))
    canvas.save(dest, "PNG")
else:
    # 上半页是正文、底部只有一两行页脚：那是印刷结尾，不是手写签署栏。
    if top_ink >= 1500 and bottom_ink < 1200:
        raise SystemExit(3)
    top = bottom_top if bottom_top < height - 8 else 0
    last.crop((0, top, width, height)).save(dest, "PNG")
"""

_PDF_TEXT = r"""
import json, sys
import pypdfium2 as pdfium
doc = pdfium.PdfDocument(sys.argv[1])
pages = []
for i in range(len(doc)):
    pages.append(doc[i].get_textpage().get_text_bounded() or "")
print(json.dumps(pages, ensure_ascii=False))
"""

_SIG_KEYS = ("授权代表签字", "甲方单位")


def is_handwriting_figure(figure: object) -> bool:
    """复核截图：不进分块、不进向量。"""
    if isinstance(figure, dict):
        figure_id = str(figure.get("figure_id") or "")
    else:
        figure_id = str(getattr(figure, "figure_id", "") or "")
    return figure_id.startswith("handwrite")


def append_handwrite_placeholder(markdown: str) -> str:
    """正文末尾留占位，不把认错的姓名写进去。已有占位则不动。"""
    text = markdown or ""
    if _PLACEHOLDER in text:
        return text
    return text.rstrip() + f"\n{_PLACEHOLDER}\n"


def printed_footer_ending(page_text: str) -> bool:
    """末页结尾是举报电话/承诺方盖章，不是签署栏。"""
    tail = re.sub(r"\s+", "", page_text or "")[-180:]
    if "授权代表" in tail or "代表人" in tail:
        return False
    return "举报电话" in tail or "承诺方" in tail


def load_pdf_text_pages(pdf_path: Path) -> list[str]:
    """读 PDF 文字层。纯扫描件没有文字层时返回空列表。"""
    py = mineru_python()
    if py is None or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        return []
    try:
        completed = subprocess.run(
            [str(py), "-c", _PDF_TEXT, str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("pdf_text_layer_failed", error=str(exc)[:160])
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        pages = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(pages, list):
        return []
    text_pages = [str(page or "") for page in pages]
    if not any(page.strip() for page in text_pages):
        return []
    return text_pages


def patch_markdown_from_pdf_pages(markdown: str, pages: list[str]) -> tuple[str, dict[str, int]]:
    """用文字层改回被误判的签署栏，并补上 OCR 没写进正文的页脚。"""
    text = markdown or ""
    stats: dict[str, int] = {}
    restored = 0
    for block in _signature_blocks(pages):
        key = next((item for item in _SIG_KEYS if item in block[0]), "")
        if not key:
            continue
        pattern = re.compile(rf"^.*{re.escape(key)}.*\[手写签字\].*(?:\n日期：[^\n]*)?", re.M)
        replacement = "\n".join(block)
        text, count = pattern.subn(replacement, text, count=1)
        restored += count
    if restored:
        stats["pdf_signature_restored"] = restored
    footer = _footer_lines(pages)
    if footer and "举报电话：" not in text:
        text = text.rstrip() + "\n" + "\n".join(footer) + "\n"
        stats["pdf_tail_restored"] = len(footer)
    return text, stats


def _signature_blocks(pages: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    for page in pages:
        lines = [line.strip() for line in page.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        index = 0
        while index < len(lines):
            line = lines[index]
            if "授权代表签字" in line or ("甲方单位" in line and "代表人" in line):
                block = [line]
                if (
                    index + 1 < len(lines)
                    and "日期" in lines[index + 1]
                    and "签字" not in lines[index + 1]
                    and "代表人" not in lines[index + 1]
                ):
                    block.append(lines[index + 1])
                    index += 1
                blocks.append(block)
            index += 1
    return blocks


def _footer_lines(pages: list[str]) -> list[str]:
    if not pages:
        return []
    lines = [
        line.strip()
        for line in pages[-1].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    out: list[str] = []
    started = False
    for line in lines:
        if not started:
            if line.startswith("举报电话"):
                started = True
            else:
                continue
        if not line or re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        out.append(line)
    return out


def mineru_python() -> Path | None:
    """MinerU 独立 venv 的解释器；里面才有 pypdfium2。"""
    candidate = Path(__file__).resolve().parents[2] / "var" / "mineru-venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def render_signature_crop(pdf_path: Path, dest_png: Path) -> bool:
    """把 PDF 最后一页底部签署带写成 PNG。失败返回 False，不打断复核。"""
    py = mineru_python()
    if py is None or not pdf_path.is_file():
        return False
    if pdf_path.suffix.lower() != ".pdf":
        return False
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(py), "-c", _RENDER, str(pdf_path), str(dest_png)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("signature_crop_failed", error=str(exc)[:160])
        return False
    if completed.returncode == 3:
        return False
    if completed.returncode != 0 or not dest_png.is_file() or dest_png.stat().st_size < 32:
        log.warning(
            "signature_crop_failed",
            code=completed.returncode,
            tail=(completed.stderr or completed.stdout or "")[-200:],
        )
        return False
    return True


def signature_figure(
    *,
    data_dir: Path,
    doc_id: int,
    pdf_path: Path,
    page: int | None,
) -> FigureInput | None:
    """落盘签署截图并返回附图；渲不出来则 None。"""
    dest = Path(data_dir) / "parse" / str(doc_id) / "images" / f"{HANDWRITE_FIGURE_ID}.png"
    if not render_signature_crop(pdf_path, dest):
        return None
    rel = str(dest.relative_to(Path(data_dir)))
    return FigureInput(
        image_path=rel,
        caption=HANDWRITE_CAPTION,
        ocr_text="",
        page=page,
        figure_id=HANDWRITE_FIGURE_ID,
    )
