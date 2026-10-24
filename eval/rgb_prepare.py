"""RGB 中文基准 → 本地评测语料（JSONL 拆成文档 + 题目）。

数据来源（CC BY-NC-SA 4.0，仅非商业用途，引用见下）：
    Jiawei Chen et al. "Benchmarking Large Language Models in Retrieval-Augmented
    Generation", AAAI 2024. https://arxiv.org/abs/2309.01431
    https://github.com/chen700564/RGB

输入（gitignored，需先下载）：
    var/eval/datasets/rgb/zh_refine.json   300 题（噪声鲁棒 / 负例拒答）
    var/eval/datasets/rgb/zh_int.json      100 题（信息整合）
    var/eval/datasets/rgb/zh_fact.json     100 题（反事实鲁棒）

输出：
    var/eval/rgb/docs/<hash>.md            去重后的文档（一篇一个文件，便于现有管道入库）
    var/eval/rgb/manifest.json             文档清单 + 语料统计
    var/eval/rgb/questions.jsonl           评测题集（含 gold/干扰文档 hash、holdout 标记）

holdout（负例拒答）设计：按题号哈希取约 1/5 的题作为"语料里没有答案"的题，
其文档不进语料库；评测时这些题应被拒答（refused=true）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ekbs.ingest.chunker import count_tokens  # noqa: E402

BASE = REPO / "var" / "eval"
RGB_SRC = BASE / "datasets" / "rgb"
OUT = BASE / "rgb"
HOLDOUT_MOD = 5  # 约 1/5 的题作为负例（语料里没有答案）


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _is_holdout(subset: str, qid: Any) -> bool:
    return int(hashlib.sha1(f"{subset}:{qid}".encode()).hexdigest()[:8], 16) % HOLDOUT_MOD == 0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _collect_refine(rows: list[dict[str, Any]], subset: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        positives = [str(x) for x in row.get("positive") or []]
        negatives = [str(x) for x in row.get("negative") or []]
        out.append(
            {
                "id": f"{subset}-{row['id']}",
                "subset": subset,
                "query": str(row["query"]),
                "answers": [str(a) for a in row.get("answer") or []],
                "gold_slots": [[_hash(p)] for p in positives],
                "distractors": [_hash(n) for n in negatives],
                "doc_texts": positives + negatives,
                "fake_answers": [],
                "holdout": _is_holdout(subset, row["id"]),
            }
        )
    return out


def _collect_int(rows: list[dict[str, Any]], subset: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        slots: list[list[str]] = []
        texts: list[str] = []
        for slot in row.get("positive") or []:
            variants = [str(x) for x in (slot if isinstance(slot, list) else [slot])]
            slots.append([_hash(t) for t in variants])
            texts.extend(variants)
        negatives = [str(x) for x in row.get("negative") or []]
        out.append(
            {
                "id": f"{subset}-{row['id']}",
                "subset": subset,
                "query": str(row["query"]),
                "answers": [str(a) for a in row.get("answer") or []],
                "gold_slots": slots,
                "distractors": [_hash(n) for n in negatives],
                "doc_texts": texts + negatives,
                "fake_answers": [],
                "holdout": _is_holdout(subset, row["id"]),
            }
        )
    return out


def _collect_fact(rows: list[dict[str, Any]], subset: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        positives = [str(x) for x in row.get("positive") or []]
        wrongs = [str(x) for x in row.get("positive_wrong") or []]
        negatives = [str(x) for x in row.get("negative") or []]
        out.append(
            {
                "id": f"{subset}-{row['id']}",
                "subset": subset,
                "query": str(row["query"]),
                "answers": [str(row.get("answer"))],
                "gold_slots": [[_hash(p)] for p in positives],
                "distractors": [_hash(n) for n in negatives],
                "wrong_docs": [_hash(w) for w in wrongs],
                "doc_texts": positives + wrongs + negatives,
                "fake_answers": [str(row.get("fakeanswer"))],
                "holdout": _is_holdout(subset, row["id"]),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="RGB 中文基准 → 评测语料/题集")
    parser.add_argument("--src", type=Path, default=RGB_SRC)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    subsets = {
        "refine": ("zh_refine.json", _collect_refine),
        "int": ("zh_int.json", _collect_int),
        "fact": ("zh_fact.json", _collect_fact),
    }
    questions: list[dict[str, Any]] = []
    for subset, (filename, collect) in subsets.items():
        path = args.src / filename
        if not path.is_file():
            print(f"缺少数据文件：{path}", file=sys.stderr)
            return 2
        questions.extend(collect(_read_jsonl(path), subset))

    # 语料 = 非 holdout 题的文档（holdout 题的文档不入库 → 该题在语料里没有答案）
    corpus: dict[str, str] = {}
    for q in questions:
        if q["holdout"]:
            continue
        for text in q["doc_texts"]:
            corpus.setdefault(_hash(text), text)

    # 文档写盘（一篇一个文件，跑现有 IngestPipeline）
    docs_dir = args.out / "docs"
    for old in docs_dir.glob("*.md"):
        old.unlink()
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_meta: dict[str, dict[str, Any]] = {}
    for h, text in corpus.items():
        title = f"rgb-{h[:12]}"
        (docs_dir / f"{h}.md").write_text(text, encoding="utf-8")
        doc_meta[h] = {
            "title": title,
            "chars": len(text),
            "tokens": count_tokens(text),
            "path": str((docs_dir / f"{h}.md").relative_to(REPO)),
        }

    stats = {
        "questions": len(questions),
        "by_subset": dict(Counter(q["subset"] for q in questions)),
        "holdout": sum(1 for q in questions if q["holdout"]),
        "corpus_docs": len(corpus),
        "corpus_chars": sum(m["chars"] for m in doc_meta.values()),
        "corpus_tokens": sum(m["tokens"] for m in doc_meta.values()),
    }
    manifest = {"stats": stats, "docs": doc_meta}
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.out / "questions.jsonl").open("w", encoding="utf-8") as fh:
        for q in questions:
            row = {k: v for k, v in q.items() if k != "doc_texts"}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"输出：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
