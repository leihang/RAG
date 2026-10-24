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
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("缺少 DASHSCOPE_API_KEY（先 source ~/.ekbs/secrets.env）")
        return 2

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        AnswerCorrectness,
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    rows = [r for r in _load_jsonl(RGB / "answers.jsonl") if not r["holdout"]]
    if args.limit:
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
    llm = LangchainLLMWrapper(
        ChatOpenAI(model="qwen-max", base_url=base_url, api_key=api_key, temperature=0)
    )
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            model="qwen3.7-text-embedding-flash",
            base_url=base_url,
            api_key=api_key,
            dimensions=1024,
        )
    )
    metrics = [Faithfulness(), AnswerRelevancy(), ContextPrecision(), ContextRecall(), AnswerCorrectness()]
    result = evaluate(
        dataset=EvaluationDataset(samples=samples),
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=False,
    )
    scores = {
        k: (None if v is None else round(float(v), 4))
        for k, v in dict(result).items()
        if isinstance(v, (int, float)) or v is None
    }
    report = {
        "samples": len(samples),
        "judge": "qwen-max (temperature=0)",
        "embedding": "qwen3.7-text-embedding-flash@1024",
        "scores": scores,
        "per_sample": [
            {k: (None if v is None else (round(float(v), 4) if isinstance(v, (int, float)) else v)) for k, v in row.items()}
            for row in result.to_pandas().to_dict(orient="records")
        ],
    }
    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"samples": report["samples"], "scores": scores}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
