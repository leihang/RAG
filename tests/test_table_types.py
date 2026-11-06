"""表型分类器 A–E（电子表格分流 Phase 0）。"""

from __future__ import annotations

from ekbs.ingest.table_serialize import detect_month_series_layout, serialize_information_blocks
from ekbs.ingest.table_types import classify_table_type, infer_year_from_source


def test_classify_month_series_as_a() -> None:
    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "3月", "4月", "5月", "6月", "9月"],
        ["A-1", "海豚电竞", "2026年", "1", "2", "3", "4", "5", "6", "131120"],
    ]
    assert classify_table_type(caption="大户用电监视", rows=rows) == "A"


def test_classify_wide_water_bill_as_e() -> None:
    """重复指标组宽账单优先标 E，不误判成 B。"""
    headers = [
        "序号",
        "水卡号",
        "用途",
        "上月起度/2024-01-01",
        "本月止度/2024-01-01",
        "用量/2024-01-01",
        "金额/2024-01-01",
        "上月起度/2024-02-01",
        "本月止度/2024-02-01",
        "用量/2024-02-01",
        "金额/2024-02-01",
    ]
    rows = [headers, ["1", "30005", "绿化", "100", "110", "10", "30", "110", "125", "15", "45"]]
    assert classify_table_type(caption="水费账单", rows=rows) == "E"


def test_classify_fee_detail_as_b() -> None:
    rows = [["户号", "月份", "费用项", "金额"]] + [
        [f"H{i:04d}", "2026-09", "电费", str(100 + i)] for i in range(80)
    ]
    assert classify_table_type(caption="收费明细", rows=rows) == "B"


def test_classify_financial_as_c() -> None:
    rows = [
        ["科目", "本期金额", "本年累计"],
        ["营业收入", "1000", "12000"],
        ["营业成本", "600", "7200"],
        ["营业成本合计", "600", "7200"],
    ]
    assert classify_table_type(caption="利润表", rows=rows) == "C"


def test_classify_ledger_as_d() -> None:
    rows = [
        ["合同号", "乙方", "状态", "负责人"],
        ["HT-001", "甲公司", "在用", "张三"],
        ["HT-002", "乙公司", "停用", "李四"],
    ]
    assert classify_table_type(caption="合同台账", rows=rows) == "D"


def test_classify_unknown_small_table() -> None:
    rows = [["说明"], ["随便写点什么"]]
    assert classify_table_type(caption="备注", rows=rows) == "unknown"


def test_month_series_without_year_column_does_not_melt_by_default() -> None:
    """安全默认：无年列 → 不熔（即使文件名含年份也不自动推断）。"""
    rows = [
        ["铺号", "用户名", "1月", "2月", "3月", "4月", "5月", "6月", "9月"],
        ["A-1", "甲商户", "10", "20", "30", "40", "50", "60", "100"],
    ]
    assert detect_month_series_layout(rows[0], rows[1:], caption="大户") is None
    assert (
        detect_month_series_layout(
            rows[0], rows[1:], caption="大户", default_year="2026"
        )
        is not None
    )


def test_infer_year_from_filename() -> None:
    assert infer_year_from_source(source_path="/data/2026年大户用电.xlsx") == "2026"
    assert infer_year_from_source(source_path="/data/用电.xlsx") is None


def test_month_series_with_default_year_melts() -> None:
    rows = [
        ["铺号", "用户名", "1月", "2月", "3月", "4月", "5月", "6月", "9月"],
        ["A-1", "海豚电竞", "10", "20", "30", "40", "50", "60", "131120"],
    ]
    leaves = serialize_information_blocks(
        caption="大户用电",
        rows=rows,
        row_chunks=True,
        default_year="2026",
    )
    hit = next(
        b
        for b in leaves
        if "用户名=海豚电竞" in b and "月份=9" in b and "用电量=131120" in b
    )
    assert "年度=2026" in hit
