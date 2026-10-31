"""查询/文档**不对称编码** A/B：同一模型、同一语料、同一批问题，只换编码方式比排序。

## 为什么要单独做这个工具

`eval_retrieval` 跑的是完整链路（稠密+稀疏+融合+精排），变量太多；而 `text_type`
只影响**嵌入**这一步。所以这里把变量隔离干净：

    语料 = 库里同一批块的 retrieval_text（每篇只嵌一次，两种编码各一份）
    查询 = 同一批问题（对称编码用默认，不对称编码用 text_type=query）
    指标 = Hit@1 / Hit@k / MRR（看**排序**，不看绝对余弦 —— 不对称模型的绝对值本来就低）

## 用法

    export EKBS_CONFIG=config/application.cloud.yaml
    .venv/bin/python -m ekbs.tools.eval_asymmetry --golden docs/eval/golden_corpus16.json
    .venv/bin/python -m ekbs.tools.eval_asymmetry --golden … --json --fail-under-gain 0.02

⚠️ 成本：语料与查询各嵌**两遍**（对称 + 不对称）。跑真语料前先看 `--dry-run` 报出的
块数与预估 token。

判据：`question` 的答案所在块用 `must_contain`（原文片段）标记；命中即算对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ekbs.config import Secrets, load_settings  # noqa: E402
from ekbs.db import Database  # noqa: E402
from ekbs.ingest.chunker import count_tokens, retrieval_text  # noqa: E402
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE  # noqa: E402
from ekbs.logging import configure_logging  # noqa: E402
from ekbs.provider.base import model_ref  # noqa: E402
from ekbs.provider.dashscope import DashScopeEmbeddingProvider  # noqa: E402


@dataclass
class Case:
    case_id: str
    question: str
    must_contain: str
    k: int = 5


@dataclass
class Result:
    label: str
    hit1: float
    hitk: float
    mrr: float
    per_case: list[dict] = field(default_factory=list)


def load_cases(path: Path, limit: int | None) -> list[Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for raw in payload.get("cases") or payload:
        cases.append(
            Case(
                case_id=str(raw.get("id") or raw.get("case_id") or len(cases)),
                question=str(raw["question"]),
                must_contain=str(raw.get("must_contain") or ""),
                k=int(raw.get("k") or 5),
            )
        )
    return cases[:limit] if limit else cases


def norm(text: str) -> str:
    return "".join((text or "").split())


def corpus_from_db(settings, only_published: bool = True):
    """取库里可检索块的 ``retrieval_text``（与入库嵌入口径一致）。"""
    db = Database(settings)
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, c.heading_path, c.content, d.title AS doc_title "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            f"WHERE {RETRIEVABLE_PREDICATE} "
            + ("AND d.status = 'published' " if only_published else "")
            + "ORDER BY c.doc_id, c.seq"
        ).fetchall()
    return [
        (
            int(r["chunk_id"]),
            retrieval_text(
                r["heading_path"], str(r["content"]), doc_title=str(r["doc_title"] or "")
            ),
        )
        for r in rows
    ]


def provider(settings, secrets, *, asymmetric: bool):
    cfg = settings.models.embed
    ref = model_ref(
        cfg.provider, cfg.model, dim=cfg.dim, version=cfg.version,
        variant="asym" if asymmetric else "",
    )
    return DashScopeEmbeddingProvider(
        ref=ref,
        api_key=secrets.get("DASHSCOPE_API_KEY"),
        base_url=cfg.base_url,
        batch_size=int(cfg.batch_size),
        timeout_s=float(cfg.timeout_s),
        max_retries=int(cfg.max_retries),
        asymmetric=asymmetric,
    )


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def evaluate(label: str, cases: list[Case], corpus, vectors: list[list[float]],
             query_vectors: list[list[float]]) -> Result:
    per_case, hits1, hitsk, rr = [], 0, 0, 0.0
    for case, qvec in zip(cases, query_vectors, strict=True):
        scored = sorted(
            ((_cos(qvec, vec), chunk_id, text) for (chunk_id, text), vec in zip(corpus, vectors, strict=True)),
            key=lambda item: -item[0],
        )
        target = norm(case.must_contain)
        rank = next(
            (i + 1 for i, (_, _, text) in enumerate(scored) if target and target in norm(text)),
            None,
        )
        if rank == 1:
            hits1 += 1
        if rank is not None and rank <= case.k:
            hitsk += 1
        if rank is not None:
            rr += 1.0 / rank
        per_case.append({"id": case.case_id, "rank": rank,
                         "top": scored[0][1] if scored else None,
                         "top_score": round(scored[0][0], 4) if scored else None})
    total = len(cases) or 1
    return Result(label=label, hit1=hits1 / total, hitk=hitsk / total, mrr=rr / total, per_case=per_case)


async def run(settings, secrets, cases: list[Case], corpus) -> dict:
    texts = [text for _, text in corpus]
    report: dict = {"corpus_chunks": len(corpus), "cases": len(cases)}
    results: dict[str, Result] = {}
    for label, asymmetric in (("A 对称（默认）", False), ("B 不对称（text_type）", True)):
        engine = provider(settings, secrets, asymmetric=asymmetric)
        vectors = await engine.embed(texts)
        # 注意：**不要**把裸字符串传给 embed() —— 它按"一串文本"逐项迭代，
        # 会把问题拆成单字各嵌一次（真的会白烧钱，实测踩到）
        query_vectors = [
            (await engine.embed_query(case.question))
            if asymmetric
            else (await engine.embed_one(case.question))
            for case in cases
        ]
        results[label] = evaluate(label, cases, corpus, vectors, query_vectors)
        total, calls = engine.pop_usage() if hasattr(engine, "pop_usage") else (0, 0)
        report[label] = {
            "ident": engine.ref.ident, "cloud_tokens": int(total), "calls": int(calls),
            "hit1": round(results[label].hit1, 4),
            "hitk": round(results[label].hitk, 4),
            "mrr": round(results[label].mrr, 4),
            "per_case": results[label].per_case,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="不对称编码 A/B（只变嵌入，其他环节不动）")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--limit", type=int, default=None, help="只用前 N 条用例")
    ap.add_argument("--dry-run", action="store_true", help="只报语料与预估 token，不调模型")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-under-gain", type=float, default=None,
                    help="B 的 MRR 相对 A 至少提升这么多，否则退出码 1（A/B 判定用）")
    args = ap.parse_args(argv)

    settings = load_settings()
    # 必须配日志：正式配置把控制台日志送到 **stderr**，否则 structlog 默认打到 stdout，
    # 会把 `--json` 的输出污染成"日志 + JSON"（管道解析直接失败，实测踩到）
    configure_logging(settings, "tools")
    secrets = Secrets()
    cases = load_cases(Path(args.golden), args.limit)
    corpus = corpus_from_db(settings)
    tokens = sum(count_tokens(text) for _, text in corpus) + sum(
        count_tokens(case.question) for case in cases
    )
    # `--json` 时进度信息走 stderr，**stdout 只留 JSON**（否则管道里混进人读文本，解析直接炸）
    header = (
        f"  语料 {len(corpus)} 块（约 {tokens:,} token，两种编码 ×2 ≈ {tokens * 2:,} token）"
        f"；用例 {len(cases)} 条"
    )
    print(header, file=sys.stderr if args.json else sys.stdout)
    if args.dry_run:
        return 0

    report = asyncio.run(run(settings, secrets, cases, corpus))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n  ── A/B 结果（同一语料、同一问题，只换编码）──")
        for label in ("A 对称（默认）", "B 不对称（text_type）"):
            item = report[label]
            print(f"  {label:24} Hit@1 {item['hit1']:.1%}　Hit@k {item['hitk']:.1%}　"
                  f"MRR {item['mrr']:.4f}　(云端 {item['cloud_tokens']:,} token)")
        a, b = report["A 对称（默认）"], report["B 不对称（text_type）"]
        print(f"\n  差值：Hit@k {b['hitk'] - a['hitk']:+.1%}　MRR {b['mrr'] - a['mrr']:+.4f}")
        worse = [c for c in b["per_case"] if c["rank"] is None]
        if worse:
            print(f"  B 组未命中用例：{[c['id'] for c in worse]}")
    if args.fail_under_gain is not None:
        a, b = report["A 对称（默认）"], report["B 不对称（text_type）"]
        if b["mrr"] - a["mrr"] < args.fail_under_gain:
            print(f"\n  ❌ B 相比 A 的 MRR 提升 {b['mrr'] - a['mrr']:+.4f} < 门槛 {args.fail_under_gain}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
