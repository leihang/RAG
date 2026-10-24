"""构建 RGB「负例拒答」专用语料（官方 noise_rate=1 口径的本地版）。

思路：只把**负例文档**（distractor）入库，且排除任何在别处被标为 positive 的文本，
这样问题在语料里确实没有答案 → 理想行为是拒答（refused=true）。

输入：var/eval/rgb/questions.jsonl（由 rgb_prepare.py 生成，用于拿负例 hash）
      原始 RGB JSONL（重新读 zh_refine.json 等，因为 holdout 文档不在 manifest 里）
输出：var/eval/rgb-neg/{docs/*.md, manifest.json, questions.jsonl}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ekbs.ingest.chunker import count_tokens  # noqa: E402

BASE = REPO / "var" / "eval"
SRC = BASE / "datasets" / "rgb"
OUT = BASE / "rgb-neg"


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _flatten_positive(row: dict[str, Any], subset: str) -> list[str]:
    out: list[str] = []
    for item in row.get("positive") or []:
        if isinstance(item, list):
            out.extend(str(x) for x in item)
        else:
            out.append(str(item))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 RGB 负例拒答语料")
    parser.add_argument("--subsets", default="refine,int,fact")
    args = parser.parse_args()
    wanted = {s.strip() for s in args.subsets.split(",") if s.strip()}

    source = {
        "refine": ("zh_refine.json", "refine"),
        "int": ("zh_int.json", "int"),
        "fact": ("zh_fact.json", "fact"),
    }
    all_positives: set[str] = set()
    negatives: dict[str, str] = {}
    questions: list[dict[str, Any]] = []
    for subset in wanted:
        filename, _ = source[subset]
        rows = _read_jsonl(SRC / filename)
        for row in rows:
            positives = _flatten_positive(row, subset)
            for text in positives:
                all_positives.add(_hash(text))
        for row in rows:
            negs = [str(x) for x in row.get("negative") or []]
            keep = []
            for text in negs:
                h = _hash(text)
                if h in all_positives:
                    continue  # 该文本在别处是正例 → 不能用（会误判为可答）
                negatives.setdefault(h, text)
                keep.append(h)
            questions.append(
                {
                    "id": f"neg-{subset}-{row['id']}",
                    "subset": subset,
                    "query": str(row["query"]),
                    "holdout": True,  # 全部按"应拒答"统计
                    "gold_slots": [],
                    "answers": [str(a) for a in row.get("answer") or []]
                    if isinstance(row.get("answer"), list)
                    else [str(row.get("answer"))],
                    "negative_docs": keep,
                }
            )

    out_docs = OUT / "docs"
    if out_docs.exists():
        for old in out_docs.glob("*.md"):
            old.unlink()
    out_docs.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"docs": {}}
    for h, text in negatives.items():
        title = f"rgbneg-{h[:12]}"
        (out_docs / f"{h}.md").write_text(text, encoding="utf-8")
        manifest["docs"][h] = {
            "title": title,
            "chars": len(text),
            "tokens": count_tokens(text),
            "path": str((out_docs / f"{h}.md").relative_to(REPO)),
        }
    manifest["stats"] = {
        "questions": len(questions),
        "by_subset": dict(Counter(q["subset"] for q in questions)),
        "neg_docs": len(negatives),
        "neg_tokens": sum(m["tokens"] for m in manifest["docs"].values()),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "questions.jsonl").open("w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(json.dumps(manifest["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
