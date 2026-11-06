"""受限结构化问答 —— 只对 ``ekbs_table_observation`` 做白名单聚合/查找。

铁律：
* 执行前必须用 ``PermContext.visible_shard_ids`` 求交；无可见分片 → 拒/空；
* SQL 白名单列 + 固定聚合（SUM/COUNT/AVG），禁止任意表达式 / Text2SQL；
* 财报同比/环比：无对应列时返回 N/A，**禁止发明 YoY**；
* 印刷合计行（``line_role=total``）优先于重算。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ekbs.logging import get_logger
from ekbs.permission.context import PermContext

log = get_logger(component="retrieval.structured_query")

MODE_STRUCTURED = "structured_query"

AggOp = Literal["sum", "count", "avg", "lookup", "filter", "printed_total", "yoy_na"]

_AGG_HINTS = (
    "合计",
    "一共",
    "总计",
    "汇总",
    "多少笔",
    "多少条",
    "几笔",
    "几条",
    "总和",
    "共计",
    "总共",
    "求和",
    "共有多少",
    "平均",
    "均值",
)
_COUNT_HINTS = (
    "多少笔",
    "多少条",
    "几笔",
    "几条",
    "有多少",
    "共有多少",
    "一共多少",
    "总共多少",
)
_AVG_HINTS = ("平均", "均值")
_YOY_HINTS = ("同比", "环比", "较上年", "较去年", "同比增长", "环比增长")
_LOOKUP_HINTS = ("是谁", "是什么", "哪家", "哪个", "查一下", "查询")
_FILTER_HINTS = ("状态=", "状态为", "状态是", "列表", "有哪些")
#: 问句里点名的费项/指标 → 等值过滤 ``metric``（白名单，防任意表达式）。
#: 不含「欠费/实收」——它们更常作金额列名或「欠费记录」口语，不当作费项过滤。
_METRIC_HINTS = (
    "电费",
    "水费",
    "气费",
    "热费",
    "服务费",
    "物业费",
    "违约金",
    "滞纳金",
    "金额",
    "用量",
    "单价",
    "费用",
)

_ENTITY_RE = re.compile(
    r"(?:户号|户名|账号|合同|合同号|水卡号|水卡|卡号|表号|编号)"
    r"[：:\s]*([A-Za-z0-9\-_\u4e00-\u9fff]+)"
)
_PERIOD_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>1[0-2]|[1-9])\s*月|"
    r"(?P<ymd>20\d{2}[-/](?:1[0-2]|0?[1-9])[-/](?:[0-3]?\d))|"
    r"(?P<ym>20\d{2}[-/](?:1[0-2]|0?[1-9]))"
)
_STATUS_RE = re.compile(r"状态\s*[=＝为是]\s*([^\s，,。；;]+)")


@dataclass
class StructuredQueryHit:
    """受限聚合/查找命中。"""

    value: float | int | str | None = None
    surface: str = ""
    unit: str = ""
    op: str = ""
    reason: str = ""
    chunk_ids: tuple[int, ...] = ()
    doc_ids: tuple[int, ...] = ()
    table_type: str = ""
    metric: str = ""
    period: str = ""
    entity_key: str = ""
    rows_touched: int = 0
    detail: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_na(self) -> bool:
        return self.value is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "surface": self.surface,
            "unit": self.unit,
            "op": self.op,
            "reason": self.reason,
            "chunk_ids": list(self.chunk_ids),
            "doc_ids": list(self.doc_ids),
            "table_type": self.table_type,
            "metric": self.metric,
            "period": self.period,
            "entity_key": self.entity_key,
            "rows_touched": self.rows_touched,
            "detail": list(self.detail),
            **self.extras,
        }


#: 兼容旧名
StructuredAnswer = StructuredQueryHit


def is_structured_query_intent(question: str) -> bool:
    text = (question or "").strip()
    if not text:
        return False
    if any(h in text for h in _YOY_HINTS):
        return True
    if any(h in text for h in _AGG_HINTS):
        return True
    if any(h in text for h in _LOOKUP_HINTS) and (
        "合同" in text or "资产" in text or "编号" in text
    ):
        return True
    if any(h in text for h in _FILTER_HINTS):
        return True
    # 宽账单：水卡/卡号 + 金额/用量（可不带「合计」）
    if re.search(r"(水卡号|水卡|卡号)", text) and re.search(
        r"(金额|用量|费用|多少)", text
    ):
        return True
    return False


def resolve_structured_op(question: str) -> AggOp:
    text = (question or "").strip()
    if any(h in text for h in _YOY_HINTS):
        return "yoy_na"
    if any(h in text for h in _COUNT_HINTS):
        return "count"
    if any(h in text for h in _AVG_HINTS):
        return "avg"
    if any(h in text for h in _FILTER_HINTS):
        return "filter"
    if any(h in text for h in _LOOKUP_HINTS):
        return "lookup"
    # 「营业成本合计」等财报印刷行优先；收费明细无 line_role=total 时会回退 SUM
    if "合计" in text or "总计" in text:
        return "printed_total"
    return "sum"


def _metric_from_question(question: str) -> str | None:
    """从问句提取白名单费项/指标名；未命中返回 None（不过滤 metric）。"""
    text = question or ""
    for hint in _METRIC_HINTS:
        if hint in text:
            return hint
    return None


def visible_observation_shard_ids(
    perm: PermContext,
    candidate_shard_ids: Sequence[str] | None = None,
) -> frozenset[str]:
    visible = frozenset(perm.visible_shard_ids)
    if not visible:
        return frozenset()
    if candidate_shard_ids is None:
        return visible
    return visible & frozenset(str(s) for s in candidate_shard_ids)


def count_observations(
    conn: sqlite3.Connection,
    *,
    perm: PermContext,
    doc_id: int | None = None,
    table_type: str | None = None,
) -> int:
    shards = visible_observation_shard_ids(perm)
    if not shards:
        return 0
    placeholders = ",".join("?" * len(shards))
    sql = (
        f"SELECT COUNT(*) AS c FROM ekbs_table_observation "
        f"WHERE shard_id IN ({placeholders})"
    )
    params: list[Any] = list(shards)
    if doc_id is not None:
        sql += " AND doc_id = ?"
        params.append(int(doc_id))
    if table_type:
        sql += " AND table_type = ?"
        params.append(str(table_type))
    row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row is not None else 0)


def _period_from_question(question: str) -> str | None:
    match = _PERIOD_RE.search(question or "")
    if not match:
        return None
    if match.group("ymd"):
        raw = match.group("ymd").replace("/", "-")
        parts = raw.split("-")
        return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    if match.group("ym"):
        raw = match.group("ym").replace("/", "-")
        parts = raw.split("-")
        return f"{parts[0]}-{int(parts[1]):02d}"
    year = match.group("year")
    month = match.group("month")
    if month and year:
        return f"{year}-{int(month):02d}"
    if month:
        return f"{int(month):02d}"
    return None


def _period_sql_clause(period: str | None, params: list[Any]) -> str:
    """期间模糊匹配：``2024-01`` 能打上观测里的 ``2024-01-01``。"""
    if not period:
        return ""
    period = period.strip()
    # 完整日
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", period):
        params.append(period)
        return " AND period = ?"
    # 年-月
    if re.fullmatch(r"20\d{2}-\d{2}", period):
        params.extend([period, f"{period}-%"])
        return " AND (period = ? OR period LIKE ?)"
    # 仅月号（01 / 1）
    if re.fullmatch(r"\d{1,2}", period):
        mm = int(period)
        params.extend(
            [
                f"%-{mm:02d}-%",
                f"%-{mm:02d}",
                f"{mm}月",
                f"%/{mm:02d}/%",
            ]
        )
        return (
            " AND (period LIKE ? OR period LIKE ? OR period LIKE ? OR period LIKE ?)"
        )
    params.extend([period, f"%{period}%"])
    return " AND (period = ? OR period LIKE ?)"


def _entity_from_question(question: str) -> str | None:
    match = _ENTITY_RE.search(question or "")
    if match:
        return match.group(1).strip()
    quoted = re.search(r"[「\"“']([^」\"”']+)[」\"”']", question or "")
    if quoted:
        return quoted.group(1).strip()
    return None


def try_structured_query(
    conn: sqlite3.Connection,
    question: str,
    perm: PermContext,
    *,
    prefer_doc_ids: Sequence[int] | None = None,
) -> StructuredQueryHit | None:
    """尝试受限结构化问答；不适用时返回 ``None``（调用方降级）。"""
    if not is_structured_query_intent(question):
        return None
    shards = tuple(visible_observation_shard_ids(perm))
    if not shards:
        log.info("structured_query_denied_no_shard", user_id=perm.user_id)
        return StructuredQueryHit(
            value=None,
            op="count",
            reason="no_visible_shard",
            surface="N/A",
        )

    op = resolve_structured_op(question)
    if op == "yoy_na":
        copied = _try_copy_yoy_column(conn, question, shards, prefer_doc_ids)
        if copied is not None:
            return copied
        return StructuredQueryHit(
            value=None,
            op="yoy_na",
            reason="yoy_column_missing",
            surface="N/A",
        )

    entity = _entity_from_question(question)
    period = _period_from_question(question)
    metric = _metric_from_question(question)
    # 宽账单未点名指标时默认「金额」，避免把用量一并 SUM 进去
    if metric is None and re.search(r"(水卡号|水卡|卡号)", question or ""):
        metric = "用量" if "用量" in (question or "") else "金额"
    status = None
    status_match = _STATUS_RE.search(question or "")
    if status_match:
        status = status_match.group(1).strip()

    if op == "lookup":
        return _lookup_row(
            conn, shards, entity=entity, question=question, prefer_doc_ids=prefer_doc_ids
        )
    if op == "filter":
        return _filter_rows(conn, shards, status=status, prefer_doc_ids=prefer_doc_ids)
    if op == "printed_total":
        printed = _printed_total(
            conn, shards, question=question, prefer_doc_ids=prefer_doc_ids
        )
        if printed is not None:
            return printed
        op = "sum"

    return _aggregate(
        conn,
        shards,
        op=op,  # type: ignore[arg-type]
        entity=entity,
        period=period,
        metric=metric,
        prefer_doc_ids=prefer_doc_ids,
    )


def _shard_clause(shards: Sequence[str]) -> tuple[str, list[Any]]:
    placeholders = ",".join("?" for _ in shards)
    return f"shard_id IN ({placeholders})", list(shards)


def _doc_clause(prefer_doc_ids: Sequence[int] | None, params: list[Any]) -> str:
    if not prefer_doc_ids:
        return ""
    placeholders = ",".join("?" for _ in prefer_doc_ids)
    params.extend(int(d) for d in prefer_doc_ids)
    return f" AND doc_id IN ({placeholders})"


def _split_ids(raw: Any) -> tuple[int, ...]:
    if raw is None:
        return ()
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return tuple(out)


def _aggregate(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    op: Literal["sum", "count", "avg"],
    entity: str | None,
    period: str | None,
    metric: str | None = None,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit:
    """白名单等值过滤：``shard_id``（ACL）+ 可选 doc / entity / period / metric。

    只允许固定 ``SUM|COUNT|AVG(value)``，禁止任意表达式。
    """
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    if entity:
        where += " AND (entity_key = ? OR entity_label = ? OR entity_key LIKE ?)"
        params.extend([entity, entity, f"%{entity}%"])
    where += _period_sql_clause(period, params)
    if metric:
        # 精确或包含：观测 metric 可能是「电费」或带后缀的「电费-峰」
        where += " AND (metric = ? OR metric LIKE ?)"
        params.extend([metric, f"%{metric}%"])

    if op == "count":
        sql = (
            f"SELECT COUNT(*) AS v FROM ekbs_table_observation WHERE {where}"
        )
        row = conn.execute(sql, params).fetchone()
        value = int(row["v"] or 0) if row else 0
        return StructuredQueryHit(
            value=value,
            surface=str(value),
            op="count",
            rows_touched=value,
            metric=metric or "",
            period=period or "",
            entity_key=entity or "",
        )

    sql = (
        f"SELECT {'AVG' if op == 'avg' else 'SUM'}(value) AS v, COUNT(*) AS n, "
        f"GROUP_CONCAT(DISTINCT chunk_id) AS cids, "
        f"GROUP_CONCAT(DISTINCT doc_id) AS docs "
        f"FROM ekbs_table_observation WHERE {where} AND value IS NOT NULL"
    )
    row = conn.execute(sql, params).fetchone()
    if row is None or row["v"] is None or int(row["n"] or 0) == 0:
        return StructuredQueryHit(
            value=None,
            op=op,
            reason="no_rows",
            surface="N/A",
            metric=metric or "",
            period=period or "",
            entity_key=entity or "",
        )

    raw_v = float(row["v"])
    value: float | int = int(raw_v) if raw_v == int(raw_v) else round(raw_v, 6)
    return StructuredQueryHit(
        value=value,
        surface=str(value),
        op=op,
        rows_touched=int(row["n"] or 0),
        chunk_ids=_split_ids(row["cids"]),
        doc_ids=_split_ids(row["docs"]),
        metric=metric or "",
        period=period or "",
        entity_key=entity or "",
    )


def _account_subject_from_question(question: str) -> str | None:
    """从「营业成本合计是多少」抽出科目名；去掉合计/期间口语尾巴。"""
    text = (question or "").strip()
    for hint in (
        "营业成本",
        "营业收入",
        "营业利润",
        "净利润",
        "利润总额",
        "总成本",
        "管理费用",
        "销售费用",
        "财务费用",
        "资产总计",
        "负债合计",
        "所有者权益",
    ):
        if hint in text:
            return hint
    # 兜底：取「…合计」前的科目片段
    match = re.search(
        r"([\u4e00-\u9fffA-Za-z0-9]{2,20}?)\s*(?:合计|总计|小计)",
        text,
    )
    if match:
        return match.group(1).strip()
    return None


def _period_preference_from_question(question: str) -> tuple[str, ...]:
    """问句偏好的期间表头关键词（越靠前越优先）。"""
    text = question or ""
    prefs: list[str] = []
    if "本年累计" in text or "本年" in text:
        prefs.append("本年累计")
    if "本期" in text or "本月" in text:
        prefs.extend(["本期金额", "本期", "本月"])
    if "期末" in text:
        prefs.append("期末")
    if "期初" in text:
        prefs.append("期初")
    if "上期" in text or "上月" in text:
        prefs.extend(["上期金额", "上期", "上月"])
    return tuple(prefs)


def _printed_total(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    question: str,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit | None:
    """优先引用 ``line_role=total`` 印刷行；不按明细行重算合计。"""
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += (
        " AND table_type = 'C' AND line_role = 'total' AND value IS NOT NULL"
    )
    subject = _account_subject_from_question(question)
    if subject:
        where += " AND (metric LIKE ? OR entity_label LIKE ? OR entity_key LIKE ?)"
        params.extend([f"%{subject}%", f"%{subject}%", f"%{subject}%"])

    rows = conn.execute(
        f"SELECT value, value_surface, chunk_id, doc_id, entity_label, metric, period "
        f"FROM ekbs_table_observation WHERE {where} ORDER BY source_row LIMIT 20",
        params,
    ).fetchall()
    if not rows:
        return None

    period_prefs = _period_preference_from_question(question)

    def _score(row: Any) -> tuple[int, int]:
        period = str(row["period"] or "")
        # 有期间偏好时：命中偏好 +0，否则惩罚；无偏好时默认偏本期金额
        if period_prefs:
            for idx, pref in enumerate(period_prefs):
                if pref in period:
                    return (0, idx)
            return (1, 99)
        if "本期" in period:
            return (0, 0)
        if "本年累计" in period:
            return (0, 1)
        return (0, 2)

    row = sorted(rows, key=_score)[0]
    raw_v = float(row["value"])
    value: float | int = int(raw_v) if raw_v == int(raw_v) else raw_v
    return StructuredQueryHit(
        value=value,
        surface=str(row["value_surface"] or value),
        op="printed_total",
        rows_touched=1,
        chunk_ids=(int(row["chunk_id"]),) if row["chunk_id"] is not None else (),
        doc_ids=(int(row["doc_id"]),),
        entity_key=str(row["entity_label"] or ""),
        metric=str(row["metric"] or ""),
        period=str(row["period"] or ""),
        detail=[
            {
                "entity": row["entity_label"],
                "metric": row["metric"],
                "period": row["period"],
            }
        ],
        extras={"prefer_printed": True},
    )


def _try_copy_yoy_column(
    conn: sqlite3.Connection,
    question: str,
    shards: Sequence[str],
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit | None:
    """仅当表内**已有**同比/环比列时抄写；禁止用本期/上期推算。"""
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += (
        " AND table_type = 'C' AND value IS NOT NULL AND ("
        "period LIKE '%同比%' OR period LIKE '%环比%' "
        "OR metric LIKE '%同比%' OR metric LIKE '%环比%')"
    )
    subject = _account_subject_from_question(question)
    if subject:
        where += " AND (metric LIKE ? OR entity_label LIKE ? OR entity_key LIKE ?)"
        params.extend([f"%{subject}%", f"%{subject}%", f"%{subject}%"])
    # 明确不要把「本期/上期」当成同比来源（防发明）
    where += " AND period NOT LIKE '%本期%' AND period NOT LIKE '%上期%'"

    row = conn.execute(
        f"SELECT value, value_surface, chunk_id, doc_id, entity_label, metric, period "
        f"FROM ekbs_table_observation WHERE {where} LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    raw_v = float(row["value"])
    value: float | int = int(raw_v) if raw_v == int(raw_v) else raw_v
    kind = "同比" if "同比" in str(row["period"]) or "同比" in question else "环比"
    return StructuredQueryHit(
        value=value,
        surface=str(row["value_surface"] or value),
        op="printed_total",
        rows_touched=1,
        chunk_ids=(int(row["chunk_id"]),) if row["chunk_id"] is not None else (),
        doc_ids=(int(row["doc_id"]),),
        entity_key=str(row["entity_label"] or ""),
        metric=str(row["metric"] or ""),
        period=str(row["period"] or ""),
        extras={
            "question": question,
            "yoy_copied": True,
            "yoy_kind": kind,
            "source": "printed_yoy_column",
        },
    )


_LEDGER_FIELD_HINTS = (
    "乙方",
    "甲方",
    "负责人",
    "状态",
    "位置",
    "名称",
    "资产名称",
    "合同名称",
    "型号",
    "规格",
)


def _ledger_entity_from_question(question: str) -> str | None:
    text = question or ""
    patterns = (
        r"(?:合同号|合同编号|合同)\s*[：:\s]*([A-Za-z0-9\-_/]+)",
        r"(?:资产编号|资产号|设备编号|流水号|工单号|编号)\s*[：:\s]*([A-Za-z0-9\-_/]+)",
        r"(?:HT|AS|EQ)[-_][A-Za-z0-9\-_/]+",
    )
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if not match:
            continue
        if match.lastindex:
            return match.group(1).strip()
        return match.group(0).strip()
    return _entity_from_question(text)


def _ledger_field_from_question(question: str, attrs: dict[str, Any]) -> str | None:
    for name in _LEDGER_FIELD_HINTS:
        if name in question and name in attrs:
            return name
    # 「…的乙方是谁」已由 hint 覆盖；兜底取首个非空属性
    return next((k for k, v in attrs.items() if v), None)


def _lookup_row(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    entity: str | None,
    question: str,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit:
    entity = entity or _ledger_entity_from_question(question)
    if not entity:
        return StructuredQueryHit(value=None, op="lookup", reason="no_entity", surface="N/A")

    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += " AND table_type = 'D' AND (entity_key = ? OR entity_key LIKE ?)"
    params.extend([entity, f"%{entity}%"])
    row = conn.execute(
        f"SELECT entity_key, entity_label, attrs_json, chunk_id, doc_id "
        f"FROM ekbs_table_observation WHERE {where} "
        f"ORDER BY CASE WHEN entity_key = ? THEN 0 ELSE 1 END LIMIT 1",
        [*params, entity],
    ).fetchone()
    if row is None:
        return StructuredQueryHit(value=None, op="lookup", reason="not_found", surface="N/A")

    try:
        attrs = json.loads(row["attrs_json"] or "{}")
    except json.JSONDecodeError:
        attrs = {}
    field_name = _ledger_field_from_question(question, attrs)
    answer = attrs.get(field_name, "") if field_name else row["entity_label"]
    if answer == "" or answer is None:
        answer = row["entity_label"]
    return StructuredQueryHit(
        value=str(answer),
        surface=str(answer),
        op="lookup",
        rows_touched=1,
        chunk_ids=(int(row["chunk_id"]),) if row["chunk_id"] is not None else (),
        doc_ids=(int(row["doc_id"]),),
        entity_key=str(row["entity_key"]),
        detail=[{"entity_key": row["entity_key"], "field": field_name, "attrs": attrs}],
        extras={"source_formats": "html_table"},
    )


def _filter_rows(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    status: str | None,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit:
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += " AND table_type = 'D'"
    # 优先 json_extract（精确键）；失败再回退 LIKE（旧库无 JSON1 时）
    if status:
        where += " AND (json_extract(attrs_json, '$.状态') = ? OR attrs_json LIKE ?)"
        params.extend([status, f'%\"状态\": \"{status}\"%'])
    try:
        rows = conn.execute(
            f"SELECT entity_key, entity_label, attrs_json, chunk_id, doc_id "
            f"FROM ekbs_table_observation WHERE {where} LIMIT 200",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        # 无 JSON1 扩展时退回模糊匹配
        where, params = _shard_clause(shards)
        where += _doc_clause(prefer_doc_ids, params)
        where += " AND table_type = 'D'"
        if status:
            where += " AND attrs_json LIKE ?"
            params.append(f"%{status}%")
        rows = conn.execute(
            f"SELECT entity_key, entity_label, attrs_json, chunk_id, doc_id "
            f"FROM ekbs_table_observation WHERE {where} LIMIT 200",
            params,
        ).fetchall()
    detail = []
    chunk_ids: list[int] = []
    doc_ids: list[int] = []
    for row in rows:
        try:
            attrs = json.loads(row["attrs_json"] or "{}")
        except json.JSONDecodeError:
            attrs = {}
        if status and attrs.get("状态") != status and status not in attrs.values():
            continue
        detail.append(
            {
                "entity_key": row["entity_key"],
                "entity_label": row["entity_label"],
                "attrs": attrs,
            }
        )
        if row["chunk_id"] is not None:
            chunk_ids.append(int(row["chunk_id"]))
        doc_ids.append(int(row["doc_id"]))
    labels = [d["entity_key"] for d in detail]
    surface = "、".join(labels[:50]) if labels else "（无匹配）"
    return StructuredQueryHit(
        value=len(detail),
        surface=surface,
        op="filter",
        rows_touched=len(detail),
        chunk_ids=tuple(chunk_ids),
        doc_ids=tuple(sorted(set(doc_ids))),
        detail=detail,
    )
