"""签署栏截图只进复核，不进检索正文以外的占位。"""

from __future__ import annotations

from pathlib import Path

from ekbs.ingest.chunker import FigureInput
from ekbs.ingest.scan_review import load_review_markdown, load_review_sidecar, prepare_scan_draft
from ekbs.ingest.signature_crop import (
    append_handwrite_placeholder,
    is_handwriting_figure,
    patch_markdown_from_pdf_pages,
    printed_footer_ending,
)


def test_blank_signature_and_footer_come_from_text_layer() -> None:
    pages = [
        "授权代表签字： 授权代表签字：\n签署日期： 年 月 日\n",
        "甲方单位： 代表人： 乙方单位： 代表人：\n日期： 日期\n",
        "廉政承诺书\n举报电话：023-65479751，举报邮箱：fkzz@cqxykg.com\n承诺方（盖章）\n年 月 日\n",
    ]
    raw = (
        "授权代表签字： 授权代表签字：[手写签字]日期： 年月日\n"
        "甲方单位： 代表人： [手写签字]位： 代表人：\n"
        "日期：\n"
        "# 廉政承诺书\n"
        "4、举报条款正文。\n"
    )
    text, stats = patch_markdown_from_pdf_pages(raw, pages)
    assert "乙方单位" in text
    assert "[手写签字]" not in text
    assert "举报电话：023-65479751" in text
    assert "fkzz@cqxykg.com" in text
    assert "承诺方（盖章）" in text
    assert stats["pdf_signature_restored"] == 2
    assert printed_footer_ending(pages[-1])
    assert not printed_footer_ending(pages[1])


def test_placeholder_once() -> None:
    assert append_handwrite_placeholder("甲方：公司\n").strip().endswith("[手写签字]")
    again = append_handwrite_placeholder("已有 [手写签字]\n")
    assert again.count("[手写签字]") == 1


def test_prepare_draft_attaches_signature_crop(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "合同.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def fake_render(pdf_path: Path, dest_png: Path) -> bool:
        assert pdf_path == pdf
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        dest_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
        return True

    monkeypatch.setattr("ekbs.ingest.signature_crop.render_signature_crop", fake_render)
    prepare_scan_draft(
        data_dir=tmp_path,
        doc_id=80,
        raw_markdown="甲方：公司经办人：日期：24年2月\n乙方：开户银行：工商银行账号：310002240902450123\n",
        engine="mineru",
        page_map=[(0, 1, 1), (10, 8, 8)],
        figures=[],
        tables=[],
        source_path=str(pdf),
    )
    text = load_review_markdown(tmp_path, 80)
    assert "310002240902450123" in text
    assert "[手写签字]" in text
    side = load_review_sidecar(tmp_path, 80)
    crops = [item for item in side.figures if is_handwriting_figure(item)]
    assert len(crops) == 1
    assert crops[0]["caption"] == "签署栏手写，未识别"
    assert crops[0]["ocr_text"] == ""
    assert crops[0]["page"] == 8
    kept = [FigureInput(image_path=crops[0]["image_path"], figure_id=crops[0]["figure_id"])]
    assert is_handwriting_figure(kept[0])
