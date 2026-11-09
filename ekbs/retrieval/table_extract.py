"""精排表格块确定性提取（计算模块兜底）。

观测表没有行项时，按规划指令在**召回文档内**的 table 块上解析
``项目=…; 类别=…; 9用量=…`` / 管道表行，禁止模型心算。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from ekbs.retrieval.structured_query import StructuredQueryHit

# 键名可含半角/全角括号与单位，如 ``9用量（kw.h)``
_KV_KEY = r"[\u4e00-\u9fffA-Za-z0-9（）()·\-_/.]+"
_KV_LINE_RE = re.compile(
    rf"(?:^|[;\n])\s*({_KV_KEY})\s*=\s*([^;\n]+)"
)
_KV_PAIR_RE = re.compile(rf"({_KV_KEY})\s*=\s*([^;]+)")
_NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")
_PIPE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_ROW_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")


def extract_from_table_chunks(  # noqa: C901 — 块过滤/行解析/投影同路径
    chunks: Sequence[Any],
    *,
    entity: str | None = None,
    period: str | None = None,
    metric: str | None = None,
    attrs_contains: str | None = None,
    columns: Sequence[str] | None = None,
    prefer_doc_ids: Sequence[int] | None = None,
    limit: int = 80,
) -> StructuredQueryHit:
    """从精排块抽出匹配行；scope 外 doc 直接跳过。"""
    prefer = {int(d) for d in (prefer_doc_ids or ())}
    if prefer_doc_ids is not None and not prefer:
        return StructuredQueryHit(
            value=None, op="extract", reason="no_scope_docs", surface="N/A"
        )

    want_cols = [c.strip() for c in (columns or ()) if str(c).strip()]
    if not want_cols:
        # 默认明细：项目 + 用量类列
        want_cols = ["项目", "用量"]

    filters = [
        t
        for t in (entity, attrs_contains, period, metric)
        if t and str(t).strip()
    ]
    # period 口语化：2026-09 → 也匹配 2026年9月 / 9用量
    period_alts = _period_aliases(period)

    detail: list[dict[str, Any]] = []
    chunk_ids: list[int] = []
    doc_ids: list[int] = []

    for chunk in chunks:
        doc_id = getattr(chunk, "doc_id", None)
        if prefer and doc_id is not None and int(doc_id) not in prefer:
            continue
        content = str(getattr(chunk, "content", "") or "")
        if not content.strip():
            continue
        # 块级粗过滤：attrs/实体关键字应出现在块里（期间可用别名）
        if attrs_contains and attrs_contains not in content and (
            entity is None or entity not in content
        ):
            # 仍可能「类别=公共」只在行里；不因缺块级词直接跳过
            pass
        rows = _parse_chunk_rows(content)
        for row in rows:
            if not _row_matches(
                row,
                entity=entity,
                attrs_contains=attrs_contains,
                period_alts=period_alts,
                metric=metric,
                filters=filters,
            ):
                continue
            projected = _project_row(row, want_cols, period_alts=period_alts)
            if not projected:
                continue
            # 同项目去重：后写覆盖（同 doc 多段重复）
            detail.append(projected)
            cid = getattr(chunk, "chunk_id", None)
            if cid is not None:
                chunk_ids.append(int(cid))
            if doc_id is not None:
                doc_ids.append(int(doc_id))
            if len(detail) >= limit:
                break
        if len(detail) >= limit:
            break

    if not detail:
        return StructuredQueryHit(
            value=None,
            op="extract",
            reason="not_found",
            surface="N/A",
            period=period or "",
            metric=metric or "",
            entity_key=entity or "",
        )

    detail = _dedupe_by_project(detail)

    # 单行且能解析主数值 → value；多行 value=行数，surface 拼可读摘要
    if len(detail) == 1:
        surface, value = _row_surface_and_value(detail[0])
    else:
        value = len(detail)
        parts = []
        for item in detail[:40]:
            label = str(item.get("项目") or item.get("entity") or next(iter(item.values()), ""))
            amount = _first_number_field(item)
            if amount is not None:
                parts.append(f"{label}={_fmt(amount)}")
            else:
                parts.append(label)
        surface = "；".join(parts)

    return StructuredQueryHit(
        value=value,
        surface=surface,
        op="extract",
        rows_touched=len(detail),
        chunk_ids=tuple(dict.fromkeys(chunk_ids)),
        doc_ids=tuple(sorted(set(doc_ids))),
        period=period or "",
        metric=metric or "",
        entity_key=entity or "",
        detail=detail,
        extras={"source": "rerank_table_chunks", "columns": list(want_cols)},
    )


def module_diff(
    left: float, right: float, *, as_ratio: bool = False
) -> tuple[float, str]:
    """模块侧差值；as_ratio 时用 right 作基数算百分比。"""
    if as_ratio:
        if right == 0:
            return 0.0, "N/A"
        ratio = (left - right) / right * 100.0
        return ratio, f"{ratio:.2f}%"
    delta = left - right
    return delta, _fmt(delta)


def module_sum(values: Sequence[float]) -> tuple[float, str]:
    total = float(sum(values))
    return total, _fmt(total)


def _period_aliases(period: str | None) -> list[str]:
    """列名匹配用别名。不用裸「9月」（会误伤「2025年9月用量」）。"""
    if not period:
        return []
    text = period.strip()
    out = [text]
    m = re.fullmatch(r"(20\d{2})-(\d{2})", text)
    if m:
        year, month = m.group(1), int(m.group(2))
        out.append(f"{year}年{month}月")
        out.append(f"{month}用量")
        out.append(f"{month}用量（")
        out.append(f"{year}-{int(month):02d}")
    return out


def _parse_chunk_rows(content: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    # 1) KV 行（序列化表）
    for line in content.splitlines():
        if "=" not in line:
            continue
        if line.startswith("相关表头") or line.startswith("表名:") or line.startswith("核心实体"):
            continue
        pairs = _KV_LINE_RE.findall(";" + line if not line.startswith(";") else line)
        if len(pairs) < 2:
            pairs = _KV_PAIR_RE.findall(line)
        if len(pairs) < 2:
            continue
        row = {k.strip(): v.strip() for k, v in pairs if k.strip()}
        if row:
            rows.append(row)

    # 2) Markdown 管道表
    lines = content.splitlines()
    headers: list[str] = []
    for index, line in enumerate(lines):
        if not _PIPE_ROW_RE.match(line) or _SEP_ROW_RE.match(line):
            continue
        cells = _split_pipe(line)
        if index + 1 < len(lines) and _SEP_ROW_RE.match(lines[index + 1]):
            headers = cells
            continue
        if headers and cells:
            row = {
                headers[i]: cells[i]
                for i in range(min(len(headers), len(cells)))
                if headers[i]
            }
            if row:
                rows.append(row)
    return rows


def _split_pipe(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [c.strip() for c in text.split("|")]


def _row_matches(
    row: dict[str, str],
    *,
    entity: str | None,
    attrs_contains: str | None,
    period_alts: Sequence[str],
    metric: str | None,
    filters: Sequence[str],
) -> bool:
    blob = " ".join(f"{k}={v}" for k, v in row.items())
    # 类别=公共 或任意字段含关键字
    if attrs_contains and attrs_contains not in blob and not any(
        attrs_contains in v for v in row.values()
    ):
        return False
    if entity:
        from ekbs.retrieval.structured_query import entity_match_variants

        # 「变压器损耗值」要对上表内「变压器损耗」
        variants = entity_match_variants(entity)
        matched = False
        for variant in variants:
            if variant in blob or any(
                variant == v or variant in v for v in row.values()
            ):
                matched = True
                break
            if variant in str(row.get("类别", "")) or variant in str(row.get("项目", "")):
                matched = True
                break
        if not matched:
            return False
    if period_alts:
        # 行内期间列或用量列名带月
        period_hit = any(a in blob for a in period_alts)
        if not period_hit and "period" in {k.lower() for k in row}:
            period_hit = any(a in str(row.get("period", "")) for a in period_alts)
        # 宽表当月列常叫「9用量」——有用量数字即可，期间由块标题保证
        # 变压器损耗明细无月列：账期在文档标题，行上只有安装地点/损耗
        if not period_hit and not _has_usage_like(row) and not _is_equipment_loss_row(
            row
        ):
            return False
    # 用电量 ↔ 用量
    if (  # noqa: SIM103 — 保留分支可读性，避免双重否定一行式
        metric
        and metric not in blob
        and not _metric_compatible(row, metric)
        and not (
            ("用电" in metric and _has_usage_like(row))
            or ("金额" in metric and any("金额" in k for k in row))
        )
    ):
        return False
    return True


def _has_usage_like(row: dict[str, str]) -> bool:
    """真用量列（排除同比/环比）。"""
    return any(
        ("用量" in k or k in {"value", "用电量"})
        and "同比" not in k
        and "环比" not in k
        for k in row
    )


def _is_equipment_loss_row(row: dict[str, str]) -> bool:
    has_place = "安装地点" in row or "自编号" in row
    has_loss = any("损耗" in k for k in row)
    return has_place and has_loss


def _metric_compatible(row: dict[str, str], metric: str) -> bool:
    if any(metric in k or metric in v for k, v in row.items()):
        return True
    # 「变压器损耗」问句 ↔ 行内「损耗」列
    return bool("损耗" in metric and any("损耗" in k for k in row))


def _dedupe_by_project(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = str(row.get("项目") or row.get("类别") or id(row))
        if key not in seen:
            order.append(key)
        seen[key] = row
    return [seen[k] for k in order]


def _project_row(  # noqa: C901 — 列投影/模糊匹配/空行过滤同路径
    row: dict[str, str],
    want_cols: Sequence[str],
    *,
    period_alts: Sequence[str] = (),
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # 始终尝试保留项目名
    for key in ("项目", "entity_key", "entity_label", "名称"):
        if key in row and row[key]:
            out["项目"] = row[key]
            break
    if "类别" in row:
        out["类别"] = row["类别"]

    for col in want_cols:
        if col in ("项目", "类别"):
            continue
        if col in row:
            out[col] = _coerce(row[col])
            continue
        if col == "用量":
            picked = _pick_usage_value(row, period_alts=period_alts)
            if picked is not None:
                out["用量"] = picked
            continue
        if col == "金额":
            for key, value in row.items():
                if "金额" in key and "同比" not in key and "环比" not in key:
                    out["金额"] = _coerce(value)
                    break
            continue
        # 模糊列名：必须足够像（避免「冷却塔节约比例」误命中宽表列「冷却塔」用电量）
        best: tuple[float, str, str] | None = None
        for key, value in row.items():
            score = _column_match_score(col, key)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, key, value)
        if best is not None:
            out[col] = _coerce(best[2])
    # 点名了具体 KPI 列（改造预算等）时：必须命中其中至少一列，
    # 避免同文档其它「项目=…」行空投影混进 detail。
    specific = [c for c in want_cols if c not in {"项目", "类别", "用量", "金额"}]
    if specific and not any(c in out for c in specific):
        return {}
    # 至少要有一个数值、非空字段或项目（方案 KV「改造预算=22,000 元」无「项目」列）
    if "项目" not in out and not any(
        isinstance(v, (int, float)) or (isinstance(v, str) and v and v != "—")
        for v in out.values()
    ):
        return {}
    return out


def _column_match_score(want: str, key: str) -> float:
    """列名相似度：精确 > 互相包含且长度比≥0.6；过短包含（冷却塔⊂冷却塔节约比例）得 0。"""
    want_s = str(want or "").strip()
    key_s = str(key or "").strip()
    if not want_s or not key_s:
        return 0.0
    if want_s == key_s:
        return 100.0
    # 「损耗」不得模糊命中「日损耗/每月损耗/空载损耗」
    if want_s == "损耗" and key_s != "损耗" and not key_s.startswith(
        ("损耗（", "损耗(")
    ):
        return 0.0
    if want_s in key_s:
        ratio = len(want_s) / max(len(key_s), 1)
        return 80.0 * ratio if ratio >= 0.6 else 0.0
    if key_s in want_s:
        ratio = len(key_s) / max(len(want_s), 1)
        return 70.0 * ratio if ratio >= 0.6 else 0.0
    return 0.0


def _pick_usage_value(
    row: dict[str, str], *, period_alts: Sequence[str] = ()
) -> Any | None:
    """优先当期「N用量」列，避开同比/环比与其它年月份用量。"""
    year = None
    for alt in period_alts:
        m = re.match(r"(20\d{2})", alt)
        if m:
            year = m.group(1)
            break
    candidates: list[tuple[int, int, str, str]] = []
    for key, value in row.items():
        if "用量" not in key:
            continue
        if "同比" in key or "环比" in key:
            continue
        raw = value.replace(",", "").strip()
        if not _NUMBER_RE.match(raw):
            continue
        period_score = 0
        if period_alts and any(a and a in key for a in period_alts):
            period_score = -100
        # 「9用量」形列名（无年份前缀）视为当月主列
        if re.match(r"^\d{1,2}用量", key):
            period_score = min(period_score, -120)
        # 其它年份的「2025年9月用量」降权
        ym = re.search(r"(20\d{2})年", key)
        if ym and year and ym.group(1) != year:
            period_score = 80
        elif ym and year and ym.group(1) == year:
            period_score = min(period_score, -90)
        candidates.append((period_score, len(key), key, value))
    if not candidates:
        return None
    candidates.sort()
    return _coerce(candidates[0][3])


#: 方案/账单单元格常带单位：「22,000 元」「约 40,848 元」「7 个月」「30 天」
_COERCE_NUMBER_RE = re.compile(
    r"^(?:约|大约|近|逾)?\s*"
    r"([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)"
    r"(?:\s*(?:元|万元|度|吨|立方米|m³|kwh|kw\.h|个月|月|天|日|%|℃))?$",
    re.IGNORECASE,
)


def _coerce(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text or text in {"—", "-", "－", "N/A"}:
        return "—"
    compact = text.replace(",", "")
    if _NUMBER_RE.match(compact):
        try:
            number = float(compact)
            if number == int(number):
                return int(number) if abs(number) < 1e15 else number
            return number
        except ValueError:
            return text
    if text.endswith("%") and _NUMBER_RE.match(text[:-1].replace(",", "")):
        return text
    # 带单位/约数：取数值进计算模块，单位留在原文 surface 由上层拼
    matched = _COERCE_NUMBER_RE.match(text.replace("，", ","))
    if matched:
        try:
            number = float(matched.group(1).replace(",", ""))
            if number == int(number):
                return int(number) if abs(number) < 1e15 else number
            return number
        except ValueError:
            return text
    return text


def _first_number_field(row: dict[str, Any]) -> float | None:
    for key in ("用量", "金额", "value", "用电量"):
        if key in row and isinstance(row[key], (int, float)):
            return float(row[key])
    for value in row.values():
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _row_surface_and_value(row: dict[str, Any]) -> tuple[str, float | int | str]:
    label = str(row.get("项目") or "")
    # 方案 KPI 行（改造预算/节约/回收期）无「项目」列：拼全量键值，避免只露第一个数
    data_keys = [k for k in row if k not in {"项目", "类别"}]
    if len(data_keys) > 1 and not label:
        parts = [
            f"{k}={_fmt(v) if isinstance(v, (int, float)) else v}"
            for k, v in row.items()
            if k not in {"项目", "类别"}
        ]
        surface = "；".join(parts)
        number = _first_number_field(row)
        return surface, number if number is not None else surface
    number = _first_number_field(row)
    if number is not None:
        surface = f"{label}={_fmt(number)}" if label else _fmt(number)
        return surface, number if number != int(number) else int(number)
    surface = "；".join(f"{k}={v}" for k, v in row.items())
    return surface, surface


def _fmt(number: float | int) -> str:
    if isinstance(number, float) and number != int(number):
        return f"{number:.2f}".rstrip("0").rstrip(".")
    return str(int(number) if isinstance(number, float) and number == int(number) else number)
