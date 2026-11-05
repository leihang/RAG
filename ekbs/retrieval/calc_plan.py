"""数值问题 → 计算计划（路由层）。

计划只决定“算什么类型”，不产出数字：

    operation=compare  # 两期/多年比较
    operation=value    # 单期直查

真正的表选择/算子执行由 table_query 完成；LLM 只可后续参与计划生成，
但目前先用确定性规则，避免把“数值意图”直接交给自由生成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ekbs.retrieval.table_time import TimeSpec, parse_time_spec

_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_COMPARE_WORDS = ("增长", "减少", "增加", "下降", "对比", "较", "同比", "环比")
_VALUE_WORDS = ("多少", "几", "合计", "总计", "总共", "是多少", "多大", "占多少")
_AVG_WORDS = ("平均", "均值")


@dataclass(frozen=True)
class CalcPlan:
    operation: str
    years: tuple[int, ...]
    time: TimeSpec
    subject_hint: str = ""


def _hint(question: str) -> str:
    text = re.sub(r"20\d{2}\s*年(\d{1,2}\s*[-—–~～至到]\s*\d{1,2}\s*月|Q[1-4]|第?[一二三四]季度|上半年|下半年|YTD|年初至今|全年)?", "", str(question or ""))
    text = re.split(r"[，,。；;：:]", text, maxsplit=1)[0]
    text = re.sub(r"(是多少|多少|几|合计|总计|总共|多大|占多少|的)+", "", text)
    return text.strip()


def generate_calc_plan(question: str) -> CalcPlan | None:
    text = str(question or "")
    time = parse_time_spec(text)
    years = tuple(dict.fromkeys(int(y) for y in _YEAR_RE.findall(text)))
    if not years:
        return None
    if len(years) >= 2 and any(word in text for word in _COMPARE_WORDS):
        if time is None:
            return None
        return CalcPlan("compare", years, time, _hint(text))
    if len(years) == 1 and any(word in text for word in _AVG_WORDS):
        return CalcPlan(
            "avg", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text),
        )
    if len(years) == 1 and any(word in text for word in _VALUE_WORDS):
        # 单值问法没写“全年/1-9月”时默认全年；由算子优先读年度 total。
        return CalcPlan(
            "value",
            years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text),
        )
    return None
