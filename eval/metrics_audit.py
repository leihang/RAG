"""评测指标核对：从各结果 JSON 汇总出报告用的规范数字，方便逐项对账。

只读，不发网络请求。用法：
    .venv/bin/python eval/metrics_audit.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "var" / "eval"


def load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def num(v: float | None, digits: int = 3) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def main() -> None:  # noqa: C901 - 一次性评测脚本
    print("# 评测指标核对表\n")

    rgb = load(EVAL / "rgb" / "retrieval_report.json")
    if rgb:
        s = rgb["summary"]
        print("## RGB 检索（500 题）")
        for name, d in [("overall", s["overall"]), *s["by_subset"].items()]:
            print(
                f"- {name}: n={d['questions']} Hit@10={num(d['hit_at_k'])} MRR={num(d['mrr'])} "
                f"Recall@10={num(d['recall_at_k'])} nDCG@10={num(d['ndcg_at_10'])} "
                f"负例拒答={pct(d['negative_refused_rate'])}"
            )

    base_answer = load(EVAL / "rgb" / "answer_summary_nogate.json")
    gate_answer = load(EVAL / "rgb" / "answer_summary.json")
    for label, d in (("RGB 答案（无门禁）", base_answer), ("RGB 答案（门禁开启，生产 band）", gate_answer)):
        if d:
            o = d["overall"]
            print(f"\n## {label}")
            print(f"- n={o['questions']} 准确率={pct(o['official_accuracy'])} 拒答={pct(o['refused_rate'])} "
                  f"假答案={pct(o.get('fake_answer_rate'))} 平均引用={num(o.get('mean_citations'), 2)}")

    neg = load(EVAL / "rgb-neg" / "retrieval_report.json")
    if neg:
        o = neg["summary"]["overall"]
        print("\n## RGB 负例-only（500 题）")
        print(f"- 拒答率={pct(o['negative_refused_rate'])}")

    mteb = load(EVAL / "mteb" / "report.json")
    if mteb:
        print("\n## DuRetrieval 向量模型")
        print(f"- nDCG@10={num(mteb['ndcg_at_10'], 4)} MRR@10={num(mteb['mrr_at_10'], 4)} "
              f"Recall@100={num(mteb['recall_at_100'], 4)} corpus={mteb['corpus']}")

    ragas = load(EVAL / "rgb" / "ragas_report_5m.json")
    if ragas:
        print("\n## RAGAS 5 项（30 题分层）")
        for k, v in ragas["scores"].items():
            if isinstance(v, float):
                print(f"- {k}={num(v, 4)}")

    print("\n## 拒答策略（节选）")
    for name, path in [
        ("前置判官", EVAL / "rgb" / "no_answer_judge_summary.json"),
        ("answer-verify claim", EVAL / "rgb" / "answer_verify_summary.json"),
    ]:
        d = load(path)
        if d:
            pos = d.get("positive", {})
            negd = d.get("negative", {})
            label = f"{name}({d.get('prompt')})" if d.get("prompt") else name
            print(f"- {label}: 误拒={pct(pos.get('judge_refuse_rate') or pos.get('verify_refuse_rate'))} "
                  f"拒答={pct(negd.get('judge_refuse_rate') or negd.get('verify_refuse_rate'))} "
                  f"(文件: {path.name}, prompt={d.get('prompt')})")

    for label, path in [
        ("CRUD 收窄-检索", EVAL / "crud" / "retrieval_summary.json"),
        ("CRUD 收窄-答案", EVAL / "crud" / "judge_summary.json"),
        ("CRUD 全量-检索", EVAL / "crud" / "summary_full.json"),
        ("CRUD 全量-答案", EVAL / "crud" / "judge_summary_judge_full.json"),
        ("CRUD 全量门禁-检索", EVAL / "crud" / "summary_full_gate.json"),
        ("CRUD 全量门禁-答案", EVAL / "crud" / "judge_summary_judge_full_gate.json"),
    ]:
        d = load(path)
        if not d:
            continue
        if "overall" in d and "answer_correct_rate" not in d["overall"]:
            o = d["overall"]
            print(f"\n## {label}\n- key_info recall@10={num(o.get('retrieval_keyinfo_recall@10'))} "
                  f"hit={pct(o.get('retrieval_hit_rate'))} refused={pct(o.get('refused_rate'))}")
        elif "overall" in d:
            o = d["overall"]
            print(f"\n## {label}\n- 答案覆盖={num(o.get('answer_keyinfo_recall'))} "
                  f"完全正确={pct(o.get('answer_correct_rate'))} 部分={pct(o.get('partial_rate'))}")


if __name__ == "__main__":
    main()
