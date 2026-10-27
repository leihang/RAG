"""重新解析/重嵌入后，把测试集的 gold_chunk_ids 重映射到当前分块。

用法：
    var/eval/venv/bin/python eval/remap_testset_gold.py \
      --testset var/eval/real/testset_raw.jsonl \
      --xlsx var/eval/real/testset_review.xlsx \
      --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "var" / "index" / "ekbs.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", default=str(REPO / "var/eval/real/testset_raw.jsonl"))
    parser.add_argument("--xlsx", default=str(REPO / "var/eval/real/testset_review.xlsx"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    from rapidfuzz import fuzz

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    path = Path(args.testset)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = 0
    for row in rows:
        docs = [int(d) for d in row.get("gold_doc_ids") or []]
        if not docs:
            continue
        # 当前这些 gold 文档的分块
        ph = ",".join("?" for _ in docs)
        chunks = [dict(r) for r in con.execute(
            f"SELECT chunk_id, doc_id, content FROM ekbs_chunk WHERE doc_id IN ({ph})", docs
        )]
        if not chunks:
            continue
        old = {int(c) for c in row.get("gold_chunk_ids") or []}
        valid = {int(c["chunk_id"]) for c in chunks}
        if old <= valid and old:
            continue
        picked: list[int] = []
        for text in row.get("reference_contexts") or []:
            text = str(text or "").strip()
            if not text:
                continue
            scored = sorted(
                ((fuzz.partial_ratio(text[:4000], str(c["content"])[:4000]), int(c["chunk_id"]))
                 for c in chunks),
                reverse=True,
            )
            for score, cid in scored[:2]:
                if score >= 60 and cid not in picked:
                    picked.append(cid)
        row["gold_chunk_ids"] = picked
        row["review_notes"] = (row.get("review_notes") or "") + " 重解析后 gold 分块已重映射，请复核。"
        changed += 1
    con.close()
    print("重映射行数", changed)
    if not args.apply:
        return 0
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    # 重建 xlsx
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "测试集复核"
    headers = ["ID", "合成类型", "问题", "参考答案", "问题（人工修改）", "答案（人工修改）",
               "gold_doc_ids", "gold文档", "gold_chunk_ids", "审核状态", "审核意见"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        ws.append([r["id"], r["synthesizer"], r["question"], r["reference"],
                   r["edited_question"], r["edited_answer"],
                   ",".join(map(str, r["gold_doc_ids"])), "；".join(r.get("gold_doc_titles") or []),
                   ",".join(map(str, r["gold_chunk_ids"])), r["review_status"], r["review_notes"]])
    for i, w in enumerate([10, 16, 45, 55, 35, 35, 16, 40, 20, 12, 35], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{ws.max_row}"
    wb.save(args.xlsx)
    print("写出", path, args.xlsx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
