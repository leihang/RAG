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
_RATIO_WORDS = ("占比", "比例", "占")
_RANK_WORDS = ("排名", "最高", "最低", "最多", "最少", "top", "Top", "TOP")
_ADD_WORDS = ("合计", "总和", "之和", "相加", "加总")
_SUB_WORDS = ("减", "差额", "相差", "多多少", "少多少")
_CONNECTIVES = ("和", "与", "、", "及", "+", "加")
_FILTER_WORDS = ("除", "排除", "不含", "不包括", "只算", "仅", "其中")
_TOP_RE = re.compile(r"(?:前|Top|top|TOP)\s*([一二三四五六七八九十\d]+)")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class CalcPlan:
    operation: str
    years: tuple[int, ...]
    time: TimeSpec
    subject_hint: str = ""
    top: int = 3
    direction: str = "desc"
    binary_op: str = "add"
    filter_mode: str = ""      # exclude | include


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
    has_two_subject_connective = any(token in text for token in _CONNECTIVES)
    if (
        len(years) == 1
        and has_two_subject_connective
        and any(word in text for word in _SUB_WORDS)
    ):
        return CalcPlan(
            "binary", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text), binary_op="sub",
        )
    if (
        len(years) == 1
        and has_two_subject_connective
        and any(word in text for word in _ADD_WORDS)
    ):
        return CalcPlan(
            "binary", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text), binary_op="add",
        )
    if len(years) == 1 and any(word in text for word in _RANK_WORDS):
        top = 3
        top_match = _TOP_RE.search(text)
        if top_match:
            raw = top_match.group(1)
            top = int(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 3)
        return CalcPlan(
            "rank", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text),
            top=top,
            direction="asc" if ("最低" in text or "最少" in text) else "desc",
        )
    if len(years) == 1 and any(word in text for word in _RATIO_WORDS):
        return CalcPlan(
            "ratio", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text),
        )
    if len(years) == 1 and any(word in text for word in _AVG_WORDS):
        return CalcPlan(
            "avg", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text),
        )
    if len(years) == 1 and any(word in text for word in _FILTER_WORDS):
        mode = "include" if any(word in text for word in ("只算", "仅", "其中")) else "exclude"
        return CalcPlan(
            "filter", years,
            time or TimeSpec("year", 1, 12, require_complete=False),
            _hint(text), filter_mode=mode,
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
