"""在 CRUD 语料里定位抽样题目的源文档（用参考答案/关键信息做精确子串匹配）。

因为 CRUD 题集只给 key_info/answer、不给源 doc id，这里：
1) 把 48 个语料分片拼成一棵大文本（记录每篇起始 offset）；
2) 对每题用 reference + key_info 做子串查找 → 命中 offset → 定位源文档 hash；
3) 输出 source_docs.json，并对比已入库文档，算出还需补灌哪些。

输出：var/eval/crud/source_docs.json
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "var" / "eval" / "datasets" / "crud" / "corpus"
OUT = REPO / "var" / "eval" / "crud"


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="定位 CRUD 抽样题目的源文档")
    parser.add_argument("--questions", default=str(OUT / "questions.jsonl"))
    args = parser.parse_args()

    # 1) 拼接语料（一篇一行；空行跳过），记录 offset→hash
    parts: list[str] = []
    offsets: list[int] = []  # 每篇起始 offset
    hashes: list[str] = []
    pos = 0
    for path in sorted(SRC.glob("*.txt")):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if len(line) < 20:
                continue
            offsets.append(pos)
            hashes.append(_hash(line))
            parts.append(line)
            pos += len(line) + 1
    big = "\n".join(parts)
    print(f"语料 {len(hashes)} 篇 / {len(big)/1e6:.1f}M 字符")

    # 2) 每题找源文档
    questions = [json.loads(line) for line in open(args.questions, encoding="utf-8")]
    sources: dict[str, set[str]] = {}
    missing = 0
    for q in questions:
        found: set[str] = set()
        needles = [str(q.get("reference") or "")] + [str(x) for x in (q.get("key_info") or [])]
        for needle in needles:
            needle = needle.strip()
            if len(needle) < 2:
                continue
            start = big.find(needle)
            while start != -1 and len(found) < 5:
                idx = bisect.bisect_right(offsets, start) - 1
                if 0 <= idx < len(hashes):
                    found.add(hashes[idx])
                start = big.find(needle, start + 1)
            if found:
                break
        if not found:
            missing += 1
        sources[q["id"]] = found

    # 3) 对比已入库
    import sqlite3

    con = sqlite3.connect(OUT.parent / "runtime-crud" / "index" / "ekbs.db")
    ingested = {
        str(row[0]).rsplit("/", 1)[-1]
        for row in con.execute("SELECT source_path FROM ekbs_document WHERE source_path LIKE 'bench://crud/%'")
    }
    all_sources = set().union(*sources.values()) if sources else set()
    need = sorted(all_sources - ingested)
    payload: dict[str, Any] = {
        "questions": len(questions),
        "questions_without_source": missing,
        "source_docs": len(all_sources),
        "already_ingested": len(all_sources & ingested),
        "need_ingest": len(need),
        "ingested_total": len(ingested),
        "per_question": {qid: sorted(v) for qid, v in sources.items()},
        "need_hashes": need,
    }
    (OUT / "source_docs.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k not in ("per_question", "need_hashes")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
