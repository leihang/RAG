"""``python -m ekbs.tools.eval_switch_sweep`` —— 检索开关 A/B 对比（同一套金标题集）。

## 为什么需要它

`eval_retrieval` 只跑**配置里当前那一套**开关，回答不了"图片多模态旁路 / MultiQuery /
融合方式 / 父块扩展 各自到底有没有用"。这套工程四条增强（`课程 17/18/19`）加进来时
**没有任何 A/B 证据**；而每条增强都可能是在往 RRF 里注水 —— 尤其图片旁路每问一次就
塞 30 个候选进融合，帮忙还是添乱必须量。

做法：复用 `eval_retrieval` 的**打分口径**（`_match_rank` 与命中/MRR 的定义一字不改），
只把 `RetrievalOverrides` 换成一串变体重跑，输出同口径对比表。因此两个工具的
Hit@k / MRR **可直接互相印证**（同一套题下标出的基线必须与 `eval_retrieval` 一致）。

负例题单独处理：它们看的是 **Top-1 rerank 分数**与是否泄漏到预期外的文档 ——
正例的 Hit@k 完全看不出"把无关内容顶进 Top-k"的代价，而噪声恰好是增强最容易带来的。

.. code-block:: bash

    export EKBS_CONFIG=config/application.cloud.yaml
    .venv/bin/python -m ekbs.tools.eval_switch_sweep \\
      --golden docs/eval/golden_corpus16.json \\
      --negative docs/eval/golden_corpus16_negative.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ekbs.config import load_settings
from ekbs.logging import configure_logging, get_logger
from ekbs.retrieval.pipeline import RetrievalOverrides
from ekbs.runtime import Runtime
from ekbs.tools.eval_retrieval import _match_rank, _user_id, load_golden

log = get_logger(component="tools.eval_switch_sweep")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN = REPO_ROOT / "docs" / "eval" / "golden_corpus16.json"
DEFAULT_NEGATIVE = REPO_ROOT / "docs" / "eval" / "golden_corpus16_negative.json"


@dataclass(frozen=True)
class Variant:
    """一个待比较的开关组合（None = 用配置里的值）。"""

    name: str
    note: str
    overrides: RetrievalOverrides


def default_variants() -> list[Variant]:
    """对照组的选法：一次只关一个，才看得出是谁的贡献。

    * ``dense_only`` / ``sparse_only`` 用 α 融合模拟"只用一侧"—— 这样能顺带回答
      **Doc2Query 的价值**（它只写 FTS，属于稀疏侧；文档里说它把口语召回从 ~67% 提到接近满分）。
    * ``no_figure`` 是本次最想验的：图片多模态旁路。
    """
    return [
        Variant("baseline", "配置原样（rrf + multi_query=3 + 图片旁路开）", RetrievalOverrides()),
        Variant("no_figure", "关掉图片多模态旁路", RetrievalOverrides(figure_multimodal=False)),
        Variant("no_multiquery", "稀疏侧只跑主查询（multi_query=1）", RetrievalOverrides(multi_query=1)),
        Variant("dense_only", "只用向量（α=1）", RetrievalOverrides(fusion="alpha", hybrid_alpha=1.0)),
        Variant("sparse_only", "只用 BM25（α=0，含 Doc2Query 问句行）", RetrievalOverrides(fusion="alpha", hybrid_alpha=0.0)),
        Variant("no_parent_expand", "不做父块扩展", RetrievalOverrides(parent_expand=False)),
        Variant("dense_topk_60", "向量召回扩到 60（看召回深度是否是瓶颈）", RetrievalOverrides(dense_top_k=60)),
        # 同义改写题集上发现"只用向量"明显好于 RRF 融合（稀疏侧在口语问法上只有 47%），
        # 于是把加权融合的中间档也纳入对比 —— 选型必须在**两套题**上都看，免得只对一套过拟合。
        Variant("alpha_85", "加权融合偏向向量（α=0.85）", RetrievalOverrides(fusion="alpha", hybrid_alpha=0.85)),
        Variant("alpha_70", "加权融合 α=0.70", RetrievalOverrides(fusion="alpha", hybrid_alpha=0.70)),
    ]


@dataclass
class NegCase:
    """一条负例题的观测。

    ⚠️ 必须用 ``top_score`` 而不是 ``chunks[0].rerank_score``：**拒答时 chunks 会被清空**，
    拿它统计等于"只统计了没被拒的那几条"—— 我第一版就是这么写的，结果 8 题里只算了 1 题，
    均值还因此恒等于同一个数（看起来像开关完全无效，其实是指标坏了）。
    """

    id: str
    question: str
    score: float | None
    refused: bool
    top_chunk: int | None = None


@dataclass
class VariantResult:
    name: str
    note: str
    scored: int = 0
    hits: int = 0
    errors: int = 0
    rr_sum: float = 0.0
    misses: list[str] = None  # type: ignore[assignment]
    neg_cases: list[NegCase] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.misses = [] if self.misses is None else self.misses
        self.neg_cases = [] if self.neg_cases is None else self.neg_cases

    @property
    def neg_scores(self) -> list[float]:
        return [c.score for c in self.neg_cases if c.score is not None]

    @property
    def neg_refused(self) -> int:
        return sum(1 for c in self.neg_cases if c.refused)

    @property
    def neg_leaked(self) -> list[NegCase]:
        """没被拒答 = 会拿无关内容去生成答案 —— 这才是负例题要抓的东西。"""
        return [c for c in self.neg_cases if not c.refused]

    @property
    def hit_at_k(self) -> float:
        return self.hits / self.scored if self.scored else 0.0

    @property
    def mrr(self) -> float:
        return self.rr_sum / self.scored if self.scored else 0.0

    @property
    def neg_top1_mean(self) -> float | None:
        scores = self.neg_scores
        return sum(scores) / len(scores) if scores else None

    @property
    def neg_top1_max(self) -> float | None:
        return max(self.neg_scores) if self.neg_scores else None


async def run_variant(
    runtime: Runtime,
    variant: Variant,
    positives: list[dict[str, Any]],
    negatives: list[dict[str, Any]],
    *,
    default_perm: Any,
    user_ids: dict[str, int],
) -> VariantResult:
    out = VariantResult(name=variant.name, note=variant.note)
    for raw in positives:
        question = str(raw.get("question") or "").strip()
        if not question:
            out.errors += 1
            continue
        k = int(raw.get("k") or 5)
        user = str(raw.get("user") or "demo_alice")
        perm = default_perm if user == "demo_alice" else await runtime.perm_context(user_ids[user])
        try:
            retrieval = await runtime.pipeline.retrieve(
                question, perm, overrides=variant.overrides
            )
        except Exception as exc:  # 单条失败不拖垮整批
            log.warning("sweep_case_failed", variant=variant.name, case=raw.get("id"), error=str(exc)[:120])
            out.errors += 1
            continue
        rank, _how = _match_rank(
            retrieval.chunks,
            must_hit_doc=(str(raw.get("must_hit_doc") or "").strip() or None),
            must_contain=(str(raw.get("must_contain") or "").strip() or None),
            k=k,
        )
        out.scored += 1
        if rank is not None:
            out.hits += 1
            out.rr_sum += 1.0 / float(rank)
        else:
            out.misses.append(str(raw.get("id") or question))

    # 负例：看 Top-1 分数是否被抬上来（正例的 Hit@k 对这种"注水"完全不敏感）
    for raw in negatives:
        question = str(raw.get("question") or "").strip()
        if not question:
            continue
        try:
            retrieval = await runtime.pipeline.retrieve(
                question, default_perm, overrides=variant.overrides, collect_debug=True
            )
        except Exception:
            continue
        scores = ((retrieval.stage_debug or {}).get("rerank") or {}).get("scores") or []
        top_chunk = int(scores[0]["chunk_id"]) if scores and scores[0].get("chunk_id") else None
        out.neg_cases.append(
            NegCase(
                id=str(raw.get("id") or question),
                question=question,
                # top_score 在拒答时仍然有值（chunks 才是被清空的那个）
                score=retrieval.top_score,
                refused=bool(retrieval.refused),
                top_chunk=top_chunk,
            )
        )
    return out


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索开关 A/B 对比（同金标题集）")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--negative", type=Path, default=DEFAULT_NEGATIVE)
    parser.add_argument("--json", action="store_true", help="额外输出 JSON")
    parser.add_argument("--only", default="", help="只跑指定变体（逗号分隔）")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="不做 A/B，改为**重校拒答阈值**：打印正/负例 top_score 分布与逐阈值误拒/漏放表",
    )
    args = parser.parse_args(argv)

    for path in (args.golden, args.negative):
        if not path.is_file():
            print(f"金标题集不存在：{path}", file=sys.stderr)
            return 2

    settings = load_settings()
    configure_logging(settings, "tools")
    if not settings.db_path.exists():
        print(f"数据库不存在：{settings.db_path}", file=sys.stderr)
        return 2
    runtime = Runtime.create(settings, service="eval_switch_sweep")
    positives = load_golden(args.golden)
    negatives = load_golden(args.negative)

    user_ids: dict[str, int] = {}
    for raw in positives:
        user = str(raw.get("user") or "demo_alice")
        if user not in user_ids:
            user_ids[user] = _user_id(runtime.db, user)
    default_perm = await runtime.perm_context(user_ids["demo_alice"])

    if args.calibrate:
        try:
            return await _calibrate(runtime, positives, negatives, default_perm)
        finally:
            await runtime.aclose()

    try:
        return await _sweep(runtime, positives, negatives, user_ids, default_perm, args)
    finally:
        await runtime.aclose()


async def _calibrate(runtime, positives, negatives, default_perm) -> int:
    """重校拒答阈值：**换融合方式 / 换语料 / 换精排模型之后都必须重跑**。

    只看正例的 Hit@k 会漏掉"该拒的没拒"（把无关内容交给 LLM 去圆），所以两侧都要量：
    正例的 top_score 下限 = 阈值能高到哪；负例的 top_score 上限 = 阈值不能低于哪。
    两带一旦重叠，就不存在"既能全答对、又不误答"的单一阈值 —— 这时要如实说明，
    并选一个误拒/漏放都可接受的折中（而不是假装存在干净分界）。
    """
    import statistics as st

    rows: list[tuple[str, str, float | None, bool]] = []
    for kind, cases in (("正例", positives), ("负例", negatives)):
        for case in cases:
            question = str(case.get("question") or "").strip()
            if not question:
                continue
            retrieval = await runtime.pipeline.retrieve(question, default_perm)
            rows.append((
                kind,
                str(case.get("id") or question),
                retrieval.top_score,
                bool(retrieval.refused),
            ))

    pos = sorted(x[2] for x in rows if x[0] == "正例" and x[2] is not None)
    neg = sorted(x[2] for x in rows if x[0] == "负例" and x[2] is not None)
    if not pos or not neg:
        print("正例或负例缺分数，无法校准", file=sys.stderr)
        return 2
    p10 = pos[max(0, len(pos) // 10 - 1)]
    print(f"正例 top_score：n={len(pos)}  min={pos[0]:.3f}  P10={p10:.3f}  中位={st.median(pos):.3f}  max={pos[-1]:.3f}")
    print(f"负例 top_score：n={len(neg)}  min={neg[0]:.3f}  中位={st.median(neg):.3f}  max={neg[-1]:.3f}")
    if neg[-1] >= pos[0]:
        print(f"⚠️ 两带**重叠**（负例最高 {neg[-1]:.3f} ≥ 正例最低 {pos[0]:.3f}）→ 单一阈值无法干净分界")
    else:
        print(f"两带分离：{neg[-1]:.3f} ←→ {pos[0]:.3f}（中点 {(neg[-1] + pos[0]) / 2:.3f}）")
    print()
    print(f"{'阈值':>6}{'误拒正例':>10}{'漏放负例':>10}   说明")
    lo = min(min(pos), min(neg))
    for t in [round(lo + 0.05 * i, 2) for i in range(4, 17)]:
        fp = [x[1] for x in rows if x[0] == "正例" and x[2] is not None and x[2] < t]
        leak = [(x[1], round(x[2] or 0, 3)) for x in rows if x[0] == "负例" and x[2] is not None and x[2] >= t]
        mark = ""
        if not fp and not leak:
            mark = "  ← 两侧都干净"
        print(f"{t:>6.2f}{len(fp):>10}{len(leak):>10}   {('漏放: ' + str(leak)) if leak else (('误拒: ' + str(fp)) if fp else '')}{mark}")
    return 0


async def _sweep(runtime, positives, negatives, user_ids, default_perm, args) -> int:
    variants = default_variants()
    if args.only:
        wanted = {v.strip() for v in args.only.split(",") if v.strip()}
        variants = [v for v in variants if v.name in wanted]

    results: list[VariantResult] = []
    for variant in variants:
        result = await run_variant(
            runtime, variant, positives, negatives,
            default_perm=default_perm, user_ids=user_ids,
        )
        results.append(result)
        neg_mean = result.neg_top1_mean
        print(
            f"  {variant.name:16} Hit@5={result.hit_at_k:6.1%}  MRR={result.mrr:.3f}  "
            f"负例Top1均值={(f'{neg_mean:.3f}' if neg_mean is not None else 'n/a'):>6}  "
            f"负例拒答={result.neg_refused}/{len(negatives)}"
        )
        if result.misses:
            print(f"      未命中：{', '.join(result.misses)}")
        for leak in result.neg_leaked:
            print(f"      ⚠ 负例漏过：{leak.id}  top_score={leak.score:.3f}  chunk={leak.top_chunk}")

    print()
    print(f"正例 {len(positives)} 题 / 负例 {len(negatives)} 题；命中口径与 eval_retrieval 一致")
    baseline = next((r for r in results if r.name == "baseline"), None)
    if baseline is not None:
        for r in results:
            if r.name == "baseline":
                continue
            dh = r.hit_at_k - baseline.hit_at_k
            dm = r.mrr - baseline.mrr
            print(
                f"  {r.name:16} 相对基线：Hit@5 {dh:+.1%}  MRR {dm:+.3f}"
                + ("   ← 变差" if dh < 0 or dm < -0.02 else "")
            )
    if args.json:
        print(json.dumps(
            [
                {
                    "variant": r.name, "note": r.note, "scored": r.scored, "hits": r.hits,
                    "errors": r.errors, "hit_at_k": r.hit_at_k, "mrr": r.mrr,
                    "misses": r.misses, "neg_top1_mean": r.neg_top1_mean,
                    "neg_top1_min": (min(r.neg_scores) if r.neg_scores else None),
                    "neg_top1_max": r.neg_top1_max, "neg_refused": r.neg_refused,
                    "neg_leaked": [
                        {"id": c.id, "question": c.question, "score": c.score, "top_chunk": c.top_chunk}
                        for c in r.neg_leaked
                    ],
                }
                for r in results
            ],
            ensure_ascii=False,
        ))
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
