"""真实语料测试集问答评测（`var/eval/real/testset_review.xlsx`）。

对每道题跑一次完整链路（检索 → 精排 → 生成），产出：
  * 引用是否命中 gold 文档（doc_id / 标题）；
  * 参考答案的关键片段是否出现在回答里；
  * 拒答率、延迟 P50/P95、失败数。

用法：
    set -a; . ~/.ekbs/secrets.env; set +a
    EKBS_CONFIG=config/application.cloud.yaml \\
      .venv/bin/python eval/real_qa_eval.py --limit 100 --concurrency 2 --user-id 5

默认用 user 5（雷航）——真实语料分片只有这个账号可见。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("EKBS_CONFIG", "config/application.cloud.yaml")

from ekbs.config import load_settings  # noqa: E402
from ekbs.retrieval.pipeline import RetrievalOverrides  # noqa: E402
from ekbs.runtime import Runtime  # noqa: E402

DEFAULT_XLSX = REPO / "var" / "eval" / "real" / "testset_review.xlsx"
OUT_DIR = REPO / "var" / "eval" / "real"

_PUNCT = re.compile(r"[，。；：、,.;:!?！？\n\r\t]+")
_NUM = re.compile(r"[0-9A-Za-z][0-9A-Za-z\-_.]{2,}")


def _segments(text: str, *, min_len: int = 4) -> list[str]:
    """把参考答案切成可判定的关键片段（按标点切 + 抽编号/数字串）。"""
    parts = [p.strip() for p in _PUNCT.split(text or "") if len(p.strip()) >= min_len]
    codes = [m.group(0) for m in _NUM.finditer(text or "")]
    out: list[str] = []
    for item in [*parts, *codes]:
        if item and item not in out:
            out.append(item)
    return out


def _load_rows(path: Path) -> list[dict[str, Any]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(c or "").strip() for c in rows[0]]

    def col(name: str) -> int:
        return header.index(name) if name in header else -1

    idx = {
        "id": col("ID"),
        "question": col("问题"),
        "reference": col("参考答案"),
        "edited_question": col("问题（人工修改）"),
        "edited_answer": col("答案（人工修改）"),
        "gold_doc_ids": col("gold_doc_ids"),
        "gold_titles": col("gold文档"),
        "review_status": col("审核状态"),
    }

    def cell(row: tuple, key: str) -> str:
        i = idx[key]
        if i < 0 or i >= len(row) or row[i] is None:
            return ""
        return str(row[i]).strip()

    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        qid = cell(row, "id")
        if not qid:
            continue
        out.append(
            {
                "id": qid,
                "question": cell(row, "edited_question") or cell(row, "question"),
                "reference": cell(row, "edited_answer") or cell(row, "reference"),
                "gold_doc_ids": [
                    int(x) for x in re.split(r"[,，\s]+", cell(row, "gold_doc_ids")) if x.isdigit()
                ],
                "gold_titles": [t for t in re.split(r"[;；\n]+", cell(row, "gold_titles")) if t.strip()],
                "review_status": cell(row, "review_status"),
            }
        )
    return out


def _judge(row: dict[str, Any], answer: Any) -> dict[str, Any]:
    cited_ids = {int(c.doc_id) for c in answer.citations}
    cited_titles = {str(c.doc_title or "") for c in answer.citations}
    gold_ids = set(row["gold_doc_ids"])
    gold_titles = set(row["gold_titles"])

    doc_hit = bool(gold_ids & cited_ids) or any(
        any(g and (g in t or t in g) for t in cited_titles) for g in gold_titles
    )
    answer_text = str(answer.answer or "")
    segs = _segments(row["reference"])
    seg_hits = [s for s in segs if s in answer_text]
    answer_hit = bool(segs) and len(seg_hits) >= max(1, min(2, len(segs)))
    return {
        "doc_hit": doc_hit,
        "answer_hit": answer_hit,
        "segments": len(segs),
        "seg_hits": len(seg_hits),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="真实语料测试集问答评测")
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--user-id", type=int, default=5)
    parser.add_argument("--config", default="config/application.cloud.yaml")
    parser.add_argument("--fusion", default="", help="覆盖融合：alpha | rrf | wrrf（空=用配置）")
    parser.add_argument("--alpha", type=float, default=None, help="覆盖 hybrid_alpha")
    args = parser.parse_args()

    rows = _load_rows(Path(args.xlsx))
    if args.limit:
        rows = rows[: args.limit]
    print(f"题目：{len(rows)} 条；用户：{args.user_id}；并发：{args.concurrency}", flush=True)

    settings = load_settings(REPO / args.config)
    runtime = Runtime.create(settings, service="eval_real_qa")
    try:
        perm = await runtime.perm_context(args.user_id)
        print(
            f"权限：clearance={perm.clearance} kbs={sorted(perm.visible_kb_ids)} "
            f"shards={sorted(perm.visible_shard_ids)}",
            flush=True,
        )
        overrides = None
        if args.fusion or args.alpha is not None:
            overrides = RetrievalOverrides(
                fusion=(args.fusion or None), hybrid_alpha=args.alpha
            )
            print(f"融合覆盖：fusion={args.fusion or '(配置)'} alpha={args.alpha}", flush=True)
        sem = asyncio.Semaphore(max(1, args.concurrency))
        out: list[dict[str, Any]] = []

        async def one(row: dict[str, Any]) -> None:
            async with sem:
                started = time.perf_counter()
                error = ""
                answer = None
                try:
                    answer = await runtime.pipeline.answer(
                        row["question"], perm, use_cache=False,
                        conversation_id=None, overrides=overrides,
                    )
                except Exception as exc:  # noqa: BLE001 - 单题失败不拖垮整批
                    error = f"{type(exc).__name__}: {exc}"
                elapsed_ms = int((time.perf_counter() - started) * 1000)

                if answer is None:
                    out.append({**row, "error": error, "latency_ms": elapsed_ms})
                    print(f"  [ERR] {row['id']} {error[:120]}", flush=True)
                    return
                judged = _judge(row, answer)
                out.append(
                    {
                        **row,
                        "answer": answer.answer,
                        "refused": bool(answer.refused),
                        "refusal_reason": answer.refusal_reason,
                        "cited_doc_ids": sorted({int(c.doc_id) for c in answer.citations}),
                        "cited_titles": sorted({str(c.doc_title or "") for c in answer.citations}),
                        "n_citations": len(answer.citations),
                        "latency_ms": elapsed_ms,
                        "error": "",
                        **judged,
                    }
                )
                mark = "✓" if judged["doc_hit"] else "✗"
                print(
                    f"  [{mark}] {row['id']} {elapsed_ms}ms refused={answer.refused} "
                    f"cite={len(answer.citations)} doc_hit={judged['doc_hit']} "
                    f"ans_hit={judged['answer_hit']}",
                    flush=True,
                )

        await asyncio.gather(*(one(row) for row in rows))
    finally:
        await runtime.aclose()

    ok = [r for r in out if not r["error"]]
    refused = [r for r in ok if r["refused"]]
    lat = sorted(r["latency_ms"] for r in ok)
    summary = {
        "fusion": args.fusion or "config",
        "alpha": args.alpha,
        "total": len(out),
        "ok": len(ok),
        "errors": len(out) - len(ok),
        "refused": len(refused),
        "refused_rate": round(len(refused) / len(ok), 4) if ok else None,
        "doc_hit": sum(1 for r in ok if r["doc_hit"]),
        "doc_hit_rate": round(sum(1 for r in ok if r["doc_hit"]) / len(ok), 4) if ok else None,
        "answer_hit": sum(1 for r in ok if r["answer_hit"]),
        "answer_hit_rate": round(sum(1 for r in ok if r["answer_hit"]) / len(ok), 4) if ok else None,
        "latency_p50_ms": int(median(lat)) if lat else None,
        "latency_p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
        "latency_max_ms": max(lat) if lat else None,
    }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"qa_eval_{stamp}.json"
    out_path.write_text(
        json.dumps({"summary": summary, "rows": out}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n===== 汇总 =====")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"\n明细：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
