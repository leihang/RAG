"""表格比较/排名：同行集合 + 受限 max/min/rank（与 numeric「只抄不算」并存）。

比较题需要**全量候选同行**；top-k 零散行 + 散文模型无法可靠全局排序。
本模块只在叶子可解析为 ``(实体, 指标值)`` 且意图为 max/min/rank 时做可审计排序。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

CompareOp = Literal["max", "min", "rank"]

#: 比较/排名意图词（优先于 numeric「用量」等取数词）
_COMPARE_HINTS = (
    "谁更高",
    "谁更低",
    "谁最大",
    "谁最小",
    "最高",
    "最低",
    "最大",
    "最小",
    "排名",
    "排行",
    "top",
    "Top",
    "TOP",
    "更高",
    "更低",
)

_MONTH_IN_QUESTION = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>1[0-2]|[1-9])\s*月"
)
_YEAR_IN_QUESTION = re.compile(r"(20\d{2})\s*年")

_KV_LINE = re.compile(
    r"(?:^|\n)(?P<body>(?:[^;\n]+=[^;\n]+)(?:;\s*[^;\n]+=[^;\n]+)+)\s*$"
)


def is_compare_rank_intent(question: str) -> bool:
    text = (question or "").strip()
    if not text:
        return False
    return any(hint in text for hint in _COMPARE_HINTS)


def detect_compare_op(question: str) -> CompareOp | None:
    """从问题里判定 max / min / rank；非比较题返回 None。"""
    text = (question or "").strip()
    if not is_compare_rank_intent(text):
        return None
    if any(h in text for h in ("排名", "排行", "top", "Top", "TOP")):
        return "rank"
    if any(h in text for h in ("谁更低", "谁最小", "最低", "最小", "更低")):
        return "min"
    return "max"


def extract_year_month_from_question(question: str) -> tuple[str | None, int | None]:
    text = question or ""
    month_match = _MONTH_IN_QUESTION.search(text)
    year: str | None = None
    month: int | None = None
    if month_match:
        if month_match.group("year"):
            year = month_match.group("year")
        month = int(month_match.group("month"))
    if year is None:
        year_match = _YEAR_IN_QUESTION.search(text)
        if year_match:
            year = year_match.group(1)
    return year, month


@dataclass(frozen=True)
class TidyMetricRow:
    """可解析的 tidy 长叶子观测。"""

    entity: str
    value: float
    value_surface: str
    year: str
    month: int
    metric: str
    category: str
    chunk_id: int
    content: str


def _row_cells_map(line: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for part in line.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        cells[key.strip()] = value.strip()
    return cells


def _tidy_data_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("表名:", "核心实体:", "相关表头:", "表格形态:")):
            continue
        if "月份=" in line and "=" in line:
            return line
    return ""


def _pick_metric_surface(cells: dict[str, str]) -> tuple[str, str]:
    for key in ("用电量", "用水量", "用气量", "数值", "用量", "金额"):
        if key in cells and cells[key].strip():
            return key, cells[key].strip()
    skip = {"铺号", "用户名", "名称", "类别", "年度", "年份", "月份", "列1", "列2"}
    for key, value in cells.items():
        if key in skip or not value.strip():
            continue
        return key, value.strip()
    return "", ""


def _pick_entity(cells: dict[str, str]) -> str:
    entity = (
        cells.get("用户名")
        or cells.get("名称")
        or cells.get("商户")
        or cells.get("项目")
        or ""
    ).strip()
    if entity:
        return entity
    return (cells.get("铺号") or next(iter(cells.values()), "")).strip()


def parse_tidy_metric_from_content(content: str, *, chunk_id: int = 0) -> TidyMetricRow | None:
    """从 tidy 叶子正文解析 ``(实体, 年, 月, 值)``；排名摘要块跳过。"""
    text = content or ""
    if "排名类型=" in text or "排名摘要" in text or "月份=" not in text:
        return None
    data_line = _tidy_data_line(text)
    if not data_line:
        return None
    cells = _row_cells_map(data_line)
    month_raw = cells.get("月份", "").strip()
    if not month_raw.isdigit():
        return None
    month = int(month_raw)
    if not 1 <= month <= 12:
        return None
    year = re.sub(r"年$", "", (cells.get("年度") or cells.get("年份") or "").strip())
    metric, value_surface = _pick_metric_surface(cells)
    entity = _pick_entity(cells)
    if not year or not value_surface or not entity:
        return None
    try:
        value = float(value_surface.replace(",", ""))
    except ValueError:
        return None
    return TidyMetricRow(
        entity=entity,
        value=value,
        value_surface=value_surface,
        year=year,
        month=month,
        metric=metric,
        category=(cells.get("类别") or "").strip(),
        chunk_id=chunk_id,
        content=text,
    )


def parse_tidy_rows_from_chunks(
    chunks: Sequence[object],
) -> list[TidyMetricRow]:
    rows: list[TidyMetricRow] = []
    for chunk in chunks:
        content = str(getattr(chunk, "content", "") or "")
        chunk_id = int(getattr(chunk, "chunk_id", 0) or 0)
        parsed = parse_tidy_metric_from_content(content, chunk_id=chunk_id)
        if parsed is not None:
            rows.append(parsed)
    return rows


def peer_keys_from_chunks(
    chunks: Sequence[object],
    question: str,
) -> list[tuple[int, str | None, int]]:
    """从命中块 + 问题抽出需要补齐的 ``(doc_id, year|None, month)``。

    问题未写年份时 ``year=None``：同行补齐按月拉全量，再由受限排序取**最新有数年份**
    （「2026年9月能耗分析」里问「9月谁更高」应对齐 2026，而不是历史峰值年）。
    """
    q_year, q_month = extract_year_month_from_question(question)
    keys: list[tuple[int, str | None, int]] = []
    seen: set[tuple[int, str | None, int]] = set()
    for chunk in chunks:
        if str(getattr(chunk, "chunk_type", "") or "") != "table":
            continue
        doc_id = int(getattr(chunk, "doc_id", 0) or 0)
        parsed = parse_tidy_metric_from_content(
            str(getattr(chunk, "content", "") or ""),
            chunk_id=int(getattr(chunk, "chunk_id", 0) or 0),
        )
        if parsed is None:
            # 排名摘要块：仍可贡献 doc + 月
            content = str(getattr(chunk, "content", "") or "")
            if q_month is None or "月份=" not in content:
                continue
            year = q_year
            if year is None:
                match = re.search(r"年度=(\d{4})", content)
                year = match.group(1) if match else None
            key = (doc_id, year if q_year else None, q_month)
            if key not in seen:
                seen.add(key)
                keys.append(key)
            continue
        month = q_month if q_month is not None else parsed.month
        year: str | None = q_year  # 问题没写年 → None，按月扩
        if year is None and q_month is None:
            year = parsed.year
        key = (doc_id, year, month)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


@dataclass
class RankAnswer:
    """受限排序结果（可审计）。"""

    op: CompareOp
    answer: str
    winner_entity: str
    winner_value: float
    winner_surface: str
    year: str
    month: int
    metric: str
    cite_chunk_ids: list[int]
    ranked: list[TidyMetricRow]


def _filter_rows_for_question(
    rows: Sequence[TidyMetricRow], question: str
) -> list[TidyMetricRow]:
    q_year, q_month = extract_year_month_from_question(question)
    filtered = list(rows)
    if q_month is not None:
        filtered = [r for r in filtered if r.month == q_month]
    if q_year:
        return [r for r in filtered if r.year == q_year]
    if not filtered:
        return []
    # 未指定年份：取该月**最新有数据的年份**（能耗月报场景默认看本年）
    latest = max(r.year for r in filtered)
    return [r for r in filtered if r.year == latest]


def _dedupe_entities(
    rows: Sequence[TidyMetricRow], *, prefer_min: bool
) -> list[TidyMetricRow]:
    by_entity: dict[str, TidyMetricRow] = {}
    for row in rows:
        prev = by_entity.get(row.entity)
        if prev is None:
            by_entity[row.entity] = row
        elif prefer_min and row.value < prev.value:
            by_entity[row.entity] = row
        elif not prefer_min and row.value > prev.value:
            by_entity[row.entity] = row
    return list(by_entity.values())


def _format_rank_answer(
    op: CompareOp, ranked: Sequence[TidyMetricRow], *, top_n: int
) -> tuple[str, list[int]]:
    winner = ranked[0]
    metric = winner.metric or "数值"
    year, month = winner.year, winner.month
    cite_ids: list[int] = []
    if op == "rank":
        lines = [f"{year}年{month}月{metric}排名（前{min(top_n, len(ranked))}）："]
        for idx, row in enumerate(ranked[:top_n], start=1):
            lines.append(f"{idx}. {row.entity}：{row.value_surface}")
            if row.chunk_id and row.chunk_id not in cite_ids:
                cite_ids.append(row.chunk_id)
        return "\n".join(lines), cite_ids
    word = "最低" if op == "min" else "最高"
    answer = (
        f"{year}年{month}月{metric}{word}的是{winner.entity}，"
        f"为 {winner.value_surface}。"
    )
    if winner.chunk_id:
        cite_ids.append(winner.chunk_id)
    for row in ranked[1:3]:
        if row.chunk_id and row.chunk_id not in cite_ids:
            cite_ids.append(row.chunk_id)
    return answer, cite_ids


def try_constrained_rank(
    question: str,
    chunks: Sequence[object],
    *,
    top_n: int = 10,
) -> RankAnswer | None:
    """在可解析 tidy 行上做 max/min/rank；条件不足返回 None（交回 prose/numeric）。"""
    op = detect_compare_op(question)
    if op is None:
        return None
    filtered = _filter_rows_for_question(parse_tidy_rows_from_chunks(chunks), question)
    if not filtered:
        return None
    unique = _dedupe_entities(filtered, prefer_min=(op == "min"))
    if not unique:
        return None
    ranked = sorted(unique, key=lambda r: r.value, reverse=(op != "min"))
    winner = ranked[0]
    answer, cite_ids = _format_rank_answer(op, ranked, top_n=top_n)
    return RankAnswer(
        op=op,
        answer=answer,
        winner_entity=winner.entity,
        winner_value=winner.value,
        winner_surface=winner.value_surface,
        year=winner.year,
        month=winner.month,
        metric=winner.metric or "数值",
        cite_chunk_ids=cite_ids,
        ranked=ranked,
    )
