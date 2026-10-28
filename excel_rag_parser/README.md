# excel-rag-parser

生产级 **Excel / CSV 表格解析器**：把「多 Sheet、多级表头、合并单元格、多段堆叠表、夹杂说明文字」的
电子表格清洗成 RAG 可入库的分块（Markdown / JSON / 文本）。

## 它能做什么

- **多格式**：`.xlsx` / `.xlsm` / `.xltx` / `.xltm`（openpyxl）、`.xls`（xlrd）、`.xlsb`（pyxlsb）、`.csv` / `.tsv`
- **合并单元格**：按 `merged_cells` 精确回填（向下 + 向右），不靠 `ffill` 猜
- **多级表头**：纵向拼接成平铺列名（`2025年_销售额`），支持多行表头
- **多段表**：一个 Sheet 里上下堆叠的多张表会被切成独立表段，每段用自己的表头
- **说明文字剥离**：标题/备注/说明单独成 `text` 块，不污染表格
- **数字隔离**：数值/财务表 → JSON 记录（`retrieval=sql`，供 Text-to-SQL）；分析/文本表 → Markdown
- **按字符预算切块**：不是按行数，长表按 `max_chunk_chars` 切片且每块强制带表头
- **稳定 `chunk_id`**：同一文件重复解析得到同一批 id，支持幂等 upsert
- **生产健壮**：错误分类 + 结构化日志 + 单 Sheet 失败不拖垮整簿 + `data_only` 取公式缓存值

## 安装

```bash
pip install -e .                 # 核心（.xlsx/.xlsm + .csv）
pip install -e ".[xls]"          # 追加旧版 .xls
pip install -e ".[xlsb]"         # 追加二进制 .xlsb
pip install -e ".[dev]"          # 开发：pytest
```

## 快速开始

```python
from excel_rag_parser import parse_excel, ParseOptions

chunks = parse_excel("报表.xlsx")
for c in chunks:
    print(c["chunk_id"], c["metadata"]["chunk_type"], c["chunk_text"][:80])

# 调参
chunks = parse_excel("报表.xlsx", ParseOptions(max_chunk_chars=1200, header_rows=2))
```

## CLI

```bash
excel-rag-parser 报表.xlsx                       # jsonl → stdout
excel-rag-parser 报表.xlsx --format json --out out.json
excel-rag-parser 报表.xlsx --max-chunk-chars 1200 --header-rows 2 -v
python -m excel_rag_parser 报表.xlsx             # 等价
```

## 输出契约

每个 chunk 形如：

```json
{
  "chunk_id": "974d6914695b1983ac2b954e",
  "chunk_text": "【Sheet名: 用电分析】【表意: …】\n\n| … |",
  "metadata": {
    "schema_version": "1.0",
    "source_file": "报表.xlsx",
    "sheet_name": "用电分析",
    "chunk_type": "markdown_table | markdown_table_slice | numeric_records | text",
    "row_range": "4-23",
    "retrieval": "vector | sql",
    "section_index": 0,
    "columns": ["用量 日期", "2026-09-30", "…"],
    "table_title": "…",
    "row_count": 20,
    "slice_index": 0,
    "record_count": 6,
    "purpose": "Text-to-SQL 结构化数据，不进向量检索"
  }
}
```

- `retrieval="vector"`：进向量库（Markdown / 文本）
- `retrieval="sql"`：**不进向量库**，入库成 SQL 表走 text-to-SQL
- `chunk_id = sha256(source_file, sheet_name, row_range, chunk_type, text)[:24]`

## 架构

```
readers.py   ── 多格式 → SheetGrid（值网格 + 合并区 + 日期归一）
grid.py      ── 值/行分类、合并填充
layout.py    ── 说明剥离 / 表头深度 / 多段切分 / 列折叠 / 数值表判定
chunker.py   ── Markdown 切片 / JSON / 文本（按字符预算）
parser.py    ── 编排
schema.py    ── 输出契约 + 稳定 chunk_id
```

## 已知限制

- `.xlsb`：pyxlsb **不公开合并区**，因此 xlsb 不做合并单元格回填；合并密集的 xlsb 建议先转 xlsx。
- `.csv`：无类型信息，全部按文本处理（不做数字推断，避免 `0012345` 丢前导零）。
- 表头/表段边界是启发式：极端模板请显式传 `header_rows`。
- General 格式日期序列号用「整数 + `[date_serial_min, date_serial_max]`」猜测，数量恰好落区间的表可能误判（可关掉或调区间）。

## 开发

```bash
pip install -e ".[dev]"
pytest -q
```
