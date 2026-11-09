"""``python -m ekbs.tools.eval_retrieval`` —— 金标检索 Hit@k / MRR（分片质量外评）。

题集用「必须命中的文档标题 / 原文片段」而不是硬绑 ``chunk_id``，重切分后仍然有效。

.. code-block:: bash

    export EKBS_CONFIG=config/application.dev.yaml
    .venv/bin/python -m ekbs.tools.eval_retrieval \\
      --golden docs/eval/golden_qa.sample.json
    .venv/bin/python -m ekbs.tools.eval_retrieval --golden path.json --json --fail-under 0.8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ekbs.config import load_settings
from ekbs.logging import configure_logging, get_logger
from ekbs.runtime import Runtime

log = get_logger(component="tools.eval_retrieval")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN = REPO_ROOT / "docs" / "eval" / "golden_qa.sample.json"


@dataclass
class CaseResult:
    id: str
    question: str
    user: str
    k: int
    hit: bool | None = None
    rank: int | None = None
    refused: bool = False
    refusal_reason: str = ""
    matched_by: str = ""
    top_docs: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)
    hit_at_k: float | None = None
    mrr: float | None = None
    scored: int = 0
    hits: int = 0
    errors: int = 0

    @property
    def ok(self) -> bool:
        return self.errors == 0 and self.scored > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "hits": self.hits,
            "errors": self.errors,
            "hit_at_k": self.hit_at_k,
            "mrr": self.mrr,
            "ok": self.ok,
            "cases": [c.as_dict() for c in self.cases],
        }


def load_golden(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        cases = data
    elif isinstance(data, dict):
        cases = data.get("cases") or data.get("items") or []
    else:
        raise ValueError("金标文件必须是 JSON 对象（含 cases）或数组")
    if not isinstance(cases, list) or not cases:
        raise ValueError("金标文件没有可用的 cases")
    return [c for c in cases if isinstance(c, dict)]


def _user_id(db, dingtalk_userid: str) -> int:
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = ?",
            (dingtalk_userid,),
        ).fetchone()
    if row is None:
        raise LookupError(f"找不到用户 dingtalk_userid={dingtalk_userid}")
    return int(row["user_id"])


def _match_rank(
    chunks: Sequence[Any],
    *,
    must_hit_doc: str | None,
    must_contain: str | None,
    k: int,
) -> tuple[int | None, str]:
    """返回 1-based rank 与匹配方式；未命中返回 (None, '')。"""
    needle_doc = (must_hit_doc or "").strip()
    needle_text = (must_contain or "").strip()
    for idx, chunk in enumerate(chunks[:k], start=1):
        title = str(getattr(chunk, "doc_title", "") or "")
        content = str(getattr(chunk, "content", "") or "")
        if needle_doc and title == needle_doc:
            if needle_text and needle_text not in content:
                # 文档对了但片段不在该块：继续找后续块
                continue
            return idx, "doc" if not needle_text else "doc+contain"
        if not needle_doc and needle_text and needle_text in content:
            return idx, "contain"
    # 文档命中但片段要求未满足：若仅要求文档，上面已返回；若要求片段，再扫一遍只看文档
    if needle_doc and needle_text:
        for _idx, chunk in enumerate(chunks[:k], start=1):
            if str(getattr(chunk, "doc_title", "") or "") == needle_doc:
                return None, "doc_only_miss_contain"
    return None, ""


async def evaluate_cases(
    runtime: Runtime,
    cases: list[dict[str, Any]],
    *,
    default_k: int = 5,
) -> EvalReport:
    report = EvalReport()
    rr_sum = 0.0
    for raw in cases:
        case_id = str(raw.get("id") or raw.get("question") or f"case-{len(report.cases)+1}")
        question = str(raw.get("question") or "").strip()
        user = str(raw.get("user") or raw.get("dingtalk_userid") or "demo_alice").strip()
        k = int(raw.get("k") or default_k)
        result = CaseResult(id=case_id, question=question, user=user, k=k)
        if not question:
            result.error = "missing_question"
            report.cases.append(result)
            report.errors += 1
            continue
        try:
            uid = _user_id(runtime.db, user)
            perm = await runtime.perm_context(uid)
            retrieval = await runtime.pipeline.retrieve(question, perm)
        except Exception as exc:  # 单条失败不拖垮整批
            result.error = str(exc)[:200]
            report.cases.append(result)
            report.errors += 1
            continue

        result.refused = bool(retrieval.refused)
        result.refusal_reason = str(retrieval.refusal_reason or "")
        result.top_docs = [str(c.doc_title or "") for c in retrieval.chunks[:k]]

        must_not = str(raw.get("must_not_hit_doc") or "").strip()
        expect_no_hit = bool(raw.get("expect_no_hit") or raw.get("expect_refuse"))
        must_hit_doc = str(raw.get("must_hit_doc") or "").strip() or None
        must_contain = str(raw.get("must_contain") or "").strip() or None

        if expect_no_hit or must_not:
            leaked = False
            if must_not and any(t == must_not for t in result.top_docs):
                leaked = True
            if expect_no_hit and must_hit_doc and must_hit_doc in result.top_docs:
                leaked = True
            # 红线类：未泄漏 / 按预期拒答或未命中目标 = hit
            result.hit = not leaked
            if result.hit:
                result.rank = 1  # 负向用例成功记 MRR=1
                result.matched_by = "negative_ok"
            else:
                result.matched_by = "negative_leak"
        else:
            rank, how = _match_rank(
                retrieval.chunks,
                must_hit_doc=must_hit_doc,
                must_contain=must_contain,
                k=k,
            )
            result.rank = rank
            result.matched_by = how
            result.hit = rank is not None

        report.cases.append(result)
        if result.error:
            report.errors += 1
            continue
        report.scored += 1
        if result.hit:
            report.hits += 1
            if result.rank and result.rank > 0:
                rr_sum += 1.0 / float(result.rank)

    if report.scored:
        report.hit_at_k = report.hits / report.scored
        report.mrr = rr_sum / report.scored
    return report


def _print_human(report: EvalReport) -> None:
    hit = f"{report.hit_at_k:.1%}" if report.hit_at_k is not None else "n/a"
    mrr = f"{report.mrr:.3f}" if report.mrr is not None else "n/a"
    print(
        f"评分 {report.scored} 条（错误 {report.errors}）；"
        f"Hit@k={hit}  hits={report.hits}  MRR={mrr}"
    )
    print("-" * 72)
    for c in report.cases:
        if c.error:
            print(f"[ERR] {c.id}: {c.error}")
            continue
        mark = "HIT" if c.hit else "MISS"
        extra = f"rank={c.rank}" if c.rank else c.matched_by or ""
        print(f"[{mark}] {c.id}  user={c.user}  k={c.k}  {extra}")
        print(f"      Q: {c.question}")
        if c.top_docs:
            print(f"      top: {c.top_docs[:5]}")
        if c.refused:
            print(f"      refused={c.refusal_reason}")


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="金标检索 Hit@k / MRR 评估")
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help=f"金标 JSON（默认 {DEFAULT_GOLDEN}）",
    )
    parser.add_argument("--k", type=int, default=5, help="默认 Top-k（可被单条覆盖）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Hit@k 低于该阈值则退出码 1（例如 0.8）",
    )
    args = parser.parse_args(argv)

    golden_path = args.golden
    if not golden_path.is_file():
        print(f"金标文件不存在：{golden_path}", file=sys.stderr)
        return 2

    cases = load_golden(golden_path)
    settings = load_settings()
    configure_logging(settings, "tools")
    if not settings.db_path.exists():
        print(f"数据库不存在：{settings.db_path}", file=sys.stderr)
        return 2

    runtime = Runtime.create(settings, service="eval_retrieval")
    try:
        report = await evaluate_cases(runtime, cases, default_k=args.k)
    finally:
        await runtime.aclose()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    log.info(
        "eval_retrieval_done",
        scored=report.scored,
        hits=report.hits,
        hit_at_k=report.hit_at_k,
        mrr=report.mrr,
        errors=report.errors,
    )
    if args.fail_under is not None and (report.hit_at_k is None or report.hit_at_k < float(args.fail_under)):
        return 1
    return 0 if report.errors == 0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
