"""CRUD 收窄口径 vs 全量口径、以及全量"无门禁 vs 门禁"对比（只读）。

用法：.venv/bin/python eval/crud_compare.py
缺文件时自动跳过，可先跑（用于报告填表）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CRUD = ROOT / "var" / "eval" / "crud"


def load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def num(v: float | None, n: int = 3) -> str:
    return "-" if v is None else f"{v:.{n}f}"


def retr(prefix: str) -> dict[str, Any] | None:
    d = load(CRUD / f"{prefix}.json")
    return d.get("overall") if d else None


def ans(prefix: str) -> dict[str, Any] | None:
    d = load(CRUD / f"{prefix}.json")
    return d.get("overall") if d else None


def main() -> None:
    scoped_retr = retr("retrieval_summary")
    scoped_ans = ans("judge_summary")
    scoped_ref = retr("summary")
    full_retr = retr("summary_full")
    full_ref = retr("summary_full")
    full_ans = ans("judge_summary_judge_full")
    gate_retr = retr("summary_full_gate")
    gate_ans = ans("judge_summary_judge_full_gate")

    print("# CRUD-RAG 口径对比\n")
    print("| 口径 | 题数 | key_info recall@10 | 答案正确率 | 拒答率 |")
    print("|---|---|---|---|---|")
    if scoped_retr and scoped_ans:
        print(f"| 收窄（13,608 篇） | {scoped_retr['n']} | {num(scoped_retr['retrieval_keyinfo_recall@10'])} "
              f"| {pct(scoped_ans['answer_correct_rate'])} | {pct(scoped_ref.get('refused_rate') if scoped_ref else None)} |")
    if full_retr and full_ans:
        print(f"| 全量（83,814 篇） | {full_retr['n']} | {num(full_retr['retrieval_keyinfo_recall@10'])} "
              f"| {pct(full_ans['answer_correct_rate'])} | {pct(full_ref.get('refused_rate') if full_ref else None)} |")
    print("\n> 全量结果未生成时留空；生成后重跑本脚本即可。\n")

    if full_retr and gate_retr and full_ans and gate_ans:
        print("## 全量口径：无门禁 vs 门禁（同一语料快照）\n")
        print("| 指标 | 无门禁 | 门禁（band 0.65–0.85） | 变化 |")
        print("|---|---|---|---|")
        for label, key, digits, fmt in [
            ("key_info recall@10", "retrieval_keyinfo_recall@10", 3, "num"),
            ("答案正确率", "answer_correct_rate", None, "pct"),
            ("部分正确率", "partial_rate", None, "pct"),
            ("拒答率", "refused_rate", None, "pct"),
        ]:
            a = full_retr.get(key) if key.startswith("retrieval") else full_ans.get(key)
            b = gate_retr.get(key) if key.startswith("retrieval") else gate_ans.get(key)
            if a is None or b is None:
                continue
            fa = num(a, digits) if fmt == "num" else pct(a)
            fb = num(b, digits) if fmt == "num" else pct(b)
            print(f"| {label} | {fa} | {fb} | {pct(b - a)} |")

    # 拒答原因（全量门禁）
    reasons = load(CRUD / "summary_full_gate.json")
    if reasons:
        print("\n## 全量门禁：分任务拒答率\n")
        print("| 任务 | n | 拒答率 |")
        print("|---|---|---|")
        for task, d in reasons.get("by_task", {}).items():
            print(f"| {task} | {d['n']} | {pct(d['refused_rate'])} |")


if __name__ == "__main__":
    main()
