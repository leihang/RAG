"""模型只产计划：JSON 校验 + 白名单。"""

from __future__ import annotations

import asyncio

from ekbs.provider.base import LLMResult
from ekbs.retrieval.plan_llm import generate_llm_calc_plan, parse_plan_payload

VALID = """{"operation":"compare","years":[2026,2025],
"time":{"kind":"month_range","start_month":1,"end_month":9,"require_complete":true},
"subject_hint":"水费"}"""


def test_parse_valid_plan() -> None:
    plan = parse_plan_payload(VALID)
    assert plan is not None
    assert plan.operation == "compare"
    assert plan.years == (2026, 2025)
    assert plan.time.start_month == 1 and plan.time.end_month == 9


def test_reject_unknown_operation_and_bad_time() -> None:
    assert parse_plan_payload('{"operation":"sql","years":[2026],"time":{"kind":"year"}}') is None
    assert parse_plan_payload('{"operation":"value","years":[2026],"time":{"kind":"month_range"}}') is None
    assert parse_plan_payload("not json") is None


def test_llm_plan_failure_is_none() -> None:
    class Bad:
        async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN201
            return LLMResult(text="随便回答，不是 JSON", model="x")

    assert asyncio.run(generate_llm_calc_plan("2026年水费多少", Bad())) is None
