"""LLM 规划查询指令 → 计算模块提取/运算 → 再生成答案。

铁律：
* LLM **只出指令、不算数**；
* 范围由系统锁定为精排命中文档；
* 文档有值则 extract；题要算且基数可取则模块 sum/compare/diff；
* 出口核验答案数字来自执行结果。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.logging import get_logger
from ekbs.retrieval.structured_query import StructuredQueryHit

log = get_logger(component="retrieval.structured_plan")

#: 对外四类 + 兼容旧 op（规则回退 / 旧测试）
_ALLOWED_OPS = frozenset(
    {
        "extract",
        "sum",
        "compare",
        "diff",
        # 兼容别名
        "lookup",
        "count",
        "avg",
        "filter",
        "printed_total",
        "yoy_na",
        "na",
    }
)

#: 规划器主推四类；lookup 等可由解析层映射
PLANNER_SYSTEM = (
    "你是数字取数/计算规划器。根据用户问题与检索到的表格片段，输出**一个** JSON 对象，"
    "不要其它文字或 Markdown 代码块。\n"
    "字段：\n"
    '{"op":"extract|sum|compare|diff|na",'
    '"entity":"实体/类别/行名或空",'
    '"period":"YYYY-MM 或 YYYY-MM..YYYY-MM 或空",'
    '"metric":"指标名或空",'
    '"attrs_contains":"类别或表名关键字或空",'
    '"columns":["项目","用量"],'
    '"reason":"一句话说明"}\n'
    "规则：\n"
    "1. 只能用上述 op；你自己不能算数；不确定就 op=na。\n"
    "2. 明细/列出/有哪些 → extract，并填 columns（默认项目+当月用量；"
    "问题点名金额/同比才加入对应列）。\n"
    "2b. 问句同时要多种指标（如「用水量和用电量」）→ op=extract，"
    "metric 可填空或第一种；系统会按问句拆多指标分别提取，禁止只答一个或把量费加总。\n"
    "3. 合计/一共 → sum；对比谁大谁小 → compare；差异/少了多少 → diff。"
    "问「用电量/用水量」时 entity 用「…电量/…水量」，不要用「…电费/…水费」。\n"
    "4. 「差异电费」→ op=extract 或 diff，entity=差异，metric 金额类，"
    "attrs_contains 含「用电」；「差异水费」attrs_contains 含「用水」。\n"
    "5. period 必须是资料账期形式（如 2026-09），不要写「9月」口语。\n"
    "6. 文档已印同比/环比且问题要它 → extract 该列；文档无而题要算 → "
    "用 diff/compare（由计算模块算，不是你算）。\n"
)

_OP_ALIASES = {
    "lookup": "extract",
    "filter": "extract",
    "printed_total": "sum",
}


@dataclass
class StructuredPlan:
    """LLM 产出的查询指令。"""

    op: str
    entity: str | None = None
    period: str | None = None
    metric: str | None = None
    attrs_contains: str | None = None
    columns: list[str] = field(default_factory=list)
    reason: str = ""
    raw: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}
        # 对外统一主 op
        self.op = _OP_ALIASES.get(self.op, self.op)


def parse_structured_plan(text: str) -> StructuredPlan | None:
    """解析规划器输出；非法 / op=na → None。"""
    payload = _extract_json_object(text)
    if not payload:
        return None
    op = str(payload.get("op") or "").strip().lower()
    if op not in _ALLOWED_OPS or op == "na":
        return None
    entity = _clean_optional(payload.get("entity"))
    period = _clean_optional(payload.get("period"))
    metric = _clean_optional(payload.get("metric"))
    attrs_contains = _clean_optional(payload.get("attrs_contains"))
    columns = _clean_columns(payload.get("columns"))
    reason = str(payload.get("reason") or "").strip()[:200]
    return StructuredPlan(
        op=op,
        entity=entity,
        period=period,
        metric=metric,
        attrs_contains=attrs_contains,
        columns=columns,
        reason=reason,
        raw=payload,
    )


def build_planner_user_message(
    question: str, snippets: Sequence[str], *, max_snippets: int = 6
) -> str:
    """规划器用 user 消息：问题 + 精排叶子摘要。"""
    blocks: list[str] = [f"问题：{question.strip()}", "", "检索片段："]
    for index, snippet in enumerate(snippets[:max_snippets], start=1):
        text = re.sub(r"\s+", " ", (snippet or "").strip())[:400]
        if text:
            blocks.append(f"[{index}] {text}")
    if len(blocks) == 3:
        blocks.append("（无片段）")
    return "\n".join(blocks)


#: 旁路执行失败后：只修正指令再跑计算模块，不降级散文读表
PLANNER_REPAIR_SYSTEM = (
    "你是数字取数规划修正器。上一次查询指令已交给计算模块执行但失败了。\n"
    "请根据失败原因与表格片段，输出**一个**修正后的 JSON 对象（字段同规划器），"
    "不要其它文字或 Markdown 代码块。\n"
    "字段：\n"
    '{"op":"extract|sum|compare|diff|na",'
    '"entity":"实体/类别/行名或空",'
    '"period":"YYYY-MM 或空",'
    '"metric":"指标名或空",'
    '"attrs_contains":"类别或表名关键字或空",'
    '"columns":["列名"],'
    '"reason":"一句话说明本次如何修正"}\n'
    "修法提示：\n"
    "1. 单元格/额度/某月金额 → 优先 op=extract，columns 用片段真实表头"
    "（如「2月」「2026年度计划金额」），不要用口语「预算额度」。\n"
    "2. 合计失败时检查 entity/metric 是否与片段行名一致；宽表不要误用 sum。\n"
    "3. period 用资料账期形式（2026-02），不要写「2月」。\n"
    "4. 仍无法对齐资料 → op=na。禁止心算、禁止编造列名。\n"
    "5. 若提供「此前已失败的指令」，不要重复相同 op/entity/period/columns 组合；"
    "换列名、换 extract/sum、或收紧 entity。\n"
)


#: 旁路规划修正上限（不含首轮规划）
STRUCTURED_PLAN_REPAIR_MAX = 5


def build_planner_repair_user_message(
    question: str,
    snippets: Sequence[str],
    *,
    failed_plan: StructuredPlan | None,
    fail_reason: str,
    attempt: int = 1,
    max_attempts: int = STRUCTURED_PLAN_REPAIR_MAX,
    history: Sequence[dict[str, Any]] = (),
    max_snippets: int = 6,
) -> str:
    """旁路失败后的修正 user 消息：原问题 + 失败指令 + 原因 + 片段。"""
    prior: dict[str, Any]
    if failed_plan is None:
        prior = {"op": None, "reason": "no_plan"}
    else:
        prior = {
            "op": failed_plan.op,
            "entity": failed_plan.entity,
            "period": failed_plan.period,
            "metric": failed_plan.metric,
            "attrs_contains": failed_plan.attrs_contains,
            "columns": list(failed_plan.columns or []),
            "reason": failed_plan.reason,
        }
    blocks: list[str] = [
        f"问题：{question.strip()}",
        "",
        f"修正轮次：{int(attempt)}/{int(max_attempts)}",
        f"失败原因：{(fail_reason or 'not_found').strip()}",
        f"失败指令：{json.dumps(prior, ensure_ascii=False)}",
    ]
    if history:
        blocks.append(
            "此前已失败的指令（勿重复相同组合）："
            + json.dumps(list(history)[-4:], ensure_ascii=False)
        )
    blocks.extend(["", "检索片段（对照真实表头/行名修正）："])
    for index, snippet in enumerate(snippets[:max_snippets], start=1):
        text = re.sub(r"\s+", " ", (snippet or "").strip())[:400]
        if text:
            blocks.append(f"[{index}] {text}")
    if blocks[-1].startswith("检索片段"):
        blocks.append("（无片段）")
    return "\n".join(blocks)


def build_narrator_messages(
    question: str, hit: StructuredQueryHit, *, leaf_snippets: Sequence[str] = ()
) -> list[dict[str, str]]:
    """第二次 LLM：只许用执行结果里的数字组织答案。"""
    result = {
        "op": hit.op,
        "value": hit.value,
        "surface": hit.surface,
        "period": hit.period,
        "metric": hit.metric,
        "entity_key": hit.entity_key,
        "reason": hit.reason,
        "is_na": hit.is_na,
        "detail": list(hit.detail or [])[:60],
        "provenance": hit.provenance(),
    }
    system = (
        "你是取数表述助手。下面「计算结果」来自计算模块（提取或运算），数字已核定。\n"
        "只根据计算结果回答用户问题；禁止心算、禁止改数、"
        "禁止补充结果里没有的列或数字。\n"
        "provenance.kind=extract：数字来自资料原文，可标引用。\n"
        "provenance.kind=aggregate：数字由模块根据多条输入算出，"
        "说明依据条数即可，不要谎称该合计印在某一段原文里。\n"
        "若 detail 非空，按其中字段罗列即可（不要自行加同比/环比）。\n"
        "若 is_na 为 true，明确说未找到可靠依据并简短说明 reason。\n"
        "不要输出 JSON，直接给中文答案；引用编号由系统后置追加。"
    )
    user = (
        f"问题：{question.strip()}\n\n"
        f"计算结果：{json.dumps(result, ensure_ascii=False)}\n"
    )
    if leaf_snippets:
        user += "\n参考片段（仅供措辞，数字以计算结果为准）：\n"
        for index, snippet in enumerate(leaf_snippets[:3], start=1):
            cleaned = re.sub(r"\s+", " ", (snippet or "").strip())[:300]
            user += f"[{index}] {cleaned}\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def verify_answer_uses_hit(answer: str, hit: StructuredQueryHit) -> bool:
    """出口核验：答案须带上执行结果中的 surface/value/detail 数字。"""
    if hit.is_na:
        return True
    text = answer or ""
    surface = str(hit.surface or "").strip()
    if surface and surface in text:
        return True
    # 多行：抽查 detail 中的数值
    numbers: list[str] = []
    for row in hit.detail or []:
        for value in row.values():
            if isinstance(value, (int, float)):
                numbers.append(_num_token(value))
            elif isinstance(value, str) and re.fullmatch(
                r"[+-]?\d+(?:\.\d+)?%?", value.replace(",", "")
            ):
                numbers.append(value.replace(",", ""))
    if numbers:
        # 至少命中一半数值（避免表述省略小数位时过严）
        hit_n = sum(1 for n in numbers if n and n in text.replace(",", ""))
        if hit_n >= max(1, len(numbers) // 2):
            return True
    value = hit.value
    if value is None:
        return False
    candidates = {
        str(value),
        f"{value}",
        f"{float(value):.2f}".rstrip("0").rstrip("."),
    }
    if isinstance(value, float) and value == int(value):
        candidates.add(str(int(value)))
    return any(c and c in text.replace(",", "") for c in candidates)


def _num_token(value: float | int) -> str:
    if isinstance(value, float) and value != int(value):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(int(value) if isinstance(value, float) and value == int(value) else value)


def _clean_optional(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text or text.lower() in {"null", "none", "空", "-"}:
        return None
    return text[:80]


def _clean_columns(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，、\s]+", raw)
        return [p.strip()[:40] for p in parts if p.strip()][:8]
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                out.append(text[:40])
            if len(out) >= 8:
                break
        return out
    return []


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
