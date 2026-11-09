"""受限结构化问答 —— 只对 ``ekbs_table_observation`` 做白名单聚合/查找。

铁律：
* 执行前必须用 ``PermContext.visible_shard_ids`` 求交；无可见分片 → 拒/空；
* SQL 白名单列 + 固定聚合（SUM/COUNT/AVG），禁止任意表达式 / Text2SQL；
* 同比/环比：有印刷列则提取；无则可用两期基数由**模块**算比率（禁止模型心算）；
* 印刷合计行（``line_role=total``）优先于重算；
* 精排路径可 ``require_prefer_docs``，计算范围锁在召回文档内。
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

AggOp = Literal[
    "sum",
    "count",
    "avg",
    "lookup",
    "extract",
    "compare",
    "diff",
    "filter",
    "printed_total",
    "yoy_na",
]

_AGG_HINTS = (
    "合计",
    "一共",
    "总计",
    "汇总",
    "累计",
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
#: 数字计算类（强制走规划→计算模块，禁止散文读表）
_NUMERIC_CALC_HINTS = (
    "明细",
    "合计",
    "一共",
    "总计",
    "汇总",
    "累计",
    "多少",
    "平均",
    "均值",
    "同比",
    "环比",
    "差异",
    "对比",
    "比较",
    "求和",
    "用量",
    "电费",
    "水费",
    "气费",
    "单价",
    "金额",
)
_PERIOD_HINT_RE = re.compile(
    r"(?:20\d{2}\s*年)|(?:1[0-2]|[1-9])\s*月|(?:全年|整年)"
)
#: 问句里「年」有、但没有「月」——单独配弱指标（费用/金额）时不应强制进 SUM。
_YEAR_ONLY_PERIOD_RE = re.compile(r"20\d{2}\s*年")
_MONTH_IN_QUESTION_RE = re.compile(r"(?:1[0-2]|[1-9])\s*月|(?:全年|整年)")
#: 方案/预算类「文中已写明的结果值」——应走散文，禁止当抄表 SUM。
#: 例：「2026年中央空调节能改造…」「2026年预算工程费用2月预算额度」
#: 年度预算分月宽表单元格不是用电量观测 SUM，交给散文 LLM（父块+表叶给齐）。
_STATED_VALUE_TOPIC_RE = re.compile(
    r"(?:节能改造|改造方案|改造成本|改造预算|投资回收期|回收期|"
    r"预计年节约|年节约电费|节约费用|节约电费|"
    r"预算额度|预算额|年度预算|预算分月|分月预算)"
)
#: 问句还要方案/正文描述（不只数字）→ 必须走 prose，不能 extract 截断成 KPI 行。
#: 「节能改造方案改造成本」里的「改造方案」是文档名片段，不算要正文；
#: 「改造方案、成本」这种列举才算。
_PROSE_ASK_RE = re.compile(
    r"(?:改造方案[、，和与是]|实施方案|方案内容|方案概述|方案要点|方案介绍|"
    r"怎么改|如何改造|改造措施|具体措施|正文|全文|"
    r"(?:^|[^改节])要点|(?:^|[^方])概述)"
)
_STRONG_AGG_HINTS = ("合计", "一共", "总计", "汇总", "累计", "同比", "环比", "求和")
_WEAK_METRICS = frozenset({"费用", "金额"})
_METER_METRICS = frozenset({"用电量", "用水量", "用气量", "用量", "电费", "水费", "气费"})

_COUNT_HINTS = (
    "多少笔",
    "多少条",
    "多少台",
    "几笔",
    "几条",
    "几台",
    "有多少",
    "共有多少",
    "一共多少",
    "总共多少",
)
#: 实体字符含 ``#``：表内常见「二期15#专配室」，缺了会抽不出地点、SUM 成全表
_ENTITY_CHARS = r"\u4e00-\u9fffA-Za-z0-9·\-_#"
#: 「一共有多少台变压器」/「变压器有几台」——台=设备台数，不是观测行数
_UNIT_COUNT_RE = re.compile(r"(?:多少|几)\s*台")
_UNIT_COUNT_ENTITY_RE = re.compile(
    r"(?:一共|总共|共)?有?(?:多少|几)\s*台\s*"
    rf"([{_ENTITY_CHARS}]{{2,24}})"
    r"|"
    rf"([{_ENTITY_CHARS}]{{2,24}}?)"
    r"(?:一共|总共|共)?有?(?:多少|几)\s*台"
)
#: 第二分支会把「一共有多少台」里的「一共」误当成设备名
_UNIT_COUNT_ENTITY_STOP = frozenset(
    {"一共", "总共", "共有", "还有", "现有", "大约", "大概", "目前", "现在", "合计"}
)
_AVG_HINTS = ("平均", "均值")
_YOY_HINTS = ("同比", "环比", "较上年", "较去年", "同比增长", "环比增长")
_LOOKUP_HINTS = ("是谁", "是什么", "哪家", "哪个", "查一下", "查询")
_FILTER_HINTS = ("状态=", "状态为", "状态是", "列表", "有哪些")
#: 问句里点名的费项/指标 → 等值过滤 ``metric``（白名单，防任意表达式）。
#: 不含「欠费/实收」——它们更常作金额列名或「欠费记录」口语，不当作费项过滤。
_METRIC_HINTS = (
    # 长词在前：避免「用电量」被短词「用量」抢走（"用量" not in "用电量"，但顺序仍要稳）
    "用电量",
    "用水量",
    "用气量",
    "电费",
    "水费",
    "气费",
    "热费",
    "服务费",
    "物业费",
    "违约金",
    "滞纳金",
    "变压器损耗",
    "金额",
    "用量",
    "单价",
    "费用",
)

_ENTITY_RE = re.compile(
    r"(?:户号|户名|账号|合同|合同号|水卡号|水卡|卡号|表号|编号)"
    r"[：:\s]*([A-Za-z0-9\-_\u4e00-\u9fff]+)"
)
#: 「嘛嘛公寓2026年1-9月用电量」——无户号前缀时，取期间/指标之前的中文名
_BARE_ENTITY_RE = re.compile(
    rf"^([{_ENTITY_CHARS}]{{2,32}}?)"
    r"(?=(?:20\d{2}\s*年)|(?:的?(?:用电量|用水量|用气量|用电|用水|用气|电费|水费|气费|热费|"
    r"金额|用量|变压器损耗))"
    r"|(?:1[0-2]|[1-9])\s*(?:[-—~至到]\s*(?:1[0-2]|[1-9])\s*)?月)"
)
#: 「2025年1-3月锅炉燃气单价」——问句以账期开头时，取月之后、指标之前的主体
#: 勿单独用「损耗」作边界：否则「…9月变压器损耗」会被抽成实体「变压器」
_PERIOD_THEN_ENTITY_RE = re.compile(
    r"^(?:20\d{2}\s*年\s*)?(?:"
    r"(?:1[0-2]|[1-9])\s*[-—~至到]\s*(?:1[0-2]|[1-9])\s*月|"
    r"(?:1[0-2]|[1-9])\s*月|"
    r"全年|整年"
    r")"
    rf"([{_ENTITY_CHARS}]{{2,24}}?)"
    r"(?=的?(?:用电量|用水量|用气量|燃气|电费|水费|气费|热费|金额|用量|单价|费用|"
    r"变压器损耗|平均|合计|总计))"
)
#: 「二期15#专配室2026年9月…」——专配室/配电房地点优先抽出
_SWITCHROOM_ENTITY_RE = re.compile(
    r"([一二三四五六七八九十\d]+期\s*\d+\s*[#＃号]?\s*(?:专配室|配电房|配电室))"
)
#: 误抽成「2025年」/指标词时丢弃
_YEAR_LIKE_ENTITY_RE = re.compile(r"^20\d{2}年?$")
_ENTITY_METRIC_BLOCKLIST = frozenset(
    {
        "用电量",
        "用水量",
        "用气量",
        "电费",
        "水费",
        "气费",
        "热费",
        "金额",
        "用量",
        "单价",
        "费用",
        "平均",
        "合计",
        "总计",
        "的用电量",
        "的用水量",
        "的用气量",
        "的金额",
        "的用量",
        "的单价",
    }
)
#: 宽表常把「三期」写在 entity_key、「嘛嘛公寓」写在 entity_label；口语「三期嘛嘛公寓」
#: 必须拆成期号+名称两侧合取，否则 LIKE '%三期嘛嘛公寓%' 永远打不中。
_PHASE_ENTITY_RE = re.compile(
    r"^(?P<phase>[一二三四五六七八九十\d]+期)(?P<name>.+)$"
)
#: 月区间必须先于单月：否则「1-9月」会被 ``9月`` 吃成仅 9 月（钉钉错出 6 千万级假合计）
_PERIOD_RANGE_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<m1>1[0-2]|[1-9])\s*[-—~至到]\s*(?P<m2>1[0-2]|[1-9])\s*月"
)
_PERIOD_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>1[0-2]|[1-9])\s*月|"
    r"(?P<ymd>20\d{2}[-/](?:1[0-2]|0?[1-9])[-/](?:[0-3]?\d))|"
    r"(?P<ym>20\d{2}[-/](?:1[0-2]|0?[1-9]))"
)
_STATUS_RE = re.compile(r"状态\s*[=＝为是]\s*([^\s，,。；;]+)")


#: 模块运算类 op：出处是「输入依据集 + op」，不是单 chunk 印刷值
_AGGREGATE_OPS = frozenset({"sum", "count", "avg", "diff", "compare"})
#: 提取类：出处可挂到来源 chunk/doc
_EXTRACT_OPS = frozenset(
    {"lookup", "extract", "filter", "printed_total", "yoy_na"}
)
_AGGREGATE_SOURCES = frozenset(
    {"module_yoy", "rerank_table_chunks+sum", "observation_multi+sum"}
)


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

    @property
    def provenance_kind(self) -> str:
        """``extract``=表内现成数；``aggregate``=模块归纳/运算结果。"""
        source = str((self.extras or {}).get("source") or "")
        if self.op in _AGGREGATE_OPS or source in _AGGREGATE_SOURCES:
            return "aggregate"
        if (self.extras or {}).get("yoy_copied"):
            return "extract"
        if self.op in _EXTRACT_OPS:
            return "extract"
        # 多行 detail 且 op 像提取
        if self.detail and self.op in {"extract", "lookup", "filter"}:
            return "extract"
        return "aggregate" if self.rows_touched > 1 and self.op in _AGGREGATE_OPS else "extract"

    def provenance(self) -> dict[str, Any]:
        """出口出处：提取挂来源块；运算挂依据集 + op/参数。"""
        kind = self.provenance_kind
        payload: dict[str, Any] = {
            "kind": kind,
            "op": self.op,
            "input_chunk_ids": list(self.chunk_ids),
            "input_doc_ids": list(self.doc_ids),
            "rows_touched": self.rows_touched,
            "entity_key": self.entity_key,
            "period": self.period,
            "metric": self.metric,
        }
        extras = self.extras or {}
        for key in (
            "source",
            "current",
            "baseline",
            "baseline_period",
            "prefer_table_type",
            "period_pinned_from",
            "columns",
            "yoy_kind",
            "yoy_copied",
        ):
            if key in extras:
                payload[key] = extras[key]
        return payload

    def as_dict(self) -> dict[str, Any]:
        # verified=True：服务端白名单聚合/查找，不是 LLM 抄写；管理台勿再套「抄写校验」文案
        kind = self.provenance_kind
        if self.is_na:
            quote = ""
        elif kind == "aggregate":
            quote = (
                f"模块 {self.op}（依据 {self.rows_touched or len(self.chunk_ids)} 条输入）"
            )
        else:
            quote = self.surface
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
            "verified": not self.is_na,
            "indicator": self.metric or self.op,
            "scope": " ".join(
                p for p in (self.entity_key, self.period, self.metric) if p
            ).strip(),
            "quote": quote,
            "provenance": self.provenance(),
            **self.extras,
        }


#: 兼容旧名
StructuredAnswer = StructuredQueryHit


def is_document_stated_value_intent(question: str) -> bool:
    """方案/预算等「文中已写明」的取值问句：不应强制进抄表 SUM。

    中央空调改造方案把成本、年节约、回收期直接写在正文；若因「2026年+费用/电费+多少」
    被判成 numeric_calc，会扫观测表把别的电费账单 SUM 进来。
    真正的用量合计（「用电量合计/同比」）仍走计算模块。
    """
    text = (question or "").strip()
    if not text or not _STATED_VALUE_TOPIC_RE.search(text):
        return False
    if any(h in text for h in _STRONG_AGG_HINTS):
        metric = _metric_from_question(text)
        if metric in _METER_METRICS:
            return False
    return True


def wants_document_prose(question: str) -> bool:
    """问句还要方案/正文描述（改造方案、要点等），不能只回 KPI 数字行。"""
    text = (question or "").strip()
    return bool(text and _PROSE_ASK_RE.search(text))


def is_stated_value_extract_intent(question: str) -> bool:
    """纯数字取值才走 extract；夹带方案描述时交给 prose 读正文。"""
    if not is_document_stated_value_intent(question):
        return False
    return not wants_document_prose(question)


def is_numeric_calc_intent(question: str) -> bool:
    """是否数字计算类：一律走规划→计算模块，失败不散文照抄。"""
    text = (question or "").strip()
    if not text:
        return False
    # 方案正文已写明的成本/节约：走散文，禁止当观测 SUM
    if is_document_stated_value_intent(text):
        return False
    if any(h in text for h in _YOY_HINTS):
        return True
    if any(h in text for h in _AGG_HINTS):
        return True
    if "明细" in text and (
        _metric_from_question(text) or any(h in text for h in ("用电", "用水", "用气", "能耗"))
    ):
        return True
    if any(h in text for h in ("差异", "对比", "比较")) and (
        _metric_from_question(text)
        or any(h in text for h in ("电费", "水费", "气费", "用量", "金额"))
    ):
        return True
    # 期间 + 指标/用量类（如「2026年9月公共用电量」）
    # 裸「2026年 + 费用/金额」太弱：常是方案年份，不是抄表账期
    if _PERIOD_HINT_RE.search(text) and (
        _metric_from_question(text)
        or any(h in text for h in _NUMERIC_CALC_HINTS)
    ):
        metric = _metric_from_question(text)
        year_only = bool(_YEAR_ONLY_PERIOD_RE.search(text)) and not bool(
            _MONTH_IN_QUESTION_RE.search(text)
        )
        if year_only and metric in _WEAK_METRICS and not any(
            h in text for h in _STRONG_AGG_HINTS
        ):
            return False
        return not (year_only and metric is None and not any(h in text for h in ("用电", "用水", "用气", "能耗", "明细", "合计", "多少")))
    return bool(_metric_from_question(text) and re.search(r"(多少|合计|总计|累计|明细)", text))


def is_structured_query_intent(question: str) -> bool:
    text = (question or "").strip()
    if not text:
        return False
    # Word/方案类「文中已写明」：交给散文 LLM（父块+表叶数据齐全），不进旁路计算
    if is_document_stated_value_intent(text):
        return False
    if is_numeric_calc_intent(text):
        return True
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
    # 「用电量是多少」类：有白名单指标 + 多少，即可进结构化（缺实体仍会 underspecified）
    return bool(_metric_from_question(text) and re.search(r"(多少|合计|总计|累计)", text))


def resolve_structured_op(question: str) -> AggOp:
    text = (question or "").strip()
    # 方案/预算类：extract（模版槽位路径），禁止默认 sum 扫观测
    if is_document_stated_value_intent(text):
        return "extract"
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


def stated_value_extract_columns(question: str) -> list[str]:
    """方案类问句要投影的列（对应序列化表 KV 键）。"""
    text = question or ""
    cols: list[str] = []
    mapping = (
        (("预算", "改造成本", "成本"), "改造预算"),
        (("节约电费", "节约费用", "年节约"), "预计年节约电费"),
        (("回收期",), "投资回收期"),
        (("工期",), "计划工期"),
    )
    for hints, col in mapping:
        if any(h in text for h in hints) and col not in cols:
            cols.append(col)
    if not cols:
        cols = ["改造预算", "预计年节约电费", "投资回收期", "计划工期"]
    return cols


def _metric_from_question(question: str) -> str | None:
    """从问句提取白名单费项/指标名；未命中返回 None（不过滤 metric）。"""
    metrics = _metrics_from_question(question)
    return metrics[0] if metrics else None


def _metrics_from_question(question: str) -> list[str]:
    """问句中出现的全部白名单指标，按出现顺序（支持「用水量和用电量」）。"""
    text = question or ""
    found: list[str] = []
    for hint in _METRIC_HINTS:
        if hint in text and hint not in found:
            # 短词「用量」勿盖过已命中的「用电量/用水量」
            if hint == "用量" and any(h.endswith("用量") and h != "用量" for h in found):
                continue
            if any(hint != other and hint in other for other in found):
                continue
            found.append(hint)
    found.sort(key=lambda m: text.index(m))
    return found


def _quantity_vs_fee_sql(metric: str | None, params: list[Any]) -> str:
    """问「用电量/用水量」时排除「电费/水费」行，避免量费同 metric 被 SUM 双计。"""
    if metric == "用电量":
        return " AND entity_key NOT LIKE '%电费%' AND IFNULL(entity_label,'') NOT LIKE '%电费%'"
    if metric == "用水量":
        return " AND entity_key NOT LIKE '%水费%' AND IFNULL(entity_label,'') NOT LIKE '%水费%'"
    if metric in {"电费", "水费", "气费", "热费"}:
        # 问费时优先费行：不在此硬排除量行，由排序偏好处理
        return ""
    return ""


def _score_entity_for_metric(entity_key: str, entity_label: str, metric: str | None) -> int:
    """越小越优先。量问排费、费问排量。"""
    blob = f"{entity_key or ''}{entity_label or ''}"
    if metric == "用电量":
        if "电费" in blob:
            return 80
        if "电量" in blob:
            return 0
    if metric == "用水量":
        if "水费" in blob:
            return 80
        if "水量" in blob:
            return 0
    if metric == "电费" and "电量" in blob and "电费" not in blob:
        return 80
    if metric == "水费" and "水量" in blob and "水费" not in blob:
        return 80
    return 40


def _score_metric_column(row_metric: str, want_metric: str | None) -> int:
    """宽账单同行多指标（金额/用量/单价）时的列偏好；越小越优先。"""
    m = row_metric or ""
    want = (want_metric or "").strip()
    if want and (m == want or want in m):
        return 0
    fee_ask = (not want) or want in {"水费", "电费", "气费", "热费", "金额", "费用"} or (
        "费" in want and "用量" not in want and "电量" not in want and "水量" not in want
    )
    qty_ask = want in {"用电量", "用水量", "用气量", "用量"} or any(
        h in want for h in ("用电量", "用水量", "用量", "电量", "水量")
    )
    if fee_ask and not qty_ask:
        if "单价" in m:
            return 90
        if "金额" in m or ("费" in m and "用量" not in m):
            return 5
        if "用量" in m or "数量" in m:
            return 70
    if qty_ask:
        if "用量" in m or "数量" in m or "电量" in m:
            return 5
        if "金额" in m or ("费" in m and "用量" not in m):
            return 70
        if "单价" in m:
            return 90
    return 40


def _metric_sql_clause(metric: str | None, params: list[Any]) -> str:
    """指标过滤；口语「水费/电费」对齐宽表里的「金额(元)」列名。"""
    if not metric:
        return ""
    if metric in {"水费", "电费", "气费", "热费", "金额", "费用"}:
        params.extend([metric, f"%{metric}%", "%金额%", "%费用%"])
        return (
            " AND (metric = ? OR metric LIKE ? OR metric LIKE ? OR metric LIKE ?)"
            " AND metric NOT LIKE '%单价%'"
        )
    params.extend([metric, f"%{metric}%"])
    return " AND (metric = ? OR metric LIKE ?)"


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


def _next_month_ym(ym: str) -> str:
    """``YYYY-MM`` → 下一月（期间半开区间上界）。"""
    year = int(ym[:4])
    month = int(ym[5:7])
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def _period_from_question(question: str) -> str | None:
    text = question or ""
    range_match = _PERIOD_RANGE_RE.search(text)
    if range_match:
        m1 = int(range_match.group("m1"))
        m2 = int(range_match.group("m2"))
        if m1 > m2:
            m1, m2 = m2, m1
        year = range_match.group("year")
        if not year:
            # 「1-9月」无年：只返回月号区间，SQL 侧按任意年的这些月份滤
            return f"{m1:02d}..{m2:02d}"
        return f"{year}-{m1:02d}..{year}-{m2:02d}"
    match = _PERIOD_RE.search(text)
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


def _pin_month_only_period_to_latest_year(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    period: str | None,
    entity: str | None,
    metric: str | None,
    prefer_doc_ids: Sequence[int] | None = None,
    table_type: str | None = None,
) -> str | None:
    """无年份的「2月」不得把 2024/2025/2026 的 2 月一并 SUM。

    实测「三期嘛嘛公寓2月用电量是多少」：period=``02`` 命中三年三期行，
    SUM=59744+48948+29905=138597；资料里直接答案是最近一年 ``2026-02``=29905。
    有观测时钉到该实体/指标下最新 ``YYYY-MM``；钉不住则保持原 period。
    """
    if not period:
        return period
    raw = period.strip()
    if not re.fullmatch(r"\d{1,2}", raw):
        return period
    mm = f"{int(raw):02d}"
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += _entity_sql_clause(entity, params)
    where += _period_sql_clause(mm, params)
    if table_type:
        where += " AND table_type = ?"
        params.append(table_type)
    if metric:
        where += " AND (metric = ? OR metric LIKE ?)"
        params.extend([metric, f"%{metric}%"])
    row = conn.execute(
        f"SELECT period AS p FROM ekbs_table_observation WHERE {where} "
        f"AND period GLOB '20[0-9][0-9]-*' "
        f"ORDER BY period DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return period
    text = str(row["p"] if isinstance(row, sqlite3.Row) else row[0] or "")
    match = re.match(r"(20\d{2}-\d{2})", text)
    if not match:
        return period
    return match.group(1)


def _period_sql_clause(period: str | None, params: list[Any]) -> str:
    """期间模糊匹配：``2024-01`` 能打上观测里的 ``2024-01-01``；``A..B`` 为闭月开上界。"""
    if not period:
        return ""
    period = period.strip()
    # 年-月区间：2026-01..2026-09 → [2026-01, 2026-10)
    if re.fullmatch(r"20\d{2}-\d{2}\.\.20\d{2}-\d{2}", period):
        start, end = period.split("..", 1)
        upper = _next_month_ym(end)
        params.extend([start, upper])
        return " AND period >= ? AND period < ?"
    # 仅月号区间：01..09（跨任意年）
    if re.fullmatch(r"\d{2}\.\.\d{2}", period):
        start_m, end_m = (int(x) for x in period.split("..", 1))
        likes: list[str] = []
        for month in range(start_m, end_m + 1):
            likes.append(f"%-{month:02d}")
            likes.append(f"%-{month:02d}-%")
        params.extend(likes)
        clause = " OR ".join(["period LIKE ?"] * len(likes))
        return f" AND ({clause})"
    # 完整日
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", period):
        params.append(period)
        return " AND period = ?"
    # 年-月；空 period 的设备表靠文档标题带账期（如「2026年9月能耗分析」）
    if re.fullmatch(r"20\d{2}-\d{2}", period):
        year, month = period.split("-", 1)
        title_ym = f"%{year}年{int(month)}月%"
        title_iso = f"%{period}%"
        params.extend([period, f"{period}-%", title_ym, title_iso])
        return (
            " AND ("
            "period = ? OR period LIKE ? OR ("
            "IFNULL(period, '') = '' AND EXISTS ("
            "SELECT 1 FROM ekbs_document d "
            "WHERE d.doc_id = ekbs_table_observation.doc_id "
            "AND (d.title LIKE ? OR d.title LIKE ?)"
            ")"
            ")"
            ")"
        )
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


def normalize_entity_query(entity: str | None) -> str | None:
    """口语常带尾巴「值」：表里是「变压器损耗」，问句却是「变压器损耗值」。"""
    text = (entity or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"值$", "", text).strip()
    if len(cleaned) >= 2:
        return cleaned
    return text


_PHASE_DIGIT_TO_CN = {
    "1": "一",
    "2": "二",
    "3": "三",
    "4": "四",
    "5": "五",
    "6": "六",
    "7": "七",
    "8": "八",
    "9": "九",
    "10": "十",
}
_ROOM_NAME_ALTS = (
    ("配电房", "专配室"),
    ("专配室", "配电房"),
    ("配电房", "配电室"),
    ("配电室", "专配室"),
    ("专配室", "配电室"),
    ("配电室", "配电房"),
)


def _location_surface_variants(text: str) -> list[str]:
    """口语地点 ↔ 表内写法：1期↔一期、4号↔4#、配电房↔专配室。"""
    seeds = [text]
    m = re.match(r"^(\d{1,2}|[一二三四五六七八九十]+)期(.*)$", text)
    if m:
        phase, rest = m.group(1), m.group(2)
        if phase.isdigit():
            cn = _PHASE_DIGIT_TO_CN.get(str(int(phase)))
            if cn:
                seeds.append(f"{cn}期{rest}")
        else:
            for digit, cn in _PHASE_DIGIT_TO_CN.items():
                if phase == cn:
                    seeds.append(f"{digit}期{rest}")
                    break
    expanded: list[str] = []
    for seed in seeds:
        expanded.append(seed)
        hashish = seed.replace("号", "#").replace("＃", "#")
        numbered = seed.replace("#", "号").replace("＃", "号")
        for item in (hashish, numbered):
            if item not in expanded:
                expanded.append(item)
    out: list[str] = []
    for item in expanded:
        if item not in out:
            out.append(item)
        for src, dst in _ROOM_NAME_ALTS:
            if src in item:
                alt = item.replace(src, dst, 1)
                if alt not in out:
                    out.append(alt)
    return out


def entity_match_variants(entity: str | None) -> list[str]:
    """匹配用实体变体（保序去重）。"""
    text = (entity or "").strip()
    if not text:
        return []
    out: list[str] = []
    for candidate in (text, normalize_entity_query(text)):
        if not candidate:
            continue
        for variant in (candidate, *_location_surface_variants(candidate)):
            if variant and variant not in out:
                out.append(variant)
    return out


def _attr_loss_value_sql() -> str:
    """损耗写在 attrs.损耗、value 为空时（变压器损耗表）仍可聚合。"""
    raw = "json_extract(attrs_json, '$.损耗')"
    return (
        "CASE "
        f"WHEN value IS NOT NULL THEN value "
        f"WHEN typeof({raw}) IN ('integer', 'real') THEN CAST({raw} AS REAL) "
        f"WHEN {raw} GLOB '[-+0-9]*' OR {raw} GLOB '[-+0-9]*.[0-9]*' "
        f"THEN CAST({raw} AS REAL) "
        "ELSE NULL END"
    )


def _is_equipment_unit_count(question: str) -> bool:
    """问「多少台/几台」时按设备台数计，禁止 COUNT(*) 观测行。"""
    return bool(_UNIT_COUNT_RE.search(question or ""))


def _is_switchroom_location_entity(entity: str | None) -> bool:
    """是否点名了专配室/配电房（损耗合计必须带地点，不能只带指标名）。"""
    text = (entity or "").strip()
    if not text:
        return False
    if _SWITCHROOM_ENTITY_RE.search(text):
        return True
    return any(token in text for token in ("专配室", "配电房", "配电室"))


def _entity_from_question(question: str) -> str | None:
    text = (question or "").strip()
    match = _ENTITY_RE.search(text)
    if match:
        return normalize_entity_query(match.group(1).strip())
    quoted = re.search(r"[「\"“']([^」\"”']+)[」\"”']", text)
    if quoted:
        return normalize_entity_query(quoted.group(1).strip())
    def _ok_entity(name: str) -> bool:
        if not name or name in {"请问", "帮忙", "查询", "告诉我", "我想问"}:
            return False
        if _YEAR_LIKE_ENTITY_RE.fullmatch(name):
            return False
        return not (name in _ENTITY_METRIC_BLOCKLIST or name.lstrip("的") in _ENTITY_METRIC_BLOCKLIST)

    # 「一共有多少台变压器」优先于 bare：否则 entity=None 会扫全库行数
    unit = _UNIT_COUNT_ENTITY_RE.search(text)
    if unit:
        name = normalize_entity_query((unit.group(1) or unit.group(2) or "").strip())
        if (
            name
            and name not in _UNIT_COUNT_ENTITY_STOP
            and _ok_entity(name)
        ):
            return name

    # 「二期15#专配室」含 #，必须专用抽取，避免 bare 字符集漏配后全表 SUM
    switchroom = _SWITCHROOM_ENTITY_RE.search(text)
    if switchroom:
        name = normalize_entity_query(re.sub(r"\s+", "", switchroom.group(1)))
        if name and _ok_entity(name):
            return name

    # 问句以账期开头时优先：避免「2025年1-3月锅炉…」被 bare 抽成「2025年」
    period_then = _PERIOD_THEN_ENTITY_RE.match(text)
    if period_then:
        name = normalize_entity_query(period_then.group(1).strip())
        if name and _ok_entity(name):
            return name
    bare = _BARE_ENTITY_RE.match(text)
    if bare:
        name = normalize_entity_query(bare.group(1).strip())
        if name and _ok_entity(name):
            return name
    return None


def try_structured_query(
    conn: sqlite3.Connection,
    question: str,
    perm: PermContext,
    *,
    prefer_doc_ids: Sequence[int] | None = None,
    require_prefer_docs: bool = False,
) -> StructuredQueryHit | None:
    """尝试受限结构化问答；不适用时返回 ``None``（调用方降级）。"""
    if not is_structured_query_intent(question):
        return None
    op = resolve_structured_op(question)
    entity = _entity_from_question(question)
    period = _period_from_question(question)
    metric = _metric_from_question(question)
    attrs_contains: str | None = None
    # 宽账单未点名指标时默认「金额」，避免把用量一并 SUM 进去
    if metric is None and re.search(r"(水卡号|水卡|卡号)", question or ""):
        metric = "用量" if "用量" in (question or "") else "金额"
    # 「差异电费/差异水费」→ 宽表行名是「差异」+ 金额列；用电/用水靠 caption 消歧
    diff = re.search(r"差异\s*(电费|水费|电量|水量)", question or "")
    if diff:
        entity = "差异"
        kind = diff.group(1)
        metric = "金额（元）" if kind.endswith("费") else "用量"
        attrs_contains = "用电" if "电" in kind else "用水"
        # 单月 lookup；区间（1-2月）必须 sum，否则 attrs 消歧后仍只取一行
        op = "sum" if period and ".." in period else "lookup"
    status = None
    status_match = _STATUS_RE.search(question or "")
    if status_match:
        status = status_match.group(1).strip()
    columns = (
        stated_value_extract_columns(question)
        if is_stated_value_extract_intent(question)
        else None
    )
    return execute_structured_plan(
        conn,
        perm,
        op=op,
        entity=entity,
        period=period,
        metric=metric,
        status=status,
        attrs_contains=attrs_contains,
        columns=columns,
        question=question,
        prefer_doc_ids=prefer_doc_ids,
        require_prefer_docs=require_prefer_docs,
    )


def execute_structured_plan(  # noqa: C901 — 多 op 分派 + 观测/块双源
    conn: sqlite3.Connection,
    perm: PermContext,
    *,
    op: AggOp | str,
    entity: str | None = None,
    period: str | None = None,
    metric: str | None = None,
    status: str | None = None,
    attrs_contains: str | None = None,
    columns: Sequence[str] | None = None,
    question: str = "",
    prefer_doc_ids: Sequence[int] | None = None,
    require_prefer_docs: bool = False,
    table_chunks: Sequence[Any] | None = None,
) -> StructuredQueryHit | None:
    """按显式指令执行：观测表优先；可按精排块兜底提取。

    ``require_prefer_docs=True`` 时必须带精排 doc 范围，禁止扫全库。
    """
    shards = tuple(visible_observation_shard_ids(perm))
    if not shards:
        log.info("structured_query_denied_no_shard", user_id=perm.user_id)
        return StructuredQueryHit(
            value=None,
            op=str(op or "count"),
            reason="no_visible_shard",
            surface="N/A",
        )
    entity = normalize_entity_query(entity) or entity

    if require_prefer_docs and not prefer_doc_ids:
        return StructuredQueryHit(
            value=None,
            op=str(op or "extract"),
            reason="no_scope_docs",
            surface="N/A",
        )

    op_name = str(op or "").strip().lower()
    # 问句含多个指标（用水量和用电量）：逐指标提取后合并，禁止只答其中一个或乱 SUM
    multi_metrics = _metrics_from_question(question) if question else []
    if len(multi_metrics) > 1 and op_name in {
        "extract",
        "lookup",
        "sum",
        "filter",
        "",
    }:
        # 多指标时忽略规划器的单一 attrs（如只写「用水」会把用电量滤光）
        merged = _extract_multi_metrics(
            conn,
            shards,
            metrics=multi_metrics,
            entity=entity,
            period=period,
            attrs_contains=None,
            prefer_doc_ids=prefer_doc_ids,
            table_chunks=table_chunks,
            question=question,
        )
        if merged is not None:
            # 即使部分/全部未命中也返回 extract，禁止继续掉进单指标 SUM（量费双计）
            return merged

    # 四类主 op 别名
    if op_name == "extract":
        op_name = "lookup"
    if op_name == "diff":
        # 差异行优先当 extract；算不出来再由调用方用块提取
        op_name = "lookup"
        if not entity:
            entity = "差异"

    if op_name == "yoy_na":
        copied = _try_copy_yoy_column(conn, question, shards, prefer_doc_ids)
        if copied is not None:
            return copied
        # 文档无印刷同比：尝试用两期基数由模块算比率（需 table_chunks 或观测多期）
        ratio_hit = _module_yoy_from_observations(
            conn,
            shards,
            entity=entity,
            metric=metric,
            period=period,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
        )
        if ratio_hit is not None:
            return ratio_hit
        return StructuredQueryHit(
            value=None,
            op="yoy_na",
            reason="yoy_column_missing",
            surface="N/A",
        )

    if op_name == "compare":
        hit = _compare_or_extract(
            conn,
            shards,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
            question=question,
        )
        if hit is not None and not hit.is_na:
            return hit
        return _extract_fallback_from_chunks(
            table_chunks,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            columns=columns,
            prefer_doc_ids=prefer_doc_ids,
            question=question,
        )

    if op_name == "lookup":
        value_hit = _value_lookup(
            conn,
            shards,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
        )
        if value_hit is not None and not value_hit.is_na:
            return value_hit
        row_hit = _lookup_row(
            conn, shards, entity=entity, question=question, prefer_doc_ids=prefer_doc_ids
        )
        if row_hit is not None and not row_hit.is_na:
            return row_hit
        # 明细/多行：观测多行匹配
        multi = _extract_observation_rows(
            conn,
            shards,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
        )
        if multi is not None and not multi.is_na:
            return multi
        return _extract_fallback_from_chunks(
            table_chunks,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            columns=columns,
            prefer_doc_ids=prefer_doc_ids,
            question=question,
        )
    if op_name == "filter":
        hit = _filter_rows(conn, shards, status=status, prefer_doc_ids=prefer_doc_ids)
        if hit is not None and not hit.is_na and hit.rows_touched:
            return hit
        return _extract_fallback_from_chunks(
            table_chunks,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            columns=columns,
            prefer_doc_ids=prefer_doc_ids,
            question=question,
        )
    if op_name == "printed_total":
        printed = _printed_total(
            conn, shards, question=question, prefer_doc_ids=prefer_doc_ids
        )
        if printed is not None:
            return printed
        op_name = "sum"

    if op_name not in {"sum", "count", "avg"}:
        return None

    # 出口管控：SUM/AVG 既无实体又无指标 → 会扫进整库观测，禁止出数
    if op_name in {"sum", "avg"} and not entity and not metric:
        log.info(
            "structured_query_underspecified",
            op=op_name,
            question_len=len(question or ""),
            user_id=perm.user_id,
        )
        return StructuredQueryHit(
            value=None,
            op=op_name,
            reason="underspecified",
            surface="N/A",
            period=period or "",
        )
    # 变压器损耗无专配室/配电房地点 → 禁止全表 SUM（曾错出 182129.6）
    if (
        op_name in {"sum", "avg"}
        and metric
        and "损耗" in metric
        and not _is_switchroom_location_entity(entity)
    ):
        log.info(
            "structured_query_underspecified",
            op=op_name,
            reason="loss_metric_no_entity",
            question_len=len(question or ""),
            user_id=perm.user_id,
        )
        return StructuredQueryHit(
            value=None,
            op=op_name,
            reason="underspecified",
            surface="N/A",
            metric=metric or "",
            period=period or "",
        )
    # 「多少台」无设备名 → 禁止把整库观测行数当成台数（曾错出 5787）
    if op_name == "count" and _is_equipment_unit_count(question) and not entity:
        log.info(
            "structured_query_underspecified",
            op=op_name,
            reason="equipment_unit_no_entity",
            question_len=len(question or ""),
            user_id=perm.user_id,
        )
        return StructuredQueryHit(
            value=None,
            op=op_name,
            reason="underspecified",
            surface="N/A",
            period=period or "",
        )

    agg = _aggregate(
        conn,
        shards,
        op=op_name,  # type: ignore[arg-type]
        entity=entity,
        period=period,
        metric=metric,
        attrs_contains=attrs_contains,
        prefer_doc_ids=prefer_doc_ids,
        prefer_doc_strict=require_prefer_docs,
        question=question,
    )
    if agg is not None and not agg.is_na:
        return agg
    if op_name == "sum" and table_chunks:
        from ekbs.retrieval.table_extract import extract_from_table_chunks, module_sum

        default_cols = (
            ["安装地点", "损耗"]
            if metric and "损耗" in metric
            else ["项目", "用量"]
        )
        extracted = extract_from_table_chunks(
            table_chunks,
            entity=entity,
            period=period,
            metric=metric,
            attrs_contains=attrs_contains,
            columns=list(columns or default_cols),
            prefer_doc_ids=prefer_doc_ids,
        )
        if extracted.is_na or not extracted.detail:
            return extracted if extracted is not None else agg
        values = [
            float(v)
            for row in extracted.detail
            for v in row.values()
            if isinstance(v, (int, float))
        ]
        if not values:
            return extracted
        total, surface = module_sum(values)
        return StructuredQueryHit(
            value=total,
            surface=surface,
            op="sum",
            rows_touched=len(values),
            chunk_ids=extracted.chunk_ids,
            doc_ids=extracted.doc_ids,
            period=period or "",
            metric=metric or "",
            entity_key=entity or "",
            detail=extracted.detail,
            extras={"source": "rerank_table_chunks+sum"},
        )
    return agg


def _value_lookup(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    entity: str | None,
    period: str | None,
    metric: str | None,
    attrs_contains: str | None,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit | None:
    """非台账表的单值查找（如「差异 / 2026-02 / 金额」）。"""
    if not entity and not metric and not period:
        return None
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += " AND table_type IN ('A','B','C','E')"
    where += _entity_sql_clause(entity, params)
    where += _period_sql_clause(period, params)
    where += _metric_sql_clause(metric, params)
    if attrs_contains:
        where += " AND attrs_json LIKE ?"
        params.append(f"%{attrs_contains}%")
    where += _quantity_vs_fee_sql(metric, params)
    rows = conn.execute(
        f"SELECT value, value_surface, metric, period, entity_key, entity_label, "
        f"chunk_id, doc_id, table_type "
        f"FROM ekbs_table_observation WHERE {where} "
        f"ORDER BY CASE WHEN metric = ? THEN 0 ELSE 1 END, period, obs_id LIMIT 8",
        [*params, metric or ""],
    ).fetchall()
    if not rows:
        # 未命中 A–C/E 时返回 None，交给 _lookup_row 处理台账 D 型
        return None
    # 同期间多行：按量/费与实体名消歧，取最优一行（避免电量+电费双行 ambiguous）
    multi_period = len({str(r["period"] or "") for r in rows}) > 1
    if len(rows) > 1 and not multi_period:
        ranked = sorted(
            rows,
            key=lambda r: (
                _score_entity_for_metric(
                    str(r["entity_key"] or ""),
                    str(r["entity_label"] or ""),
                    metric,
                ),
                _score_metric_column(str(r["metric"] or ""), metric),
                0 if str(r["metric"] or "") == (metric or "") else 1,
            ),
        )
        best = ranked[0]
        best_score = (
            _score_entity_for_metric(
                str(best["entity_key"] or ""), str(best["entity_label"] or ""), metric
            ),
            _score_metric_column(str(best["metric"] or ""), metric),
        )
        second = ranked[1] if len(ranked) > 1 else None
        second_score = (
            (
                _score_entity_for_metric(
                    str(second["entity_key"] or ""),
                    str(second["entity_label"] or ""),
                    metric,
                ),
                _score_metric_column(str(second["metric"] or ""), metric),
            )
            if second is not None
            else (999, 999)
        )
        if best_score < second_score or (attrs_contains and best_score[0] <= 40):
            rows = [best]
    if len(rows) > 1 and (not attrs_contains or multi_period):
        surfaces = [str(r["value_surface"] or r["value"]) for r in rows[:3]]
        return StructuredQueryHit(
            value=None,
            op="lookup",
            reason="ambiguous",
            surface="N/A",
            period=period or "",
            metric=metric or "",
            entity_key=entity or "",
            detail=[{"surface": s} for s in surfaces],
        )
    row = rows[0]
    surface = str(row["value_surface"] or row["value"] or "")
    return StructuredQueryHit(
        value=row["value"],
        surface=surface,
        op="lookup",
        rows_touched=1,
        chunk_ids=(int(row["chunk_id"]),) if row["chunk_id"] is not None else (),
        doc_ids=(int(row["doc_id"]),),
        table_type=str(row["table_type"] or ""),
        metric=str(row["metric"] or ""),
        period=str(row["period"] or ""),
        entity_key=str(row["entity_key"] or ""),
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


def _entity_sql_clause(entity: str | None, params: list[Any]) -> str:
    """实体过滤：等值 / 模糊，并对「三期嘛嘛公寓」做期号+名称跨列合取。

    宽表常见 key=``三期3幢表号…``、label=``嘛嘛公寓``；整串 LIKE 打不中。
    水费宽账单：entity_key 是水卡号，「1期绿化总水」在 attrs.用途 —— 必须扫 attrs_json。
    """
    variants = entity_match_variants(entity)
    if not variants:
        return ""
    parts: list[str] = []
    for variant in variants:
        # metric 也要扫：设备台数问「变压器」时，行上是 metric=变压器损耗、attrs 无「变压器」字样
        parts.append(
            "(entity_key = ? OR entity_label = ? "
            "OR entity_key LIKE ? OR entity_label LIKE ? "
            "OR attrs_json LIKE ? OR metric LIKE ?)"
        )
        params.extend(
            [
                variant,
                variant,
                f"%{variant}%",
                f"%{variant}%",
                f"%{variant}%",
                f"%{variant}%",
            ]
        )
        phase_match = _PHASE_ENTITY_RE.match(variant)
        if phase_match:
            phase = phase_match.group("phase")
            name = (phase_match.group("name") or "").strip()
            if len(name) >= 2:
                parts.append(
                    "("
                    "(entity_key LIKE ? OR entity_label LIKE ? OR attrs_json LIKE ?) "
                    "AND (entity_key LIKE ? OR entity_label LIKE ? OR attrs_json LIKE ?)"
                    ")"
                )
                params.extend(
                    [
                        f"%{phase}%",
                        f"%{phase}%",
                        f"%{phase}%",
                        f"%{name}%",
                        f"%{name}%",
                        f"%{name}%",
                    ]
                )
    return " AND (" + " OR ".join(parts) + ")"


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
    attrs_contains: str | None = None,
    prefer_doc_ids: Sequence[int] | None,
    prefer_doc_strict: bool = False,
    question: str = "",
) -> StructuredQueryHit:
    """白名单等值过滤：``shard_id``（ACL）+ 可选 doc / entity / period / metric / attrs。

    只允许固定 ``SUM|COUNT|AVG(value)``，禁止任意表达式。

    ``prefer_doc_ids`` 默认是软偏好：先在这些文档里找；无行时回退到可见分片全库
    观测（仍过 ACL）。否则「9 月能耗分析」进上下文时，1–9 月合计会被错误锁死在
    无观测明细的文档上，降级成 prose 拒答。

    ``prefer_doc_strict=True``（数字计算强制路径）：禁止放宽到其它文档，避免
    「改造方案成本」之类问句在方案 doc 无观测时把电费账单 SUM 进来。

    用电量/用水量：优先 ``table_type=B``（户内抄表明细按费用承担对象），避免与
    大户监视（A）同月双计；B 无行再回退 A/全类型。

    问句只有「2月」无年份时，先钉到该实体/指标最新 ``YYYY-MM``，禁止跨年 SUM。

    ``attrs_contains`` 必须下沉到 SQL（如「差异电费」vs「差异水费」），不能先 SUM
    再靠单点 lookup 补救——区间账期时 lookup 对不上 ``YYYY-MM..YYYY-MM``。
    """
    type_order: list[str | None] = [None]
    if metric in {"用电量", "用水量"} and op == "sum":
        type_order = ["B", None]

    hit: StructuredQueryHit | None = None
    for table_type in type_order:
        pinned = _pin_month_only_period_to_latest_year(
            conn,
            shards,
            period=period,
            entity=entity,
            metric=metric,
            prefer_doc_ids=prefer_doc_ids,
            table_type=table_type,
        )
        hit = _aggregate_once(
            conn,
            shards,
            op=op,
            entity=entity,
            period=pinned,
            metric=metric,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
            table_type=table_type,
            question=question,
        )
        if not hit.is_na:
            if table_type == "B":
                hit.extras = {**(hit.extras or {}), "prefer_table_type": "B"}
            if pinned != period:
                hit.extras = {
                    **(hit.extras or {}),
                    "period_pinned_from": period or "",
                }
            break
        if prefer_doc_ids and not prefer_doc_strict:
            pinned_r = _pin_month_only_period_to_latest_year(
                conn,
                shards,
                period=period,
                entity=entity,
                metric=metric,
                prefer_doc_ids=None,
                table_type=table_type,
            )
            relaxed = _aggregate_once(
                conn,
                shards,
                op=op,
                entity=entity,
                period=pinned_r,
                metric=metric,
                attrs_contains=attrs_contains,
                prefer_doc_ids=None,
                table_type=table_type,
                question=question,
            )
            if not relaxed.is_na:
                relaxed.extras = {
                    **(relaxed.extras or {}),
                    "prefer_doc_relaxed": True,
                    **({"prefer_table_type": "B"} if table_type == "B" else {}),
                    **(
                        {"period_pinned_from": period or ""}
                        if pinned_r != period
                        else {}
                    ),
                }
                log.info(
                    "structured_query_prefer_doc_relaxed",
                    op=op,
                    prefer_docs=list(prefer_doc_ids),
                    rows=relaxed.rows_touched,
                    table_type=table_type,
                )
                return relaxed
    assert hit is not None
    return hit


def _aggregate_once(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    op: Literal["sum", "count", "avg"],
    entity: str | None,
    period: str | None,
    metric: str | None = None,
    attrs_contains: str | None = None,
    prefer_doc_ids: Sequence[int] | None,
    table_type: str | None = None,
    question: str = "",
) -> StructuredQueryHit:
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += _entity_sql_clause(entity, params)
    where += _period_sql_clause(period, params)
    if table_type:
        where += " AND table_type = ?"
        params.append(table_type)
    where += _metric_sql_clause(metric, params)
    where += _quantity_vs_fee_sql(metric, params)
    if attrs_contains:
        where += " AND attrs_json LIKE ?"
        params.append(f"%{attrs_contains}%")

    if op == "count":
        # 台数：按设备身份去重（优先 attrs.序号，否则 entity_key），勿 COUNT(*) 损耗明细行
        if _is_equipment_unit_count(question):
            unit_expr = (
                "CASE "
                "WHEN IFNULL(json_extract(attrs_json, '$.序号'), '') != '' "
                "THEN json_extract(attrs_json, '$.序号') "
                "WHEN IFNULL(entity_key, '') != '' THEN entity_key "
                "ELSE NULL END"
            )
            sql = (
                f"SELECT COUNT(DISTINCT {unit_expr}) AS v "
                f"FROM ekbs_table_observation WHERE {where}"
            )
        else:
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

    value_expr = (
        _attr_loss_value_sql()
        if metric and "损耗" in metric
        else "value"
    )
    sql = (
        f"SELECT {'AVG' if op == 'avg' else 'SUM'}({value_expr}) AS v, COUNT(*) AS n, "
        f"GROUP_CONCAT(DISTINCT chunk_id) AS cids, "
        f"GROUP_CONCAT(DISTINCT doc_id) AS docs "
        f"FROM ekbs_table_observation WHERE {where} AND ({value_expr}) IS NOT NULL"
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


def _entity_stem_for_metrics(entity: str | None, metrics: Sequence[str]) -> str | None:
    """多指标时去掉实体尾部的指标词，避免「公司承担用电量」挡住用水量。"""
    if not entity:
        return None
    stem = entity.strip()
    suffixes = sorted(
        {
            *metrics,
            "用电量",
            "用水量",
            "用气量",
            "电费",
            "水费",
            "气费",
            "热费",
            "电量",
            "水量",
            "气量",
        },
        key=len,
        reverse=True,
    )
    changed = True
    while changed and stem:
        changed = False
        for suffix in suffixes:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)].rstrip("的 ·_-")
                changed = True
                break
    return stem or entity


def _extract_multi_metrics(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    metrics: Sequence[str],
    entity: str | None,
    period: str | None,
    attrs_contains: str | None,
    prefer_doc_ids: Sequence[int] | None,
    table_chunks: Sequence[Any] | None,
    question: str,
) -> StructuredQueryHit | None:
    """多指标各提一行，合并为 extract detail（不做跨指标 SUM）。"""
    stem = _entity_stem_for_metrics(entity, metrics) or _entity_from_question(question)
    detail: list[dict[str, Any]] = []
    chunk_ids: list[int] = []
    doc_ids: list[int] = []
    for metric in metrics:
        # 先精确行名「公司承担用电量」，再退回主体「公司承担」
        entity_candidates: list[str | None] = []
        if stem:
            entity_candidates.append(f"{stem}{metric}")
            short = {"用电量": "电量", "用水量": "水量", "用气量": "气量"}.get(metric)
            if short:
                entity_candidates.append(f"{stem}{short}")
            entity_candidates.append(stem)
        if entity and entity not in entity_candidates:
            entity_candidates.append(entity)
        if not entity_candidates:
            entity_candidates.append(None)

        hit: StructuredQueryHit | None = None
        for ent in entity_candidates:
            hit = _value_lookup(
                conn,
                shards,
                entity=ent,
                period=period,
                metric=metric,
                attrs_contains=attrs_contains,
                prefer_doc_ids=prefer_doc_ids,
            )
            if hit is not None and not hit.is_na:
                break
            if prefer_doc_ids:
                hit = _value_lookup(
                    conn,
                    shards,
                    entity=ent,
                    period=period,
                    metric=metric,
                    attrs_contains=attrs_contains,
                    prefer_doc_ids=None,
                )
                if hit is not None and not hit.is_na:
                    break
        if hit is None or hit.is_na:
            hit = _extract_fallback_from_chunks(
                table_chunks,
                entity=stem or entity,
                period=period,
                metric=metric,
                attrs_contains=attrs_contains,
                columns=["项目", "用量"],
                prefer_doc_ids=prefer_doc_ids,
                question=question,
            )
        if hit is None or hit.is_na:
            detail.append({"指标": metric, "用量": "—", "status": "not_found"})
            continue
        detail.append(
            {
                "指标": metric,
                "项目": hit.entity_key or stem or entity or metric,
                "用量": hit.value,
                "period": hit.period or period or "",
            }
        )
        chunk_ids.extend(hit.chunk_ids)
        doc_ids.extend(hit.doc_ids)

    ok = [d for d in detail if d.get("用量") not in {None, "—", "N/A"}]
    if not ok:
        return StructuredQueryHit(
            value=None, op="extract", reason="not_found", surface="N/A"
        )
    parts = [f"{d['指标']}={d['用量']}" for d in ok]
    return StructuredQueryHit(
        value=len(ok),
        surface="；".join(parts),
        op="extract",
        rows_touched=len(ok),
        chunk_ids=tuple(dict.fromkeys(chunk_ids)),
        doc_ids=tuple(sorted(set(int(d) for d in doc_ids))),
        period=period or "",
        entity_key=stem or entity or "",
        detail=detail,
        extras={"source": "multi_metric_extract", "metrics": list(metrics)},
    )


def _extract_fallback_from_chunks(
    table_chunks: Sequence[Any] | None,
    *,
    entity: str | None,
    period: str | None,
    metric: str | None,
    attrs_contains: str | None,
    columns: Sequence[str] | None,
    prefer_doc_ids: Sequence[int] | None,
    question: str = "",
) -> StructuredQueryHit:
    from ekbs.retrieval.table_extract import extract_from_table_chunks

    if not table_chunks:
        return StructuredQueryHit(
            value=None, op="extract", reason="not_found", surface="N/A"
        )
    entity = normalize_entity_query(entity) or entity or _entity_from_question(question)
    period = period or _period_from_question(question)
    metric = metric or _metric_from_question(question)
    cols = list(columns) if columns else ["项目", "用量"]
    # 规划常把列名误塞进 metric（如「每月损耗」），会滤掉用电分析「9用量」行
    metric_is_column = bool(metric) and (
        metric in cols or any(metric == c or metric in c or c in metric for c in cols)
    )
    use_metric = None if metric_is_column else metric
    hit = extract_from_table_chunks(
        table_chunks,
        entity=entity,
        period=period,
        metric=use_metric,
        attrs_contains=attrs_contains,
        columns=cols,
        prefer_doc_ids=prefer_doc_ids,
    )
    # 规划列常指到理论损耗表（安装地点/每月损耗）；回退用电分析「项目+用量」
    if hit.is_na and cols != ["项目", "用量"]:
        retry = extract_from_table_chunks(
            table_chunks,
            entity=entity,
            period=period,
            metric=None,
            attrs_contains=None,
            columns=["项目", "用量"],
            prefer_doc_ids=prefer_doc_ids,
        )
        if not retry.is_na:
            return retry
    return hit


def _extract_observation_rows(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    entity: str | None,
    period: str | None,
    metric: str | None,
    attrs_contains: str | None,
    prefer_doc_ids: Sequence[int] | None,
    limit: int = 80,
) -> StructuredQueryHit | None:
    """观测表多行提取（明细）；少于 2 行时返回 None 以继续块兜底。"""
    where, params = _shard_clause(shards)
    where += _doc_clause(prefer_doc_ids, params)
    where += _period_sql_clause(period, params)
    where += _entity_sql_clause(entity, params)
    where += _metric_sql_clause(metric, params)
    if attrs_contains:
        where += " AND attrs_json LIKE ?"
        params.append(f"%{attrs_contains}%")
    rows = conn.execute(
        f"SELECT entity_key, entity_label, period, metric, value, value_surface, "
        f"attrs_json, chunk_id, doc_id FROM ekbs_table_observation "
        f"WHERE {where} AND value IS NOT NULL LIMIT ?",
        [*params, limit],
    ).fetchall()
    if len(rows) < 2:
        return None
    detail: list[dict[str, Any]] = []
    chunk_ids: list[int] = []
    doc_ids: list[int] = []
    for row in rows:
        label = row["entity_label"] or row["entity_key"]
        try:
            attrs = json.loads(row["attrs_json"] or "{}")
        except json.JSONDecodeError:
            attrs = {}
        usage = str(attrs.get("用途") or "").strip()
        if usage:
            label = usage
        item: dict[str, Any] = {
            "项目": label,
            "用量": float(row["value"]) if row["value"] is not None else None,
            "metric": row["metric"],
            "period": row["period"],
        }
        detail.append(item)
        if row["chunk_id"] is not None:
            chunk_ids.append(int(row["chunk_id"]))
        doc_ids.append(int(row["doc_id"]))
    parts = [
        f"{d['项目']}={d['用量']}" for d in detail if d.get("用量") is not None
    ]
    return StructuredQueryHit(
        value=len(detail),
        surface="；".join(parts[:40]),
        op="extract",
        rows_touched=len(detail),
        chunk_ids=tuple(dict.fromkeys(chunk_ids)),
        doc_ids=tuple(sorted(set(doc_ids))),
        period=period or "",
        metric=metric or "",
        entity_key=entity or "",
        detail=detail,
        extras={"source": "observation_multi"},
    )


def _compare_or_extract(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    entity: str | None,
    period: str | None,
    metric: str | None,
    attrs_contains: str | None,
    prefer_doc_ids: Sequence[int] | None,
    question: str,
) -> StructuredQueryHit | None:
    """对比：先尽力抽出相关行，surface 列出供表述。"""
    multi = _extract_observation_rows(
        conn,
        shards,
        entity=entity,
        period=period,
        metric=metric,
        attrs_contains=attrs_contains,
        prefer_doc_ids=prefer_doc_ids,
    )
    if multi is not None:
        multi.op = "compare"
        return multi
    value_hit = _value_lookup(
        conn,
        shards,
        entity=entity,
        period=period,
        metric=metric,
        attrs_contains=attrs_contains,
        prefer_doc_ids=prefer_doc_ids,
    )
    if value_hit is not None and not value_hit.is_na:
        value_hit.op = "compare"
        return value_hit
    return None


def _module_yoy_from_observations(
    conn: sqlite3.Connection,
    shards: Sequence[str],
    *,
    entity: str | None,
    metric: str | None,
    period: str | None,
    attrs_contains: str | None,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit | None:
    """无印刷同比时：取本期与去年同期（或上月）由模块算比率。"""
    from ekbs.retrieval.table_extract import module_diff

    if not period or ".." in period:
        return None
    # 仅单月 YYYY-MM
    if not re.fullmatch(r"20\d{2}-\d{2}", period):
        return None
    year = int(period[:4])
    month = period[5:7]
    prior = f"{year - 1}-{month}"

    def _one(ym: str) -> float | None:
        hit = _value_lookup(
            conn,
            shards,
            entity=entity,
            period=ym,
            metric=metric,
            attrs_contains=attrs_contains,
            prefer_doc_ids=prefer_doc_ids,
        )
        if hit is None or hit.is_na or not isinstance(hit.value, (int, float)):
            return None
        return float(hit.value)

    cur = _one(period)
    old = _one(prior)
    if cur is None or old is None:
        return None
    ratio, surface = module_diff(cur, old, as_ratio=True)
    return StructuredQueryHit(
        value=ratio,
        surface=surface,
        op="diff",
        rows_touched=2,
        period=period,
        metric=metric or "",
        entity_key=entity or "",
        extras={
            "source": "module_yoy",
            "current": cur,
            "baseline": old,
            "baseline_period": prior,
        },
    )
