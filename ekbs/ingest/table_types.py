"""电子表格 ``table_type`` 启发式分类器（A–E / unknown）。

不调 LLM：同表同结果、可单测、入库路径零出网。优先复用
:func:`ekbs.ingest.table_serialize.detect_month_series_layout`（A）与
:func:`ekbs.ingest.table_serialize._is_wide_metric_table`（E）；B/C/D 用轻量
列名/行数启发，未命中一律 ``unknown``（只走 chunks，不写观测表）。
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from ekbs.ingest.table_serialize import (
    _is_wide_metric_table,
    _prepare_information_blocks,
    coerce_rows_with_headers,
    detect_month_series_layout,
)

TableType = Literal["A", "B", "C", "D", "E", "unknown"]

_AMOUNT_HEADER_RE = re.compile(
    r"(金额|费用|单价|用量|数量|读数|起度|止度|合计|小计|余额|欠费)"
)
#: 台账主键：不含「户号/账号」（那些是收费明细 B 的身份列）
_LEDGER_KEY_RE = re.compile(
    r"(合同号|合同编号|资产编号|流水号|单号|工单号|设备编号|资产号)"
)
_ACCOUNT_HEADER_RE = re.compile(
    r"(户号|户名|账号|客户号|水卡|用户编号|表号|费用承担对象|承担对象)"
)
_PERIOD_COL_RE = re.compile(r"(月份|账期|期间|年月|所属期|抄表月|抄表日期)")
#: 户内抄表明细：按名取列，列数常 >15；不得因列宽被踢出收费明细。
_METER_BEARER_RE = re.compile(r"(费用承担对象|承担对象)")
_METER_USAGE_RE = re.compile(r"(合计用量|用量)")
_METER_PRICE_RE = re.compile(r"(单价)")
_FINANCIAL_CAPTION_RE = re.compile(
    r"(利润表|资产负债表|损益表|现金流量表|财报|财务报表|科目余额)"
)
_TOTAL_CELL_RE = re.compile(r"(合计|小计|总计)")
_PERIOD_HEADER_RE = re.compile(
    r"(本期|上期|本年|去年|期末|期初|本月|上月|同比|环比|20\d{2})"
)
_YEAR_IN_PATH_RE = re.compile(r"(20\d{2})\s*年?")


def infer_year_from_source(
    *,
    source_path: str = "",
    caption: str = "",
) -> str | None:
    """从文件名/题注提取四位年份；供 ``month_series_infer_year_from_filename`` 使用。

    默认配置下**不会**自动调用；缺年列时仍不熔。
    """
    for text in (source_path, caption):
        if not text:
            continue
        # 取路径最后一段，避免目录名里的年份抢先
        leaf = str(text).replace("\\", "/").rsplit("/", 1)[-1]
        match = _YEAR_IN_PATH_RE.search(leaf)
        if match:
            return match.group(1)
    return None


def classify_table_type(
    *,
    caption: str = "",
    body: str = "",
    rows: Sequence[Sequence[str]] | None = None,
    header_row_count: int = 0,
) -> TableType:
    """对一张表给出 ``A|B|C|D|E|unknown``。

    优先级：A（月份宽表）→ E（重复指标组宽账单）→ C（财报）→ B（长明细）→ D（台账）
    → unknown。调用方对 unknown **不写**观测行。
    """
    prepared = _prepared_matrix(
        caption=caption, body=body, rows=rows, header_row_count=header_row_count
    )
    if prepared is None:
        return "unknown"
    cap, headers, data_rows = prepared

    if detect_month_series_layout(headers, data_rows, caption=cap) is not None:
        return "A"
    if _is_wide_metric_table(headers):
        return "E"
    if _looks_financial(cap, headers, data_rows):
        return "C"
    if _looks_fee_detail(headers, data_rows):
        return "B"
    if _looks_ledger(headers, data_rows):
        return "D"
    return "unknown"


#: 兼容旧名
classify_table = classify_table_type


def classify_prepared_table(
    *,
    caption: str,
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    source_path: str = "",
) -> TableType:
    """对已规整的表头/数据行分类（与序列化共用同一预处理结果）。"""
    del source_path
    headers_l = [str(h).strip() for h in headers]
    if not headers_l or not data_rows:
        return "unknown"
    if detect_month_series_layout(headers_l, data_rows, caption=caption) is not None:
        return "A"
    if _is_wide_metric_table(headers_l):
        return "E"
    if _looks_financial(caption, headers_l, data_rows):
        return "C"
    if _looks_fee_detail(headers_l, data_rows):
        return "B"
    if _looks_ledger(headers_l, data_rows):
        return "D"
    return "unknown"


def _prepared_matrix(
    *,
    caption: str,
    body: str,
    rows: Sequence[Sequence[str]] | None,
    header_row_count: int,
) -> tuple[str, list[str], list[list[str]]] | None:
    if rows is None:
        if not (body or "").strip():
            return None
        matrix, header_n = coerce_rows_with_headers(body)
        header_row_count = header_n or header_row_count
    else:
        matrix = [list(r) for r in rows]
    prepared = _prepare_information_blocks(
        caption=caption,
        rows=matrix,
        table_id="",
        header_row_count=header_row_count,
    )
    if prepared is None:
        return None
    cap, _preamble, _form, headers, data_rows = prepared
    if not headers or not data_rows:
        return None
    return cap, headers, data_rows


def _looks_financial(
    caption: str,
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
) -> bool:
    if _FINANCIAL_CAPTION_RE.search(caption or ""):
        return True
    period_cols = sum(1 for h in headers if _PERIOD_HEADER_RE.search(str(h)))
    if period_cols < 1 or len(headers) < 2:
        return False
    sample = data_rows[: min(40, len(data_rows))]
    total_hits = sum(
        1
        for row in sample
        if row and _TOTAL_CELL_RE.search(str(row[0]).strip())
    )
    return total_hits >= 1 and period_cols >= 1


def _looks_meter_reading(headers: Sequence[str]) -> bool:
    """户内/能源抄表明细：费用承担对象 + 合计用量 + 单价（按名映射，允许宽表）。"""
    texts = [str(h) for h in headers]
    has_bearer = any(_METER_BEARER_RE.search(h) for h in texts)
    has_usage = any(_METER_USAGE_RE.search(h) for h in texts)
    has_price = any(_METER_PRICE_RE.search(h) for h in texts)
    return has_bearer and has_usage and has_price


def _looks_fee_detail(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
) -> bool:
    """收费明细：少列 + 金额/用量；有户号类列时行数门槛更低。

    户内抄表明细列数常 17+，用签名列识别，**不**走 ``n_cols > 15`` 上限。
    """
    if _looks_meter_reading(headers):
        return len(data_rows) >= 1
    n_rows, n_cols = len(data_rows), len(headers)
    if n_rows < 3 or n_cols > 15 or n_cols < 3:
        return False
    amount_cols = sum(1 for h in headers if _AMOUNT_HEADER_RE.search(str(h)))
    if amount_cols < 1:
        return False
    has_account = any(_ACCOUNT_HEADER_RE.search(str(h)) for h in headers)
    has_period = any(_PERIOD_COL_RE.search(str(h)) for h in headers)
    if has_account or has_period:
        return True
    return n_rows >= 20


def _looks_ledger(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
) -> bool:
    """台账：有合同/资产类主键，列数适中；与收费明细互斥。"""
    if len(data_rows) < 2 or not (2 <= len(headers) <= 20):
        return False
    if _looks_fee_detail(headers, data_rows):
        return False
    has_key = any(_LEDGER_KEY_RE.search(str(h)) for h in headers)
    if not has_key:
        return False
    amount_cols = sum(1 for h in headers if _AMOUNT_HEADER_RE.search(str(h)))
    return amount_cols <= 2
