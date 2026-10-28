"""真实语料回归（语料不存在时自动跳过）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from excel_rag_parser import ParseOptions, parse_excel
from excel_rag_parser.readers import read_workbook

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_XLSX = (
    REPO_ROOT
    / "var/raw/wuye/工程部受控文件L3/2026年9月能耗分析/2026年9月能耗分析.xlsx"
)
REAL_XLS = (
    REPO_ROOT
    / "var/raw/wuye/工程部/1、程序文件/2025版装修施工作业指导书（附件）/支持表格/4.5、商铺交接验收表.xls"
)


@pytest.mark.skipif(not REAL_XLSX.is_file(), reason="真实 .xlsx 语料不存在")
def test_real_energy_analysis_splits_sections() -> None:
    chunks = parse_excel(REAL_XLSX)
    dian = [c for c in chunks if c["metadata"]["sheet_name"] == "用电分析"]
    sections = {
        c["metadata"].get("section_index") for c in dian if c["metadata"].get("columns")
    }
    assert len(sections) >= 3

    def text_of(index: int) -> str:
        return "\n".join(
            c["chunk_text"] for c in dian if c["metadata"].get("section_index") == index
        )

    assert "总电量" in text_of(0)
    assert "总电费" in text_of(1)
    assert "总电费" not in text_of(0)


@pytest.mark.skipif(not REAL_XLS.is_file(), reason="真实 .xls 语料不存在")
def test_real_xls_is_readable() -> None:
    grids = read_workbook(REAL_XLS, ParseOptions())
    assert grids
    assert grids[0].name
