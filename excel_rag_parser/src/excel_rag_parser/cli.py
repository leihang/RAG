"""命令行入口：``excel-rag-parser FILE [options]`` 或 ``python -m excel_rag_parser``。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .exceptions import ExcelRagParserError
from .log import configure
from .options import ParseOptions
from .parser import parse_excel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="excel-rag-parser",
        description="把 Excel/CSV 表格解析成 RAG 可入库的分块（Markdown / JSON / 文本）。",
    )
    parser.add_argument("file", type=Path, help="要解析的 Excel/CSV 文件")
    parser.add_argument("--format", choices=("jsonl", "json"), default="jsonl",
                        help="输出格式（默认 jsonl，流式友好）")
    parser.add_argument("--out", type=Path, default=None, help="输出文件；默认 stdout")
    parser.add_argument("--max-chunk-chars", type=int, default=2000,
                        help="单块字符预算（默认 2000）")
    parser.add_argument("--min-slice-rows", type=int, default=3,
                        help="长表切片每片最少行数（默认 3）")
    parser.add_argument("--numeric-column-ratio", type=float, default=0.7,
                        help="数值列占比阈值（默认 0.7）")
    parser.add_argument("--header-rows", type=int, default=None,
                        help="显式表头行数（覆盖启发式）")
    parser.add_argument("--force-all-text", action="store_true",
                        help="关闭数字隔离，全部走 Markdown（调试）")
    parser.add_argument("--no-text-notes", action="store_true",
                        help="不输出说明/备注文本块")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(logging.DEBUG if args.verbose else logging.INFO)

    try:
        options = ParseOptions(
            header_rows=args.header_rows,
            max_chunk_chars=args.max_chunk_chars,
            min_slice_rows=args.min_slice_rows,
            numeric_column_ratio=args.numeric_column_ratio,
            force_all_text=args.force_all_text,
            include_text_notes=not args.no_text_notes,
        )
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    try:
        chunks = parse_excel(args.file, options)
    except (ExcelRagParserError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = json.dumps(chunks, ensure_ascii=False, indent=2)
    else:
        payload = "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks)

    if args.out is not None:
        args.out.write_text(payload + "\n", encoding="utf-8")
        print(f"已写出 {len(chunks)} 块 → {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
