"""答案质量 judge（阶段 B，RAGAS 0.4.x + qwen-max 判官）。

前提：先跑 eval/answer_eval.py 生成 var/eval/rgb/answers.jsonl（含 contexts）。
运行（eval venv）：
    set -a; . ~/.ekbs/secrets.env; set +a
    var/eval/venv/bin/python eval/ragas_judge.py [--limit 80]

指标：Faithfulness / AnswerRelevancy / ContextPrecision / ContextRecall / AnswerCorrectness。
判官固定 qwen-max（temperature=0），embedding 固定 qwen3.7-text-embedding-flash；
报告里记录模型名与样本数，保证可复现。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RGB = REPO / "var" / "eval" / "rgb"


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 答案质量评测")
    parser.add_argument("--limit", type=int, default=0, help="只判前 N 条（试跑建议 30–80）")
    parser.add_argument("--out", default="var/eval/rgb/ragas_report.json")
    parser.add_argument("--per-subset", type=int, default=0, help="每个子集取前 N 条（0=用 --limit 口径）")
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("缺少 DASHSCOPE_API_KEY（先 source ~/.ekbs/secrets.env）")
        return 2

    from openai import OpenAI

    from ragas import EvaluationDataset, RunConfig, SingleTurnSample, evaluate
    from ragas.embeddings import OpenAIEmbeddings as RagasOpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics import (
        AnswerCorrectness,
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    rows = [r for r in _load_jsonl(RGB / "answers.jsonl") if not r["holdout"]]
    if args.per_subset:
        picked: list[dict] = []
        for subset in ("refine", "int", "fact"):
            picked.extend([r for r in rows if r["subset"] == subset][: args.per_subset])
        rows = picked
    elif args.limit:
        rows = rows[: args.limit]
    samples = [
        SingleTurnSample(
            user_input=r["query"],
            response=r["answer"],
            retrieved_contexts=r.get("contexts") or [],
            reference="；".join(str(a) for a in (r.get("answers") or [])),
        )
        for r in rows
        if r.get("contexts")
    ]
    print(f"待判 {len(samples)} 条（来自 {len(rows)} 条可答样本）")

    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=3)
    llm = llm_factory("qwen-max", client=client)
    embeddings = RagasOpenAIEmbeddings(client=client, model="qwen3.7-text-embedding-flash")
    metrics = [Faithfulness(), AnswerRelevancy(), ContextPrecision(), ContextRecall(), AnswerCorrectness()]
    result = evaluate(
        dataset=EvaluationDataset(samples=samples),
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=False,
        run_config=RunConfig(timeout=180, max_workers=2),
    )
    frame = result.to_pandas()
    skip = {"user_input", "retrieved_contexts", "response", "reference"}
    metric_cols = [c for c in frame.columns if c not in skip]
    import math
    scores = {}
    for col in metric_cols:
        vals = [float(x) for x in frame[col].tolist() if isinstance(x, (int, float)) and not math.isnan(float(x))]
        scores[col] = round(sum(vals) / len(vals), 4) if vals else None
    scores["valid_counts"] = {col: int(sum(1 for x in frame[col].tolist() if isinstance(x, (int, float)) and not math.isnan(float(x)))) for col in metric_cols}
    report = {
        "samples": len(samples),
        "judge": "qwen-max (temperature=0)",
        "embedding": "qwen3.7-text-embedding-flash@1024",
        "scores": scores,
        "per_sample": frame.to_dict(orient="records"),
    }
    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"samples": report["samples"], "scores": scores}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
