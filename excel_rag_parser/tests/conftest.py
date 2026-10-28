"""测试夹具：合成复杂 Excel + 定位真实语料。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试在「未安装」时也能直接 import（src 布局）
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# 合成工作簿
# ---------------------------------------------------------------------------

def build_complex_xlsx(path: Path) -> Path:
    """构造一个覆盖「多级表头 + 合并单元格 + 说明文字 + 数值表 + 多段表」的工作簿。"""
    from openpyxl import Workbook

    wb = Workbook()

    # Sheet「销售」：标题 + 说明 + 两行表头（横向合并「2025年」）+ 纵向合并数据 + 尾注
    ws = wb.active
    ws.title = "销售"
    ws["A1"] = "2025年销售统计"
    ws["A2"] = "说明：金额为含税口径，单位万元。"
    ws["A3"] = "大区"
    ws["B3"] = "2025年"
    ws["D3"] = "备注"
    ws.merge_cells("A3:A4")
    ws.merge_cells("B3:C3")
    ws.merge_cells("D3:D4")
    ws["B4"] = "销售额"
    ws["C4"] = "利润"

    ws.merge_cells("A5:A6")
    ws.merge_cells("A7:A8")
    rows = [
        ("华东", 120.5, 30.2, "正常"),
        (None, 98.7, 22.1, "微降"),
        ("华南", 80.0, 18.5, "正常"),
        (None, 65.3, 12.4, "下降"),
    ]
    for i, (region, revenue, profit, note) in enumerate(rows):
        r = 5 + i
        if region is not None:
            ws.cell(r, 1, region)
        ws.cell(r, 2, revenue)
        ws.cell(r, 3, profit)
        ws.cell(r, 4, note)
    ws["A9"] = "注：环比为环比增长率。"

    # Sheet「财务」：纯数值 → JSON
    ws2 = wb.create_sheet("财务")
    ws2["A1"] = "2025年上半年财务指标"
    header = ["月份", "收入(万元)", "成本(万元)", "利润(万元)"]
    for c, name in enumerate(header, 1):
        ws2.cell(2, c, name)
    finance = [
        ("1月", 100.0, 60.0, 40.0),
        ("2月", 120.5, 70.2, 50.3),
        ("3月", 98.7, 55.1, 43.6),
    ]
    for i, row in enumerate(finance):
        for c, value in enumerate(row, 1):
            ws2.cell(3 + i, c, value)

    # Sheet「多段」：上下两张结构不同的表
    ws3 = wb.create_sheet("多段")
    ws3["A1"] = "多段示例"
    ws3["A2"] = "项目"
    ws3["B2"] = "金额"
    ws3["A3"], ws3["B3"] = "甲", 10
    ws3["A4"], ws3["B4"] = "乙", 20
    ws3["A6"] = "第二段"
    ws3["A7"] = "名称"
    ws3["B7"] = "数量"
    ws3["A8"], ws3["B8"] = "丙", 3
    ws3["A9"], ws3["B9"] = "丁", 4

    wb.save(path)
    return path


def build_empty_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "空表"
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# 真实语料
# ---------------------------------------------------------------------------

REAL_XLSX = (
    REPO_ROOT
    / "var/raw/wuye/工程部受控文件L3/2026年9月能耗分析/2026年9月能耗分析.xlsx"
)
REAL_XLS = (
    REPO_ROOT
    / "var/raw/wuye/工程部/1、程序文件/2025版装修施工作业指导书（附件）/支持表格/4.5、商铺交接验收表.xls"
)


@pytest.fixture
def complex_xlsx(tmp_path: Path) -> Path:
    return build_complex_xlsx(tmp_path / "complex.xlsx")


@pytest.fixture
def empty_xlsx(tmp_path: Path) -> Path:
    return build_empty_xlsx(tmp_path / "empty.xlsx")


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sample.csv"
    path.write_text("名称,数量,单价\n甲,3,1.5\n乙,5,2.0\n", encoding="utf-8")
    return path
