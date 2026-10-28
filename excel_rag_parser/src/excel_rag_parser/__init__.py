"""excel_rag_parser —— 生产级 Excel/CSV 表格解析器 → RAG 分块。

用法::

    from excel_rag_parser import parse_excel, ParseOptions

    chunks = parse_excel("报表.xlsx")
    for chunk in chunks:
        print(chunk["chunk_id"], chunk["metadata"]["chunk_type"], chunk["chunk_text"][:80])
"""

from __future__ import annotations

__version__ = "1.0.0"

from .exceptions import (  # noqa: E402
    CorruptFileError,
    EmptyWorkbookError,
    ExcelRagParserError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from .options import ParseOptions  # noqa: E402
from .parser import parse_excel  # noqa: E402
from .readers import SUPPORTED_SUFFIXES  # noqa: E402
from .schema import SCHEMA_VERSION  # noqa: E402

__all__ = [
    "parse_excel",
    "ParseOptions",
    "ExcelRagParserError",
    "UnsupportedFormatError",
    "CorruptFileError",
    "MissingDependencyError",
    "EmptyWorkbookError",
    "SUPPORTED_SUFFIXES",
    "SCHEMA_VERSION",
    "__version__",
]
