"""模型只产计划：把自然语言数值问题编译成 CalcPlan。

模型只允许输出结构化计划（operation/years/time/...），不输出答案或数字；
执行器只接受白名单 operation，并重新校验年份/时间边界。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ekbs.logging import get_logger
from ekbs.retrieval.calc_plan import CalcPlan
from ekbs.retrieval.table_time import TimeSpec

log = get_logger(component="retrieval.plan_llm")

_ALLOWED_OPS = {"value", "compare", "avg", "ratio", "rank", "binary", "filter"}
_ALLOWED_TIME_KINDS = {"month_range", "quarter", "half", "ytd", "year"}
_SYSTEM = """你是表格问答的计划生成器，不是回答器。
只输出一个 JSON 对象，不要解释、不要 Markdown、不要任何答案数字。
允许的 operation：value/compare/avg/ratio/rank/binary/filter。
字段：
- operation: 字符串
- years: 整数数组（问题里出现的年份）
- time: {kind: month_range|quarter|half|ytd|year, start_month?:1-12, end_month?:1-12, require_complete?:bool}
- subject_hint: 指标提示（可空）
- top: rank 时的数量
- direction: rank 时 asc|desc
- binary_op: add|sub|mul|div
- filter_mode: include|exclude
只做意图/计划分类，不要计算。"""


def _strip_json(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    return raw[start:end + 1] if start >= 0 and end > start else ""


def parse_plan_payload(  # noqa: C901
    text: str,
) -> CalcPlan | None:
    raw = _strip_json(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    operation = str(data.get("operation") or "")
    if operation not in _ALLOWED_OPS:
        return None
    years = data.get("years")
    if not isinstance(years, list) or not years:
        return None
    clean_years: list[int] = []
    for year in years:
        if not isinstance(year, int) or not (2000 <= year <= 2100):
            return None
        clean_years.append(year)
    raw_time = data.get("time")
    if not isinstance(raw_time, dict):
        return None
    kind = str(raw_time.get("kind") or "")
    if kind not in _ALLOWED_TIME_KINDS:
        return None
    try:
        time = TimeSpec(
            kind=kind,
            start_month=(
                int(raw_time["start_month"]) if raw_time.get("start_month") is not None else None
            ),
            end_month=(
                int(raw_time["end_month"]) if raw_time.get("end_month") is not None else None
            ),
            require_complete=bool(raw_time.get("require_complete", True)),
        )
    except (TypeError, ValueError):
        return None
    if kind in {"month_range", "quarter", "half"}:
        if time.start_month is None or not (1 <= time.start_month <= 12):
            return None
        if time.end_month is None or not (time.start_month <= time.end_month <= 12):
            return None
    binary_op = str(data.get("binary_op") or "add")
    if binary_op not in {"add", "sub", "mul", "div"}:
        return None
    filter_mode = str(data.get("filter_mode") or "")
    if filter_mode not in {"", "include", "exclude"}:
        return None
    top = data.get("top", 3)
    if not isinstance(top, int) or not (1 <= top <= 20):
        top = 3
    direction = str(data.get("direction") or "desc")
    if direction not in {"asc", "desc"}:
        direction = "desc"
    return CalcPlan(
        operation=operation,
        years=tuple(dict.fromkeys(clean_years)),
        time=time,
        subject_hint=str(data.get("subject_hint") or ""),
        top=top,
        direction=direction,
        binary_op=binary_op,
        filter_mode=filter_mode,
    )


async def generate_llm_calc_plan(question: str, provider: Any) -> CalcPlan | None:
    """调用模型生成计划；失败返回 None，由规则/门禁兜底。"""
    try:
        result = await provider.generate(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": str(question or "")},
            ]
        )
        plan = parse_plan_payload(getattr(result, "text", ""))
        if plan is None:
            log.warning("llm_plan_invalid", question_len=len(str(question or "")))
        return plan
    except Exception as exc:  # noqa: BLE001 —— 计划生成失败不能打断问答
        log.warning("llm_plan_failed", error=str(exc)[:240])
        return None
