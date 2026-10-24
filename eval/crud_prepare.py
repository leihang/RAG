"""CRUD-RAG 语料/题集准备：80k 新闻语料 → 一篇一个 md + 抽样题集。

数据（Apache-2.0）：
    https://github.com/IAAR-Shanghai/CRUD_RAG
    Jiawei Chen? 论文：CRUD-RAG: A Comprehensive Chinese Benchmark for RAG (ACM TOIS 2024)
    语料：data/80000_docs/documents_dup_part_*（一行一篇新闻）
    题集：src/quest_eval/QuestAnswer{1,2,3}Docs_quest_gt_save.json
          {entry_id: {key_info[], question[], answers[]}}

输出：
    var/eval/crud/docs/<hash>.md   语料（去重后）
    var/eval/crud/manifest.json    文档清单
    var/eval/crud/questions.jsonl  抽样题集（key_info 作为检索/答案覆盖依据）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ekbs.ingest.chunker import count_tokens  # noqa: E402

SRC = REPO / "var" / "eval" / "datasets" / "crud"
OUT = REPO / "var" / "eval" / "crud"
TASKS = {
    "1doc": ("QuestAnswer1Doc.json", "q1"),
    "2docs": ("QuestAnswer2Docs.json", "q2"),
    "3docs": ("QuestAnswer3Docs.json", "q3"),
}


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD-RAG 语料/题集准备")
    parser.add_argument("--entries-1doc", type=int, default=100)
    parser.add_argument("--entries-2docs", type=int, default=50)
    parser.add_argument("--entries-3docs", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    limits = {"1doc": args.entries_1doc, "2docs": args.entries_2docs, "3docs": args.entries_3docs}

    # 1) 语料：一行一篇，按文本去重
    texts: dict[str, str] = {}
    files = sorted((SRC / "corpus").glob("*.txt"))
    if not files:
        print(f"缺少语料文件：{SRC / 'corpus'}/*.txt", file=sys.stderr)
        return 2
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if len(line) < 20:
                continue
            texts.setdefault(_hash(line), line)
    print(f"语料文件 {len(files)} 个，去重后文档 {len(texts)} 篇")

    docs_dir = OUT / "docs"
    if docs_dir.exists():
        for old in docs_dir.glob("*.md"):
            old.unlink()
    docs_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"docs": {}}
    sample = list(texts.items())[:1000]
    avg_tokens = sum(count_tokens(t) for _, t in sample) / len(sample) if sample else 0
    for h, text in texts.items():
        title = f"crud-{h[:12]}"
        (docs_dir / f"{h}.md").write_text(text, encoding="utf-8")
        manifest["docs"][h] = {
            "title": title,
            "chars": len(text),
            "path": str((docs_dir / f"{h}.md").relative_to(REPO)),
        }
    manifest["stats"] = {
        "corpus_docs": len(texts),
        "corpus_chars": sum(m["chars"] for m in manifest["docs"].values()),
        "corpus_tokens_est": int(avg_tokens * len(texts)),
    }

    # 2) 题集：每个 task 抽前 N 个 entry，展开成单题
    random.seed(args.seed)
    questions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for task, (filename, prefix) in TASKS.items():
        data = json.loads((SRC / filename).read_text(encoding="utf-8"))
        entries = list(data.items())
        limit = limits[task]
        if limit and limit < len(entries):
            entries = random.sample(entries, limit)
        for entry_id, value in entries:
            key_info = [str(x) for x in value.get("key_info") or []]
            qs = [str(x) for x in value.get("question") or []]
            ans = [str(x) for x in value.get("answers") or []]
            for i, q in enumerate(qs):
                ref = ans[i] if i < len(ans) else ""
                questions.append(
                    {
                        "id": f"crud-{task}-{entry_id}-{i}",
                        "subset": task,
                        "query": q,
                        "reference": ref,
                        "key_info": key_info,
                        "entry_id": entry_id,
                    }
                )
                counts[task] += 1
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "questions.jsonl").open("w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")
    stats = {**manifest["stats"], "questions": len(questions), "by_task": dict(counts)}
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
