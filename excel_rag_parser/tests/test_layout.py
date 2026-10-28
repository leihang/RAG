from __future__ import annotations

from excel_rag_parser.grid import MergeRange, SheetGrid, fill_merged
from excel_rag_parser.layout import (
    analyze_sheet,
    collapse_identical_columns,
    flatten_header,
    is_numeric_table,
)
from excel_rag_parser.options import ParseOptions


def test_fill_merged_vertical() -> None:
    grid = SheetGrid("t", [["区域", None, None], [None, 1, 2], [None, 3, 4]],
                     [MergeRange(0, 0, 2, 0)])
    filled = fill_merged(grid)
    assert filled.rows[1][0] == "区域"
    assert filled.rows[2][0] == "区域"


def test_flatten_header_multilevel() -> None:
    header = [
        ["大区", "2025年", "2025年", "备注"],
        ["大区", "销售额", "利润", "备注"],
    ]
    assert flatten_header(header, [0, 1, 2, 3]) == [
        "大区", "2025年_销售额", "2025年_利润", "备注",
    ]


def test_flatten_header_dedupes_same_names() -> None:
    header = [["金额", "金额"]]
    assert flatten_header(header, [0, 1]) == ["金额", "金额_2"]


def test_collapse_identical_columns() -> None:
    data = [["甲", "同一段分析", "同一段分析"], ["乙", "另一段", "另一段"]]
    merges = [MergeRange(1, 1, 1, 2)]  # 末级表头跨 col1..col2
    assert collapse_identical_columns([0, 1, 2], data, merges, last_header_row=1) == [0, 1]


def test_is_numeric_table_true() -> None:
    options = ParseOptions()
    rows = [[100.0, 60.0, 40.0], [120.0, 70.0, 50.0]]
    assert is_numeric_table(rows, ["收入(万元)", "成本(万元)", "利润(万元)"], options) is True


def test_is_numeric_table_false_for_analysis() -> None:
    options = ParseOptions()
    rows = [[1, 100, "同比-10%，分析原因：天气转凉，商铺空调使用率下降。"]]
    assert is_numeric_table(rows, ["类别", "用量", "分析"], options) is False


def test_analyze_sheet_splits_stacked_tables() -> None:
    rows = [
        ["多段示例", None, None],
        ["项目", "金额", None],
        ["甲", 10, None],
        ["乙", 20, None],
        [None, None, None],
        ["第二段", None, None],
        ["名称", "数量", None],
        ["丙", 3, None],
    ]
    layout = analyze_sheet(SheetGrid("多段", rows, []), ParseOptions())
    assert len(layout.sections) == 2
    assert layout.sections[0].data_start == 2
    assert layout.sections[1].header_start == 6
