"""结构化观测表双写 —— 只服务可计算问答；不替换向量路径。

契约：
1. 始终写 chunks（由 ingest/chunker 负责）；
2. 按表型可选写观测行；``unknown`` 不写；
3. 删除/重解析时与 ``ekbs_chunk`` 同事务清理（见 :func:`delete_observations_for_doc`）。

列口径对齐 ``sql/001_init.sql`` 的 ``ekbs_table_observation``。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from ekbs.ingest.table_serialize import (
    _MonthSeriesLayout,
    _is_wide_metric_table,
    _iter_metric_group_slices,
    _leading_identity_width,
    _normalize_year_value,
    _parse_metric_number,
    _prepare_information_blocks,
    coerce_rows_with_headers,
    detect_month_series_layout,
    normalize_header_period,
)
from ekbs.ingest.table_types import TableType
from ekbs.logging import get_logger

log = get_logger(component="ingest.table_observations")

_PERIOD_HEADER_RE = re.compile(
    r"(本期|本年累计|期末|期初|本月|本年|上期|去年同期|"
    r"同比|环比|增减|变动|"
    r"20\d{2}\s*年|(?:1[0-2]|[1-9])\s*月)"
)
#: 宽表头 ``用量(KW/h)/2026-02``：不能 ``split("/", 1)``——会在 ``KW/h`` 的斜杠处截断，
#: 把 period 写成 ``h)/2026-02``（doc 235 熙丰用电差异行实测）。
_HEADER_PERIOD_RE = re.compile(r"(20\d{2}-\d{2})(?:-\d{2})?\s*$")


def _period_from_metric_header(header: str) -> str:
    """从表头取出账期 ``YYYY-MM``（容忍 ``用量(KW/h)/2026-02`` 这类内含斜杠的单位）。"""
    text = str(header or "").strip()
    match = _HEADER_PERIOD_RE.search(text)
    if not match:
        return ""
    return normalize_header_period(match.group(1))


def _metric_name_from_header(header: str, hint: str) -> str:
    """表头去掉账期后缀后的指标名；``用量(KW/h)/2026-02`` → ``用量(KW/h)``。"""
    text = str(header or "").strip()
    text = _HEADER_PERIOD_RE.sub("", text).rstrip("/").strip()
    return text or hint


@dataclass(frozen=True)
class TableObservation:
    """单条可计算观测（入库行的内存形态）。"""

    table_id: str
    table_type: TableType
    entity_key: str
    entity_label: str
    period: str
    metric: str
    value: float | None
    value_surface: str
    attrs_json: str = "{}"
    line_role: str = "normal"
    source_row: int | None = None
    chunk_id: int | None = None


#: 兼容测试/旧调用名
ObservationRow = TableObservation


def delete_observations_for_doc(conn: sqlite3.Connection, doc_id: int) -> int:
    """重解析前清掉该文档全部观测行；返回删除行数。"""
    cur = conn.execute(
        "DELETE FROM ekbs_table_observation WHERE doc_id = ?", (doc_id,)
    )
    return int(cur.rowcount or 0)


def write_observations(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    shard_id: str,
    observations: Sequence[TableObservation],
) -> int:
    """批量插入观测行。调用方应已先 :func:`delete_observations_for_doc`。"""
    if not observations:
        return 0
    rows = [
        (
            doc_id,
            shard_id,
            obs.table_id,
            obs.table_type,
            obs.entity_key,
            obs.entity_label,
            obs.period,
            obs.metric,
            obs.value,
            obs.value_surface,
            obs.line_role,
            obs.source_row,
            obs.chunk_id,
            obs.attrs_json,
        )
        for obs in observations
    ]
    conn.executemany(
        "INSERT INTO ekbs_table_observation("
        "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
        "period, metric, value, value_surface, line_role, source_row, chunk_id, "
        "attrs_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


#: 别名：与计划文档 / 测试里的 insert_observations 对齐
insert_observations = write_observations


def build_observations_for_table(
    *,
    caption: str = "",
    body: str = "",
    table_id: str = "",
    rows: Sequence[Sequence[str]] | None = None,
    header_row_count: int = 0,
    source_path: str = "",
    table_type: TableType | None = None,
) -> list[TableObservation] | tuple[TableType, list[TableObservation]]:
    """按分类结果生成观测；``unknown`` 返回空列表。

    默认返回 ``list[TableObservation]``（入库路径）。若调用方需要类型标签，
    传 ``return_type=True`` 不在此签名 —— 用 :func:`build_observations_with_type`。
    """
    del source_path  # 文件名推年由 chunker 开关控制；观测构建跟 layout
    ttype, obs = build_observations_with_type(
        caption=caption,
        body=body,
        table_id=table_id,
        rows=rows,
        header_row_count=header_row_count,
        table_type=table_type,
    )
    del ttype
    return obs


def build_observations_with_type(
    *,
    caption: str = "",
    body: str = "",
    table_id: str = "",
    rows: Sequence[Sequence[str]] | None = None,
    header_row_count: int = 0,
    table_type: TableType | None = None,
) -> tuple[TableType, list[TableObservation]]:
    """返回 ``(table_type, observations)``。"""
    if rows is None:
        matrix, header_n = coerce_rows_with_headers(body)
        header_row_count = header_n or header_row_count
    else:
        matrix = [list(r) for r in rows]
    prepared = _prepare_information_blocks(
        caption=caption,
        rows=matrix,
        table_id=table_id or "",
        header_row_count=header_row_count,
    )
    if prepared is None:
        return "unknown", []
    cap, _pre, _fl, headers, data_rows = prepared
    from ekbs.ingest.table_types import classify_prepared_table

    ttype = table_type or classify_prepared_table(
        caption=cap, headers=headers, data_rows=data_rows
    )

    builders = {
        "A": _observations_month_series_prepared,
        "B": _observations_fee_detail,
        "C": _observations_financial,
        "D": _observations_ledger,
        "E": _observations_wide_bill,
    }
    builder = builders.get(ttype)
    if builder is None:
        return "unknown", []
    return ttype, builder(
        caption=cap, table_id=table_id or "", headers=headers, data_rows=data_rows
    )


def write_observations_for_tables(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    shard_id: str,
    tables: Sequence[Any],
    source_path: str = "",
) -> int:
    """对入库 ``TableInput`` 列表分类并双写观测；返回写入行数。"""
    del source_path
    delete_observations_for_doc(conn, doc_id)
    all_obs: list[TableObservation] = []
    by_type: dict[str, int] = {}
    for table in tables:
        body = str(getattr(table, "body", "") or "")
        caption = str(getattr(table, "caption", "") or "")
        table_id = str(getattr(table, "table_id", "") or "")
        if not body.strip():
            continue
        ttype, obs = build_observations_with_type(
            caption=caption, body=body, table_id=table_id
        )
        if obs:
            by_type[ttype] = by_type.get(ttype, 0) + len(obs)
            all_obs.extend(obs)
    written = write_observations(
        conn, doc_id=doc_id, shard_id=shard_id, observations=all_obs
    )
    if written:
        log.info(
            "table_observations_written",
            doc_id=doc_id,
            shard_id=shard_id,
            rows=written,
            by_type=by_type,
            tables=len(tables),
        )
    return written


#: 兼容旧名
dual_write_tables = write_observations_for_tables


# ------------------------------------------------------------------
# 各表型
# ------------------------------------------------------------------


def _attrs_json(mapping: dict[str, Any]) -> str:
    return json.dumps(mapping, ensure_ascii=False, sort_keys=True)


def _entity_from_identity(id_values: Sequence[str]) -> tuple[str, str]:
    nonempty = [v for v in id_values if v]
    if not nonempty:
        return "", ""
    label = next((v for v in reversed(nonempty) if v), nonempty[0])
    key = nonempty[0] if len(nonempty) == 1 else "|".join(nonempty)
    return key, label


def _find_col(headers: Sequence[str], hints: Sequence[str]) -> int | None:
    """按 hint 优先级找列：先精确相等，再「hint 为列名后缀/全含」。

    必须先扫完高优先级 hint，再扫低优先级 —— 否则 ``费用`` 会误命中 ``费用项``，
    把金额列抢走（收费明细 SUM 全变 None）。
    """
    normalized = [str(h).strip() for h in headers]
    for hint in hints:
        for i, text in enumerate(normalized):
            if text == hint:
                return i
    for hint in hints:
        for i, text in enumerate(normalized):
            if hint and hint in text:
                return i
    return None


def _observations_month_series_prepared(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    layout = detect_month_series_layout(headers, data_rows, caption=caption)
    if layout is None:
        return []
    return _melt_month_layout(table_id=table_id, layout=layout, data_rows=data_rows)


def _melt_month_layout(
    *,
    table_id: str,
    layout: _MonthSeriesLayout,
    data_rows: Sequence[Sequence[str]],
) -> list[TableObservation]:
    out: list[TableObservation] = []
    for row_idx, row in enumerate(data_rows):
        if layout.default_year and layout.year_col >= len(row):
            year = _normalize_year_value(layout.default_year)
        else:
            year_raw = (
                str(row[layout.year_col]).strip() if layout.year_col < len(row) else ""
            )
            year = _normalize_year_value(year_raw)
        if not year:
            continue
        id_pairs = [
            (name, str(row[col]).strip() if col < len(row) else "")
            for col, name in layout.identity
        ]
        if not any(v for _n, v in id_pairs):
            continue
        entity = next((v for _n, v in reversed(id_pairs) if v), id_pairs[0][1])
        entity_map = {name: value for name, value in id_pairs if value}
        if layout.category:
            entity_map["类别"] = layout.category
        booth = entity_map.get("铺号") or entity_map.get("列1") or ""
        entity_key = booth or entity
        attrs_json = _attrs_json(entity_map)
        for col, month in layout.months:
            raw = str(row[col]).strip() if col < len(row) else ""
            if not raw:
                continue
            out.append(
                TableObservation(
                    table_id=table_id,
                    table_type="A",
                    entity_key=entity_key,
                    entity_label=entity,
                    period=f"{year}-{int(month):02d}",
                    metric=layout.metric,
                    value=_parse_metric_number(raw),
                    value_surface=raw,
                    attrs_json=attrs_json,
                    source_row=row_idx,
                )
            )
    return out


def _parse_usage_cell(raw: str) -> float:
    """合计用量：退铺/空 → 0；禁止因非数值静默丢行。"""
    text = (raw or "").strip()
    if not text or text in {"退铺", "-", "—", "None", "null"}:
        return 0.0
    # 千分位逗号可剥；``0.04,0.77`` 不是用量（落在单价列）
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text):
        text = text.replace(",", "")
    elif "," in text or "，" in text:
        return 0.0
    number = _parse_metric_number(text)
    return float(number) if number is not None else 0.0


def _parse_price_cells(raw: str) -> list[float]:
    """单价：支持 ``0.04,0.77`` 一格两价；``float`` 直接抛错的坑在这里消化。"""
    text = (raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[,，/;；]+", text) if re.search(r"[,，/;；]", text) else [text]
    out: list[float] = []
    for part in parts:
        number = _parse_metric_number(part.strip())
        if number is not None:
            out.append(float(number))
    return out


def _classify_utility_kind(meter_name: str, prices: Sequence[float]) -> str | None:
    """电/水分类：单价数值特征优先，表名关键词仅作补充。

    生产踩坑：只认表名含「电表/充电」会漏掉「总表/空调/专配/纯编号」等电表
    （实测 345 条、约 40.6 万度；嘛嘛公寓总表 58678 度即此盲区）。
    全表单价簇里 ``< 1.5`` 的均为电价（0.84/0.53/0.77/…），``≥ 1.5`` 为水价
    （3.5/4.55/5.2）。
    """
    meter = meter_name or ""
    if any(k in meter for k in ("水表", "用水", "消防水", "绿化水")):
        return "water"
    if prices and any(p < 1.5 for p in prices):
        return "electric"
    if prices and all(p >= 1.5 for p in prices):
        return "water"
    if any(k in meter for k in ("电表", "充电", "用电", "电度")):
        return "electric"
    # 总表/空调/专配/纯编号：无单价时不敢妄断
    return None


def _period_from_meter_date(raw: str) -> str:
    """抄表日期 → 账期 ``YYYY-MM``。

    月初抄表（日≤5）用量归属上月，便于与「大户用电监视」9 月列对账
    （``2026-10-01`` → ``2026-09``）。
    """
    text = (raw or "").strip().replace("/", "-")
    match = re.match(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        match = re.match(r"(20\d{2})-(\d{1,2})$", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}"
        return text[:7] if text else ""
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if day <= 5:
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    return f"{year}-{month:02d}"


def _observations_meter_reading(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    """户内抄表明细：按名取列 → 清洗 → 电/水分类 → 按费用承担对象写用量观测。"""
    from ekbs.ingest.table_types import _looks_meter_reading

    if not _looks_meter_reading(headers):
        return []
    bearer_col = _find_col(headers, ("费用承担对象", "承担对象"))
    usage_col = _find_col(headers, ("合计用量", "用量"))
    price_col = _find_col(headers, ("单价（元）", "单价(元）", "单价"))
    meter_col = _find_col(headers, ("表计名称", "表名", "表计编号"))
    date_col = _find_col(headers, ("抄表日期", "抄表月", "账期"))
    amount_col = _find_col(headers, ("金额（元）", "金额(元)", "金额"))
    if bearer_col is None or usage_col is None:
        return []

    out: list[TableObservation] = []
    for row_idx, row in enumerate(data_rows):
        # 默认参数绑定当前行，避免闭包捕获循环变量（B023）
        def cell(idx: int | None, bound_row: Sequence[Any] = row) -> str:
            if idx is None or idx >= len(bound_row):
                return ""
            return str(bound_row[idx]).strip()

        bearer = cell(bearer_col)
        usage_raw = cell(usage_col)
        price_raw = cell(price_col)
        meter = cell(meter_col)
        # 关键字段全空 → 跳过
        if not bearer and not usage_raw and not meter:
            continue
        if not bearer:
            continue
        prices = _parse_price_cells(price_raw)
        kind = _classify_utility_kind(meter, prices)
        if kind is None:
            continue
        usage = _parse_usage_cell(usage_raw)
        period = _period_from_meter_date(cell(date_col))
        metric = "用电量" if kind == "electric" else "用水量"
        attrs = {
            str(headers[i]): str(row[i]).strip() if i < len(row) else ""
            for i in range(len(headers))
        }
        attrs["caption"] = caption
        attrs["utility_kind"] = kind
        if prices:
            attrs["unit_price"] = ",".join(str(p) for p in prices)
        out.append(
            TableObservation(
                table_id=table_id,
                table_type="B",
                entity_key=bearer,
                entity_label=bearer,
                period=period,
                metric=metric,
                value=usage,
                value_surface=usage_raw or str(usage),
                attrs_json=_attrs_json(attrs),
                source_row=row_idx,
            )
        )
        amount_raw = cell(amount_col)
        if amount_raw:
            amount = _parse_metric_number(amount_raw.replace(",", ""))
            if amount is None and prices:
                # 双价格无法稳乘；有现成金额列才写
                amount = _parse_metric_number(amount_raw)
            if amount is not None:
                out.append(
                    TableObservation(
                        table_id=table_id,
                        table_type="B",
                        entity_key=bearer,
                        entity_label=bearer,
                        period=period,
                        metric="电费" if kind == "electric" else "水费",
                        value=float(amount),
                        value_surface=amount_raw,
                        attrs_json=_attrs_json(attrs),
                        source_row=row_idx,
                    )
                )
    return out


def _observations_fee_detail(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    from ekbs.ingest.table_types import _looks_meter_reading

    if _looks_meter_reading(headers):
        return _observations_meter_reading(
            caption=caption, table_id=table_id, headers=headers, data_rows=data_rows
        )
    account_col = _find_col(
        headers,
        (
            "费用承担对象",
            "承担对象",
            "户号",
            "户名",
            "账号",
            "客户号",
            "水卡",
            "用户编号",
            "表号",
            "用户",
        ),
    )
    # 「金额」必须先于「费用」，避免命中「费用项」列
    amount_col = _find_col(
        headers, ("金额", "实收", "欠费", "电费", "水费", "气费", "费用")
    )
    period_col = _find_col(
        headers, ("月份", "账期", "期间", "年月", "所属期", "抄表月", "抄表日期")
    )
    fee_item_col = _find_col(headers, ("费用项", "费项", "费用名称", "项目", "科目"))
    usage_col = _find_col(headers, ("合计用量", "用量", "数量", "度数"))

    out: list[TableObservation] = []
    for row_idx, row in enumerate(data_rows):
        def cell(idx: int | None, bound_row: Sequence[Any] = row) -> str:
            if idx is None or idx >= len(bound_row):
                return ""
            return str(bound_row[idx]).strip()

        account = cell(account_col) or cell(0)
        period = normalize_header_period(cell(period_col))
        fee_item = cell(fee_item_col) or (caption.strip() or "金额")
        amount_raw = cell(amount_col) if amount_col is not None else ""
        usage_raw = cell(usage_col) if usage_col is not None else ""
        attrs = {
            str(headers[i]): str(row[i]).strip() if i < len(row) else ""
            for i in range(len(headers))
            if i not in {account_col, amount_col, period_col, fee_item_col}
        }
        if amount_raw:
            out.append(
                TableObservation(
                    table_id=table_id,
                    table_type="B",
                    entity_key=account,
                    entity_label=account,
                    period=period,
                    metric=fee_item or "金额",
                    value=_parse_metric_number(amount_raw),
                    value_surface=amount_raw,
                    attrs_json=_attrs_json(attrs),
                    source_row=row_idx,
                )
            )
        elif usage_raw:
            out.append(
                TableObservation(
                    table_id=table_id,
                    table_type="B",
                    entity_key=account,
                    entity_label=account,
                    period=period,
                    metric="用量",
                    value=_parse_metric_number(usage_raw),
                    value_surface=usage_raw,
                    attrs_json=_attrs_json(attrs),
                    source_row=row_idx,
                )
            )
        else:
            out.append(
                TableObservation(
                    table_id=table_id,
                    table_type="B",
                    entity_key=account,
                    entity_label=account,
                    period=period,
                    metric=fee_item or "记录",
                    value=None,
                    value_surface="",
                    attrs_json=_attrs_json(attrs),
                    source_row=row_idx,
                )
            )
    return out


def _line_role_from_label(label: str) -> str:
    text = (label or "").strip()
    if not text:
        return "normal"
    if "合计" in text or "总计" in text:
        return "total"
    if "小计" in text:
        return "subtotal"
    return "normal"


def _financial_account_col(headers: Sequence[str]) -> int:
    """科目列：优先「科目/项目/项目名称」，否则第 0 列。"""
    for hint in ("科目", "项目名称", "项目", "会计科目"):
        idx = _find_col(headers, (hint,))
        if idx is not None:
            return idx
    return 0


def _observations_financial(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    """财报：科目 × 期间；合计/小计行打 ``line_role``，供印刷合计优先引用。"""
    account_col = _financial_account_col(headers)
    period_cols = [
        i
        for i, h in enumerate(headers)
        if i != account_col and _PERIOD_HEADER_RE.search(str(h))
    ]
    if not period_cols:
        period_cols = [i for i in range(len(headers)) if i != account_col]

    out: list[TableObservation] = []
    for row_idx, row in enumerate(data_rows):
        account = str(row[account_col]).strip() if account_col < len(row) else ""
        if not account:
            continue
        role = _line_role_from_label(account)
        for col in period_cols:
            raw = str(row[col]).strip() if col < len(row) else ""
            if not raw:
                continue
            number = _parse_metric_number(raw)
            # 合计行允许非纯数字表面（如「—」）入库为空值，便于定位行角色
            if number is None and role == "normal":
                continue
            period_name = str(headers[col]).strip()
            out.append(
                TableObservation(
                    table_id=table_id,
                    table_type="C",
                    entity_key=account,
                    entity_label=account,
                    period=period_name,
                    metric=account,
                    value=number,
                    value_surface=raw,
                    line_role=role,
                    attrs_json=_attrs_json(
                        {
                            "caption": caption,
                            "period_header": period_name,
                            "line_role": role,
                        }
                    ),
                    source_row=row_idx,
                )
            )
    return out


def _observations_ledger(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    del caption
    key_col = _find_col(
        headers,
        ("合同号", "合同编号", "资产编号", "资产号", "设备编号", "流水号", "单据号", "编号"),
    )
    if key_col is None:
        key_col = 0
    out: list[TableObservation] = []
    for row_idx, row in enumerate(data_rows):
        key = str(row[key_col]).strip() if key_col < len(row) else ""
        if not key:
            continue
        attrs = {
            str(headers[i]): str(row[i]).strip() if i < len(row) else ""
            for i in range(len(headers))
            if i != key_col
        }
        label = (
            attrs.get("名称")
            or attrs.get("乙方")
            or attrs.get("资产名称")
            or attrs.get("合同名称")
            or next((v for v in attrs.values() if v), key)
        )
        out.append(
            TableObservation(
                table_id=table_id,
                table_type="D",
                entity_key=key,
                entity_label=str(label),
                period="",
                metric="ledger",
                value=None,
                value_surface="",
                attrs_json=_attrs_json(attrs),
                source_row=row_idx,
            )
        )
    return out


def _wide_bill_entity(
    id_headers: Sequence[str], id_values: Sequence[str]
) -> tuple[str, str]:
    """身份列里优先用水卡号/户号作 entity_key，便于问答等值过滤。"""
    key, label = _entity_from_identity(id_values)
    for hint in ("水卡号", "水卡", "卡号", "户号", "表号", "账号"):
        for i, header in enumerate(id_headers):
            if hint in str(header) and i < len(id_values) and id_values[i]:
                return id_values[i], id_values[i]
    return key, label


def _observations_wide_bill(
    *,
    caption: str,
    table_id: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[TableObservation]:
    """宽账单：按「身份 + 单月指标组」熔成观测；金额与用量分列写入。"""
    if not _is_wide_metric_table(headers):
        return _observations_fee_detail(
            caption=caption, table_id=table_id, headers=headers, data_rows=data_rows
        )
    id_n = _leading_identity_width(headers)
    out: list[TableObservation] = []
    for slice_headers, slice_rows in _iter_metric_group_slices(headers, data_rows):
        period = ""
        for h in slice_headers[id_n:]:
            period = _period_from_metric_header(str(h))
            if period:
                break
        # 一组内可写多指标（金额、用量…），避免问答只能命中其中一个
        metric_cols: list[tuple[int, str]] = []
        for hint in ("金额", "费用", "实收", "用量", "数量", "单价"):
            col = _find_col(slice_headers, (hint,))
            if col is None or col < id_n:
                continue
            name = _metric_name_from_header(str(slice_headers[col]), hint)
            if any(c == col for c, _ in metric_cols):
                continue
            metric_cols.append((col, name))
        if not metric_cols:
            continue
        id_headers = [str(h) for h in slice_headers[:id_n]]
        for row_idx, row in enumerate(slice_rows):
            id_values = [str(row[i]).strip() if i < len(row) else "" for i in range(id_n)]
            entity_key, entity_label = _wide_bill_entity(id_headers, id_values)
            if not entity_key and not entity_label:
                continue
            attrs = {
                str(slice_headers[i]): str(row[i]).strip() if i < len(row) else ""
                for i in range(len(slice_headers))
            }
            attrs["caption"] = caption
            for col, metric_name in metric_cols:
                raw = str(row[col]).strip() if col < len(row) else ""
                if not raw:
                    continue
                number = _parse_metric_number(raw)
                if number is None:
                    continue
                out.append(
                    TableObservation(
                        table_id=table_id,
                        table_type="E",
                        entity_key=entity_key,
                        entity_label=entity_label,
                        period=period,
                        metric=metric_name,
                        value=number,
                        value_surface=raw,
                        attrs_json=_attrs_json(attrs),
                        source_row=row_idx,
                    )
                )
    return out
