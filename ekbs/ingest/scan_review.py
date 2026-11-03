"""扫描件解析草稿与人工复核闸门。

扫描 PDF/合同 OCR 误差大：解析 + 清洗后先落盘待复核，**人工确认后再分块/嵌入**。

两道判定，命中任一即停在 ``review``：

* 路径或标题含「扫描」（与语料目录 ``pdf-扫描/`` 约定一致，``.md`` 测试件也走这条）；
* 解析引擎是 MinerU（``.pdf`` / ``.png`` / ``.jpg`` / ``.jpeg``）。文件名没写「扫描」
  也要等人确认——上传落在 ``internal_public_document/`` 时路径里没有这个字。

Word、Excel、PPT、Markdown 解析后同样停在 ``review``，只是不走扫描件清洗。
确认通过后才分块发布。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.clean import clean_figure_text, clean_markdown
from ekbs.ingest.seal_filter import partition_seal_figures, strip_figure_anchors
from ekbs.ingest.signature_crop import (
    append_handwrite_placeholder,
    is_handwriting_figure,
    load_pdf_text_pages,
    patch_markdown_from_pdf_pages,
    printed_footer_ending,
    signature_figure,
)
from ekbs.ingest.table_merge import merge_continued_tables
from ekbs.logging import get_logger

log = get_logger(component="ingest.scan_review")

#: 文档状态：解析清洗完成，等待管理台人工确认后再分块
STATUS_REVIEW = "review"

DRAFT_MARKDOWN_NAME = "cleaned.md"
DRAFT_SIDECAR_NAME = "review_sidecar.json"

_SCAN_HINT = re.compile(r"扫描|scan", re.IGNORECASE)


def is_scan_source(source_path: str | None, title: str | None = None) -> bool:
    """路径或标题是否标明扫描件（需人工复核后再分块）。"""
    hay = f"{source_path or ''}\n{title or ''}"
    return bool(_SCAN_HINT.search(hay))


def requires_human_review(
    source_path: str | None,
    title: str | None = None,
    *,
    engine: str | None = None,
) -> bool:
    """解析清洗后是否停在复核，而不是直接分块发布。

    MinerU 覆盖全部 PDF 与独立图片，OCR/版面误差大；不能只靠目录名里有没有「扫描」。
    文本引擎（docx/md/xlsx 等）不因本函数强制复核。
    """
    if is_scan_source(source_path, title):
        return True
    return (engine or "").strip().lower() == "mineru"


def draft_dir(data_dir: Path, doc_id: int) -> Path:
    return Path(data_dir) / "parse" / str(doc_id)


def draft_markdown_path(data_dir: Path, doc_id: int) -> Path:
    return draft_dir(data_dir, doc_id) / DRAFT_MARKDOWN_NAME


def draft_sidecar_path(data_dir: Path, doc_id: int) -> Path:
    return draft_dir(data_dir, doc_id) / DRAFT_SIDECAR_NAME


@dataclass
class ReviewSidecar:
    """复核通过后重建 ingest 所需的附属信息。"""

    engine: str = ""
    page_map: list[list[int]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    clean_stats: dict[str, Any] = field(default_factory=dict)
    #: 文档总页数。docx 无真实分页时由解析器从页眉「页数：1/2」抽取；
    #: 不存的话入库阶段只能靠 page_map 推算，而 docx 的 page_map 为空 → page_count 永远 NULL。
    page_count: int | None = None


def save_review_draft(
    *,
    data_dir: Path,
    doc_id: int,
    markdown: str,
    engine: str = "",
    page_map: Sequence[tuple[int, int, int]] | None = None,
    figures: Sequence[FigureInput] | None = None,
    tables: Sequence[TableInput] | None = None,
    clean_stats: dict[str, Any] | None = None,
    page_count: int | None = None,
) -> str:
    """写入清洗后正文与 sidecar；返回相对 ``data_dir`` 的 parse_path（目录）。"""
    out = draft_dir(data_dir, doc_id)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / DRAFT_MARKDOWN_NAME
    md_path.write_text(markdown or "", encoding="utf-8")
    sidecar = ReviewSidecar(
        engine=engine or "",
        page_map=[[int(a), int(b), int(c)] for a, b, c in (page_map or [])],
        figures=[asdict(f) for f in (figures or [])],
        tables=[asdict(t) for t in (tables or [])],
        clean_stats=dict(clean_stats or {}),
        page_count=int(page_count) if page_count else None,
    )
    (out / DRAFT_SIDECAR_NAME).write_text(
        json.dumps(asdict(sidecar), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rel = str(out.relative_to(Path(data_dir)))
    log.info("review_draft_saved", doc_id=doc_id, path=rel, chars=len(markdown or ""))
    return rel


def load_review_markdown(data_dir: Path, doc_id: int) -> str:
    path = draft_markdown_path(data_dir, doc_id)
    if not path.is_file():
        raise FileNotFoundError(f"复核草稿不存在：{path}")
    return path.read_text(encoding="utf-8")


def write_review_markdown(data_dir: Path, doc_id: int, markdown: str) -> None:
    path = draft_markdown_path(data_dir, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown or "", encoding="utf-8")


_TABLE_BODY_LIMIT = 500_000
_CAPTION_LIMIT = 500
_OCR_LIMIT = 8_000


def save_review_asset_edits(
    data_dir: Path,
    doc_id: int,
    markdown: str,
    *,
    tables: Sequence[dict[str, Any]] | None = None,
    figures: Sequence[dict[str, Any]] | None = None,
) -> None:
    """保存复核稿。传入 tables/figures 时按这份清单覆盖，没传的一侧保持原样。

    图片只允许改已有图的题注和 OCR，不能换路径、也不能凭空加一张图。
    """
    sidecar = load_review_sidecar(data_dir, doc_id)
    next_tables = (
        list(sidecar.tables)
        if tables is None
        else _tables_from_edits(tables, sidecar.tables)
    )
    next_figures = (
        list(sidecar.figures) if figures is None else _figures_from_edits(sidecar.figures, figures)
    )
    save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown=markdown,
        engine=sidecar.engine,
        page_map=[(int(row[0]), int(row[1]), int(row[2])) for row in sidecar.page_map if len(row) >= 3],
        page_count=sidecar.page_count,
        figures=[
            FigureInput(
                image_path=str(item.get("image_path") or ""),
                caption=str(item.get("caption") or ""),
                ocr_text=str(item.get("ocr_text") or ""),
                page=item.get("page"),
                figure_id=str(item.get("figure_id") or ""),
            )
            for item in next_figures
        ],
        tables=[
            TableInput(
                body=str(item.get("body") or ""),
                caption=str(item.get("caption") or ""),
                page=item.get("page"),
                table_id=str(item.get("table_id") or ""),
                preserve_layout=bool(item.get("preserve_layout")),
            )
            for item in next_tables
        ],
        clean_stats=sidecar.clean_stats,
    )


def _tables_from_edits(
    edits: Sequence[dict[str, Any]],
    existing: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    # UI 回存时可能不带 preserve_layout；按 table_id 从旧稿继承，避免编辑一次就丢版式
    preserve_by_id = {
        str(item.get("table_id") or ""): bool(item.get("preserve_layout")) for item in existing
    }
    out: list[dict[str, Any]] = []
    for item in edits:
        body = str(item.get("body_html") if item.get("body_html") is not None else item.get("body") or "")
        if len(body) > _TABLE_BODY_LIMIT:
            raise ValueError(f"表格 HTML 过长（>{_TABLE_BODY_LIMIT} 字符）")
        page = item.get("page")
        table_id = str(item.get("table_id") or "")
        out.append(
            {
                "table_id": table_id,
                "caption": str(item.get("caption") or "")[:_CAPTION_LIMIT],
                "page": page if isinstance(page, int) else None,
                "body": body,
                "preserve_layout": bool(item.get("preserve_layout"))
                or preserve_by_id.get(table_id, False),
            }
        )
    return out


def _figures_from_edits(
    existing: Sequence[dict[str, Any]],
    edits: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(item.get("figure_id") or ""): item for item in existing}
    out: list[dict[str, Any]] = []
    for item in edits:
        fid = str(item.get("figure_id") or "")
        prev = by_id.get(fid)
        if prev is None:
            raise ValueError(f"未知图片 {fid}")
        updated = dict(prev)
        updated["caption"] = str(item.get("caption") or "")[:_CAPTION_LIMIT]
        if item.get("ocr_text") is not None:
            updated["ocr_text"] = str(item.get("ocr_text") or "")[:_OCR_LIMIT]
        out.append(updated)
    return out


def load_review_sidecar(data_dir: Path, doc_id: int) -> ReviewSidecar:
    path = draft_sidecar_path(data_dir, doc_id)
    if not path.is_file():
        return ReviewSidecar()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ReviewSidecar(
        engine=str(raw.get("engine") or ""),
        page_map=list(raw.get("page_map") or []),
        figures=list(raw.get("figures") or []),
        tables=list(raw.get("tables") or []),
        clean_stats=dict(raw.get("clean_stats") or {}),
        page_count=int(raw["page_count"]) if raw.get("page_count") else None,
    )


def figures_from_sidecar(sidecar: ReviewSidecar) -> list[FigureInput]:
    """从 sidecar 还原 FigureInput；图注清洗交给 ``ingest_markdown``（避免双重 clean）。"""
    out: list[FigureInput] = []
    for item in sidecar.figures:
        out.append(
            FigureInput(
                image_path=str(item.get("image_path") or ""),
                caption=str(item.get("caption") or ""),
                ocr_text=str(item.get("ocr_text") or ""),
                page=item.get("page"),
                figure_id=str(item.get("figure_id") or ""),
            )
        )
    return out


def tables_from_sidecar(sidecar: ReviewSidecar) -> list[TableInput]:
    out: list[TableInput] = []
    for item in sidecar.tables:
        out.append(
            TableInput(
                body=str(item.get("body") or ""),
                caption=str(item.get("caption") or ""),
                page=item.get("page"),
                table_id=str(item.get("table_id") or ""),
                preserve_layout=bool(item.get("preserve_layout")),
            )
        )
    return out


def page_map_from_sidecar(sidecar: ReviewSidecar) -> list[tuple[int, int, int]]:
    result: list[tuple[int, int, int]] = []
    for row in sidecar.page_map:
        if len(row) >= 3:
            result.append((int(row[0]), int(row[1]), int(row[2])))
    return result


def prepare_scan_draft(
    *,
    data_dir: Path,
    doc_id: int,
    raw_markdown: str,
    engine: str,
    page_map: Sequence[tuple[int, int, int]] | None,
    figures: Sequence[FigureInput],
    tables: Sequence[TableInput],
    source_path: str | None = None,
    page_count: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """清洗正文与图注，落盘，返回 (parse_path 相对路径, clean_stats)。"""
    cleaned = clean_markdown(raw_markdown)
    merged_md, merged_tables, merge_stats = merge_continued_tables(
        cleaned.text, list(tables)
    )
    stats = dict(cleaned.stats or {})
    stats.update(merge_stats)
    pdf_pages: list[str] = []
    if source_path:
        pdf_pages = load_pdf_text_pages(Path(source_path))
        merged_md, text_stats = patch_markdown_from_pdf_pages(merged_md, pdf_pages)
        stats.update(text_stats)
    # 先按原题注/画面丢章，再清洗留下的图注（清洗会抹掉「印章」字样）
    kept_figures, dropped_seals = partition_seal_figures(list(figures), data_dir)
    if dropped_seals:
        stats["seal_figures_dropped"] = len(dropped_seals)
        merged_md = strip_figure_anchors(
            merged_md, {str(fig.figure_id or "") for fig in dropped_seals}
        )
        log.info("seal_figures_dropped", doc_id=doc_id, count=len(dropped_seals))
    cleaned_figures = [
        FigureInput(
            image_path=fig.image_path,
            caption=clean_figure_text(fig.caption or ""),
            ocr_text=clean_figure_text(fig.ocr_text or ""),
            page=fig.page,
            figure_id=fig.figure_id,
        )
        for fig in kept_figures
    ]
    last_page = None
    if page_map:
        last_page = max(int(row[2]) for row in page_map if len(row) >= 3)
    skip_crop = bool(pdf_pages) and printed_footer_ending(pdf_pages[-1])
    if source_path and not skip_crop:
        crop = signature_figure(
            data_dir=data_dir,
            doc_id=doc_id,
            pdf_path=Path(source_path),
            page=last_page,
        )
        if crop is not None:
            cleaned_figures.append(crop)
            merged_md = append_handwrite_placeholder(merged_md)
            stats["handwrite_crop"] = 1
            log.info("signature_crop_saved", doc_id=doc_id, page=last_page)
    rel = save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown=merged_md,
        engine=engine,
        page_map=page_map,
        figures=cleaned_figures,
        tables=merged_tables,
        clean_stats=stats,
        page_count=page_count,
    )
    return rel, stats


def mark_document_review(db: Any, doc_id: int, parse_path: str) -> None:
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = ?, parse_path = ?, "
            "updated_at = datetime('now') WHERE doc_id = ?",
            (STATUS_REVIEW, parse_path, doc_id),
        )
        conn.commit()


def upgrade_review_draft_seals(data_dir: Path, doc_id: int) -> dict[str, Any]:
    """已落盘草稿补丢印章图（旧草稿可能仍把公章放在 figures 里）。"""
    try:
        markdown = load_review_markdown(data_dir, doc_id)
    except FileNotFoundError:
        return {}
    sidecar = load_review_sidecar(data_dir, doc_id)
    figures = figures_from_sidecar(sidecar)
    if not figures:
        return {}
    kept, dropped = partition_seal_figures(figures, data_dir)
    if not dropped:
        return {}
    new_md = strip_figure_anchors(markdown, {str(fig.figure_id or "") for fig in dropped})
    clean_stats = dict(sidecar.clean_stats or {})
    clean_stats["seal_figures_dropped"] = int(clean_stats.get("seal_figures_dropped") or 0) + len(dropped)
    save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown=new_md,
        engine=sidecar.engine,
        page_map=page_map_from_sidecar(sidecar),
        figures=kept,
        tables=tables_from_sidecar(sidecar),
        clean_stats=clean_stats,
    )
    log.info("review_draft_seals_dropped", doc_id=doc_id, count=len(dropped))
    return {"seal_figures_dropped": len(dropped)}


def upgrade_review_draft_tables(data_dir: Path, doc_id: int) -> dict[str, Any]:
    """对已落盘的复核草稿补做跨页续表合并（旧草稿可能仍是拆表）。

    有合并时回写 cleaned.md + sidecar；无变化则不动磁盘。
    """
    try:
        markdown = load_review_markdown(data_dir, doc_id)
    except FileNotFoundError:
        return {}
    sidecar = load_review_sidecar(data_dir, doc_id)
    tables = tables_from_sidecar(sidecar)
    if not tables:
        return {}
    new_md, new_tables, stats = merge_continued_tables(markdown, tables)
    if not stats.get("tables_merged_pairs") and not stats.get("tables_repaired"):
        return stats
    clean_stats = dict(sidecar.clean_stats or {})
    clean_stats.update(stats)
    save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown=new_md,
        engine=sidecar.engine,
        page_map=page_map_from_sidecar(sidecar),
        figures=figures_from_sidecar(sidecar),
        tables=new_tables,
        clean_stats=clean_stats,
    )
    return stats


def upgrade_review_draft_tail(
    data_dir: Path,
    doc_id: int,
    source_path: str | None,
) -> dict[str, Any]:
    """旧草稿：签署栏误判改回文字层，印刷页脚不再当作手写截图。"""
    try:
        markdown = load_review_markdown(data_dir, doc_id)
    except FileNotFoundError:
        return {}
    sidecar = load_review_sidecar(data_dir, doc_id)
    pages = load_pdf_text_pages(Path(source_path)) if source_path else []
    new_md, stats = patch_markdown_from_pdf_pages(markdown, pages)
    figures = figures_from_sidecar(sidecar)
    drop_crop = any(is_handwriting_figure(fig) for fig in figures) and bool(pages) and (
        printed_footer_ending(pages[-1])
    )
    if not stats and not drop_crop:
        return {}
    if drop_crop:
        figures = [fig for fig in figures if not is_handwriting_figure(fig)]
        stats = dict(stats)
        stats["handwrite_crop_dropped"] = 1
    clean_stats = dict(sidecar.clean_stats or {})
    clean_stats.update(stats)
    save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown=new_md,
        engine=sidecar.engine,
        page_map=page_map_from_sidecar(sidecar),
        figures=figures,
        tables=tables_from_sidecar(sidecar),
        clean_stats=clean_stats,
    )
    log.info("review_draft_tail_upgraded", doc_id=doc_id, **stats)
    return stats
